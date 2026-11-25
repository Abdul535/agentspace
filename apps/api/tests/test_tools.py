import asyncio
from pathlib import Path

import pytest
from pydantic import ValidationError

from agentspace.agent.tool import ToolContext, ToolError, ToolResult
from agentspace.ai.messages import joined_text
from agentspace.tools.edit import EditParams, EditTool, Replacement
from agentspace.tools.read import MAX_BYTES, MAX_LINES, ReadParams, ReadTool
from agentspace.tools.write import WriteParams, WriteTool
from agentspace.workspace.file_lock import _entries, file_lock
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


async def read_result(ctx: ToolContext, path: str, offset: int | None = None, limit: int | None = None) -> ToolResult:
    return await ReadTool().execute(ReadParams(path=path, offset=offset, limit=limit), ctx)


async def read(ctx: ToolContext, path: str, offset: int | None = None, limit: int | None = None) -> str:
    return joined_text((await read_result(ctx, path, offset, limit)).content)


async def test_write_creates_parent_dirs_and_read_returns_content(ctx: ToolContext, ws: Path) -> None:
    out = await WriteTool().execute(WriteParams(path="a/b/notes.txt", content="one\ntwo\n"), ctx)
    assert joined_text(out.content) == "Wrote 8 bytes to a/b/notes.txt"
    assert out.details == {"path": "a/b/notes.txt", "bytes": 8}
    assert (ws / "a/b/notes.txt").read_text() == "one\ntwo\n"
    assert await read(ctx, "a/b/notes.txt") == "one\ntwo"


async def test_write_overwrites(ctx: ToolContext, ws: Path) -> None:
    await WriteTool().execute(WriteParams(path="f.txt", content="old"), ctx)
    await WriteTool().execute(WriteParams(path="f.txt", content="new"), ctx)
    assert (ws / "f.txt").read_text() == "new"


async def test_read_offset_and_limit(ctx: ToolContext, ws: Path) -> None:
    (ws / "f.txt").write_text("\n".join(f"line {i}" for i in range(1, 11)))
    out = await read(ctx, "f.txt", offset=3, limit=2)
    assert out == "line 3\nline 4\n\n[6 more lines in file. Use offset=5 to continue.]"
    assert await read(ctx, "f.txt", offset=10) == "line 10"


async def test_read_details_describe_the_page(ctx: ToolContext, ws: Path) -> None:
    (ws / "f.txt").write_text("\n".join(f"line {i}" for i in range(1, 11)))
    page = await read_result(ctx, "f.txt", offset=3, limit=2)
    assert page.details == {"path": "f.txt", "first_line": 3, "last_line": 4, "total_lines": 10, "truncated": True}
    rest = await read_result(ctx, "f.txt", offset=5)
    assert rest.details is not None and rest.details["last_line"] == 10 and not rest.details["truncated"]


async def test_read_truncates_at_line_limit(ctx: ToolContext, ws: Path) -> None:
    (ws / "big.txt").write_text("\n".join("x" for _ in range(MAX_LINES + 5)))
    out = await read(ctx, "big.txt")
    assert out.endswith(f"[Showing lines 1-{MAX_LINES} of {MAX_LINES + 5}. Use offset={MAX_LINES + 1} to continue.]")


async def test_read_truncates_at_byte_limit(ctx: ToolContext, ws: Path) -> None:
    line = "y" * 1023  # 1 KiB with the newline
    (ws / "wide.txt").write_text("\n".join(line for _ in range(100)))
    out = await read(ctx, "wide.txt")
    assert f"[Showing lines 1-{MAX_BYTES // 1024} of 100." in out


async def test_read_single_line_over_byte_limit(ctx: ToolContext, ws: Path) -> None:
    (ws / "one.txt").write_text("z" * (MAX_BYTES * 2))
    out = await read(ctx, "one.txt")
    assert "[Line 1 is 100.0KB, over the 50KB limit; showing its first part only.]" in out


async def test_read_errors(ctx: ToolContext, ws: Path) -> None:
    (ws / "f.txt").write_text("a\nb")
    (ws / "bin").write_bytes(b"\x00\x01")
    (ws / "empty.txt").write_text("")
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


async def test_symlink_out_of_workspace_is_refused(ctx: ToolContext, ws: Path, tmp_path: Path) -> None:
    (tmp_path / "secret.txt").write_text("secret")
    (ws / "link").symlink_to(tmp_path)
    with pytest.raises(ToolError, match="outside the workspace"):
        await read(ctx, "link/secret.txt")


async def test_absolute_path_inside_workspace_is_allowed(ctx: ToolContext, ws: Path) -> None:
    (ws / "f.txt").write_text("ok")
    assert await read(ctx, str(ws / "f.txt")) == "ok"


async def edit(ctx: ToolContext, path: str, *pairs: tuple[str, str]) -> ToolResult:
    edits = [Replacement(old_text=old, new_text=new) for old, new in pairs]
    return await EditTool().execute(EditParams(path=path, edits=edits), ctx)


