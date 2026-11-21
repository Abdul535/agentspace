"""Database engine and connection setup: Postgres through asyncpg (ADR-027)."""

import uuid
from collections.abc import AsyncIterator
from typing import Any

from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlmodel.ext.asyncio.session import AsyncSession

TRANSACTION_POOLER_PORT = 6543
"""Supabase's Supavisor in transaction mode: each transaction may run on a different server connection."""


def connect_args(database_url: str, statement_timeout_ms: int) -> dict[str, Any]:
    """asyncpg options for this URL: a cap on each statement, TLS to Supabase, no prepared statements on its pooler."""
    url = make_url(database_url)
    args: dict[str, Any] = {
        # Gives up on a server that doesn't answer, instead of holding the request.
        "timeout": 10,
        # A stuck query frees its connection after this long, so it can't starve every other request.
        "server_settings": {"statement_timeout": str(statement_timeout_ms)},
    }
    # Supabase accepts TLS on every host; without this asyncpg may connect in plain text. A URL `ssl=` wins.
    host = url.host or ""
    if host.endswith((".supabase.co", ".supabase.com")) and "ssl" not in url.query and "sslmode" not in url.query:
        args["ssl"] = "require"
    if url.port == TRANSACTION_POOLER_PORT:
        # A statement prepared on one server connection is unknown on the next, and a name reused
        # across clients collides: so no cached statements, and a unique name for each one.
        args["statement_cache_size"] = 0
        args["prepared_statement_cache_size"] = 0
        args["prepared_statement_name_func"] = lambda: f"__asyncpg_{uuid.uuid4()}__"
    return args


def create_engine(database_url: str, *, pool_size: int = 5, statement_timeout_ms: int = 25_000) -> AsyncEngine:
    """An async engine with a bounded pool: at most `2 * pool_size` connections per API process."""
    return create_async_engine(
        database_url,
        connect_args=connect_args(database_url, statement_timeout_ms),
        pool_size=pool_size,
        max_overflow=pool_size,
        # A request waits this long for a free connection, then fails, instead of queueing forever.
        pool_timeout=10,
        # Replaces connections the server or a pooler dropped while they sat idle.
        pool_pre_ping=True,
        pool_recycle=30 * 60,
    )


def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Opens DB sessions; objects stay readable after commit, which the streaming route relies on."""
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def release(db: AsyncSession) -> None:
    """End the session's open read transaction, so its connection goes back to the pool until the next query.

    Call it before waiting on something slow (a model reply, a long stream): reads start a transaction that
    otherwise keeps a Postgres connection checked out until the session closes. Loaded rows stay readable.
    """
    await db.commit()


# TS: an async generator used as setup/teardown: `async with` closes the session after the caller is done.
async def open_db(factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[AsyncSession]:
    """One DB session, closed when the caller is done with it."""
    async with factory() as db:
        yield db
