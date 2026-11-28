# ADR-004 — Tool execution behind a Workspace protocol, with a sandbox per session

- **Status:** Accepted (spike answered 2026-11-27, Daytona passes); its `Workspace` interface sketch is
  replaced by ADR-034 (built), and the sandbox design is detailed in ADR-035
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

## Spike results (2026-11-27)
`apps/api/scripts/spike_daytona.py`, Daytona SDK 0.232.0, default snapshot. Daytona
passes questions 1 and 2, so E2B was not tried.

1. **Files survive stop → start: yes**, and archive → start too. Stop took 14.4 s,
   start 3.3 s. Archive returned in 0.7 s and start from it in 1.1 s, which is
   suspiciously fast: archiving likely runs on after the call returns, so a real
   start from a fully archived sandbox is still unmeasured. Creating a new
   sandbox took 0.6–0.8 s.
2. **`exec`: yes, with two catches.** Exit code (3), stdout and stderr merged in
   order, and `cwd` are right. A timeout raises `DaytonaProcessExecutionTimeoutError`
   and the command is killed.
   - **A background child that keeps the output open blocks `exec`.**
     `(sleep 300; echo late) &` hung the first spike run for 25 minutes. With a
     timeout, `exec` raises and the child is killed. With its output redirected
     (`nohup … >/dev/null 2>&1 &`), `exec` returns in 0.3 s and the child keeps
     running. So every command needs a timeout, even when the model gives none.
   - **Live output needs a "session".** Plain `exec` returns output only at the
     end. A session command (`run_async=True`, then
     `get_session_command_logs_async`) streams: the first chunk arrived after
     0.6 s, and the stream closed when the command ended (4.3 s, exit 4). A
     session is one long-lived shell, so a bare `exit` ends the shell and the log
     stream never closes: run each command in its own `sh -c`.
3. **Bytes and folders: yes.** `upload_file` creates parent folders, a binary
   round trip is identical, `list_files` and `get_file_info` work, and a missing
   file raises `DaytonaFileNotFoundError`.
4. **`rg` is in the default image** (`/usr/bin/rg`, ripgrep 14.1.1). No custom
   snapshot needed.
5. **The network can be blocked:** with `network_block_all` the sandbox can't even
   resolve DNS. The SDK also has `network_allow_list` (CIDRs) and
   `domain_allow_list`.
6. **Found again by label and by name: yes.** `list(labels=…)` finds it, and
   `get(name)` returns it. A second `create` with a taken name is refused
   (`DaytonaConflictError`), so a name we choose works as a get-or-create key.
   **It stops on its own:** the first run's sandbox was found stopped after 15
   minutes (`auto_stop_interval=15`) although an `exec` was still hanging, so
   Daytona counts API calls as activity, not running processes.

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
