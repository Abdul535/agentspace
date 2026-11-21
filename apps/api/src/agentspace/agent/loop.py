"""The agent loop: ask the model, run the tools it calls, repeat until it answers without tools.

A simplified port of pi's `runLoop` (`packages/agent/src/agent-loop.ts`): a
reply's tool calls run in parallel or one at a time (`LoopConfig.tool_execution`),
each through prepare, execute and finalize with the `LoopConfig` hooks. Steering
messages go in before the next model call, and follow-ups restart a run that would
stop (ADR-003 step 14). The run ends after a batch in which every call set
`terminate`, or when `finish_turn` says so; `max_turns` caps it either way.
The loop holds no storage; callers persist messages from `message_end` events.
The transcript it works on can hold app messages; `LoopConfig.convert_to_llm` turns
it into what the model reads before every model call (ADR-008).
With no prompt, it continues the transcript as it is (ADR-003 step 15), like pi's
`agentLoopContinue`: a retry after an error, a stop or a crash.

`run_loop` reports events through an `emit` callback, so any code holding it can
report one (ADR-006). `run_agent` wraps it as an async generator for callers
that read every event; closing or cancelling that generator stops the run at its
current await. The server drives `run_loop` through `RunManager` instead (ADR-007).
"""

import asyncio
import contextlib
import copy
import dataclasses
from collections.abc import AsyncGenerator, Awaitable, Callable, Sequence
from dataclasses import dataclass

from pydantic import BaseModel, ValidationError

from agentspace.agent.events import (
    AgentEndEvent,
    AgentEvent,
    AgentStartEvent,
    EndReason,
    MessageEndEvent,
    MessageStartEvent,
    MessageUpdateEvent,
    ToolExecutionEndEvent,
    ToolExecutionStartEvent,
    ToolExecutionUpdateEvent,
    TurnEndEvent,
    TurnStartEvent,
)
from agentspace.agent.hooks import (
    AfterToolCallContext,
    BeforeToolCallContext,
    ConvertToLlm,
    FinishTurnContext,
    GetMessages,
    LoopConfig,
    TurnDecision,
)
from agentspace.agent.messages import AgentMessage, convert_to_llm
from agentspace.agent.tool import Tool, ToolContext, ToolError, ToolResult
from agentspace.ai.messages import (
    AssistantMessage,
    TextContent,
    ThinkingLevel,
    ToolCall,
    ToolResultMessage,
    UserMessage,
)
from agentspace.ai.provider import (
    Context,
    DoneEvent,
    ErrorEvent,
    Price,
    Provider,
    TextDeltaEvent,
    ThinkingDeltaEvent,
    ToolCallEndEvent,
    prepare_transcript,
)

# Tool result for calls in a reply the token limit cut off; their arguments may be incomplete.
TRUNCATED_CALL = (
    'Tool call "{name}" was not executed: the response hit the output token limit, '
    "so its arguments may be truncated. Re-issue the tool call with complete arguments."
)


# Receives every event the loop reports, in order; awaited, like pi's `AgentEventSink`.
# TS: type Emit = (event: AgentEvent) => Promise<void>
type Emit = Callable[[AgentEvent], Awaitable[None]]


def can_continue(history: Sequence[AgentMessage], convert: ConvertToLlm = convert_to_llm) -> bool:
    """Whether the model can be asked to reply to this transcript with no new message.

    It can if what the model would see ends in a user message or a tool result: it still
    owes a reply. Not if it ends in an assistant message: the model would answer itself.
    Checked on the transcript as sent: converted (so app messages such as notices don't
    count), then `prepare_transcript` (a failed reply is dropped, a tool call cut off
    before its result gets an error result).
    """
    sent = prepare_transcript(convert(list(history)))
    return bool(sent) and not isinstance(sent[-1], AssistantMessage)


