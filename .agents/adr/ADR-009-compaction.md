# ADR-009 — Compaction: summarize older messages when the context nears the model's window

- **Status:** Accepted
- **Date:** 2026-11-20
- **Deciders:** project owner

## Context
ADR-003 step 17. Every model call sends the whole transcript, converted
(ADR-008). A long session eventually grows past the model's context window. The
provider then refuses the request, the reply is saved with `stop_reason="error"`,
and every later prompt fails the same way. Today the only way out is to start a
new session.

What we already have:

- **The window size.** The model catalog (ADR-005) reports `context_length` per
  model; it is `None` when the catalog doesn't know.
- **The token count.** Each `AssistantMessage.usage` holds the input and output
  tokens the provider reported for that reply.
- **A place for the summary.** App messages and `convert_to_llm` (ADR-008).
- **A way to retry.** Continue (step 15) restarts a run from a transcript that ends
  in a user message or tool result.

How pi does it (`coding-agent/src/core/compaction/`, `agent-session.ts`):

- **When.** pi checks after each run (`_checkCompaction`, `agent-session.ts:3119`).
  - *Threshold:* the last reply's tokens exceed `context window - reserveTokens`,
    where `reserveTokens` is 16k (`shouldCompact`, `compaction.ts:267`). pi
    compacts before the next prompt.
  - *Overflow:* the provider refused with a context-overflow error, which it
    recognizes by matching known error texts (`ai/src/utils/overflow.ts`). pi
    compacts and retries **once**; if that fails too, it stops with a clear error.
  - *Manual:* the user runs `/compact`, optionally with instructions.
- **What is kept.** pi walks back from the newest message and keeps about
  `keepRecentTokens` (20k), estimating tokens as characters ÷ 4
  (`findCutPoint`, `compaction.ts:446`). It never cuts at a tool result, so a
  kept result always has its call.
- **The summary.** The older messages are converted, written out as plain text
  (`[User]: …`, `[Assistant tool calls]: …`; `serializeConversation`,
  `utils.ts:121`) and sent as **one** user message. Plain text keeps the model
  from carrying on the conversation instead of summarizing it.
  - The prompt asks for fixed sections: Goal, Constraints, Progress, Key
    Decisions, Next Steps, Critical Context (`compaction.ts:507`).
  - When an earlier summary exists, an "update this summary" prompt is used
    instead (`compaction.ts:540-579`).
  - A summary that errored or was cut off by the token limit is rejected
    (`getSummarizationFailure`).
- **Nothing is deleted.** pi appends a `compaction` entry holding the summary and
  `firstKeptEntryId`. `buildContextEntries` (`session-manager.ts:476`) builds the
  model's context as the latest summary, then the kept entries, then everything
  after the summary.

## Decision
**1. A summary message.** `agent/messages.py` gains `CompactionSummaryMessage`
with these fields:

- `role: "compaction_summary"`;
- `summary`: the text;
- `kept`: how many messages directly before this one stay in the model's
  context;
- `tokens_before`: the context size when it was made, for the UI;
- `timestamp`.

It is **appended** to the transcript like any message. Nothing is deleted or
rewritten, and the database schema does not change.

`kept` counts back from the summary's own position, so it needs no message ids:
a transcript is a list, and the summary knows where it sits in it.

**2. The model's view starts at the latest summary.** A new `agent/compaction.py`
holds `build_context(messages)`. It finds the last `CompactionSummaryMessage`
and returns:

- that summary;
- the `kept` messages before it;
- everything after it.

With no summary, it returns the list unchanged. `stream_run` passes
`build_context(run.active.messages)` as the loop's `history`, the way pi's
session layer hands the agent its context. The run's own `messages`, which
snapshots and the UI use, stay complete.

The default `convert_to_llm` turns the summary into a user message: "The
conversation before this point was compacted into this summary:
`<summary>…</summary>`".

`can_continue` and the continue route check the built context, not the full
transcript.

**3. Making a summary.** `compact(messages, provider, model, …)` in
`agent/compaction.py` works in four steps:

1. *Find the cut.* Walk back from the newest message in the built context and
   keep about `compaction_keep_recent_tokens` (20k by default; characters ÷ 4).
   The cut lands only on a user or assistant message. If there is nothing before
   the cut, there is nothing to compact (422 for a manual request).
2. *Write out the older part.* Convert it with `convert_to_llm` (a previous
   summary is included, so its content carries forward). Then write it as plain
   text in pi's `[User]: …` style, shortening long tool results.
