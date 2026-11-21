# ADR-024 — Login and API auth with Supabase (Google sign-in)

- **Status:** Accepted
- **Date:** 2026-11-21
- **Deciders:** project owner

## Context
Step 27 of ADR-022 (and ADR-014): the API has no auth, so anyone who reaches it
runs the agent on our OpenRouter key. The roadmap left the provider open
(Clerk, Cognito or Supabase Auth). The owner chose **Supabase Auth** with
**Google sign-in only** for now. Supabase also gives each project a Postgres,
which step 55 can use instead of RDS (decided there, not here).

How the references do it:

- **CompanyOS** uses Supabase. `apps/api/src/shared/jwt-verify.ts` checks the
  token's signature and expiry locally on every request (ES256 against the
  project's public keys), and `jwt-liveness.ts` also looks up the session row in
  Supabase's `auth.sessions`, so a sign-out takes effect at once.
- **pi** and **opencode** are local, single-user apps with no login.

## Decision
- **The browser signs in, the API only checks.** The web app uses
  `@supabase/ssr`: a `/sign-in` page with "Continue with Google", and an
  `/auth/callback` route that swaps the OAuth code for a session (PKCE). The
  session lives in cookies on the web app's origin. `src/proxy.ts` (Next 16's
  middleware) refreshes it and sends signed-out visitors to `/sign-in`.
- **Bearer token on every API call.** The typed client (`openapi-fetch`
  middleware) and the SSE `fetch` calls add `Authorization: Bearer <access
  token>`, read from the Supabase browser client, which refreshes it before it
  expires. Not cookies: the API never has to parse Supabase's cookie format,
  and a page on another site can't make the browser send the token, so there is
  no CSRF.
- **The API checks the JWT locally.** A FastAPI dependency (`require_user`,
  `agentspace/auth.py`) is attached to every router in `main.py`; only
  `GET /health` is left open. It verifies the signature with the project's
  public keys (JWKS at `<supabase_url>/auth/v1/.well-known/jwks.json`, fetched
  once and cached by PyJWT's `PyJWKClient`), plus `exp`, `iss` and
  `aud = "authenticated"`. No network call per request. It answers 401 for a
  missing, bad or expired token (the detail says which), 503 if the keys can't
  be fetched.
- **Sign-out:** tokens are short-lived (Supabase's default, 1 hour) and the
  browser stops refreshing after sign-out, so an old token dies within the
  hour. CompanyOS's live-session check waits until step 55 puts us on
  Supabase's Postgres.
- **One switch for local mode.** Auth is on when `AGENTSPACE_SUPABASE_URL` is
  set on the API and `NEXT_PUBLIC_SUPABASE_URL` on the web app. Without them the
  app runs as it does today (one local user, no login), and the API logs a
  warning at startup. Step 58 (first deploy) must refuse to start in production
  without it.
- **UI states:** sign-in (with a spinner while Google loads), a sign-in error
  from the callback, "Your session expired, sign in again" when the API answers
  401 or the session ends in another tab, and the signed-in user's email with a
  sign-out button in the sidebar.
- **Deferred:** the Host/Origin allowlist and CORS move to step 58, where the
  real hostnames are known. The web app reaches the API on its own origin
  (`/api` rewrite), so no CORS is needed now, and with bearer tokens a
  DNS-rebinding page can't get a token. Users, `user_id` on rows and per-user
  queries are step 56.

## Alternatives considered
- **Clerk** — polished UI kit, but a second vendor next to the Postgres we may
  take from Supabase, and paid sooner.
- **Cognito** — stays in AWS, but its hosted UI and SDKs are clumsy.
- **Ask Supabase on every request (`auth.getUser`)** — catches sign-out at
  once, but adds a network hop to every API call. CompanyOS moved away from it.
- **Send the Supabase cookie to the API** — no header code in the browser, but
  the API would parse Supabase's chunked cookie format, and cookies need CSRF
  protection.
- **Middleware instead of `Depends`** — works, but routes then dig the user out
  of the raw request; a dependency hands each route a typed `user` (step 56) and
  leaving `/health` open is one line.

## Consequences
- New API dep: `pyjwt[crypto]`. New web deps: `@supabase/supabase-js`,
  `@supabase/ssr`.
- New settings: `AGENTSPACE_SUPABASE_URL` (API), `NEXT_PUBLIC_SUPABASE_URL` and
  `NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY` (web, both public values).
- The app's pages move into an `(app)` route group, so `/sign-in` renders
  without the sidebar.
- Tests sign tokens with their own key pair; the verifier takes its keys from a
  stub instead of the network.
- Step 56 builds on `require_user`: it returns the user every query will filter
  by.
