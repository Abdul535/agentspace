# Phase 2 research — loop control in pi, and what it means for our loop

Reference for ADR-003 steps 7–12. Everything about pi below was read from the
upstream clone at `../pi-upstream` (2026-11-17). Paths are relative to that
clone unless they start with `apps/`. Short names used throughout:

- `loop.ts` = `packages/agent/src/agent-loop.ts`
- `types.ts` = `packages/agent/src/types.ts`
- `agent.ts` = `packages/agent/src/agent.ts`
- `loop.test.ts` = `packages/agent/test/agent-loop.test.ts`
- `session.ts` = `packages/coding-agent/src/core/agent-session.ts`
- `tools/` = `packages/coding-agent/src/core/tools/`

Our side: `apps/api/src/agentspace/agent/loop.py` (`run_agent`,
`execute_tool_call`, `error_result`), `agent/tool.py` (`Tool`, `ToolResult`,
`ToolError`, `ToolContext`), `agent/events.py`, `api/sessions.py`
(`send_message` SSE route), `ai/provider.py` (`prepare_transcript`).

---

## 0. The shape of pi's tool pipeline (read this first)

Every tool call goes through three phases. Steps 7–11 all hang off them.

| Phase | pi function | What it does |
|---|---|---|
| prepare | `prepareToolCall` (`loop.ts:708-779`) | abort check → find tool → `prepareArguments` → validate → `beforeToolCall` → abort check → block? Returns either `{kind:"prepared"}` or `{kind:"immediate", result}` |
| execute | `executePreparedToolCall` (`loop.ts:824-860`) | calls `tool.execute(id, args, signal, onUpdate)`, times it, turns a throw into an error result, closes the update gate |
| finalize | `finalizeExecutedToolCall` (`loop.ts:862-913`) | runs `afterToolCall` and merges its override |

Then the batch runners decide ordering and events:
`executeToolCalls` (`loop.ts:518-533`) picks `executeToolCallsSequential`
(`loop.ts:540-590`) or `executeToolCallsParallel` (`loop.ts:592-660`), and both
return `{ messages, terminate }` (`ExecutedToolCallBatch`, `loop.ts:535-538`).

Our `execute_tool_call` (`loop.py`) is all three phases fused into one function,
with no hooks. **The first concrete refactor for Phase 2 is to split it into
`prepare_tool_call` / `execute_prepared_tool_call` / `finalize_tool_call`**,
mirroring pi. Steps 7, 8, 9, 10 and 11 each then touch one phase.

How pi gets events out: the loop takes an `emit: AgentEventSink` callback
(`loop.ts:35`) and `await`s every emit. `agentLoop` (`loop.ts:47-70`) runs the
loop in the background and pushes each emitted event into an `EventStream` that
the caller iterates. Our loop is an async generator that `yield`s. That
difference becomes the central design problem in step 9 (concurrent tasks cannot
`yield` from a generator) and step 7 (an approval prompt must reach the client
while the loop is blocked in a hook). See "Dependencies & suggested order".

---

## Step 7 — `beforeToolCall` / `afterToolCall` hooks

### How pi does it

Both hooks are optional fields on `AgentLoopConfig` (`types.ts:319-341`):

```ts
beforeToolCall?: (context: BeforeToolCallContext, signal?: AbortSignal) => Promise<BeforeToolCallResult | undefined>;
afterToolCall?: (context: AfterToolCallContext, signal?: AbortSignal) => Promise<AfterToolCallResult | undefined>;
```

**When `beforeToolCall` runs** — inside `prepareToolCall`, after argument
preparation and validation, before execution (`loop.ts:716-771`):

```ts
if (signal?.aborted) { return { kind: "immediate", result: createErrorToolResult("Operation aborted"), isError: true }; }
const tool = tools.find((t) => t.name === toolCall.name);
if (!tool) { return { kind: "immediate", result: createErrorToolResult(`Tool ${toolCall.name} not found`), isError: true }; }
try {
    const preparedToolCall = prepareToolCallArguments(tool, toolCall);
    const validatedArgs = validateToolArguments(tool, preparedToolCall);
    if (config.beforeToolCall) {
        const beforeResult = await config.beforeToolCall({ assistantMessage, toolCall, args: validatedArgs, context: currentContext }, signal);
        if (signal?.aborted) { return { kind: "immediate", result: createErrorToolResult("Operation aborted"), isError: true }; }
        if (beforeResult?.block) {
            const result = createErrorToolResult(beforeResult.reason || "Tool execution was blocked");
            if (beforeResult.terminate === true) { result.terminate = true; }
            return { kind: "immediate", result, isError: true };
        }
    }
    return { kind: "prepared", toolCall, tool, args: validatedArgs };
} catch (error) {
    return { kind: "immediate", result: createErrorToolResult(error instanceof Error ? error.message : String(error)), isError: true };
}
```

So unknown tools and invalid arguments never reach the hook
(`loop.test.ts:2335`: `hookCalls` equals `["before a", "after a", "before c", "before e", "after e"]` —
`b` failed validation and `d` was an unknown tool).

**Inputs**

- `BeforeToolCallContext` (`types.ts:107-116`): `assistantMessage`,
  `toolCall` (the *raw* block from the assistant message, original
  arguments), `args` (validated, prepared arguments), `context` (messages +
  tools).
- `AfterToolCallContext` (`types.ts:119-132`): same plus `result` (the
  executed result before overrides) and `isError`.

**`beforeToolCall` return** — `BeforeToolCallResult` (`types.ts:66-74`):
`{ block?: boolean; reason?: string; terminate?: boolean }`. `undefined` or
`{block: false}` lets the call run. A blocked call becomes an error result whose
text is `reason` or `"Tool execution was blocked"` (`loop.ts:755`), with
`details: {}` (`createErrorToolResult`, `loop.ts:915-920`). A blocked call never
runs `afterToolCall` (`loop.test.ts:2334` comment "blocked calls skip afterToolCall")
and has no `durationMs` (`loop.test.ts:505`).

**Mutating `args`**: the hook may mutate `args` in place, and the mutated
object is executed **without re-validation** (`loop.test.ts:581-640`, "should
execute mutated beforeToolCall args without revalidation" — the hook sets a
string field to `123` and the tool receives `123`). The coding agent documents
this as the way extensions patch arguments (`packages/coding-agent/src/core/extensions/types.ts:1272-1276`:
"`event.input` is mutable … No re-validation is performed after mutation").
Because `validateToolArguments` validates a `structuredClone` of the arguments
(`packages/ai/src/utils/validation.ts:58-59`), mutating `args` never changes
the tool call stored in the transcript.

**When `afterToolCall` runs and how it merges** — `finalizeExecutedToolCall`
(`loop.ts:873-905`):

