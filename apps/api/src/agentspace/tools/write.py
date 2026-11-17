"""The `write` tool, as pi's `packages/coding-agent/src/core/tools/write.ts`."""

from pathlib import Path
from typing import Annotated

from asyncer import asyncify
from pydantic import BaseModel, Field

from agentspace.agent.tool import Tool, ToolContext, ToolError, ToolResult
from agentspace.tools.file_lock import file_lock
from agentspace.tools.paths import resolve_in_workspace


class WriteParams(BaseModel):
    """Arguments of the `write` tool."""

    path: Annotated[str, Field(description="Path to the file to write, relative to the workspace")]
    content: Annotated[str, Field(description="Content to write to the file")]


# TS: class WriteTool extends Tool<WriteParams>
class WriteTool(Tool[WriteParams]):
    """Creates or overwrites a file in the workspace."""

    name = "write"
    description = (
        "Write content to a file. Creates the file if it doesn't exist, overwrites it if it does. "
        "Creates parent directories as needed. Use only for new files or complete rewrites."
    )
    params = WriteParams
    snippet = "Create or overwrite files"
    guidelines = ("Use write only for new files or complete rewrites.",)

    async def execute(self, args: WriteParams, ctx: ToolContext) -> ToolResult:
        """Write the file off the event loop and report how many bytes were written."""
        path = resolve_in_workspace(ctx.workspace, args.path)
        # TS: write_text blocks (like fs.writeFileSync); asyncify runs it on a worker thread and awaits it.
        async with file_lock(path):
            size = await asyncify(write_text)(path, args.path, args.content)
        return ToolResult.text(f"Wrote {size} bytes to {args.path}", {"path": args.path, "bytes": size})


def write_text(path: Path, shown_path: str, content: str) -> int:
    """Write UTF-8 text, creating parent folders; returns the byte count."""
    if path.is_dir():
        raise ToolError(f"{shown_path} is a directory.")
    path.parent.mkdir(parents=True, exist_ok=True)  # TS: fs.mkdirSync(dirname, { recursive: true })
    data = content.encode()
    path.write_bytes(data)
    return len(data)
