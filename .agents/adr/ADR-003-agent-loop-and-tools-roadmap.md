# ADR-003 — Agent loop and tools roadmap: pi parity, one step at a time

- **Status:** Accepted
- **Date:** 2026-11-17
- **Deciders:** project owner

## Context
ADR-001 built a minimal loop (`agent/loop.py`): stream a reply, run its tool calls
one at a time, repeat until no tool calls or `max_turns`. It has two tools (`read`,
`write`), no hooks, and a run tied to its HTTP request. ADR-001's consequences list
what is missing.

pi's loop (`../pi-upstream/packages/agent/src/agent-loop.ts`, config in `types.ts`,
stateful wrapper in `agent.ts`) and its tools
(`packages/coding-agent/src/core/tools/`) show the full feature set. The owner wants
every gap listed here as an ordered roadmap, to build one item per step. Each item
names the pi file to read first.

## Decision
We grow `agent/` and `tools/` toward pi's design in the order below. Each step is
small enough for one change and leaves the app working. A step that changes an
architectural choice gets its own ADR. Small steps only tick their box here.

The extension points keep pi's names in snake_case (`before_tool_call`,
`transform_context`, …), as optional fields on one `LoopConfig` passed to
`run_agent`, so the loop does not grow a long parameter list.

### Phase 1 — Tools
1. [x] **Structured tool results.** `execute` returns a `ToolResult`
   (`content` blocks for the model, `details` for the UI, `is_error`)
   instead of `str`. Keep `ToolError` for the simple failure case. `details` is
   saved on `ToolResultMessage` and never sent to the provider.
   *pi:* `AgentToolResult` in `agent/src/types.ts`.
2. [x] **`edit` tool.** `path` + `edits[{old_text, new_text}]`. Each `old_text` is
   unique and non-overlapping, and matched against the original file. Return a diff
   in `details`. Built with exact matching only. It keeps the BOM and CRLF, and
   `details` holds a unified `patch` (stdlib `difflib`) + `first_changed_line`.
   Not built yet: pi's fuzzy match fallback (smart quotes, trailing spaces), and
   writing back a file with mixed line endings exactly as it was (a file with any
   CRLF is written back all-CRLF). *pi:* `tools/edit.ts`, `edit-diff.ts`.
3. [x] **`bash` tool.** `command` + `timeout?`, with `cwd` set to the workspace.
   Keep the tail of the output (2000 lines / 50 KB) and save the full output to a
   file when it is cut. Kill the process tree on timeout or cancel. Commands run
   on the host with the API's permissions, so this needs step 7 (permissions) or
   a sandbox before anyone else can reach the app.
   Built as follows:
   - The command's stdout/stderr goes straight to a temp file, not a pipe. With a
     pipe, asyncio's `proc.wait()` waits for every holder of the pipe, so one
     background child would hang the call.
   - The command runs in its own process group. That group is killed when the
     shell exits (ending background children), on timeout, and on cancel.
   - The command gets an allowlisted environment (PATH, HOME, LANG, …), so the
     server's API keys never reach it. pi does not do this.
   - No default timeout, as in pi. A hung command runs until the run is cancelled.
   - Unix only.
   - Opt-in: sessions get `bash` only with `AGENTSPACE_ENABLE_BASH=true`, until
     there is a sandbox or per-command approval. An allowlisted environment does
     not protect secrets on disk: a command can still read `apps/api/.env`. The API
     also has no auth or Host check yet, so a DNS-rebinding page can drive it.
   *pi:* `tools/bash.ts`, `core/bash-executor.ts`, `tools/truncate.ts`.
4. [x] **`grep`, `find`, `ls` tools.** Read-only, respect `.gitignore`, and have
   result limits. Built as follows:
   - `grep` and `find` run ripgrep, which is now a system requirement. `find` uses
     `rg --files`, not pi's `fd`, so only one binary is needed.
   - Flags on every search (`tools/ripgrep.py`): `--hidden --no-require-git
     --sort path`, plus `--glob '!.git'` last, because a later glob wins.
   - Symlinks are not followed, so a search stays inside the workspace.
   - Not built yet: pi's case where a match inside the last kept match's trailing
     context is shown as context. We stop at that match instead.
   - `ls` is plain Python.
   *pi:* `tools/grep.ts`, `find.ts`, `ls.ts`.
5. [x] **Tool sets.** Name presets: `coding` = read, bash, edit, write and
   `read_only` = read, grep, find, ls. Store the chosen set on the session.
   Built as follows:
   - Presets `all` (the default, `AGENTSPACE_DEFAULT_TOOL_SET`), `coding` and
     `read_only` live in `tools/__init__.py`.
   - `POST /sessions` takes `tool_set`. The session stores the expanded tool
     names (`sessions.tools`, JSON), not the preset name, so changing a preset
     never changes an existing session.
   - `bash` is still dropped at run time unless `AGENTSPACE_ENABLE_BASH` is on.
   - Migration `3220c661edc2` gives existing rows all seven tools.
   - The web UI has no tool-set picker yet.
   *pi:* `tools/index.ts`, `DEFAULT_TOOL_NAMES` in `core/settings-manager.ts`.
