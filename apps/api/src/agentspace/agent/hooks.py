"""Extension points and settings of the agent loop, after pi's `AgentLoopConfig` (`packages/agent/src/types.ts`).

ADR-003 step 7: `before_tool_call` can block a call, `after_tool_call` can rewrite
its result. Both are optional and async; leaving one out means "no change".
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from agentspace.agent.tool import ExecutionMode
from agentspace.ai.messages import AssistantMessage, TextContent, ToolCall


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


@dataclass(frozen=True)
class AfterToolCallContext:
    """What `after_tool_call` sees: the call and the result the tool returned."""

    assistant_message: AssistantMessage
    tool_call: ToolCall
    args: BaseModel
    content: list[TextContent]
    details: dict[str, Any] | None
    is_error: bool


@dataclass(frozen=True)
class AfterToolCallResult:
    """Fields to replace on the result. None keeps the tool's own value; anything else, even `[]`, replaces it."""

    content: list[TextContent] | None = None
    details: dict[str, Any] | None = None
    is_error: bool | None = None


# TS: type BeforeToolCall = (ctx: BeforeToolCallContext) => Promise<BeforeToolCallResult | undefined>
type BeforeToolCall = Callable[[BeforeToolCallContext], Awaitable[BeforeToolCallResult | None]]
type AfterToolCall = Callable[[AfterToolCallContext], Awaitable[AfterToolCallResult | None]]


@dataclass(frozen=True)
class LoopConfig:
    """Optional hooks and settings for one run, kept on one object so `run_agent` doesn't grow a parameter each."""

    tool_execution: ExecutionMode = "parallel"
    """How a reply's tool calls run (ADR-003 step 9). A tool with `execution_mode="sequential"` overrides it."""

    before_tool_call: BeforeToolCall | None = None
    """Runs after validation, before execution. Raising blocks the call too (fail closed)."""
    after_tool_call: AfterToolCall | None = None
    """Runs after execution, unless the call was blocked. Raising replaces the real result with an error."""
