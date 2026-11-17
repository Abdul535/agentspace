"""The `read` tool. Same limits and paging as pi's `packages/coding-agent/src/core/tools/read.ts`, text files only."""

from pathlib import Path
from typing import Annotated, Any

from asyncer import asyncify
from pydantic import BaseModel, Field

from agentspace.agent.tool import Tool, ToolContext, ToolError, ToolResult
from agentspace.tools.paths import resolve_in_workspace

# Output caps per call, so one read can't flood the model's context; whichever is hit first wins.
MAX_LINES = 2000
MAX_BYTES = 50 * 1024
# A NUL byte in the first 8 KiB marks a file as binary.
BINARY_SNIFF_BYTES = 8192


# TS: z.object({ path: z.string().describe(...), offset: z.number().int().min(1).optional(), ... })
class ReadParams(BaseModel):
    """Arguments of the `read` tool."""

    path: Annotated[str, Field(description="Path to the file to read, relative to the workspace")]
    offset: Annotated[int | None, Field(ge=1, description="Line number to start reading from (1-indexed)")] = None
    limit: Annotated[int | None, Field(ge=1, description="Maximum number of lines to read")] = None


# TS: class ReadTool extends Tool<ReadParams>
class ReadTool(Tool[ReadParams]):
    """Returns a page of a text file in the workspace."""

    name = "read"
    description = (
        f"Read the contents of a text file. Output is truncated to {MAX_LINES} lines or "
        f"{MAX_BYTES // 1024}KB, whichever is hit first. Use offset/limit for large files. "
        "When you need the full file, continue with offset until complete."
    )
    params = ReadParams
    snippet = "Read file contents"
    guidelines = ("Use read to examine files instead of cat or sed.",)

    async def execute(self, args: ReadParams, ctx: ToolContext) -> ToolResult:
        """Read the file off the event loop."""
        path = resolve_in_workspace(ctx.workspace, args.path)
        # TS: the file I/O below blocks (like fs.readFileSync); asyncify runs it on a worker thread and awaits it.
        return await asyncify(read_text_page)(path, args.path, args.offset, args.limit)


def read_text_page(path: Path, shown_path: str, offset: int | None, limit: int | None) -> ToolResult:
    """The requested lines, cut at the caps, with a note saying where to continue.

    `details` holds the shown line range, the file's line count and whether more is left.
    """
    # TS: f"...{x}..." strings are template literals: `...${x}...`.
    if not path.exists():
        raise ToolError(f"File not found: {shown_path}")
    if path.is_dir():
        raise ToolError(f"{shown_path} is a directory, not a file.")
    data = path.read_bytes()  # TS: a Buffer; .decode() below is buffer.toString("utf8")
    if b"\0" in data[:BINARY_SNIFF_BYTES]:
        raise ToolError(f"{shown_path} looks like a binary file; only text files can be read.")
    lines = data.decode("utf-8", errors="replace").splitlines()
    if not lines:
        return ToolResult.text("(empty file)", page_details(shown_path, 0, 0, 0, truncated=False))

    start = (offset or 1) - 1
    if start >= len(lines):
        raise ToolError(f"Offset {offset} is beyond the end of the file ({len(lines)} lines).")
    end = len(lines) if limit is None else min(start + limit, len(lines))

    # Take lines until either cap would be crossed.
    shown: list[str] = []
    size = 0
    for line in lines[start:end]:  # TS: lines.slice(start, end)
        line_bytes = len(line.encode()) + 1
        if len(shown) >= MAX_LINES or size + line_bytes > MAX_BYTES:
            break
        shown.append(line)
        size += line_bytes

    # The first line alone is over the byte cap: return its start rather than nothing.
    if not shown:
        line_kb = len(lines[start].encode()) / 1024
        head = lines[start].encode()[:MAX_BYTES].decode(errors="ignore")
        return ToolResult.text(
            f"{head}\n\n[Line {start + 1} is {line_kb:.1f}KB, over the {MAX_BYTES // 1024}KB limit; "
            "showing its first part only.]",
            page_details(shown_path, start + 1, start + 1, len(lines), truncated=True),
        )

    # Tell the model how to get the rest, if anything is left.
    last = start + len(shown)
    text = "\n".join(shown)
    if last < end:
        text += f"\n\n[Showing lines {start + 1}-{last} of {len(lines)}. Use offset={last + 1} to continue.]"
    elif last < len(lines):
        text += f"\n\n[{len(lines) - last} more lines in file. Use offset={last + 1} to continue.]"
    return ToolResult.text(text, page_details(shown_path, start + 1, last, len(lines), truncated=last < len(lines)))


def page_details(path: str, first_line: int, last_line: int, total_lines: int, *, truncated: bool) -> dict[str, Any]:
    """The `details` of a read: which lines were shown and whether the file goes on."""
    return {
        "path": path,
        "first_line": first_line,
        "last_line": last_line,
        "total_lines": total_lines,
        "truncated": truncated,
    }
