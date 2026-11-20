"""Extension points and settings of the agent loop, after pi's `AgentLoopConfig` (`packages/agent/src/types.ts`).

ADR-003 step 7: `before_tool_call` can block a call, `after_tool_call` can rewrite
its result. Step 12: `finish_turn` can end the run or ask for one more turn.
Step 14: `get_steering_messages` / `get_follow_up_messages` feed messages the user
queued while the run was going. Step 16: `convert_to_llm` turns the transcript into
what the model reads (ADR-008).
All are optional and async; leaving one out means "no change".
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel

from agentspace.agent.messages import AgentMessage, convert_to_llm
from agentspace.agent.tool import ExecutionMode
from agentspace.ai.messages import AssistantMessage, Message, ToolCall, ToolResultMessage, UserContent


@dataclass(frozen=True)
class BeforeToolCallContext:
    """What `before_tool_call` sees: a call whose arguments already passed validation."""

    assistant_message: AssistantMessage
    """The reply that made the call."""
    tool_call: ToolCall
    """The call as the model sent it; `arguments` is the raw JSON."""
    args: BaseModel
    """The validated arguments the tool will get. Changing a field changes what runs, without revalidation."""


@dataclass(frozen=True)
class BeforeToolCallResult:
    """Return `block=True` to skip the tool; the model gets `reason` as an error result."""

    block: bool = False
    reason: str | None = None
    terminate: bool = False
    """With `block`, also ask the run to stop after this batch, as `ToolResult.terminate` does."""


@dataclass(frozen=True)
class AfterToolCallContext:
    """What `after_tool_call` sees: the call and the result the tool returned."""

    assistant_message: AssistantMessage
    tool_call: ToolCall
    args: BaseModel
    content: list[UserContent]
    details: dict[str, Any] | None
    is_error: bool


@dataclass(frozen=True)
class AfterToolCallResult:
    """Fields to replace on the result. None keeps the tool's own value; anything else, even `[]`, replaces it."""

    content: list[UserContent] | None = None
    details: dict[str, Any] | None = None
    is_error: bool | None = None
    terminate: bool | None = None


@dataclass(frozen=True)
class FinishTurnContext:
    """What `finish_turn` sees: the turn that just ended, after its tool results were added."""

    turn: int
    """1 for the first turn of the run; `max_turns` still caps it."""
    message: AssistantMessage
    """The model's reply this turn."""
    tool_results: list[ToolResultMessage]
    new_messages: list[AgentMessage]
    """Everything this run has added so far, starting with the prompt."""


# "end" stops the run now; "continue" asks for at least one more turn, even after a final answer or a
# terminating batch. None keeps the normal rule (go on while the model calls tools). After pi's `AgentTurnDecision`.
type TurnDecision = Literal["end", "continue"]


# TS: type BeforeToolCall = (ctx: BeforeToolCallContext) => Promise<BeforeToolCallResult | undefined>
type BeforeToolCall = Callable[[BeforeToolCallContext], Awaitable[BeforeToolCallResult | None]]
type AfterToolCall = Callable[[AfterToolCallContext], Awaitable[AfterToolCallResult | None]]
type FinishTurn = Callable[[FinishTurnContext], Awaitable[TurnDecision | None]]
# Hands over queued messages (possibly none); the loop calls it at fixed points, like pi's `getSteeringMessages`.
type GetMessages = Callable[[], Awaitable[list[AgentMessage]]]

# The transcript as the app keeps it in, the messages the model gets out.
# TS: type ConvertToLlm = (messages: AgentMessage[]) => Message[]
type ConvertToLlm = Callable[[list[AgentMessage]], list[Message]]


@dataclass(frozen=True)
class LoopConfig:
    """Optional hooks and settings for one run, kept on one object so `run_agent` doesn't grow a parameter each."""

    tool_execution: ExecutionMode = "parallel"
    """How a reply's tool calls run (ADR-003 step 9). A tool with `execution_mode="sequential"` overrides it."""

    before_tool_call: BeforeToolCall | None = None
    """Runs after validation, before execution. Raising blocks the call too (fail closed)."""
    after_tool_call: AfterToolCall | None = None
    """Runs after execution, unless the call was blocked. Raising replaces the real result with an error."""
    finish_turn: FinishTurn | None = None
    """Runs after each turn, before `turn_end`. Ignored after a provider error or abort, which always end the run.
    Raising ends the run with that error, like any other loop failure."""
    get_steering_messages: GetMessages | None = None
    """Polled when the run starts and after each turn; what it returns goes in before the next model call."""
    get_follow_up_messages: GetMessages | None = None
    """Polled once the run would otherwise stop; anything it returns starts another turn."""
    convert_to_llm: ConvertToLlm = convert_to_llm
    """Turns the transcript into the messages the model gets, before every model call. Plain, not async;
    raising fails the run (ADR-008). The default drops the app's own messages."""
