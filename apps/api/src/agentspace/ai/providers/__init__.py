"""Provider registry: maps a provider name to a factory that builds it from settings.

To add a provider, write an adapter that implements `Provider` and register a
factory here. Sessions store the provider name, so each one can use a different one.
Each run is paid with the session owner's own key for it (ADR-026).
"""

from collections.abc import Callable

from agentspace.ai.provider import Provider
from agentspace.ai.providers.openai_compat import OpenAICompatProvider
from agentspace.config import Settings

# Builds a provider from the app settings (base URLs) and the user's own API key.
# TS: type ProviderFactory = (settings: Settings, apiKey: string) => Provider
type ProviderFactory = Callable[[Settings, str], Provider]


def openrouter(settings: Settings, api_key: str) -> Provider:
    """OpenRouter, through its OpenAI-compatible Chat Completions API."""
    return OpenAICompatProvider(
        name="openrouter",
        base_url=settings.openrouter_base_url,
        api_key=api_key,
        headers={"X-Title": "agentspace"},
        # The user fixes these on their own OpenRouter account, so say where (ADR-026).
        error_hints={
            401: "OpenRouter refused your API key. Replace it in Settings → API keys.",
            402: "Your OpenRouter account is out of credits. Add credits at openrouter.ai, then retry.",
        },
    )


# Every provider a session can name, by the name stored on the session.
# TS: const PROVIDERS: Record<string, ProviderFactory>
PROVIDERS: dict[str, ProviderFactory] = {
    "openrouter": openrouter,
}

# Each provider's name as the web app shows it.
PROVIDER_LABELS: dict[str, str] = {
    "openrouter": "OpenRouter",
}


# TS: class UnknownProviderError extends Error {} — a docstring alone is a valid body.
class UnknownProviderError(LookupError):
    """A session or request named a provider that is not registered."""


class MissingApiKeyError(LookupError):
    """The user has saved no API key for the provider their session uses."""


def get_provider(name: str, settings: Settings, api_key: str | None) -> Provider:
    """Build the provider registered under `name` with the user's key for it."""
    factory = PROVIDERS.get(name)  # TS: PROVIDERS[name] ?? None; plain PROVIDERS[name] would raise if missing
    if factory is None:
        # TS: `raise` is `throw`.
        raise UnknownProviderError(f"Unknown provider '{name}'. Known: {', '.join(sorted(PROVIDERS))}.")
    if not api_key:
        raise MissingApiKeyError(name)
    return factory(settings, api_key)
