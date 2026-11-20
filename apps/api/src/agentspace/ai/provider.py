"""The provider interface: one streaming call that every model API implements.

A provider turns `(model, Context)` into a stream of `AssistantEvent`s that ends
with exactly one `DoneEvent` or `ErrorEvent`. It never raises for request or
response failures: those end the stream with an `ErrorEvent` whose message has
`stop_reason="error"`. Cancelling the consuming task cancels the request.
Modeled on pi's `AssistantMessageEvent` stream (`packages/ai/src/types.ts`).
"""

from collections.abc import AsyncIterator
from typing import Any, Literal, Protocol

from pydantic import BaseModel

from agentspace.ai.messages import (
    AssistantMessage,
    Message,
    TextContent,
    ThinkingLevel,
    ToolCall,
    ToolResultMessage,
)


class ToolSpec(BaseModel):
    """What the model is told about a tool."""

    name: str
    description: str
    parameters: dict[str, Any]
    """JSON Schema of the arguments object."""


class Context(BaseModel):
    """Everything one model request needs: prompt, transcript, tools and limits."""

    system_prompt: str = ""
    messages: list[Message] = []  # TS: Message[]
    tools: list[ToolSpec] = []
    max_tokens: int | None = None
    """Cap on output tokens. Without one, some APIs reserve the model's maximum and refuse low-credit keys."""
    thinking_level: ThinkingLevel = "off"
    """How hard a reasoning model should think; "off" sends nothing, so the model's default applies."""


class StartEvent(BaseModel):
    """The provider accepted the request and the reply is starting."""

    type: Literal["start"] = "start"


class TextDeltaEvent(BaseModel):
    """A new piece of reply text."""

    type: Literal["text_delta"] = "text_delta"
    delta: str


class ThinkingDeltaEvent(BaseModel):
    """A new piece of the model's reasoning, streamed before its answer."""

    type: Literal["thinking_delta"] = "thinking_delta"
    delta: str


class ToolCallEndEvent(BaseModel):
    """A tool call whose arguments finished streaming."""

    type: Literal["tool_call_end"] = "tool_call_end"
    tool_call: ToolCall


class DoneEvent(BaseModel):
    """The reply finished normally; carries the complete message."""

    type: Literal["done"] = "done"
    message: AssistantMessage


class ErrorEvent(BaseModel):
    """The request failed; carries the partial message with `stop_reason` error."""

    type: Literal["error"] = "error"
    message: AssistantMessage


# What a provider stream yields; the last item is always a DoneEvent or an ErrorEvent.
type AssistantEvent = StartEvent | ThinkingDeltaEvent | TextDeltaEvent | ToolCallEndEvent | DoneEvent | ErrorEvent


class Provider(Protocol):
    """A model API adapter. Implementations live in `agentspace.ai.providers`."""

    # TS: like an interface: any class with these members fits; no `implements` needed.
    name: str

    def stream(self, model: str, context: Context) -> AsyncIterator[AssistantEvent]:
        """Send one request and yield the reply as it streams; never raises for API failures."""
        # TS: implementations use `async def` + `yield`, i.e. an `async function*` returning AsyncIterable.
        ...


# Stand-in result for a tool call whose run was cancelled before it returned.
INTERRUPTED_TOOL_RESULT = "Tool call was interrupted before it returned a result."


def prepare_transcript(messages: list[Message]) -> list[Message]:
    """Make a stored transcript valid to send to a model.

    - Assistant messages that ended in `error` or `aborted` are dropped: they
      can hold partial text or half-streamed tool calls.
    - Every tool call gets a result before the next message. Model APIs reject
      a call without one, which happens when a run is cancelled between the
      assistant message and its tool results; an error result stands in.
    - A tool result whose call is not pending is dropped.
    """
    repaired: list[Message] = []
    pending: list[ToolCall] = []

    # TS: a nested function closing over `pending` and `repaired`, like an inner arrow function.
    def flush() -> None:
        """Close every still-pending call with an error result."""
        for call in pending:
            repaired.append(
                ToolResultMessage(
                    tool_call_id=call.id,
                    tool_name=call.name,
                    content=[TextContent(text=INTERRUPTED_TOOL_RESULT)],
                    is_error=True,
                )
            )
        pending.clear()

    for message in messages:
        # TS: isinstance(x, Cls) is `x instanceof Cls`, and narrows the type the same way.
        if isinstance(message, ToolResultMessage):
            # Keep a result only if it answers a call we are still waiting on.
            if any(call.id == message.tool_call_id for call in pending):
                # TS: `pending[:] = ...` replaces the contents in place, so `flush` keeps seeing the same list.
                pending[:] = [call for call in pending if call.id != message.tool_call_id]
                repaired.append(message)
            continue
        # Any other message ends the previous reply's tool round.
        flush()
        if isinstance(message, AssistantMessage):
            if message.stop_reason in ("error", "aborted"):
                continue
            pending.extend(message.tool_calls())
        repaired.append(message)
    flush()
    return repaired
