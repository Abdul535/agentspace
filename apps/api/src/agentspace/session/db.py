"""Database engine and connection setup."""

from collections.abc import AsyncIterator

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlmodel.ext.asyncio.session import AsyncSession


def create_engine(database_url: str) -> AsyncEngine:
    """An async engine; on SQLite, every connection enforces foreign keys and uses WAL."""
    engine = create_async_engine(database_url)
    if engine.dialect.name == "sqlite":
        # TS: this decorator registers the function below, like engine.on("connect", sqlitePragmas).
        @event.listens_for(engine.sync_engine, "connect")
        def sqlite_pragmas(dbapi_connection, _record) -> None:  # noqa: ANN001
            """SQLite leaves foreign keys off by default; WAL lets reads run while a write is open."""
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.close()

    return engine


def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Opens DB sessions; objects stay readable after commit, which the streaming route relies on."""
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


# TS: an async generator used as setup/teardown: `async with` closes the session after the caller is done.
async def open_db(factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[AsyncSession]:
    """One DB session, closed when the caller is done with it."""
    async with factory() as db:
        yield db
