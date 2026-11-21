"""Postgres (ADR-027): the migrations build what the models describe, and connections fit Supabase."""

import pytest
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy.engine import Connection
from sqlmodel import SQLModel

from agentspace.session import models  # noqa: F401  (registers the tables on SQLModel.metadata)
from agentspace.session.db import connect_args, create_engine


@pytest.mark.anyio
async def test_the_migrations_build_exactly_what_the_models_describe(database_url: str) -> None:
    def diff(connection: Connection) -> list:
        """What `alembic revision --autogenerate` would write: empty when nothing drifted."""
        return compare_metadata(MigrationContext.configure(connection), SQLModel.metadata)

    engine = create_engine(database_url)
    async with engine.connect() as connection:
        assert await connection.run_sync(diff) == []
    await engine.dispose()


def test_every_connection_caps_how_long_a_statement_may_run() -> None:
    args = connect_args("postgresql+asyncpg://u:p@127.0.0.1:55322/db", statement_timeout_ms=1234)
    assert args["server_settings"] == {"statement_timeout": "1234"}
    assert "ssl" not in args and "statement_cache_size" not in args


def test_supabase_hosts_get_tls_unless_the_url_says_otherwise() -> None:
    assert connect_args("postgresql+asyncpg://u:p@db.abc.supabase.co:5432/postgres", 1)["ssl"] == "require"
    assert "ssl" not in connect_args("postgresql+asyncpg://u:p@db.abc.supabase.co/postgres?ssl=disable", 1)


def test_the_transaction_pooler_gets_no_prepared_statements() -> None:
    args = connect_args("postgresql+asyncpg://u:p@aws-0-eu.pooler.supabase.com:6543/postgres", 1)
    assert args["statement_cache_size"] == 0 and args["prepared_statement_cache_size"] == 0
    # Unique names, so two statements prepared through the same pooled server connection never clash.
    assert args["prepared_statement_name_func"]() != args["prepared_statement_name_func"]()