# TS: an `async function*` taking one options object (`*` = keyword-only); callers `for await` its events.
async def run_agent(
    *,
    provider: Provider,
    model: str,
    system_prompt: str,
    tools: Sequence[Tool],
    history: Sequence[AgentMessage],
    prompt: UserMessage | None,
    ctx: ToolContext,
    max_turns: int,
    max_tokens: int | None = None,
    thinking_level: ThinkingLevel = "off",
    config: LoopConfig | None = None,
) -> AsyncGenerator[AgentEvent]:  # TS: AsyncGenerator<AgentEvent>; it has aclose(), a bare AsyncIterator doesn't
    """Run the agent on one prompt (or continue, with None) and yield its events: `run_loop` in a task."""
    # None marks the end: the loop task finished and no more events are coming.
    queue: asyncio.Queue[AgentEvent | None] = asyncio.Queue()
    loop_task = asyncio.create_task(
        run_loop(
            provider=provider,
            model=model,
            system_prompt=system_prompt,
            tools=tools,
            history=history,
            prompt=prompt,
            ctx=ctx,
            max_turns=max_turns,
            max_tokens=max_tokens,
            thinking_level=thinking_level,
            config=config,
            emit=queue.put,
        )
    )
    # Runs however the task ends (done, raised or cancelled), so the reader below never waits forever.
    loop_task.add_done_callback(lambda _: queue.put_nowait(None))
    try:
        # TS: `:=` assigns inside the condition, like `while ((event = await queue.get()) !== null)`.
        while (event := await queue.get()) is not None:
            yield event
        loop_task.result()  # re-raise an error from inside the loop to the reader
    finally:
        # The reader stopped early (client gone, or cancelled): stop the run too.
        if not loop_task.done():
            loop_task.cancel()
            # TS: like `await task.catch(() => {})` for this one error type.
            with contextlib.suppress(asyncio.CancelledError):
                await loop_task


async def run_loop(
    *,
    provider: Provider,
    model: str,
    system_prompt: str,
    tools: Sequence[Tool],
    history: Sequence[AgentMessage],
    prompt: UserMessage | None,
    ctx: ToolContext,
    max_turns: int,
    max_tokens: int | None = None,
    thinking_level: ThinkingLevel = "off",
    price: Price | None = None,
    config: LoopConfig | None = None,
    emit: Emit,
) -> None:
    """Run the agent until it answers, fails or hits `max_turns`, passing each event to `emit`.

    With a `prompt`, the run starts by adding it. With None, it continues `history` as it is,
    which must pass `can_continue` (pi's `agentLoopContinue`).
    """
    # TS: `or` here works like `??`: a LoopConfig is always truthy, so only None falls through.
    config = config or LoopConfig()
    if prompt is None and not can_continue(history, config.convert_to_llm):
        raise ValueError("Cannot continue: the transcript is empty or ends with the model's reply.")
    messages: list[AgentMessage] = list(history)
    new_messages: list[AgentMessage] = []
    tool_specs = [tool.spec() for tool in tools]

    # TS: an inner function closing over `messages` and `new_messages`.
    def add(message: AgentMessage) -> None:
        """Append to the context sent to the model and to the list this run returns."""
        messages.append(message)
        new_messages.append(message)

    async def finish_turn(
        turn: int, assistant: AssistantMessage, results: list[ToolResultMessage]
    ) -> TurnDecision | None:
        """Ask the `finish_turn` hook, if any, what to do after this turn."""
        if config.finish_turn is None:
            return None
        # A copy, so the hook can't change what this run returns.
        return await config.finish_turn(FinishTurnContext(turn, assistant, results, list(new_messages)))

    await emit(AgentStartEvent())
    if prompt is not None:
        await emit(MessageStartEvent(message=prompt))
        add(prompt)
        await emit(MessageEndEvent(message=prompt))

    async def poll(get: GetMessages | None) -> list[AgentMessage]:
        """Queued messages from a hook, or none when the hook isn't set."""
        return await get() if get else []

    # Steering the user typed before the run got going goes in with the first turn, like pi.
    pending = await poll(config.get_steering_messages)
    reason: EndReason = "max_turns"
    for turn in range(1, max_turns + 1):  # TS: for (let turn = 1; turn <= maxTurns; turn++)
        await emit(TurnStartEvent(turn=turn))

        # Queued messages join the context before the model is asked again.
        for message in pending:
            await emit(MessageStartEvent(message=message))
            add(message)
            await emit(MessageEndEvent(message=message))
        pending = []

        # Stream the model's reply, forwarding thinking, text and tool-call pieces as they arrive.
        assistant: AssistantMessage | None = None
        await emit(MessageStartEvent(message=AssistantMessage(provider=provider.name, model=model)))
        # The model gets the transcript converted: the app's own messages are dropped or rewritten here.
        llm_messages = config.convert_to_llm(list(messages))
        context = Context(
            system_prompt=system_prompt,
            messages=llm_messages,
            tools=tool_specs,
            max_tokens=max_tokens,
            thinking_level=thinking_level,
            price=price,
        )
        # TS: for await (const event of provider.stream(...)), then a switch on the event's class.
        async for event in provider.stream(model, context):
            match event:
                case ThinkingDeltaEvent() | TextDeltaEvent() | ToolCallEndEvent():
                    await emit(MessageUpdateEvent(event=event))
                case DoneEvent() | ErrorEvent():
                    assistant = event.message
        # A well-behaved provider always ends with Done or Error; guard against one that doesn't.
        if assistant is None:
            assistant = AssistantMessage(
                provider=provider.name,
                model=model,
                stop_reason="error",
                error_message="The provider stream ended without a final message.",
            )
        add(assistant)
        await emit(MessageEndEvent(message=assistant))

        if assistant.stop_reason in ("error", "aborted"):
            # The hook still sees the turn, but can't keep a failed run going (pi does the same).
            await finish_turn(turn, assistant, [])
            await emit(TurnEndEvent(turn=turn, message=assistant, tool_results=[]))
            reason = "error" if assistant.stop_reason == "error" else "aborted"
            break

        calls = assistant.tool_calls()
        finished = await execute_tool_calls(calls, tools, ctx, assistant, config, emit)
        results = [f.message for f in finished]
        for result in results:
            add(result)
        decision = await finish_turn(turn, assistant, results)
        await emit(TurnEndEvent(turn=turn, message=assistant, tool_results=results))

        if decision == "end":
            reason = "finish_turn"
            break
        # Messages steered in during this turn always get a reply, whatever the tool calls did.
        pending = await poll(config.get_steering_messages)
        # One more turn on the same context, even after a final answer or a terminating batch.
        if pending or decision == "continue":
            continue
        # Go on while the model calls tools, unless every call asked to stop (pi's `shouldTerminateToolBatch`).
        # TS: all(...) is results.every(...)
        if calls and not all(f.terminate for f in finished):
            continue
        # The run would stop here; follow-ups queued for this moment start another turn.
        pending = await poll(config.get_follow_up_messages)
        if pending:
            continue
        # No tool calls means a final answer; otherwise every call asked to stop.
        reason = "terminated" if calls else "done"
        break

    await emit(AgentEndEvent(reason=reason, new_messages=new_messages))