async def test_edit_applies_disjoint_edits_against_the_original(ctx: ToolContext, ws: Path) -> None:
    (ws / "f.py").write_text("a = 1\nb = 2\nc = 3\n")
    out = await edit(ctx, "f.py", ("c = 3", "c = 30"), ("a = 1", "a = 10"))
    assert joined_text(out.content) == "Successfully replaced 2 block(s) in f.py."
    assert (ws / "f.py").read_text() == "a = 10\nb = 2\nc = 30\n"
    assert out.details is not None and out.details["first_changed_line"] == 1
    assert "-a = 1\n+a = 10\n" in out.details["patch"] and "+c = 30\n" in out.details["patch"]


async def test_edit_keeps_bom_and_crlf(ctx: ToolContext, ws: Path) -> None:
    (ws / "w.txt").write_bytes(b"\xef\xbb\xbfone\r\ntwo\r\nthree\r\n")
    out = await edit(ctx, "w.txt", ("two\nthree", "2\n3"))
    assert (ws / "w.txt").read_bytes() == b"\xef\xbb\xbfone\r\n2\r\n3\r\n"
    assert out.details is not None and out.details["first_changed_line"] == 2


async def test_edit_errors(ctx: ToolContext, ws: Path) -> None:
    (ws / "f.txt").write_text("x\nx\ny\nz\n")
    with pytest.raises(ToolError, match="Could not find the text"):
        await edit(ctx, "f.txt", ("nope", "q"))
    with pytest.raises(ToolError, match="Found 2 occurrences of the text"):
        await edit(ctx, "f.txt", ("x", "q"))
    with pytest.raises(ToolError, match=r"edits\[1\].old_text must not be empty"):
        await edit(ctx, "f.txt", ("y", "q"), ("", "q"))
    with pytest.raises(ToolError, match=r"edits\[0\] and edits\[1\] overlap"):
        await edit(ctx, "f.txt", ("y\nz", "q"), ("z", "q"))
    with pytest.raises(ToolError, match="No changes made"):
        await edit(ctx, "f.txt", ("y", "y"))
    with pytest.raises(ToolError, match="File not found"):
        await edit(ctx, "missing.txt", ("a", "b"))
    with pytest.raises(ToolError, match="outside the workspace"):
        await edit(ctx, "../f.txt", ("a", "b"))
    assert (ws / "f.txt").read_text() == "x\nx\ny\nz\n"


async def test_edit_requires_at_least_one_edit() -> None:
    with pytest.raises(ValidationError):
        EditParams(path="f.txt", edits=[])


def prepared_edits(args: dict) -> list[Replacement]:
    """Run EditTool's argument repair, then validate, as the loop does."""
    return EditParams.model_validate(EditTool().prepare_arguments(args)).edits


def test_edit_repairs_common_argument_shapes() -> None:
    one = [Replacement(old_text="a", new_text="b")]
    # edits as a JSON string: a list, or a single object
    assert prepared_edits({"path": "f", "edits": '[{"old_text": "a", "new_text": "b"}]'}) == one
    assert prepared_edits({"path": "f", "edits": '{"old_text": "a", "new_text": "b"}'}) == one
    # a single object instead of a list
    assert prepared_edits({"path": "f", "edits": {"old_text": "a", "new_text": "b"}}) == one
    # camelCase keys, inside edits or at the top level
    assert prepared_edits({"path": "f", "edits": [{"oldText": "a", "newText": "b"}]}) == one
    assert prepared_edits({"path": "f", "oldText": "a", "newText": "b"}) == one
    # the old single-edit shape is appended after any edits already given
    xy = {"old_text": "x", "new_text": "y"}
    both = prepared_edits({"path": "f", "edits": [xy], "old_text": "a", "new_text": "b"})
    assert both == [Replacement(old_text="x", new_text="y"), *one]


def test_edit_leaves_unfixable_arguments_for_validation() -> None:
    with pytest.raises(ValidationError):
        prepared_edits({"path": "f", "edits": "[not json"})
    with pytest.raises(ValidationError):
        prepared_edits({"path": "f", "edits": [{"old_text": "a"}]})
    valid = {"path": "f", "edits": [{"old_text": "a", "new_text": "b"}]}
    assert EditTool().prepare_arguments(dict(valid)) == valid


async def hold(path: Path, log: list[str], name: str) -> None:
    """Take `path`'s lock, note when it's held and released, and yield to other tasks while holding it."""
    async with file_lock(path):
        log.append(f"{name} in")
        await asyncio.sleep(0.01)
        log.append(f"{name} out")


async def test_file_lock_serializes_one_file_and_shares_it_through_symlinks(tmp_path: Path) -> None:
    target = tmp_path / "f.txt"
    link = tmp_path / "link.txt"
    link.symlink_to(target)  # the target doesn't need to exist yet
    log: list[str] = []

    await asyncio.gather(hold(target, log, "a"), hold(link, log, "b"))

    assert log == ["a in", "a out", "b in", "b out"]
    assert _entries == {}  # released locks are dropped


async def test_file_lock_lets_different_files_overlap(tmp_path: Path) -> None:
    log: list[str] = []
    await asyncio.gather(hold(tmp_path / "a.txt", log, "a"), hold(tmp_path / "b.txt", log, "b"))
    assert log[:2] == ["a in", "b in"]
