"""HTTP route for the model catalog the web app's picker shows (ADR-005)."""

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from agentspace.ai.catalog import CatalogError, ModelInfo
from agentspace.api.deps import CatalogDep, SettingsDep

router = APIRouter(prefix="/models", tags=["models"])


class ModelCatalog(BaseModel):
    """Every model a session can switch to, plus which ones to pin."""

    default_model: str
    """The model new sessions get."""
    favorites: list[str]
    """Model ids to pin at the top, in the order configured."""
    models: list[ModelInfo]
    """Tool-capable models, sorted by name."""


@router.get("")
async def list_models(catalog: CatalogDep, settings: SettingsDep) -> ModelCatalog:
    """The catalog; 502 when OpenRouter can't be reached and nothing is cached."""
    try:
        models = await catalog.models()
    except CatalogError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    return ModelCatalog(default_model=settings.default_model, favorites=settings.favorite_models, models=models)
