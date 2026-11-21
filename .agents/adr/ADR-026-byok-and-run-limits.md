# ADR-026 — Users bring their own key; a cap on runs at once

- **Status:** Accepted
- **Date:** 2026-11-21
- **Deciders:** project owner

## Context
Step 57 of ADR-022. Since ADR-024/025 anyone can sign in and owns their own
sessions, but every run is paid for with one server key (`OPENROUTER_API_KEY`).
One user could spend any amount on it, and one user could start any number of
runs at once and slow the API down for everyone else.

The owner chose **bring your own key (BYOK)** for every user, the local user
too: the server's key is no longer used for runs.

How the references do it:

- **CompanyOS** lets an account connect its own model key (Settings → Model).
  A customer key is billed `billingMode: 'none'`: it never touches the Kortix
  wallet. Keys are encrypted per account (ADR-025 copied that). A key is
  checked with one cheap call when it is saved; a network failure records
  `unknown` and never blocks the save. Every provider refusal (no key, a key
  the provider refused, no funds) gets its own code and a fix the person can
  click, never a generic error (learning 2026-11-11: 41 accounts hit a 401 on
  a key that was saved without a check).
- **pi** and **opencode** keep the user's own keys on their machine
  (`AuthStorage`, `auth.json`); they are personal apps with no cap on runs.

## Decision
- **A `provider_keys` table**: one row per user and provider
  (`PRIMARY KEY (user_id, provider)`), holding the key encrypted with the
  user's `SecretBox` key (ADR-025), its last four characters to show, and when
  it was saved. Only `openrouter` exists today.
- **Routes** `GET /provider-keys`, `PUT /provider-keys/{provider}` and
  `DELETE /provider-keys/{provider}`. The key itself never comes back: the
  list shows `sk-or-…a1b2`.
- **Checked when saved.** `PUT` calls OpenRouter's `GET /key` once. A key
  OpenRouter refuses (401/403) is not saved and the route answers 422 with the
  reason. If OpenRouter can't be reached, the key is saved anyway (it may be
  fine) and the response says it wasn't checked.
- **Runs use the session owner's key.** `provider_lookup` loads the caller's
  keys, so the provider is built with them. A run with no key never starts: a
  409 with code `no_api_key` and "Add your OpenRouter key in Settings". The
  server's `OPENROUTER_API_KEY` is no longer read by runs.
- **Local mode is seeded.** At startup in local mode (no Supabase URL), if
  `OPENROUTER_API_KEY` is set and the local user has no key yet, it is copied
  into the local user's row, encrypted. After that the stored key is the one
  that counts; changing `.env` later does nothing, since the local user can
  change it in Settings like anyone else.
- **A refusal mid-run says what to do.** OpenRouter's 401 becomes "OpenRouter
  refused your API key. Replace it in Settings." and 402 becomes "Your
  OpenRouter account is out of credits. Add credits at openrouter.ai.", instead
  of the raw `HTTP 401` text.
- **At most `max_runs_per_user` runs at once** (default 3). `RunManager.claim`
  counts the user's runs under its claim lock; one more is a 429 with code
  `too_many_runs`. A run frees its slot when it ends, so there is nothing to
  reset.
- **No daily spend cap and no request rate limit.** With BYOK the money is the
  user's, and OpenRouter lets them cap their own key. Request rate limits wait
  for the deploy (step 58), where the load balancer can do them.
- **The web app** gets a Settings → API keys page (add, replace, remove, with
  the check's result) and a link to it from the sidebar. The composer shows
  "Add your OpenRouter key to start" with a link when the user has none, and
  the run errors above link to the page too.

## Alternatives considered
- **Our key with a per-user daily cap** — users need no OpenRouter account,
  but we pay for everything and need billing to recover it. Not for now.
- **Both (our key as a fallback)** — CompanyOS does this, and its learnings
  show the cost: BYOK spend showing up as our charge, fallbacks picked by
  mistake. One payer is simpler.
- **Keep the server key for local mode** — no seed step, but local mode would
  run code the public app never runs. One path for everyone.
- **Save an invalid key with an `invalid` status** — CompanyOS records the
  verdict. Refusing it at once is simpler and tells the user right away.

## Consequences
- A new user can't chat until they add a key; the UI has to make that the
  obvious next step.
- `AGENTSPACE_SECRET_KEY` now protects model keys too: losing it loses every
  saved key (users add them again).
- The run cap lives in this process's memory, like the runs themselves
  (ADR-007); step 33 moves it to the `runs` table with them.
- A provider added later registers its own key check and gets a row per user.
