# ADR-012 — Thinking: save, stream and control the model's reasoning

- **Status:** Accepted
- **Date:** 2026-11-20
- **Deciders:** project owner

## Context
ADR-003 step 18. Reasoning models (Claude, GPT-5, Gemini, DeepSeek R1, Qwen, …)
think before they answer. Today `openai_compat.py` reads only `delta.content`
and `delta.tool_calls`, so all of that is lost:

- **Thinking is dropped.** The reasoning text the provider streams is never
  shown, saved or sent back.
- **No control.** There is no way to ask for more, less or no thinking. Each
  model uses its default.
- **Tool calls can break.** Some models sign their reasoning, and when the reply
  calls a tool they need that signed reasoning sent back on the next turn.
  Without it the model loses its train of thought, and some providers refuse
  the request.

What we already have:

- **One provider:** OpenRouter through the Chat Completions adapter. OpenRouter
  puts every vendor's reasoning into one shape: a `reasoning` request field
  (`{"effort": "low" | …}`), `delta.reasoning` text in the stream, and
  `delta.reasoning_details` (structured blocks, some signed or encrypted).
- **A model catalog** (ADR-005) built from OpenRouter's `/models`, whose
  `supported_parameters` lists `"reasoning"` for models that can think.
- **Streaming events:** the provider yields `TextDeltaEvent`s, the loop wraps them
  in `MessageUpdateEvent`, `ActiveRun.track` (`runs.py`) builds the in-progress
  reply for clients that join late, and the web app's `use-agent-run.ts` adds
  each delta to a draft.
- **Session settings** with a switch route: `PATCH /sessions/{id}` changes the
  model and saves a notice (ADR-008).

How pi does it:

- **A content block.** `ThinkingContent` (`ai/src/types.ts:436`) holds
  `thinking` (the text), `thinkingSignature` (opaque data for sending it back)
  and `redacted`. An `AssistantMessage.content` is
  `(TextContent | ThinkingContent | ToolCall)[]`.
- **Levels.** `ThinkingLevel` is `"off" | "minimal" | "low" | "medium" | "high" |
  "xhigh" | "max"` (`agent/src/types.ts:349`), `"off"` by default. The agent
  passes it as `reasoning` on each call (`agent.ts:480`). The coding agent saves
  a `thinking_level_change` entry when it changes (`session-manager.ts:70`).
  `clampThinkingLevel` (`models.ts:1315`) moves a level a model can't do to the
  nearest one it can.
- **Request.** For OpenRouter, `openai-completions.ts:929` sends
  `reasoning: {effort}`, and `{effort: "none"}` when off.
- **Stream.** It reads the first non-empty of `reasoning_content`, `reasoning`
  and `reasoning_text` (`openai-completions.ts:584`), and emits
  `thinking_start` / `thinking_delta` / `thinking_end` events. The thinking block
  goes before the text block. `reasoning_details` are collected on the side and
  stored as JSON in `thinkingSignature`; they are not shown.
- **Sending it back.** When building the next request (`openai-completions.ts:1305`)
  it sends `reasoning_details` back when it has them, otherwise the text under the
  field name it arrived in.
- **Compaction** counts thinking toward the token estimate (`compaction.ts:324`)
  but leaves it out of the text it summarizes (`utils.ts:115`).

## Decision
**1. A thinking block.** `ai/messages.py` gains `ThinkingContent`:

- `type: "thinking"`;
- `thinking`: the reasoning text (empty when the provider sent only encrypted
  details);
- `signature`: what is needed to send it back, or `None`. It holds either the
  wire field name the text came in (`"reasoning"`, `"reasoning_content"`) or the
  `reasoning_details` list as JSON, as pi does.

`AssistantMessage.content` becomes `TextContent | ThinkingContent | ToolCall`,
and a `thinking()` method joins the thinking text. `text()` stays answer-only,
so compaction and titles don't pick up reasoning. Old rows still parse: a new
union member breaks nothing.

`Usage` gains `reasoning: int = 0`, from OpenRouter's
`completion_tokens_details.reasoning_tokens`. It is part of `output`, not added
to it.

