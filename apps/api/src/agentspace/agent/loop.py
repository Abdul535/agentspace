"""The agent loop: ask the model, run the tools it calls, repeat until it answers without tools.

A simplified port of pi's `runLoop` (`packages/agent/src/agent-loop.ts`): a
reply's tool calls run in parallel or one at a time (`LoopConfig.tool_execution`),
each through prepare, execute and finalize with the `LoopConfig` hooks, and there
are no steering/follow-up queues yet.
The loop holds no storage; callers persist messages from `message_end` events.

`run_loop` reports events through an `emit` callback, so any code holding it can
report one (ADR-006). `run_agent` wraps it as an async generator for the SSE
route; closing or cancelling that generator stops the run at its current await.
"""

import asyncio
import contextlib
import copy
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
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
    TurnEndEvent,
    TurnStartEvent,
)
from agentspace.agent.hooks import AfterToolCallContext, BeforeToolCallContext, LoopConfig
from agentspace.agent.tool import Tool, ToolContext, ToolError, ToolResult
from agentspace.ai.messages import AssistantMessage, Message, TextContent, ToolCall, ToolResultMessage, UserMessage
from agentspace.ai.provider import Context, DoneEvent, ErrorEvent, Provider, TextDeltaEvent, ToolCallEndEvent

# Tool result for calls in a reply the token limit cut off; their arguments may be incomplete.
TRUNCATED_CALL = (
    'Tool call "{name}" was not executed: the response hit the output token limit, '
    "so its arguments may be truncated. Re-issue the tool call with complete arguments."
)


# Receives every event the loop reports, in order; awaited, like pi's `AgentEventSink`.
# TS: type Emit = (event: AgentEvent) => Promise<void>
type Emit = Callable[[AgentEvent], Awaitable[None]]


# TS: an `async function*` taking one options object (`*` = keyword-only); callers `for await` its events.
async def run_agent(
    *,
    provider: Provider,
    model: str,
    system_prompt: str,
    tools: Sequence[Tool],
    history: Sequence[Message],
    prompt: UserMessage,
    ctx: ToolContext,
    max_turns: int,
    max_tokens: int | None = None,
    config: LoopConfig | None = None,
) -> AsyncIterator[AgentEvent]:
    """Run the agent on one prompt and yield its events: `run_loop` in a task, like pi's `agentLoop`."""
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
    history: Sequence[Message],
    prompt: UserMessage,
    ctx: ToolContext,
    max_turns: int,
    max_tokens: int | None = None,
    config: LoopConfig | None = None,
    emit: Emit,
) -> None:
    """Run the agent on one prompt until it answers, fails or hits `max_turns`, passing each event to `emit`."""
    # TS: `or` here works like `??`: a LoopConfig is always truthy, so only None falls through.
    config = config or LoopConfig()
    messages: list[Message] = list(history)
    new_messages: list[Message] = []
    tool_specs = [tool.spec() for tool in tools]

    # TS: an inner function closing over `messages` and `new_messages`.
    def add(message: Message) -> None:
        """Append to the context sent to the model and to the list this run returns."""
        messages.append(message)
        new_messages.append(message)

    await emit(AgentStartEvent())
    await emit(MessageStartEvent(message=prompt))
    add(prompt)
    await emit(MessageEndEvent(message=prompt))

    reason: EndReason = "max_turns"
    for turn in range(1, max_turns + 1):  # TS: for (let turn = 1; turn <= maxTurns; turn++)
        await emit(TurnStartEvent(turn=turn))

        # Stream the model's reply, forwarding text and tool-call pieces as they arrive.
        assistant: AssistantMessage | None = None
        await emit(MessageStartEvent(message=AssistantMessage(provider=provider.name, model=model)))
        context = Context(system_prompt=system_prompt, messages=messages, tools=tool_specs, max_tokens=max_tokens)
        # TS: for await (const event of provider.stream(...)), then a switch on the event's class.
        async for event in provider.stream(model, context):
            match event:
                case TextDeltaEvent() | ToolCallEndEvent():
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
            await emit(TurnEndEvent(turn=turn, message=assistant, tool_results=[]))
            reason = "error" if assistant.stop_reason == "error" else "aborted"
            break

        calls = assistant.tool_calls()
        results = await execute_tool_calls(calls, tools, ctx, assistant, config, emit)
        for result in results:
            add(result)
        await emit(TurnEndEvent(turn=turn, message=assistant, tool_results=results))

        # A reply without tool calls is the final answer.
        if not calls:
            reason = "done"
            break

    await emit(AgentEndEvent(reason=reason, new_messages=new_messages))


