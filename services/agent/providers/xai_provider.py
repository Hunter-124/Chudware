"""xAI Grok API-key provider — OpenAI-compatible chat completions at api.x.ai.

Auth: classic API key from console.x.ai (`XAI_API_KEY` / config.json api_key).
Not the SuperGrok / X Premium+ OAuth path — that is :mod:`xai_sub_provider`.

Contract: OpenAI-compatible Chat Completions at ``https://api.x.ai/v1``.
Cloud provider: NEVER offline-safe.
"""
from __future__ import annotations

from .openai_provider import OpenAIProvider

# https://docs.x.ai/docs — OpenAI-compatible root
DEFAULT_MODEL = "grok-4"
BASE_URL = "https://api.x.ai/v1"


class XAIProvider(OpenAIProvider):
    """OpenAI-compatible adapter pinned to xAI's public API."""

    name = "xai"

    def __init__(self, api_key: str | None = None, model: str = DEFAULT_MODEL,
                 supports_vision: bool = True, timeout: float = 120.0,
                 base_url: str | None = None):
        # base_url is fixed to xAI unless explicitly overridden (tests / proxies).
        super().__init__(
            api_key=api_key,
            model=model,
            base_url=base_url or BASE_URL,
            supports_vision=supports_vision,
            timeout=timeout,
        )
