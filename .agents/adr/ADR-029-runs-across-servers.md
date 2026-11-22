# ADR-029 — Runs across several API servers: a `runs` table in Postgres, events and control in Redis Streams

- **Status:** Accepted
- **Date:** 2026-11-22
- **Deciders:** project owner

## Context
ADR-022 step 33 (ADR-014 step 33), the second and last step of Milestone B.
After it, the API service can run with more than one task behind the ALB.

Today every part of a run lives in one process's memory (`runs.py`, ADR-007,
ADR-028):

- **The lock.** `RunManager.runs` (a dict) and `claim_lock` (an `asyncio.Lock`)
  enforce "one run per session". A second task has its own dict, so two tasks
  can each start a run on the same session.
- **The cap.** `RunManager.claim` counts the user's runs in that dict
  (ADR-026, `max_runs_per_user`). Two tasks each count only their own.
- **The events.** `ActiveRun.publish` numbers each event (`<run_id>.<seq>`),
  keeps the last 10,000 in `ActiveRun.recent` and puts it on each subscriber's
  `asyncio.Queue`. A client whose `GET …/events` lands on another task finds
  no run there and gets "not running".
- **The snapshot.** `ActiveRun.snapshot()` reads the reply being written and the
  running tools from memory. Only the owning task has them.
- **Stop, steer, follow-up.** `abort` cancels the `asyncio.Task`; `queue` appends
  to `ActiveRun.steering` / `follow_ups`. Both need the run object, so a request
  that lands on another task gets a 409.
- **`running` on `SessionPublic`.** `runs.is_running(id)`, memory again.
- **A task that dies** (crash, OOM, deploy, `SIGKILL`) takes its runs with it.
  Nobody writes an `agent_end`, so on one task the next start simply has no run;
  on several, the others would never know.

The owner's model for this, from the step 33 walkthrough
(`.agents/learning-progress.md`), in restaurant terms:

- a **shared numbered notebook** every waiter can read = a Redis Stream per
  session;
- **notes the cook who took the order watches** = stop requests and
  steer/follow-up messages, sent to the task that owns the run;
- a **busy sign with a check-in time** on the table = a Postgres `runs` row: one
  active run per session, its owner, and a heartbeat. A sign whose check-in time
  is too old means the cook left: the run is marked interrupted.

How the reference designs do it:

