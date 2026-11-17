import asyncio
import os
import time
from pathlib import Path

import pytest

from agentspace.agent.tool import ToolContext, ToolResult
from agentspace.tools import default_tools
from agentspace.tools.bash import BashParams, BashTool
from agentspace.tools.read import MAX_BYTES, MAX_LINES

pytestmark = pytest.mark.anyio


@pytest.fixture
def ctx(tmp_path: Path) -> ToolContext:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    return ToolContext(workspace=workspace)


async def bash(ctx: ToolContext, command: str, timeout: float | None = None) -> ToolResult:
    return await BashTool().execute(BashParams(command=command, timeout=timeout), ctx)


def text(result: ToolResult) -> str:
    return result.content[0].text


async def test_runs_in_the_workspace_with_stdout_and_stderr(ctx: ToolContext) -> None:
    result = await bash(ctx, "pwd; echo oops >&2")
    assert text(result) == f"{ctx.workspace.resolve()}\noops"
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


async def test_background_children_do_not_block_and_are_killed(ctx: ToolContext) -> None:
    started = time.monotonic()
    result = await bash(ctx, "(sleep 30; touch late.txt) & echo $! > bg.pid; echo done")
    assert time.monotonic() - started < 5
    assert text(result) == "done"
    assert_dead(int((ctx.workspace / "bg.pid").read_text()))


async def test_cancelling_the_run_kills_the_command(ctx: ToolContext) -> None:
    task = asyncio.create_task(bash(ctx, "echo $$ > sh.pid; sleep 30"))
    pid_file = ctx.workspace / "sh.pid"
    while not pid_file.exists() or not pid_file.read_text():
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert_dead(int(pid_file.read_text()))


async def test_long_output_keeps_the_tail_and_saves_the_rest(ctx: ToolContext) -> None:
    total = MAX_LINES + 500
    result = await bash(ctx, f"seq 1 {total}")
    out = text(result)
    assert out.startswith("501\n")
    assert f"[Showing lines 501-{total} of {total}. Full output: " in out
    assert result.details is not None and result.details["truncated"]
    full = Path(result.details["full_output_path"])
    assert full.read_text().splitlines()[0] == "1"
    full.unlink()


async def test_one_huge_line_shows_its_end(ctx: ToolContext) -> None:
    result = await bash(ctx, f"head -c {MAX_BYTES * 3} /dev/zero | tr '\\0' x; echo END")
    out = text(result)
    assert out.split("\n\n")[0].endswith("xEND")
    assert f"[Showing the last {MAX_BYTES // 1024}KB of line 1." in out
    assert result.details is not None
    Path(result.details["full_output_path"]).unlink()


def assert_dead(pid: int) -> None:
    """The process is gone (or a zombie about to be reaped)."""
    for _ in range(100):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.01)
    pytest.fail(f"process {pid} is still running")


def test_bash_is_opt_in() -> None:
    assert "bash" not in [t.name for t in default_tools()]
    assert "bash" in [t.name for t in default_tools(bash=True)]
