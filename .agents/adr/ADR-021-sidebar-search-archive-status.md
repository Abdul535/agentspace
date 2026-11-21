# ADR-021 — Sidebar: search, archive, and a running spinner by polling

- **Status:** Accepted
- **Date:** 2026-11-21
- **Deciders:** project owner

## Context
The session list in the sidebar grows without end, can't be searched, and
doesn't show which sessions are running. `GET /sessions` already returns
`running` (ADR-007), but the web app only refetched the list when a run it
watched ended. "Waiting for input" is also wanted, but nothing pauses a run for
the user yet; that comes with tool approval (ADR-014 step 26).

How the reference designs do it:

- **opencode:** archiving sets `time_archived` on the session row
  (`packages/core/src/session/sql.ts:71`); lists skip rows that have one.
  Status reaches clients as `session.status` / `session.idle` events on its one
  global SSE stream, pushed rather than polled.
- **pi:** no archive (sessions are local files) and no multi-session sidebar.

## Decision
1. **Archive is a timestamp.** `sessions.archived_at` (nullable, migration
   `b7e2a9c41d05`). `POST /sessions/{id}/archive` sets it; 204, or 409 while the
   session runs, so a run never goes on where nobody can see or stop it.
   `GET /sessions` leaves archived sessions out; `GET /sessions/{id}` still
   opens one by its link. No unarchive yet.
2. **Search is client-side**: the sidebar filters the list it already has by
   title. The list is small and already loaded.
3. **Status by polling.** `useSessions` refetches every 10 s, every 2 s while
   any session runs. A run this tab starts or watches also refreshes the list on
   its first event and when it ends, so the spinner is immediate for the
   common case.
4. **"Waiting for input" waits for step 26**, which will add that state to
   `SessionPublic` next to `running`.

## Alternatives considered
- **A global SSE status stream (opencode)**: instant and no wasted requests,
  but a new long-lived stream and a server-side broadcaster for one spinner.
  Worth it once more live state (approvals, step 26) needs pushing.
- **Delete instead of archive**: loses the transcript; archive can be undone
  later by clearing the column.
- **Server-side search**: needed only once the list is paginated, or to search
  message text.

## Consequences
- A run started from another tab or a script shows in the sidebar up to 10 s
  late.
- An idle app makes one small `GET /sessions` every 10 s.
- Archived sessions can't be listed or restored from the UI yet.
