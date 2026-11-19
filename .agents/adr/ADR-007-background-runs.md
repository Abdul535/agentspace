# ADR-007 — Background runs: the server owns a run; clients subscribe, reconnect and abort

- **Status:** Accepted
- **Date:** 2026-11-19
- **Deciders:** project owner

## Context
ADR-003 step 13. Today the HTTP request owns a run (ADR-001):

- `send_message` (`api/sessions.py`) iterates `run_agent` and saves each message
  at `message_end`. `run_agent`'s `finally` (`agent/loop.py`) cancels the loop task
  when the reader stops, so closing the tab, a dropped Wi-Fi link or a proxy
  timeout all kill the run. The half-written reply is lost.
- `claim_run` (`api/deps.py`) holds the "one run per session" slot in
  `app.state.active_runs`, a set in process memory, until the response ends.
- There is no way to stop a run except closing the connection, and no way to
  watch a run from a second tab or after a reload.

Steps 14 (steering and follow-up queues) and 15 (continue) both need a run that
exists apart from any one request.

How the reference designs do it:

- **pi:** the `Agent` object owns the run (`packages/agent/src/agent.ts`):
  `activeRun` holds the promise and an `AbortController` (`:224`, `:527`), and
  `abort()` cancels it (`:350`). `subscribe(listener)` (`:275`) only delivers *new*
  events. A late listener catches up by reading `agent.state` first: the finished
  `messages` plus `streamingMessage`, the reply still being written, which the
  agent keeps up to date from `message_start`/`message_update`/`message_end`
  (`:578-586`).
- **opencode:** a run coordinator (`packages/core/src/session/run-coordinator.ts`)
  runs one execution per session. `POST /prompt` writes to a DB inbox and calls
  `wake()`. Clients listen on one global SSE stream (`GET /event`,
  `packages/server/src/handlers/event.ts`) with a 15 s heartbeat and no replay.
  They fetch state over REST, then apply live events. A "turn in flight" claim is
  written to SQLite (`execution.ts:78-85`), so a run killed by a crash resumes when
  the server starts again.

## Decision
**1. A `RunManager` owns runs.** It is created in `lifespan` and kept on
`app.state.runs`, replacing `active_runs`. It holds one `ActiveRun` per running
session:

- `task`: the `asyncio.Task` that runs `run_loop`. Holding a reference here also
  keeps the task alive, because asyncio keeps only weak references to tasks.
- `messages`: the full transcript: the history loaded at start plus each message
  as it ends.
- `streaming`: the assistant message being written, or `None`, kept up to date
  from `message_start`/`message_update`/`message_end` like pi's `streamingMessage`.
- `subscribers`: a set of bounded `asyncio.Queue`s, one per connected client.

The run's `emit` updates `messages`/`streaming`, saves the message on
`message_end`, then puts the event on every subscriber's queue. If a queue is
full, that subscriber is dropped and its stream ends. A slow client is closed
rather than holding the run back or growing memory. It reconnects and catches up
from the snapshot (point 3).

**2. The run gets its own DB session.** The request's `StoreDep` closes when the
response ends, so the run task opens a session from `app.state.db_factory` and
holds it until the run finishes.

**3. Routes.**

- `POST /sessions/{id}/messages`: claims the session (409 if it is already
  running), starts the run, and streams its events as today. The difference is
  that closing this stream only unsubscribes; the run goes on. The web client
  needs no change for the normal case.
- `GET /sessions/{id}/events` (new): subscribes to the session's run. The first
  SSE event is a `run_snapshot` with `messages` and `streaming`, followed by live
  events. The snapshot is built and the subscriber added in one step with no
  `await` between them, so no event can fall between the two. If no run is
  active, the stream sends a snapshot of the saved transcript and ends.
- `POST /sessions/{id}/abort` (new): cancels the run's task; returns 204, or 409
  if nothing is running. Cancellation behaves as it does today: finished messages
  are kept, and the half-written reply is dropped.
- `GET /sessions/{id}` and `GET /sessions` gain `running: bool`, so the web app
  knows to attach to the events stream after a reload. `PATCH` keeps its 409
  check, now asked of the `RunManager`.

**4. Ending a run.** A done-callback removes the run from the manager, then sends
each subscriber an end marker so its stream closes. A cancelled or crashed loop
never emits its own `agent_end`. So for those, the manager sends one first,
with `reason="aborted"` or `reason="error"` (both already in `EndReason`), and
logs the exception. On shutdown,
`lifespan` cancels every run and awaits them before the engine closes.

**5. `run_agent` stays** as the one-subscriber wrapper (ADR-006). Tests and any
caller that wants "run and read every event" keep using it. The server uses
`run_loop` through the `RunManager`.

**6. One worker, still.** Runs and their subscribers live in this process's
memory. Moving the lock into the database (in ADR-003's step 13 text) is
**deferred**: with several workers, the events would also have to cross between
processes (a pub/sub, for example Redis or Postgres `LISTEN`), or a client
connected to worker B would never see worker A's run. A DB lock on its own would
not solve that. Crash recovery is also out of scope: a run killed by a restart
leaves the transcript as it is today, and `prepare_transcript` patches any tool
call left without a result.

**7. Web.** On load, if `running` is true, open `GET /events` and render from the
snapshot. Add a Stop button that calls `/abort`. `stream.ts` gains a GET variant
of its SSE reader.

## Alternatives considered
- **Event log with replay (`Last-Event-ID`).** Number every event, keep a
  per-run buffer, and replay from the client's last id. Exact, but it means
  buffering every token, and a snapshot gives the same result for one process.
  Worth doing only if clients need every intermediate event, not just the state.
- **Client refetches over REST, then subscribes (opencode).** No snapshot event,
  but events that land between the GET and the subscribe are lost unless the
  client dedupes them. opencode handles this with a global event stream and
  client-side state stores; we have one stream per session, so the snapshot is
  simpler.
- **`POST /messages` returns 202, and every client listens on `GET /events`.** One
  code path for events, but it costs an extra round trip, and the web client
  would have to change for the normal case. The POST stream is kept for now; it
  is just a subscriber.
- **Redis for run state and pub/sub now.** Needed only with several workers; with
  one process, a dict and queues do the job without another service to run.
- **A durable "turn in flight" claim in SQLite (opencode).** Allows resuming after
  a crash, but it brings resume logic that step 15 (continue) should design.
  Deferred to step 15.

## Consequences
- Closing the tab no longer stops a run. Runs end when they finish, hit
  `max_turns`, are aborted, or the server shuts down. A run with no subscribers
  still spends tokens, so the Stop button matters.
- Supersedes ADR-001's "a run is tied to its HTTP request: closing the connection
  cancels it". When this ADR is accepted, ADR-001's status becomes "Superseded in
  part by ADR-007". The rest of ADR-001 stands.
- Still one worker. Several workers need a shared lock *and* cross-process events.
  That belongs in a later ADR, together with sandboxed workspaces (ADR-004).
- Steps 14 and 15 build on this: steering and follow-up messages go to the
  `ActiveRun`, and "continue" starts a run from a stored transcript.
- Tests to add: the run finishes after the POST client disconnects;
  `GET /events` mid-run starts with a snapshot that has the finished messages
  and the partial reply; abort stops the run and frees the slot; a second POST
  while running gets a 409; a full subscriber queue is dropped without stalling
  the run.
