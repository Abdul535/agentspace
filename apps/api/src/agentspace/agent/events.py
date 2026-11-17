"""Events the agent loop emits. Clients render them; the session store persists every `message_end`.

Sequence for one prompt (modeled on pi's `packages/agent` event flow):

    agent_start
    message_start/end (user)
    turn_start
      message_start (assistant, empty) · message_update* · message_end (assistant)
      tool_execution_start · tool_execution_update* · tool_execution_end            per tool call
      message_start/end (tool_result)                                              per tool call, in call order
    turn_end
    ...more turns while the assistant calls tools...
    agent_end
"""

from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field

from agentspace.agent.tool import ToolResult
from agentspace.ai.messages import AssistantMessage, Message, ToolResultMessage
from agentspace.ai.provider import TextDeltaEvent, ToolCallEndEvent

# Why a run stopped: the model answered, it hit max_turns, the provider failed, or it was cancelled.
type EndReason = Literal["done", "max_turns", "error", "aborted"]


# TS: each event is a Pydantic model (zod schema + type); `type: Literal["x"]` is the tag, like `type: "x"`.
class AgentStartEvent(BaseModel):
    """A run started."""

    type: Literal["agent_start"] = "agent_start"


class TurnStartEvent(BaseModel):
    """A new turn: one model request plus the tool calls it makes."""

    type: Literal["turn_start"] = "turn_start"
    turn: int


class MessageStartEvent(BaseModel):
    """A message began; for an assistant reply the message is still empty."""

    type: Literal["message_start"] = "message_start"
    message: Message


class MessageUpdateEvent(BaseModel):
    """Assistant messages only: one streamed piece of the message being generated."""

    type: Literal["message_update"] = "message_update"
    event: Annotated[TextDeltaEvent | ToolCallEndEvent, Field(discriminator="type")]


class MessageEndEvent(BaseModel):
    """A message is complete; this is the point where it gets saved."""

    type: Literal["message_end"] = "message_end"
    message: Message


class ToolExecutionStartEvent(BaseModel):
    """A tool call started running."""

    type: Literal["tool_execution_start"] = "tool_execution_start"
    tool_call_id: str
    tool_name: str
    arguments: dict[str, Any]


class ToolExecutionUpdateEvent(BaseModel):
    """Partial output of a running tool call, e.g. bash output so far. Replaces the previous update; never saved."""

    type: Literal["tool_execution_update"] = "tool_execution_update"
    tool_call_id: str
    tool_name: str
    partial: ToolResult


class ToolExecutionEndEvent(BaseModel):
    """A tool call finished, successfully or with an error result."""

    type: Literal["tool_execution_end"] = "tool_execution_end"
    result: ToolResultMessage


class TurnEndEvent(BaseModel):
    """A turn finished: the reply and the results of its tool calls."""

    type: Literal["turn_end"] = "turn_end"
    turn: int
    message: AssistantMessage
    tool_results: list[ToolResultMessage]


class AgentEndEvent(BaseModel):
    """The run is over; lists every message it added to the transcript."""

    type: Literal["agent_end"] = "agent_end"
    reason: EndReason
    new_messages: list[Message]


# Any agent event; `type` tells them apart. This is what the API streams to clients.
# TS: type AgentEvent = AgentStartEvent | TurnStartEvent | ..., a discriminated union on `type`.
type AgentEvent = Annotated[
    AgentStartEvent
    | TurnStartEvent
    | MessageStartEvent
    | MessageUpdateEvent
    | MessageEndEvent
    | ToolExecutionStartEvent
    | ToolExecutionUpdateEvent
    | ToolExecutionEndEvent
    | TurnEndEvent
    | AgentEndEvent,
    Field(discriminator="type"),
]
