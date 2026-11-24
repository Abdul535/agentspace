# ADR-030 — First deploy: one API task on ECS Fargate, Upstash Redis, web on Vercel, a drain on SIGTERM

- **Status:** Accepted; §4 (infrastructure set up by hand) superseded by ADR-031
- **Date:** 2026-11-22
- **Deciders:** project owner

## Context
ADR-022 step 58. Everything before it is built: Postgres on Supabase (ADR-027),
login (ADR-024), per-user data and limits (ADR-025, ADR-026), runs shared
across servers through Postgres and Redis (ADR-028, ADR-029). Nothing runs
anywhere but a laptop yet: there is no image, no AWS setup and no production
settings.

Two things shape the step:

- **Workspace files are on the task's own disk** (`<data_dir>/workspaces`).
  Sandboxes (Milestone C) are skipped for now. With two tasks, a file the agent
  writes on one is missing on the other.
- **A deploy stops the old task.** ECS sends `SIGTERM`, then `SIGKILL` after
  the container's `stopTimeout` (default 30 s, at most 120 s on Fargate). An
  agent run can take many minutes. Today `RunManager.shutdown` ends every run
  as interrupted the moment shutdown starts.

The owner's prediction (`.agents/learning-progress.md`): on `SIGTERM`, wait for
the running work to finish. Right direction; the wait needs a limit, because
`SIGKILL` comes whatever the app is doing.

How the reference designs do it:

- **CompanyOS** runs its API on ECS Fargate behind an ALB
  (`infra/terraform/modules/ecs-api/`). It raises `stopTimeout` to 120 s and the
  target group's `deregistration_delay` to 300 s: with the old 30 s, *"every
  deploy, scale-in and Spot reclaim cut live turns mid-frame"*
  (`variables.tf:76`). Its gateway drains for a budget (`GATEWAY_DRAIN_MS`)
  before it exits. Secrets come from Secrets Manager, and migrations run in the
  deploy, not at app start.
- **pi and opencode** are local apps: nothing to deploy.

## Decision
**In one line:** one Docker image runs one API task on ECS Fargate behind an
ALB. Postgres is Supabase, Redis is Upstash, the web app is on Vercel, and a
deploy lets running replies finish for up to 100 s before it interrupts them.

### 1. One API task for now
`desiredCount = 1`, no autoscaling. ADR-029 makes several tasks safe for runs,
but the workspace files (above) are not. Going to more than one task waits for
sandboxes (ADR-022 step 28) or a shared disk. The deploy still overlaps two
tasks for a short while (below). That is safe: runs are claimed in Postgres, and
the overlap is short.

Workspace files are lost on every deploy, as ADR-022 already says. The web app
must say so before this is public; that is its own change.

### 2. The image (`apps/api/Dockerfile`)
- Built in two stages from `ghcr.io/astral-sh/uv` (Python 3.13). Dependencies go
  in their own layer (`uv sync --locked --no-dev --no-install-project`), so a
  change to the code only rebuilds the last layer.
- The final stage is `python:3.13-slim-bookworm` with `ripgrep` (the `grep` and
  `find` tools need it), a non-root `app` user, and `AGENTSPACE_DATA_DIR=/app/data`.
- **One image, two commands.** The service runs `uvicorn`. The migration task
  runs the same image with the command `alembic upgrade head`.
- Started with `uvicorn ... --proxy-headers --timeout-graceful-shutdown 5`, not
  `fastapi run`, which can't set the graceful timeout.

### 3. Draining on a deploy
A rolling deploy with `minimumHealthyPercent = 100`, `maximumPercent = 200`:

1. ECS starts the new task. Once `/health` passes, the ALB sends it new
   requests.
2. The old task is deregistered. The ALB sends it nothing new, and keeps its
   open streams for up to **`deregistration_delay = 120` s**. Its runs go on.
3. ECS sends `SIGTERM`. uvicorn stops listening and closes the remaining
   connections within **5 s**. The open streams drop, and clients reconnect
   with `Last-Event-ID` (ADR-028). They reach the new task, which reads the same
   Redis Stream (ADR-029), so they keep seeing the reply as it is written.
