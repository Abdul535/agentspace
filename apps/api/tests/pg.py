"""Test databases on the local Postgres (ADR-027): one migrated template per run, a fresh copy for every test."""

import asyncio
import os
import secrets
from typing import Any

import asyncpg

SERVER_URL = os.environ.get("AGENTSPACE_TEST_DATABASE_URL", "postgresql://postgres:postgres@127.0.0.1:55322/postgres")
"""Where test databases are made: the local Supabase from `scripts/db.sh`, or CI's Postgres service."""
PREFIX = "agentspace_test_"


def app_url(name: str) -> str:
    """The app's URL (asyncpg through SQLAlchemy) for the database `name` on the test server."""
    base = SERVER_URL.rsplit("/", 1)[0].replace("postgresql://", "postgresql+asyncpg://", 1)
    return f"{base}/{name}"


async def _run(url: str, sql: str, *args: Any) -> list[tuple]:
    """Run one statement on its own connection and return its rows as tuples."""
    conn = await asyncpg.connect(url.replace("postgresql+asyncpg://", "postgresql://", 1))
    try:
        return [tuple(row) for row in await conn.fetch(sql, *args)]
    finally:
        await conn.close()


def run(url: str, sql: str, *args: Any) -> list[tuple]:
    """`_run` for sync tests and fixtures; `$1`, `$2`… are the arguments."""
    return asyncio.run(_run(url, sql, *args))


def db_rows(sql: str, *args: Any) -> list[tuple]:
    """Rows straight from this test's database, as someone with a copy of it would see them."""
    return run(os.environ["AGENTSPACE_DATABASE_URL"], sql, *args)


def new_run_prefix() -> str:
    """Names this run's databases: the process id says whether the run that made them is still alive."""
    return f"{PREFIX}{os.getpid()}_{secrets.token_hex(3)}_"


def create_database(name: str, template: str | None = None) -> str:
    """Create `name` (empty, or a copy of `template`) and return the app's URL for it."""
    copy = f' TEMPLATE "{template}"' if template else ""
    run(SERVER_URL, f'CREATE DATABASE "{name}"{copy}')
    return app_url(name)


def drop_database(name: str) -> None:
    """Drop `name`, closing any connection a test left open."""
    run(SERVER_URL, f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


def drop_stale_databases() -> None:
    """Drop test databases left by runs that crashed: their process id isn't running any more."""
    for (name,) in run(SERVER_URL, "SELECT datname FROM pg_database WHERE datname LIKE $1", f"{PREFIX}%"):
        pid = name.removeprefix(PREFIX).split("_", 1)[0]
        if pid.isdigit() and not pid_alive(int(pid)):
            drop_database(name)


def pid_alive(pid: int) -> bool:
    """Whether a process with this id runs on this machine."""
    try:
        os.kill(pid, 0)  # signal 0 checks only that the process exists
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # it exists, but belongs to another user
    return True
