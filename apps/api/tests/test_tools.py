from pathlib import Path

import pytest

from agentspace.agent.tool import ToolContext, ToolError, ToolResult
from agentspace.tools.read import MAX_BYTES, MAX_LINES, ReadParams, ReadTool
from agentspace.tools.write import WriteParams, WriteTool

pytestmark = pytest.mark.anyio


@pytest.fixture
def ctx(tmp_path: Path) -> ToolContext:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    return ToolContext(workspace=workspace)


async def read_result(ctx: ToolContext, path: str, offset: int | None = None, limit: int | None = None) -> ToolResult:
    return await ReadTool().execute(ReadParams(path=path, offset=offset, limit=limit), ctx)


async def read(ctx: ToolContext, path: str, offset: int | None = None, limit: int | None = None) -> str:
    return (await read_result(ctx, path, offset, limit)).content[0].text


async def test_write_creates_parent_dirs_and_read_returns_content(ctx: ToolContext) -> None:
    out = await WriteTool().execute(WriteParams(path="a/b/notes.txt", content="one\ntwo\n"), ctx)
    assert out.content[0].text == "Wrote 8 bytes to a/b/notes.txt"
    assert out.details == {"path": "a/b/notes.txt", "bytes": 8}
    assert (ctx.workspace / "a/b/notes.txt").read_text() == "one\ntwo\n"
    assert await read(ctx, "a/b/notes.txt") == "one\ntwo"


async def test_write_overwrites(ctx: ToolContext) -> None:
    await WriteTool().execute(WriteParams(path="f.txt", content="old"), ctx)
    await WriteTool().execute(WriteParams(path="f.txt", content="new"), ctx)
    assert (ctx.workspace / "f.txt").read_text() == "new"


async def test_read_offset_and_limit(ctx: ToolContext) -> None:
    (ctx.workspace / "f.txt").write_text("\n".join(f"line {i}" for i in range(1, 11)))
    out = await read(ctx, "f.txt", offset=3, limit=2)
    assert out == "line 3\nline 4\n\n[6 more lines in file. Use offset=5 to continue.]"
    assert await read(ctx, "f.txt", offset=10) == "line 10"


async def test_read_details_describe_the_page(ctx: ToolContext) -> None:
    (ctx.workspace / "f.txt").write_text("\n".join(f"line {i}" for i in range(1, 11)))
    page = await read_result(ctx, "f.txt", offset=3, limit=2)
    assert page.details == {"path": "f.txt", "first_line": 3, "last_line": 4, "total_lines": 10, "truncated": True}
    rest = await read_result(ctx, "f.txt", offset=5)
    assert rest.details is not None and rest.details["last_line"] == 10 and not rest.details["truncated"]


async def test_read_truncates_at_line_limit(ctx: ToolContext) -> None:
    (ctx.workspace / "big.txt").write_text("\n".join("x" for _ in range(MAX_LINES + 5)))
    out = await read(ctx, "big.txt")
    assert out.endswith(f"[Showing lines 1-{MAX_LINES} of {MAX_LINES + 5}. Use offset={MAX_LINES + 1} to continue.]")


async def test_read_truncates_at_byte_limit(ctx: ToolContext) -> None:
    line = "y" * 1023  # 1 KiB with the newline
    (ctx.workspace / "wide.txt").write_text("\n".join(line for _ in range(100)))
    out = await read(ctx, "wide.txt")
    assert f"[Showing lines 1-{MAX_BYTES // 1024} of 100." in out


async def test_read_single_line_over_byte_limit(ctx: ToolContext) -> None:
    (ctx.workspace / "one.txt").write_text("z" * (MAX_BYTES * 2))
    out = await read(ctx, "one.txt")
    assert "[Line 1 is 100.0KB, over the 50KB limit; showing its first part only.]" in out


async def test_read_errors(ctx: ToolContext) -> None:
    (ctx.workspace / "f.txt").write_text("a\nb")
    (ctx.workspace / "bin").write_bytes(b"\x00\x01")
    (ctx.workspace / "empty.txt").write_text("")
    with pytest.raises(ToolError, match="File not found"):
        await read(ctx, "missing.txt")
    with pytest.raises(ToolError, match="beyond the end"):
        await read(ctx, "f.txt", offset=5)
    with pytest.raises(ToolError, match="binary"):
        await read(ctx, "bin")
    with pytest.raises(ToolError, match="is a directory"):
        await read(ctx, ".")
    assert await read(ctx, "empty.txt") == "(empty file)"


@pytest.mark.parametrize("path", ["../outside.txt", "/etc/hosts", "a/../../outside.txt"])
async def test_paths_outside_workspace_are_refused(ctx: ToolContext, path: str) -> None:
    with pytest.raises(ToolError, match="outside the workspace"):
        await read(ctx, path)
    with pytest.raises(ToolError, match="outside the workspace"):
        await WriteTool().execute(WriteParams(path=path, content="x"), ctx)


async def test_symlink_out_of_workspace_is_refused(ctx: ToolContext, tmp_path: Path) -> None:
    (tmp_path / "secret.txt").write_text("secret")
    (ctx.workspace / "link").symlink_to(tmp_path)
    with pytest.raises(ToolError, match="outside the workspace"):
        await read(ctx, "link/secret.txt")


async def test_absolute_path_inside_workspace_is_allowed(ctx: ToolContext) -> None:
    (ctx.workspace / "f.txt").write_text("ok")
    assert await read(ctx, str(ctx.workspace / "f.txt")) == "ok"
