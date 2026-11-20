# ADR-013 — `prepare_request`: change the model or thinking level before each model call

- **Status:** Rejected (2026-11-20): not needed now; the owner skipped step 19
- **Date:** 2026-11-20
- **Deciders:** project owner

## Context
ADR-003 step 19. A run uses one model and one thinking level from start to end:
`stream_run` (`api/sessions.py`) reads `session.model` and
`session.thinking_level` once and passes them to `run_loop`, which uses them for
every turn. There is no way to say "this turn is simple, use a cheaper model" or
"the model just hit an error, think harder on the retry".

What we already have:

- **`LoopConfig`** (`agent/hooks.py`) holds the loop's optional async hooks:
  `before_tool_call`, `after_tool_call`, `finish_turn`, the two queue polls and
  `convert_to_llm`.
- **One model call per turn**, built in `run_loop` (`agent/loop.py`): queued
  messages are added, the placeholder `MessageStartEvent` is emitted with the
  model name, `convert_to_llm` runs, a `Context` is built, and
  `provider.stream(model, context)` is called.
- **The model is recorded on each reply.** `AssistantMessage.provider` / `.model`
  say who wrote it. `to_wire_messages` (`openai_compat.py:272`) sends signed
  thinking back only when the reply's model matches the request's (ADR-012).
- **Compaction** (ADR-009) runs after the loop with the session's model, and an
  overflow retry calls `run_loop` again.

How pi does it:

- **`prepareRequest`** (`agent/src/types.ts:186`) gets
  `{context, model, thinkingLevel}` and may return any of them to replace.
  It is called right before every conversational request, after queued messages
  were added (`agent-loop.ts:228`), and not for compaction or other side calls.
- **The change sticks for the rest of the run**: the loop keeps the returned
  values in `config` and `currentContext`, so the next call starts from them.
- **A rejection ends the run with an error** (the coding agent's routed models
  rely on it, `agent-session.ts:852`).
- **`prepareNextTurn`** is a second, similar hook run once between turns that
  can also add messages. Not on our roadmap.
- The coding agent uses `prepareRequest` to rebuild the context from the session
  and to route "virtual" models to a real one per request.

## Decision
**1. The hook.** `LoopConfig.prepare_request: PrepareRequest | None`, async like
the other hooks:

```python
@dataclass(frozen=True)
class PrepareRequestContext:
    turn: int                      # 1 for the first turn, as in FinishTurnContext
    model: str                     # what this call would use
    thinking_level: ThinkingLevel
    messages: list[AgentMessage]   # a copy of the transcript, before convert_to_llm

@dataclass(frozen=True)
class PrepareRequestResult:
    model: str | None = None              # None keeps the value
    thinking_level: ThinkingLevel | None = None

type PrepareRequest = Callable[[PrepareRequestContext], Awaitable[PrepareRequestResult | None]]
```

**2. When it runs.** In `run_loop`, once per turn: after queued messages are
added, before the placeholder `MessageStartEvent`. So the placeholder, the
`Context` and the saved `AssistantMessage` all carry the model that really
answered. It is not called for compaction's summary call, which stays on the
session's settings, as in pi.

**3. Per call, not sticky.** The hook always sees the session's model and level
(the values `run_loop` was given), and what it returns applies to that one call.
Returning `None` means "the session's settings". pi keeps the change for later
calls instead. We don't, so the hook has no hidden state to reason about: each
turn it decides from scratch. A hook that wants "stay on the big model once
switched" can remember that itself.

**4. Not saved to the session.** The session's `model` and `thinking_level` are
unchanged, and no notice is saved (unlike a user's switch, ADR-008/012). The
reply's own `model` field is the record of which model answered.

**5. Failures.** A hook that raises fails the run with that error, like
`finish_turn`. There is no safe default to fall back to: the hook was the one
choosing. The loop doesn't check the model name: a wrong one comes back from
the provider as an error reply, which ends the run as any provider error does.

**6. Only the loop.** No route, setting or UI uses it yet, as with the other
hooks (steps 7, 8, 11, 12). Tests drive it with a fake provider: a hook that
swaps the model on turn 2 shows the swapped model in the request, the
placeholder event and the saved reply, and turn 3 is back on the session's
model.

**Not built:** changing the context (messages, system prompt, tools) from the
hook. pi allows it; we leave it with `transform_context` (deferred by ADR-009)
until something needs it, since `convert_to_llm` already shapes what the model
reads. Also not built: changing the provider (needs step 20, a second provider),
pi's `prepareNextTurn`, and any policy such as "cheap model for simple turns".

## Alternatives considered
- **Sticky changes, as in pi** — matches pi, and a run that escalates to a
  bigger model stays there without the hook tracking it. But the hook's input
  then depends on what it returned before, and `None` no longer means the
  session's settings. Easy to add later as a flag if a policy needs it.
- **Let the hook replace the whole `Context`** — one return type covers
  everything, as in pi. But it hands the hook the converted messages, the tool
  specs and `max_tokens` too, and it overlaps with `convert_to_llm`. Nothing
  needs that yet.
- **Save the change on the session** — the next run would start on the new
  model. But then a per-turn choice silently changes the user's setting, and the
  model picker would no longer show what the user chose.
- **Make the routing a setting instead of a hook** (e.g. `cheap_model` used for
  turns without tool calls) — usable from the UI right away, but bakes one
  policy into the loop. The hook comes first; a setting can be built on it.

## Consequences
- One run can hold replies from different models. Code that assumes every reply
  in a run came from `session.model` must read `AssistantMessage.model` instead.
  Signed thinking already follows the reply's model (ADR-012), so a model swap
  sends only the text of the other model's reasoning, if anything.
- Compaction's threshold check and summary call still use the session's model
  and its context window. A hook that moves a turn to a model with a smaller
  window can overflow it; the existing overflow path (ADR-009) compacts and
  retries, and the retry calls the hook again.
- A swap misses the prompt cache: caches are kept per model, so the swapped
  turn pays full input price on the other model. The next turn back on the
  session's model can still reuse its earlier cache while it is warm (minutes
  on most providers). A hook that swaps every other turn can cost more than it
  saves; a cheap model is worth it on long, simple turns. Matters once prompt
  caching is built (ADR-014 step 36).
- Asking for thinking on a model that can't reason sends the `reasoning` field
  anyway, and what OpenRouter does with it for such a model isn't checked yet
  (to verify when building). The hook should check the catalog
  (`ModelInfo.reasoning`) before raising the level.
- Step 20 (a second provider) will need the result to name a provider too, or
  the model to carry it. The context and result types are dataclasses, so a
  field can be added without breaking hooks that don't set it.
