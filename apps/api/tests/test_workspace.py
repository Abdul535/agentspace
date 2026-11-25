"""The `Workspace` protocol through `LocalWorkspace`: its contract, sad paths and races (ADR-004)."""

import asyncio
import shutil
import time
from collections.abc import Callable
from contextlib import aclosing
from pathlib import Path

import pytest

from agentspace.agent.tool import ToolError
from agentspace.workspace import DirEntry, Workspace
from agentspace.workspace.local import LocalWorkspace

pytestmark = pytest.mark.anyio


@pytest.fixture
def root(tmp_path: Path) -> Path:
    """The workspace folder, with a secret file beside it that must stay out of reach."""
    folder = tmp_path / "ws"
    folder.mkdir()
    (tmp_path / "secret.txt").write_text("secret")
    return folder


@pytest.fixture
def ws(root: Path) -> Workspace:
    """Typed as the protocol, so the tests use only what every backend offers."""
    return LocalWorkspace(root)


async def test_stat_tells_files_folders_and_nothing_apart(ws: Workspace, root: Path) -> None:
    (root / "dir").mkdir()
    (root / "f.txt").write_text("x")
    (root / "dirlink").symlink_to(root / "dir")
    assert await ws.stat("f.txt") == "file"
    assert await ws.stat("dir") == "dir"
    assert await ws.stat(".") == "dir"
    assert await ws.stat("dirlink") == "dir"  # symlinks are followed
    assert await ws.stat("missing") is None


async def test_read_and_list_report_what_is_wrong_with_the_path(ws: Workspace, root: Path) -> None:
    (root / "f.txt").write_text("x")
    with pytest.raises(ToolError, match="File not found: nope.txt"):
        await ws.read_bytes("nope.txt")
    with pytest.raises(ToolError, match=r"^\. is a directory, not a file\.$"):
        await ws.read_bytes(".")
    with pytest.raises(ToolError, match="Path not found: nope"):
        await ws.list_dir("nope")
    with pytest.raises(ToolError, match="Not a directory: f.txt"):
        await ws.list_dir("f.txt")


async def test_list_dir_marks_folders_and_links_to_folders(ws: Workspace, root: Path) -> None:
    (root / "sub").mkdir()
    (root / "a.txt").write_text("a")
    (root / "sublink").symlink_to(root / "sub")
    entries = sorted(await ws.list_dir("."), key=lambda e: e.name)
    assert entries == [DirEntry("a.txt", False), DirEntry("sub", True), DirEntry("sublink", True)]


async def test_update_file_creates_parents_and_returns_the_change_result(ws: Workspace, root: Path) -> None:
    seen: list[bytes | None] = []

    def change(old: bytes | None) -> tuple[bytes, str]:
        seen.append(old)
        return b"new", "done"

    assert await ws.update_file("a/b/c.txt", change) == "done"
    assert seen == [None]  # the file didn't exist yet
    assert (root / "a/b/c.txt").read_bytes() == b"new"
    assert await ws.read_bytes("a/b/c.txt") == b"new"


async def test_update_file_writes_nothing_when_the_change_raises(ws: Workspace, root: Path) -> None:
    (root / "f.txt").write_text("keep")

    def refuse(_old: bytes | None) -> tuple[bytes, None]:
        raise ToolError("no")

    with pytest.raises(ToolError, match="no"):
        await ws.update_file("f.txt", refuse)
    assert (root / "f.txt").read_text() == "keep"


async def test_update_file_refuses_a_folder(ws: Workspace, root: Path) -> None:
    (root / "dir").mkdir()
    with pytest.raises(ToolError, match="dir is a directory, not a file."):
        await ws.update_file("dir", lambda _old: (b"x", None))


async def test_parallel_updates_of_one_file_lose_nothing(ws: Workspace, root: Path) -> None:
    (root / "log.txt").write_bytes(b"")

    def append(line: bytes) -> Callable[[bytes | None], tuple[bytes, None]]:
        """A change that appends `line`, pausing between read and write so an unlocked update would lose it."""

        def change(old: bytes | None) -> tuple[bytes, None]:
            time.sleep(0.005)  # runs on a worker thread; widens the read-modify-write window
            return (old or b"") + line, None

        return change

    lines = [f"{i}\n".encode() for i in range(20)]
    await asyncio.gather(*(ws.update_file("log.txt", append(line)) for line in lines))
    assert sorted((root / "log.txt").read_bytes().splitlines(keepends=True)) == sorted(lines)


@pytest.mark.parametrize("path", ["../secret.txt", "/etc/hosts", "a/../../secret.txt"])
async def test_every_method_refuses_paths_outside_the_workspace(ws: Workspace, path: str) -> None:
    with pytest.raises(ToolError, match="outside the workspace"):
        await ws.stat(path)
    with pytest.raises(ToolError, match="outside the workspace"):
        await ws.read_bytes(path)
    with pytest.raises(ToolError, match="outside the workspace"):
        await ws.list_dir(path)
    with pytest.raises(ToolError, match="outside the workspace"):
        await ws.update_file(path, lambda _old: (b"x", None))
    with pytest.raises(ToolError, match="outside the workspace"):
        async with aclosing(ws.ripgrep(["--files"], cwd=path)) as lines:
            async for _ in lines:
                pass


async def test_read_roots_are_readable_but_never_writable(root: Path, tmp_path: Path) -> None:
    skill = tmp_path / "skill"
    skill.mkdir()
    (skill / "SKILL.md").write_text("how to")
    ws: Workspace = LocalWorkspace(root, read_roots=[skill])
    assert await ws.read_bytes(str(skill / "SKILL.md")) == b"how to"
    assert await ws.stat(str(skill)) == "dir"
    with pytest.raises(ToolError, match="outside the workspace"):
        await ws.update_file(str(skill / "SKILL.md"), lambda _old: (b"x", None))
    assert (skill / "SKILL.md").read_text() == "how to"


@pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep (rg) is not installed")
async def test_ripgrep_runs_in_the_given_folder_and_maps_failures(ws: Workspace, root: Path) -> None:
    (root / "src").mkdir()
    (root / "src" / "a.txt").write_text("hit\n")
    async with aclosing(ws.ripgrep(["--files"], cwd="src")) as lines:
        assert [line async for line in lines] == ["a.txt"]
    async with aclosing(ws.ripgrep(["nothing-matches-this", "."], cwd=".")) as lines:
        assert [line async for line in lines] == []  # exit 1 is "no match", not an error
    with pytest.raises(ToolError):
        async with aclosing(ws.ripgrep(["(unclosed", "."], cwd=".")) as lines:
            async for _ in lines:
                pass


async def test_exec_runs_in_the_root_writes_output_and_reports_the_exit_code(
    ws: Workspace, root: Path, tmp_path: Path
) -> None:
    out = tmp_path / "out.log"
    assert await ws.exec("pwd; echo oops >&2; exit 3", timeout=None, output=out) == 3
    assert out.read_text() == f"{root.resolve()}\noops\n"


async def test_exec_times_out(ws: Workspace, tmp_path: Path) -> None:
    with pytest.raises(TimeoutError):
        await ws.exec("sleep 5", timeout=0.1, output=tmp_path / "out.log")