```ts
if (afterResult) {
    // Structured content not replaced along with the content may no longer match it.
    const structuredContent =
        afterResult.structuredContent ?? (afterResult.content ? undefined : result.structuredContent);
    result = {
        ...result,
        content: afterResult.content ?? result.content,
        details: afterResult.details ?? result.details,
        usage: afterResult.usage ?? result.usage,
        terminate: afterResult.terminate ?? result.terminate,
    };
    if (structuredContent === undefined) delete result.structuredContent;
    else result.structuredContent = structuredContent;
    isError = afterResult.isError ?? isError;
}
```

Field-by-field rules (also the doc comment at `types.ts:76-104`):

| Field in `AfterToolCallResult` | Effect |
|---|---|
| `content` | replaces the whole content list (no deep merge) |
| `details` | replaces the whole details value |
| `isError` | replaces the error flag |
| `usage` | replaces usage |
| `terminate` | replaces the early-termination hint |
| `structuredContent` | replaces it; **if `content` is given without `structuredContent`, structured content is dropped** |
| omitted | keeps the executed value (`??` — so `null` would also count as "omitted" in TS) |

Pinned by `loop.test.ts:2337-2356`: overrides `{content}` → `undefined`,
`{structuredContent}` → replaced, `{content, structuredContent}` → both kept,
`{details}` → original structured content kept.

It runs after `execute` and before `tool_execution_end` is emitted
(`types.ts:328-329`), so the UI and the transcript only ever see the merged result.
`durationMs` measures `execute()` only, excluding both hooks (`loop.test.ts:455-507`).

**Errors thrown by the hooks**

- `beforeToolCall` throws → caught by the `try` in `prepareToolCall`
  (`loop.ts:772-778`) → the call is not executed and becomes an error result
  with the exception message. Fail-closed.
- `afterToolCall` throws → caught in `finalizeExecutedToolCall`
  (`loop.ts:901-904`) → **the tool's real result is replaced** by an error
  result with the exception message, `isError = true`. Note the tool has
  already run (side effects happened), but the model is told it failed.

**Abort handling** — the loop checks `signal.aborted` before preparing
(`loop.ts:718-724`; the hook is not called at all, `loop.test.ts:1055-1056`
"The hook is not asked about a call that will not run") and again right after
`beforeToolCall` returns (`loop.ts:747-753`), so an approval prompt that the
user aborts is never treated as "allowed". Beyond that the hooks receive the
signal and "are responsible for honoring it" (`types.ts:324-325`, `338-339`).

**`runToolCall` — tools calling tools** (`loop.ts:792-822`). An exported
function that runs one call through prepare → execute → finalize with the
caller's hooks, but emits no events and adds no messages:

```ts
export async function runToolCall(toolCall: AgentToolCall, options: RunToolCallOptions): Promise<AgentToolCallOutcome> {
    const { assistantMessage, context, signal } = options;
    const preparation = await prepareToolCall(context, assistantMessage, toolCall, options, signal, options.tools);
    if (preparation.kind === "immediate") {
        return { toolCall, result: preparation.result, isError: preparation.isError };
    }
    const executed = await executePreparedToolCall(preparation, signal, options.onUpdate ?? (() => {}));
    return finalizeExecutedToolCall(context, assistantMessage, preparation, executed, options, signal);
}
```

It never rejects for tool failures (`loop.ts:810-812`). The point is that a
permission check applies to nested calls too. The coding agent wires it in
`session.ts:794-825` (`_executeNestedToolCall`): nested calls get id
`<parentId>/<n>`, go through `_beforeToolCall`/`_afterToolCall` with
`parentToolCallId`, and the session itself emits `tool_execution_*` events with
`parentToolCallId` (`packages/coding-agent/src/core/nested-tool-calls.ts:176-254`).

**How the coding agent uses the hooks for permissions.** pi has *no built-in
permission system*: tools run with the process's OS permissions
(`packages/coding-agent/docs/how-pi-works.md:47-49`). Permissions are
extensions on the `tool_call` / `tool_result` events, which the session bridges
to the loop hooks:

- `session.ts:715-718` installs `agent.beforeToolCall = (ctx) => this._beforeToolCall(ctx)` once; the
  callbacks read the current extension runner at call time.
- `_beforeToolCall` (`session.ts:721-745`) forwards `{toolName, toolCallId, input: args}`
  to `runner.emitToolCall`. Non-`Error` throws become
  `Error("Extension failed, blocking execution: …")`.
- `runner.emitToolCall` (`packages/coding-agent/src/core/extensions/runner.ts:1256-1274`)
  calls every handler in order; **the first `block: true` wins and stops the
  chain**; handler errors are *not* caught, so they propagate to the loop and
  block the call (fail-closed).
- `runner.emitToolResult` (`runner.ts:1197-1254`) chains `tool_result`
  handlers: each sees the previous handler's changes; a `content` change drops
  `structuredContent` unless the same handler sets it (`runner.ts:1208-1211`);
  handler errors are **caught and reported**, not fatal (`runner.ts:1229-1237`,
  fail-open). `_afterToolCall` (`session.ts:747-786`) then also normalizes
  images and returns `undefined` when nothing changed.

Extension examples:

- `packages/coding-agent/examples/extensions/permission-gate.ts:13-33` — for
  `bash` commands matching `rm -rf`, `sudo`, `chmod/chown 777`, asks
  `ctx.ui.select("…Allow?", ["Yes","No"])`; anything but `"Yes"` →
  `{ block: true, reason: "Blocked by user" }`. With no UI (`!ctx.hasUI`) it
  blocks by default: `"Dangerous command blocked (no UI for confirmation)"`.
- `protected-paths.ts:13-29` — for `write`/`edit`, blocks when `input.path`
  contains `.env`, `.git/` or `node_modules/` (substring match, no prompt).
- `confirm-destructive.ts` is **not** a tool hook: it confirms *session* actions
  (`session_before_switch`, `session_before_fork` → `{cancel: true}`). Useful
  only as a pattern for "ask, then cancel".

How the prompt reaches a non-terminal client: in RPC mode
(`packages/coding-agent/src/modes/rpc/rpc-mode.ts:95-134`) `ctx.ui.select/confirm`
writes an `{type:"extension_ui_request", id, method, …}` line and returns a
promise stored under `id`; the client answers with
`{type:"extension_ui_response", id, value|confirmed|cancelled}`
(`rpc-types.ts:253-297`). On abort or timeout the promise resolves to the
**default** (`undefined` for select, `false` for confirm), which the gate treats
as "no". This is the model for our web approval flow.

`terminate` on a block: `#5998` regression
(`packages/coding-agent/test/suite/regressions/5998-blocked-tool-terminate.test.ts:16-51`)
— a `tool_call` handler returning `{block, reason, terminate: true}` ends the run
after the batch, and the `tool_execution_end` event's result carries
`terminate: true`.

### Edge cases & gotchas

- Hook sees validated args, not raw ones; `toolCall.arguments` is the raw model
  output (`types.ts:110-113`). Show the user `args` in an approval prompt.