4. The app's shutdown waits up to **`AGENTSPACE_SHUTDOWN_GRACE_SECONDS = 100`**
   for its runs to finish. Check-ins and stop/steer requests still work during
   that time, so Stop pressed in the browser still reaches the run.
5. Runs still going after that end as `interrupted` (ADR-029), with a closing
   `agent_end`. The web client tells the user to press Continue.
6. **`stopTimeout = 120`**, which leaves 15 s for the last saves and the
   tracer's flush before `SIGKILL`.

The new setting defaults to `0`, so `fastapi dev` reloads and the test suite
don't wait. The ALB's idle timeout stays above the stream's 15 s ping
(ADR-028); 120 s is the value used.

### 4. AWS pieces
- **ECR**, for the image.
- **ECS Fargate**: 0.5 vCPU / 1 GiB, port 8000, logs to CloudWatch.
- **ALB**:
  - HTTPS on 443 with an ACM certificate for `api.<domain>`.
  - A target group with a `/health` check.
  - Deregistration delay 120 s, idle timeout 120 s, no stickiness.
- **Secrets Manager** for the secret settings, injected as environment
  variables by the task definition (below).
- **Migrations** run as a one-off `aws ecs run-task` with the command overridden
  to `alembic upgrade head`, before `aws ecs update-service`. A failed migration
  stops the deploy before any task runs new code.
- The infrastructure is set up by hand (console or CLI) for this one service.
  Infrastructure as code (CompanyOS uses Terraform) is left for when there is
  more than one service.

### 5. Postgres: Supabase's session pooler
`AGENTSPACE_DATABASE_URL` points at the **session pooler**
(`<project>.pooler.supabase.com:5432`). The direct connection is IPv6-only
without Supabase's IPv4 add-on, and Fargate's default networking is IPv4
(ADR-027). The session pooler keeps one server connection per client
connection, so asyncpg's prepared statements work as they do locally.

### 6. Redis: Upstash, not ElastiCache
This replaces ADR-029's "production uses ElastiCache for Valkey 8".

- Upstash speaks the Redis protocol over TLS (`rediss://`). Its docs list every
  command `run_log.py` uses: `XADD` with `MAXLEN ~`, `XREAD BLOCK`, `XTRIM`,
  `XREVRANGE`, `MULTI`/`EXEC`, `EXPIRE` and `PERSIST`.
- **A Fixed plan**, not Pay-as-you-go. Pay-as-you-go bills every command, and
  this app sends many even when idle: every open stream blocks on `XREAD` for
  5 s at a time, the control reader does the same, and every token of a reply is
  an `XADD`. Fixed plans have no per-command billing.
- **An idle server reads its control Stream every 30 s, not 5 s**
  (`CONTROL_BLOCK_MS`), and renews that Stream's expiry once per read, not on
  every heartbeat. A blocked read returns as soon as a request is written, so
  Stop is still instant. Measured on an idle server: 4 commands a minute,
  down from 24. That's about 170K a month, so a test server fits Upstash's free
  500K. Event reads stay at 5 s: they exist only while a run streams, and their
  empty reads are what notice a run whose end marker was lost.
- **The same AWS region** as the ECS task. Upstash is reached over the internet,
  so a far region adds its round trip to every event.
- **Checked before the first deploy:** the test suite run once against an
  Upstash database (`AGENTSPACE_TEST_REDIS_URL=rediss://…`). The tests cover the
  behavior a Redis-like service could differ on, such as blocking reads across
  connections and trimming.

