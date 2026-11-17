import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from pydantic import BaseModel

from agentspace.agent.events import (
    AgentEndEvent,
    AgentEvent,
    MessageEndEvent,
    ToolExecutionEndEvent,
    ToolExecutionStartEvent,
    ToolExecutionUpdateEvent,
    TurnEndEvent,
)
from agentspace.agent.hooks import (
    AfterToolCallContext,
    AfterToolCallResult,
    BeforeToolCallContext,
    BeforeToolCallResult,
    LoopConfig,
)
from agentspace.agent.loop import run_agent, run_loop
from agentspace.agent.tool import OnUpdate, Tool, ToolContext, ToolResult
from agentspace.ai.messages import AssistantMessage, Message, TextContent, ToolCall, ToolResultMessage, UserMessage
from agentspace.ai.provider import AssistantEvent, Context
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


class StrictEchoTool(EchoTool):
    """EchoTool that also accepts `message` for `text`, and refuses an `explode` argument outright."""

    def prepare_arguments(self, args: dict) -> dict:
        if "explode" in args:
            raise ValueError("cannot prepare")
        if "message" in args:
            args["text"] = args.pop("message")
        return args


async def test_prepare_arguments_fixes_args_without_changing_the_transcript(tmp_path: Path) -> None:
    echo = StrictEchoTool()
    reply = tool_reply(call("echo", "c1", message="hi"), call("echo", "c2", explode=True))
    events = await run(FakeProvider([reply, text_reply("ok")]), tmp_path, tools=[echo])

    results = [m for m in ended(events) if isinstance(m, ToolResultMessage)]
    assert echo.ran == ["hi"]
    assert not results[0].is_error
    assert results[1].is_error and "cannot prepare" in results[1].content[0].text
    # The stored call still holds what the model sent.
    assert reply.tool_calls()[0].arguments == {"message": "hi"}


async def test_edit_with_camel_case_keys_runs_end_to_end(tmp_path: Path) -> None:
    (tmp_path / "f.txt").write_text("hello\n")
    reply = tool_reply(call("edit", path="f.txt", edits='[{"oldText": "hello", "newText": "bye"}]'))
    events = await run(FakeProvider([reply, text_reply("ok")]), tmp_path)

    result = next(m for m in ended(events) if isinstance(m, ToolResultMessage))
    assert not result.is_error, result.content[0].text
    assert (tmp_path / "f.txt").read_text() == "bye\n"


async def test_run_loop_reports_the_same_events_through_emit(tmp_path: Path) -> None:
    replies: list[Reply] = [tool_reply(call("echo", text="a")), text_reply("ok")]
    emitted: list[AgentEvent] = []

    async def emit(event: AgentEvent) -> None:
        emitted.append(event)

    await run_loop(
        provider=FakeProvider(list(replies)),
        model="m",
        system_prompt="sys",
        tools=[EchoTool()],
        history=[],
        prompt=UserMessage(content="go"),
        ctx=ToolContext(workspace=tmp_path),
        max_turns=10,
        emit=emit,
    )
    yielded = await run(FakeProvider(list(replies)), tmp_path, tools=[EchoTool()])

    assert [e.type for e in emitted] == [e.type for e in yielded]


class BrokenProvider(FakeProvider):
    """A provider whose stream crashes instead of ending with an error message."""

    async def stream(self, model: str, context: Context) -> AsyncIterator[AssistantEvent]:
        raise RuntimeError("socket exploded")
        yield  # unreachable; makes this an async generator like the real stream


async def test_an_error_inside_the_loop_reaches_the_reader(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="socket exploded"):
        await run(BrokenProvider([]), tmp_path)


class SlowTool(EchoTool):
    """Waits forever, and records whether it was cancelled."""

    name = "slow"

    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.cancelled = False

    async def execute(self, args: EchoParams, ctx: ToolContext) -> ToolResult:
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return ToolResult.text("never")


async def test_closing_the_reader_early_cancels_the_running_tool(tmp_path: Path) -> None:
    slow = SlowTool()
    events = run_agent(
        provider=FakeProvider([tool_reply(call("slow", text="a")), text_reply("ok")]),
        model="m",
        system_prompt="sys",
        tools=[slow],
        history=[],
        prompt=UserMessage(content="go"),
        ctx=ToolContext(workspace=tmp_path),
        max_turns=10,
    )
    # Read until the call starts, wait for the tool to be running, then stop reading, as a disconnected client does.
    async for event in events:
        if isinstance(event, ToolExecutionStartEvent):
            break
    await slow.started.wait()
    await events.aclose()  # TS: like calling `return()` on an async iterator

    assert slow.cancelled