- `args` mutation is executed without re-validation (`loop.test.ts:581`).
- Unknown tool / invalid args / `argumentsError` short-circuit before the hook (`loop.test.ts:2335`).
- Blocked → no `afterToolCall`, no `durationMs` (`loop.test.ts:2334`, `505`).
- `before` throw → call not executed; `after` throw → real result discarded (`loop.ts:772-778`, `901-904`).
- Abort during a hook → `"Operation aborted"` regardless of what the hook returned (`loop.ts:747-753`).
- Content-only override drops `structuredContent` (`loop.test.ts:2337`). We have no `structured_content` yet, so this rule only matters if we add it.
- `details` override replaces, never deep-merges (`types.ts:90`).

### Events / types involved

No new events. New types (pi names → ours):

```ts
interface BeforeToolCallResult { block?: boolean; reason?: string; terminate?: boolean }        // types.ts:66
interface AfterToolCallResult { content?; details?; structuredContent?; isError?; usage?; terminate? } // types.ts:92
interface BeforeToolCallContext { assistantMessage; toolCall; args; context }                    // types.ts:107
interface AfterToolCallContext  { assistantMessage; toolCall; args; result; isError; context }  // types.ts:119
interface AgentToolCallOutcome  { toolCall; result; isError; durationMs? }                       // types.ts:449
```

### What changes in our code

- **New `LoopConfig`** (ADR-003 already says hooks go on one config object).
  A frozen dataclass in a new `agent/config.py` (or in `loop.py`) with
  `before_tool_call` and `after_tool_call` as `Callable[..., Awaitable[...] | None] | None`.
  `run_agent(..., config: LoopConfig = LoopConfig())`. Move
  `max_turns`/`max_tokens` there later or now.
- **New Pydantic/dataclass types** in `agent/tool.py` (or `agent/hooks.py`):
  `BeforeToolCallContext(assistant_message, tool_call, args, messages, tools)`,
  `BeforeToolCallResult(block=False, reason=None, terminate=False)`,
  `AfterToolCallContext(... result, is_error)`, `AfterToolCallResult` with every
  field `None` by default. Use `None` as "not provided" — `x if x is not None else old`
  is the Python form of `??` (do **not** use `or`, which would treat `[]`
  content or `{}` details as "not provided").
- **Split `execute_tool_call`** into:
  - `prepare_tool_call(call, tools, assistant, config) -> PreparedCall | ToolResultMessage`
    (lookup → `arguments_error` → `prepare_arguments` (step 8) → `model_validate` → `before_tool_call` → block).
    Wrap the hook call in `try/except Exception` like pi.
  - `execute_prepared(prepared, ctx, on_update) -> ExecutedOutcome` (today's `try/except ToolError/Exception` block).
  - `finalize_tool_call(prepared, executed, config) -> ToolCallOutcome` (after-hook merge, `try/except Exception`).
- **`ToolCallOutcome`** (call, `ToolResult`, `is_error`) between phases, so
  `is_error` can be overridden separately from the result like pi does.
- **`run_tool_call(call, *, tools, ctx, assistant, config, on_update=None)`**
  public function in `loop.py`, = pi's `runToolCall`, so a future tool that
  calls tools goes through the same hooks. Can wait until a tool needs it.
- Abort: Python has no `AbortSignal`; cancellation is `asyncio.CancelledError`
  raised at the current `await`. A hook awaiting user approval is simply
  cancelled with the run. pi's "check `aborted` after the hook" exists because
  a JS promise can resolve after abort; in Python, if the task is cancelled the
  hook's `await` raises and the code after it never runs. We only need an
  explicit flag if we later add an abort that does *not* cancel the task (step 13
  "abort route" could still be implemented as `task.cancel()`).
- **Never catch `CancelledError`** in the new `except Exception` blocks — it is
  a `BaseException` in Python 3.8+, so `except Exception` already lets it through.
  Add a `# TS:` note, since TS `catch` catches everything.
- `api/sessions.py`: build the hooks (permission policy) and pass `LoopConfig`.
  The bash opt-in flag (`AGENTSPACE_ENABLE_BASH`) can later become a
  `before_tool_call` policy ("ask before bash").

### Suggested tests (`tests/test_loop.py`)

- `before_tool_call` returning `block` → tool not executed; error result text is `reason`; default text when `reason` is `None`.
- Hook is not called for an unknown tool or invalid args.
- Hook receives validated args (a Pydantic model instance), and mutating them changes what `execute` gets.
- `before_tool_call` raising → error result with its message, tool not executed.
- `after_tool_call` override: content-only, details-only, `is_error` flip, `None` (no change), and `[]` content (must replace, not be ignored).
- `after_tool_call` raising → error result replaces the real one.
- Blocked call: `after_tool_call` not called.
- Cancelling the run while `before_tool_call` awaits → tool not executed, run ends `aborted`.

---

## Step 8 — `prepareArguments`

### How pi does it

An optional per-tool method on `AgentTool` (`types.ts:481-485`):

```ts
/**
 * Optional compatibility shim for raw tool-call arguments before schema validation.
 * Must return an object that matches `TParameters`.
 */
prepareArguments?: (args: unknown) => AgentToolParams<TParameters>;
```

Called by `prepareToolCallArguments` (`loop.ts:694-706`) inside the `try` of
`prepareToolCall`, right before validation:

```ts
function prepareToolCallArguments(tool: AgentTool<any>, toolCall: AgentToolCall): AgentToolCall {
    if (!tool.prepareArguments) { return toolCall; }
    const preparedArguments = tool.prepareArguments(toolCall.arguments);
    if (preparedArguments === toolCall.arguments) { return toolCall; }
    return { ...toolCall, arguments: preparedArguments as Record<string, any> };
}
```

The prepared call is only used for validation; `prepared.toolCall` handed to the
hooks and events stays the original (`loop.ts:766-771` returns `toolCall`, not
`preparedToolCall`). A throwing shim becomes an error result via the same
`catch` (`loop.ts:772-778`).

**The real example** — edit tool's `prepareEditArguments`
(`tools/edit.ts:98-129`, wired at `tools/edit.ts:153`). It fixes three model mistakes:

```ts
// Some models (Opus 4.6, GLM-5.1) send edits as a JSON string instead of an array.
// Others send a single edit object instead of a one-element edits array.
if (typeof args.edits === "string") {
    try {
        const parsed = JSON.parse(args.edits);
        if (Array.isArray(parsed)) { args.edits = parsed; }
        else if (isSingleEditInput(parsed)) { args.edits = [parsed]; }
    } catch {}
} else if (isSingleEditInput(args.edits)) {
    args.edits = [args.edits];
}
const legacy = args as LegacyEditToolInput;
if (typeof legacy.oldText !== "string" || typeof legacy.newText !== "string") { return args as EditToolInput; }
const edits = Array.isArray(legacy.edits) ? [...legacy.edits] : [];
edits.push({ oldText: legacy.oldText, newText: legacy.newText });
const { oldText: _oldText, newText: _newText, ...rest } = legacy;
return { ...rest, edits } as EditToolInput;
```

1. `edits` sent as a JSON **string** → parsed (invalid JSON is left alone so validation reports it).
2. `edits` sent as a **single object** → wrapped in a list.
3. **Legacy top-level** `oldText`/`newText` (the old single-edit schema) → folded into `edits`, appended after any existing ones.

Tests: `packages/coding-agent/test/edit-tool-legacy-input.test.ts` —
legacy fields are *not* in the public schema (`:21-25`), folding (`:27-38`),
appending (`:40`), pass-through of valid and non-object input (`:57`, `:67`),
stringified edits (`:93-103`), invalid JSON left alone (`:105-116`). The loop-level test is
`loop.test.ts:642-719`. The edit tool is the only built-in tool with a shim
(`grep prepareArguments packages/coding-agent/src` finds only `edit.ts:153` and wrappers).

### Edge cases & gotchas

- **pi's shim mutates the raw arguments in place** (`args.edits = parsed`,
  `edit.ts:111`, `117`), i.e. it rewrites `toolCall.arguments` inside the stored
  assistant message. The identity check at `loop.ts:699` then sees the same
  object and skips the copy. Our version should copy first (deep copy) so the
  transcript keeps what the model really sent.
