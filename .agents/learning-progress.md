# Learning progress

How the project owner is learning the codebase, one row per activity. The rules
are in `AGENTS.md`: predict before each step, explain it back after, a quiz before
each commit, and a debugging session offered after every second commit.

**Commits since the last debugging offer:** 0 (offered 2026-11-20 with the CI commit)

## Log

| Date | Step / change | Activity | Result | Gaps to revisit |
|------|---------------|----------|--------|-----------------|
| 2026-11-17 | ADR-003 step 2: `edit` tool | Quiz | 3/4 | |
| 2026-11-17 | ADR-003 step 3: `bash` tool | Quiz | 1/4 | Which questions were missed wasn't recorded |
| 2026-11-17 | `AGENTSPACE_ENABLE_BASH` opt-in | Quiz | 2/3 | |
| 2026-11-17 | ADR-003 step 4: `grep`, `find`, `ls` | Quiz | 2/4 | Which questions were missed wasn't recorded |
| 2026-11-17 | ADR-003 step 5: tool sets | Quiz | 3/4 | |
| 2026-11-17 | ADR-003 step 6: system prompt from tools | Quiz | 3/4 | |
| 2026-11-17 | ADR-003 steps 7–12, ADR-006 | Quiz | not recorded | Hooks exist but `send_message` passes no `LoopConfig` |
| 2026-11-18 | pi agent loop vs ours (diagram) | Walkthrough | Saw why our loop differs from pi: Python task cancellation vs `AbortSignal`, run tied to the HTTP request, `max_turns` cap | |
| 2026-11-18 | Learning rules in `AGENTS.md` | Quiz | 2/3 | The debugging counter resets when the session is offered, not accepted (`AGENTS.md:30`) |
| 2026-11-18 | `pnpm dev:api` script | Quiz | 3/3 | |
| 2026-11-18 | After 2 commits | Debugging offer | Declined ("not now"), then asked for one | |
| 2026-11-18 | Debug session 1: `except BaseException` swallows cancellation | Debugging | Found the function unaided, the line with one nudge; the why needed a walkthrough | `run_agent` vs `run_loop`, tasks and cancellation, `Exception` vs `BaseException`: see `.agents/learning-plan.md` topics 1–4 |
| 2026-11-18 | Cancellation test assertion; ADR-004, Daytona spike, Phase 2 research | Quiz | Skipped at the owner's request | |
| 2026-11-19 | More reference designs, architectures diagram, ADR-003 accepted | Quiz | 4/4 | |
| 2026-11-19 | ADR-003 step 13: background runs | Prediction | Right core idea: server owns the run, client re-subscribes. Proposed Redis for state | Missed the catch-up gap (events between disconnect and resubscribe); state that SQLite already holds the transcript; Redis not needed for one process |
| 2026-11-19 | ADR-003 step 13: background runs (ADR-007) | Explain-back, quiz | Skipped at the owner's request ("commit and don't ask anything") | Background runs untested by quiz: `RunManager.claim` lock, done-callback vs `finally`, snapshot + subscribe with no `await` between |
| 2026-11-19 | After 2 commits | Debugging offer | Offered with step 14 | |
| 2026-11-19 | ADR-003 step 14: steering and follow-up queues | Prediction | Skipped ("go for step 14"); debugging offer not taken up | Where queued messages enter the loop; steering vs follow-up timing; `one_at_a_time` vs `all` |
| 2026-11-19 | ADR-003 step 14: steering and follow-up queues | Explain-back, quiz | Skipped at the owner's request ("commit and don't ask anything") | Loop poll order (steering before follow-ups), `accepting` flag in `take_follow_ups`, why the web lists a queued message before the request |
| 2026-11-19 | ADR-003 step 15: continue | Prediction | Didn't know; walked through pi's `agentLoopContinue` / `Agent.continue` | Why a transcript ending in an assistant message can't continue (the model would be replying to itself); continue = run the loop with no new prompt |
| 2026-11-19 | ADR-003 step 15: continue | Explain-back | Skipped (went straight to "commit and push") | |
| 2026-11-19 | ADR-003 step 15: continue | Quiz | 1/3 answered right, 1 skipped | Q1 skipped: `can_continue` on a transcript ending in a failed reply (True: `prepare_transcript` drops it). Q2: a 422 from a dependency never starts the run; `claim_run`'s `finally` → `release` frees the slot. Q4 (step 14 gap, still open): after the last empty follow-up poll `accepting` is False, so steer gets a 409, not a conversion |
| 2026-11-19 | After 2 commits | Debugging offer | Offered with step 15 | |
| 2026-11-20 | ADR-003 step 16: custom messages + `convert_to_llm` | Prediction | No idea; walked through pi's `convertToLlm` (`coding-agent/src/core/messages.ts`) | Two transcripts: what is saved vs what the model gets; translate-or-drop happens right before each model call, not at save time |
| 2026-11-19 | After 2 commits | Debugging offer | Not taken up (moved on to step 16) | |
| 2026-11-20 | ADR-003 step 16: app messages + `convert_to_llm` (ADR-008) | Explain-back | Skipped (went straight to "commit it") | |
| 2026-11-20 | ADR-003 step 16: app messages + `convert_to_llm` (ADR-008) | Quiz | 2/3 | Q3: a converter that raises is not caught; the exception ends the run before `provider.stream` (`loop.py:208`), nothing is sent. No fallback |
| 2026-11-20 | CI (ADR-010): GitHub Actions + `ty` | Prediction | Skipped ("go for 1"); asked which Python type checkers the industry uses | How the API-types check runs: export the spec with Python, generate the TS with Node, then `git diff --exit-code` fails on any change |
| 2026-11-20 | CI (ADR-010): GitHub Actions, `ty`, pre-commit hook | Explain-back | Skipped (went straight to "let's commit it") | |
| 2026-11-20 | CI (ADR-010): GitHub Actions, `ty`, pre-commit hook | Quiz | 0/4 | Q1: `ended()` drops notices via `convert_to_llm` (`test_loop.py:72`). Q2: `AsyncGenerator` because `aclosing()` needs `aclose()` (`find.py:63`), flagged by `ty` not ruff. Q3: `git diff --exit-code` compares working tree to the index, so unstaged regenerated API types fail the hook (`package.json:13`). Q4: `assert` narrows the type *and* raises at runtime; TS `!` is erased |
| 2026-11-20 | After 2 commits | Debugging offer | Offered with the CI commit | |
| 2026-11-20 | CI fix: fresh-machine failures (catalog cache test, `next typegen`) | Quiz | 0/3 | Q1: `time.monotonic()` counts from boot, so `_fetched_at = 0` is still "fresh" on a just-booted CI machine (`catalog.py:41`). Q2: `PageProps`/`LayoutProps` come from `next typegen`; without it `tsc` fails on a fresh clone. Q3: `monotonic()` ≈ `performance.now()`, not `Date.now()` |
