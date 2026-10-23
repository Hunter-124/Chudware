"""vLLM per-task LoRA adapter provider.

A single vLLM instance launched with ``--enable-lora --lora-modules <name>=<path> ...``
exposes an OpenAI-compatible API where **the LoRA adapter is selected by the ``model``
field** of the request (the served adapter name), and the base model is requested by its
own served name.

So a vLLM adapter provider IS just an OpenAI-compatible client (contract §2) pinned to the
vLLM endpoint, with the served-model name set to the adapter (or base). It enters the app
as just another provider with an ``adapter`` field (``contracts/providers.md`` §7) — no new
provider stack. :class:`services.agent.providers.logging_provider.LoggingProvider` wraps it
unchanged.

Offline classification: vLLM runs locally, so it is offline-safe **iff its base_url host is
loopback** — handled name-by-name in ``services/agent/config.py:provider_offline_safe`` (the
loopback-checked path, alongside ``openai``/``llamacpp``), NOT the never-offline cloud set.

This module imports no model runtime; ``vllm`` itself is an external server the user
launches. Talking to it is plain HTTP via the OpenAI SDK.
"""
from __future__ import annotations

from .openai_provider import OpenAIProvider

# vLLM's default OpenAI-compatible endpoint.
DEFAULT_BASE_URL = "http://localhost:8000/v1"
DEFAULT_MODEL = "qwen3-4b"


class VLLMProvider(OpenAIProvider):
    """OpenAI-compatible client for a vLLM serving instance, LoRA-adapter aware.

    Parameters
    ----------
    base_model:
        The base model's served name (used when no adapter is selected).
    adapter:
        The served LoRA adapter name (as registered via vLLM ``--lora-modules``), or ``None``
        to call the base model. When set, it becomes the request ``model`` field — that is how
        vLLM routes a call to a specific LoRA. Also exposed as ``self.adapter`` so the routing
        layer / telemetry can record which adapter answered.
    """

    name = "vllm"

    def __init__(
        self,
        base_model: str = DEFAULT_MODEL,
        *,
        adapter: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        supports_vision: bool = False,
        timeout: float = 120.0,
    ) -> None:
        self.base_model = base_model
        self.adapter = adapter
        # vLLM selects the LoRA by the request `model` name; base model when no adapter.
        served_name = adapter or base_model
        super().__init__(
            api_key=api_key or "vllm",
            model=served_name,
            base_url=base_url or DEFAULT_BASE_URL,
            supports_vision=supports_vision,
            timeout=timeout,
        )

    def telemetry_model(self) -> dict:
        """The ``model`` block for an ``ai_artifact`` event: provider/base-model/adapter."""
        return {"provider": self.name, "model": self.base_model, "adapter": self.adapter}
