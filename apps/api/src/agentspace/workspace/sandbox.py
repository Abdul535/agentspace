"""`SandboxWorkspace`: a session's workspace inside a remote sandbox (ADR-035).

The sandbox itself is behind the `Box` port, so the vendor (Daytona, `workspace/daytona.py`) and a
test fake are interchangeable. This module owns what every vendor shares: path checks, the error
wording `LocalWorkspace` uses, the per-file lock, rg's exit codes, and the command time limit.
"""

import asyncio
import contextlib
import posixpath
import shlex
from collections.abc import AsyncGenerator, Awaitable, Callable, Sequence
from pathlib import Path
from typing import Protocol

from asyncer import asyncify

from agentspace.agent.tool import ToolError
from agentspace.workspace import DirEntry, PathKind
from agentspace.workspace.file_lock import key_lock

# Text chunks of a command's output, in the order they arrive.
type OnOutput = Callable[[str], None]


class Box(Protocol):
    """One running sandbox: files by absolute path, and commands.

    Vendor failures (unreachable, timed out, refused) raise `ToolError` with a message safe to show.
    """

    @property
    def id(self) -> str:
        """The vendor's id for this sandbox; names its file locks."""
        ...

    async def kind(self, path: str) -> PathKind | None:
        """File, folder (symlinks followed), or None when nothing is at `path`."""
        ...

    async def download(self, path: str) -> bytes:
        """The whole file at `path`, which must exist and be a file."""
        ...

    async def upload(self, path: str, data: bytes) -> None:
        """Write `data` to `path`, creating its parent folders."""
        ...

    async def entries(self, path: str) -> list[DirEntry]:
        """The entries of the folder at `path`, which must exist and be a folder."""
        ...

    async def run(self, command: str, *, cwd: str, on_stdout: OnOutput, on_stderr: OnOutput) -> int:
        """Run `command` with bash in `cwd`, stdin empty, and return its exit code once it exits.

        Output reaches the callbacks as it is written. Every process the command started is killed on
        return and when the call is cancelled, so a background child can't hold the call open.
        """
        ...


# Opens (creates or wakes) the session's sandbox; called on first use, again only if it failed.
type OpenBox = Callable[[], Awaitable[Box]]


