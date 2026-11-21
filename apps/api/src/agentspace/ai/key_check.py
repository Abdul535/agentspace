"""Checks a user's API key with one cheap call to its provider before it is saved (ADR-026)."""

from typing import Literal

import httpx

# `unknown`: the provider couldn't be reached or answered oddly, so the key may still be fine.
type KeyVerdict = Literal["valid", "invalid", "unknown"]

CHECK_TIMEOUT = httpx.Timeout(10)


async def check_openrouter_key(
    base_url: str, api_key: str, transport: httpx.AsyncBaseTransport | None = None
) -> KeyVerdict:
    """Ask OpenRouter about the key (`GET /key`), which costs nothing and runs no model."""
    try:
        async with httpx.AsyncClient(timeout=CHECK_TIMEOUT, transport=transport) as client:
            response = await client.get(f"{base_url.rstrip('/')}/key", headers={"Authorization": f"Bearer {api_key}"})
    except httpx.HTTPError:
        return "unknown"
    if response.status_code in (401, 403):
        return "invalid"
    return "valid" if response.is_success else "unknown"
