"""Recognize a provider error that means "the request is longer than the model's context window".

Providers say it in their own words, so this matches known phrasings, after pi's
`packages/ai/src/utils/overflow.ts`. Used to compact and retry (ADR-009).
"""

import re

from agentspace.ai.messages import AssistantMessage

# TS: re.compile(..., re.IGNORECASE) is a /.../i regex literal.
OVERFLOW_PATTERNS = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"exceeds the context window",  # OpenAI
        r"maximum context length",  # OpenRouter, OpenAI-compatible proxies
        r"context window exceeded",  # LiteLLM
        r"context[_ ]length[_ ]exceeded",  # generic error codes
        r"longer than the model'?s context length",  # Together AI
        r"greater than the context length",  # LM Studio
        r"exceeds the available context size",  # llama.cpp
        r"prompt is too long",  # Anthropic
        r"input is too long",
    )
]


def is_context_overflow(message: AssistantMessage) -> bool:
    """Whether this failed reply failed because its request didn't fit the model's context window."""
    if message.stop_reason != "error" or not message.error_message:
        return False
    # TS: any(...) is patterns.some(...)
    return any(pattern.search(message.error_message) for pattern in OVERFLOW_PATTERNS)
