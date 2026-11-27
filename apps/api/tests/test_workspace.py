"""The `Workspace` protocol on every backend: its contract, sad paths and races (ADR-004, ADR-034, ADR-035).

Each test runs on `LocalWorkspace`, on `SandboxWorkspace` over a fake sandbox on this machine, and,
when `DAYTONA_API_KEY` is set, on `SandboxWorkspace` over a real Daytona sandbox. Tests make their
files with the workspace's own `exec`, so they never reach past the protocol.
"""

import asyncio
import os
import shlex
import shutil
import time
import uuid
from collections.abc import AsyncGenerator, Callable, Iterator, Sequence
from contextlib import aclosing
from dataclasses import dataclass
from pathlib import Path

import pytest
from daytona import AsyncDaytona, CreateSandboxFromSnapshotParams, Daytona, DaytonaConfig
from host_box import HostBox

from agentspace.agent.tool import ToolError
from agentspace.workspace import DirEntry, Workspace
from agentspace.workspace.daytona import DaytonaBox
from agentspace.workspace.local import LocalWorkspace
from agentspace.workspace.sandbox import Box, SandboxWorkspace

pytestmark = pytest.mark.anyio

DAYTONA_KEY = os.environ.get("DAYTONA_API_KEY")


@dataclass
class Backend:
    """One backend under test: where its workspace is, and how to make one."""

    name: str
    base: str
    """An absolute folder outside the workspace, for files it must not reach (and skill folders)."""
    root: str
    """The workspace folder, as the backend's commands see it."""
    make: Callable[[Sequence[str]], Workspace]
    """A workspace on `root` that may also read the given absolute folders."""


@pytest.fixture(scope="session")
def daytona_sandbox() -> Iterator[str]:
    """One real Daytona sandbox for the whole test run, deleted at the end; its name."""
    assert DAYTONA_KEY is not None
    client = Daytona(DaytonaConfig(api_key=DAYTONA_KEY))
    name = f"agentspace-test-{uuid.uuid4().hex[:12]}"
    sandbox = client.create(
        CreateSandboxFromSnapshotParams(
            name=name, labels={"agentspace-test": "1"}, auto_stop_interval=15, network_block_all=True
        )
    )
    try:
        yield name
    finally:
        client.delete(sandbox)


@pytest.fixture(
    params=[
        "local",
        "sandbox",
        pytest.param("daytona", marks=pytest.mark.skipif(DAYTONA_KEY is None, reason="DAYTONA_API_KEY is not set")),
    ]
)
async def backend(request: pytest.FixtureRequest, tmp_path: Path) -> AsyncGenerator[Backend]:
    """The backend named by the test's parameter, with an empty workspace folder."""
    if request.param == "local":
        (tmp_path / "ws").mkdir()
        yield Backend(
            "local",
            str(tmp_path),
            str(tmp_path / "ws"),
            lambda roots: LocalWorkspace(tmp_path / "ws", read_roots=[Path(r) for r in roots]),
        )
        return
    if request.param == "sandbox":
        box = HostBox()
        (tmp_path / "ws").mkdir()
        yield Backend(
            "sandbox", str(tmp_path), str(tmp_path / "ws"), lambda roots: _sandbox_ws(box, str(tmp_path / "ws"), roots)
        )
        return
    name = request.getfixturevalue("daytona_sandbox")
    # TS: `async with` closes the client when the test ends, like `await using`.
    async with AsyncDaytona(DaytonaConfig(api_key=DAYTONA_KEY)) as client:
        daytona_box = DaytonaBox(await client.get(name))
        base = f"/home/daytona/t-{uuid.uuid4().hex[:8]}"  # each test gets its own folders in the shared sandbox
        await daytona_box.run(f"mkdir -p {base}/ws", cwd="/", on_stdout=lambda _c: None, on_stderr=lambda _c: None)
        yield Backend("daytona", base, f"{base}/ws", lambda roots: _sandbox_ws(daytona_box, f"{base}/ws", roots))


def _sandbox_ws(box: Box, root: str, roots: Sequence[str]) -> SandboxWorkspace:
    """A `SandboxWorkspace` on an already-open box."""

    async def open_box() -> Box:
        """The box, already running."""
        return box

    return SandboxWorkspace(open_box, root=root, read_roots=roots, default_timeout=30)