- Non-object input passes through untouched so the validator produces the error
  (`edit.ts:99-101`).
- Runs before the `argumentsError` check (that check lives in
  `validateToolArguments`, `packages/ai/src/utils/validation.ts:55-57`), but the
  copied call keeps `argumentsError`, so a JSON-parse failure still fails.
- The shim's output is validated; a shim cannot smuggle invalid data past the schema.

### Events / types involved

None. `tool_execution_start.args` stays the raw arguments (`loop.ts:552-557`).

### What changes in our code

- `agent/tool.py`: add a non-abstract method on `Tool`:
  `def prepare_arguments(self, args: dict[str, Any]) -> dict[str, Any]: return args`
  (default = no-op, so no `None` check is needed; `# TS:` note: an overridable
  method with a default body, like a non-abstract method on an abstract class).
- `loop.py` `prepare_tool_call`: `raw = copy.deepcopy(call.arguments)`;
  `args = tool.params.model_validate(tool.prepare_arguments(raw))` inside the
  same `try` that turns exceptions into an error result.
- **Order question for us**: today `arguments_error` short-circuits before
  validation. Keep that before `prepare_arguments` (simpler; `arguments` is `{}` then anyway).
- `tools/edit.py` `EditTool.prepare_arguments`: the same three fixes, plus one
  that pi does not need but we do — **camelCase keys**. Our schema uses
  `old_text`/`new_text` (`tools/edit.py:23-30`), while models trained on pi/Claude
  Code-style tools often send `oldText`/`newText`. Map those inside `edits[]` and
  at top level. (Alternative: Pydantic `validation_alias=AliasChoices("old_text", "oldText")`
  on `Replacement` — no shim needed for that part. Team choice.)

### Suggested tests

- Edit: `edits` as JSON string, as single object, legacy top-level `old_text`/`new_text`, camelCase keys, invalid JSON string → validation error result.
- The assistant message's stored `tool_call.arguments` is unchanged after a shim ran.
- A shim that raises → error result, tool not executed.
- Tools without an override behave exactly as today.

---

## Step 9 — Parallel tool execution

### How pi does it

Mode selection (`loop.ts:518-533`):

```ts
const hasSequentialToolCall = toolCalls.some(
    (tc) => currentContext.tools?.find((t) => t.name === tc.name)?.executionMode === "sequential",
);
if (config.toolExecution === "sequential" || hasSequentialToolCall) {
    return executeToolCallsSequential(...);
}
return executeToolCallsParallel(...);
```

- Config default is `"parallel"` (`types.ts:308-317`; `agent.ts:261`
  `this.toolExecution = runtimeOptions.toolExecution ?? "parallel"`).
- **One tool with `executionMode: "sequential"` makes the whole batch
  sequential** (`types.ts:501-510`), not just that tool
  (`loop.test.ts:920-1001`, `1072-1156`). `executionMode: "parallel"` is the same
  as omitting it (`loop.test.ts:1158-1229`).
- No built-in coding-agent tool sets `executionMode`; `bash`, `edit`, `write`
  all run in parallel (`grep executionMode packages/coding-agent/src` finds only
  the wrapper pass-through and nested calls).

**Parallel runner** (`loop.ts:592-660`):

1. **Prepare sequentially, in source order.** For each call: emit
   `tool_execution_start`, then `await prepareToolCall(...)` (which includes
   `beforeToolCall`, so approval prompts are one at a time). An immediate
   outcome (unknown, invalid, blocked) emits `tool_execution_end` right away
   (`loop.ts:611-620`). A prepared call is pushed as an un-started thunk
   (`loop.ts:622-643`).
2. **Execute concurrently** — the thunks start only at `Promise.all`
   (`loop.ts:646-648`), i.e. after *every* call is prepared and approved.
   Each thunk: if aborted → `"Operation aborted"`; else execute → finalize
   (`afterToolCall`) → emit `tool_execution_end`. So **`tool_execution_end` is
   emitted in completion order.**
3. **Results in source order** — after all settle, emit `message_start`/`message_end`
   for each tool-result message in the order the model issued the calls
   (`loop.ts:649-654`), and return them in that order (that is also the order in
   `turn_end.toolResults` and in the transcript).

```ts
const orderedFinalizedCalls = await Promise.all(
    finalizedCalls.map((entry) => (typeof entry === "function" ? entry() : Promise.resolve(entry))),
);
const messages: ToolResultMessage[] = [];
for (const finalized of orderedFinalizedCalls) {
    const toolResultMessage = createToolResultMessage(finalized);
    await emitToolResultMessage(toolResultMessage, emit);
    messages.push(toolResultMessage);
}
```

Pinned by `loop.test.ts:721-813`: `tool_execution_end` ids `["tool-2","tool-1"]`,
tool-result `message_end` ids and `turn_end.toolResults` `["tool-1","tool-2"]`.

**Sequential runner** (`loop.ts:540-590`): per call, start → prepare → execute →
finalize → end → result message, before the next call. Same per-call semantics,
no interleaving.

**Abort** (`#9986`, "every tool call gets a result"):

- Sequential, abort while call 1 runs: call 1 gets its own error, calls 2–3 get
  `"Operation aborted"` from the early check in `prepareToolCall`; every call
  still gets start/end events and a result message (`loop.test.ts:1003-1032`).
- Parallel, abort during call 2's `beforeToolCall`: nothing executes; the hook is
  not called for call 3; all three get `"Operation aborted"` — call 1 from the
  check at the top of its thunk (`loop.ts:623-631`), call 2 from the post-hook
  check, call 3 from the pre-check (`loop.test.ts:1034-1070`).
- Already-running tools get the signal and must stop themselves.

