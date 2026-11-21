# agentspace

A coding agent you host yourself: a FastAPI backend that runs the agent loop, and a Next.js
chat UI that streams what the agent is doing as it happens. The agent gets a workspace folder
per session and tools to work in it: `read`, `write`, `edit`, `grep`, `find`, `ls`, and `bash` when
enabled.

No agent framework. The loop, tool calling, provider adapters and session storage are all in
this repo ([pi](https://github.com/earendil-works/pi) is the reference design, not a dependency).

## Stack

| | |
|---|---|
| `apps/api/` | Python 3.13, FastAPI, SQLModel + SQLite (async), Alembic, uv |
| `apps/web/` | Next.js 16 (App Router), TypeScript, Tailwind CSS 4, shadcn/ui, TanStack Query, pnpm |
| Models | Any OpenAI-compatible API; OpenRouter out of the box |

## Run it

Needs [ripgrep](https://github.com/BurntSushi/ripgrep) on `PATH` for the `grep` and `find` tools
(`brew install ripgrep`). Two processes. The backend first:

```bash
cd apps/api
echo 'OPENROUTER_API_KEY=sk-or-...' > .env
uv run fastapi dev          # http://127.0.0.1:8000, docs at /docs
```

Or `pnpm dev:api` from the repo root, once `.env` exists. Then the web app, from the repo root:

```bash
pnpm install
pnpm dev:web                # http://localhost:3000
```

The browser only ever talks to the Next.js origin: `/api/*` is rewritten to the API in
`next.config.ts` (set `API_URL` if the backend isn't on `127.0.0.1:8000`), so there is no CORS
to configure. Migrations run when the API starts. The SQLite database and the per-session
workspaces live in `data/` (gitignored).

## How it works

```
prompt ──> agent loop ──> provider (OpenRouter) ──> model
              │  ▲                                    │
              │  └──────── tool results ──────────────┘
              └──> read / write in data/workspaces/<session_id>/
```

- **`ai/`** — provider-neutral message types and a `Provider` protocol whose one method streams
  a reply. Providers are registered by name; each session stores its provider and model.
- **`agent/`** — the loop: stream a reply, run its tool calls one at a time, feed the results
  back, repeat until a reply has no tool calls or `max_turns` is hit. It emits events
  (`turn_start`, `message_delta`, `tool_start`, `message_end`, …) and owns no storage.
- **`tools/`** — `read` and `write`, refused for any path outside the session's workspace.
- **`session/`** — sessions and an append-only message transcript in SQLite.
- **`api/`** — `POST /sessions`, `GET /sessions`, `GET /sessions/{id}`, and
  `POST /sessions/{id}/messages`, which streams the run's events as SSE. One run per session
  at a time.

The web app reads that SSE stream over `fetch` (`EventSource` can't POST) and renders the
streaming text and tool calls live; closing the tab aborts the request, which cancels the run.
Everything already finished is saved.

Full reasoning and trade-offs are in the ADRs: [`ADR-001`](.agents/adr/ADR-001-minimal-agent-core.md)
(agent core), [`ADR-002`](.agents/adr/ADR-002-web-api-integration.md) (web ↔ API).

## Configuration

Set in the environment or `apps/api/.env`: `OPENROUTER_API_KEY`, `AGENTSPACE_DEFAULT_MODEL`
(default `anthropic/claude-sonnet-5.5`), `AGENTSPACE_DEFAULT_PROVIDER`, `AGENTSPACE_DATA_DIR`,
`AGENTSPACE_DATABASE_URL`, `AGENTSPACE_MAX_TURNS`, `AGENTSPACE_MAX_OUTPUT_TOKENS`, `AGENTSPACE_ENABLE_BASH`
(default `false`: the `bash` tool runs commands unsandboxed as your user), `LANGFUSE_PUBLIC_KEY` and `LANGFUSE_SECRET_KEY` (both set turns on a Langfuse trace per run, ADR-018;
`LANGFUSE_BASE_URL` defaults to the EU cloud, `https://us.cloud.langfuse.com` for the US one). See
[`config.py`](apps/api/src/agentspace/config.py).

## Development

```bash
pnpm check                      # from the root: everything CI runs (lint, format, types, tests, build)
uv run pytest                   # from apps/api
uv run ruff check . && uv run ruff format .
uv run ty check                 # Python type check
pnpm lint:web                   # from the root
pnpm gen:api                    # regenerate the web app's API types after an API change
```

`pnpm install` sets up a git pre-commit hook (husky) that runs `pnpm check` before every
commit; CI ([`.github/workflows/ci.yml`](.github/workflows/ci.yml)) runs the same checks on
every push to `dev` and every pull request.

`pnpm gen:api` exports the OpenAPI spec without starting the server and runs
`openapi-typescript` into `apps/web/src/lib/api/schema.d.ts`; both generated files are
committed, so run it and commit the result whenever a route or model changes.

Adding a model provider is one adapter implementing `Provider`
([`ai/provider.py`](apps/api/src/agentspace/ai/provider.py)) plus a factory in
[`ai/providers/__init__.py`](apps/api/src/agentspace/ai/providers/__init__.py). Any
OpenAI-compatible API just needs the factory.

## Not built yet

Auth, background runs, parallel tool calls, context compaction, branching, Markdown rendering,
sandboxed execution. Contributor and agent conventions are in [`AGENTS.md`](AGENTS.md).

## Landing page

`apps/landing` is the public page (ADR-023): a static Next.js export with SEO tags, JSON-LD,
`robots.txt`, `sitemap.xml`, `/llms.txt` and a share image.

```bash
pnpm dev:landing                                   # http://localhost:3001 (next to the web app's port)
SITE_URL=https://example.com APP_URL=https://app.example.com pnpm --filter landing build   # writes apps/landing/out/
```
