"""Keeps file tools inside the session's workspace folder."""

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
