"""The `grep` tool: search file contents with ripgrep, after pi's `packages/coding-agent/src/core/tools/grep.ts`."""

import json
from base64 import b64decode
from contextlib import aclosing
from typing import Annotated, Any

from pydantic import BaseModel, Field

from agentspace.agent.tool import Tool, ToolContext, ToolError, ToolResult
from agentspace.tools.output import head_within_cap, with_notices
from agentspace.tools.paths import resolve_in_workspace
from agentspace.tools.read import MAX_BYTES
from agentspace.tools.ripgrep import rg_lines, strip_dot_slash

DEFAULT_LIMIT = 100
MAX_LINE_CHARS = 500


class GrepParams(BaseModel):
    """Arguments of the `grep` tool."""

    pattern: Annotated[str, Field(description="Search pattern (regex or literal string)")]
    path: Annotated[
        str | None, Field(description="Directory or file to search, relative to the workspace (default: all of it)")
    ] = None
    glob: Annotated[
        str | None,
        Field(
            description="Filter files by glob pattern relative to path, e.g. 'src/**/*.ts'. "
            "Without '/', e.g. '*.ts', it matches file names at any depth"
        ),
    ] = None
    ignore_case: Annotated[bool, Field(description="Case-insensitive search")] = False
    literal: Annotated[bool, Field(description="Treat pattern as a literal string instead of a regex")] = False
    context: Annotated[int, Field(ge=0, description="Number of lines to show before and after each match")] = 0
    limit: Annotated[
        int, Field(ge=1, description=f"Maximum number of matches to return (default: {DEFAULT_LIMIT})")
    ] = DEFAULT_LIMIT


# TS: class GrepTool extends Tool<GrepParams>
class GrepTool(Tool[GrepParams]):
    """Finds lines matching a pattern in the workspace's files."""

    name = "grep"
    description = (
        "Search file contents for a pattern. Returns matching lines with file paths and line numbers. "
        f"Respects .gitignore. Output is truncated to {DEFAULT_LIMIT} matches or {MAX_BYTES // 1024}KB "
        f"(whichever is hit first). Long lines are truncated to {MAX_LINE_CHARS} chars."
    )
    params = GrepParams
    snippet = "Search file contents for patterns (respects .gitignore)"

    async def execute(self, args: GrepParams, ctx: ToolContext) -> ToolResult:
        """Run rg and format its matches as `path:line: text`, context lines as `path-line- text`."""
        shown_path = args.path or "."
        target = resolve_in_workspace(ctx.workspace, shown_path)
        if not target.exists():
            raise ToolError(f"Path not found: {shown_path}")
        # rg prints paths relative to where it searches: a directory searches from inside it, a file from its folder.
        cwd, search = (target, ".") if target.is_dir() else (target.parent, target.name)

        flags = ["--json"]
        if args.ignore_case:
            flags.append("--ignore-case")
        if args.literal:
            flags.append("--fixed-strings")
        if args.glob:
            flags += ["--glob", strip_dot_slash(args.glob)]
        if args.context:
            flags += ["--context", str(args.context)]

        lines: list[str] = []
        matches = 0
        limit_reached = long_lines = False
        last_file, window_end = "", 0  # the last kept match's file and the end of its trailing context

        # TS: aclosing is `await using`: when the block exits (even via break), the generator is closed and rg killed.
        async with aclosing(rg_lines(flags, [args.pattern, search], cwd)) as events:
            async for raw in events:
                event = json.loads(raw)
                kind = event["type"]
                if kind not in ("match", "context"):
                    continue  # begin/end/summary events
                data = event["data"]
                path = rg_text(data["path"]).removeprefix("./")
                number: int = data["line_number"]
                if matches >= args.limit:
                    if kind == "match":
                        limit_reached = True  # a match past the limit proves results were cut
                        break
                    if path != last_file or number > window_end:
                        continue  # leading context of a match we won't show
                text = rg_text(data["lines"]).rstrip("\r\n")
                if len(text) > MAX_LINE_CHARS:
                    text, long_lines = text[:MAX_LINE_CHARS] + "…", True
                sep = ":" if kind == "match" else "-"
                lines.append(f"{path}{sep}{number}{sep} {text}")
                if kind == "match":
                    matches += 1
                    last_file, window_end = path, number + args.context

        if not matches:
            return ToolResult.text("No matches found", {"matches": 0})
        text, cut = head_within_cap(lines)
        notices: list[str] = []
        if limit_reached:
            more = f"Use limit={args.limit * 2} for more, or refine pattern"
            notices.append(f"{args.limit} matches limit reached. {more}")
        if cut:
            notices.append(f"{MAX_BYTES // 1024}KB limit reached")
        if long_lines:
            notices.append(f"Some lines truncated to {MAX_LINE_CHARS} chars. Use read tool to see full lines")
        details = {"matches": matches, "limit_reached": limit_reached, "truncated": cut}
        return ToolResult.text(with_notices(text, notices), details)


def rg_text(field: dict[str, Any]) -> str:
    """Text of an rg JSON field: `text`, or `bytes` (base64) when it isn't valid UTF-8."""
    if "text" in field:
        return field["text"]
    return b64decode(field["bytes"]).decode("utf-8", errors="replace")
