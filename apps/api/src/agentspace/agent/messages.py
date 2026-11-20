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


class CompactionSummaryMessage(BaseModel):
    """A summary standing in for the older part of the transcript, so it fits the model's window (ADR-009)."""

    role: Literal["compaction_summary"] = "compaction_summary"
    summary: str
    kept: int
    """How many transcript positions directly before this message the model still reads as they are."""
    tokens_before: int
    """About how many tokens the model's context held when this was made; for the UI."""
    timestamp: int = Field(default_factory=now_ms)


# Any message a transcript can hold; `role` tells them apart when parsing JSON.
# TS: type AgentMessage = Message | NoticeMessage | ..., one flat union narrowed by `role`.
type AgentMessage = Annotated[
    UserMessage | AssistantMessage | ToolResultMessage | NoticeMessage | CompactionSummaryMessage,
    Field(discriminator="role"),
]

# Wraps a summary for the model, so it reads as background rather than as the user's request.
SUMMARY_PREFIX = "The conversation before this point was compacted into this summary:\n\n<summary>\n"
SUMMARY_SUFFIX = "\n</summary>"


def convert_to_llm(messages: list[AgentMessage]) -> list[Message]:
    """The default converter: summaries become user messages, notices are dropped, the rest pass through."""
    out: list[Message] = []
    for m in messages:
        # TS: `match` on classes is a switch over `instanceof` checks.
        match m:
            case CompactionSummaryMessage():
                out.append(UserMessage(content=SUMMARY_PREFIX + m.summary + SUMMARY_SUFFIX, timestamp=m.timestamp))
            case UserMessage() | AssistantMessage() | ToolResultMessage():
                out.append(m)
            case NoticeMessage():
                pass
    return out
