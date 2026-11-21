# ADR-014 — Roadmap part two: safety, reliability and bigger agents

- **Status:** Proposed
- **Date:** 2026-11-20
- **Deciders:** project owner

## Context
ADR-003 lists 25 steps toward pi's loop and tools; 18 are done and step 19 has
its ADR (ADR-013). Its list ends at Phase 5 (tool changes, skills, MCP,
branching). Reading pi again (`../pi-upstream` at `9cd49d2`, 2026-11-16) and
opencode (`../opencode`) shows features ADR-003 never listed:

- **pi's coding agent** (`packages/coding-agent/src/core/`): auto-retry
  (`retry` in `settings-schema.ts`, `auto_retry_*` events in
  `agent-session.ts`), cost on every reply (`cost` in `ai/src/types.ts`,
  `usage-totals.ts`), prompt caching (`cacheRetention`, `cache-stats.ts`),
  context files like `AGENTS.md` (`resource-loader.ts`), project trust
  (`trust-manager.ts`, `docs/security.md`), prompt templates and slash commands
  (`prompt-templates.ts`, `slash-commands.ts`), session export
  (`session-export.ts`, `export-html/`), virtual models routed per request
  (`virtual-models.ts`, `docs/virtual-models.md`), nested tool calls
  (`nested-tool-calls.ts`).
- **New pi packages:** `durable` (runs saved step by step so a crash resumes
  mid-tool, tools that "park" until a person or webhook answers, subagents and
  child tasks, `tool-search`, watches that resume from a position),
  `codemode` (model-written scripts that call tools in a sandbox), `mcp`
  (with OAuth), `telemetry`.
- **opencode** (`packages/core/src/`): `permission.ts` (ask before a tool runs),
  `snapshot.ts` (undo file changes), `model-retry.ts`, `instruction-discovery.ts`,
  `command.ts`, `tool/todowrite.txt`, `websearch.ts`, `tool/html-markdown.ts`,
  `job.ts` (background jobs), `automation.ts` (scheduled runs), `artifact.ts`.

The owner also listed concepts from production agent platforms to cover:
async tasks, event replay without duplicates, runs across servers, sandboxes,
background subagents with fan-in, playbooks, scheduling, provider failover, MCP
authorization, browser automation, observability, multimodal output and a
versioned workspace. They are slotted into the phases below.

ADR-003 also leaves gaps it names but never schedules: bash "needs step 7
(permissions) or a sandbox before anyone else can reach the app", and "the API
has no auth or Host check yet".

## Decision
ADR-003's list continues here, with the same rules: one step per change, read
the named files first, a step that changes the architecture gets its own ADR,
and a step is ticked here with a short "Built as follows" when it lands.
ADR-003 stays as it is for steps 1–25.

### Phase 6 — Safety (before anyone else can reach the app)
26. [ ] **Tool approval.** A `before_tool_call` policy that pauses a call and
    asks the user: allow once, allow always for this session, or deny (the model
    gets the reason). An approval card in the chat, an API route to answer it,
    and per-tool rules (`bash` asks, `read` doesn't). The run waits on the
    answer, so it needs background runs (step 13, done).
    *pi:* durable's "Gates before a tool runs" (`durable/README.md`).
    *opencode:* `permission.ts`, `permission/`.
27. [x] **API auth and Host check.** A token on every API call and a Host /
    Origin allowlist, so a page in another tab can't drive the API (DNS
    rebinding, ADR-003 step 3). Not in pi (it is local-only); ours is a server.
28. [ ] **Sandbox.** Run tools in an isolated machine (Daytona; see
    `apps/api/scripts/spike_daytona.py`) behind the workspace interface
    (ADR-004), so `bash` no longer runs on the host with the API's permissions.
    Opt-in per session at first.
    *pi:* `docs/containerization.md`, `durable/src/env/`.
29. [ ] **Context files.** Read `AGENTS.md` (and `CLAUDE.md`) from the workspace
    into the system prompt's preamble, with a size cap.
    *pi:* `resource-loader.ts`, project trust in `docs/security.md`.
    *opencode:* `instruction-discovery.ts`.

