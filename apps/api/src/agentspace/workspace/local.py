"""`LocalWorkspace`: a session's workspace as a folder on this server's disk (ADR-004).

Commands run on the host as the API's user. That is not a sandbox: a command can reach
anything the API's user can (ADR-003 step 3), which is why `bash` is off in production.
"""

import asyncio
import os
import shutil
import signal
from collections.abc import AsyncGenerator, Callable, Sequence
from pathlib import Path

from asyncer import asyncify

from agentspace.agent.tool import ToolError
from agentspace.workspace import DirEntry, PathKind
from agentspace.workspace.file_lock import file_lock
from agentspace.workspace.paths import resolve_in_workspace, resolve_readable

# Environment variables a command inherits; everything else (API keys included) stays with the server.
ENV_ALLOWLIST = ("PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LC_ALL", "TERM", "TMPDIR")


class LocalWorkspace:
    """The folder `root` on this server, plus read-only folders (skills) that reads may reach."""

    def __init__(self, root: Path, read_roots: Sequence[Path] = ()) -> None:
        """`root` must exist; `read_roots` are folders outside it that may be read but never changed."""
        self.root = root
        self.read_roots = tuple(read_roots)

    def _readable(self, path: str) -> Path:
        """`path` resolved for reading: inside the workspace or a read-only root."""
        return resolve_readable(self.root, self.read_roots, path)

    async def stat(self, path: str) -> PathKind | None:
        """File, folder or nothing; symlinks are followed."""
        target = self._readable(path)
        # TS: these checks block (like fs.statSync); asyncify runs them on a worker thread.
        return await asyncify(_kind)(target)

    async def read_bytes(self, path: str) -> bytes:
        """The whole file, read off the event loop."""
        return await asyncify(_read)(self._readable(path), path)

    async def list_dir(self, path: str) -> list[DirEntry]:
        """The folder's entries, read off the event loop."""
        return await asyncify(_list)(self._readable(path), path)

    async def update_file[T](self, path: str, change: Callable[[bytes | None], tuple[bytes, T]]) -> T:
        """Read, change and write under the file's lock, on a worker thread."""
        target = resolve_in_workspace(self.root, path)
        # The lock spans the whole read-modify-write, so a parallel call can't write in between.
        # TS: `async with` is like `await using`: the lock is released however the block exits.
        async with file_lock(target):
            return await asyncify(_update)(target, path, change)

    async def ripgrep(self, args: Sequence[str], *, cwd: str) -> AsyncGenerator[str]:
        """Run the server's `rg` in `cwd` and yield its lines; stopping early kills it."""
        folder = self._readable(cwd)
        rg = shutil.which("rg")  # TS: like the `which` npm package; None when not on PATH
        if rg is None:
            raise ToolError("ripgrep (rg) is not installed on the server. Install it, e.g. `brew install ripgrep`.")
        proc = await asyncio.create_subprocess_exec(
            rg,
            *args,
            cwd=folder,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,  # small with --no-messages: only fatal errors are printed
        )
        assert proc.stdout is not None and proc.stderr is not None  # set because of PIPE; tells the type checker
        try:
            # TS: `async for` over a StreamReader yields one line (bytes) at a time, like readline's `for await`.
            async for line in proc.stdout:
                yield line.decode("utf-8", errors="replace").rstrip("\n")
            stderr = await proc.stderr.read()
            # Exit 1 only means "nothing matched".
            if await proc.wait() not in (0, 1):
                message = stderr.decode(errors="replace").strip()
                raise ToolError(message or f"ripgrep exited with code {proc.returncode}")
        finally:
            # The caller stopped early (a limit) or the run was cancelled: stop rg instead of letting it finish.
            if proc.returncode is None:
                proc.kill()
                await proc.wait()

    async def exec(self, command: str, *, timeout: float | None, output: Path) -> int:
        """Run `command` with bash in the workspace folder; the whole process group is killed at the end."""
        # The command writes straight to the file: no pipe for a background child to hold open, which
        # would make `proc.wait()` hang (asyncio waits for pipes to close), and no writes on the event loop.
        # TS: `with` closes the file when the block ends, like `using` / try-finally.
        with output.open("wb") as out:
            # TS: like child_process.spawn("bash", ["-c", command], { detached: true, ... }).
            proc = await asyncio.create_subprocess_exec(
                "bash",
                "-c",
                command,
                cwd=self.root,
                env={key: os.environ[key] for key in ENV_ALLOWLIST if key in os.environ},  # TS: a dict comprehension
                stdin=asyncio.subprocess.DEVNULL,
                stdout=out,
                stderr=asyncio.subprocess.STDOUT,  # one stream, in the order the command wrote it
                start_new_session=True,  # own process group, so killpg reaches every child
            )
        try:
            # TS: asyncio.timeout(None) never fires, like skipping the setTimeout.
            async with asyncio.timeout(timeout):
                await proc.wait()
        finally:
            # Background children outlive the shell; on timeout or cancellation the shell is still running too.
            _kill_group(proc.pid)
            if proc.returncode is None:
                await proc.wait()
        code = proc.returncode
        assert code is not None  # set once wait() returned; the assert tells the type checker
        # A signal-killed shell reports -N; report 128 + N as shells do, so it never reads as success.
        return code if code >= 0 else 128 - code


def _kind(target: Path) -> PathKind | None:
    """What is at `target`, following symlinks."""
    if target.is_dir():
        return "dir"
    return "file" if target.exists() else None


def _read(target: Path, shown_path: str) -> bytes:
    """The file's bytes, or a `ToolError` the model can act on."""
    if not target.exists():
        raise ToolError(f"File not found: {shown_path}")
    if target.is_dir():
        raise ToolError(f"{shown_path} is a directory, not a file.")
    return target.read_bytes()


def _list(target: Path, shown_path: str) -> list[DirEntry]:
    """The folder's entries; `is_dir()` follows symlinks, so a link to a folder counts as one."""
    if not target.exists():
        raise ToolError(f"Path not found: {shown_path}")
    if not target.is_dir():
        raise ToolError(f"Not a directory: {shown_path}")
    return [DirEntry(child.name, child.is_dir()) for child in target.iterdir()]


def _update[T](target: Path, shown_path: str, change: Callable[[bytes | None], tuple[bytes, T]]) -> T:
    """One read-modify-write of `target`; nothing is written if `change` raises."""
    if target.is_dir():
        raise ToolError(f"{shown_path} is a directory, not a file.")
    current = target.read_bytes() if target.exists() else None
    data, result = change(current)
    target.parent.mkdir(parents=True, exist_ok=True)  # TS: fs.mkdirSync(dirname, { recursive: true })
    target.write_bytes(data)
    return result


def _kill_group(pid: int) -> None:
    """SIGKILL every process in the command's group; fine if they are already gone."""
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):  # TS: a tuple catches either class, like two instanceof checks
        pass