@pytest.fixture
def ws(backend: Backend) -> Workspace:
    """Typed as the protocol, so the tests use only what every backend offers."""
    return backend.make([])


async def sh(ws: Workspace, command: str, tmp_path: Path) -> str:
    """Run a setup command in the workspace and return its output; fails the test on a non-zero exit."""
    out = tmp_path / f"sh-{uuid.uuid4().hex}.log"
    code = await ws.exec(command, timeout=30, output=out)
    text = out.read_text()
    assert code == 0, f"{command!r} exited {code}: {text}"
    return text


async def test_stat_tells_files_folders_and_nothing_apart(ws: Workspace, tmp_path: Path) -> None:
    await sh(ws, "mkdir dir && printf x > f.txt && ln -s dir dirlink", tmp_path)
    assert await ws.stat("f.txt") == "file"
    assert await ws.stat("dir") == "dir"
    assert await ws.stat(".") == "dir"
    assert await ws.stat("dirlink") == "dir"  # symlinks are followed
    assert await ws.stat("missing") is None


async def test_read_and_list_report_what_is_wrong_with_the_path(ws: Workspace, tmp_path: Path) -> None:
    await sh(ws, "printf x > f.txt", tmp_path)
    with pytest.raises(ToolError, match="File not found: nope.txt"):
        await ws.read_bytes("nope.txt")
    with pytest.raises(ToolError, match=r"^\. is a directory, not a file\.$"):
        await ws.read_bytes(".")
    with pytest.raises(ToolError, match="Path not found: nope"):
        await ws.list_dir("nope")
    with pytest.raises(ToolError, match="Not a directory: f.txt"):
        await ws.list_dir("f.txt")


async def test_list_dir_marks_folders_and_links_to_folders(ws: Workspace, tmp_path: Path) -> None:
    await sh(ws, "mkdir sub && printf a > a.txt && ln -s sub sublink", tmp_path)
    entries = sorted(await ws.list_dir("."), key=lambda e: e.name)
    assert entries == [DirEntry("a.txt", False), DirEntry("sub", True), DirEntry("sublink", True)]


async def test_read_bytes_round_trips_binary(ws: Workspace) -> None:
    payload = bytes(range(256)) * 4
    await ws.update_file("blob.bin", lambda _old: (payload, None))
    assert await ws.read_bytes("blob.bin") == payload


async def test_update_file_creates_parents_and_returns_the_change_result(ws: Workspace, tmp_path: Path) -> None:
    seen: list[bytes | None] = []

    def change(old: bytes | None) -> tuple[bytes, str]:
        seen.append(old)
        return b"new", "done"

    assert await ws.update_file("a/b/c.txt", change) == "done"
    assert seen == [None]  # the file didn't exist yet
    assert await ws.read_bytes("a/b/c.txt") == b"new"
    # Really on the backend, not cached. Daytona's log ends an unfinished last line with a newline.
    assert (await sh(ws, "cat a/b/c.txt", tmp_path)).rstrip("\n") == "new"


async def test_update_file_writes_nothing_when_the_change_raises(ws: Workspace, tmp_path: Path) -> None:
    await sh(ws, "printf keep > f.txt", tmp_path)

    def refuse(_old: bytes | None) -> tuple[bytes, None]:
        raise ToolError("no")

    with pytest.raises(ToolError, match="no"):
        await ws.update_file("f.txt", refuse)
    assert await ws.read_bytes("f.txt") == b"keep"


async def test_update_file_refuses_a_folder(ws: Workspace, tmp_path: Path) -> None:
    await sh(ws, "mkdir dir", tmp_path)
    with pytest.raises(ToolError, match="dir is a directory, not a file."):
        await ws.update_file("dir", lambda _old: (b"x", None))


async def test_parallel_updates_of_one_file_lose_nothing(ws: Workspace) -> None:
    await ws.update_file("log.txt", lambda _old: (b"", None))

    def append(line: bytes) -> Callable[[bytes | None], tuple[bytes, None]]:
        """A change that appends `line`, pausing between read and write so an unlocked update would lose it."""

        def change(old: bytes | None) -> tuple[bytes, None]:
            time.sleep(0.005)  # runs on a worker thread; widens the read-modify-write window
            return (old or b"") + line, None

        return change

    lines = [f"{i}\n".encode() for i in range(10)]
    await asyncio.gather(*(ws.update_file("log.txt", append(line)) for line in lines))
    assert sorted((await ws.read_bytes("log.txt")).splitlines(keepends=True)) == sorted(lines)


