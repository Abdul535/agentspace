import asyncio
from pathlib import Path

import pytest
from pydantic import BaseModel

from agentspace.agent.events import AgentEndEvent, AgentEvent, MessageEndEvent
from agentspace.agent.hooks import (
    AfterToolCallContext,
    AfterToolCallResult,
    BeforeToolCallContext,
    BeforeToolCallResult,
    LoopConfig,
)
from agentspace.agent.loop import run_agent
from agentspace.agent.tool import Tool, ToolContext, ToolResult
from agentspace.ai.messages import AssistantMessage, Message, TextContent, ToolCall, ToolResultMessage, UserMessage
from agentspace.ai.providers.fake import FakeProvider, Reply, text_reply, tool_reply
from agentspace.tools import TOOL_SETS, build_tools

pytestmark = pytest.mark.anyio


async def run(
    provider: FakeProvider,
    workspace: Path,
    prompt: str = "go",
    max_turns: int = 10,
    tools: list[Tool] | None = None,
    config: LoopConfig | None = None,
) -> list[AgentEvent]:
    return [
        event
        async for event in run_agent(
            provider=provider,
            model="m",
            system_prompt="sys",
            tools=build_tools(TOOL_SETS["all"], bash=True) if tools is None else tools,
            history=[],
            prompt=UserMessage(content=prompt),
            ctx=ToolContext(workspace=workspace),
            max_turns=max_turns,
            config=config,
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
    assert [t.name for t in last.tools] == ["read", "bash", "edit", "write", "grep", "find", "ls"]
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


class EchoParams(BaseModel):
    text: str


class EchoTool(Tool[EchoParams]):
    """Returns its text and records every text it ran with."""

    name = "echo"
    description = "Echo the text."
    params = EchoParams

    def __init__(self) -> None:
        self.ran: list[str] = []

    async def execute(self, args: EchoParams, ctx: ToolContext) -> ToolResult:
        self.ran.append(args.text)
        return ToolResult.text(args.text, details={"len": len(args.text)})


async def run_echo(tmp_path: Path, config: LoopConfig, *calls: ToolCall) -> tuple[EchoTool, list[ToolResultMessage]]:
    """Run one reply with `calls` against an EchoTool; return the tool and the tool results."""
    echo = EchoTool()
    events = await run(FakeProvider([tool_reply(*calls), text_reply("ok")]), tmp_path, tools=[echo], config=config)
    return echo, [m for m in ended(events) if isinstance(m, ToolResultMessage)]


async def test_before_tool_call_can_block_with_or_without_a_reason(tmp_path: Path) -> None:
    async def before(ctx: BeforeToolCallContext) -> BeforeToolCallResult | None:
        if ctx.tool_call.id == "c1":
            return BeforeToolCallResult(block=True, reason="Not allowed here.")
        if ctx.tool_call.id == "c2":
            return BeforeToolCallResult(block=True)
        return None

    calls = [call("echo", "c1", text="a"), call("echo", "c2", text="b"), call("echo", "c3", text="c")]
    echo, results = await run_echo(tmp_path, LoopConfig(before_tool_call=before), *calls)

    assert echo.ran == ["c"]
    assert [(r.is_error, r.content[0].text) for r in results] == [
        (True, "Not allowed here."),
        (True, "Tool execution was blocked."),
        (False, "c"),
    ]


async def test_before_tool_call_gets_validated_args_and_can_change_them(tmp_path: Path) -> None:
    seen: list[str] = []

    async def before(ctx: BeforeToolCallContext) -> None:
        assert isinstance(ctx.args, EchoParams)
        seen.append(ctx.tool_call.id)
        ctx.args.text = ctx.args.text.upper()

    calls = [call("echo", "c1", text="hi"), call("nope", "c2"), call("echo", "c3", text=5)]
    echo, results = await run_echo(tmp_path, LoopConfig(before_tool_call=before), *calls)

    # Unknown tools and invalid arguments fail before the hook is asked.
    assert seen == ["c1"]
    assert echo.ran == ["HI"]
    assert [r.is_error for r in results] == [False, True, True]


async def test_a_failing_before_tool_call_blocks_the_call(tmp_path: Path) -> None:
    async def before(ctx: BeforeToolCallContext) -> None:
        raise RuntimeError("policy store offline")

    echo, results = await run_echo(tmp_path, LoopConfig(before_tool_call=before), call("echo", text="a"))

    assert echo.ran == []
    assert results[0].is_error and "policy store offline" in results[0].content[0].text


async def test_after_tool_call_replaces_only_the_fields_it_sets(tmp_path: Path) -> None:
    async def after(ctx: AfterToolCallContext) -> AfterToolCallResult | None:
        match ctx.tool_call.id:  # TS: like a `switch` on the id
            case "c1":
                return AfterToolCallResult(content=[TextContent(text="[redacted]")])
            case "c2":
                return AfterToolCallResult(is_error=True)
            case "c3":
                return AfterToolCallResult(content=[], details={})
            case _:
                return None

    calls = [call("echo", f"c{i}", text="secret") for i in range(1, 5)]
    _, results = await run_echo(tmp_path, LoopConfig(after_tool_call=after), *calls)

    assert [(r.content, r.details, r.is_error) for r in results] == [
        ([TextContent(text="[redacted]")], {"len": 6}, False),
        ([TextContent(text="secret")], {"len": 6}, True),
        ([], {}, False),  # an empty list or dict replaces; only None means "keep"
        ([TextContent(text="secret")], {"len": 6}, False),
    ]


async def test_a_failing_after_tool_call_replaces_the_result_with_an_error(tmp_path: Path) -> None:
    async def after(ctx: AfterToolCallContext) -> None:
        raise ValueError("filter crashed")

    echo, results = await run_echo(tmp_path, LoopConfig(after_tool_call=after), call("echo", text="secret"))

    assert echo.ran == ["secret"]  # the tool did run
    assert results[0].is_error and "filter crashed" in results[0].content[0].text
    assert "secret" not in results[0].content[0].text


async def test_after_tool_call_is_skipped_for_blocked_calls(tmp_path: Path) -> None:
    after_calls: list[str] = []

    async def before(ctx: BeforeToolCallContext) -> BeforeToolCallResult:
        return BeforeToolCallResult(block=True)

    async def after(ctx: AfterToolCallContext) -> None:
        after_calls.append(ctx.tool_call.id)

    config = LoopConfig(before_tool_call=before, after_tool_call=after)
    await run_echo(tmp_path, config, call("echo", text="a"))

    assert after_calls == []


async def test_cancelling_while_before_tool_call_waits_stops_the_run(tmp_path: Path) -> None:
    waiting = asyncio.Event()
    echo = EchoTool()

    async def before(ctx: BeforeToolCallContext) -> None:
        waiting.set()
        await asyncio.Event().wait()  # an approval that never comes

    provider = FakeProvider([tool_reply(call("echo", text="a")), text_reply("ok")])
    # TS: like starting a promise you can cancel later; cancel() raises CancelledError at its current await.
    task = asyncio.create_task(run(provider, tmp_path, tools=[echo], config=LoopConfig(before_tool_call=before)))
    await waiting.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert echo.ran == []
