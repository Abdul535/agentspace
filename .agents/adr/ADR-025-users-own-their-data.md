# ADR-025 — Users own their data

- **Status:** Accepted
- **Date:** 2026-11-21
- **Deciders:** project owner

## Context
Step 56 of ADR-022. Step 27 (ADR-024) checks a Supabase token on every route,
but nothing is stored per user yet: everyone who signs in sees and changes the
same sessions and MCP servers. MCP `env` values hold users' API keys and are
stored as plain text (ADR-017). We are still on SQLite. Postgres (step 55)
isn't built yet.

How the references do it:

- **CompanyOS** puts an `accountId` on its rows and adds `eq(accountId, …)` to
  every query. An unknown id and a row the caller may not see get the same
  answer: "an unknown id → 404" (`app.ts`). Stored secrets are encrypted with
  AES-256-GCM, each account with its own key derived from one server secret
  through HKDF (`secrets/account-resource.ts`), and saved as a versioned
  `v1:iv:tag:data` string.
- **pi** and **opencode** are local apps with one user and no login, so they
  have no owners.

## Decision
- **A `users` table** keyed by the sign-in provider's user id (Supabase's
  `sub`), with `email` and `created_at`. The row is created on the user's first
  request (the `get_user` dependency, `api/deps.py`) and its email is kept up to
  date. There is no separate sign-up step.
- **A `user_id` on `sessions` and `mcp_servers`**: not null, indexed, with a
  foreign key to `users` (`ON DELETE CASCADE`). Messages belong to their
  session, so they need no column of their own.
- **The owner filter lives in one place, not in each route.**
  `SessionStore(db, user_id)` only finds, lists and creates that user's
  sessions, and every session route loads its row through `get_session_row`.
  The MCP routes use one `owned_by(user)` filter in each query. A route can't
  forget a check it never writes.
- **Another user's row is a 404** with the same text as a missing id, never a
  403, so nobody can learn that a session id exists.
- **MCP server names are unique per user** (`UNIQUE (user_id, name)`), so two
  users can each have a `github` server.
- **Local mode belongs to a fixed `local` user.** With no login,
  `require_user` returns `None`, and `get_user` uses the `local` row. The
  migration creates that row and gives it every session and MCP server saved
  before this change, so local data stays where it was. A signed-in user starts
  with no data. Supabase ids are UUIDs, so `local` can't clash with one.
- **Skills stay global and read-only** (ADR-016): one folder that every user's
  sessions read. Per-user skills can come later.
- **MCP `env` values are encrypted at rest** (`agentspace/encryption.py`).
  Following CompanyOS: AES-256-GCM, with a key per user derived by HKDF from
  `AGENTSPACE_SECRET_KEY`, stored as `v1:<base64url of nonce + ciphertext>`.
  Variable names stay readable. A value copied into another user's row can't be
  decrypted there. The owner still gets the plain values back from the API, for
  the edit form. When the API can't decrypt a value (the key changed), it
  answers 500 with a message naming the server and the setting. The migration
  encrypts the existing values, and its downgrade decrypts them again. This
  replaces ADR-017's "stored as plain text".
- **The key:** `AGENTSPACE_SECRET_KEY` (at least 32 characters). Without it,
  the API generates one once into `<data_dir>/secret.key` (mode 600) and logs a
  warning. That keeps local dev at one command, but the key then sits next to
  the database it protects. Step 58 sets the key from Secrets Manager.
- **The MCP admin token stays** (ADR-017). It guards running commands on the
  host, which is a different question from who owns a row.

## Alternatives considered
- **Admin user with a master key owns old rows.** This was the owner's first
  idea. It needs a new secret to manage and its own way to sign in, and it
  doesn't answer what local mode is. A fixed `local` user covers both cases
  with nothing new to keep secret.
- **Filter in each route.** Explicit, but one missed `WHERE` leaks a session.
  The scoped store makes the safe path the only one.
- **403 for someone else's row.** Clearer for debugging, but it confirms the
  id exists.
- **Plain-text env (the owner's first pick).** Simpler, but a leaked database
  file or backup would expose every user's tokens. Encrypting costs one module
  and one setting.
- **Postgres row-level security.** Enforced in the database, but we're still on
  SQLite. Worth reconsidering after step 55.

## Consequences
- New dependency: `cryptography` (already pulled in by `pyjwt[crypto]`, now
  listed directly). New setting: `AGENTSPACE_SECRET_KEY`.
- Losing the secret key loses every saved MCP env value. It must be backed up
  with the database, but stored separately from it.
- Every new table that holds user data needs a `user_id` and must be read
  through a scoped store or the `owned_by` style filter. Tests sign in as two
  users (`tests/test_users.py`).
- A user's run reads only that user's MCP servers. Run slots and MCP processes
  stay keyed by session id, which is unique across users.
- Not built: deleting a user, an admin view of all users, per-user skills, and
  hiding env values from API responses (ADR-017 stage 8).
