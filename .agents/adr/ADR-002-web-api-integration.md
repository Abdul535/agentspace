# ADR-002 — Web ↔ API integration: same-origin proxy, generated types, SSE over fetch

- **Status:** Accepted
- **Date:** 2026-11-16
- **Deciders:** project owner

## Context
The Next.js app (`apps/web`) needs to list and create sessions and run prompts
against the FastAPI backend (ADR-001), showing the agent's output as it streams.
Constraints: TanStack Query owns server state, shadcn/ui owns components, and the
backend's `POST /sessions/{id}/messages` answers with server-sent events.

## Decision
- **Same-origin proxy.** `next.config.ts` rewrites `/api/:path*` to the API
  (`API_URL`, default `http://127.0.0.1:8000`). The browser only talks to the Next
  origin, so the API needs no CORS. Verified that SSE streams through the rewrite
  without buffering.
- **Types generated from OpenAPI.** The SSE route yields `AgentEvent` models (not
  hand-built `ServerSentEvent`s), so FastAPI puts every event schema in the spec.
  `pnpm gen:api` exports the spec without starting the server
  (`apps/api/scripts/export_openapi.py` → `apps/web/openapi.json`) and runs
  `openapi-typescript` into `src/lib/api/schema.d.ts`. `openapi-fetch` gives a typed
  client for the JSON routes (`src/lib/api/client.ts`). Generated files are committed.
- **Streaming via fetch.** `EventSource` cannot POST, so `src/lib/api/stream.ts`
  reads the fetch body and splits SSE `data:` lines into typed events. Aborting the
  fetch closes the connection, which cancels the run on the server (ADR-001).
- **Live run state outside the query cache.** `useAgentRun` keeps the in-flight
  messages, the streaming text draft and the executing tool calls. When the run
  ends it refetches the session (the server saved every finished message), then
  drops its live copy.
- **Routes.** `/` is an empty state with "New session"; `/sessions/[id]` is the chat.
  The sidebar lists sessions. With Cache Components on, both read URL params
  inside `<Suspense>`.

## Alternatives considered
- **CORS + `NEXT_PUBLIC_API_URL`** — works, but adds API config and exposes the API
  origin to the browser; the rewrite keeps one origin.
- **Hand-written TS types for events** — no codegen, but drifts from the Pydantic models.
- **WebSocket** — bidirectional, but the run is one request with one response
  stream; SSE over fetch is simpler and already what the API speaks.

## Consequences
- After any API schema change, run `pnpm gen:api` and commit the result; type errors
  in the web app then point at what changed.
- The run lives as long as the browser tab's request: closing or reloading the tab
  stops it (finished messages are kept). A stopped run's partial reply is not shown.
- Assistant text renders as plain text; Markdown rendering is not built yet.