### 7. Settings in production
| Setting | Value |
|---|---|
| `AGENTSPACE_DATABASE_URL` | the session pooler URL, as `postgresql+asyncpg://` (secret) |
| `AGENTSPACE_REDIS_URL` | `rediss://default:<password>@<host>.upstash.io:6379` (secret) |
| `AGENTSPACE_SECRET_KEY` | `openssl rand -base64 32`, set once and never changed (secret) |
| `AGENTSPACE_SUPABASE_URL` | the Supabase project URL |
| `AGENTSPACE_AUTO_MIGRATE` | `false`; the migration task does it |
| `AGENTSPACE_SHUTDOWN_GRACE_SECONDS` | `100` |
| `AGENTSPACE_ENABLE_BASH`, `AGENTSPACE_ENABLE_MCP` | `false` (the defaults) |
| `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY` | optional (secret) |

`AGENTSPACE_SECRET_KEY` is required in production, not optional. Without it the
app generates a key into `/app/data`, which a deploy wipes. After that, every
saved provider key and MCP env value could no longer be decrypted.

`OPENROUTER_API_KEY` is not set: users bring their own key (ADR-026).

### 8. The web app on Vercel
- `apps/web` deploys from the monorepo with the root directory `apps/web`.
- `API_URL=https://api.<domain>`. The `/api/*` rewrite in `next.config.ts`
  sends the browser's calls there, so there is still no CORS.
- Add the Vercel URL to Supabase Auth's redirect URLs.
- **To check on the first deploy:** that a long reply's stream passes through
  the rewrite without being cut. If Vercel's proxy cuts it, the client
  reconnects with `Last-Event-ID` (ADR-028) and loses nothing. If that happens
  often, the web app can call `api.<domain>` directly for streams only, behind
  CORS.

## Alternatives considered
- **Wait for every run on `SIGTERM`** (the owner's prediction, taken
  literally). `SIGKILL` comes after `stopTimeout` (at most 120 s) whatever the
  app is doing. A run still going then is cut with no `agent_end` and no
  `interrupted` status. A run is left for the next server's sweep only after
  30 s of silence (ADR-029). Waiting up to a budget, then interrupting, always
  ends with a cue.
- **Several tasks with EFS for the workspaces.** Files would survive deploys,
  and every task would see them. It is one more AWS piece to run and pay for,
  and ADR-022 rejects it for the public product (with `bash`, files sit next to
  our secrets). Kept as a stopgap if one task is not enough before sandboxes.
- **ElastiCache instead of Upstash.** It sits inside the VPC (lower latency, no
  internet hop) and needs no outside vendor. It costs more at this size (a
  primary and a replica, billed by the hour) and must be sized by hand. Upstash's
  smallest Fixed plan is enough for now. The app only needs a `rediss://` URL,
  so moving later is a change of setting.
- **Run migrations at app start (`auto_migrate`).** Two tasks starting together
  would race, and a failed migration would leave a task crash-looping halfway
  through a rollout. ADR-022 already rules it out for production.
- **The web app on ECS too.** One cloud, but Vercel builds and serves Next.js
  with no setup, and the web app has no state to share with the API.

## Consequences
- The API can be deployed. Each deploy keeps running replies alive for up to
  about 220 s (deregistration delay plus grace). Only longer replies end as
  `interrupted`, and they end with a message and a Continue.
- One task means no redundancy. A crash or an AZ outage stops the API until ECS
  starts a new task (a minute or two). Runs on the dead task are marked
  interrupted by the next task's sweep.
- Workspace files are lost on each deploy until sandboxes arrive. The UI must
  say so before the app is public.
- Moving to more than one task is a change of `desiredCount` plus autoscaling,
  once workspaces leave the task's disk.
- A new setting, `AGENTSPACE_SHUTDOWN_GRACE_SECONDS`, must stay below
  `stopTimeout` minus the time the last saves take.
- The image is checked by building it; CI does not build it yet.
- Checked against Upstash on 2026-11-22: 24 of 26 run tests passed. One test
  reads `CLIENT LIST`, which Upstash leaves out; that's test-only. The other
  showed a gap that exists with any Redis: a sweep commits `interrupted` in
  Postgres before it writes the closing `agent_end` to Redis. A `GET …/events`
  that lands between the two gets the saved transcript without the
  "interrupted" cue. In-region, that window is one Redis round trip (a few ms).
  Closing it is a follow-up.
