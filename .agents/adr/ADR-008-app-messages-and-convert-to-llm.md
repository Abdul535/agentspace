# ADR-008 — App messages in the transcript, converted for the model by `convert_to_llm`

- **Status:** Accepted
- **Date:** 2026-11-20
- **Deciders:** project owner

## Context
ADR-003 step 16. Today the saved transcript and what the model gets are the same
list:

- `Message` (`ai/messages.py`) is `UserMessage | AssistantMessage |
  ToolResultMessage`. The store parses every `messages` row as one of the three
  (`session/store.py`).
- `run_loop` (`agent/loop.py`) sends its `messages` list to the provider as it
  is. The provider adapter then runs `prepare_transcript` (`ai/provider.py`),
  which drops failed replies and patches tool calls that never got a result.

So nothing can be saved that is only for the person reading the chat, such as a
"model switched" notice, a shell command the user ran, or (step 17) a summary that
stands in for older messages. Each of these needs to be shown in the chat and
then dropped or rewritten before the model is called.

How the reference designs do it:

- **pi**, at two levels:
  - *Messages.* `AgentMessage` is `Message` plus the app's own roles
    (`packages/agent/src/types.ts:365-374`). The coding agent adds
    `bashExecution`, `custom`, `branchSummary` and `compactionSummary`
    (`coding-agent/src/core/messages.ts:28-66`).
  - *Conversion.* Before every model call, the loop runs `transformContext` and
    then `convertToLlm` (`agent-loop.ts:397-404`). The coding agent's
    `convertToLlm` (`messages.ts:148-194`) rewrites the custom roles as user
    messages, drops `bashExecution` marked `excludeFromContext`, and drops any
    role it does not know.
  - *Contract.* The function must not throw; it returns a safe fallback instead
    (`types.ts:203`). Whether a run can continue is judged on the converted list
    (`agent-loop.ts:76-78`).
  - *Entries.* The session file holds entries, not messages:
    `message`, `model_change`, `compaction`, `custom` (never sent),
    `custom_message` (sent) and others (`session-manager.ts:60-190`). A model
    switch is a `model_change` entry. `buildSessionContext` turns the entries
    into the message list.
- **opencode:** messages are made of typed parts, and the parts are converted to
  the AI SDK's model messages right before the call. Parts the model must not see
  are skipped in that step.

## Decision
**1. `AgentMessage` in the agent layer.** A new `agent/messages.py` defines
`AgentMessage = Message | NoticeMessage`, a union told apart by `role`.
`ai/messages.py` does not change: providers still only know the three LLM roles.
The first app role:

- `NoticeMessage` (`role: "notice"`, `text`, `timestamp`): a line for the person
  reading the chat. The model never sees it.

Step 17 adds `compaction_summary` the same way. Further roles (a shell command
the user ran, extension messages) are added when a feature needs them.

**2. `convert_to_llm` runs before every model call.**

- `LoopConfig.convert_to_llm: Callable[[list[AgentMessage]], list[Message]]`. It
  is plain, not `async`; step 17's `transform_context` will be the async one.
- The default, `agent.messages.convert_to_llm`, keeps the three LLM roles and
  drops the rest, as pi's does for roles it doesn't know.
- `run_loop` keeps its working list as `list[AgentMessage]`. It calls the
  converter when it builds each `Context`, just before `provider.stream`.
- A converter that raises fails the run (`agent_end`, reason `error`). Unlike
  pi, we don't promise that it never throws: a converter bug should be loud, not
  quietly send the model a different transcript.
- `prepare_transcript` stays in the provider adapter and runs after conversion.
  Converting is app knowledge; making a transcript valid for the provider API is
  provider knowledge.

**3. Saved and streamed as `AgentMessage`.**

- `SessionStore.messages` parses rows as `AgentMessage`. The `messages.role`
  column is already a free string, so there is **no schema change** and no
  migration, and old rows parse as before.
- `MessageStartEvent`, `MessageEndEvent`, `AgentEndEvent.new_messages`,
  `RunSnapshot.messages`, `SessionDetail.messages` and `run_loop`'s `history`
  widen to `AgentMessage`.

**4. `can_continue` checks the converted list.** It runs
`prepare_transcript(convert_to_llm(history))`. A notice saved after a failed run
must not hide that the model still owes a reply, and a notice at the end must
not count as a reply either.

**5. First use: a notice on model switch.** `PATCH /sessions/{id}` appends
`NoticeMessage("Switched model from A to B")` after saving the new model, only
when the model actually changed. PATCH already returns 409 while a run is going,
so a notice never lands between a tool call and its result. The web chat shows
notices as a quiet divider line, and the activity panel ignores them.

## Alternatives considered
- **pi's two levels: session entries plus messages.** A model switch would be a
  `model_change` entry, a different kind of row from messages, and a context
  builder would turn entries into messages. It's the cleaner split once there
  are entries that are not messages at all (labels, extension state, branch
  points). It's more than step 16 needs: our only non-LLM row today is
  something to *show* in the chat, which is a message. Revisit with branching
  (step 25) or extension state.
- **Convert when saving (store the LLM version).** No converter at call time.
  But the UI would lose the real thing (it would show "user: I ran `ls`…" instead
  of a command block), and compaction needs the original messages too. Rejected.
- **Convert in the provider adapter, next to `prepare_transcript`.** One place
  for both steps, but every adapter would have to know the app's message types,
  and `ai/` would depend on `agent/`. Rejected.
- **Add the app roles to `ai.messages.Message`.** Fewer types, but every provider
  would have to handle roles it can never send. Rejected.
- **A generic `custom` role now (pi's `CustomMessage`, with `custom_type` and
  `display`).** Flexible, but nothing writes one yet. Added when extensions
  (Phase 5) need it.

## Consequences
- The transcript the UI shows and the one the model gets can now differ, on
  purpose. Code that needs the model's view calls the converter; code that shows
  or saves the transcript uses `AgentMessage`.
- The web client's `Message` type gains `notice`, and the generated API types
  force every `role` switch to handle it.
- Step 17 (compaction) builds on this: a `compaction_summary` role, converted to
  a user message, plus an async `transform_context` that runs before
  `convert_to_llm`.
- Tests to add:
  - the default converter drops notices and keeps the three LLM roles in order;
  - a custom `convert_to_llm` sees the `AgentMessage`s and its output is what
    the provider gets;
  - a notice never reaches the provider;
  - `can_continue` looks past a notice at the end;
  - PATCH with a new model saves one notice, and PATCH with the same model saves
    none;
  - a transcript holding a notice round-trips through the store and `GET`.
