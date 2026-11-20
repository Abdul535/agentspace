"""MCP tools as `Tool`s the loop can call, after pi's `extensions/mcp/tools.ts` and `mcp/src/naming.ts` (ADR-017).

Arguments go to the server as the model sent them: the server validates them
against its own schema. Results become text and images; other block types
become a short note. Long text keeps its start and end.
"""

import hashlib
import json
import re
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel, ConfigDict

from agentspace.agent.tool import Tool, ToolContext, ToolError, ToolResult
from agentspace.ai.messages import ImageContent, TextContent, UserContent
from agentspace.ai.provider import ToolSpec
from agentspace.mcp.client import CallToolResult, McpError, McpToolInfo, StdioClient
from agentspace.tools.read import MAX_BYTES

# Provider tool names allow at most 64 characters of `[A-Za-z0-9_-]`.
MAX_TOOL_NAME_LENGTH = 64
# Image types the model APIs accept (ADR-015); other images become a note.
IMAGE_TYPES = ("image/png", "image/jpeg", "image/gif", "image/webp")


def mcp_tool_name(server: str, tool: str, taken: set[str]) -> str:
    """`mcp__<server>__<tool>` with anything outside `[A-Za-z0-9_]` as `_`; hashed when too long or already taken."""
    name = re.sub(r"[^A-Za-z0-9_]", "_", f"mcp__{server}__{tool}")
    if len(name) <= MAX_TOOL_NAME_LENGTH and name not in taken:
        return name
    # Two tools can sanitize to one name (`a-b`, `a_b`); a hash of the real names tells them apart.
    digest = hashlib.sha256(f"{server}\0{tool}".encode()).hexdigest()[:8]
    return f"{name[: MAX_TOOL_NAME_LENGTH - len(digest) - 1]}_{digest}"


def cut_middle(text: str, limit: int = MAX_BYTES) -> str:
    """`text` if it fits in `limit` bytes, else its start and end around a note of how much was cut."""
    data = text.encode()
    if len(data) <= limit:
        return text
    half = limit // 2
    head, tail = data[:half].decode(errors="ignore"), data[-half:].decode(errors="ignore")
    return f"{head}\n\n[…{len(data) - 2 * half} bytes truncated…]\n\n{tail}"


def block_to_content(block: dict[str, Any]) -> UserContent:
    """One MCP content block as a block the model can read."""
    # TS: `match` on a dict pattern is like a switch on `block.type` that also destructures the fields.
    match block:
        case {"type": "text", "text": str(text)}:
            return TextContent(text=text)
        case {"type": "image", "data": str(data), "mimeType": str(mime)} if mime in IMAGE_TYPES:
            return ImageContent(data=data, mime_type=mime)
        case {"type": "resource", "resource": {"text": str(text)}}:
            return TextContent(text=text)
        case {"type": "resource_link", "uri": str(uri)}:
            return TextContent(text=f"[Resource link: {uri}]")
        case {"type": "resource", "resource": {"uri": str(uri)}}:
            return TextContent(text=f"[Binary resource {uri} omitted]")
        case {"type": kind}:
            return TextContent(text=f"[{kind} content omitted]")
        case _:
            return TextContent(text="[Unknown content omitted]")


def convert_result(result: CallToolResult) -> list[UserContent]:
    """The tool's content for the model: adjacent text joined and capped, images kept as they are."""
    blocks = [block_to_content(block) for block in result.content]
    if not blocks and result.structured_content is not None:
        blocks = [TextContent(text=json.dumps(result.structured_content, indent=2))]
    if not blocks:
        return [TextContent(text="(no output)")]
    content: list[UserContent] = []
    for block in blocks:
        # TS: `isinstance` narrows like `instanceof`; joining keeps one cap across many small text blocks.
        if isinstance(block, TextContent) and content and isinstance(content[-1], TextContent):
            content[-1] = TextContent(text=f"{content[-1].text}\n{block.text}")
        else:
            content.append(block)
    return [TextContent(text=cut_middle(b.text)) if isinstance(b, TextContent) else b for b in content]


class McpArgs(BaseModel):
    """An open object: the server checks the arguments against its own schema, so we pass them on unchanged."""

    model_config = ConfigDict(extra="allow")


# How a tool reaches its server's live client, reconnecting if the process died since (ADR-017).
# TS: type GetClient = () => Promise<StdioClient>
type GetClient = Callable[[], Awaitable[StdioClient]]


class McpTool(Tool[McpArgs]):
    """One tool of one MCP server; `name`, `description` and `snippet` are set per instance, not per class."""

    params = McpArgs

    def __init__(self, *, name: str, server: str, info: McpToolInfo, get_client: GetClient, timeout: float) -> None:
        self.name = name
        self.server = server
        self.info = info
        self.get_client = get_client
        self.timeout = timeout
        self.description = (info.description or "").strip() or info.title or f"MCP tool {info.name} from {server}"
        first_line = self.description.splitlines()[0] if self.description else ""
        self.snippet = f"[{server}] {first_line[:200]}"

    def spec(self) -> ToolSpec:
        """The server's own input schema, so the model sees exactly what the server expects."""
        spec = super().spec()
        spec.parameters = self.info.input_schema
        return spec

    async def execute(self, args: McpArgs, ctx: ToolContext) -> ToolResult:
        """Call the tool on its server. A dead server, a timeout or a server error becomes a tool error."""
        details = {"server": self.server, "tool": self.info.name}
        try:
            client = await self.get_client()
            result = await client.call_tool(self.info.name, args.model_dump(), timeout=self.timeout)
        except TimeoutError as exc:
            raise ToolError(f"The MCP tool {self.info.name} timed out after {self.timeout:g} s.") from exc
        except McpError as exc:
            raise ToolError(f"MCP server '{self.server}': {exc}") from exc
        return ToolResult(content=convert_result(result), details=details, is_error=result.is_error)
