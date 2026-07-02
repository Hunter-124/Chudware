"""OpenRouter adapter — one login (PKCE), hundreds of models, user-balance billing.

Contract: contracts/providers.md §4.1. OpenRouter speaks the OpenAI Chat Completions API, so
this is the generic OpenAI-compatible adapter pinned to the OpenRouter base_url. The API key
is either a BYO OpenRouter key (`api_key` / OPENROUTER_API_KEY) or one minted by the blessed
PKCE login (`oauth_pkce.complete_login`) and then stored via the normal config.json mechanism.

Cloud provider: NEVER offline-safe (§5). It is refused before construction in offline mode.
"""
from __future__ import annotations

from .openai_provider import OpenAIProvider

DEFAULT_MODEL = "openrouter/auto"
BASE_URL = "https://openrouter.ai/api/v1"


class OpenRouterProvider(OpenAIProvider):
    name = "openrouter"

    def __init__(self, api_key: str | None = None, model: str = DEFAULT_MODEL,
                 supports_vision: bool = True, timeout: float = 120.0):
        # base_url is fixed to OpenRouter; the parent handles the OpenAI-compatible wire form,
        # timeouts, and the empty-choices / reasoning-content degradations.
        super().__init__(api_key=api_key, model=model, base_url=BASE_URL,
                         supports_vision=supports_vision, timeout=timeout)