class SandboxWorkspace:
    """The folder `root` inside a sandbox, opened on first use, plus read-only folders (skills) reads may reach."""

    def __init__(self, open_box: OpenBox, *, root: str, read_roots: Sequence[str] = (), default_timeout: float) -> None:
        """`root` and `read_roots` are absolute paths in the sandbox; `default_timeout` caps a command given none."""
        self._open_box = open_box
        self._box: Box | None = None
        self._opening = asyncio.Lock()
        self.root = posixpath.normpath(root)
        self.read_roots = tuple(posixpath.normpath(r) for r in read_roots)
        self.default_timeout = default_timeout

    async def _get_box(self) -> Box:
        """The sandbox, opened once; parallel tool calls wait for the same opening."""
        async with self._opening:
            if self._box is None:
                # Not cached on failure, so the next tool call tries again.
                self._box = await self._open_box()
            return self._box

    def _inside(self, path: str) -> str:
        """`path` as an absolute sandbox path, refused unless it is in the workspace.

        Lexical only: the sandbox is the security boundary, so a symlink out of the folder reaches
        nothing but the session's own sandbox (ADR-035).
        """
        resolved = _normalize(self.root, path)
        if not _within(resolved, self.root):
            raise ToolError(f"Path '{path}' is outside the workspace.")
        return resolved

    def _readable(self, path: str) -> str:
        """`path` resolved for reading: inside the workspace, or (given absolute) inside a read-only root."""
        try:
            return self._inside(path)
        except ToolError:
            resolved = _normalize(self.root, path)
            if posixpath.isabs(path) and any(_within(resolved, r) for r in self.read_roots):
                return resolved
            raise

    async def stat(self, path: str) -> PathKind | None:
        """File, folder or nothing."""
        target = self._readable(path)
        return await (await self._get_box()).kind(target)

    async def read_bytes(self, path: str) -> bytes:
        """The whole file, or the same `ToolError`s as `LocalWorkspace`."""
        target = self._readable(path)
        box = await self._get_box()
        match await box.kind(target):  # TS: `match` is a switch that compares values (and shapes)
            case None:
                raise ToolError(f"File not found: {path}")
            case "dir":
                raise ToolError(f"{path} is a directory, not a file.")
            case "file":
                return await box.download(target)

    async def list_dir(self, path: str) -> list[DirEntry]:
        """The folder's entries, or the same `ToolError`s as `LocalWorkspace`."""
        target = self._readable(path)
        box = await self._get_box()
        match await box.kind(target):
            case None:
                raise ToolError(f"Path not found: {path}")
            case "file":
                raise ToolError(f"Not a directory: {path}")
            case "dir":
                return await box.entries(target)

    async def update_file[T](self, path: str, change: Callable[[bytes | None], tuple[bytes, T]]) -> T:
        """Download, change and upload under the file's lock (in-process: one server owns a run, ADR-029)."""
        target = self._inside(path)
        box = await self._get_box()
        async with key_lock(f"sandbox:{box.id}:{target}"):
            kind = await box.kind(target)
            if kind == "dir":
                raise ToolError(f"{path} is a directory, not a file.")
            current = await box.download(target) if kind == "file" else None
            # TS: asyncify runs `change` (an edit's diff on a big file, say) on a worker thread.
            data, result = await asyncify(change)(current)
            await box.upload(target, data)
            return result

    async def ripgrep(self, args: Sequence[str], *, cwd: str) -> AsyncGenerator[str]:
        """Run the sandbox's `rg` in `cwd` and yield its lines as they arrive; stopping early kills it."""
        folder = self._readable(cwd)
        box = await self._get_box()
        chunks: asyncio.Queue[str | None] = asyncio.Queue()  # None marks the end of the output
        stderr: list[str] = []

        async def run() -> int:
            """rg to completion, then the end marker."""
            try:
                return await box.run(
                    shlex.join(["rg", *args]), cwd=folder, on_stdout=chunks.put_nowait, on_stderr=stderr.append
                )
            finally:
                chunks.put_nowait(None)

        task = asyncio.create_task(run())
        try:
            pending = ""
            while (chunk := await chunks.get()) is not None:
                lines = (pending + chunk).split("\n")
                pending = lines.pop()  # the last piece may be half a line; it waits for the next chunk
                for line in lines:
                    yield line
            if pending:
                yield pending
            code = await task
            if code == 127:
                raise ToolError("ripgrep (rg) is not installed in the sandbox.")
            if code not in (0, 1):  # 1 only means "nothing matched"
                raise ToolError("".join(stderr).strip() or f"ripgrep exited with code {code}")
        finally:
            # The caller stopped early or the run was cancelled: cancelling `run` kills rg in the sandbox.
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    async def exec(self, command: str, *, timeout: float | None, output: Path) -> int:
        """Run `command` with bash in the workspace folder, its output copied into `output` as it arrives.

        A command given no timeout still gets `default_timeout`: in a sandbox, a background child holding
        the output open would otherwise keep the call open forever (ADR-004 spike, question 2).
        """
        box = await self._get_box()
        with output.open("wb") as out:

            def write(chunk: str) -> None:
                """Append one chunk; small writes, so done on the event loop."""
                out.write(chunk.encode())
                out.flush()  # bash's live view reads the file while the command runs

            # One stream, in the order written: a vendor's separate stdout and stderr pipes can reorder them.
            merged = f"exec 2>&1\n{command}"
            async with asyncio.timeout(timeout or self.default_timeout):
                return await box.run(merged, cwd=self.root, on_stdout=write, on_stderr=write)


def _normalize(root: str, path: str) -> str:
    """`path` made absolute against `root`, with `.` and `..` folded away."""
    joined = path if posixpath.isabs(path) else posixpath.join(root, path)
    normalized = posixpath.normpath(joined)
    # POSIX keeps a leading `//` as is; it means the same folder as `/`.
    return "/" + normalized.lstrip("/")


def _within(path: str, folder: str) -> bool:
    """Whether `path` is `folder` or inside it (both normalized)."""
    return path == folder or path.startswith(folder.rstrip("/") + "/")
