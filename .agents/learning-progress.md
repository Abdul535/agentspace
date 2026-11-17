# Learning progress

How the project owner is learning the codebase, one row per activity. The rules
are in `AGENTS.md`: predict before each step, explain it back after, a quiz before
each commit, and a debugging session offered after every second commit.

**Commits since the last debugging offer:** 0 (started counting 2026-11-18)

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
| 2026-11-18 | After 2 commits | Debugging offer | Offered | |
