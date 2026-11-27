import asyncio
import os
import tempfile
import time
from pathlib import Path

import pytest

from agentspace.agent.tool import ToolContext, ToolResult
from agentspace.ai.messages import joined_text
from agentspace.tools import TOOL_SETS, build_tools
from agentspace.tools.bash import BashParams, BashTool, read_tail
from agentspace.tools.read import MAX_BYTES, MAX_LINES
from agentspace.workspace.local import LocalWorkspace

pytestmark = pytest.mark.anyio


@pytest.fixture
def ws(tmp_path: Path) -> Path:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    return workspace


@pytest.fixture
def ctx(ws: Path) -> ToolContext:
    return ToolContext(workspace=LocalWorkspace(ws))


async def bash(ctx: ToolContext, command: str, timeout: float | None = None) -> ToolResult:
    return await BashTool().execute(BashParams(command=command, timeout=timeout), ctx)


def text(result: ToolResult) -> str:
    return joined_text(result.content)


async def test_runs_in_the_workspace_with_stdout_and_stderr(ctx: ToolContext, ws: Path) -> None:
    result = await bash(ctx, "pwd; echo oops >&2")
    assert text(result) == f"{ws.resolve()}\noops"
    assert not result.is_error
    assert result.details is not None and result.details["exit_code"] == 0 and not result.details["truncated"]


async def test_no_output(ctx: ToolContext) -> None:
    assert text(await bash(ctx, "true")) == "(no output)"


async def test_non_zero_exit_is_an_error_with_output(ctx: ToolContext) -> None:
    result = await bash(ctx, "echo partial; exit 3")
    assert result.is_error and text(result) == "partial\n\nCommand exited with code 3"
    assert result.details is not None and result.details["exit_code"] == 3


async def test_server_secrets_are_not_inherited(ctx: ToolContext, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-secret")
    assert text(await bash(ctx, 'echo "key=[$OPENROUTER_API_KEY]"')) == "key=[]"


async def test_timeout_kills_the_command_and_keeps_its_output(ctx: ToolContext) -> None:
    started = time.monotonic()
    result = await bash(ctx, "echo started; sleep 30", timeout=0.5)
    assert time.monotonic() - started < 5
    assert result.is_error and text(result) == "started\n\nCommand timed out after 0.5 seconds"
    assert result.details is not None and result.details["exit_code"] is None


async def test_background_children_do_not_block_and_are_killed(ctx: ToolContext, ws: Path) -> None:
    started = time.monotonic()
    result = await bash(ctx, "(sleep 30; touch late.txt) & echo $! > bg.pid; echo done")
    assert time.monotonic() - started < 5
    assert text(result) == "done"
    assert_dead(int((ws / "bg.pid").read_text()))


async def test_cancelling_the_run_kills_the_command(ctx: ToolContext, ws: Path) -> None:
    task = asyncio.create_task(bash(ctx, "echo $$ > sh.pid; sleep 30"))
    pid_file = ws / "sh.pid"
    while not pid_file.exists() or not pid_file.read_text():
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert_dead(int(pid_file.read_text()))


async def test_long_output_keeps_the_tail_and_saves_the_rest_in_the_workspace(ctx: ToolContext) -> None:
    total = MAX_LINES + 500
    result = await bash(ctx, f"seq 1 {total}")
    out = text(result)
    assert out.startswith("501\n")
    assert f"[Showing lines 501-{total} of {total}. Full output: " in out
    assert result.details is not None and result.details["truncated"]
    # Inside the workspace, so the model can `read` it; the server's temp copy is gone.
    saved = result.details["full_output_path"]
    assert saved.startswith(".agentspace/bash/")
    assert f"Full output: {saved}]" in out
    assert (await ctx.workspace.read_bytes(saved)).decode().splitlines()[0] == "1"
    assert not list(Path(tempfile.gettempdir()).glob(Path(saved).name))


async def test_one_huge_line_shows_its_end(ctx: ToolContext) -> None:
    result = await bash(ctx, f"head -c {MAX_BYTES * 3} /dev/zero | tr '\\0' x; echo END")
    out = text(result)
    assert out.split("\n\n")[0].endswith("xEND")
    assert f"[Showing the last {MAX_BYTES // 1024}KB of line 1." in out
    assert result.details is not None
    assert (await ctx.workspace.read_bytes(result.details["full_output_path"])).endswith(b"xEND\n")


def assert_dead(pid: int) -> None:
    """The process is gone (or a zombie about to be reaped)."""
    for _ in range(100):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.01)
    pytest.fail(f"process {pid} is still running")


def test_bash_needs_the_server_switch_whatever_the_session_stores() -> None:
    assert [t.name for t in build_tools(TOOL_SETS["coding"], bash=False)] == ["read", "edit", "write"]
    assert [t.name for t in build_tools(TOOL_SETS["coding"], bash=True)] == ["read", "bash", "edit", "write"]


async def test_running_command_streams_its_output_so_far(ctx: ToolContext) -> None:
    updates: list[str] = []

    async def on_update(partial: ToolResult) -> None:
        updates.append(text(partial))

    live = ToolContext(workspace=ctx.workspace, on_update=on_update)
    result = await BashTool().execute(BashParams(command="for i in 1 2 3; do echo line $i; sleep 0.15; done"), live)

    assert text(result) == "line 1\nline 2\nline 3"
    assert len(updates) >= 2
    assert updates[0].startswith("line 1")
    # Each update is a snapshot of everything so far, so each one extends the last.
    assert all(later.startswith(earlier) for earlier, later in zip(updates, updates[1:], strict=False))


def test_read_tail_starts_at_a_line_break_when_it_cuts(tmp_path: Path) -> None:
    log = tmp_path / "out.log"
    log.write_text("first line\nsecond\nthird\n")
    assert read_tail(log, 1024) == "first line\nsecond\nthird\n"
    assert read_tail(log, 10) == "third\n"  # "ond\nthird\n" minus the partial "ond"