**2. A per-session thinking level.**

- `ThinkingLevel = Literal["off", "minimal", "low", "medium", "high"]`, `"off"` by
  default as in pi. `xhigh` and `max` are left out until a model we use needs them.
- A `sessions.thinking_level` column (migration, existing rows get `"off"`).
  `POST /sessions` takes it, and `PATCH /sessions/{id}` can change it next to
  `model`. As with a model switch, it is refused with 409 while a run is going,
  and a real change saves a notice ("Thinking: high").
- `Context.thinking_level` carries it to the provider for every call, compaction's
  summary call included.

**3. The request.** When the level is not `"off"`, the adapter sends
`"reasoning": {"effort": level}`. `"off"` sends nothing, so the model uses its
default. pi sends `effort: "none"` for off instead; we don't, because models that
always think refuse `"none"`. A model that thinks by default still has its
thinking shown.

**4. The stream.**

- A new `ThinkingDeltaEvent(delta)` joins `AssistantEvent` and
  `MessageUpdateEvent.event`.
- The adapter reads the first non-empty of `delta.reasoning_content`,
  `delta.reasoning` and `delta.reasoning_text`, and adds `delta.reasoning_details`
  to a list. Thinking is read before `content` in each chunk, so the thinking
  block comes first.
- At the end, the thinking block is put before the text and the tool calls.
  The details list, when there is one, wins as the `signature`.
- On an error, thinking that arrived is kept, like partial text today.
- `ActiveRun.track` grows the in-progress reply's thinking block the same way it
  grows text, so a client that reconnects sees it.

**5. Sending it back.** `to_wire_messages` adds `reasoning_details` to an
assistant message whose thinking `signature` is a details list. If the signature
is a field name, it sends the text back under that name. Thinking with no
signature is not sent. The signature is sent only when the reply came from the
same provider and model as the request: signed reasoning from another model is
rejected or meaningless. `convert_to_llm` and `prepare_transcript` are unchanged:
the thinking block is part of the assistant message.

**6. Compaction.** The token estimate counts thinking characters. The
summarized text leaves thinking out, as in pi.

**7. The web app.**

- A thinking-level picker next to the model picker, shown only when the
  catalog says the model can reason. `ModelInfo` gains `reasoning: bool` from
  `supported_parameters`.
- Streamed thinking goes in its own draft field. A reply shows it as a
  collapsed "Thinking" section above the answer, open while it streams.

**Not built:** pi's level clamping per model, `xhigh`/`max`, token budgets per
level, redacted-thinking display, and the other vendor formats in pi's
`thinkingFormat` (Z.ai, Qwen, …); OpenRouter covers them for now. Changing the
level per turn is step 19 (`prepare_request`).

## Alternatives considered
- **Show thinking but don't save it** — less to store, but the signed reasoning
  is lost, and a model that called a tool can't pick up its reasoning on the
  next turn.
- **Thinking as a separate app message (ADR-008)** — keeps `AssistantMessage`
  unchanged, but thinking belongs to one reply and has to travel with it when
  it is sent back. pi keeps it as a content block.
- **`effort: "none"` for off, as pi does** — really turns thinking off where it
  can, but fails on models that always think. Could be added later as a
  separate `"none"` level.
- **A thinking budget in tokens instead of levels** — exact, but every model
  has different limits. OpenRouter maps effort levels to budgets for us.

## Consequences
- Every place that walks `AssistantMessage.content` has to handle a third block
  type: the adapter, `runs.py`, compaction, the web chat. Code that uses
  `text()` and `tool_calls()` is unchanged.
- Thinking tokens cost money and count toward the context window. `"off"` by
  default keeps today's cost unless a session asks for more.
- Stored thinking grows the database. Thinking is never deleted, the same as
  any message (ADR-009).
- The `signature` format is OpenRouter's. The Anthropic adapter (step 20) will
  have its own signed thinking and must read and write its own shape. A signature
  from one provider is not sent to another; a model switch across providers
  sends only the text, if anything.
- Step 19 (`prepare_request`) can change `thinking_level` per call, because it is
  on `Context` and not fixed inside the adapter.
