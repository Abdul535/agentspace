"""Provider registry: maps a provider name to a factory that builds it from settings.

To add a provider, write an adapter that implements `Provider` and register a
factory here. Sessions store the provider name, so each one can use a different one.
"""

from collections.abc import Callable

from agentspace.ai.provider import Provider
from agentspace.ai.providers.openai_compat import OpenAICompatProvider
from agentspace.config import Settings

# Builds a provider from the app settings (API keys, base URLs).
# TS: type ProviderFactory = (settings: Settings) => Provider
type ProviderFactory = Callable[[Settings], Provider]


def openrouter(settings: Settings) -> Provider:
    """OpenRouter, through its OpenAI-compatible Chat Completions API."""
    return OpenAICompatProvider(
        name="openrouter",
        base_url=settings.openrouter_base_url,
        api_key=settings.openrouter_api_key,
        headers={"X-Title": "agentspace"},
    )


# Every provider a session can name, by the name stored on the session.
# TS: const PROVIDERS: Record<string, ProviderFactory>
PROVIDERS: dict[str, ProviderFactory] = {
    "openrouter": openrouter,
}


# TS: class UnknownProviderError extends Error {} — a docstring alone is a valid body.
class UnknownProviderError(LookupError):
    """A session or request named a provider that is not registered."""


def get_provider(name: str, settings: Settings) -> Provider:
    """Build the provider registered under `name`."""
    factory = PROVIDERS.get(name)  # TS: PROVIDERS[name] ?? None; plain PROVIDERS[name] would raise if missing
    if factory is None:
        # TS: `raise` is `throw`.
        raise UnknownProviderError(f"Unknown provider '{name}'. Known: {', '.join(sorted(PROVIDERS))}.")
    return factory(settings)
