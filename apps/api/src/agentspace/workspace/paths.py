"""Keeps file tools inside the session's workspace folder."""

from collections.abc import Sequence
from pathlib import Path

from agentspace.agent.tool import ToolError


def resolve_in_workspace(workspace: Path, path: str) -> Path:
    """Resolve `path` (relative to the workspace, or absolute) and refuse anything outside the workspace.

    Symlinks are resolved first, so a link inside the workspace cannot point a tool outside it.
    """
    # TS: pathlib.Path is node:path and node:fs in one object; `root / candidate` is path.join.
    root = workspace.resolve()
    candidate = Path(path)
    resolved = (candidate if candidate.is_absolute() else root / candidate).resolve()
    if not resolved.is_relative_to(root):
        raise ToolError(f"Path '{path}' is outside the workspace.")
    return resolved


def resolve_readable(workspace: Path, read_roots: Sequence[Path], path: str) -> Path:
    """Like `resolve_in_workspace`, but also allow paths inside the read-only roots (skill folders, ADR-016).

    Relative paths still resolve against the workspace; only absolute ones can reach a root.
    """
    try:
        return resolve_in_workspace(workspace, path)
    except ToolError:
        resolved = Path(path).resolve()
        # TS: any(...) over a generator is like roots.some((root) => ...)
        if Path(path).is_absolute() and any(resolved.is_relative_to(root.resolve()) for root in read_roots):
            return resolved
        raise
