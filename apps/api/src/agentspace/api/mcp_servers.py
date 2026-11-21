"""HTTP routes for the MCP servers on the settings page: list, add, change, delete and test (ADR-017).

Each user has their own servers, and their env values are encrypted in the database (ADR-025).
"""

import asyncio
import secrets
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Security, status
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, Field
from sqlalchemy import ColumnElement
from sqlmodel import col, select

from agentspace.api.deps import DBDep, SecretBoxDep, SettingsDep, UserDep
from agentspace.encryption import SecretBox, SecretError
from agentspace.mcp.client import McpError, StdioClient
from agentspace.mcp.manager import McpManager, ServerConfig
from agentspace.session.models import McpServerRow, UserRow, utcnow

router = APIRouter(prefix="/mcp-servers", tags=["mcp"])

# Letters, digits, `_` and `-`, as pi allows; it becomes part of every tool name.
NAME_PATTERN = r"^[A-Za-z0-9_-]{1,32}$"
TEST_TIMEOUT_S = 60


def get_mcp(request: Request) -> McpManager:
    """The app-wide MCP connection manager."""
    return request.app.state.mcp


McpDep = Annotated[McpManager, Depends(get_mcp)]

# Read by hand below (auto_error=False), so a missing header gets our own message.
admin_token_header = APIKeyHeader(name="X-MCP-Admin-Token", auto_error=False)


def require_mcp_admin(settings: SettingsDep, token: Annotated[str | None, Security(admin_token_header)] = None) -> None:
    """Only callers with the admin token may save or start a command: these routes run programs on the host."""
    expected = settings.mcp_admin_token
    if not expected:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Set AGENTSPACE_MCP_ADMIN_TOKEN on the API to add, change or test MCP servers.",
        )
    # compare_digest takes the same time however much of the token matches, so timing can't reveal it.
    if token is None or not secrets.compare_digest(token.encode(), expected.encode()):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Wrong or missing MCP admin token.")


# Added to every route that saves, changes or starts a command.
AdminOnly = [Depends(require_mcp_admin)]


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
    admin_token_set: bool
    """Whether `AGENTSPACE_MCP_ADMIN_TOKEN` is set; without it no one can add, change or test servers."""
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


def plain_env(row: McpServerRow, box: SecretBox) -> dict[str, str]:
    """The row's env values decrypted, or a 500 that says why they can't be."""
    try:
        return box.decrypt_dict(row.user_id, row.env)
    except SecretError as exc:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, f"MCP server '{row.name}': {exc}") from exc


def public(row: McpServerRow, box: SecretBox) -> McpServerPublic:
    """The server as the API returns it, to its owner only, with its env readable for the edit form."""
    # TS: `a | b` on dicts is `{ ...a, ...b }`: the row's fields with `env` swapped for the readable one.
    return McpServerPublic.model_validate(row.model_dump() | {"env": plain_env(row, box)})


def server_config(row: McpServerRow, box: SecretBox) -> ServerConfig:
    """The row as what the manager needs to start it."""
    return ServerConfig(
        id=row.id,
        name=row.name,
        command=row.command,
        args=tuple(row.args),
        env=tuple(sorted(plain_env(row, box).items())),
    )


def owned_by(user: UserRow) -> ColumnElement[bool]:
    """The filter every query here uses: only the caller's servers."""
    return col(McpServerRow.user_id) == user.id


async def enabled_server_configs(
    db: DBDep, settings: SettingsDep, user: UserDep, box: SecretBoxDep
) -> list[ServerConfig]:
    """The servers the caller's runs connect to: their enabled ones, or none while MCP is off."""
    if not settings.enable_mcp:
        return []
    rows = await db.exec(
        select(McpServerRow).where(owned_by(user), col(McpServerRow.enabled)).order_by(col(McpServerRow.name))
    )
    return [server_config(row, box) for row in rows]


McpServerConfigsDep = Annotated[list[ServerConfig], Depends(enabled_server_configs)]


