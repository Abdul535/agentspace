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


class ImageContent(BaseModel):
    """An image in a user message or tool result, inline as base64 (ADR-015)."""

    type: Literal["image"] = "image"
    data: str
    """Base64 of the image bytes, without a `data:` prefix."""
    mime_type: str
    """One of `image/png`, `image/jpeg`, `image/gif`, `image/webp`."""


# What a user message or a tool result can hold; `type` tells the blocks apart when parsing JSON.
# TS: (TextContent | ImageContent), narrowed by `type`.
type UserContent = Annotated[TextContent | ImageContent, Field(discriminator="type")]


# Stands in for each image when the model can't see images (ADR-015), after pi's `blockImages`.
IMAGE_NOT_SHOWN = "[Image not shown: this model can't see images.]"


def joined_text(content: str | list[UserContent]) -> str:
    """The text blocks of `content` joined, leaving images out; a plain string is returned as is."""
    if isinstance(content, str):
        return content
    return "".join(block.text for block in content if isinstance(block, TextContent))


def images_of(content: str | list[UserContent]) -> list[ImageContent]:
    """The image blocks of `content`, in order; none for a plain string."""
    if isinstance(content, str):
        return []
    return [block for block in content if isinstance(block, ImageContent)]


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
    content: str | list[UserContent]
    """Plain text, or text and image blocks (ADR-015); old rows and text-only prompts stay a string."""
    timestamp: int = Field(default_factory=now_ms)  # TS: computed per instance, like `timestamp = Date.now()`

    def text(self) -> str:
        """The prompt's text without its images, for titles and summaries."""
        return joined_text(self.content)


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
    content: list[UserContent]
    details: dict[str, Any] | None = None
    """The tool's structured facts for the UI; providers never send this to the model."""
    is_error: bool = False
    timestamp: int = Field(default_factory=now_ms)


# Any message in a transcript; `role` tells them apart when parsing JSON.
# TS: type Message = UserMessage | AssistantMessage | ToolResultMessage, narrowed by `role`.
type Message = Annotated[UserMessage | AssistantMessage | ToolResultMessage, Field(discriminator="role")]


def strip_images(messages: list[Message]) -> list[Message]:
    """The messages with every image swapped for a short note; the stored transcript keeps the images (ADR-015)."""
    out: list[Message] = []
    for m in messages:
        if isinstance(m, UserMessage | ToolResultMessage) and images_of(m.content):
            # TS: like `{ ...m, content: ... }`: a copy with one field changed; the stored message is untouched.
            out.append(m.model_copy(update={"content": swap_images(m.content)}))
        else:
            out.append(m)
    return out


def swap_images(content: str | list[UserContent]) -> list[UserContent]:
    """`content` as blocks, each image replaced by `IMAGE_NOT_SHOWN`."""
    blocks: list[UserContent] = [TextContent(text=content)] if isinstance(content, str) else content
    return [TextContent(text=IMAGE_NOT_SHOWN) if isinstance(b, ImageContent) else b for b in blocks]
