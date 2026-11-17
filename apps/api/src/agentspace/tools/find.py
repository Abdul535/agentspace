"""The `find` tool: list files matching a glob, after pi's `packages/coding-agent/src/core/tools/find.ts`.

pi runs `fd`; `rg --files` does the same job with the binary `grep` already needs.
"""

from contextlib import aclosing
from typing import Annotated

from pydantic import BaseModel, Field

from agentspace.agent.tool import Tool, ToolContext, ToolError, ToolResult
from agentspace.tools.output import head_within_cap, with_notices
from agentspace.tools.paths import resolve_in_workspace
from agentspace.tools.read import MAX_BYTES
from agentspace.tools.ripgrep import rg_lines, strip_dot_slash

DEFAULT_LIMIT = 1000


class FindParams(BaseModel):
    """Arguments of the `find` tool."""

    pattern: Annotated[
        str,
        Field(
            description="Glob pattern relative to the search directory, e.g. 'src/**/*.ts'. "
            "Without '/', e.g. '*.ts', it matches file names at any depth"
        ),
    ]
    path: Annotated[
        str | None, Field(description="Directory to search in, relative to the workspace (default: all of it)")
    ] = None
    limit: Annotated[int, Field(ge=1, description=f"Maximum number of results (default: {DEFAULT_LIMIT})")] = (
        DEFAULT_LIMIT
    )


# TS: class FindTool extends Tool<FindParams>
class FindTool(Tool[FindParams]):
    """Lists the workspace's files whose path matches a glob."""

    name = "find"
    description = (
        "Search for files by glob pattern. Returns matching file paths relative to the search directory. "
        f"Respects .gitignore. Output is truncated to {DEFAULT_LIMIT} results or {MAX_BYTES // 1024}KB "
        "(whichever is hit first)."
    )
    params = FindParams

    async def execute(self, args: FindParams, ctx: ToolContext) -> ToolResult:
        """Run `rg --files` filtered by the glob, stopping one past the limit."""
        shown_path = args.path or "."
        target = resolve_in_workspace(ctx.workspace, shown_path)
        if not target.exists():
            raise ToolError(f"Path not found: {shown_path}")
        if not target.is_dir():
            raise ToolError(f"Not a directory: {shown_path}")

        paths: list[str] = []
        limit_reached = False
        flags = ["--files", "--glob", strip_dot_slash(args.pattern)]
        async with aclosing(rg_lines(flags, ["."], target)) as lines:
            async for line in lines:
                # One more than the limit proves the list was cut; stop rg there.
                if len(paths) == args.limit:
                    limit_reached = True
                    break
                paths.append(line.removeprefix("./"))

        if not paths:
            return ToolResult.text("No files found matching pattern", {"count": 0})
        text, cut = head_within_cap(paths)
        notices: list[str] = []
        if limit_reached:
            more = f"Use limit={args.limit * 2} for more, or refine pattern"
            notices.append(f"{args.limit} results limit reached. {more}")
        if cut:
            notices.append(f"{MAX_BYTES // 1024}KB limit reached")
        details = {"count": len(paths), "limit_reached": limit_reached, "truncated": cut}
        return ToolResult.text(with_notices(text, notices), details)