async def get_server_row(server_id: str, db: DBDep, user: UserDep) -> McpServerRow:
    """The caller's server named in the path, or a 404, also when it is someone else's."""
    row = (await db.exec(select(McpServerRow).where(McpServerRow.id == server_id, owned_by(user)))).first()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"MCP server {server_id} not found.")
    return row


ServerRowDep = Annotated[McpServerRow, Depends(get_server_row)]


async def check_name_free(db: DBDep, user: UserDep, name: str, except_id: str | None = None) -> None:
    """409 if another of the caller's servers has this name, counting `-` and `_` as the same (tool names clash)."""
    key = name.replace("-", "_")
    for row in await db.exec(select(McpServerRow).where(owned_by(user))):
        if row.id != except_id and row.name.replace("-", "_") == key:
            raise HTTPException(status.HTTP_409_CONFLICT, f"An MCP server named '{row.name}' already exists.")


@router.get("")
async def list_servers(db: DBDep, settings: SettingsDep, user: UserDep, box: SecretBoxDep) -> McpServerList:
    """The caller's saved servers, by name."""
    rows = await db.exec(select(McpServerRow).where(owned_by(user)).order_by(col(McpServerRow.name)))
    servers = [public(row, box) for row in rows]
    return McpServerList(
        mcp_enabled=settings.enable_mcp, admin_token_set=bool(settings.mcp_admin_token), servers=servers
    )


@router.post("", status_code=status.HTTP_201_CREATED, dependencies=AdminOnly)
async def create_server(body: McpServerCreate, db: DBDep, user: UserDep, box: SecretBoxDep) -> McpServerPublic:
    """Save a server for the caller; 409 if they have one by that name. Their sessions pick it up next message."""
    await check_name_free(db, user, body.name)
    row = McpServerRow(
        user_id=user.id,
        name=body.name,
        command=body.command,
        args=body.args,
        env=box.encrypt_dict(user.id, body.env),
        enabled=body.enabled,
    )
    db.add(row)
    await db.commit()
    return public(row, box)


@router.patch("/{server_id}", dependencies=AdminOnly)
async def update_server(
    body: McpServerUpdate, row: ServerRowDep, db: DBDep, user: UserDep, box: SecretBoxDep, mcp: McpDep
) -> McpServerPublic:
    """Change a server; its running processes stop now and start again with the new settings when next needed."""
    if body.name is not None:
        await check_name_free(db, user, body.name, except_id=row.id)
    # exclude_unset: only the fields the client sent, so a missing field never resets one.
    for key, value in body.model_dump(exclude_unset=True).items():
        if value is not None:
            setattr(row, key, box.encrypt_dict(user.id, value) if key == "env" else value)  # TS: row[key] = value
    row.updated_at = utcnow()
    db.add(row)
    await db.commit()
    await mcp.close_server(row.id)
    return public(row, box)


@router.delete("/{server_id}", status_code=status.HTTP_204_NO_CONTENT, dependencies=AdminOnly)
async def delete_server(row: ServerRowDep, db: DBDep, mcp: McpDep) -> None:
    """Delete a server and stop its processes."""
    await db.delete(row)
    await db.commit()
    await mcp.close_server(row.id)


@router.post("/{server_id}/test", dependencies=AdminOnly)
async def test_server(row: ServerRowDep, settings: SettingsDep, box: SecretBoxDep) -> McpServerTest:
    """Start the server once, list its tools and stop it. A failure is a result with `ok: false`, not an HTTP error.

    403 while MCP is off, since this runs the server's command on the host.
    """
    if not settings.enable_mcp:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "MCP is off. Set AGENTSPACE_ENABLE_MCP=true to start servers.")
    cwd = settings.data_dir / "mcp-test"
    cwd.mkdir(parents=True, exist_ok=True)
    client = StdioClient(row.command, row.args, plain_env(row, box), cwd)
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
