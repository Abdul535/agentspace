# ADR-027 — Postgres everywhere, on Supabase

- **Status:** Accepted
- **Date:** 2026-11-22
- **Deciders:** project owner

## Context
Step 55 of ADR-022. The API keeps everything in one SQLite file in `data_dir`.
That can't be shared by two API tasks, and Fargate wipes it on every deploy. Since
ADR-024 we have a Supabase project for sign-in, and every Supabase project comes
with a Postgres.

The owner predicted "Postgres everywhere" (`learning-progress.md`, 2026-11-21).
The prediction's gaps became requirements here:
- two branches' migrations must never meet in one database (the owner later chose
  one shared dev database, so this is now a guard; see below);
- tests are isolated from each other;
- dev needs Docker;
- CI needs a Postgres service.

The owner first asked for Drizzle as the ORM.

How the references do it:

- **CompanyOS** uses Postgres everywhere: hosted Supabase in production, and
  local Supabase through the Supabase CLI for dev (`supabase/config.toml`,
  Postgres on 54322). CI runs a plain `postgres` service (`db-drift.yml`).
  - **Client** (`packages/db/src/client.ts`): a small pool (6 per process, sized
    for rolling deploys) and a 25 s `statement_timeout`. A 2026-06-08 incident
    is behind both: with no limits, one stuck query held a connection forever,
    requests queued behind it, and unrelated endpoints timed out. There are also
    connect and idle timeouts, and TLS to `*.supabase.co`. Prepared statements
    are off by default: on Supavisor's transaction pooler (port 6543) each
    transaction may land on a different server connection, so a statement
    prepared on one connection doesn't exist on the next.
  - **Migrations** (`MIGRATIONS.md`): drizzle-kit generates SQL from the schema,
    and node-pg-migrate applies it, before services roll.
  - **Worktrees** can get their own database inside the one shared local
    Supabase (`scripts/worktree/lib/branch-db.ts`). Their learning of
    2026-11-06 shows why: a worktree's tests ran four branch-only migrations on
    the shared database, and every other checkout broke.
- **pi** (JSONL files) and **opencode** (SQLite) are local apps, so they don't
  face this.

## Decision
- **ORM: keep SQLModel + Alembic**, with the `asyncpg` driver. Drizzle is
  TypeScript-only, and our API is Python. SQLModel is our Drizzle: one class is
  the table and the row type, and queries are typed. Alembic is our drizzle-kit:
  `alembic revision --autogenerate` diffs the models against the database and
  writes a migration file to review and commit, and `upgrade head` applies it.
  `aiosqlite` is removed. The owner chose to start fresh, so the old SQLite
  files are left on disk and nothing copies them.
- **Postgres in every environment.**
  - Production: hosted Supabase, the same project as sign-in.
  - Dev and tests: local Supabase through the Supabase CLI.
  - CI: a service built from the same image, `public.ecr.aws/supabase/postgres:17.6.1.167`.
    It runs fine without the CLI.

  SQLite isn't kept for dev. It would hide the very differences this step is
  about.
- **Local Supabase runs only its database and Studio.** `supabase/config.toml`
  turns off auth, the REST API, storage and the rest, because sign-in stays on
  the hosted project (ADR-024). Studio, the table viewer, stays on. Its ports
  start at 55320, so it can run next to another project's stack on the 543xx
  defaults. Four containers start: Postgres,
  Studio with its helper postgres-meta, and Kong, which can't be turned off.