class TimedParams(BaseModel):
    text: str
    delay: float = 0


class TimedTool(Tool[TimedParams]):
    """Sleeps for `delay`, tracking how many calls run at once and in what order they finish."""

    name = "timed"
    description = "Wait, then echo."
    params = TimedParams

    def __init__(self) -> None:
        self.active = 0
        self.most_at_once = 0
        self.finished: list[str] = []

    async def execute(self, args: TimedParams, ctx: ToolContext) -> ToolResult:
        self.active += 1
        self.most_at_once = max(self.most_at_once, self.active)
        await asyncio.sleep(args.delay)
        self.active -= 1
        self.finished.append(args.text)
        return ToolResult.text(args.text)


class SequentialTimedTool(TimedTool):
    name = "timed_seq"
    execution_mode = "sequential"


def tool_ids(events: list[AgentEvent], kind: type) -> list[str]:
    """Tool call ids of the given event type, or of ended tool-result messages for MessageEndEvent."""
    if kind is MessageEndEvent:
        return [m.tool_call_id for m in ended(events) if isinstance(m, ToolResultMessage)]
    if kind is ToolExecutionEndEvent:
        return [e.result.tool_call_id for e in events if isinstance(e, ToolExecutionEndEvent)]
    return [e.tool_call_id for e in events if isinstance(e, ToolExecutionStartEvent)]


async def test_parallel_calls_overlap_and_results_keep_the_models_order(tmp_path: Path) -> None:
    timed = TimedTool()
    reply = tool_reply(call("timed", "c1", text="slow", delay=0.05), call("timed", "c2", text="fast"))
    events = await run(FakeProvider([reply, text_reply("ok")]), tmp_path, tools=[timed])

    assert timed.most_at_once == 2
    assert tool_ids(events, ToolExecutionStartEvent) == ["c1", "c2"]
    assert tool_ids(events, ToolExecutionEndEvent) == ["c2", "c1"]  # in the order they finished
    assert tool_ids(events, MessageEndEvent) == ["c1", "c2"]  # in the order the model made them
    turn_end = next(e for e in events if isinstance(e, TurnEndEvent) and e.tool_results)
    assert [r.tool_call_id for r in turn_end.tool_results] == ["c1", "c2"]


async def test_sequential_config_or_tool_runs_the_batch_one_at_a_time(tmp_path: Path) -> None:
    timed, timed_seq = TimedTool(), SequentialTimedTool()
    calls = [call("timed", "c1", text="a", delay=0.01), call("timed", "c2", text="b", delay=0.01)]
    await run(
        FakeProvider([tool_reply(*calls), text_reply("ok")]),
        tmp_path,
        tools=[timed],
        config=LoopConfig(tool_execution="sequential"),
    )
    assert timed.most_at_once == 1

    # One sequential tool in the batch makes the other tool's calls wait their turn too.
    timed = TimedTool()
    mixed = [*calls, call("timed_seq", "c3", text="c")]
    await run(FakeProvider([tool_reply(*mixed), text_reply("ok")]), tmp_path, tools=[timed, timed_seq])
    assert timed.most_at_once == 1 and timed.finished == ["a", "b"]


async def test_parallel_batch_prepares_every_call_before_any_runs(tmp_path: Path) -> None:
    log: list[str] = []

    class LoggingTool(TimedTool):
        async def execute(self, args: TimedParams, ctx: ToolContext) -> ToolResult:
            log.append(f"run {args.text}")
            return await super().execute(args, ctx)

    async def before(ctx: BeforeToolCallContext) -> BeforeToolCallResult | None:
        log.append(f"before {ctx.tool_call.id}")
        return BeforeToolCallResult(block=True, reason="no") if ctx.tool_call.id == "c2" else None

    calls = [call("timed", "c1", text="a"), call("timed", "c2", text="b"), call("timed", "c3", text="c")]
    events = await run(
        FakeProvider([tool_reply(*calls), text_reply("ok")]),
        tmp_path,
        tools=[LoggingTool()],
        config=LoopConfig(before_tool_call=before),
    )

    assert log[:3] == ["before c1", "before c2", "before c3"]
    assert sorted(log[3:]) == ["run a", "run c"]
    # The refused call ends during preparation, before the next call starts.
    order = [
        (type(e).__name__, getattr(e, "tool_call_id", None) or e.result.tool_call_id)
        for e in events
        if isinstance(e, ToolExecutionStartEvent | ToolExecutionEndEvent)
    ]
    assert order[:4] == [
        ("ToolExecutionStartEvent", "c1"),
        ("ToolExecutionStartEvent", "c2"),
        ("ToolExecutionEndEvent", "c2"),
        ("ToolExecutionStartEvent", "c3"),
    ]


