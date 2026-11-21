"""Database tables for sessions.

A session's transcript is an append-only list of `messages` rows. Each row
links to the previous one through `parent_id` (as pi's session entries do), so
branching can be added later without a schema change; today the chain is linear
and `seq` orders it.
"""

import secrets
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, Column, String, UniqueConstraint
from sqlmodel import Field, SQLModel

from agentspace.ai.messages import ThinkingLevel


def utcnow() -> datetime:
    """The current time in UTC, for row timestamps."""
    return datetime.now(UTC)


LOCAL_USER_ID = "local"
"""The one user of local mode (no login), who also owns every row made before users existed (ADR-025)."""


# TS: like a Drizzle table definition: one class is both the table schema and the row type.
class UserRow(SQLModel, table=True):
    """Someone who signed in; their sessions and MCP servers are theirs alone (ADR-025)."""

    __tablename__ = "users"

    id: str = Field(primary_key=True)
    """The sign-in provider's user id (Supabase's `sub`), or `local`."""
    email: str | None = None
    created_at: datetime = Field(default_factory=utcnow)


class SessionRow(SQLModel, table=True):
    """One session: which provider and model it uses, how hard it thinks, and its system prompt."""

    __tablename__ = "sessions"

    # TS: `lambda: ...` is an arrow function, here `() => crypto.randomUUID()` without dashes.
    id: str = Field(default_factory=lambda: uuid.uuid4().hex, primary_key=True)
    user_id: str = Field(foreign_key="users.id", index=True, ondelete="CASCADE")
    """Who owns it; every query filters by it, so other users can't see it exists."""
    title: str | None = None
    provider: str
    model: str
    system_prompt: str
    tools: list[str] = Field(sa_column=Column(JSON, nullable=False))
    """Names of the tools the agent may call, in order; `bash` still needs the server to enable it."""
    # A plain string column: SQLModel can't map a Literal type itself, so the column is spelled out.
    thinking_level: ThinkingLevel = Field(default="off", sa_column=Column(String, nullable=False))
    """How hard the model thinks on this session's runs (ADR-012)."""
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    archived_at: datetime | None = None
    """When the session was archived; archived sessions are left out of the list."""


class MessageRow(SQLModel, table=True):
    """One message of a session's transcript, stored as JSON."""

    __tablename__ = "messages"
    __table_args__ = (UniqueConstraint("session_id", "seq"),)

    id: str = Field(default_factory=lambda: secrets.token_hex(8), primary_key=True)
    session_id: str = Field(foreign_key="sessions.id", index=True, ondelete="CASCADE")
    parent_id: str | None = Field(default=None, foreign_key="messages.id")
    seq: int
    role: str
    data: dict[str, Any] = Field(sa_column=Column(JSON, nullable=False))
    """The message itself (`agentspace.ai.messages.Message`), as JSON."""
    created_at: datetime = Field(default_factory=utcnow)


class McpServerRow(SQLModel, table=True):
    """One MCP server from the settings page and the command that starts it; every enabled one joins its user's runs."""

    __tablename__ = "mcp_servers"
    __table_args__ = (UniqueConstraint("user_id", "name"),)

    id: str = Field(default_factory=lambda: uuid.uuid4().hex, primary_key=True)
    user_id: str = Field(foreign_key="users.id", index=True, ondelete="CASCADE")
    name: str
    """Unique per user. Names its tools `mcp__<name>__<tool>`; letters, digits, `_` and `-` only."""
    command: str
    """One executable, not a shell line: e.g. `npx`."""
    args: list[str] = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    env: dict[str, str] = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))
    """Extra environment variables for the process; each value is encrypted for its user (ADR-025)."""
    enabled: bool = True
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class ProviderKeyRow(SQLModel, table=True):
    """A user's own API key for one model provider; their runs are paid with it (ADR-026)."""

    __tablename__ = "provider_keys"

    user_id: str = Field(foreign_key="users.id", primary_key=True, ondelete="CASCADE")
    provider: str = Field(primary_key=True)
    """A name from the provider registry, e.g. `openrouter`."""
    api_key: str
    """Encrypted for its user (ADR-025); never sent back to the browser."""
    last4: str
    """The key's last four characters, so the settings page can show which key is saved."""
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
