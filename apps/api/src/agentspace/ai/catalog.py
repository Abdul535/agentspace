"""The model catalog: which models a session can switch to, fetched from OpenRouter (ADR-005)."""

import time

import httpx
from pydantic import BaseModel

CACHE_SECONDS = 3600  # 1 hour
FETCH_TIMEOUT = httpx.Timeout(15)


# TS: a Pydantic model is a zod schema and its inferred type in one: fields are validated on construction.
class ModelInfo(BaseModel):
    """One model the picker can offer."""

    provider: str
    id: str
    name: str
    context_length: int | None
    input_price: float | None
    """US dollars per million input tokens; None when the price varies, as for routers like `openrouter/auto`."""
    output_price: float | None
    """US dollars per million output tokens; None when the price varies."""


class CatalogError(Exception):
    """The catalog couldn't be fetched and nothing is cached."""


class OpenRouterCatalog:
    """OpenRouter's tool-capable models, cached in memory for an hour."""

    def __init__(self, base_url: str, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.transport = transport
        self._models: list[ModelInfo] = []
        self._fetched_at = 0.0

    async def models(self) -> list[ModelInfo]:
        """The cached list, refetched once it's an hour old; a failed refetch keeps serving the old list."""
        if self._models and time.monotonic() - self._fetched_at < CACHE_SECONDS:
            return self._models
        try:
            self._models = await self._fetch()
            self._fetched_at = time.monotonic()
        # TS: one `except` can catch several error classes, like `catch (e)` + `if (e instanceof A || e instanceof B)`.
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            if not self._models:
                raise CatalogError(f"Couldn't load models from OpenRouter: {exc}") from exc
        return self._models

    async def _fetch(self) -> list[ModelInfo]:
        """GET `/models` (public, no API key) and keep the models that can call tools."""
        # TS: `async with` closes the client on exit, like `await using client = ...`.
        async with httpx.AsyncClient(timeout=FETCH_TIMEOUT, transport=self.transport) as client:
            response = await client.get(f"{self.base_url}/models")
            response.raise_for_status()
            data = response.json()["data"]
        # The agent loop needs tool calling, so models without it are useless here.
        # TS: a list comprehension, like data.filter(...).map(...) in one expression.
        models = [to_model_info(raw) for raw in data if "tools" in (raw.get("supported_parameters") or [])]
        return sorted(models, key=lambda model: model.name.lower())


def to_model_info(raw: dict) -> ModelInfo:
    """One entry of OpenRouter's `/models` response, trimmed to what the picker shows."""
    pricing = raw.get("pricing") or {}
    return ModelInfo(
        provider="openrouter",
        id=raw["id"],
        name=raw.get("name") or raw["id"],
        context_length=raw.get("context_length"),
        input_price=per_million(pricing.get("prompt")),
        output_price=per_million(pricing.get("completion")),
    )


def per_million(per_token: str | None) -> float | None:
    """OpenRouter's per-token price string as dollars per million tokens; its "-1" means the price varies."""
    if per_token is None:
        return None
    price = float(per_token)
    # round() drops float noise like 3.0000000000000004 from the multiplication.
    return None if price < 0 else round(price * 1_000_000, 6)