### Phase 7 — Reliability and cost
30. [ ] **Auto-retry.** Retry a reply that failed for a passing reason (rate
    limit, 5xx, dropped connection) with backoff and a cap, and stream
    `auto_retry_start` / `auto_retry_end` so the UI shows "retrying in 4 s".
    Overflow stays with compaction (ADR-009); other errors stay final.
    *pi:* `retry` settings, `isRetryableAssistantError`, `agent-session.ts`.
    *opencode:* `model-retry.ts`.
31. [ ] **Provider failover.** When retries on one provider run out, try the
    same request on a backup model or provider from a list. Needs step 20 for a
    second provider. *pi:* `model-resolver.ts`. *opencode:* `model-retry.ts`.
32. [ ] **Event replay without duplicates.** Number each run event. A client
    that reconnects sends the last number it saw (SSE `Last-Event-ID`) and gets
    only what it missed, never an event twice. Today a reconnect gets a fresh
    snapshot of the reply instead (ADR-007).
    *pi:* durable's "Watching a Conversation" (`from` on a watch).
33. [ ] **Runs across several workers.** Move the run lock and the event fan-out
    out of one process (a DB lock, then a shared stream such as Redis Streams),
    so the API can run with several workers. Deferred by ADR-007; supersedes part
    of it. Needs step 32.
34. [x] **Usage and cost.** Price each reply from the catalog (OpenRouter's
    `pricing`, or its billed cost when sent), save it on `Usage`, and show a
    per-session total in the UI.
    *pi:* `cost` in `ai/src/types.ts`, `usage-totals.ts`.
    Built as follows (ADR-019): the adapter estimates from the catalog price on
    `Context`, then uses OpenRouter's billed `usage.cost` when sent; the chat
    header sums the transcript; Langfuse gets it as `cost_details`.
35. [x] **Observability.** A trace per run: each model call (model, tokens,
    cost, latency) and each tool call, viewable later, with an optional export
    to a tracing tool such as Langfuse. Needs step 34.
    *pi:* `packages/telemetry`, `core/telemetry.ts`.
    Built as follows (ADR-018), before step 34 at the owner's choice:
    - `agentspace/tracing.py` turns the loop's events into one Langfuse trace per
      run: `model` generations (tokens, time to first token, input and output),
      tool observations, compaction spans, all under a `run` root grouped by
      session. Fed from `RunManager.drive`'s `emit`; the loop is unchanged.
    - Off unless `LANGFUSE_PUBLIC_KEY` and `LANGFUSE_SECRET_KEY` are set; tracing
      errors are logged, never raised. Step 34 (ADR-019) now sends our cost.
36. [ ] **Prompt caching.** Mark the stable start of the request (system prompt,
    tools, older turns) as cacheable, and count cache reads/writes in `Usage`.
    Needs step 20 (Anthropic adapter), where it matters most.
    *pi:* `cacheRetention` and `sessionId` in `ai/src/types.ts`, `cache-stats.ts`.

### Phase 8 — Everyday features
37. [ ] **Prompt templates and slash commands.** Saved prompts the user types as
    `/name args` in the input box, expanded before they become a user message.
    Skills (step 23) can show up here too.
    *pi:* `prompt-templates.ts`, `slash-commands.ts`. *opencode:* `command.ts`.
38. [ ] **Undo file changes.** Snapshot the workspace before each turn's writes
    and let the user roll a turn back. *opencode:* `snapshot.ts`. pi has no
    equivalent.
39. [ ] **Versioned workspace.** Keep every snapshot as a history of the
    workspace's files: browse, diff and restore any version, and give each
    branch (step 25) its own line of versions. Builds on step 38.
40. [ ] **Todo tool.** The model keeps a short task list the UI shows next to the
    chat, for long jobs. *opencode:* `tool/todowrite.txt`.
41. [ ] **Web fetch and search tools.** Fetch a URL as Markdown, and search the
    web. Opt-in like `bash`, since they reach the network.
    *opencode:* `websearch.ts`, `tool/html-markdown.ts`.
42. [ ] **Browser automation.** A tool that drives a real browser (open, click,
    type, read, screenshot) inside the sandbox. Needs step 28.
43. [ ] **Multimodal output.** Tools and the model can return files and images
    the chat shows and the user can download (charts, slides, generated images),
    not only text. Builds on step 21 (images in).
    *pi:* `ai/src/images.ts`, `durable/src/images/`. *opencode:* `artifact.ts`.
