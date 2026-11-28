# ADR-035 — A Daytona sandbox per session (`SandboxWorkspace`)

- **Status:** Accepted (built 2026-11-28)
- **Date:** 2026-11-27
- **Deciders:** project owner

## Context
ADR-022 step 28, the second step of Milestone C (ADR-033's order). Step 59
(ADR-034) put every tool behind the `Workspace` protocol, with `LocalWorkspace`
as the only backend. Files still live on the API task's disk, so a deploy or a
crash loses them, and `bash` stays off in production because it would run on
our server, next to our secrets.

The Daytona spike (ADR-004, "Spike results") passed. It also found three things
the design has to handle:

- `exec` hangs while a background child holds the output open.
- Live output needs a Daytona "session" command.
- Auto-stop counts API calls as activity, not running processes.

How the references handle the lifecycle:

- **CompanyOS** creates the sandbox when the session is created. It keeps a
  `session_sandboxes` row with a unique `session_id` (`onConflictDoNothing`), and
  the vendor box is found again by a fixed name plus an idempotency key. Its own
  reaper stops a box 15 minutes after a turn and archives it at 12 h; the
  vendor's auto-stop is only a 12 h backstop. Nothing is deleted automatically.
  While a box wakes, the UI says "Waking this session's computer. Messages you
  send are queued and go out automatically."
- **opencode's `WorkspaceDriver`** (`packages/core/src/workspace/driver.ts`)
  has `create`, `connect`, `suspendForIdle` and `destroy`. `create` must be
  get-or-create, idempotent per id, because the core retries after crashes:
  "adopt an existing match instead of creating a duplicate".

The owner's prediction (`.agents/learning-progress.md`): the sandbox is created
and found again by the session id, which matches both. The idle stop, races,
crash recovery and the waiting state are added here.

## Decision
**In one line:** with `AGENTSPACE_WORKSPACE_BACKEND=daytona`, each session's
files and commands live in a Daytona sandbox named after the session. It is
created on the first prompt, found again by that name, stopped by Daytona after
15 idle minutes, and woken on the next prompt behind a designed "starting
workspace" state.

### Lifecycle (`workspace/sandboxes.py`)
- **The sandbox's name is the key:** `agentspace-<env>-<session_id>`, with
  labels `agentspace.session` and `agentspace.env`. No database column. Daytona
  refuses a second sandbox with a taken name (spike Q6b), so the name itself is
  the unique constraint.
- **`ensure(session_id)` is get-or-create, and safe to repeat:**
  1. `get(name)`. If found and stopped or archived, `start` it.
  2. If not found, `create`. On `DaytonaConflictError` (another server or an
     earlier crashed attempt got there first), go back to step 1.

  That covers two prompts at once and a crash between create and use, the cases
  opencode's driver warns about.
- **It runs inside the run**, before the first tool call, and only when the run
  uses a workspace tool. A session that only chats never gets a sandbox.
- **Idle stop is Daytona's:** `auto_stop_interval=15` (minutes) and
  `auto_archive_interval` = 1 day. Because only API calls count as activity, a
  command still running after 5 minutes calls `refresh_activity` (and again
  every 5 minutes), so the sandbox isn't stopped under it. We run no reaper of
  our own: unlike CompanyOS, there's no daemon in the box to keep alive between
  turns.
- **No automatic delete.** Sessions are archived, not deleted, today. Deleting a
  sandbox comes with deleting a session, when that exists.

### `SandboxWorkspace` (`workspace/sandbox.py`), the same six methods
It sits on a small `Box` port (`kind`, `download`, `upload`, `entries`, `run`).
`DaytonaBox` (`workspace/daytona.py`) fills it for Daytona, and `HostBox`
(`tests/host_box.py`) fills it with this machine for tests. The vendor
behind `ensure` is the `Sandboxes` port (`find`, `create`, `start`, `home`).
The workspace folder is `<home>/workspace`, and skills go in
`<home>/.agentspace/skills`.

- **Paths** resolve lexically against the sandbox's workspace folder
  (`/home/daytona/workspace`). There is no symlink check: the sandbox is the
  security boundary, so a link pointing outside it reaches only that session's
  own box. The same `ToolError` wording as `LocalWorkspace`.
- **`stat`, `read_bytes`, `list_dir`** call `fs.get_file_info`, `download_file`
  and `list_files`, and map `DaytonaFileNotFoundError` to the same messages.
- **`update_file`** downloads the file, applies `change`, and uploads the result,
  under the same in-process per-file lock (ADR-034). That is enough while one
  server owns a session's run (ADR-029).
- **`exec`** runs as a Daytona session command (`run_async=True`) under
  `setsid`, so it has its own process group, and streams the log into the
  local `output` file. It starts with `exec 2>&1`: Daytona keeps stdout and
  stderr on separate pipes, which can reorder them.
  - **The exit code comes in the stream.** Daytona reports no exit code while
    a background child still holds the output open. That was found in
    building, beyond the spike. So the command's `EXIT` trap prints
    `<nonce>:<code>` on stdout, after everything the command wrote, then kills
    its own process group. `ExitMarker` takes that line out of the output.
    One command costs three calls: create session, execute, stream. The
    session is deleted in the background. Each call took 0.3–1 s from a
    laptop, so a command took about 2–3 s. This is to be measured again from
    AWS.
  - **Every command gets a timeout,** even when the model gives none: the
    model's own, or `AGENTSPACE_SANDBOX_EXEC_TIMEOUT_SECONDS` (default 10
    minutes). `bash` then says the workspace's limit was hit and suggests a
    longer `timeout`.
  - On timeout or cancel, before the trap has run, the process group is
    killed through a pid file the command wrote.
