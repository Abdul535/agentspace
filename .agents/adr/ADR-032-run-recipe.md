# ADR-032 — Run behaviour lives in the run recipe, not in HTTP routes

- **Status:** Accepted
- **Date:** 2026-11-22
- **Deciders:** project owner

## Context
`stream_run` in `api/sessions.py` decided everything about a run:

- the tools (built-in, plus MCP)
- the skills rescan
- the system prompt and the trace
- the image rule
- the steer and follow-up queues
- the compaction policy

The manual compact route kept its own copy of the compaction call. Every run
feature (ADR-009, 015, 016, 017, 018, 019) edited that one HTTP helper, which
made `sessions.py` the most-changed file. The compaction rules could only be
tested through the test client with Postgres and Redis. A later worker process
(ADR-022 step 61) can't be handed a closure built inside a request.

Issue #35 and PR #36 made the change.

## Decision
- **One module.** `agent/run_recipe.py` builds what a run of a given kind does.
  It takes a claimed `Run` and a kind (`PromptRun`, `ContinueRun`,
  `CompactRun`), plus settings and, optionally, a model lookup, the MCP manager
  and the MCP server configs. It returns the loop function `RunManager`
  drives. A compact run needs only settings.
- **What moved, unchanged.** ADR-009's threshold and overflow checks now live
  there, along with ADR-015's image rule, ADR-016's skills rescan, ADR-017's MCP
  tool assembly and ADR-018's system prompt hand-off. These ADRs still hold;
  only their location changed. Where they name `stream_run`, read
  `run_recipe`.
- **What routes keep.** Only HTTP concerns:
  - request checks and the claim's typed refusals
  - releasing the DB connection (ADR-027)
  - the first-prompt title
  - turning stream entries into SSE events
- **Testing.** Run behaviour is tested at the run recipe's interface
  (`tests/test_run_recipe.py`): a fake provider and a list-collecting `emit`,
  with no HTTP, Postgres or Redis. HTTP tests cover only HTTP concerns.

## Alternatives considered
- **Keep it in the route.** No change needed. But every run feature keeps
  editing HTTP code, the tests stay slow, and a worker can't rebuild a run.
- **Also narrow `RunManager`'s interface (claim/attach/release) now.** That
  would be a bigger, riskier change. It was left for a later step (candidate #2
  of the 2026-11-22 architecture review).

## Consequences
- **One place to edit.** A new run feature (for example tool approvals) edits
  the run recipe, not the routes.
- **Failures become events.** Creating the workspace and rescanning skills now
  happen inside the run. A failure there ends the run with an `agent_end`
  error event instead of breaking the stream.
- **Ready for workers.** A worker (step 61) can build a run from a claimed run
  and a kind, without an HTTP request.
- **Name.** "Run recipe" is defined in `GLOSSARY.md`.
