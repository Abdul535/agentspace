from pathlib import Path

import pytest
from pydantic import BaseModel

from agentspace.agent.events import AgentEndEvent, AgentEvent, MessageEndEvent
from agentspace.agent.loop import run_agent
from agentspace.agent.tool import Tool, ToolContext, ToolResult
from agentspace.ai.messages import AssistantMessage, Message, TextContent, ToolCall, ToolResultMessage, UserMessage
from agentspace.ai.providers.fake import FakeProvider, Reply, text_reply, tool_reply
from agentspace.tools import default_tools

pytestmark = pytest.mark.anyio


async def run(
    provider: FakeProvider, workspace: Path, prompt: str = "go", max_turns: int = 10, tools: list[Tool] | None = None
) -> list[AgentEvent]:
    return [
        event
        async for event in run_agent(
            provider=provider,
            model="m",
            system_prompt="sys",
            tools=default_tools() if tools is None else tools,
            history=[],
            prompt=UserMessage(content=prompt),
            ctx=ToolContext(workspace=workspace),
            max_turns=max_turns,
        )
    ]


def ended(events: list[AgentEvent]) -> list[Message]:
    return [e.message for e in events if isinstance(e, MessageEndEvent)]


def call(name: str, id: str = "c1", **arguments: object) -> ToolCall:
    return ToolCall(id=id, name=name, arguments=arguments)


async def test_write_then_read_then_answer(tmp_path: Path) -> None:
    replies: list[Reply] = [
        tool_reply(call("write", "c1", path="hello.txt", content="hi there")),
        tool_reply(call("read", "c2", path="hello.txt")),
        text_reply("The file says: hi there"),
    ]
    provider = FakeProvider(replies)
    events = await run(provider, tmp_path)

    assert (tmp_path / "hello.txt").read_text() == "hi there"
    messages = ended(events)
    assert [m.role for m in messages] == ["user", "assistant", "tool_result", "assistant", "tool_result", "assistant"]
    read_result = messages[4]
    assert isinstance(read_result, ToolResultMessage) and read_result.content[0].text == "hi there"
    assert read_result.details is not None and read_result.details["path"] == "hello.txt"
    assert isinstance(events[-1], AgentEndEvent) and events[-1].reason == "done"
    assert events[-1].new_messages == messages
    # Every request carries the system prompt, every tool and the transcript so far.
    last = provider.contexts[-1]
    assert last.system_prompt == "sys"
    assert [t.name for t in last.tools] == ["read", "write", "edit"]
    assert [m.role for m in last.messages] == ["user", "assistant", "tool_result", "assistant", "tool_result"]


async def test_tool_failures_become_error_results_and_the_loop_continues(tmp_path: Path) -> None:
    bad_json = ToolCall(id="c4", name="read", arguments={}, arguments_error="Invalid JSON")
    replies: list[Reply] = [
        tool_reply(
            call("nope", "c1"),
            call("read", "c2"),  # missing required `path`
            call("read", "c3", path="missing.txt"),
            bad_json,
        ),
        text_reply("done"),
    ]
    events = await run(FakeProvider(replies), tmp_path)

    results = [m for m in ended(events) if isinstance(m, ToolResultMessage)]
    assert all(r.is_error for r in results)
    texts = [r.content[0].text for r in results]
    assert texts[0] == "Tool nope not found."
    assert texts[1].startswith("Invalid arguments for read:") and "path" in texts[1]
    assert texts[2] == "File not found: missing.txt"
    assert texts[3] == "Invalid arguments for read: Invalid JSON"
    assert events[-1].reason == "done"


async def test_tool_calls_cut_off_by_length_are_not_executed(tmp_path: Path) -> None:
    cut = tool_reply(call("write", "c1", path="x.txt", content="partial"))
    cut.stop_reason = "length"
    events = await run(FakeProvider([cut, text_reply("retrying later")]), tmp_path)

    assert not (tmp_path / "x.txt").exists()
    result = next(m for m in ended(events) if isinstance(m, ToolResultMessage))
    assert result.is_error and "output token limit" in result.content[0].text
    assert events[-1].reason == "done"


async def test_provider_error_ends_the_run(tmp_path: Path) -> None:
    failed = AssistantMessage(provider="fake", model="m", stop_reason="error", error_message="HTTP 500")
    events = await run(FakeProvider([failed]), tmp_path)

    assert [m.role for m in ended(events)] == ["user", "assistant"]
    assert events[-1].reason == "error"


async def test_max_turns_stops_a_run_that_keeps_calling_tools(tmp_path: Path) -> None:
    replies: list[Reply] = [tool_reply(call("read", f"c{i}", path=".")) for i in range(5)]
    events = await run(FakeProvider(replies), tmp_path, max_turns=3)

    assert sum(isinstance(m, AssistantMessage) for m in ended(events)) == 3
    assert events[-1].reason == "max_turns"


async def test_streamed_text_is_reported_as_message_updates(tmp_path: Path) -> None:
    reply = AssistantMessage(provider="fake", model="m", content=[TextContent(text="a"), TextContent(text="b")])
    events = await run(FakeProvider([reply]), tmp_path)
    deltas = [e.event.delta for e in events if e.type == "message_update"]
    assert deltas == ["a", "b"]


class NoParams(BaseModel):
    pass


class SoftFailTool(Tool[NoParams]):
    """Fails by returning `is_error=True` instead of raising, keeping its details."""

    name = "soft_fail"
    description = "Always fails."
    params = NoParams

    async def execute(self, args: NoParams, ctx: ToolContext) -> ToolResult:
        return ToolResult(content=[TextContent(text="nope")], details={"code": 7}, is_error=True)


async def test_returned_error_results_keep_their_details(tmp_path: Path) -> None:
    replies: list[Reply] = [tool_reply(call("soft_fail")), text_reply("ok")]
    events = await run(FakeProvider(replies), tmp_path, tools=[SoftFailTool()])

    result = next(m for m in ended(events) if isinstance(m, ToolResultMessage))
    assert result.is_error and result.content[0].text == "nope" and result.details == {"code": 7}
