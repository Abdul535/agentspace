"""`HostBox`: a fake sandbox on this machine, for testing `SandboxWorkspace` and the lifecycle offline.

Its "sandbox paths" are host paths, so a test can make files with the workspace's own `exec`.
It keeps the `Box` contract a vendor adapter must: output streams to the callbacks, and every
process the command started is killed when it returns or is cancelled.
"""

import asyncio
import contextlib
import os
import signal
from pathlib import Path

from agentspace.agent.tool import ToolError
from agentspace.workspace import DirEntry, PathKind
from agentspace.workspace.sandbox import OnOutput


class HostBox:
    """A `Box` whose files and commands are this machine's."""

    def __init__(self, box_id: str = "host") -> None:
        """`box_id` names it in file locks, like a vendor's sandbox id."""
        self._id = box_id
        self.commands: list[str] = []
        """Every command run, for tests that check what the workspace sent."""

    @property
    def id(self) -> str:
        """The fake sandbox's id."""
        return self._id

    async def kind(self, path: str) -> PathKind | None:
        """File, folder (following symlinks) or None."""
        target = Path(path)
        if target.is_dir():
            return "dir"
        return "file" if target.exists() else None

    async def download(self, path: str) -> bytes:
        """The file's bytes; a missing file is a vendor failure here, as the contract says it must exist."""
        try:
            return Path(path).read_bytes()
        except OSError as exc:
            raise ToolError(f"The sandbox failed (download): {exc}") from exc

    async def upload(self, path: str, data: bytes) -> None:
        """Write, creating parent folders."""
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_bytes(data)

    async def entries(self, path: str) -> list[DirEntry]:
        """The folder's entries."""
        return [DirEntry(child.name, child.is_dir()) for child in Path(path).iterdir()]

    async def run(self, command: str, *, cwd: str, on_stdout: OnOutput, on_stderr: OnOutput) -> int:
        """bash in its own process group, piped so output streams; the group is killed at the end."""
        self.commands.append(command)
        proc = await asyncio.create_subprocess_exec(
            "bash",
            "-c",
            command,
            cwd=cwd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        assert proc.stdout is not None and proc.stderr is not None

        async def pump(stream: asyncio.StreamReader, on_output: OnOutput) -> None:
            """Hand each chunk to its callback until the pipe closes."""
            while chunk := await stream.read(4096):
                on_output(chunk.decode(errors="replace"))

        pumps = [asyncio.create_task(pump(proc.stdout, on_stdout)), asyncio.create_task(pump(proc.stderr, on_stderr))]
        try:
            # Polled, like the Daytona adapter: `proc.wait()` also waits for the pipes, which a child may hold.
            while (code := proc.returncode) is None:
                await asyncio.sleep(0.02)
            # Like the Daytona adapter: the command is done, so its leftover children go, and the pipes close.
            _kill_group(proc.pid)
            await asyncio.gather(*pumps)
            return code if code >= 0 else 128 - code
        finally:
            _kill_group(proc.pid)
            for task in pumps:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            if proc.returncode is None:
                await proc.wait()


def _kill_group(pid: int) -> None:
    """SIGKILL the command's process group; fine if it is gone."""
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pid, signal.SIGKILL)