# TS: types are hoisted, but Python evaluates signature annotations at def time, so this sits above its first use.
@dataclass(frozen=True)
class FinishedCall:
    """A call's final result, and whether it asked the run to stop after this batch (ADR-003 step 11)."""

    message: ToolResultMessage
    terminate: bool = False
    """Kept off the message, as in pi: it steers this run only, and is never sent to the model or saved."""


async def execute_tool_calls(
    calls: Sequence[ToolCall],
    tools: Sequence[Tool],
    ctx: ToolContext,
    assistant: AssistantMessage,
    config: LoopConfig,
    emit: Emit,
) -> list[FinishedCall]:
    """Run a reply's tool calls and return their results in the order the model made the calls."""
    names = {call.name for call in calls}
    # One tool that can't run alongside others makes the whole batch sequential, as in pi.
    sequential = config.tool_execution == "sequential" or any(
        tool.execution_mode == "sequential" for tool in tools if tool.name in names
    )
    # A reply cut off by the token limit may carry truncated arguments, so none of its calls run.
    if assistant.stop_reason == "length" or sequential:
        return await run_sequentially(calls, tools, ctx, assistant, config, emit)
    return await run_in_parallel(calls, tools, ctx, assistant, config, emit)


async def run_sequentially(
    calls: Sequence[ToolCall],
    tools: Sequence[Tool],
    ctx: ToolContext,
    assistant: AssistantMessage,
    config: LoopConfig,
    emit: Emit,
) -> list[FinishedCall]:
    """Each call starts, runs and reports its result before the next one starts."""
    results: list[FinishedCall] = []
    for call in calls:
        await emit(ToolExecutionStartEvent(tool_call_id=call.id, tool_name=call.name, arguments=call.arguments))
        if assistant.stop_reason == "length":
            result = FinishedCall(error_result(call, TRUNCATED_CALL.format(name=call.name)))
        else:
            result = await execute_tool_call(call, tools, ctx, assistant, config, emit)
        await emit(ToolExecutionEndEvent(result=result.message))
        await emit_result_message(result.message, emit)
        results.append(result)
    return results