- **One shared dev database, with an opt-out.** The owner wants the same dev
  data in every worktree, so every checkout uses `postgres` by default. It's
  also the one database Supabase Studio shows (http://localhost:55323).
  - **The risk:** a branch's new migration would upgrade the shared database,
    and every other checkout's API would then refuse to start ("Can't locate
    revision"). That's CompanyOS's 2026-11-06 collision.
  - **The guard:** `scripts/db.sh` refuses to start on the shared database
    when the branch has migration files that `dev` lacks. It names them and
    says to run `pnpm db:own`.
  - **The opt-out:** `pnpm db:own` writes `AGENTSPACE_DATABASE_URL` for
    `agentspace_<branch>` into `.worktree.env` and creates that database.
    Deleting the line shares again.
  - `scripts/db.sh` also checks that Docker is running (and says so plainly if
    not), starts Supabase when needed and waits for Postgres. `pnpm dev:api`
    and `scripts/worktree-setup.sh` run it, so dev stays one command.
- **Tests get a fresh database each** (`tests/pg.py`, `conftest.py`).
  - Each pytest run creates `agentspace_test_<pid>_<random>_template` and
    migrates it once. Every test then gets its own copy (`CREATE DATABASE …
    TEMPLATE`, about 22 ms), which is dropped afterwards. The copy's URL goes
    into `AGENTSPACE_DATABASE_URL`, so the app under test reads it like any
    setting.
  - A run that crashed leaves its databases behind. The next run drops the ones
    whose process id is no longer alive. Two worktrees can run tests at the same
    moment, and neither touches the other's databases or the dev ones.
  - The server comes from `AGENTSPACE_TEST_DATABASE_URL`, which defaults to the
    local Supabase. `pnpm check:api` starts it first.
- **Connections** (`session/db.py`), following CompanyOS:
  - Each process has a pool of `AGENTSPACE_DATABASE_POOL_SIZE` (5), plus as
    many more when busy.
  - A request waits at most 10 s for a free connection, then fails instead of
    queueing forever.
  - `pool_pre_ping` replaces connections a server or pooler dropped, and
    connections are recycled every 30 minutes.
  - Connecting times out after 10 s, and `statement_timeout` is
    `AGENTSPACE_DATABASE_STATEMENT_TIMEOUT_MS` (25 s).
  - TLS is required to `*.supabase.co` and `*.supabase.com` unless the URL sets
    `ssl`.
  - On port 6543 (the transaction pooler), statement caches are off and every
    prepared statement gets a unique name.

  Production should connect through the direct connection or the session
  pooler. The direct one is IPv6-only on Supabase without its IPv4 add-on, so
  Fargate will likely use the session pooler. That is chosen in step 58.
- **A connection isn't held while the model works.** A read starts a
  transaction, and that transaction keeps its connection checked out until it
  ends. SQLite never showed this, but on Postgres each open chat stream held
  one connection for the whole run, so a few streams could use up the pool. The
  SSE routes and `RunManager.drive` call `release(db)` (a commit) before they
  wait. Each saved message then takes a connection only for its own write.
  `test_a_streaming_run_holds_no_database_connection_while_the_model_works`
  checks this.
- **Schema, checked while porting:**
  - **JSON columns are JSONB.** The models say so, and migration
    `f2c7d9a4e810` converts the four columns.
  - **`UTCDateTime` is `timestamp with time zone`.** Values go in and come out
    as UTC.
  - **Strings are `varchar` with no length** (unlimited in Postgres, as in
    SQLite), so no existing value can be cut off.
  - **`provider_keys` keeps its composite key** `(user_id, provider)`.
  - **Every existing migration runs on a fresh Postgres, down to empty and up
    again.** Migration `c3f1a8d92e47` needed one fix: it dropped the
    unique-name constraint by the name SQLite was given, while Postgres had
    named it `mcp_servers_name_key`. It now looks the name up. That changes
    nothing for a database it already ran on.
  - **`test_the_migrations_build_exactly_what_the_models_describe`** fails when
    a model and the migrations disagree.
  - **SQLite's PRAGMAs are gone**, with SQLite itself.
- **Migrations still run when the app starts** (`auto_migrate`). Applying them in
  the deploy before tasks roll, as CompanyOS does, is step 58's job.

## Alternatives considered
- **Drizzle + drizzle-kit** — the owner's first pick. It needs a TypeScript
  service between FastAPI and the database, or a second schema to keep in sync
  with the Python models. SQLModel + Alembic is the same idea in our language.
- **SQLite for dev and tests, Postgres only in production** — nothing new to
  install. But JSONB, time zones, case-sensitive `LIKE`, connection limits and
  constraint names would only show up after deploying.
- **A plain `postgres` container (`docker compose`)** — lighter, but a slightly
  different Postgres from production's, and a second way to run things beside
  the Supabase CLI we already need for the project.
- **A full Supabase stack per worktree** — CompanyOS's isolated mode. About
  0.7 GB of RAM and eight containers each, to isolate what one `CREATE DATABASE`
  already isolates.
- **A database per worktree, always** — what this ADR first built. Branches
  never collide, but each worktree starts empty and Studio can't see its data.
  The owner chose shared data plus the guard and `pnpm db:own`.
- **One shared database with no guard** — simplest, but one branch's migration
  silently breaks every other checkout's API.
- **A rolled-back transaction per test** — fast, but the app opens its own
  connections and commits on every write. A test would have to take over the
  app's engine, and runs in the background would see another transaction.
- **Prepared statements on** — one round trip less per query. It's off on the
  transaction pooler only, and left to asyncpg's default elsewhere. That's
  enough until we measure.

## Consequences
- Dev needs Docker running and the Supabase CLI installed
  (`brew install supabase/tap/supabase`). The first `pnpm dev:api` pulls the
  image, which takes about a minute.
- Tests need the local server too. `pnpm check:api` (and the pre-commit hook)
  starts it; plain `uv run pytest` says how to start it when it can't connect.
- The local server listens on `0.0.0.0:55322`, with the password `postgres`, as
  the Supabase CLI sets it. Fine on a laptop, but nothing secret belongs in a
  local database.
- Old SQLite data isn't carried over.
- A branch that adds a migration needs `pnpm db:own` before `pnpm dev:api`;
  the guard says so. After it merges, the other worktrees can run it on the
  shared database once they're rebased.
- Studio only shows the shared database. A worktree's own database is read
  with psql (`docker exec -it supabase_db_agentspace psql -U postgres -d
  <name>`) or a client such as TablePlus.
- asyncpg connections belong to the event loop that opened them. A test that
  queries outside the app's loop opens its own engine
  (`test_runs_are_built_with_the_owners_key`).
- New settings: `AGENTSPACE_DATABASE_POOL_SIZE`,
  `AGENTSPACE_DATABASE_STATEMENT_TIMEOUT_MS`, and `AGENTSPACE_TEST_DATABASE_URL`
  for tests. `AGENTSPACE_DATABASE_URL` now defaults to the main checkout's local
  database, not a file.
- What's open now: Postgres row-level security (ADR-025), live sign-out against
  `auth.sessions` (ADR-024, which needs the API on the same project's database),
  and runs in a table (step 33).
