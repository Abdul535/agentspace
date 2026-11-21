import itertools
import os
import secrets
from collections.abc import Iterator

import pg
import pytest
import redis

from agentspace.ai.catalog import ModelInfo, OpenRouterCatalog
from agentspace.encryption import SecretBox
from agentspace.session.migrate import upgrade_to_head


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class OfflineCatalog(OpenRouterCatalog):
    """A catalog that lists no models, so a run asking what its model can do never calls OpenRouter."""

    async def _fetch(self) -> list[ModelInfo]:
        return []


@pytest.fixture(autouse=True)
def offline_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every app a test creates gets the offline catalog; tests that need models override `get_catalog`."""
    monkeypatch.setattr("agentspace.main.OpenRouterCatalog", OfflineCatalog)


@pytest.fixture(scope="session")
def template_database() -> Iterator[tuple[str, Iterator[int]]]:
    """This run's migrated template database, and a counter that names each test's copy of it."""
    try:
        pg.drop_stale_databases()
    except OSError as error:
        pytest.exit(f"No Postgres at {pg.SERVER_URL} ({error}). Start it with `bash scripts/db.sh --server`.", 1)
    prefix = pg.new_run_prefix()
    template = f"{prefix}template"
    # Migrated once per run, so every test starts from this branch's schema without running Alembic itself.
    upgrade_to_head(pg.create_database(template), SecretBox("test-secret-" * 3))
    yield prefix, itertools.count()
    for (name,) in pg.run(pg.SERVER_URL, "SELECT datname FROM pg_database WHERE datname LIKE $1", f"{prefix}%"):
        pg.drop_database(name)


@pytest.fixture(autouse=True)
def database_url(template_database: tuple[str, Iterator[int]], monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """A fresh, migrated database for each test (a copy of the template), which the app's Settings read."""
    prefix, counter = template_database
    name = f"{prefix}{next(counter)}"
    url = pg.create_database(name, template=f"{prefix}template")
    monkeypatch.setenv("AGENTSPACE_DATABASE_URL", url)
    yield url
    pg.drop_database(name)


REDIS_URL = os.environ.get("AGENTSPACE_TEST_REDIS_URL", "redis://127.0.0.1:55379/0")
"""The Redis tests use (ADR-029): the local one from `scripts/db.sh`, or CI's Redis service."""


@pytest.fixture(scope="session")
def redis_server() -> str:
    """The test Redis's URL, once it answers; the run stops with how to start it if it doesn't."""
    try:
        redis.Redis.from_url(REDIS_URL).ping()
    except redis.ConnectionError as error:
        pytest.exit(f"No Redis at {REDIS_URL} ({error}). Start it with `bash scripts/db.sh --server`.", 1)
    return REDIS_URL


@pytest.fixture(autouse=True)
def redis_prefix(redis_server: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """This test's own Redis keys, which the app's Settings read; deleted after the test."""
    prefix = f"agentspace_test:{secrets.token_hex(4)}:"
    monkeypatch.setenv("AGENTSPACE_REDIS_URL", redis_server)
    monkeypatch.setenv("AGENTSPACE_REDIS_PREFIX", prefix)
    yield prefix
    client = redis.Redis.from_url(redis_server)
    if keys := list(client.scan_iter(f"{prefix}*")):
        client.delete(*keys)