**File mutation queue** (`tools/file-mutation-queue.ts:1-61`) — a per-file
lock so parallel `edit`/`write` calls to the same file do not lose each other's
changes:

```ts
export async function withFileMutationQueue<T>(filePath: string, fn: () => Promise<T>): Promise<T> {
    const registration = registrationQueue.then(async () => {
        const key = await getMutationQueueKey(filePath);
        const currentQueue = fileMutationQueues.get(key) ?? Promise.resolve();
        let releaseNext!: () => void;
        const nextQueue = new Promise<void>((resolveQueue) => { releaseNext = resolveQueue; });
        const chainedQueue = currentQueue.then(() => nextQueue);
        fileMutationQueues.set(key, chainedQueue);
        return { key, currentQueue, chainedQueue, releaseNext };
    });
    ...
    await currentQueue;
    try { return await fn(); }
    finally {
        releaseNext();
        if (fileMutationQueues.get(key) === chainedQueue) { fileMutationQueues.delete(key); }
    }
}
```

- Key = `realpath(resolve(path))`, falling back to the resolved path when the
  file does not exist yet (`:16-26`) — so symlink aliases share a queue
  (`packages/coding-agent/test/file-mutation-queue.test.ts:77-98`).
- A global `registrationQueue` serializes the async key lookup so FIFO order
  matches call order (`:33-49`).
- Different files run in parallel (`test:56-75`); same file is FIFO (`test:38-54`).
- Used by `edit` (`tools/edit.ts:158`) and `write` (`tools/write.ts:66`), sharing
  one queue (`test:131-174`); the whole read-modify-write runs inside it.
- **Abort keeps the lock**: tools do not reject from an abort listener; they
  check `signal.aborted` after each `await` so the lock is held until the
  in-flight write settles (`tools/write.ts:67-74` comment;
  `test:176-218` "keeps write queue locked while an aborted write is still in flight").
- Exported for extension tools (`packages/coding-agent/src/core/tools/index.ts:20`).

### Edge cases & gotchas

- Blocked/invalid calls in a parallel batch emit `tool_execution_end` *during*
  preparation, before later calls' `tool_execution_start` (`loop.ts:611-620`).
- No tool executes until all hooks have answered (`loop.ts:646`), so a slow
  approval delays the whole batch.
- Tool-result messages are only emitted after the slowest tool finishes. In our
  app that means **results are persisted only at the end of the batch**; if the
  connection drops mid-batch, finished results are lost and
  `prepare_transcript` (`ai/provider.py:89-135`) stands in
  `"Tool call was interrupted before it returned a result."` for each.
- The `length`-truncated path (`failToolCallsFromTruncatedMessage`,
  `loop.ts:488-513`) is always sequential and never terminates the batch. We already have this.
- `executionMode` is looked up from the *context's* tools; an unknown tool name
  does not force sequential.
- The nested-call runner honours `executionMode: "sequential"` with its own
  global queue (`nested-tool-calls.ts:203-214`, test `nested-tool-calls.test.ts:164`).

### Events / types involved

No new event types. Ordering contract:
`tool_execution_start` × N (source order) → `tool_execution_end` × N
(completion order; immediates first) → tool-result `message_start/end` × N
(source order) → `turn_end` (source order).

```ts
type ToolExecutionMode = "sequential" | "parallel";   // types.ts:47
executionMode?: ToolExecutionMode;                     // AgentTool, types.ts:510
toolExecution?: ToolExecutionMode;                     // AgentLoopConfig, types.ts:317
```

### What changes in our code