async def run_in_parallel(
    calls: Sequence[ToolCall],
    tools: Sequence[Tool],
    ctx: ToolContext,
    assistant: AssistantMessage,
    config: LoopConfig,
    emit: Emit,
) -> list[FinishedCall]:
    """Prepare the calls in order, run the cleared ones at the same time, then report results in call order.

    Preparing in order keeps `before_tool_call` approvals one at a time, and no tool starts
    until every call has been cleared or refused, as in pi's `executeToolCallsParallel`.
    """
    prepared: list[PreparedCall | FinishedCall] = []
    for call in calls:
        await emit(ToolExecutionStartEvent(tool_call_id=call.id, tool_name=call.name, arguments=call.arguments))
        outcome = await prepare_tool_call(call, tools, assistant, config)
        if isinstance(outcome, FinishedCall):  # refused: it ends now, before later calls start
            await emit(ToolExecutionEndEvent(result=outcome.message))
        prepared.append(outcome)

    async def run_one(p: PreparedCall) -> FinishedCall:
        """Execute and finalize one cleared call, reporting its end as soon as it finishes."""
        result = await finalize_tool_call(p, await execute_prepared(p, ctx, emit), assistant, config)
        await emit(ToolExecutionEndEvent(result=result.message))
        return result

    # TS: like `Promise.all`, but if the run is cancelled every running tool is cancelled too.
    # The calls never raise (failures are results), so one tool can't take its siblings down.
    tasks: dict[int, asyncio.Task[FinishedCall]] = {}
    async with asyncio.TaskGroup() as group:
        for index, outcome in enumerate(prepared):
            if isinstance(outcome, PreparedCall):
                tasks[index] = group.create_task(run_one(outcome))

    # Results go back in the model's call order, whatever order the tools finished in.
    results = [
        outcome if isinstance(outcome, FinishedCall) else tasks[i].result() for i, outcome in enumerate(prepared)
    ]
    for result in results:
        await emit_result_message(result.message, emit)
    return results


async def emit_result_message(result: ToolResultMessage, emit: Emit) -> None:
    """Report a tool result as a message: the `message_end` is what callers save."""
    await emit(MessageStartEvent(message=result))
    await emit(MessageEndEvent(message=result))


@dataclass(frozen=True)
class PreparedCall:
    """A call that passed every check and is cleared to run."""

    call: ToolCall
    tool: Tool
    args: BaseModel


async def execute_tool_call(
    call: ToolCall,
    tools: Sequence[Tool],
    ctx: ToolContext,
    assistant: AssistantMessage,
    config: LoopConfig,
    emit: Emit,
) -> FinishedCall:
    """Run one tool call through prepare, execute and finalize, like pi's `runToolCall`. Nothing raises."""
    prepared = await prepare_tool_call(call, tools, assistant, config)
    if isinstance(prepared, FinishedCall):  # TS: like `if (prepared.kind === "immediate")`
        return prepared
    result = await execute_prepared(prepared, ctx, emit)
    return await finalize_tool_call(prepared, result, assistant, config)


