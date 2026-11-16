# agentspace API

FastAPI backend: the agent loop, model providers, tools and sessions. Design: `.agents/adr/ADR-001-minimal-agent-core.md`.

## Run

```bash
cd apps/api
echo 'OPENROUTER_API_KEY=sk-or-...' > .env
uv run fastapi dev          # http://127.0.0.1:8000, docs at /docs
```

Migrations run at startup. Data (SQLite database and per-session workspaces) goes to `data/`.

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
`AGENTSPACE_DATABASE_URL`, `AGENTSPACE_MAX_TURNS`, `AGENTSPACE_MAX_OUTPUT_TOKENS` (default 8192; OpenRouter
refuses a request whose cap is above what the key's remaining credit covers). See `src/agentspace/config.py`.

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
