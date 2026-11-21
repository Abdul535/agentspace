# ADR-028 — Numbered run events: resume a dropped stream from `Last-Event-ID`

- **Status:** Accepted
- **Date:** 2026-11-22
- **Deciders:** project owner

## Context
ADR-022 step 32 (ADR-014 step 32), the first step of Milestone B. Today
(ADR-007) a run's events carry no id. If a stream drops mid-run (Wi-Fi, a proxy
or load-balancer timeout, a laptop waking up), the web client shows an error and
stops listening, even though the run keeps going on the server. The only way back
is a reload, which gets a fresh `run_snapshot`.

Step 33 moves events into a Redis Stream per session, so that any API task can
serve any client. A reconnect there lands on a task that has never seen this
client, so it has to know exactly where the client stopped. Redis Stream entries
already have ordered ids, so numbering events now gives step 33 a contract that
only needs a different store behind it.

The ALB (step 58) closes a connection that stays quiet past its idle timeout. A
run that waits a long time on a tool or the model sends nothing during that wait.

How the reference designs do it:

- **opencode:** one SSE stream with a 15 s heartbeat (`: heartbeat` comments,
  `packages/server/src/handlers/event.ts:23`) and **no replay**. A client that
  reconnects fetches state over REST again.
- **pi:** durable's "Watching a Conversation" takes a `from` position and replays
  from there.
- **SSE itself:** each event can carry an `id:`, and a client that reconnects
  sends the last one back as the `Last-Event-ID` header.

## Decision
**1. Every run event is numbered.** `ActiveRun.publish` gives each event the next
`seq` (1, 2, 3, … with no gaps) and wraps it as a `RunEvent(seq, event)`. Its SSE
id is `"<run_id>.<seq>"`, where `run_id` is new for each run. That way an id from
an earlier run of the same session can never be resumed in a later one.

**2. A run keeps its recent events.** `ActiveRun.recent` is a deque of the latest
`REPLAY_SIZE` (10,000) events, kept only while the run lives. ADR-007 turned this
down as "buffering every token". It is bounded now, and step 33 keeps the same
log in Redis (`XADD … MAXLEN`) anyway.

**3. Every stream sends ids.** `POST …/messages`, `…/continue` and `…/compact`
and `GET …/events` yield `ServerSentEvent(data=event, id=…)`. The snapshot gets
the id of the last event it covers, so resuming after a snapshot starts at the
next event.

**4. `GET …/events` reads `Last-Event-ID`.** `ActiveRun.resume(id)` returns the
events after it plus a new subscription, with no `await` in between, so no event
falls in the gap. The stream then sends only those events, with no snapshot. If
the id can't be replayed exactly (another run, malformed, newer than the run, or
older than `recent` still holds), the stream falls back to a snapshot, as before.
With no run going, it is still one snapshot of the saved transcript.

**5. Heartbeat: FastAPI's own.** `EventSourceResponse` (FastAPI 0.163) already
sends a `: ping` comment after 15 s without an event (`fastapi.sse._PING_INTERVAL`).
That is opencode's interval, so we add no code for it, only a test that pins the
behavior. The ALB idle timeout (step 58) must stay above 15 s.

**6. The web client reconnects by itself.** `readEvents` reads `id:` lines and
yields `{ id, event }`. When a stream ends or fails before an `agent_end` (or a
snapshot with `running: false`), `listen` reopens it with `watchRun(…, lastId)`.
It waits 0.5, 1, 2, 4, 8, 8 and 8 s between tries, and the count resets once
events flow again. Meanwhile the chat shows "Connection lost. Reconnecting…". An
HTTP error (`HttpError`: 401, 404, …) or a failure before the first event is not
retried, and neither is a stream stopped on purpose. Once the retries run out, the
error says the run may still be going and to reload.

**7. OpenAPI keeps the event types.** FastAPI leaves `ServerSentEvent` routes out
of the schema, so each stream route declares `responses={200: {"model": …}}`, and
`pnpm gen:api` still produces `AgentEvent` and `RunSnapshot`.

## Alternatives considered
- **Number events per session instead of per run.** Ids would still make sense
  across runs, but the number would have to live in the database to survive a
  restart. Per run, plus a fallback to a snapshot, is enough. Step 33's Redis
  Stream is per session, and its own ids order events across runs.
- **No buffer, always a snapshot (ADR-007 as is).** Already exact on one server.
  But each reconnect resends the whole transcript, and step 33 would still need
  ids to resume on another task.
- **Our own heartbeat task.** It would duplicate what FastAPI already does.
- **Browser `EventSource`, which reconnects by itself.** It can't POST or send our
  auth header, and ADR-007 chose `fetch` for that reason.

## Consequences
- A dropped connection now heals by itself with nothing shown twice or lost. That
  is the contract step 33 builds on: there, `recent` becomes the session's Redis
  Stream and `<run_id>.<seq>` becomes the Stream entry id.
- Supersedes in part ADR-007's "a reconnect gets a fresh snapshot": a reconnect
  with an id gets only the missed events. The snapshot remains for a first attach
  and as the fallback.
- Memory: up to 10,000 events per running run, and each is small (mostly text
  deltas). They are freed when the run ends.
- Still one worker: `recent` lives in this process (step 33 moves it).
