# ADR-004 — Tool execution behind a Workspace protocol, with a sandbox per session

- **Status:** Proposed
- **Date:** 2026-11-17
- **Deciders:** project owner

## Context
Our tools act on the host. `read`, `write`, `edit` and `ls` use `pathlib` on
`data/workspaces/<session_id>/`. `bash`, `grep` and `find` start processes on the
host (ADR-003 steps 1–4). `bash` is opt-in (`AGENTSPACE_ENABLE_BASH`), because a
command runs as the API's user and can read anything that user can, including
`apps/api/.env` with the provider key. Path checks guard the file tools but not
`bash`.

The owner will run the agent in a sandbox (Daytona or E2B) from now on. pi solves
this with swappable "operations" per tool (`BashOperations`, `EditOperations`,
`ReadOperations` in `packages/coding-agent/src/core/tools/`), documented as being
for delegating "to remote systems (for example SSH)". The agent loop never knows
where a tool runs.

## Decision
- **The API stays outside the sandbox; tools run inside one.** Secrets, the
  database and the agent loop stay on the server. Each session gets its own
  sandbox, and only its tools act there.
- **A `Workspace` protocol** (`agentspace/workspace/`) is the only way tools touch
  files or run commands. It has a small async interface:
  - `read_bytes(path)` and `write_bytes(path, data)`, where writing creates parent
    folders
  - `stat(path)`, returning exists, is_dir and size
  - `list_dir(path)`
  - `exec(command, *, cwd, timeout)`, returning the exit code, combined output,
    and whether it timed out

  All paths are relative to the workspace root.
- **Two implementations:**
  - **`LocalWorkspace`** is today's behaviour: the `data/workspaces/<id>/` folder,
    `resolve_in_workspace` path checks, and process-group killing for `exec`.
  - **`SandboxWorkspace`** calls the vendor SDK. It is chosen by a setting
    (`AGENTSPACE_WORKSPACE_BACKEND=local|daytona`) and looked up through a registry,
    the way `ai/providers` is.
- **Tools become thin.** Truncation, notices, `details` and argument handling stay
  in the tools. File access, process handling and running `rg` move behind
  `Workspace`. `ToolContext.workspace` changes from a `Path` to a `Workspace`.
- **Path checks stay on both backends.** In a sandbox they are a second layer, so a
  bad path fails with the same clear error everywhere.
- **The sandbox's lifetime follows the session.** The sandbox is created on a
  session's first prompt and found again by a label holding the session id. It is
  stopped when idle and resumed on the next prompt. Its files must survive a stop.
- **First vendor: Daytona, if the spike below confirms it.** The spike is a script
  outside the app (`apps/api/scripts/spike_daytona.py`) that answers:
  1. Do files survive stop → start? How long do stop and start take?
  2. Does `exec` return the exit code, merged stdout/stderr and the right working
     folder? Does a timeout kill the command and its background children?
  3. Can we read and write bytes, and list folders?
  4. Is `rg` in the default image, or does a custom image or snapshot need it?
  5. Can the sandbox's network be blocked or limited?
  6. Can we find a sandbox by label, and does it stop on its own when idle?

  The answers are written into this ADR before it is accepted. If Daytona fails
  question 1 or 2, try E2B against the same script.
- **Order:** this lands before ADR-003 step 7 (hooks). Phase 2 rebuilds the tool
  pipeline anyway, and the hooks should be written against `Workspace`.

## Alternatives considered
- **The whole API inside the sandbox.** Easy to deploy, but the provider key and
  the database would sit next to the agent, and every session would share one
  sandbox.
- **Call the vendor SDK straight from each tool.** Less code at first, but it ties
  every tool to one vendor and breaks local development and tests.
- **Local containers (Docker, bwrap, nsjail).** No vendor to depend on, but it
  makes us run isolation infrastructure ourselves, and bwrap and nsjail are
  Linux-only. A `LocalWorkspace` variant could add this later behind the same
  protocol.

## Consequences
- **Tests use `LocalWorkspace`** and stay fast and offline. Sandbox tests are
  marked and run only when a vendor key is set.
- **Sandbox access becomes something to wait on.** Each tool call is a network
  round trip, so latency matters. Parallel tool calls (ADR-003 step 9) matter more.
- **`bash` becomes safe enough to turn on by default** with the sandbox backend.
  `AGENTSPACE_ENABLE_BASH` keeps guarding the local backend.
- **The `bash` code moves behind `exec`.** Its process-group killing and output
  log go to `LocalWorkspace.exec`. On the sandbox side, timeouts and output limits
  are the vendor's, and the spike checks them.
- **The sandbox image must include `rg`,** or `grep` and `find` fail there.
- **Cost:** sandbox time is billed per second, so idle sandboxes must stop on
  their own.