- `agent/tool.py`: `execution_mode: ClassVar[Literal["sequential", "parallel"]] = "parallel"` on `Tool`.
- `LoopConfig.tool_execution: Literal["sequential", "parallel"] = "parallel"`
  (or start with `"sequential"` as the default to keep today's behaviour, and flip later — team choice).
- `loop.py`: replace the `for call in calls:` block with
  `execute_tool_calls(...)` → `_sequential(...)` / `_parallel(...)`, each returning
  `ToolBatch(results: list[ToolResultMessage], terminate: bool)` (step 11 uses `terminate`).
- **The generator problem.** Concurrent tasks cannot `yield` from `run_agent`.
  Options:
  1. *Batch-local queue*: in `_parallel`, start each execution as a task inside an
     `asyncio.TaskGroup`; tasks `await events.put(...)` (updates and end
     events); the generator loops `yield await events.get()` until all tasks are
     done (e.g. a sentinel per task), then yields result messages in source
     order. Smallest change.
  2. *pi's shape*: rewrite the core as `async def run_loop(..., emit)` that
     `await emit(event)`s, and make `run_agent` a thin generator that runs it as a
     task and yields from an `asyncio.Queue` (pi's `agentLoop` + `EventStream`,
     `loop.ts:47-70`). Solves step 7's approval-prompt problem and step 10's
     updates in one place. Use a small bounded queue (`maxsize=1`) to keep pi's
     back-pressure (pi `await`s each emit and its listeners, `agent.ts:618-620`).
     The generator must cancel the producer task in a `finally` (client disconnect
     closes the generator).
  Recommendation: option 2, done once, before or as part of step 9.
- `Promise.all` → `asyncio.TaskGroup` (Python 3.11+). Each per-call coroutine
  must catch `Exception` itself (it already converts failures to error results),
  because one task raising in a `TaskGroup` cancels its siblings, unlike
  `Promise.all` which just rejects. `asyncio.gather` returns results in argument
  order, which gives source order for free.
- Abort: cancellation of the run cancels the `TaskGroup`'s children. pi's
  "answer every call with Operation aborted" is about keeping the transcript
  valid when the run *continues*; ours stops on cancel and
  `prepare_transcript` already repairs missing results. Only needed if we add a
  non-cancelling abort.
- **File mutation queue**: new `tools/mutation_queue.py` with an async context
  manager `async with file_lock(path):`, backed by a module-level
  `dict[Path, asyncio.Lock]` keyed by `path.resolve()` (`resolve()` follows
  symlinks and works for missing files, so no ENOENT fallback is needed).
  `asyncio.Lock` is FIFO-fair and the lookup is synchronous, so pi's
  `registrationQueue` is unnecessary. Delete the dict entry when nobody waits.
  Wrap `edit`'s read-modify-write and `write`'s write in it (`tools/edit.py:72`,
  `tools/write.py:33-`).
- Our `edit`/`write` run blocking I/O through `asyncer.asyncify`, whose default
  `abandon_on_cancel=False` (checked in the installed `asyncer/_main.py`) makes
  a cancelled task wait for the worker thread to finish. So a cancelled write
  still holds the lock until the file write ends — the same guarantee pi gets by
  checking `signal.aborted` after each await.
- `bash`: concurrency is fine for the loop, but two parallel commands writing the
  same files are not protected by the queue (same in pi).

### Suggested tests

- Two slow tools: both run concurrently (an event/latch proves overlap); `tool_execution_end` in completion order; result `message_end`s and `turn_end.tool_results` in source order.
- A tool with `execution_mode="sequential"` in the batch → no overlap for any call.
- `LoopConfig.tool_execution="sequential"` → no overlap.
- A blocked call in a parallel batch ends immediately; the others still run.
- One tool raising does not cancel the others.
- `before_tool_call` runs for all calls before any `execute` starts.
- `file_lock`: same path serializes; different paths overlap; symlink alias shares the lock; two parallel `edit`s on one file keep both changes.
- Cancelling mid-batch leaves no orphan tasks (all tools see `CancelledError`).

---

## Step 10 — Tool progress updates

### How pi does it

`execute` takes a 4th argument `onUpdate` (`types.ts:495-500`), typed
`AgentToolUpdateCallback` (`types.ts:457-463`):

```ts
/**
 * Callback used by tools to stream partial execution updates.
 * The callback is scoped to the current `execute()` invocation. Calls made after
 * the tool promise settles are ignored.
 */
export type AgentToolUpdateCallback<T = any> = (partialResult: AgentToolResult<T>) => void;
```

A partial result has the same shape as a final result (content + details).
The loop wraps it into an event (`emitToolExecutionUpdate`, `loop.ts:781-790`) and
gates it (`executePreparedToolCall`, `loop.ts:829-859`):

```ts
const updateEvents: Promise<void>[] = [];
let acceptingUpdates = true;
...
const result = await prepared.tool.execute(prepared.toolCall.id, prepared.args as never, signal, (partialResult) => {
    if (!acceptingUpdates) return;
    updateEvents.push(Promise.resolve(onUpdate(partialResult)));
});
const durationMs = elapsed();
acceptingUpdates = false;
await Promise.all(updateEvents);
return { result, isError: result.isError === true, durationMs };
```

- The tool calls `onUpdate` synchronously (fire-and-forget); the loop collects
  the emit promises and **awaits them all before returning**, so every update
  event is emitted before that call's `tool_execution_end`.
- After `execute` settles (success, throw, or `finally`), `acceptingUpdates = false`
  and late calls (e.g. from a timer the tool forgot to clear) are dropped silently.
- `runToolCall` passes `options.onUpdate ?? (() => {})` (`loop.ts:820`), so
  nested callers can observe updates (`loop.test.ts:2333`).

**bash throttling** (`tools/bash.ts:305-348`, constant
`BASH_UPDATE_THROTTLE_MS = 100` in `tools/renderers/bash.ts:19`):

```ts
const scheduleOutputUpdate = () => {
    if (!onUpdate) return;
    updateDirty = true;
    const delay = BASH_UPDATE_THROTTLE_MS - (Date.now() - lastUpdateAt);
    if (delay <= 0) { clearUpdateTimer(); emitOutputUpdate(); return; }
    updateTimer ??= setTimeout(() => { updateTimer = undefined; emitOutputUpdate(); }, delay);
};
```

- Leading-edge + trailing-edge throttle: at most one update per 100 ms, and the
  last chunk is never lost (a timer flushes it; `updateDirty` avoids empty repeats).
- Each update carries a **snapshot of the tail of the output so far**, not a
  delta: `content: [{type:"text", text: snapshot.content}]`, `details:
  {truncation?, fullOutputPath}` (`bash.ts:310-322`). The UI just replaces what
  it shows.
- An initial empty update `{content: [], details: undefined}` is sent at start
  (`bash.ts:346-348`) so the UI can show a "running" block immediately.
- `finishOutput` stops accepting output, clears the timer and flushes a final
  update before building the result (`bash.ts:357-365`).

### Edge cases & gotchas

- Late updates after settle are ignored, not errors (`loop.ts:840`, `858`).
- Update events precede `tool_execution_end` for the same call (`loop.ts:846`, `851`).
- In parallel mode updates of different calls interleave; `toolCallId` identifies them.
- pi never persists updates; only the final result becomes a message.
- Nested calls emit `tool_execution_update` with `parentToolCallId` (`nested-tool-calls.ts:221-231`).

### Events / types involved

```ts
| { type: "tool_execution_update"; toolCallId: string; toolName: string; args: any; partialResult: any }  // types.ts:542
```

### What changes in our code

- `agent/events.py`: add
  `ToolExecutionUpdateEvent(type="tool_execution_update", tool_call_id, tool_name, arguments, partial: ToolResult)`
  and add it to the `AgentEvent` union. Update the sequence diagram in the module docstring.
  Run `pnpm gen:api` if events appear in the OpenAPI schema.
- `agent/tool.py`: `execute(self, args, ctx, on_update: OnUpdate | None = None)`,
  `type OnUpdate = Callable[[ToolResult], None]` (sync, like pi). This touches every
  tool's signature (7 tools) — or put `on_update` on a per-call context object
  instead of `ToolContext` (which is per-session and frozen). Team choice; a
  per-call `ToolCallContext(workspace, tool_call_id, on_update)` keeps `execute`'s
  signature stable for future additions (signal, nested calls).
- `loop.py` `execute_prepared`: the callback does `if accepting: queue.put_nowait(event)`;
  set `accepting = False` in a `finally`. With an `asyncio.Queue` (step 9 option 2)
  FIFO order already guarantees updates are yielded before the call's end event,
  so pi's "await all update promises" collapses to "put the end event on the
  same queue afterwards". With the current pure generator, updates cannot be
  yielded while `execute` is awaited — **step 10 needs step 9's event-queue design.**
- `tools/bash.py`: our bash writes output to a temp file, not a pipe
  (ADR-003 step 3), so there are no data callbacks. Options: poll the log file
  every 100 ms in a side task while `proc.wait()` runs and call `on_update` with
  the current tail (reuse `render_output`'s tail logic), or switch to a pipe
  reader + file tee. Polling fits the existing design and is naturally throttled.
  Send an initial empty update, and one final flush.
- `api/sessions.py`: nothing to persist; just yields the new event. Web UI:
  render the latest partial in the tool block.

### Suggested tests

- A fake tool calling `on_update` twice → two update events, both before its `tool_execution_end`.
- `on_update` called after `execute` returned (e.g. stored and called later) → no event, no error.
- Parallel: updates from two tools interleave but each tool's updates precede its own end.
- Bash: a command printing over ~300 ms produces several updates, at least one before the end; the last update contains the final output; no more than ~1 update per 100 ms.

---

## Step 11 — Early termination

### How pi does it

`terminate?: boolean` on `AgentToolResult` (`types.ts:441-445`), on
`AfterToolCallResult` (`types.ts:99-103`) and on `BeforeToolCallResult`
(`types.ts:69-73`). Each documents the same rule: "Early termination only
happens when every finalized tool result in the batch sets this to true."

```ts
function shouldTerminateToolBatch(finalizedCalls: FinalizedToolCallOutcome[]): boolean {
    return finalizedCalls.length > 0 && finalizedCalls.every((finalized) => finalized.result.terminate === true);
}
```
(`loop.ts:690-692`, used by both runners `loop.ts:588`, `658`.)

In `runLoop` (`loop.ts:276-281`): `hasMoreToolCalls = !executedToolBatch.terminate`.
So `terminate` only means "do not send these results back to the model
automatically". The run still goes through `finishTurn`, `turn_end`, the
steering poll and the follow-up poll (`loop.ts:295-317`).

Three sources of `terminate`:

1. the tool's own result (`loop.test.ts:1855-1904`: one call, `terminate: true` → 1 provider call, transcript ends at the tool result);
2. a blocked call: `{block, reason, terminate: true}` (`loop.ts:756-758`, `loop.test.ts:1906-1962`, `#5998` regression);
3. `afterToolCall` returning `{ terminate: true }` (`loop.test.ts:2087-2131`), or `{terminate: false}` to cancel a tool's request.

### Edge cases & gotchas

- **Every** result must set it. A mixed batch continues
  (`loop.test.ts:1964-2020`: one blocked+terminate call, one normal call → second
  provider request; `2022-2085`: two results, one terminating → continues).
- Errors not from a block (unknown tool, validation, thrown) never carry `terminate`.
- `length`-truncated batches never terminate (`loop.ts:512`).
- Empty batch → `false` (`finalizedCalls.length > 0`).
- **`terminate` is not persisted**: `createToolResultMessage` (`loop.ts:933-947`)
  copies content, details, usage, isError, durationMs — not `terminate`. It is
  visible only in `tool_execution_end.result` (`5998` test checks that event).
- **Steering and follow-ups still continue the run** after a terminating batch:
  `while (hasMoreToolCalls || pendingMessages.length > 0)` (`loop.ts:192`) and the
  follow-up check (`loop.ts:311-317`). Inferred from code; I found no test that pins this.
- `finishTurn` returning `continue` after a terminating batch forces one more
  request (`loop.ts:303-323`; see step 12).

### Events / types involved

No new event. `terminate` rides on `ToolResult` and therefore on `tool_execution_end.result`.

### What changes in our code

- `agent/tool.py` `ToolResult`: `terminate: bool = False`. Not copied into
  `ToolResultMessage` (match pi) — or copy it if the UI should show "agent stopped here" after reload; decide.
- `BeforeToolCallResult.terminate` and `AfterToolCallResult.terminate: bool | None` (step 7 types).
- `ToolExecutionEndEvent` today carries only `result: ToolResultMessage`. Either add
  `terminate: bool` to the event, or carry the `ToolResult`. pi's event has the full result.
- `loop.py`: batch returns `terminate = bool(outcomes) and all(o.result.terminate for o in outcomes)`
  (`# TS:` `all()` ≈ `.every()`). Turn loop: stop when there are no calls **or** the
  batch terminated. `AgentEndEvent.reason`: add a value, e.g. `"terminated"`, or reuse `"done"`.

### Suggested tests

- Single call with `terminate=True` → one provider call; run ends after the tool result.
- Two calls, one terminating → second provider call.
- Blocked with `terminate=True` → no execution, one provider call, error result saved.
- `after_tool_call` setting `terminate=True` ends; setting `False` overrides a tool's `True`.
- Truncated (`length`) batch never terminates.

---

## Step 12 — `finishTurn` hook

### How pi does it

Type (`types.ts:134-158`):

```ts
export interface AgentTurnContext { message: AssistantMessage; toolResults: ToolResultMessage[]; context: AgentContext; newMessages: AgentMessage[] }
export type AgentTurnDecision = { action: "continue" } | { action: "end" };
export type FinishTurn = (turn: AgentTurnContext, signal?: AbortSignal)
    => AgentTurnDecision | void | Promise<AgentTurnDecision | undefined> | Promise<void>;
```

Called after the assistant message and all tool-result message events, before
`turn_end` (`loop.ts:289-296`; ordering pinned by `loop.test.ts:1231-1283`:
`["message_end:toolResult", "finishTurn", "turn_end"]`). The context passed
already contains the tool results (`loop.test.ts:1253`).

Scheduling after it returns (`loop.ts:295-326`):

```ts
const decision = await config.finishTurn?.(lastCompletedTurn, signal);
await emit({ type: "turn_end", message, toolResults });
if (decision?.action === "end") {
    await emit({ type: "agent_end", messages: newMessages });
    return;
}
explicitContinuation = decision?.action === "continue";
pendingMessages = (await config.getSteeringMessages?.()) || [];
if (hasMoreToolCalls || pendingMessages.length > 0) {
    explicitContinuation = false;
}
// ...outer loop, after the inner loop exits:
const followUpMessages = (await config.getFollowUpMessages?.()) || [];
if (followUpMessages.length > 0) { explicitContinuation = false; pendingMessages = followUpMessages; continue; }
// No natural request was selected, so fulfill the continuation decision with one context-only turn.
if (explicitContinuation) { explicitContinuation = false; continue; }
break;
```

Semantics:

- **`undefined`** → normal scheduling (continue if tool calls didn't terminate, or steering/follow-ups exist).
- **`{action:"end"}`** → `turn_end` then `agent_end` immediately: no steering poll, no
  follow-up poll, no `prepareNextTurn`, even if the turn had tool calls whose
  results the model has not seen (`loop.test.ts:1340-1394`, `1756-1853`; the
  `steeringPolls === 1` there is the initial poll at `loop.ts:185`).
- **`{action:"continue"}`** → guarantees *one* more provider request, but adds
  none if a natural one is already scheduled: tool results
  (`loop.test.ts:1430-1468`), steering or follow-ups (`loop.test.ts:1470-1525`)
  satisfy it. Otherwise the loop makes exactly one context-only request — no new
  message is appended (`loop.test.ts:1396-1428`: 2 provider calls, 2 `finishTurn` calls).
  It also overrides a terminating batch (`hasMoreToolCalls` is false, so
  `explicitContinuation` survives) — inferred from code.
- **Error/aborted assistant replies are hard exits**: `finishTurn` is still called
  (`loop.ts:254-265`) but its return value is ignored; no follow-up poll, one
  provider call (`loop.test.ts:1285-1338`, run for both `"error"` and `"aborted"`).
- A thrown `finishTurn` is not caught by the loop (no `try` at `loop.ts:295`);
  pi's `Agent` turns any loop exception into an error assistant message +
  `agent_end` (`agent.ts:533-560`).

How the coding agent uses it: `session.ts:949-958` chains it — extension
`turn_end` boundary handlers may request continuation; a previously installed
`finishTurn`'s `end` wins; otherwise `continue` if either asked.

**`prepareNextTurn` and `prepareRequest`** (only what explains `finishTurn`):

- `prepareNextTurn(lastCompletedTurn)` (`types.ts:273-280`) runs at the top of the
  next inner-loop iteration, i.e. only when the loop *will* continue, before
  `turn_start` (`loop.ts:194-217`). It can swap context/model/thinking and append
  messages (that is where pi compacts, `session.ts:961-990`). It then re-polls
  steering if the earlier poll was empty (`loop.ts:210-215`;
  `loop.test.ts:1703`). `end` skips it.
- `prepareRequest` (`types.ts:182-189`) runs right before *every* provider
  request including the first, after pending messages are appended
  (`loop.ts:228-248`), and does not poll queues (`loop.test.ts:1581`).
- Full per-turn order: `finishTurn` → `turn_end` → steering poll →
  (follow-up poll if idle) → `prepareNextTurn` → maybe steering poll →
  `turn_start` → pending messages → `prepareRequest` → provider stream.

### Edge cases & gotchas

- `end` beats everything: queued follow-ups stay queued (`loop.test.ts:1756`, `followUpPolls === 0`).
- `continue` never causes more than one extra request per decision.
- `continue` on an error/aborted reply is ignored.
- The hook sees `newMessages` (everything this run would return) and the full context.

### Events / types involved

No new events. Types above. `AgentTurnContext.newMessages` (`types.ts:142`).

### What changes in our code

- `LoopConfig.finish_turn: Callable[[TurnContext], Awaitable[TurnDecision | None]] | None`,
  `TurnDecision = Literal["end", "continue"]` (simpler than pi's object) or a small model.
  `TurnContext(turn: int, message, tool_results, messages, new_messages)` — include
  `turn` number because our policy (max turns) needs it; pi's `Agent` has no turn counter in the context.
- `loop.py`: today's `for turn in range(1, max_turns + 1)` becomes `while True`
  with the decision logic. Call `finish_turn` **before** yielding `TurnEndEvent`.
  On error/aborted call it but ignore the result. Without steering/follow-ups
  (Phase 3), the rule reduces to:
  - `end` → stop;
  - batch has calls and did not terminate → next turn;
  - else `continue` → one context-only turn; else stop.
- `max_turns` becomes the default `finish_turn` (`end` when `turn >= max_turns`
  and the turn had tool calls). Keep the setting; build the hook from it in
  `api/sessions.py`. Composing several policies = pi's chaining at `session.ts:951-957`
  (any `end` wins, else any `continue`).
- `EndReason`: `"max_turns"` can no longer be inferred by the loop; let the hook
  give a reason (e.g. `TurnDecision(action="end", reason="max_turns")`) or add `"ended_by_hook"`.
- **Gotcha for us:** ending after a turn with tool calls leaves results the model
  never saw. That is valid for the next prompt (results are in the transcript).

### Suggested tests

- Ordering: tool-result `message_end` → `finish_turn` called → `turn_end`.
- `end` after a tool turn → one provider call; tool results saved.
- `continue` on a final-answer turn → exactly one extra provider call with no new message appended.
- `continue` on a tool turn → no extra call beyond the natural one.
- `continue` after a terminating batch → one more call.
- Provider error → `finish_turn` called, run ends `error` even if it returns `continue`.
- Default max-turns policy reproduces today's `test_max_turns_stops_a_run_that_keeps_calling_tools`.

---

## Dependencies & suggested order

1. **Step 0: `LoopConfig` + pipeline split** (no behaviour change). Introduce
   `LoopConfig`, split `execute_tool_call` into prepare/execute/finalize with a
   `ToolCallOutcome`. Every later step needs this.
2. **Step 8 `prepare_arguments`** — smallest, lives entirely in prepare; good warm-up.
3. **Step 7 hooks** — prepare (before) and finalize (after). Pure policy hooks
   (protected paths, deny bash) work with the current generator. **Interactive
   approval does not**: while `run_agent` awaits the hook, it cannot `yield` an
   "approval requested" event. That needs either the emit/queue design below or
   the hook getting an `emit` it can push into.
4. **Event-queue refactor** (pi's `emit` + `EventStream`, `loop.ts:47-70`): core
   loop as a producer task, `run_agent` yields from a bounded `asyncio.Queue`,
   cancels the task in `finally`. Prerequisite for 9 (concurrent tasks), 10
   (updates during `execute`), and interactive approvals in 7. Do it once.
5. **Step 11 `terminate`** — small; needs the batch to return `ToolBatch(results, terminate)`, which step 9 introduces anyway. Can come before 9 with the sequential runner.
6. **Step 9 parallel** — needs 4; touches `edit`/`write` for the file lock.
7. **Step 10 updates** — needs 4; touches every tool's `execute` signature (decide
   the per-call context shape first) and bash.
8. **Step 12 `finish_turn`** — independent of 9/10, but interacts with 11
   (`continue` overrides `terminate`) and replaces `max_turns`. Its full
   semantics only matter once Phase 3 steering/follow-ups (step 14) exist; build
   the reduced rule now, keep the code shaped like pi's (`explicit_continuation`
   flag) so step 14 slots in.

Interactions to keep in mind: abort semantics (7, 9) depend on whether "abort" is
task cancellation (today, and ADR-001) or a flag (possible in step 13);
`terminate` (11) and `finish_turn` (12) both decide whether the loop continues —
put that decision in one place in `loop.py`.

## Open questions

1. **Approval prompts over SSE.** The run is one `POST /sessions/{id}/messages`
   SSE response. Proposed: the hook awaits an `asyncio.Future` stored in an
   in-process registry keyed by request id, emits an `approval_request` event
   (`{id, tool_call_id, tool_name, args}`), and a new route
   `POST /sessions/{id}/approvals/{request_id}` resolves it — pi's RPC
   `extension_ui_request`/`response` (`rpc-mode.ts:95-134`). Timeout and
   disconnect resolve to "deny". This works with a single worker; multiple
   workers need step 13's server-owned runs. Is it acceptable before step 13?
2. **Default policy.** What is gated by default: `bash` always, only patterns like
   pi's `permission-gate.ts`, or writes outside an allowlist too? Does
   "approval" replace the `AGENTSPACE_ENABLE_BASH` opt-in?
3. **Default `tool_execution`**: pi defaults to parallel. Do we flip on day one,
   given parallel delays persisting results until the batch ends (dropped
   connection loses finished results)?
4. **Per-call context vs. growing `execute`'s signature** (`on_update`, later
   abort/nested calls): a per-call `ToolCallContext`, or more parameters?
5. **`after_tool_call` override sentinel**: `None` = "not provided" (pi's `??`).
   Is that enough, or do we need to allow setting `details` back to `None`?
6. **Persist `terminate`** on `ToolResultMessage` or keep it event-only like pi?
7. **`EndReason` once `max_turns` is a hook**: hook-supplied reason, or a generic `"ended_by_hook"`?
8. **Add pi's `structured_content`?** Only needed if a programmatic caller
   (nested tool calls, codemode) appears; it drives the "content-only override drops it" rule.
9. **Hook errors**: copy pi's asymmetry (before = fail-closed, after = real result replaced by an error), or let `after` errors keep the real result and log?