@pytest.mark.parametrize("path", ["../secret.txt", "/etc/hosts", "a/../../secret.txt"])
async def test_every_method_refuses_paths_outside_the_workspace(ws: Workspace, path: str, tmp_path: Path) -> None:
    await sh(ws, "printf secret > ../secret.txt", tmp_path)
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
    assert (await sh(ws, "cat ../secret.txt", tmp_path)).rstrip("\n") == "secret"


async def test_read_roots_are_readable_but_never_writable(backend: Backend, tmp_path: Path) -> None:
    skill = f"{backend.base}/skill"
    ws = backend.make([skill])
    await sh(ws, f"mkdir -p {shlex.quote(skill)} && printf 'how to' > {shlex.quote(skill)}/SKILL.md", tmp_path)
    assert await ws.read_bytes(f"{skill}/SKILL.md") == b"how to"
    assert await ws.stat(skill) == "dir"
    with pytest.raises(ToolError, match="outside the workspace"):
        await ws.update_file(f"{skill}/SKILL.md", lambda _old: (b"x", None))
    assert await ws.read_bytes(f"{skill}/SKILL.md") == b"how to"


async def test_ripgrep_runs_in_the_given_folder_and_maps_failures(
    backend: Backend, ws: Workspace, tmp_path: Path
) -> None:
    if backend.name != "daytona" and shutil.which("rg") is None:
        pytest.skip("ripgrep (rg) is not installed")
    await sh(ws, "mkdir src && echo hit > src/a.txt", tmp_path)
    async with aclosing(ws.ripgrep(["--files"], cwd="src")) as lines:
        assert [line async for line in lines] == ["a.txt"]
    async with aclosing(ws.ripgrep(["nothing-matches-this", "."], cwd=".")) as lines:
        assert [line async for line in lines] == []  # exit 1 is "no match", not an error
    with pytest.raises(ToolError):
        async with aclosing(ws.ripgrep(["(unclosed", "."], cwd=".")) as lines:
            async for _ in lines:
                pass


async def test_ripgrep_stopping_early_still_returns_the_first_lines(
    backend: Backend, ws: Workspace, tmp_path: Path
) -> None:
    if backend.name != "daytona" and shutil.which("rg") is None:
        pytest.skip("ripgrep (rg) is not installed")
    await sh(ws, "for i in $(seq 1 2000); do echo hit > f$i.txt; done", tmp_path)
    got: list[str] = []
    async with aclosing(ws.ripgrep(["--files"], cwd=".")) as lines:
        async for line in lines:
            got.append(line)
            if len(got) == 3:
                break
    assert len(got) == 3 and all(line.startswith("f") for line in got)


async def test_exec_runs_in_the_root_writes_output_and_reports_the_exit_code(
    backend: Backend, ws: Workspace, tmp_path: Path
) -> None:
    out = tmp_path / "out.log"
    assert await ws.exec("pwd -P; echo oops >&2; exit 3", timeout=None, output=out) == 3
    expected_root = backend.root if backend.name == "daytona" else os.path.realpath(backend.root)
    assert out.read_text() == f"{expected_root}\noops\n"


async def test_exec_times_out(ws: Workspace, tmp_path: Path) -> None:
    with pytest.raises(TimeoutError):
        await ws.exec("sleep 5", timeout=0.5, output=tmp_path / "out.log")


async def test_exec_returns_when_a_background_child_holds_the_output_and_kills_it(
    ws: Workspace, tmp_path: Path
) -> None:
    started = time.monotonic()
    code = await ws.exec("(sleep 30; echo late) & echo $! > child.pid; echo hi", timeout=20, output=tmp_path / "o.log")
    assert code == 0
    assert time.monotonic() - started < 15  # not held open until the child's 30 s are up
    assert (tmp_path / "o.log").read_text() == "hi\n"
    assert await sh(ws, "kill -0 $(cat child.pid) 2>/dev/null && echo ALIVE || echo DEAD", tmp_path) == "DEAD\n"