6. [x] **System prompt from tools.** Each tool adds a short snippet and guidelines
   to the system prompt. Built as follows:
   - `agent/prompt.py` builds the prompt on every run: the session's stored
     `system_prompt` as the preamble, then tagged `<tools>`, `<rules>` (tool
     guidelines, de-duplicated, plus general rules) and `<workspace>` sections.
   - `<workspace>` says paths are relative and that `/` is refused.
   - Unlike pi, a custom preamble keeps the tool sections, and the cwd is not
     shown (no host paths in the prompt).
   *pi:* `promptSnippet`/`promptGuidelines` on each tool, `core/system-prompt.ts`.

### Phase 2 — Loop control
7. [x] **`before_tool_call` / `after_tool_call` hooks.** Called after argument
   validation. `before` can return `block` + `reason` (shown to the model as an
   error result), and `after` can override fields of the result. This is the base
   for permission rules, such as asking before running `bash`.
   *pi:* `prepareToolCall` and `finalizeExecutedToolCall` in `agent-loop.ts`.
   Built as follows:
   - `agent/hooks.py` holds `LoopConfig` and the hook context/result types;
     `run_agent(config=...)` takes it.
   - `execute_tool_call` is split into `prepare_tool_call` (lookup, validation,
     `before`), `execute_prepared` and `finalize_tool_call` (`after`), like pi.
   - A `before` that raises blocks the call; an `after` that raises replaces the
     result with an error. Cancellation passes through both (`except Exception`).
   - Not built: `terminate` on a block (step 11), and any policy using the hooks.
8. [x] **`prepare_arguments`.** An optional per-tool fix-up of raw arguments before
   validation, for models that send slightly wrong shapes.
   *pi:* `prepareToolCallArguments`.
   Built as follows:
   - `Tool.prepare_arguments(args)` defaults to no change. The loop calls it on a
     deep copy of the raw arguments, so the transcript keeps what the model sent;
     a raise becomes an error result.
   - `edit` repairs `edits` sent as a JSON string or a single object, the old
     top-level `old_text`/`new_text` shape, and camelCase `oldText`/`newText`.
