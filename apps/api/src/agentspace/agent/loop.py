"""The agent loop: ask the model, run the tools it calls, repeat until it answers without tools.

A simplified port of pi's `runLoop` (`packages/agent/src/agent-loop.ts`): tools
run one at a time, and there are no steering/follow-up queues or hooks yet.
The loop holds no storage; callers persist messages from `message_end` events.
Cancelling the consuming task stops the run at its current await.
"""

from collections.abc import AsyncIterator, Sequence

from pydantic import ValidationError

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
from agentspace.agent.tool import Tool, ToolContext, ToolError
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
) -> AsyncIterator[AgentEvent]:
    """Run the agent on one prompt and yield its events until it answers, fails or hits `max_turns`."""
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
                result = await execute_tool_call(call, tools, ctx)
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


async def execute_tool_call(call: ToolCall, tools: Sequence[Tool], ctx: ToolContext) -> ToolResultMessage:
    """Run one tool call. Every failure becomes an error result for the model; nothing raises."""
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
    try:
        text = await tool.execute(args, ctx)
    except ToolError as exc:
        return error_result(call, str(exc))
    except Exception as exc:  # A tool bug must not end the run; the model sees it and can adapt.
        return error_result(call, f"{type(exc).__name__}: {exc}")
    return ToolResultMessage(tool_call_id=call.id, tool_name=call.name, content=[TextContent(text=text)])


def error_result(call: ToolCall, text: str) -> ToolResultMessage:
    """A failed tool result carrying `text` as the explanation for the model."""
    return ToolResultMessage(tool_call_id=call.id, tool_name=call.name, content=[TextContent(text=text)], is_error=True)