3. *Ask the session's model.* Send that text in one user message with pi's
   section prompt, or the update prompt when an earlier summary exists. No
   tools, no system prompt beyond a short "you summarize conversations", and
   `max_tokens` set to `max_output_tokens`.
4. *Check the reply.* A reply that stopped on `error` or `length`, or tried to
   call a tool, is rejected, and nothing is saved.

**4. When it runs.** All three cases run inside a run task (ADR-007). So while
compacting, the session is busy (PATCH and prompts get a 409), the Stop button
cancels it, and the summary is saved through the normal `emit` path. Two new
events, `compaction_start {reason}` and `compaction_end {reason, error}`, frame
it; the summary itself arrives as `message_start`/`message_end`.

- **Threshold.** The run's loop function in `stream_run` checks after `run_loop`
  returns. The context size is the last good reply's `usage` (input + output),
  plus characters ÷ 4 for any messages after that reply. If the size exceeds
  `context_length - compaction_reserve_tokens` (16k by default), it compacts.
  If the catalog doesn't know the window, the threshold check is skipped.
- **Overflow.** The run ended with `reason="error"`, and the error text matches
  a known context-overflow pattern (`ai/overflow.py`, a short list after pi's).
  The loop function then compacts and continues (step 15) **once**. A second
  overflow ends the run with the error as it is.
- **Manual.** `POST /sessions/{id}/compact` streams events like `/continue`. It
  returns 409 while running, and 422 when there is nothing to compact.

These events can come after the run's `agent_end`, and an overflow retry adds a
second `agent_start`…`agent_end` to the same stream. The stream still ends only
when the task does. That is how pi orders them too.

**5. Settings.** `compaction_enabled` (true), `compaction_reserve_tokens` (16384)
and `compaction_keep_recent_tokens` (20000).

**6. Web.** A summary shows as a divider: "Conversation compacted · N messages
summarized". It expands to show the summary. The messages above it stay visible.
While `compaction_start` is active, a "Compacting…" status shows. A **Compact**
item in the session header calls the manual route.

## Alternatives considered
- **pi's entry layer (`firstKeptEntryId`).** A compaction would be a session
  entry pointing at a message row id. Our transcript is a list of messages with
  no ids in the models, and ADR-008 chose one layer, so a relative `kept` count
  does the same job. Revisit with branching (step 25), where ids matter.
- **Delete or replace the old messages.** Less to send and store, but the
  history and the user's view would be lost. Rejected: the database is the
  record.
- **Do the cut inside `convert_to_llm`.** One place for everything. But every
  custom converter would then have to reimplement compaction. Rejected: building
  the context and converting messages are separate jobs, as they are in pi.
- **An async `transform_context` hook that compacts mid-run** (named as step 17's
  in ADR-008). Not needed yet: a run that overflows mid-way ends with an error,
  and the overflow path compacts and continues it. The hook can come later with
  pruning (dropping old tool output) if that's wanted.
- **Count tokens exactly with a tokenizer.** Needs a tokenizer per model family.
  The provider's own `usage` is exact for everything up to the last reply, and
  characters ÷ 4 is close enough for the rest.
- **Manual only.** Simpler, but a long session just breaks until the user
  notices. Rejected in favour of automatic plus a button.
- **A cheaper model for summaries.** Saves money on long sessions. Left out for
  now: the session model is known to accept this context. It can come with
  `prepare_request` (step 19).

## Consequences
- A long session keeps working: the model gets summary + recent messages, the UI
  and the database keep everything.
- Compaction is a model call the user didn't type. It costs tokens, and the
  summary can lose details. The prompt asks for exact paths, names and errors to
  be kept, and the full history stays readable.
- A tool result larger than the whole window can't be fixed by compaction. The
  retry overflows again and the run ends with the provider's error.
- A run's event stream can now hold compaction events and, after an overflow, a
  second `agent_start`…`agent_end`. The web client reads events until the
  stream closes, so it already copes.
- Tests to add:
  - `build_context` with no summary, one summary, and two summaries (the latest
    wins);
  - the cut point never lands on a tool result and keeps about the budget;
  - the summary request is one user message holding the plain-text transcript,
    using the update prompt when an earlier summary exists;
  - a summary that errors or hits `length` saves nothing;
  - the threshold check compacts after a run, and doesn't when the window is
    unknown;
  - an overflow error compacts and continues once, and a second overflow stops;
  - the manual route returns 409 and 422, and saves one summary;
  - the converted summary is what the model gets on the next prompt.
