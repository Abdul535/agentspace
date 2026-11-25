"""The `Workspace` protocol: the only way tools touch a session's files or run commands (ADR-004, ADR-034).

Tools hand a workspace the path string the model sent; the workspace resolves it, refuses
anything outside, and does the work. `LocalWorkspace` does it on this server's disk. A
sandbox backend (ADR-022 step 28) will do it inside a remote sandbox, and no tool changes.
"""

from collections.abc import AsyncGenerator, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

# TS: type PathKind = "file" | "dir"
type PathKind = Literal["file", "dir"]


@dataclass(frozen=True)
class DirEntry:
    """One entry of a directory listing."""

    name: str
    is_dir: bool
    """True for a folder, and for a symlink to one."""


# TS: like an interface — any class with these methods fits, no "implements" needed.
class Workspace(Protocol):
    """A session's files and commands, wherever they live.

    Every `path` is the string the model sent: relative to the workspace root, or absolute.
    A path outside the workspace raises `ToolError("Path '<path>' is outside the workspace.")`.
    The read methods (`stat`, `read_bytes`, `list_dir`, `ripgrep`) may also reach the read-only
    roots (loaded skills' folders, ADR-016); `update_file` and `exec` never can.
    Every error a tool should show the model is a `ToolError` naming the path as given.
    """

    async def stat(self, path: str) -> PathKind | None:
        """Whether `path` is a file or a folder (following symlinks), or None if nothing is there."""
        ...

    async def read_bytes(self, path: str) -> bytes:
        """The whole file. Raises `ToolError` "File not found: …" or "… is a directory, not a file."."""
        ...

    async def list_dir(self, path: str) -> list[DirEntry]:
        """The folder's entries, unsorted. Raises `ToolError` "Path not found: …" or "Not a directory: …"."""
        ...

    async def update_file[T](self, path: str, change: Callable[[bytes | None], tuple[bytes, T]]) -> T:
        """Read the file, write what `change` returns, and hand back its second value; creates parent folders.

        `change` gets the current bytes, or None when the file doesn't exist; it may raise `ToolError`
        to leave the file untouched. Calls on the same file run one at a time, so parallel `write` and
        `edit` calls never lose each other's changes. Raises `ToolError` "… is a directory, not a file.".
        """
        ...

    def ripgrep(self, args: Sequence[str], *, cwd: str) -> AsyncGenerator[str]:
        """Run `rg` with `args` in the folder `cwd` and yield its output lines.

        Iterate it inside `contextlib.aclosing(...)`, so stopping early stops rg at once.
        Exit code 1 (no match) is not an error; anything else, or rg missing, raises `ToolError`.
        """
        ...

    async def exec(self, command: str, *, timeout: float | None, output: Path) -> int:
        """Run a shell command in the workspace root and return its exit code (128 + N if killed by signal N).

        stdout and stderr go to the local file `output`, merged in the order written, while it runs.
        Raises `TimeoutError` after `timeout` seconds. Every process the command started is gone on return.
        """
        ...
