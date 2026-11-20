"""Provider-neutral conversation messages.

Every provider adapter converts these to and from its own wire format, so the
agent loop, the tools and the session store never see provider details.
Modeled on pi's `packages/ai/src/types.ts`.
"""

import time
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field

# TS: type StopReason = "stop" | "length" | "tool_use" | "error" | "aborted"
type StopReason = Literal["stop", "length", "tool_use", "error", "aborted"]
"""Why an assistant message ended.

- `stop`: the model finished its answer.
- `length`: the output token limit cut the message off.
- `tool_use`: the model is waiting for tool results.
- `error`: the provider request failed; see `error_message`.
- `aborted`: the run was cancelled mid-stream.
"""

# How hard a reasoning model thinks before it answers (ADR-012); "off" asks for nothing, so the model's default applies.
type ThinkingLevel = Literal["off", "minimal", "low", "medium", "high"]


def now_ms() -> int:
    """Current time as Unix milliseconds, the timestamp unit every message uses."""
    return int(time.time() * 1000)  # TS: Date.now(); time.time() counts seconds, not ms


# TS: `class X(BaseModel)` is a zod schema and its inferred type in one; data is validated when created/parsed.
class TextContent(BaseModel):
    """A block of plain text in a message."""

    type: Literal["text"] = "text"  # TS: type: "text" — a fixed tag that is also the default
    text: str


class ThinkingContent(BaseModel):
    """The model's reasoning before its answer; shown in the UI and sent back to the same model (ADR-012)."""

    type: Literal["thinking"] = "thinking"
    thinking: str
    """The reasoning text; empty when the provider sent only encrypted reasoning."""
    signature: str | None = None
    """What sending it back needs: the wire field the text came in, or the provider's reasoning details as JSON."""


class ToolCall(BaseModel):
    """The model asking to run one tool with JSON arguments."""

    type: Literal["tool_call"] = "tool_call"
    id: str
    name: str
    arguments: dict[str, Any] = {}  # TS: Record<string, unknown>; Pydantic copies the default per instance
    arguments_error: str | None = None
    """Set when the model's arguments were not a valid JSON object; `arguments` is then `{}`."""


class Usage(BaseModel):
    """Token counts the provider reported for one model reply."""

    input: int = 0
    output: int = 0
    total: int = 0
    reasoning: int = 0
    """Thinking tokens; already counted in `output`, not on top of it."""


class UserMessage(BaseModel):
    """A prompt from the person using the session."""

    role: Literal["user"] = "user"
    content: str
    timestamp: int = Field(default_factory=now_ms)  # TS: computed per instance, like `timestamp = Date.now()`


class AssistantMessage(BaseModel):
    """One model reply: thinking, text and tool calls, plus how and why it ended."""

    role: Literal["assistant"] = "assistant"
    # TS: (ThinkingContent | TextContent | ToolCall)[], a discriminated union narrowed by `type`.
    content: list[Annotated[ThinkingContent | TextContent | ToolCall, Field(discriminator="type")]] = []
    provider: str
    model: str
    usage: Usage = Usage()
    stop_reason: StopReason = "stop"
    error_message: str | None = None  # TS: errorMessage: string | null = null
    timestamp: int = Field(default_factory=now_ms)

    # TS: `self` is `this`, written out as the first parameter of every method.
    def text(self) -> str:
        """All text blocks joined, without thinking or tool calls."""
        # TS: this.content.filter((b) => b instanceof TextContent).map((b) => b.text).join("")
        return "".join(block.text for block in self.content if isinstance(block, TextContent))

    def thinking(self) -> str:
        """All thinking blocks joined; kept out of `text()` so summaries and titles see only the answer."""
        return "".join(block.thinking for block in self.content if isinstance(block, ThinkingContent))

    def tool_calls(self) -> list[ToolCall]:
        """The tool calls in this reply, in the order the model made them."""
        # TS: a list comprehension, like this.content.filter((b) => b instanceof ToolCall)
        return [block for block in self.content if isinstance(block, ToolCall)]


class ToolResultMessage(BaseModel):
    """What one tool call returned, sent back to the model as the next message."""

    role: Literal["tool_result"] = "tool_result"
    tool_call_id: str
    tool_name: str
    content: list[TextContent]
    details: dict[str, Any] | None = None
    """The tool's structured facts for the UI; providers never send this to the model."""
    is_error: bool = False
    timestamp: int = Field(default_factory=now_ms)


# Any message in a transcript; `role` tells them apart when parsing JSON.
# TS: type Message = UserMessage | AssistantMessage | ToolResultMessage, narrowed by `role`.
type Message = Annotated[UserMessage | AssistantMessage | ToolResultMessage, Field(discriminator="role")]
