"""The `write` tool, as pi's `packages/coding-agent/src/core/tools/write.ts`."""

from typing import Annotated

from pydantic import BaseModel, Field

from agentspace.agent.tool import Tool, ToolContext, ToolResult


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
        """Write the file as UTF-8, replacing whatever was there, and report how many bytes were written."""
        data = args.content.encode()
        # The old content is ignored; going through update_file still waits for an `edit` of the same file.
        await ctx.workspace.update_file(args.path, lambda _old: (data, None))
        size = len(data)
        return ToolResult.text(f"Wrote {size} bytes to {args.path}", {"path": args.path, "bytes": size})
