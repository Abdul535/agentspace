# agentspace API

FastAPI backend: the agent loop, model providers, tools and sessions. Design: `.agents/adr/ADR-001-minimal-agent-core.md`.

## Run

Needs [ripgrep](https://github.com/BurntSushi/ripgrep) on `PATH` for the `grep` and `find` tools
(`brew install ripgrep`).

```bash
cd apps/api
echo 'OPENROUTER_API_KEY=sk-or-...' > .env
uv run fastapi dev          # http://127.0.0.1:8000, docs at /docs
```

Needs Postgres (ADR-027): run `pnpm dev:api` from the repo root instead, and it starts local Supabase
in Docker and checks the shared dev database first (`scripts/db.sh`). Migrations run at startup.
Per-session workspaces go to `data/`. Tests (`uv run pytest`) need the same server: each test gets a
fresh copy of a database migrated once per run.

## Try it

```bash
# create a session (provider, model and system_prompt are optional)
curl -s -X POST localhost:8000/sessions -H 'content-type: application/json' -d '{}'

# send a prompt; the agent's events stream back as SSE
curl -N -X POST localhost:8000/sessions/<id>/messages \
  -H 'content-type: application/json' -d '{"content": "Write hello.txt saying hi, then read it back"}'

# the stored transcript
curl -s localhost:8000/sessions/<id>
```

## Settings

Environment variables (or `.env`): `OPENROUTER_API_KEY`, `AGENTSPACE_DEFAULT_MODEL`
(default `anthropic/claude-sonnet-5.5`), `AGENTSPACE_DEFAULT_PROVIDER`, `AGENTSPACE_DATA_DIR`,
`AGENTSPACE_DATABASE_URL` (a `postgresql+asyncpg://` URL; `AGENTSPACE_DATABASE_POOL_SIZE` and `AGENTSPACE_DATABASE_STATEMENT_TIMEOUT_MS` tune its connections), `AGENTSPACE_MAX_TURNS`, `AGENTSPACE_MAX_OUTPUT_TOKENS` (default 8192; OpenRouter
refuses a request whose cap is above what the key's remaining credit covers), `AGENTSPACE_ENABLE_BASH`
(default `false`; gives sessions the `bash` tool, which runs commands unsandboxed as your user), `LANGFUSE_PUBLIC_KEY` and `LANGFUSE_SECRET_KEY` (both set turns on a Langfuse trace per run, ADR-018;
`LANGFUSE_BASE_URL` defaults to the EU cloud, `https://us.cloud.langfuse.com` for the US one),
`AGENTSPACE_SUPABASE_URL` (set: every route but `/health` needs a Supabase sign-in token, ADR-024),
`AGENTSPACE_SECRET_KEY` (encrypts MCP env values, ADR-025; at least 32 characters, e.g. `openssl rand -base64 32`;
unset, one is generated into `<data_dir>/secret.key`). See
`src/agentspace/config.py`.

## Develop

```bash
uv run pytest
uv run ruff check . && uv run ruff format .
uv run alembic revision --autogenerate -m "..."   # after changing session/models.py
```

## Add a provider

Write an adapter implementing `Provider` (`src/agentspace/ai/provider.py`) and register a factory in
`src/agentspace/ai/providers/__init__.py`. Any OpenAI-compatible API only needs a new factory around
`OpenAICompatProvider`.
