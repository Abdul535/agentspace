# ADR-022 — Roadmap: scale out to a public, multi-user service

- **Status:** Proposed
- **Date:** 2026-11-21
- **Deciders:** project owner

## Context
The owner wants agentspace hosted for **many public users** (anyone can sign
up), on AWS (ECS Fargate), with more than one server. Today the API runs as one
process on one machine:

- **Database:** SQLite, a file in `data_dir` (`config.py:73`).
- **Workspaces and skills:** folders on that machine's disk
  (`data/workspaces/<session_id>/`, `data/skills/`).
- **Runs:** `RunManager` (`runs.py`, ADR-007) keeps each run's `asyncio.Task`,
  its transcript, the reply being written, and its SSE subscribers in memory.
  "One run per session" is enforced in that memory too.
- **MCP:** `mcp/manager.py` starts stdio servers as child processes of the API
  and keeps them in memory, and the server rows (`args`, `env`) are shared by
  everyone.
- **Tools:** `bash`, `grep`/`find` (`rg`) and the file tools act on the host
  with the API's permissions (ADR-004 is still Proposed: there is no
  `Workspace` protocol yet).
- **Auth:** none. Anyone who can reach the API can run the agent on our
  OpenRouter key.
- **Migrations:** `auto_migrate` applies them when the app starts (`main.py:31`).

On serverless (Vercel) almost all of this breaks. On Fargate it runs as **one**
task, but the disk is wiped on every deploy and a second task would not know
about the first one's runs. The owner chose a managed Postgres, a public
multi-user product, and a hosted login provider (the "build from scratch" rule
covers the agent, not auth).

ADR-014 already lists several of the steps needed: 27 (API auth), 28 (sandbox),
32 (event replay) and 33 (runs across workers). This ADR keeps those numbers,
adds the missing steps from 55 on, and fixes the order for this goal.

How the reference designs do it:

- **CompanyOS** runs on ECS Fargate (`infra/GITOPS.md`): API, gateway and
  frontend as separate services, Terraform for load balancers and autoscaling,
  one Secrets Manager document per environment, migrations applied by the deploy
  workflow before services roll, Redis for shared state, and a list of allowed
  sandbox providers for agent work.
- **opencode** claims a "turn in flight" in SQLite
  (`packages/core/src/session/run-coordinator.ts`, `execution.ts:78-85`) so a
  run killed by a crash resumes on restart, and streams events over one SSE
  endpoint with a 15 s heartbeat.
- **pi** is local-only: its `durable` package is the reference for runs that
  outlive a process (step 54).

## Decision
Scale out in four milestones. Each one leaves a working, deployable app, so
there is never one big rewrite. Each step is one change (one branch, one PR),
and each step marked **(ADR)** gets its own ADR, with the owner's prediction
first, as for every roadmap step.

### Milestone A — Safe public beta on one task
After A the app can be public, on one Fargate task, with `bash` and stdio MCP
**off** (they would run on our server).

55. [x] **Postgres (ADR-027: everywhere, local Supabase for dev and tests).** `uv add asyncpg`; `AGENTSPACE_DATABASE_URL=
    postgresql+asyncpg://...`. Run the test suite and every migration against
    Postgres. CI gets a Postgres service so tests use what production uses.
    To decide in its ADR: is SQLite kept for local dev and tests, or is Postgres
    used everywhere (a `docker compose` for dev)? Check: JSON columns
    (`session/models.py`) as `JSONB`, timezones on `UTCDateTime`, string lengths,
    and the SQLite-only PRAGMAs in `session/db.py` staying behind their dialect
    check.
27. [ ] **Login and API auth (ADR).** A hosted provider (Clerk, Cognito or
    Supabase Auth, chosen in the ADR) handles sign-up and sign-in. The web app
    sends its token, and a FastAPI dependency checks the JWT on every route
    except health. Add a Host/Origin allowlist and CORS. Designed sign-in,
    signed-out and expired states in the UI.
