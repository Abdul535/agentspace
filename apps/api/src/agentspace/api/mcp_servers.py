"""HTTP routes for the MCP servers on the settings page: list, add, change, delete and test (ADR-017)."""

import asyncio
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlmodel import col, select

from agentspace.api.deps import DBDep, SettingsDep
from agentspace.mcp.client import McpError, StdioClient
from agentspace.mcp.manager import McpManager, ServerConfig
from agentspace.session.models import McpServerRow, utcnow

router = APIRouter(prefix="/mcp-servers", tags=["mcp"])

# Letters, digits, `_` and `-`, as pi allows; it becomes part of every tool name.
NAME_PATTERN = r"^[A-Za-z0-9_-]{1,32}$"
TEST_TIMEOUT_S = 60


def get_mcp(request: Request) -> McpManager:
    """The app-wide MCP connection manager."""
    return request.app.state.mcp


McpDep = Annotated[McpManager, Depends(get_mcp)]


class McpServerCreate(BaseModel):
    """A new stdio server: the command that starts it and its environment."""

    name: str = Field(pattern=NAME_PATTERN)  # TS: z.string().regex(...)
    command: str = Field(min_length=1)
    args: list[str] = []
    env: dict[str, str] = {}
    enabled: bool = True


class McpServerUpdate(BaseModel):
    """Fields to change on a server; unset ones stay. Its running processes restart on the next message."""

    name: str | None = Field(default=None, pattern=NAME_PATTERN)
    command: str | None = Field(default=None, min_length=1)
    args: list[str] | None = None
    env: dict[str, str] | None = None
    enabled: bool | None = None


class McpServerPublic(BaseModel):
    """A server as the API returns it."""

    id: str
    name: str
    command: str
    args: list[str]
    env: dict[str, str]
    enabled: bool
    created_at: datetime
    updated_at: datetime


class McpServerList(BaseModel):
    """The saved servers, and whether the API starts them at all."""

    mcp_enabled: bool
    """False until `AGENTSPACE_ENABLE_MCP=true`; servers can be saved but not tested or used."""
    servers: list[McpServerPublic]


class McpToolSummary(BaseModel):
    """One tool a server offers, as the test shows it."""

    name: str
    description: str | None


class McpServerTest(BaseModel):
    """The outcome of starting a server and listing its tools."""

    ok: bool
    error: str | None = None
    server_name: str | None = None
    server_version: str | None = None
    instructions: str | None = None
    tools: list[McpToolSummary] = []


def server_config(row: McpServerRow) -> ServerConfig:
    """The row as what the manager needs to start it."""
    return ServerConfig(
        id=row.id,
        name=row.name,
        command=row.command,
        args=tuple(row.args),
        env=tuple(sorted(row.env.items())),
    )


async def enabled_server_configs(db: DBDep, settings: SettingsDep) -> list[ServerConfig]:
    """The servers every run connects to: the enabled ones, or none while MCP is off."""
    if not settings.enable_mcp:
        return []
    rows = await db.exec(select(McpServerRow).where(col(McpServerRow.enabled)).order_by(col(McpServerRow.name)))
    return [server_config(row) for row in rows]


McpServerConfigsDep = Annotated[list[ServerConfig], Depends(enabled_server_configs)]


async def get_server_row(server_id: str, db: DBDep) -> McpServerRow:
    """The server named in the path, or a 404."""
    row = await db.get(McpServerRow, server_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"MCP server {server_id} not found.")
    return row


ServerRowDep = Annotated[McpServerRow, Depends(get_server_row)]


async def check_name_free(db: DBDep, name: str, except_id: str | None = None) -> None:
    """409 if another server has this name, counting `-` and `_` as the same (their tool names would clash)."""
    key = name.replace("-", "_")
    for row in await db.exec(select(McpServerRow)):
        if row.id != except_id and row.name.replace("-", "_") == key:
            raise HTTPException(status.HTTP_409_CONFLICT, f"An MCP server named '{row.name}' already exists.")


@router.get("")
async def list_servers(db: DBDep, settings: SettingsDep) -> McpServerList:
    """Every saved server, by name."""
    rows = await db.exec(select(McpServerRow).order_by(col(McpServerRow.name)))
    servers = [McpServerPublic.model_validate(row, from_attributes=True) for row in rows]
    return McpServerList(mcp_enabled=settings.enable_mcp, servers=servers)


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_server(body: McpServerCreate, db: DBDep) -> McpServerPublic:
    """Save a server; 409 if its name is taken. Sessions pick it up on their next message."""
    await check_name_free(db, body.name)
    # TS: `**body.model_dump()` spreads the fields as keyword arguments, like new Row({ ...body }).
    row = McpServerRow(**body.model_dump())
    db.add(row)
    await db.commit()
    return McpServerPublic.model_validate(row, from_attributes=True)


@router.patch("/{server_id}")
async def update_server(body: McpServerUpdate, row: ServerRowDep, db: DBDep, mcp: McpDep) -> McpServerPublic:
    """Change a server; its running processes stop now and start again with the new settings when next needed."""
    if body.name is not None:
        await check_name_free(db, body.name, except_id=row.id)
    # exclude_unset: only the fields the client sent, so a missing field never resets one.
    for key, value in body.model_dump(exclude_unset=True).items():
        if value is not None:
            setattr(row, key, value)  # TS: row[key] = value
    row.updated_at = utcnow()
    db.add(row)
    await db.commit()
    await mcp.close_server(row.id)
    return McpServerPublic.model_validate(row, from_attributes=True)


@router.delete("/{server_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_server(row: ServerRowDep, db: DBDep, mcp: McpDep) -> None:
    """Delete a server and stop its processes."""
    await db.delete(row)
    await db.commit()
    await mcp.close_server(row.id)


@router.post("/{server_id}/test")
async def test_server(row: ServerRowDep, settings: SettingsDep) -> McpServerTest:
    """Start the server once, list its tools and stop it. A failure is a result with `ok: false`, not an HTTP error.

    403 while MCP is off, since this runs the server's command on the host.
    """
    if not settings.enable_mcp:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "MCP is off. Set AGENTSPACE_ENABLE_MCP=true to start servers.")
    cwd = settings.data_dir / "mcp-test"
    cwd.mkdir(parents=True, exist_ok=True)
    client = StdioClient(row.command, row.args, row.env, cwd)
    try:
        async with asyncio.timeout(TEST_TIMEOUT_S):
            await client.start()
            tools = await client.list_tools()
    except (McpError, TimeoutError) as exc:
        message = str(exc) or f"No answer within {TEST_TIMEOUT_S} s."
        if isinstance(exc, TimeoutError) and (tail := client.stderr_text().strip()):
            message += f" Its last output:\n{tail}"
        return McpServerTest(ok=False, error=message)
    finally:
        await client.close()
    return McpServerTest(
        ok=True,
        server_name=client.server_info.get("name"),
        server_version=client.server_info.get("version"),
        instructions=client.instructions,
        tools=[McpToolSummary(name=tool.name, description=tool.description) for tool in tools],
    )
