"""A scripted provider for tests: replays queued assistant messages. Modeled on pi's `faux.ts`."""

from collections.abc import AsyncIterator, Callable

from agentspace.ai.messages import AssistantMessage, TextContent, ToolCall
from agentspace.ai.provider import (
    AssistantEvent,
    Context,
    DoneEvent,
    ErrorEvent,
    StartEvent,
    TextDeltaEvent,
    ToolCallEndEvent,
)

# A scripted reply, or a function that builds one from the request it answers.
type Reply = AssistantMessage | Callable[[Context], AssistantMessage]


# TS: it never says `implements Provider`; it fits the Protocol by shape alone.
class FakeProvider:
    """Answers each request with the next scripted reply, streamed block by block."""

    def __init__(self, replies: list[Reply], name: str = "fake") -> None:
        self.name = name
        self.replies = list(replies)  # TS: [...replies], a copy
        self.contexts: list[Context] = []
        """Every context the provider was called with, for assertions."""

    async def stream(self, model: str, context: Context) -> AsyncIterator[AssistantEvent]:
        """Record the request and replay the next reply; error when the script runs out."""
        self.contexts.append(context.model_copy(deep=True))
        if not self.replies:
            yield ErrorEvent(
                message=AssistantMessage(
                    provider=self.name, model=model, stop_reason="error", error_message="No scripted reply left."
                )
            )
            return
        reply = self.replies.pop(0)
        # TS: `a if cond else b` is `cond ? a : b`; model_copy(deep=True) is like structuredClone.
        message = reply.model_copy(deep=True) if isinstance(reply, AssistantMessage) else reply(context)
        message.provider, message.model = self.name, model  # TS: [a, b] = [x, y]
        yield StartEvent()
        for block in message.content:
            if isinstance(block, TextContent):
                yield TextDeltaEvent(delta=block.text)
            else:
                yield ToolCallEndEvent(tool_call=block)
        if message.stop_reason in ("error", "aborted"):
            yield ErrorEvent(message=message)
        else:
            yield DoneEvent(message=message)


def text_reply(text: str) -> AssistantMessage:
    """A final answer with no tool calls."""
    return AssistantMessage(provider="fake", model="fake", content=[TextContent(text=text)])


# TS: `*calls` is a rest parameter (...calls); `[*a, *b]` below is array spread.
def tool_reply(*calls: ToolCall, text: str = "") -> AssistantMessage:
    """A reply that calls tools, optionally with some text first."""
    content: list[TextContent | ToolCall] = [TextContent(text=text)] if text else []
    return AssistantMessage(provider="fake", model="fake", content=[*content, *calls], stop_reason="tool_use")