async def execute_tool_calls(
    calls: Sequence[ToolCall],
    tools: Sequence[Tool],
    ctx: ToolContext,
    assistant: AssistantMessage,
    config: LoopConfig,
    emit: Emit,
) -> list[ToolResultMessage]:
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
) -> list[ToolResultMessage]:
    """Each call starts, runs and reports its result before the next one starts."""
    results: list[ToolResultMessage] = []
    for call in calls:
        await emit(ToolExecutionStartEvent(tool_call_id=call.id, tool_name=call.name, arguments=call.arguments))
        if assistant.stop_reason == "length":
            result = error_result(call, TRUNCATED_CALL.format(name=call.name))
        else:
            result = await execute_tool_call(call, tools, ctx, assistant, config)
        await emit(ToolExecutionEndEvent(result=result))
        await emit_result_message(result, emit)
        results.append(result)
    return results


async def run_in_parallel(
    calls: Sequence[ToolCall],
    tools: Sequence[Tool],
    ctx: ToolContext,
    assistant: AssistantMessage,
    config: LoopConfig,
    emit: Emit,
) -> list[ToolResultMessage]:
    """Prepare the calls in order, run the cleared ones at the same time, then report results in call order.

    Preparing in order keeps `before_tool_call` approvals one at a time, and no tool starts
    until every call has been cleared or refused, as in pi's `executeToolCallsParallel`.
    """
    prepared: list[PreparedCall | ToolResultMessage] = []
    for call in calls:
        await emit(ToolExecutionStartEvent(tool_call_id=call.id, tool_name=call.name, arguments=call.arguments))
        outcome = await prepare_tool_call(call, tools, assistant, config)
        if isinstance(outcome, ToolResultMessage):  # refused: it ends now, before later calls start
            await emit(ToolExecutionEndEvent(result=outcome))
        prepared.append(outcome)

    async def run_one(p: PreparedCall) -> ToolResultMessage:
        """Execute and finalize one cleared call, reporting its end as soon as it finishes."""
        result = await finalize_tool_call(p, await execute_prepared(p, ctx), assistant, config)
        await emit(ToolExecutionEndEvent(result=result))
        return result

    # TS: like `Promise.all`, but if the run is cancelled every running tool is cancelled too.
    # The calls never raise (failures are results), so one tool can't take its siblings down.
    tasks: dict[int, asyncio.Task[ToolResultMessage]] = {}
    async with asyncio.TaskGroup() as group:
        for index, outcome in enumerate(prepared):
            if isinstance(outcome, PreparedCall):
                tasks[index] = group.create_task(run_one(outcome))

    # Results go back in the model's call order, whatever order the tools finished in.
    results = [
        outcome if isinstance(outcome, ToolResultMessage) else tasks[i].result() for i, outcome in enumerate(prepared)
    ]
    for result in results:
        await emit_result_message(result, emit)
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
    call: ToolCall, tools: Sequence[Tool], ctx: ToolContext, assistant: AssistantMessage, config: LoopConfig
) -> ToolResultMessage:
    """Run one tool call through prepare, execute and finalize, like pi's `runToolCall`. Nothing raises."""
    prepared = await prepare_tool_call(call, tools, assistant, config)
    if isinstance(prepared, ToolResultMessage):  # TS: like `if (prepared.kind === "immediate")`
        return prepared
    result = await execute_prepared(prepared, ctx)
    return await finalize_tool_call(prepared, result, assistant, config)


