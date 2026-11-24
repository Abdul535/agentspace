# ADR-033 — Workspace files survive deploys by moving into sandboxes, with no stopgap

- **Status:** Accepted
- **Date:** 2026-11-24
- **Deciders:** project owner

## Context
The API runs as one ECS Fargate task (ADR-030 §1). Each session's workspace is a
folder on that task's disk (`<data_dir>/workspaces/<session_id>`, with
`AGENTSPACE_DATA_DIR=/app/data`). A deploy starts a new task and stops the old
one, and a crash replaces the task too, so every file the agent wrote is lost.
ADR-030 says the web app must tell users this before the app is public.

The tools still take a plain `Path` (`ToolContext.workspace`,
`tools/paths.py`). ADR-004's `Workspace` protocol (ADR-022 step 59) is not built,
so nothing can yet put the files anywhere but the task's disk.

How the reference designs keep workspace files:

- **CompanyOS** runs its API on ECS but keeps no files there. Each session gets
  its own cloud sandbox (`session_id == sandbox_id`; Platinum, Daytona as a
  fallback), and the workspace lives in the sandbox. Replacing an API task
  touches no file.
- **pi and opencode** work in a folder on the user's own machine, so there is no
  server to replace.

The owner's prediction (`.agents/learning-progress.md`): keep the files in a
volume or storage that outlives the server, and show a loading state until they
are back. Right about the storage. Today there is nothing to load, though, so
the honest cue until the fix ships is a warning.

## Decision
**In one line:** workspace files move into a sandbox per session (ADR-022
Milestone C), built now. There is no interim EFS or S3 stopgap.

- **Order:** step 59 (the `Workspace` protocol with `LocalWorkspace` only),
  then the Daytona spike (`apps/api/scripts/spike_daytona.py`, ADR-004's six
  questions, E2B if Daytona fails 1 or 2), then step 28 (`SandboxWorkspace`,
  with files living in the sandbox). Step 60 (MCP in the sandbox) stays after.
  Each step gets its own ADR and PR, as ADR-022 lists.
- **Until step 28 ships,** the app stays private (owner and invited testers).
  If it has to go public sooner, the web app first shows a notice that files
  are temporary (option A below). That notice is its own change.
- **The UI once sandboxes exist:** a session whose sandbox is starting or
  resuming shows a designed "starting workspace" state, not a blank page or a
  spinner with no words. That is where the owner's loading screen belongs.
- **Infra waits for PR #38 (ADR-031).** Any Terraform this needs (a sandbox
  vendor key in Secrets Manager, for one) is built on `dev` after #38 merges.

## Alternatives considered
- **A. A UI notice only.** Half a day, no cost, but files are still lost on
  every deploy and crash. Kept as the fallback if the app goes public before
  step 28.
- **B. Sync each workspace to S3 or Supabase Storage.** Archive the folder after
  each run and restore it before the next. Costs cents and allows several tasks.
  But it is 2–3 days of app code that sandboxes replace, and a crash loses the
  current run's files.
- **C. EFS mounted at the workspaces folder.** About a day of Terraform, no app
  change, survives deploys and crashes, and allows several tasks (ADR-030 kept it
  as the stopgap). It was the recommendation. The owner chose to skip the
  stopgap and build the real fix. ADR-022 also rejects EFS for the public
  product: once `bash` is on, files sit next to our secrets and other users'
  files.

## Consequences
- Workspace files keep being lost on each deploy and crash until step 28 ships.
  Deploys should stay rare and the app private until then.
- Steps 59 and 28 come before other roadmap work. Step 59 changes no behaviour;
  the existing tool tests prove it.
- A sandbox vendor becomes a new dependency and cost, billed per second while a
  sandbox runs. Idle sandboxes must stop on their own (ADR-004).
- Each tool call becomes a network round trip to the sandbox, so tool latency
  rises. Parallel tool calls matter more.
- Once workspaces leave the task's disk, ADR-030's "one task" limit for files is
  gone. Going to more tasks is then a change of `desiredCount` plus autoscaling.
- `bash`, `grep` and `find` can be turned on for public users in sandboxed
  sessions (ADR-004), and the sandbox image must include `rg`.
