# ADR-011 — One worktree per feature, merged into `dev` by squash PR

- **Status:** Accepted
- **Date:** 2026-11-20
- **Deciders:** project owner

## Context
Several features were built at once in the same checkout, on `dev`. Their uncommitted edits mixed: one feature's
half-done change failed another's pre-commit hook (ADR-010), and it was hard to tell which change belonged to which
feature. A second checkout of the repo (a git worktree) per feature fixes that, but a new worktree on its own can't run:

- `node_modules` and `apps/api/.venv` are not in git, so a new worktree has no dependencies.
- `apps/api/.env` (the OpenRouter key) is gitignored, so it is not there either.
- Every checkout's dev servers want ports 8000 (API) and 3000 (web), so two features can't run side by side.

How the references do it:

- **CompanyOS** (closest): `pnpm worktree create --name <slug>` makes the worktree, installs, gives it a block of ports
  (base + slot × 100) and an optional copy of the database, and records it in a registry. One objective = one branch =
  one worktree; the main checkout is only for running and investigating. Each branch lands on `dev` as a PR that the
  author squash-merges once local checks pass.
- **opencode**: worktrees based on `origin/dev`, kept in a gitignored `.worktrees/`.
- **OpenMausBot**: gitignores `.worktrees/` and `.claude/worktrees/`, nothing more.
- **pi**: no worktree tooling; agents must not switch a checkout's branch unless asked.

T3 Code, the editor used here, can start each thread in a new worktree and runs project actions marked "run on worktree
create" inside it.

## Decision
**One feature = one branch (`feat/<slug>`, `fix/<slug>`, `chore/<slug>`) = one worktree = one T3 thread.** The main
checkout (`~/Desktop/oss/agentspace`) stays on `dev` for running the app and looking around; feature work happens in a
worktree.

**Setup** — `scripts/worktree-setup.sh` (also `pnpm setup:worktree`), run by T3 when it creates a worktree:

1. Copies `apps/api/.env` and `apps/web/.env.local` from the main checkout when missing.
2. `pnpm install --frozen-lockfile` (also installs the husky hook) and `uv sync --locked`.
3. Picks the first free slot n ≥ 1, giving API port `8000 + 10n` and web port `3000 + 10n`. A slot is taken if another
   worktree's `.worktree.env` claims it or something is listening on either port. It writes the result to
   `.worktree.env` (gitignored) as `API_PORT`, `PORT` (read by `next dev`) and `API_URL` (read by `next.config.ts`).
   The main checkout is slot 0 and has no `.worktree.env`.

`pnpm dev:api` and `pnpm dev:web` load `.worktree.env` when it exists, so the same commands work in every checkout.

Each worktree gets its own SQLite database: `data_dir` is relative (`apps/api/data`, gitignored), so it starts empty and
the app applies the migrations on start.

**T3 project settings** — new threads default to a worktree (`defaultThreadEnvMode: worktree`). Actions: *Setup* (the
script, runs on worktree create), *API* and *Web* (dev servers, in the background), *Check* (`pnpm check`).

**Landing a feature** — rebase on `dev`, push the branch, open a PR into `dev` with auto-merge on
(`gh pr merge --auto --squash --delete-branch`). CI (ADR-010) runs on the PR; when it passes, GitHub squash-merges it
into one commit on `dev` and deletes the branch. Then the worktree is removed (`git worktree remove <path>`).
Branch protection on `dev` requires both CI jobs to pass and a PR to merge; the owner can still override it.

**Conflicts to expect when rebasing** — two migrations written in parallel both point at the same `down_revision`:
git merges them cleanly, but Alembic then has two heads and `upgrade head` fails. Point the later migration's
`down_revision` at the earlier one. Regenerate generated files instead of merging them by hand: `pnpm gen:api`
(`openapi.json`, `schema.d.ts`), `uv lock`, `pnpm install`.

**The learning rules** (`AGENTS.md`) move from "per commit" to "per PR": the quiz runs before a feature's PR is opened,
and the debugging-offer counter counts merged PRs. `.agents/learning-progress.md` is written only in the main
checkout, never in a worktree, since every feature would append to the same table and conflict.

## Alternatives considered
- **Branches in one checkout** (`git switch`) — no setup, but only one feature at a time and uncommitted work has to be
  stashed on each switch.
- **CompanyOS's full worktree CLI** (registry, autoclean, per-worktree database copy, Supabase stacks) — built for many
  agents and services; a 60-line script covers a two-server app with SQLite.
- **Ports by stride 100** (CompanyOS) — room for many services per slot; we have two, so a stride of 10 keeps ports
  easy to read (8010/3010, 8020/3020).
- **A copy of the main database per worktree** — keeps your sessions, but a feature that changes the schema could break
  it; an empty database is safer and is migrated automatically.
- **Normal merge commits / rebase-merge** — keep every small commit on `dev`. Squash gives one commit per feature, easy
  to read and revert; the small steps stay on the PR.
- **Merging locally into `dev`** — no PR, but CI runs only after the change is already on `dev`.

## Consequences
- Several features can be edited, tested and run at the same time without touching each other.
- A new worktree is ready in about 4 s (measured: `pnpm install` 3.7 s, `uv sync` 0.2 s): both link files from a
  shared cache instead of downloading or copying them, so the extra disk space is small too.
- Worktree databases start empty: sessions made in one checkout don't show in another.
- T3's preview URL is fixed per action, so the Web action can't open the right port by itself; the setup script prints
  the URLs.
- Commit hashes on a branch don't reach `dev` (the squash makes a new commit), so a merged branch must not be reused.
- Direct pushes to `dev` stop being the normal path; even small fixes go through a PR.
