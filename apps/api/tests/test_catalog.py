from collections.abc import Iterator
from pathlib import Path
from typing import cast

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agentspace.ai.catalog import CatalogError, OpenRouterCatalog
from agentspace.api.deps import get_catalog
from agentspace.config import Settings
from agentspace.main import create_app

OPENROUTER_MODELS = {
    "data": [
        {
            "id": "zeta/tools",
            "name": "Zeta: Tools",
            "context_length": 200000,
            "pricing": {"prompt": "0.000003", "completion": "0.000015"},
            "supported_parameters": ["tools", "temperature"],
        },
        {
            "id": "alpha/no-tools",
            "name": "Alpha: No tools",
            "context_length": 8000,
            "pricing": {"prompt": "0", "completion": "0"},
            "supported_parameters": ["temperature"],
        },
        {
            "id": "openrouter/auto",
            "name": "Auto Router",
            "context_length": 2000000,
            "pricing": {"prompt": "-1", "completion": "-1"},
            "supported_parameters": ["tools"],
        },
    ]
}


def catalog_with(handler) -> OpenRouterCatalog:
    """A catalog whose HTTP calls go to `handler` instead of the network."""
    return OpenRouterCatalog("https://openrouter.test/api/v1", transport=httpx.MockTransport(handler))


@pytest.mark.anyio
async def test_keeps_tool_models_sorted_with_prices_per_million() -> None:
    catalog = catalog_with(lambda request: httpx.Response(200, json=OPENROUTER_MODELS))

    models = await catalog.models()

    assert [m.id for m in models] == ["openrouter/auto", "zeta/tools"]
    zeta = models[1]
    assert (zeta.provider, zeta.context_length, zeta.input_price, zeta.output_price) == ("openrouter", 200000, 3, 15)
    assert models[0].input_price is None and models[0].output_price is None


@pytest.mark.anyio
async def test_caches_and_serves_the_old_list_when_a_refetch_fails() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls  # TS: lets the inner function reassign the outer `calls`, which closures do by default in TS
        calls += 1
        return httpx.Response(200, json=OPENROUTER_MODELS) if calls == 1 else httpx.Response(500)

    catalog = catalog_with(handler)
    first = await catalog.models()
    assert await catalog.models() == first and calls == 1

    catalog._fetched_at = 0  # make the cache stale
    assert await catalog.models() == first and calls == 2


@pytest.mark.anyio
async def test_raises_when_nothing_is_cached() -> None:
    catalog = catalog_with(lambda request: httpx.Response(503))
    with pytest.raises(CatalogError):
        await catalog.models()


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    settings = Settings(_env_file=None, data_dir=tmp_path, default_model="zeta/tools", favorite_models=["zeta/tools"])
    app = create_app(settings)
    catalog = catalog_with(lambda request: httpx.Response(200, json=OPENROUTER_MODELS))
    app.dependency_overrides[get_catalog] = lambda: catalog
    with TestClient(app) as client:
        yield client


def test_models_route_returns_catalog_default_and_favorites(client: TestClient) -> None:
    body = client.get("/models").json()
    assert body["default_model"] == "zeta/tools"
    assert body["favorites"] == ["zeta/tools"]
    assert [m["id"] for m in body["models"]] == ["openrouter/auto", "zeta/tools"]


def test_models_route_is_502_when_openrouter_is_down(client: TestClient) -> None:
    # TS: cast() is `as FastAPI`: TestClient types `app` as any ASGI app, ours is a FastAPI one.
    cast(FastAPI, client.app).dependency_overrides[get_catalog] = lambda: catalog_with(
        lambda request: httpx.Response(503)
    )
    assert client.get("/models").status_code == 502
