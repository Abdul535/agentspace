# Running several tool calls from one model reply

**Date:** 2026-11-17

## Symptom
A model reply can hold several tool calls (say `read` + `bash`). Our loop runs
them one after another (`for call in calls:` in
`apps/api/src/agentspace/agent/loop.py`), so a quick `read` waits behind a slow
`bash`. pi runs them in parallel by default. Copying that isn't a one-line
change, because our loop can't report events from tools running at the same time.

## Cause
**pi has two runners** (`packages/agent/src/agent-loop.ts` in `../pi-upstream`):

- `executeToolCalls` (`:518-533`) picks **sequential** when the agent is set to
  `toolExecution: "sequential"` or when *any* tool in the batch has
  `executionMode: "sequential"`. Otherwise it picks **parallel**, which is the
  default (`packages/agent/src/agent.ts:261`).
- **Sequential** (`:540-590`): each call runs start → prepare → execute →
  finalize → end → result message before the next one starts.
- **Parallel** (`:592-660`) works in three phases:
  1. **Prepare in order, one at a time.** For each call it emits
     `tool_execution_start` and runs `prepareToolCall` (find the tool,
     `prepareArguments`, validate, `beforeToolCall`). A failure or a block is
     recorded right away. Otherwise it stores a function that will execute the
     call later. This is sequential on purpose: approval prompts come one at a
     time, in the model's order.
  2. **Execute concurrently.** `Promise.all` runs the stored functions. Each one
     emits its own `tool_execution_update` and `tool_execution_end` as soon as it
     finishes.
  3. **Results in call order.** The `toolResult` messages go out in the model's
     call order once every call is done, even if a later call finished first.

**Why pi can do this and we can't yet.** pi's loop reports events through an
`emit()` callback, which any concurrent task can call. Our `run_agent` is an
async generator, and only the generator body can `yield`. A tool running inside
`asyncio.gather` has no way to yield its own events.

TS comparison: it's the difference between passing `onEvent` into
`Promise.all` workers, which all call it, and an `async function*` that can
only `yield` from its own body, never from inside a `.then()` or a worker.

**Same-file writes** are handled inside the tools, not in the loop. pi's `write`
and `edit` wrap their I/O in `withFileMutationQueue(path, fn)`
(`packages/coding-agent/src/core/tools/file-mutation-queue.ts`). It keeps one
promise chain per real path (symlinks resolved), so writes to the same file
wait their turn while writes to different files still run in parallel.

## Fix
Built in ADR-003 step 9, on top of ADR-006.

- **Bridge, done once (ADR-006).** The loop core became
  `run_loop(..., emit)` like pi's `runLoop`, and `run_agent` became a thin
  generator over it: it runs `run_loop` as a task feeding an `asyncio.Queue`
  and yields from the queue. Any task can now call `emit`.
- **pi's three phases** in `run_in_parallel`
  (`apps/api/src/agentspace/agent/loop.py`): prepare in order, run the cleared
  calls in an `asyncio.TaskGroup` (TS: `Promise.all`, except that cancelling
  the run also cancels every running tool), then emit result messages in call
  order.
- **Mode:** `LoopConfig.tool_execution` defaults to `"parallel"`, and
  `Tool.execution_mode = "sequential"` makes the whole batch sequential.
- **Same-file safety:** `tools/file_lock.py`, with one `asyncio.Lock` per
  resolved path, wraps `write` and `edit`'s whole read-modify-write. Without
  it, two parallel edits to one file lost a change in 20 of 20 runs; with it,
  in 0 of 20.

## Not a bug
- **Result messages don't follow finish order.** In parallel mode
  `tool_execution_end` fires when each call finishes, but the `toolResult`
  messages come out in the order the model made the calls. The model always
  reads results lined up with its calls.
- **Preparation isn't parallel**, even in parallel mode, so `beforeToolCall`
  approval prompts never pile up.
- **One sequential tool makes the whole batch sequential.** It doesn't split
  the batch.
- **pi's built-in tools don't set `executionMode`.** Same-file safety comes from
  the file mutation queue. `executionMode` is for extension tools, such as one
  that asks the user a question.
- **The run ends early only if every result sets `terminate`**
  (`shouldTerminateToolBatch`, `agent-loop.ts:690`). This holds in both modes.