async def test_cancelling_a_parallel_batch_cancels_every_running_tool(tmp_path: Path) -> None:
    class CountingSlowTool(SlowTool):
        def __init__(self) -> None:
            super().__init__()
            self.running = 0
            self.cancels = 0

        async def execute(self, args: EchoParams, ctx: ToolContext) -> ToolResult:
            self.running += 1
            try:
                return await super().execute(args, ctx)
            except asyncio.CancelledError:
                self.cancels += 1
                raise

    slow = CountingSlowTool()
    reply = tool_reply(call("slow", "c1", text="a"), call("slow", "c2", text="b"))
    task = asyncio.create_task(run(FakeProvider([reply, text_reply("ok")]), tmp_path, tools=[slow]))
    while slow.running < 2:
        await asyncio.sleep(0)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert slow.cancels == 2


async def test_parallel_edits_to_one_file_keep_both_changes(tmp_path: Path) -> None:
    (tmp_path / "f.txt").write_text("alpha\nbeta\n")
    reply = tool_reply(
        call("edit", "c1", path="f.txt", edits=[{"old_text": "alpha", "new_text": "ALPHA"}]),
        call("edit", "c2", path="f.txt", edits=[{"old_text": "beta", "new_text": "BETA"}]),
    )
    events = await run(FakeProvider([reply, text_reply("ok")]), tmp_path)

    assert not any(r.is_error for r in ended(events) if isinstance(r, ToolResultMessage))
    assert (tmp_path / "f.txt").read_text() == "ALPHA\nBETA\n"


class ProgressTool(Tool[TimedParams]):
    """Reports two partial results, then finishes; keeps its callback to try a late update afterwards."""

    name = "progress"
    description = "Report progress."
    params = TimedParams

    def __init__(self) -> None:
        self.kept: list[OnUpdate] = []

    async def execute(self, args: TimedParams, ctx: ToolContext) -> ToolResult:
        assert ctx.on_update is not None
        self.kept.append(ctx.on_update)
        for step in ("1", "2"):
            await ctx.on_update(ToolResult.text(f"{args.text} {step}"))
            await asyncio.sleep(args.delay)
        return ToolResult.text(f"{args.text} done")


def tool_events(events: list[AgentEvent]) -> list[tuple[str, str]]:
    """(event type, call id) for tool start/update/end events, in order."""
    out: list[tuple[str, str]] = []
    for e in events:
        if isinstance(e, ToolExecutionStartEvent | ToolExecutionUpdateEvent):
            out.append((e.type, e.tool_call_id))
        elif isinstance(e, ToolExecutionEndEvent):
            out.append((e.type, e.result.tool_call_id))
    return out


async def test_tool_updates_are_emitted_before_the_calls_end(tmp_path: Path) -> None:
    progress = ProgressTool()
    reply = tool_reply(call("progress", "c1", text="a", delay=0.02), call("progress", "c2", text="b", delay=0.01))
    events = await run(FakeProvider([reply, text_reply("ok")]), tmp_path, tools=[progress])

    updates = [e for e in events if isinstance(e, ToolExecutionUpdateEvent)]
    assert sorted((u.tool_call_id, u.partial.content[0].text) for u in updates) == [
        ("c1", "a 1"),
        ("c1", "a 2"),
        ("c2", "b 1"),
        ("c2", "b 2"),
    ]
    # Parallel calls interleave, but each call's updates come before its own end.
    order = tool_events(events)
    for call_id in ("c1", "c2"):
        mine = [kind for kind, cid in order if cid == call_id]
        assert mine == ["tool_execution_start", "tool_execution_update", "tool_execution_update", "tool_execution_end"]
    # Updates are live only: the saved result is the final one.
    results = [m for m in ended(events) if isinstance(m, ToolResultMessage)]
    assert [r.content[0].text for r in results] == ["a done", "b done"]


async def test_updates_after_the_call_finished_are_ignored(tmp_path: Path) -> None:
    progress = ProgressTool()
    events = await run(
        FakeProvider([tool_reply(call("progress", text="a")), text_reply("ok")]), tmp_path, tools=[progress]
    )
    before = len(events)

    await progress.kept[0](ToolResult.text("too late"))  # no error, and nothing is emitted

    assert len(events) == before
    assert not any(isinstance(e, ToolExecutionUpdateEvent) and "late" in e.partial.content[0].text for e in events)