async def prepare_tool_call(
    call: ToolCall, tools: Sequence[Tool], assistant: AssistantMessage, config: LoopConfig
) -> PreparedCall | FinishedCall:
    """Find the tool, fix up and validate the arguments, then ask `before_tool_call`. Any failure is an error result."""
    tool = next((t for t in tools if t.name == call.name), None)  # TS: tools.find((t) => t.name === call.name) ?? null
    if tool is None:
        return FinishedCall(error_result(call, f"Tool {call.name} not found."))
    if call.arguments_error:
        return FinishedCall(error_result(call, f"Invalid arguments for {call.name}: {call.arguments_error}"))
    # A copy, so a tool's fix-ups never rewrite the call stored in the assistant message.
    # TS: copy.deepcopy is structuredClone.
    try:
        raw = tool.prepare_arguments(copy.deepcopy(call.arguments))
    except Exception as exc:
        return FinishedCall(error_result(call, f"Invalid arguments for {call.name}: {type(exc).__name__}: {exc}"))
    # TS: `except X` catches only X, like `catch (e) { if (!(e instanceof X)) throw e; ... }`.
    try:
        args = tool.params.model_validate(raw)  # TS: schema.parse(raw)
    except ValidationError as exc:
        return FinishedCall(error_result(call, f"Invalid arguments for {call.name}:\n{exc}"))
    if config.before_tool_call is not None:
        # A hook that crashes must not wave the call through, so the call is blocked (pi does the same).
        # TS: unlike `catch`, `except Exception` lets cancellation (asyncio.CancelledError) through, so a
        # run cancelled while the hook waits for approval stops there instead of turning into a result.
        try:
            decision = await config.before_tool_call(BeforeToolCallContext(assistant, call, args))
        except Exception as exc:
            text = f"Tool call blocked: before_tool_call failed: {type(exc).__name__}: {exc}"
            return FinishedCall(error_result(call, text))
        if decision is not None and decision.block:
            message = error_result(call, decision.reason or "Tool execution was blocked.")
            return FinishedCall(message, terminate=decision.terminate)
    return PreparedCall(call=call, tool=tool, args=args)


async def execute_prepared(prepared: PreparedCall, ctx: ToolContext, emit: Emit) -> ToolResult:
    """Run the tool, passing its progress on as update events. A raised error becomes an error result."""
    accepting = True

    async def on_update(partial: ToolResult) -> None:
        """Report partial output; ignored once the call has finished, like pi's update gate."""
        if accepting:
            event = ToolExecutionUpdateEvent(
                tool_call_id=prepared.call.id, tool_name=prepared.call.name, partial=partial
            )
            await emit(event)

    # TS: dataclasses.replace is `{ ...ctx, onUpdate }` for a frozen dataclass: a copy with one field changed.
    call_ctx = dataclasses.replace(ctx, on_update=on_update)
    try:
        return await prepared.tool.execute(prepared.args, call_ctx)
    except ToolError as exc:
        return ToolResult(content=[TextContent(text=str(exc))], is_error=True)
    except Exception as exc:  # A tool bug must not end the run.
        return ToolResult(content=[TextContent(text=f"{type(exc).__name__}: {exc}")], is_error=True)
    finally:
        # A tool may keep the callback (a timer, a task it forgot to stop); later updates must not follow the end.
        accepting = False


async def finalize_tool_call(
    prepared: PreparedCall, result: ToolResult, assistant: AssistantMessage, config: LoopConfig
) -> FinishedCall:
    """Let `after_tool_call` replace fields of the result, then wrap it as the message the model reads."""
    content, details, is_error, terminate = result.content, result.details, result.is_error, result.terminate
    if config.after_tool_call is not None:
        hook_ctx = AfterToolCallContext(assistant, prepared.call, prepared.args, content, details, is_error)
        try:
            override = await config.after_tool_call(hook_ctx)
        except Exception as exc:
            # The tool already ran, but its result can't be trusted unfiltered, so the model is told it failed.
            return FinishedCall(error_result(prepared.call, f"after_tool_call failed: {type(exc).__name__}: {exc}"))
        if override is not None:
            # `is not None`, not `or`: an empty list or dict is a real replacement, not "keep the old value".
            content = override.content if override.content is not None else content
            details = override.details if override.details is not None else details
            is_error = override.is_error if override.is_error is not None else is_error
            terminate = override.terminate if override.terminate is not None else terminate
    message = ToolResultMessage(
        tool_call_id=prepared.call.id, tool_name=prepared.call.name, content=content, details=details, is_error=is_error
    )
    return FinishedCall(message, terminate=terminate)


def error_result(call: ToolCall, text: str) -> ToolResultMessage:
    """A failed tool result carrying `text` as the explanation for the model."""
    return ToolResultMessage(tool_call_id=call.id, tool_name=call.name, content=[TextContent(text=text)], is_error=True)