async def prepare_tool_call(
    call: ToolCall, tools: Sequence[Tool], assistant: AssistantMessage, config: LoopConfig
) -> PreparedCall | ToolResultMessage:
    """Find the tool, fix up and validate the arguments, then ask `before_tool_call`. Any failure is an error result."""
    tool = next((t for t in tools if t.name == call.name), None)  # TS: tools.find((t) => t.name === call.name) ?? null
    if tool is None:
        return error_result(call, f"Tool {call.name} not found.")
    if call.arguments_error:
        return error_result(call, f"Invalid arguments for {call.name}: {call.arguments_error}")
    # A copy, so a tool's fix-ups never rewrite the call stored in the assistant message.
    # TS: copy.deepcopy is structuredClone.
    try:
        raw = tool.prepare_arguments(copy.deepcopy(call.arguments))
    except Exception as exc:
        return error_result(call, f"Invalid arguments for {call.name}: {type(exc).__name__}: {exc}")
    # TS: `except X` catches only X, like `catch (e) { if (!(e instanceof X)) throw e; ... }`.
    try:
        args = tool.params.model_validate(raw)  # TS: schema.parse(raw)
    except ValidationError as exc:
        return error_result(call, f"Invalid arguments for {call.name}:\n{exc}")
    if config.before_tool_call is not None:
        # A hook that crashes must not wave the call through, so the call is blocked (pi does the same).
        # TS: unlike `catch`, `except Exception` lets cancellation (asyncio.CancelledError) through, so a
        # run cancelled while the hook waits for approval stops there instead of turning into a result.
        try:
            decision = await config.before_tool_call(BeforeToolCallContext(assistant, call, args))
        except Exception as exc:
            return error_result(call, f"Tool call blocked: before_tool_call failed: {type(exc).__name__}: {exc}")
        if decision is not None and decision.block:
            return error_result(call, decision.reason or "Tool execution was blocked.")
    return PreparedCall(call=call, tool=tool, args=args)


async def execute_prepared(prepared: PreparedCall, ctx: ToolContext) -> ToolResult:
    """Run the tool. A raised error becomes an error result, so the model sees it and can adapt."""
    try:
        return await prepared.tool.execute(prepared.args, ctx)
    except ToolError as exc:
        return ToolResult(content=[TextContent(text=str(exc))], is_error=True)
    except Exception as exc:  # A tool bug must not end the run.
        return ToolResult(content=[TextContent(text=f"{type(exc).__name__}: {exc}")], is_error=True)


async def finalize_tool_call(
    prepared: PreparedCall, result: ToolResult, assistant: AssistantMessage, config: LoopConfig
) -> ToolResultMessage:
    """Let `after_tool_call` replace fields of the result, then wrap it as the message the model reads."""
    content, details, is_error = result.content, result.details, result.is_error
    if config.after_tool_call is not None:
        hook_ctx = AfterToolCallContext(assistant, prepared.call, prepared.args, content, details, is_error)
        try:
            override = await config.after_tool_call(hook_ctx)
        except Exception as exc:
            # The tool already ran, but its result can't be trusted unfiltered, so the model is told it failed.
            return error_result(prepared.call, f"after_tool_call failed: {type(exc).__name__}: {exc}")
        if override is not None:
            # `is not None`, not `or`: an empty list or dict is a real replacement, not "keep the old value".
            content = override.content if override.content is not None else content
            details = override.details if override.details is not None else details
            is_error = override.is_error if override.is_error is not None else is_error
    return ToolResultMessage(
        tool_call_id=prepared.call.id, tool_name=prepared.call.name, content=content, details=details, is_error=is_error
    )


def error_result(call: ToolCall, text: str) -> ToolResultMessage:
    """A failed tool result carrying `text` as the explanation for the model."""
    return ToolResultMessage(tool_call_id=call.id, tool_name=call.name, content=[TextContent(text=text)], is_error=True)
