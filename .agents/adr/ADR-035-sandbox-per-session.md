# ADR-035 — A Daytona sandbox per session (`SandboxWorkspace`)

- **Status:** Proposed
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
  long `exec` calls `refresh_activity` every few minutes so the sandbox isn't
  stopped under it. We run no reaper of our own: unlike CompanyOS, there's no
  daemon in the box to keep alive between turns.
- **No automatic delete.** Sessions are archived, not deleted, today. Deleting a
  sandbox comes with deleting a session, when that exists.

### `SandboxWorkspace` (`workspace/sandbox.py`), the same six methods
- **Paths** resolve lexically against the sandbox's workspace folder
  (`/home/daytona/workspace`). There is no symlink check: the sandbox is the
  security boundary, so a link pointing outside it reaches only that session's
  own box. The same `ToolError` wording as `LocalWorkspace`.
- **`stat`, `read_bytes`, `list_dir`** call `fs.get_file_info`, `download_file`
  and `list_files`, and map `DaytonaFileNotFoundError` to the same messages.
- **`update_file`** downloads the file, applies `change`, and uploads the result,
  under the same in-process per-file lock (ADR-034). That is enough while one
  server owns a session's run (ADR-029).
- **`exec`** runs as a session command (`sh -c …` in its own process group,
  `run_async=True`), streams the log into the local `output` file, and polls for
  the exit code.
  - **Every command gets a timeout,** even when the model gives none: the
    model's own, or `AGENTSPACE_SANDBOX_EXEC_TIMEOUT` (default 10 minutes). A
    child holding the output open would otherwise hang the run (spike Q2).
  - On timeout or cancel, we kill the process group and delete the command's
    Daytona session.
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
Sandboxes are created with a `domain_allow_list`: PyPI, npm, GitHub and their
CDNs, held in one setting (`AGENTSPACE_SANDBOX_ALLOWED_DOMAINS`). Everything
else is blocked. The list is applied on create, and `update_network_settings`
brings older sandboxes up to date on `ensure`.

### What the user sees
`ensure` emits a new `workspace_status` event: `starting` when it has to create
or start a sandbox, `ready` when done, `failed` with the reason. The web app
shows a designed state above the composer for `starting`, worded like:

> "Starting this session's computer… Your message is queued and goes out on its
> own."

`failed` becomes an error with a retry. A quick `get` of a running sandbox
emits nothing, so nothing flickers.

### Settings and limits
- `AGENTSPACE_WORKSPACE_BACKEND=local|daytona`, looked up through a small
  registry the way `ai/providers` is. `DAYTONA_API_KEY` is a secret (Secrets
  Manager, through Terraform).
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
- Tests: `tests/test_workspace.py` runs against both backends. The Daytona runs
  are marked and run only when `DAYTONA_API_KEY` is set. The lifecycle
  (`ensure`, conflict retry, start when stopped) is tested with a fake Daytona
  client.
- Not moved: MCP still starts on our server (step 60), and existing local
  workspace files are not migrated (they are lost on deploy anyway).
- Open: the real time to start from archive, and whether deleting a Daytona
  session kills the commands started in it. The implementation checks both.
