"""The agent loop: ask the model, run the tools it calls, repeat until it answers without tools.

A simplified port of pi's `runLoop` (`packages/agent/src/agent-loop.ts`): tools
run one at a time, each through prepare, execute and finalize with the
`LoopConfig` hooks, and there are no steering/follow-up queues yet.
The loop holds no storage; callers persist messages from `message_end` events.
Cancelling the consuming task stops the run at its current await.
"""

from collections.abc import AsyncIterator, Sequence
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
    """Run the agent on one prompt and yield its events until it answers, fails or hits `max_turns`."""
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

    yield AgentStartEvent()
    yield MessageStartEvent(message=prompt)
    add(prompt)
    yield MessageEndEvent(message=prompt)

    reason: EndReason = "max_turns"
    for turn in range(1, max_turns + 1):  # TS: for (let turn = 1; turn <= maxTurns; turn++)
        yield TurnStartEvent(turn=turn)

        # Stream the model's reply, forwarding text and tool-call pieces as they arrive.
        assistant: AssistantMessage | None = None
        yield MessageStartEvent(message=AssistantMessage(provider=provider.name, model=model))
        context = Context(system_prompt=system_prompt, messages=messages, tools=tool_specs, max_tokens=max_tokens)
        # TS: for await (const event of provider.stream(...)), then a switch on the event's class.
        async for event in provider.stream(model, context):
            match event:
                case TextDeltaEvent() | ToolCallEndEvent():
                    yield MessageUpdateEvent(event=event)
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
        yield MessageEndEvent(message=assistant)

        if assistant.stop_reason in ("error", "aborted"):
            yield TurnEndEvent(turn=turn, message=assistant, tool_results=[])
            reason = "error" if assistant.stop_reason == "error" else "aborted"
            break

        # Run the reply's tool calls one at a time, in the order the model made them.
        calls = assistant.tool_calls()
        results: list[ToolResultMessage] = []
        for call in calls:
            yield ToolExecutionStartEvent(tool_call_id=call.id, tool_name=call.name, arguments=call.arguments)
            if assistant.stop_reason == "length":
                result = error_result(call, TRUNCATED_CALL.format(name=call.name))
            else:
                result = await execute_tool_call(call, tools, ctx, assistant, config)
            yield ToolExecutionEndEvent(result=result)
            yield MessageStartEvent(message=result)
            add(result)
            results.append(result)
            yield MessageEndEvent(message=result)
        yield TurnEndEvent(turn=turn, message=assistant, tool_results=results)

        # A reply without tool calls is the final answer.
        if not calls:
            reason = "done"
            break

    yield AgentEndEvent(reason=reason, new_messages=new_messages)


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
    """Find the tool, validate the arguments, then ask `before_tool_call`. A failure at any check is an error result."""
    tool = next((t for t in tools if t.name == call.name), None)  # TS: tools.find((t) => t.name === call.name) ?? null
    if tool is None:
        return error_result(call, f"Tool {call.name} not found.")
    if call.arguments_error:
        return error_result(call, f"Invalid arguments for {call.name}: {call.arguments_error}")
    # TS: `except X` catches only X, like `catch (e) { if (!(e instanceof X)) throw e; ... }`.
    try:
        args = tool.params.model_validate(call.arguments)  # TS: schema.parse(call.arguments)
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
