"""Managed llama.cpp provider.

Wraps the generic OpenAI-compatible provider, but first ensures a local llama.cpp server is
reachable. This keeps the agent/provider layer clean while giving Chudware a real packaging seam for a
headless local model backend.
"""
from __future__ import annotations

from .openai_provider import OpenAIProvider

DEFAULT_MODEL = "local-gguf"


class LlamaCppProvider(OpenAIProvider):
    name = "llamacpp"

    def __init__(self, cfg: dict | None = None, model: str = DEFAULT_MODEL):
        from installer import llamacpp

        cfg = cfg or {}
        self.runtime = llamacpp.ensure_server_running(cfg)
        super().__init__(
            api_key=cfg.get("api_key") or "llama-cpp",
            model=cfg.get("model") or model,
            base_url=self.runtime["base_url"],
            supports_vision=bool(cfg.get("vision", False)),
        )
