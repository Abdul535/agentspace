# ADR-018 — Langfuse tracing, built from the loop's events

- **Status:** Accepted
- **Date:** 2026-11-21
- **Deciders:** project owner

## Context
ADR-014 step 35 asks for "a trace per run: each model call (model, tokens, cost,
latency) and each tool call, viewable later, with an optional export to a
tracing tool such as Langfuse". The owner wants Langfuse now, to see how the
agent performs. Step 35 lists step 34 (usage and cost) as a dependency. Step 34
isn't built, and the owner chose to go ahead without it.

pi (`../pi-upstream`), read for this ADR:

- `packages/telemetry/`: a vendor-neutral contract. A `TelemetryContext` starts
  spans, a span holds attributes, events and a status, and child spans nest under
  their parent. The default is `NOOP_TELEMETRY_CONTEXT`. pi ships no exporter:
  the app plugs in an adapter (OpenTelemetry, Sentry, logs).
- `packages/ai/src/telemetry.ts`: one `pi.ai.request` span per model request,
  with provider, model, reasoning level, usage, cost, stop reason and response
  id, plus a `pi.ai.first_chunk` event and duration metrics. Content is never
  recorded, only block kinds and lengths.
- `packages/durable/src/harness/telemetry.ts`: spans for durable task phases and
  tool calls.

Langfuse's Python SDK (v5) is built on OpenTelemetry. `start_observation`
opens a typed observation (`agent`, `generation`, `tool`, `span`) that stays
open until `.end()`, can be nested under a parent explicitly, and is sent in
batches from a background thread. `propagate_attributes` puts a `session_id` on
the trace, so Langfuse groups a chat's runs together. It is an observability
client, not an agent framework, so the no-framework rule allows it.

## Decision
- **Tracing listens to the loop's events (ADR-006), and the loop doesn't
  change.** `RunManager.drive`'s `emit` already sees every event: it saves the
  message, then `run.track`s it. Now it also calls `run.trace.observe(event)`
  before publishing. The `agent_end` that `finish` writes for a crashed or
  cancelled run is observed too, and `finish` calls `run.trace.end()`. The
  system prompt is in no event and is built per loop pass (after MCP tools
  join), so `stream_run` hands it over with `run.trace.set_system_prompt` just
  before calling the loop.
- **One trace per run** (`agentspace/tracing.py`), grouped by session:
  - `run` (agent): input is the first user message, output is the last
    answer, and metadata holds the end reason. An error end is marked `ERROR`.
    It closes when the run finishes, not at `agent_end`, because an overflow
    retry (ADR-009) ends the loop twice.
  - `model` (generation), one per assistant reply, from `message_start` to
    `message_end`. It records the model, what the model was sent (the system
    prompt as a first `system` message, then the transcript through
    `convert_to_llm` + `strip_images`, so no base64), the reply content, token
    usage (`input`, `output`, `total`; reasoning tokens go in metadata because
    `output` already counts them), stop reason, and time to first token (the
    first `message_update`). An `error` stop is marked `ERROR`.
  - `<tool name>` (tool), one per tool call, from `tool_execution_start` to
    `tool_execution_end`: arguments in, result text out, `ERROR` on
    `is_error`. Tools are keyed by call id, so parallel tools (step 9) overlap
    correctly.
  - `compaction` (span), from `compaction_start` to `compaction_end`, marked
    `ERROR` when compaction failed.
  - Observations still open when the run ends (cancelled, crashed) are closed
    as `WARNING`.
- **Off unless configured, and it can never hurt a run** (the owner's
  prediction, and pi's no-op default). `Tracer.from_settings` makes a client
  only when both `LANGFUSE_PUBLIC_KEY` and `LANGFUSE_SECRET_KEY` are set.
  Otherwise each run gets the base `RunTrace`, whose methods do nothing. Every
  tracing call is wrapped: a failure logs a warning and the run goes on. The SDK
  sends from its own thread. Shutdown flushes after runs stop, through
  `asyncify`, because the flush blocks on the network.
- **Langfuse Cloud by default.** `LANGFUSE_BASE_URL` defaults to the EU cloud
  (`https://cloud.langfuse.com`). Set it to `https://us.cloud.langfuse.com`, or
  to a self-hosted URL, and no code changes.
- **No cost yet.** We send token counts, and Langfuse prices well-known model
  names from its own price table. Step 34 will send our own cost
  (`cost_details`) from the catalog.

## Alternatives considered
- **Calls inside the loop and the provider adapter**, as pi does with
  `observeModelRequest`. This gives exact request timing (including auth and
  setup) and covers model calls outside a run, such as compaction's summary
  call and title generation. But the loop would have to carry a tracing context
  everywhere, while the events already have everything we need. If those other
  calls need tracing later, the provider can get a span of its own then.
- **pi's vendor-neutral contract, with Langfuse as one adapter.** That's the
  right shape for a library many apps embed. We are one app with one backend,
  and `RunTrace` is already the seam: another backend would be another subclass.
- **Our own HTTP client for Langfuse's ingestion API** (httpx is already a
  dependency). This would need no new package, but we would rewrite the
  batching, retries and the background sender, and that API is the legacy path
  next to the OTel one.
- **Recording no content, as pi does.** That is safer for a library. Here the
  owner wants to see what the model was sent and what it answered, and the
  traces go to their own Langfuse project. Images are stripped to keep traces
  small.

## Consequences
- Setting two keys in `apps/api/.env` turns on a full trace per run in
  Langfuse, with a session view per chat. No keys means no change and no
  network calls.
- Prompts, replies and tool outputs (file contents, command output) are sent to
  Langfuse. Don't turn it on for a workspace whose data shouldn't leave the
  machine, or point `LANGFUSE_BASE_URL` at a self-hosted instance.
- Each model observation carries the whole transcript, so long sessions send a
  lot of data. If that becomes a problem, record only the new messages.
- Model calls outside a run (compaction's summary request, titles) aren't
  observed as generations yet. The compaction span shows how long it took.
- Step 34 fills in exact OpenRouter costs. The observation is already in place
  to carry them.
