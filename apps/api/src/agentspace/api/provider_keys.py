"""HTTP routes for each user's own model API keys: list, save (checked first) and remove (ADR-026)."""

from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field

from agentspace.ai.key_check import KeyVerdict, check_openrouter_key
from agentspace.ai.providers import PROVIDER_LABELS, PROVIDERS
from agentspace.api.deps import DBDep, SecretBoxDep, SettingsDep, UserDep
from agentspace.session.keys import list_keys, save_key
from agentspace.session.models import ProviderKeyRow

router = APIRouter(prefix="/provider-keys", tags=["provider-keys"])

# Checks a key for a provider: (provider, key) → verdict.
type KeyChecker = Callable[[str, str], Awaitable[KeyVerdict]]


def key_checker(settings: SettingsDep) -> KeyChecker:
    """The real check against each provider. Tests override this dependency, so they never call OpenRouter."""

    async def check(provider: str, api_key: str) -> KeyVerdict:
        """Only OpenRouter is registered today; it is the only provider to check."""
        return await check_openrouter_key(settings.openrouter_base_url, api_key)

    return check


KeyCheckerDep = Annotated[KeyChecker, Depends(key_checker)]


# TS: a Pydantic model is like a zod schema and its inferred type in one: it validates the body and types it.
class ProviderKeyPublic(BaseModel):
    """A provider and the user's key for it, if any; the key itself never leaves the server."""

    provider: str
    label: str
    """The provider's name to show, e.g. `OpenRouter`."""
    last4: str | None
    """The saved key's last four characters; `None` when no key is saved."""
    updated_at: datetime | None


class ProviderKeySave(BaseModel):
    """The key to save; surrounding spaces from a paste are trimmed."""

    api_key: str = Field(min_length=8, max_length=500)


class ProviderKeySaved(ProviderKeyPublic):
    """The saved key, and whether the provider confirmed it works."""

    checked: bool
    """False when the provider couldn't be reached: the key is saved but unconfirmed."""


def public(provider: str, row: ProviderKeyRow | None) -> ProviderKeyPublic:
    """The provider's entry for the settings page."""
    return ProviderKeyPublic(
        provider=provider,
        label=PROVIDER_LABELS.get(provider, provider),
        last4=row.last4 if row else None,
        updated_at=row.updated_at if row else None,
    )


def require_known(provider: str) -> None:
    """A 404 for a provider name that isn't registered."""
    if provider not in PROVIDERS:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown provider '{provider}'.")


@router.get("")
async def list_provider_keys(db: DBDep, user: UserDep) -> list[ProviderKeyPublic]:
    """Every registered provider, with the caller's saved key for it or none."""
    rows = {row.provider: row for row in await list_keys(db, user.id)}
    return [public(provider, rows.get(provider)) for provider in PROVIDERS]


@router.put("/{provider}")
async def save_provider_key(
    provider: str, body: ProviderKeySave, db: DBDep, user: UserDep, box: SecretBoxDep, check: KeyCheckerDep
) -> ProviderKeySaved:
    """Check the key with the provider, then add or replace it. A key the provider refuses is not saved."""
    require_known(provider)
    api_key = body.api_key.strip()
    verdict = await check(provider, api_key)
    if verdict == "invalid":
        label = PROVIDER_LABELS.get(provider, provider)
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"{label} refused this key. Check that you copied all of it and that it isn't disabled.",
        )
    row = await save_key(db, box, user.id, provider, api_key)
    # TS: `**dict` spreads it into keyword arguments, like `{ ...entry, checked }`.
    return ProviderKeySaved(**public(provider, row).model_dump(), checked=verdict == "valid")


@router.delete("/{provider}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_provider_key(provider: str, db: DBDep, user: UserDep) -> Response:
    """Remove the caller's key; their sessions can't run until they add one again."""
    require_known(provider)
    row = await db.get(ProviderKeyRow, (user.id, provider))
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No key is saved for this provider.")
    await db.delete(row)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