- **opencode** keeps a "turn in flight" claim on the session row in its database
  (`packages/core/src/session/store.ts:82`, set in `execution.ts:78-85` "in the
  same transaction as the started event"). A claim that survives with no
  terminal event means the process died, and the next start resumes it. Queued
  input lives in a DB inbox table (`inbox.ts`), and `wake()` is a doorbell that
  makes the running execution drain again (`run-coordinator.ts:17`). It is one
  process: there is no heartbeat and no second owner, and the bus is in memory.
- **CompanyOS** runs on ECS Fargate with Redis (ElastiCache) for state shared
  between tasks: `REDIS_HOST`, `REDIS_PORT`, `REDIS_SSL` come from Secrets Manager
  (`infra/terraform/environments/prod/variables.tf:200`).
- **pi durable** commits everything to storage before showing it, and a reader in
  another process opens the same storage `readOnly` and watches commits
  ("Watching a Conversation"). Work cut off by a crash stays pending and resumes.
  Resuming mid-run is step 54, not this step: here an orphaned run only gets
  marked interrupted.

## Decision
**In one line:** Postgres says *who* runs a session (a `runs` row with an owner
and a heartbeat); Redis carries *what happens* (a Stream of events per session)
and *what to do* (a Stream of stop/steer/follow-up requests per owning task).
Every task can serve every request.

### 1. The `runs` table
One row per run, kept after the run ends (it is cheap, and step 61 and debugging
both want the history).

| column | type | meaning |
|---|---|---|
| `id` | `text` PK | the run id (`uuid4().hex`), the same one the trace and events use |
| `session_id` | `text` FK → `sessions.id`, `ON DELETE CASCADE` | |
| `user_id` | `text` FK → `users.id` | copied from the session, so the cap is one indexed count |
| `status` | `text` | `running`, `done`, `aborted`, `error`, `interrupted` |
| `owner` | `text` | the task that drives it: `<hostname>:<pid>:<boot uuid>`, new on every start |
| `heartbeat_at` | `timestamptz` | last check-in, always set with the database's `now()` |
| `started_at`, `ended_at` | `timestamptz` | `ended_at` is null while running |

- **One active run per session:** a partial unique index,
  `CREATE UNIQUE INDEX runs_one_active ON runs (session_id) WHERE status = 'running'`.
  Two tasks that insert at the same moment: Postgres lets one in and gives the
  other a unique violation, which becomes `SessionBusyError` (409, as today).
- A second partial index, `(user_id) WHERE status = 'running'`, for the cap.
- **Statuses** map from the loop's `AgentEndEvent.reason`: `done`, `max_turns`,
  `finish_turn` and `terminated` end as `done`; `aborted` and `error` as
  themselves. `interrupted` is new: no one heard from the owner. It is also a new
  `EndReason`, so the web client can say what happened ("The server running this
  reply stopped before it finished. What was saved is kept: press Continue or
  send a message to go on.").
- **Heartbeat: every 5 s.** Each task runs one background loop that does
  `UPDATE runs SET heartbeat_at = now() WHERE owner = :me AND status = 'running'
  RETURNING id`. One query per task, not per run. A run of ours that the query
  did not return was swept by someone else (we were paused or cut off): we cancel
  it locally, so a run never has two drivers.
- **Stale after 30 s** (six missed beats), long enough to ride out a GC pause or
  a slow database. Both are settings (`AGENTSPACE_RUN_HEARTBEAT_SECONDS`,
  `AGENTSPACE_RUN_STALE_SECONDS`), so tests can shorten them.
- **Who sweeps, and when:** every task, in the same 5 s loop, runs
  `UPDATE runs SET status = 'interrupted', ended_at = now() WHERE status = 'running'
  AND heartbeat_at < now() - interval '30 seconds' RETURNING id, session_id`.
  Postgres row locks make two sweepers safe: each row is returned to only one of
  them, and that one appends the closing `agent_end` (`interrupted`) and the end
  marker to the session's Stream, so every open stream ends with a cue. `claim` also sweeps the
  session and the user before it counts, so a dead task's run never blocks a new
  prompt for longer than the stale timeout. The database's clock is the only
  clock, so tasks with skewed clocks agree.
- **Ending a run** is `UPDATE … SET status = :s, ended_at = now() WHERE id = :id
  AND status = 'running'`. The `AND status = 'running'` makes a late finish after
  a sweep a no-op.
- **Shutdown (`SIGTERM`, a deploy):** `RunManager.shutdown` cancels its runs as
  today, but ends them as `interrupted`, not `aborted`, as ADR-022 step 58 asks.
  The user stopped nothing; the server did.
- **A claim that never starts** (`continuable`/`compactable` return 422) deletes
  its row, as `release` frees the slot today.
- **`running` on `SessionPublic`** becomes "has a `running` row": one query for
  the user's running session ids in `list_sessions`, an `EXISTS` for one session.

### 2. Events: one Redis Stream per session
- **Key** `<prefix>session:<session_id>:events`, where `<prefix>` is the new
  setting `AGENTSPACE_REDIS_PREFIX` (default `agentspace:`). Tests give each
  test its own prefix.
- **Writing.** Only the owner writes. `emit` stays: save a finished message to
  Postgres, then `XADD key MAXLEN ~ 10000 * run <run_id> event <json>`, in that
  order, so a client never sees a message that isn't saved. `~` lets Redis trim
  in whole blocks, which is much cheaper and keeps at least 10,000 entries
  (`REPLAY_SIZE`, unchanged). The owner publishes one event at a time, so the
  Stream order is the loop's order.
- **SSE ids are Stream entry ids** (`1764000000000-0`: milliseconds, then a
  counter). They replace `<run_id>.<seq>`, and `ActiveRun.seq` and `recent` go
  away. The web client needs **no change**: `stream.ts` reads `id:` as a string,
  `use-agent-run.ts` keeps the last one and sends it back as `Last-Event-ID`,
  and never parses it.
- **Reading.** Every SSE stream reads from Redis, the owner's own `POST
  …/messages` stream included, so there is one path. Each open stream runs its
  own `XREAD BLOCK 5000 … STREAMS key <last id>` loop, so it holds one Redis
  connection while it waits. That is the simplest exact design: every stream
  reads from its own position, with no hand-over between a catch-up read and a
  shared live feed. One shared reader per session per task can come later if
  connection counts ever matter.
- **Resume (`Last-Event-ID`).** Exact or not at all, as in ADR-028. The id is
  replayed when it parses as a Stream id and its entry is still in the Stream
  and belongs to the running run. Trimming only drops the oldest entries, so then
  every entry after it is there too. The run may have ended since: its Stream
  outlives it by an hour, so the client still gets the tail, ending with the
  `agent_end`. That matters on a deploy, where the client's stream dies with its
  server and reconnects elsewhere; that way it learns the run was interrupted
  instead of finding the reply silently cut short. Otherwise (an old
  `<run_id>.<seq>` id from before the deploy, another run, or too old) the stream
  falls back to a snapshot.
- **Snapshot, built by any task.** Checkpoints carry two more fields: `saved`
  (the transcript length in Postgres at that point) and `pending` (the tool calls
  still running). The first checkpoint is an entry with no event, written by
  `claim`. After that, every `message_end` entry is one. To build a snapshot, a
  task reads the Stream backwards to the run's newest checkpoint and loads
  `messages[:saved]` from Postgres. Messages are append-only (`MessageRow.seq`),
  so that is exactly the transcript at the checkpoint. It then folds the entries
  after it with `ActiveRun.track`, which rebuilds the reply being written. The
  snapshot's id is the last entry it folded. It never reads the owner's memory,
  so the owner can be busy, or on another machine. Only a single reply of more
  than 10,000 events can lose its checkpoint to trimming. Then the snapshot
  replays what is left over the saved transcript.
- **End of a stream:** an end-marker entry (`run <id> end 1`). The run's
  `agent_end` can't be it: after one, the loop may still compact and run again.
  The owner writes the marker when its task is done, after ending the row. A
  sweeper writes it for a run it ended. A stream whose read comes back empty
  also checks the `runs` row, so a run whose marker never reached Redis (lost in
  a failover) still ends its streams. With no `running` row, `GET …/events` is
  one snapshot of the saved transcript, as today.
- **Cleanup.** When a run ends, the owner sets `EXPIRE key 3600`: the tail stays
  an hour for slow reconnects, then Redis frees it. `claim` empties the Stream
  for the next run (`XTRIM MAXLEN 0`, exact, then `PERSIST`) in the same
  transaction that writes its first checkpoint. So the Stream only ever holds the
  current run, or the last one.

### 3. Stop, steer and follow-up: a control Stream per owning task
- **Key** `<prefix>server:<owner>:control`. Each task reads only its own, with one
  `XREAD BLOCK` loop. Entries name the run:
  `run <run_id> kind abort|steer|follow_up content <text>`. A Stream, not
  pub/sub, so a message sent while the owner was reconnecting to Redis is read
  when it comes back, not lost.
- **Any task takes the request.** The route loads the `running` row (none: 409,
  as today), sweeps it if its heartbeat is stale (then 409 for steer/follow-up),
  and `XADD`s to the owner's control Stream.
- **Steer/follow-up return 202 once the entry is written.** The owner puts it on
  the run's `MessageQueue`. If the run has already made its last poll
  (`accepting` is False), the owner drops it. That is safe: the web client
  already keeps what it queued, and anything not delivered by `agent_end` comes
  back through `onUnsent`. So the 409 for "the run will never pick it up" goes
  away; the 409 for "no run" stays.
- **Abort** writes `abort`, then waits up to 10 s on the session's event Stream
  for the run's `agent_end`, so the 204 still means "stopped". If the owner is
  gone (stale heartbeat), the route does what a sweeper would, but ends the run
  as `aborted`, since the user asked. If the owner is alive but slow past 10 s,
  the route returns 202: the stop is on its way, and the open stream ends with
  `agent_end` when it lands.
- **The owner is gone with entries left** in its control Stream: no one reads
  them. Every boot gets a new owner id, so no new task inherits them. The Stream
  gets `EXPIRE` 1 h, renewed by its owner's heartbeat, so it disappears after
  its owner does.

### 4. Max runs per user (ADR-026)
`claim` becomes one Postgres transaction:

1. `SELECT … FROM users WHERE id = :user FOR UPDATE`: a row lock that lines up
   two prompts of the same user, on any tasks, while other users go on in
   parallel. It replaces `claim_lock`.
2. Sweep this user's stale runs.
3. `SELECT count(*) FROM runs WHERE user_id = :user AND status = 'running'`; at
   the cap, `TooManyRunsError` (429 `too_many_runs`, unchanged).
4. `INSERT` the `running` row; a unique violation is `SessionBusyError`.
5. Commit, then load the history. No one else can append now, so it is complete.

The setting, the error codes and the UI stay as ADR-026 has them.

### 5. Local dev, CI and tests
- **Redis 8.2** (`redis:8.2-alpine` on Docker Hub). Streams need Redis 5 or
  later; production uses ElastiCache for Valkey 8, which speaks the same protocol
  (step 58 sets it up).
  Python client: `redis` (redis-py, which ships `redis.asyncio`), added with
  `uv add redis`.
- **Local dev: started by `scripts/db.sh`,** next to Postgres, not by a
  `docker compose` file. The repo has no compose file; Postgres comes from the
  Supabase CLI. `db.sh` already runs before `pnpm dev:api` and `pnpm check:api`,
  checks that Docker is up, and is safe to rerun. It starts one shared container
  if it isn't running:
  `docker run -d --name agentspace_redis --restart unless-stopped -p 127.0.0.1:55379:6379 redis:8.2-alpine`.
  The port sits next to Supabase's (`55322`). Every checkout shares it, as they
  share Postgres. That is safe because session ids are UUIDs and owner ids are
  new per boot. `AGENTSPACE_REDIS_URL` defaults to `redis://127.0.0.1:55379/0`.
  `pnpm dev:api` stays one command.
- **The API fails at startup** with a clear message if Redis is unreachable, as
  it does for Postgres. There is no in-memory fallback mode: two code paths
  would let the one we don't test break.
- **CI:** a `redis` service in the `api` job of `.github/workflows/ci.yml`,
  `image: redis:8.2-alpine` (Docker Hub, not `public.ecr.aws`: CI hit ECR
  Public's "Data limit exceeded"), port `6379:6379`, health check
  `redis-cli ping`, and `AGENTSPACE_TEST_REDIS_URL` set like
  `AGENTSPACE_TEST_DATABASE_URL`.
- **Tests: real Redis, no fake,** for the same reason ADR-027 chose real Postgres.
  The behavior we rely on (blocking `XREAD` across connections, exact and
  approximate trimming, `EXPIRE` on streams) is where a fake would differ. Each test gets a random `AGENTSPACE_REDIS_PREFIX` and deletes its keys
  after. The multi-server tests run two `RunManager`s with different owner ids
  on one Redis and one database: prompt on A, watch, steer and abort through B.
  Kill A's heartbeat, and B sweeps the run as `interrupted`.

## Alternatives considered
- **Postgres `LISTEN/NOTIFY` for events (ADR-022's fallback).** One less service,
  but a payload is capped at 8 KB (one tool result can be bigger), nothing is
  kept, so there is no replay from `Last-Event-ID`, and each listening task holds
  a dedicated connection that Supabase's transaction-mode pooler doesn't support.
  We'd have to write events to a table for replay and use NOTIFY only as a
  doorbell, which rebuilds a Stream by hand. It stays the fallback if Redis ever
  has to go.
- **Keep one server.** ADR-022 already turned this down: many users need more
  than one task, and each deploy would cut off every run.
- **Sticky sessions at the ALB** (cookie stickiness to the task that runs the
  session). It doesn't cover a second tab or device that got another cookie, or a
  stop request from one. When the task dies, the client sticks to nothing, so we
  would still need the sweep. Load spreads unevenly. And step 61 (workers) needs
  the shared Stream anyway.
- **Redis pub/sub for events.** Fire-and-forget: a listener that blinks misses
  events, and nothing is kept for replay.
- **A Redis lock (`SET NX PX`) instead of the `runs` row.** Fast, but the cap
  needs a count that is consistent with the lock, the history is worth keeping,
  and a lock that expires while its holder is paused needs the same fencing
  anyway. Postgres already gives us a transaction and a unique index.
- **The owner answers snapshot requests** over the control Stream. That is exact
  from memory, but it is a request/reply on every attach, it fails exactly when
  the owner is slow or gone, and the whole transcript (images included) would
  travel through Redis. Folding from a checkpoint needs only Postgres and the
  Stream.
- **A Stream of control entries per run**, instead of per owner. Same delivery,
  but one blocking reader per running run instead of one per task.
- **opencode's DB inbox for steer/follow-up.** It survives the owner dying, but
  the run that would read it dies too until step 54 can resume runs. Worth
  revisiting then.
- **`fakeredis` in tests.** Faster and needs no service, but see §5: the parts we
  depend on are where it would differ from the real thing.

## Consequences
- **Supersedes in part ADR-007:** the run lock, the event fan-out, the snapshot,
  stop and the steer/follow-up queues' delivery move out of process memory. The
  rest of ADR-007 stands: the server owns the run, and clients subscribe,
  reconnect and abort. Its status line changes to say so.
- **Supersedes in part ADR-028:** Stream entry ids replace `<run_id>.<seq>`, and
  the Stream replaces `ActiveRun.recent`. The `Last-Event-ID` contract, the
  heartbeat and the web client's reconnect are unchanged.
- The API can now run with desired count > 1 (step 58 can raise it, with
  ElastiCache added). A request may land on any task.
- **But keep one API task until Milestone C.** A session's workspace files still
  live on the disk of the task that ran the agent: a run on task A writes a file
  that the next run, on task B, can't read. Steps 59 and 28 move workspaces into a
  sandbox every task reaches; only then can the desired count go above 1.
- **One more service everywhere:** Redis in dev (via `db.sh`), CI and production.
  If Redis is down, runs can't start or be watched. Saved transcripts still load,
  since they are in Postgres.
- A dying task's runs end within about 30 s as `interrupted`, with an
  `agent_end` on every open stream and a message in the UI. The reply that was
  half written is lost; finished messages are kept. Step 54 can resume instead.
- **One small web change:** show the new `interrupted` reason.
- Steer/follow-up sent just as the run ends now get a 202 and come back through
  `onUnsent`, not a 409. The UX is the same, because the client already handles
  both.
- Each open SSE stream holds one Redis connection while it waits (ElastiCache
  allows tens of thousands). A shared reader per session per task can come later.
- Each event is one extra hop (owner → Redis → reader), a millisecond or two
  inside one AWS region.
- `runs` rows accumulate (one per prompt); add retention later if they ever
  matter.
- **Leads to step 61 (workers):** a worker is a task that owns runs and never
  serves HTTP. The `runs` row, the event Stream and the control Stream are
  already the whole interface between the two, so step 61 only adds the queue
  that hands a run to a worker.
