import pytest

from agentspace.ai.catalog import ModelInfo, OpenRouterCatalog


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