- **`ripgrep`** runs `rg` the same way and yields lines as they stream. Stopping
  early kills it.
- **`bash`'s full-output file** is also written to
  `.agentspace/bash/<call_id>.log` in the sandbox, and the notice gives that
  path, so the model can `read` it (closes ADR-034's gap).

### Skills
Each run copies the loaded skill folders into the sandbox at
`/home/daytona/.agentspace/skills/<name>/`, skipping the copy when a hash of
the folder matches what is already there. The system prompt lists those paths.
`read` and `ls` can reach them, and `bash` can run a skill's scripts (owner's
choice).

### Network (owner's choice: package sites only)
Daytona's network policy depends on the account tier
([network limits](https://www.daytona.io/docs/en/network-limits/)). Tiers 1–2
allow only "essential services": package registries, git hosts and model
providers. They refuse any per-sandbox override, and a create with a
`domain_allow_list` fails. Tiers 3–4 allow the whole internet and accept an
allow list. So the setting is a mode, `AGENTSPACE_SANDBOX_NETWORK`:

- **`vendor_default`** (the default): Daytona's tier policy. On our account
  (Tier 1–2), PyPI answers and `example.com` is blocked, which is the
  owner's choice already.
- **`allow_list`**: only `AGENTSPACE_SANDBOX_ALLOWED_DOMAINS` (PyPI, npm,
  GitHub). This is for Tier 3+, where it must also list every registry, since
  essential services aren't added for you. Applied on create, and re-applied
  when a sandbox is woken.
- **`blocked`**: no network.

Moving to Tier 3+ means switching production to `allow_list`, or sandboxes
get the whole internet.

### What the user sees
`ensure` emits a new `workspace_status` event: `starting` when it has to create
or start a sandbox, `ready` when done, `failed` with the reason. The web app
shows a status line above the composer for `starting`:

> "Starting this session's computer… Its tools wait for it, usually a few
> seconds. Files you made before are kept."

On `failed`, the tool call returns the reason to the model, which replies. The
web app keeps the reason ("This session's computer couldn't start: …") in the
error alert after the run. The next tool call tries again. A `get` that finds
the sandbox running emits nothing, so nothing flickers.

### Settings and limits
- `AGENTSPACE_WORKSPACE_BACKEND=local|daytona`. Startup fails if `daytona` has
  no `DAYTONA_API_KEY`. In production, `terraform apply -var
  sandboxes_enabled=true` makes the key's secret box and sets the backend,
  with sandbox prefix `agentspace-prod`. `set-secrets.sh` asks for the key.
- **`bash` is allowed on every sandboxed session.** On `local` it stays behind
  `AGENTSPACE_ENABLE_BASH`.
- **Per-user cap:** a sandbox runs only during a run and for 15 minutes after.
  The existing cap on runs (`max_runs_per_user`, step 57) therefore caps a
  user's running sandboxes at about that number. Stopped and archived ones cost
  only storage. No second cap.

## Alternatives considered
- **A database row per sandbox (CompanyOS).** It allows status queries without
  a vendor call, but it is a second source of truth that can drift from
  Daytona's. The fixed name already gives uniqueness and lookup.
- **Create the sandbox when the session is created (CompanyOS).** The first
  prompt is faster, but every session that only chats pays for a sandbox.
- **Our own reaper instead of Daytona's auto-stop (CompanyOS).** More control,
  but a loop and a deadline column we don't need yet. Daytona's timer plus
  `refresh_activity` covers it.
- **Plain `exec` for `bash`.** Simpler, but no live output, and a background
  child hangs it.
- **Read skills from the server** (the owner's other option). Less code, but
  `bash` couldn't run a skill's scripts.
- **E2B.** Not needed: Daytona passed questions 1 and 2.

## Consequences
- Workspace files survive deploys and crashes. ADR-030's one-task limit for
  files is gone, and `bash` can be on for public users.
- Each tool call is a network round trip (tens of ms), and a cold session waits
  a few seconds (3.3 s from stopped; from archived not yet measured). Parallel
  tool calls (ADR-003 step 9) matter more.
- Daytona becomes a dependency and a per-second cost. If Daytona is down,
  sandboxed sessions can't use tools, and the run says so.
- Tests: `tests/test_workspace.py` runs every protocol test on the local
  backend, on `SandboxWorkspace` over `HostBox`, and on a real Daytona sandbox
  when `DAYTONA_API_KEY` is set (all 17 pass there). `tests/test_sandboxes.py`
  covers the lifecycle with a fake vendor: create once, wake, adopt a taken
  name, two prompts at once, failure and retry, skill copies.
  `tests/test_daytona.py` covers the exit marker offline, plus one real
  create, find, refuse, stop and wake cycle with the network check.
- Not moved: MCP still starts on our server (step 60), and existing local
  workspace files are not migrated (they are lost on deploy anyway).
- Open: the real time to start from archive. Deleting a Daytona session is no
  longer relied on to kill anything: the trap or the pid-file kill does.
- Not done here: a reconnecting browser misses a `starting` sent before it
  reconnected, since `run_snapshot` doesn't carry it. The tool call still shows
  as running.