44. [ ] **Session export.** Download a session as Markdown or HTML.
    *pi:* `session-export.ts`, `export-html/`.
45. [ ] **Scheduled runs.** Start a prompt on a session on a timer ("every
    morning, check X"), with a list of schedules and their past runs.
    *opencode:* `automation.ts`, `automation/`.

### Phase 9 — Bigger agents
46. [ ] **Async tasks.** A tool can start slow work (a long `bash` command, a
    subagent) in the background and return at once with a task id. The model
    keeps working and gets the result as a follow-up message (step 14) when it
    finishes; it can also check or stop the task.
    *pi:* durable's "Tools that park". *opencode:* `job.ts`, `job/`.
47. [ ] **Subagents.** A tool that starts a child session on a task (its own
    model and tool set) and returns the child's answer; stopping the parent stops
    the child. *pi:* durable's "Abort and Subagents",
    `test/examples/21-subagent-*.ts`.
48. [ ] **Nested agents, in full.** Background children (with step 46), several
    children whose answers are gathered into one result (fan-in), choosing what
    context and tools a child inherits, files a child leaves behind as artifacts,
    and opening a child's chat from the parent's call in the UI. Needs step 47.
    *pi:* durable's "Child Tasks", `test/examples/22-subagent-background.ts`.
49. [ ] **Playbooks and skill authoring.** Playbooks: saved multi-step recipes
    a run follows (and can embed in another run). Skills written by chatting
    with the agent, and imported or exported as packages. Needs step 23.
    *pi:* `docs/skills.md`, `docs/packages.md`.
50. [ ] **MCP authorization.** Sign in to MCP servers that need OAuth, store
    their tokens per user, and refresh them. Needs step 24.
    *pi:* `packages/mcp/src/oauth/`, `auth-provider.ts`.
51. [ ] **Tool search.** Offer large tool sets (MCP, step 24) only on demand: the
    model searches "deferred" tools and loads the ones it needs. Needs step 24.
    *pi:* `durable/src/tool-search/`.
52. [ ] **Virtual models.** A named model that picks a real one per request
    (cheap for simple turns, strong for hard ones). Needs step 19, which was
    skipped: build it first from ADR-013.
    *pi:* `virtual-models.ts`, `docs/virtual-models.md`.
53. [ ] **Code mode.** One tool that runs a model-written script in a sandbox,
    where the script calls our tools; only its output goes back to the model.
    Needs step 28 and nested tool calls.
    *pi:* `packages/codemode`, `nested-tool-calls.ts`.
54. [ ] **Durable runs.** Save each step before it runs so a crash resumes
    mid-tool, with checkpoints, and let a tool wait hours for a webhook or a
    person without holding a process (a workflow engine such as Restate is one
    way). Would supersede parts of ADR-007 and step 15's "the user decides".
    *pi:* `packages/durable` (whole package; read its README first).

## Alternatives considered
- **Edit ADR-003's list** — one list in one place, but ADR-003 is accepted and
  the rules say to extend or supersede, not rewrite. Its own steps and ticks are
  unchanged, so a second list that continues the numbering is enough.
- **Copy every pi feature** — pi also has a TUI, themes, keybindings, RPC mode,
  npm extension packages and telemetry exporters. Those belong to a terminal
  app; ours is a web app with its own UI. Left out.
- **Put safety last, as features** — steps 26–28 come first because bash, web
  fetch, browsers and subagents are unsafe to expose without them.
- **A separate phase for the owner's concepts** — keeps them visible as a set,
  but splits related work (failover next to retry, async tasks next to
  subagents). Slotted into the matching phases instead.

## Consequences
- The numbering continues from 26, so "step N" is unique across ADR-003 and this
  ADR.
- Steps 19 and 20 (ADR-003) were skipped; 31, 36 and 52 need them built
  first.
- Order to keep: 26–27 before the app is reachable by anyone else; 31 and 36
  after 20; 33 after 32; 35 after 34; 39 after 38; 42 and 53 after 28; 48 after
  46 and 47; 49 after 23; 50 and 51 after 24; 52 after 19; 54 is last because it
  changes how runs are stored.
- Steps 26, 28, 33, 46, 47 and 54 each change the architecture and will need
  their own ADR when built.
