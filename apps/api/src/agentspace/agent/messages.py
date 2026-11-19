"""Messages the app keeps in a transcript, and how they become what the model reads (ADR-008).

A transcript holds `AgentMessage`s: the three LLM messages plus the app's own
roles, which exist for the person reading the chat. Before every model call,
`convert_to_llm` turns the transcript into plain `Message`s, after pi's
`AgentMessage` and `convertToLlm` (`coding-agent/src/core/messages.ts`).
"""

from typing import Annotated, Literal

from pydantic import BaseModel, Field

from agentspace.ai.messages import AssistantMessage, Message, ToolResultMessage, UserMessage, now_ms


class NoticeMessage(BaseModel):
    """A line in the chat for the person using it, such as a model switch; never sent to the model."""

    role: Literal["notice"] = "notice"
    text: str
    timestamp: int = Field(default_factory=now_ms)


# Any message a transcript can hold; `role` tells them apart when parsing JSON.
# TS: type AgentMessage = Message | NoticeMessage, one flat union narrowed by `role`.
type AgentMessage = Annotated[
    UserMessage | AssistantMessage | ToolResultMessage | NoticeMessage, Field(discriminator="role")
]


def convert_to_llm(messages: list[AgentMessage]) -> list[Message]:
    """The default converter: keep the messages the model understands, drop the app's own."""
    # TS: messages.filter((m) => m.role !== "notice"), with the type narrowed to Message
    return [m for m in messages if isinstance(m, UserMessage | AssistantMessage | ToolResultMessage)]