56. [x] **Users own their data (ADR-025).** A `users` table keyed by the provider's
    user id. `user_id` goes on sessions and MCP servers, and every query filters
    by it: another user's session is a 404, never a 403, so its existence leaks
    nothing. Decide whether skills are global read-only or per user. Encrypt
    MCP `env` values at rest (they hold users' secrets).
57. [x] **Limits per user (ADR-026: BYOK for everyone, max runs at once).** At most N concurrent runs per user, a daily
    spend cap from the cost we already store (ADR-019), and request rate limits.
    Decide: our OpenRouter key with caps, or users bring their own key, or both.
    The UI says when and why a limit was hit and when it resets.
58. [ ] **First deploy (ADR).** One Dockerfile for the API (uv, Python 3.13).
    Fargate service with **desired count 1**, RDS Postgres, ALB with a raised
    idle timeout for SSE, secrets from Secrets Manager. Migrations run as a
    one-off task before the service rolls, and `auto_migrate` is off in
    production (several tasks starting together would race). The web app goes
    on Vercel or ECS, with `/api/*` pointing at the ALB. `enable_bash=false`,
    `enable_mcp=false`. Health check route. On `SIGTERM`, running runs are saved
    as interrupted, so the user sees what happened instead of a silent stop.

### Milestone B — Several API tasks
After B the API service can run with desired count > 1 and autoscale.

32. [ ] **Event replay without duplicates** (ADR-014). Number each run event;
    a reconnect sends `Last-Event-ID` and gets only what it missed. Add a 15 s
    SSE heartbeat (opencode), so the ALB never closes a quiet stream.
33. [ ] **Runs across several tasks (ADR)** (ADR-014). A `runs` table in
    Postgres holds the "one run per session" claim (a unique constraint on the
    active run), its owner task and a heartbeat. Events go to a Redis Stream per
    session (ElastiCache), so any API task can serve any client's SSE stream,
    and stop requests reach the task that owns the run. A run whose owner's
    heartbeat goes stale is marked interrupted. Supersedes the in-memory parts
    of ADR-007.

### Milestone C — Tools back on, in sandboxes
After C, `bash`, `grep`/`find` and MCP work again for public users, isolated
per session, and workspace files survive deploys.

59. [x] **Workspace protocol, local only** (ADR-004, built; interface in ADR-034). Tools go through
    `Workspace` with `LocalWorkspace` as the only backend. No change users can
    see; the tests prove it.
28. [ ] **Sandbox per session (ADR)** (ADR-014). `SandboxWorkspace` on Daytona
    (answer the questions in `scripts/spike_daytona.py` first, E2B if it fails).
    Workspace files live in the sandbox, not on our tasks. `bash` is allowed
    only on sandboxed sessions. Network limits and a per-user sandbox cap
    (step 57).
60. [ ] **MCP inside the sandbox (ADR).** Stdio MCP servers start inside the
    session's sandbox instead of on our task, and remote (HTTP) MCP servers are
    allowed directly. Then `enable_mcp` can be on in production.

### Milestone D — Separate workers
After D, slow agent runs and fast API requests scale separately.

61. [ ] **Worker service (ADR).** The API only accepts requests and serves
    streams. It puts a run on a queue (Redis or a Postgres `SKIP LOCKED` table,
    chosen in the ADR). Worker tasks take runs, drive the agent loop, and publish
    events to Redis (step 33). Workers scale on queue depth, API tasks on
    requests. A second Dockerfile entrypoint, same code.

Later, unchanged: step 54 (durable runs) makes an interrupted run resume
mid-tool instead of being marked interrupted.

## Alternatives considered
- **Stay on one VPS or one Fargate task** — simplest and cheapest, but the owner
  needs more than one server for many users, and every deploy cuts off running
  agents.
- **Serverless (Vercel, Lambda)** — no long-lived process, so runs, SSE streams
  and child processes (`bash`, MCP) die when a request ends. Would need the
  whole loop rebuilt as a workflow first (step 54).
- **Scale out first, auth later** — more copies of an API anyone can use for
  free on our key. Auth and limits come first.
- **EFS for workspaces instead of sandboxes** — keeps files across deploys
  cheaply, but `bash` still runs on our tasks with access to our secrets and
  other users' files. Not safe for a public product.
- **Postgres `LISTEN/NOTIFY` instead of Redis for events** — one less service,
  but no replay from an event id and payloads are capped at 8 KB. Kept as a
  fallback in step 33's ADR.
- **Build our own login** — good practice for learning, but passwords, resets,
  email and OAuth are a lot of security code to own for a public product.

## Consequences
- Order to keep: 55 → 27 → 56 → 57 → 58 (Milestone A); 32 → 33 (B); 59 → 28 →
  60 (C); 61 after 33 and 28 (D).
- Steps 27, 28, 32 and 33 are ticked in ADR-014, where they are listed; the new
  steps 55–61 are ticked here.
- New AWS pieces to pay for and run: RDS, ALB, Fargate, then ElastiCache (B)
  and a sandbox vendor (C).
- Until Milestone C, public users have the file tools only (`read`, `write`,
  `edit`, `ls`), and their workspaces are lost on a deploy. The UI must say so.
- Every route and query gets a user check from step 56 on; tests need a
  signed-in user fixture.
- Local dev stays one command (`pnpm dev:api`), with Redis and Postgres from
  `docker compose` once steps 33 and 55 need them.