9. [x] **Parallel tool execution.** Prepare calls in order, run them with
   `asyncio.gather` / `TaskGroup`, and emit results in the model's call order. A
   per-tool `execution_mode = "sequential"` forces the whole batch to run in order.
   Writes to the same file are serialized.
   *pi:* `executeToolCallsParallel`, `tools/file-mutation-queue.ts`.
   Built as follows (on ADR-006's `emit`):
   - `LoopConfig.tool_execution` defaults to `"parallel"`, as in pi; a tool with
     `execution_mode = "sequential"` makes its whole batch sequential, and a
     reply cut off by the token limit always runs the sequential path.
   - `run_in_parallel` prepares calls in order (refusals end right away), runs the
     cleared ones in an `asyncio.TaskGroup`, emits each `tool_execution_end` as it
     finishes, then the result messages in call order.
   - `tools/file_lock.py`: one `asyncio.Lock` per resolved path, held by `write`
     and by `edit`'s whole read-modify-write.
10. [x] **Tool progress events.** Add an `on_update` callback to `execute` and a
    `tool_execution_update` event, for example to stream bash output live.
    *pi:* `emitToolExecutionUpdate`.
    Built as follows:
    - `on_update` is an async field on `ToolContext`, not a new `execute`
      argument, so tools that don't report progress are unchanged. The loop
      gives each call its own copy and ignores updates after `execute` returns.
    - `ToolExecutionUpdateEvent` carries a snapshot (`partial: ToolResult`), not
      a delta. Updates are streamed, never sent to the model or saved.
    - `bash` checks its output file every 100 ms and sends the last 8 KB when it
      grew. The web app shows the latest snapshot inside the running tool call.
11. [x] **Early termination.** Add `terminate` to `ToolResult`, and stop after a
    batch when every result in it sets it. *pi:* `shouldTerminateToolBatch`.
    Built as follows:
    - `BeforeToolCallResult.terminate` (with `block`) and
      `AfterToolCallResult.terminate` (`None` keeps the tool's value) can set it too.
    - The loop carries the flag in `FinishedCall`, next to the result message,
      so it is never sent to the model or saved.
    - Error results (unknown tool, bad arguments, failed hooks, truncated calls)
      never terminate. The run ends with `reason="terminated"`.
12. [x] **`finish_turn` hook.** Can return `end` or `continue` after each turn.
    It replaces the hard `max_turns` cut with a policy. *pi:* `FinishTurn`.
    Built as follows:
    - `LoopConfig.finish_turn` gets a `FinishTurnContext` (turn number, reply,
      tool results, the run's messages so far) after the tool results are added
      and before `turn_end`.
    - It returns `"end"`, `"continue"` or `None`. `"end"` stops the run with
      `reason="finish_turn"`. `"continue"` runs one more turn on the same context,
      even after a final answer or a terminating batch. `None` keeps the normal rule.
    - After a provider error or abort the hook still runs, but its answer is
      ignored, as in pi.
    - `max_turns` stays as an outer safety cap that the hook can't lift, so a hook
      that always continues can't loop forever. pi has no such cap.

### Phase 3 — Runs that outlive the request
13. [x] **Background runs.** Built per ADR-007 (the DB lock is deferred there). A run is an `asyncio` task owned by the server, not
    by the request. Clients subscribe to its events and can reconnect, and an
    explicit `POST /sessions/{id}/abort` cancels it. Move the run lock into the
    database so the API can run with several workers. Needs its own ADR, because
    it supersedes ADR-001's "closing the connection cancels the run".
14. [ ] **Steering and follow-up queues.** `steer(msg)` is added after the current
    turn, and `follow_up(msg)` runs once the agent would otherwise stop. Each queue
    has a `QueueMode` of `all` or `one_at_a_time`. Exposed through API routes and
    the web input box. Needs step 13.
    *pi:* `getSteeringMessages`, `getFollowUpMessages`, the outer loop in `runLoop`,
    `steer`/`followUp` in `agent.ts`.
15. [ ] **Continue.** Resume from a transcript that ends in a user or tool-result
    message without a new prompt, for retry after an error.
    *pi:* `agentLoopContinue`.

### Phase 4 — Context and models
16. [ ] **Custom message types + `convert_to_llm`.** The transcript can hold
    app-only messages (notices, bash run by the user, summaries) that are turned
    into LLM messages or dropped at the LLM-call boundary.
    *pi:* `CustomAgentMessages`, `core/messages.ts`.
17. [ ] **`transform_context` + compaction.** When the context nears the model's
    window, summarize older messages into one summary message and keep recent turns.
    *pi:* `core/compaction/`, `prepareNextTurn`.
18. [ ] **Thinking / reasoning.** Thinking content blocks in `AssistantMessage`, a
    per-session `thinking_level`, and thinking deltas streamed to the UI.
    *pi:* `ThinkingLevel`, `thinking_*` events.
19. [ ] **`prepare_request`.** Swap the model, thinking level or context right
    before each provider call, for example a cheaper model for simple turns.
    *pi:* `PrepareRequest`.
20. [ ] **More providers + `get_api_key`.** An Anthropic Messages adapter and
    per-call key lookup. *pi:* `packages/ai/src/providers/`.
21. [ ] **Images.** Image content in user messages, `read` returning images, and a
    switch to block images for models that cannot take them.
    *pi:* `read.ts`, `blockImages` in `core/sdk.ts`.

### Phase 5 — Extending the agent
22. [ ] **Changing tools mid-session.** Announce tools added or removed to the
    model through a system message, so the transcript replays the right tool set.
    *pi:* `declareToolChanges`.
23. [ ] **Skills.** Load skill folders (`SKILL.md`) and list them in the system
    prompt, and let the model read one on demand. *pi:* `core/skills.ts`.
24. [ ] **MCP tools.** Turn an MCP server's tools into `Tool`s.
    *pi:* `packages/mcp`, `extensions/mcp/tools.ts`.
25. [ ] **Branching.** Fork a session from any message using the existing
    `parent_id` chain. *pi:* `core/session-manager.ts`.

## Alternatives considered
- **Build features as needed, with no roadmap** — less up-front writing, but the
  order matters (bash before permissions is unsafe; queues need background runs),
  and that is easy to lose without a list.
- **Port pi's loop in one go** — reaches parity quickly, but it is ~950 lines of
  TypeScript to learn while learning Python. It would also go against the "one step
  at a time" rule.
- **One ADR per feature, written up front** — precise, but they would go stale
  before they are built. Instead, ADRs are written when a step changes the
  architecture (13 at least).

## Consequences
- Steps are numbered and checked off here as they land. Reordering is fine, but
  keep the dependencies: 3 needs 7 before the app is exposed, 14 needs 13, and
  17 needs 16.
- `run_agent` moves from loose keyword arguments to a `LoopConfig` as hooks arrive.
  `execute`'s return type changes in step 1, which touches `read` and `write`.
- Step 13 will supersede part of ADR-001. Everything else extends ADR-001 without
  changing its decisions.
