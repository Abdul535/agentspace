"""The `ls` tool: list one directory, after pi's `packages/coding-agent/src/core/tools/ls.ts`."""

from pathlib import Path
from typing import Annotated

from asyncer import asyncify
from pydantic import BaseModel, Field

from agentspace.agent.tool import Tool, ToolContext, ToolError, ToolResult
from agentspace.tools.output import head_within_cap, with_notices
from agentspace.tools.paths import resolve_in_workspace
from agentspace.tools.read import MAX_BYTES

DEFAULT_LIMIT = 500


class LsParams(BaseModel):
    """Arguments of the `ls` tool."""

    path: Annotated[
        str | None, Field(description="Directory to list, relative to the workspace (default: its root)")
    ] = None
    limit: Annotated[
        int, Field(ge=1, description=f"Maximum number of entries to return (default: {DEFAULT_LIMIT})")
    ] = DEFAULT_LIMIT


# TS: class LsTool extends Tool<LsParams>
class LsTool(Tool[LsParams]):
    """Lists a directory's entries, folders marked with a trailing `/`."""

    name = "ls"
    description = (
        "List directory contents. Returns entries sorted alphabetically, with '/' suffix for directories. "
        f"Includes dotfiles. Output is truncated to {DEFAULT_LIMIT} entries or {MAX_BYTES // 1024}KB "
        "(whichever is hit first)."
    )
    params = LsParams

    async def execute(self, args: LsParams, ctx: ToolContext) -> ToolResult:
        """Read the directory off the event loop."""
        shown_path = args.path or "."
        target = resolve_in_workspace(ctx.workspace, shown_path)
        # TS: os.scandir blocks (like fs.readdirSync); asyncify runs it on a worker thread and awaits it.
        return await asyncify(list_dir)(target, shown_path, args.limit)


def list_dir(target: Path, shown_path: str, limit: int) -> ToolResult:
    """The first `limit` entries, case-insensitively sorted, with notices for anything cut."""
    if not target.exists():
        raise ToolError(f"Path not found: {shown_path}")
    if not target.is_dir():
        raise ToolError(f"Not a directory: {shown_path}")

    # TS: sorted(..., key=f) sorts by f(x), like .sort((a, b) => f(a).localeCompare(f(b))).
    children = sorted(target.iterdir(), key=lambda p: p.name.lower())
    # is_dir() follows symlinks, so a link to a folder is listed as one.
    entries = [child.name + ("/" if child.is_dir() else "") for child in children[:limit]]
    if not entries:
        return ToolResult.text("(empty directory)", {"count": 0})

    text, cut = head_within_cap(entries)
    notices: list[str] = []
    limit_reached = len(children) > limit
    if limit_reached:
        notices.append(f"{limit} entries limit reached. Use limit={limit * 2} for more")
    if cut:
        notices.append(f"{MAX_BYTES // 1024}KB limit reached")
    details = {"count": len(entries), "total": len(children), "limit_reached": limit_reached, "truncated": cut}
    return ToolResult.text(with_notices(text, notices), details)
