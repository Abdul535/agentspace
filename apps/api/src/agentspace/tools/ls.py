"""The `ls` tool: list one directory, after pi's `packages/coding-agent/src/core/tools/ls.ts`."""

from typing import Annotated

from pydantic import BaseModel, Field

from agentspace.agent.tool import Tool, ToolContext, ToolResult
from agentspace.tools.output import head_within_cap, with_notices
from agentspace.tools.read import MAX_BYTES
from agentspace.workspace import DirEntry

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
    snippet = "List directory contents"

    async def execute(self, args: LsParams, ctx: ToolContext) -> ToolResult:
        """List the directory through the workspace."""
        return render_listing(await ctx.workspace.list_dir(args.path or "."), args.limit)


def render_listing(found: list[DirEntry], limit: int) -> ToolResult:
    """The first `limit` entries, case-insensitively sorted, with notices for anything cut."""
    # TS: sorted(..., key=f) sorts by f(x), like .sort((a, b) => f(a).localeCompare(f(b))).
    children = sorted(found, key=lambda entry: entry.name.lower())
    entries = [child.name + ("/" if child.is_dir else "") for child in children[:limit]]
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
