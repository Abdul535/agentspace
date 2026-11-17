# ADR-006 — The loop emits events through a callback; `run_agent` wraps it as a generator

- **Status:** Accepted
- **Date:** 2026-11-17
- **Deciders:** project owner

## Context
`run_agent` (`agent/loop.py`) is an async generator: the loop body `yield`s each
event, and `send_message` re-yields them as SSE. That fit while the loop was the
only code producing events. Python only lets a generator's own body `yield`, so
code running in another task can't produce an event. Several planned steps need
exactly that (ADR-003):

- **Step 9, parallel tools.** Tools running together under `asyncio.TaskGroup`
  each report `tool_execution_end` when they finish.
- **Step 10, progress.** A tool streams `tool_execution_update` from inside
  `execute`.
- **Approvals on the step 7 hooks.** `before_tool_call` must send an "approval
  needed" event while the loop is paused inside the hook.
- **Step 13, background runs.** A run owned by the server pushes events to
  subscribers that come and go.

pi solves this with a callback. `runLoop` (`packages/agent/src/agent-loop.ts`)
takes `emit: AgentEventSink` and awaits `emit(event)` for every event, and any
code holding `emit` can call it. Its public `agentLoop()` (`:47-70`) starts
`runLoop` in the background, pushes each emitted event into an `EventStream`,
and hands callers that async iterable.

## Decision
- **`run_loop(..., emit)` is the loop.** Same body as before, with every
  `yield event` replaced by `await emit(event)`. `emit` is an async callable,
  `Emit = Callable[[AgentEvent], Awaitable[None]]`, so future emitters that need
  to await (broadcast to subscribers, step 13) fit without changing the type.
- **`run_agent(...)` keeps its signature and stays an async generator**, as a
  thin wrapper like pi's `agentLoop`:
  1. Start `run_loop` as a task, with `emit` putting events on an unbounded
     `asyncio.Queue`.
  2. When the task finishes, a done-callback puts an end marker on the queue.
  3. Yield events from the queue until the marker, then call `task.result()` so
     an exception inside the loop reaches the reader, as it did before.
  4. In `finally`, cancel the task if it is still running and wait for it. A
     closed or cancelled reader (client disconnect) therefore still stops the
     run at its current await (ADR-001).
- **Nothing outside `loop.py` changes.** `send_message`, the SSE event types,
  the generated TS types and the web app see the same events in the same order.
  Step 9 builds on this; the refactor itself adds no concurrency.

## Alternatives considered
- **A queue only around parallel tool batches** — the generator stays the loop,
  and each batch's tasks put events on a queue the generator drains. It is the
  smallest change for step 9, but steps 10 and 13 and approvals would each need
  their own bridge, ending in this design anyway.
- **Keep the generator and pass tools a callback that buffers events until the
  batch ends** — no concurrency issue, but events arrive late (a fast `read`
  reports "done" only when the slow `bash` finishes), which defeats steps 9 and 10.
- **A bounded queue (`maxsize=1`)** — keeps the loop paced by the reader, as the
  generator was. It needs a two-way wait between the queue and the task, because
  a full queue can block the end marker, so it is more code. Nothing needs that
  pacing today: the loop is already paced by the model stream and the tools.

## Consequences
- Any code given `emit` can report events: concurrent tools, hooks waiting for
  approval, and later a server-owned run.
- The loop can run a few events ahead of the reader. Events still arrive in
  emit order, and `send_message` still saves each message on its `message_end`.
  If the client disconnects, events already emitted but not yet read are
  dropped along with the run, the same as an unfinished await was before.
- Cancellation now crosses a task boundary. The wrapper's `finally` owns it, and
  a test pins it (cancelling while `before_tool_call` waits never runs the tool).
- Step 13 can call `run_loop` directly with its own `emit` (fan out to
  subscribers) and skip the wrapper.
