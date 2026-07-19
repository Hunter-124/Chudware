"""Moonshot AI (Kimi) API-key provider — OpenAI-compatible chat completions.

Auth: API key from platform.moonshot.ai (`MOONSHOT_API_KEY` / config.json
``keys.moonshot`` / ``moonshot.api_key``).

Contract: OpenAI-compatible Chat Completions at ``https://api.moonshot.ai/v1``.
Cloud provider: NEVER offline-safe.

Kimi K3 is a SINGLE model on the official API — ``kimi-k3`` — with a
1,048,576-token context window and FLAT pricing ($3/$15 per 1M in/out, $0.30
cached input) regardless of how much context is sent. The ``kimi-k3-256k``
model id is an ALIAS we normalize back to ``kimi-k3`` on the wire; it exists so
different agent assignments can declare a smaller context window (fewer packed
input tokens → less spend when the harness input cap is raised above 256k). On
the stock harness (input capped ~6k tokens) both variants behave identically.
"""
from __future__ import annotations

from .openai_provider import OpenAIProvider

DEFAULT_MODEL = "kimi-k3"
BASE_URL = "https://api.moonshot.ai/v1"

# Alias id → wire id. K3 is a single API model (`kimi-k3`); K2.7 ships as
# `kimi-k2.7-code` on the official API. GUI catalog uses the short ids.
_WIRE_ALIASES = {
    "kimi-k3-256k": "kimi-k3",
    "kimi-k3-1m": "kimi-k3",
    "moonshotai/kimi-k3": "kimi-k3",
    "kimi-latest": "kimi-k3",
    "kimi-k2.7": "kimi-k2.7-code",
    "kimi-k2.7-code": "kimi-k2.7-code",
}


def wire_model(model: str | None) -> str:
    """Map a catalog/alias model id to the id the Moonshot API accepts."""
    m = (model or DEFAULT_MODEL).strip()
    return _WIRE_ALIASES.get(m, m)


class MoonshotProvider(OpenAIProvider):
    """OpenAI-compatible adapter pinned to Moonshot's Kimi API."""

    name = "moonshot"

    def __init__(self, api_key: str | None = None, model: str = DEFAULT_MODEL,
                 supports_vision: bool = True, timeout: float = 300.0,
                 base_url: str | None = None, reasoning_effort: str | None = None):
        # Normalize the -256k / -1m / vendor-prefixed aliases to the wire id.
        super().__init__(
            api_key=api_key,
            model=wire_model(model),
            base_url=base_url or BASE_URL,
            supports_vision=supports_vision,
            timeout=timeout,
            reasoning_effort=reasoning_effort,
        )
