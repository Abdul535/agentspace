# ADR-001 — Minimal agent core: own loop, pluggable providers, sessions in SQLite

- **Status:** Accepted
- **Date:** 2026-11-16
- **Deciders:** project owner

## Context
First working agent: a model that can read and write files in a workspace, with a
conversation that persists across requests, served by FastAPI. The project rule
is no agent framework; pi (`../pi-upstream`) is the reference design. The user
chose OpenRouter as the first provider and wants providers swappable later,
FastAPI routers (no CLI), and SQLite through SQLModel (Drizzle was considered and
dropped because it is TypeScript-only).

## Decision
Code lives in `apps/api/src/agentspace/`, in layers that only import downward:

- **`ai/`: provider-neutral messages and a `Provider` protocol.** `messages.py`
  defines `UserMessage`, `AssistantMessage` (text + tool-call blocks, `stop_reason`,
  usage) and `ToolResultMessage`, after pi's `packages/ai/src/types.ts`. A provider
  implements one call, `stream(model, Context) -> AsyncIterator[AssistantEvent]`,
  and never raises: failures end the stream with an `ErrorEvent`. Providers are
  registered by name in `ai/providers/__init__.py`; each session stores its
  provider name and model. `prepare_transcript` makes a stored transcript valid
  to send (drops errored/aborted assistant messages, adds a stand-in result for a
  tool call that never got one), as pi's `transform-messages.ts` does.
- **First provider: OpenAI-compatible Chat Completions over plain httpx**
  (`openai_compat.py`), configured for OpenRouter. The same adapter serves any
  compatible API by registering another factory.
- **`agent/`: the loop**, a simplified port of pi's `runLoop`: stream a reply,
  run its tool calls one at a time, add the results, repeat until a reply has no
  tool calls or `max_turns` is reached. Unknown tools, invalid arguments and tool
  exceptions become error results the model sees. Tool calls in a reply cut off by
  the token limit are not run. The loop emits pi's event sequence and holds no storage.
- **`tools/`: `read` and `write`** with pi's limits (2000 lines / 50 KB, offset
  paging). Paths are resolved, symlinks included, and refused outside the
  session's workspace folder (`data/workspaces/<session_id>/`).
- **`session/`: SQLite via async SQLModel**, Alembic migrations applied at startup.
  `sessions` holds provider, model and system prompt; `messages` is an append-only
  transcript (`seq` order, `parent_id` chain as in pi, the message as JSON).
  The API saves each message on its `message_end` event.
- **`api/`: FastAPI routers.** `POST /sessions`, `GET /sessions`,
  `GET /sessions/{id}`, and `POST /sessions/{id}/messages`, which streams agent
  events as SSE (`event` = type, `data` = JSON). One run per session at a time
  (409 otherwise), tracked in process memory.

## Alternatives considered
- **Vendor SDKs or an agent framework** — less code, but the project rule forbids
  it and it hides the loop we want to own.
- **Anthropic Messages API first** — best Claude support but one vendor; OpenRouter
  via the OpenAI-compatible API reaches Claude and most other models with one adapter.
- **JSONL session files (pi's format)** — simplest, but the app needs listing and
  querying sessions, which a database does better; the row shape keeps pi's linkage.
- **Drizzle ORM** — TypeScript-only; would split schema ownership across languages.

## Consequences
- A new provider is one adapter plus one registry entry; nothing else changes.
- A run is tied to its HTTP request: closing the connection cancels it. Finished
  messages are kept; a half-streamed reply is lost; an unanswered tool call gets
  a stand-in result on the next request. Background runs come later.
- The run lock is per process, so the API must run as one worker until the lock
  moves to the database.
- Not built yet: parallel tool calls, steering/follow-up queues, compaction,
  branching, thinking blocks, images, auth, sandboxed computers.
