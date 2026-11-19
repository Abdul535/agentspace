# ADR-010 — CI checks on GitHub Actions, with `ty` for Python types

- **Status:** Accepted
- **Date:** 2026-11-20
- **Deciders:** project owner

## Context
Nothing checked a commit after it left the laptop: lint, tests and the build ran only when someone remembered to.
Two mistakes nothing caught: Python type errors (no type checker was set up; `ty` found 59 problems, `pyright` 106) and
an API change pushed without running `pnpm gen:api`, which leaves the web app's types out of date.

The reference repos all run CI on GitHub Actions: pi (build, check, tests split across parallel jobs), opencode (lint,
`tsc`, unit tests on push to `dev` and on PRs), CompanyOS (path filters, `next build` with a cache, Dependabot, CodeQL),
OpenMausBot (lint, test shards, read-only permissions). All of them pin actions to a commit SHA.

## Decision
One workflow, `.github/workflows/ci.yml`, on every push to `dev`, every pull request and on demand, with two jobs that
run side by side:

- **api:** `uv sync --locked`, `ruff check`, `ruff format --check`, `ty check --error-on-warning`, `pytest`.
- **web:** `pnpm install --frozen-lockfile`, `eslint`, `tsc --noEmit`, `pnpm gen:api` followed by `git diff --exit-code`
  on the generated files, `next build`.

Habits taken from the references: a newer push cancels the older run (pi), `contents: read` permissions (OpenMausBot),
actions pinned to a SHA with the version in a comment (all four). Dependabot opens one weekly PR per ecosystem
(actions, npm, uv), like CompanyOS.

The same checks run before every commit: `pnpm check` (root `package.json`, split into `check:api`, `check:web` and
`check:api-types`) is called by a husky pre-commit hook, as in pi, which runs `npm run check` from `.husky/pre-commit`.
`pnpm install` installs the hook through the `prepare` script. CI calls `pnpm check:api-types` too, so that check has one
definition; the other steps are listed one by one in `ci.yml` so the GitHub log shows which one failed.

Python types are checked with `ty` (Astral, the makers of uv and ruff), a dev dependency so the version is locked.
`scripts/spike_daytona.py` is excluded: it needs the Daytona SDK, which isn't a project dependency.

## Alternatives considered
- **mypy** — the oldest and most used Python type checker. Slow, and needs plugins for Pydantic.
- **pyright** — mature, powers VS Code's Pylance. Stricter here (106 problems vs 59), and it runs on Node.
- **Path filters** (CompanyOS) — skip the web job when only the API changed. Not worth it at this size: both jobs
  together take a few minutes.
- **pre-push hook instead of pre-commit** (opencode) — fewer runs, but a broken commit can sit locally until the push.
  The whole check takes about 10 s here, so pre-commit is cheap enough.
- **Checking only the staged files** (`git stash --keep-index`, lint-staged) — exact, but stashing would touch other
  work in progress in the same tree. The hook checks the working tree, like pi's.
- **CodeQL and secret scanning** — useful for a large public repo with many contributors; left for later.

## Consequences
- Every commit on `dev` gets a ✅ or ❌ on GitHub, and a commit that would go ❌ is stopped locally first.
- Uncommitted work elsewhere in the tree can fail the hook for an unrelated commit.
- `ty` is pre-1.0, so an update can add new errors; Dependabot's uv PR shows them in CI before they land.
- Forgetting `pnpm gen:api` or committing an unformatted file now fails CI.
- Tests that read an event or message union narrow it first (`end_of`, `isinstance`) instead of reading attributes
  that only some members have.
