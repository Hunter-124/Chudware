"""Provider configuration — resolve which backend + key to use.

Order of precedence: explicit arg → AI_CAD_PROVIDER env var → %APPDATA%/AI-CAD/config.json →
default. The env var outranks config.json on purpose: it is a per-process override (tests use
AI_CAD_PROVIDER=mock to guarantee no live model is called), while config.json is ambient user
state. config.json shape:

    {
      "provider": "anthropic",          // or "openai"
      "model": "claude-opus-4-8",
      "api_key": "sk-ant-...",          // optional; else read from env
      "openai": {"base_url": "http://localhost:1234/v1"}
    }
"""
from __future__ import annotations

import json
import os

from engine import paths

from .providers.base import Provider


def load_config() -> dict:
    if paths.CONFIG.exists():
        try:
            return json.loads(paths.CONFIG.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return {}
    return {}


DEFAULT_LOCAL_MODEL = "gemma-4-e4b"
DEFAULT_LOCAL_BASE_URL = "http://localhost:1234/v1"


def get_provider(name: str | None = None, cfg: dict | None = None) -> Provider:
    cfg = cfg if cfg is not None else load_config()
    name = (name or os.environ.get("AI_CAD_PROVIDER") or cfg.get("provider") or "").lower()

    if not name:
        # Nothing configured: default to a local LM Studio (OpenAI-compatible) endpoint
        # running gemma-4-e4b, so the app is testable out of the box on this machine.
        from .providers.openai_provider import OpenAIProvider

        oai = cfg.get("openai", {})
        return OpenAIProvider(
            api_key=cfg.get("api_key") or os.environ.get("OPENAI_API_KEY") or "lm-studio",
            model=cfg.get("model") or DEFAULT_LOCAL_MODEL,
            base_url=oai.get("base_url") or DEFAULT_LOCAL_BASE_URL,
            supports_vision=oai.get("vision", True),
        )

    if name == "mock":
        # Deterministic mock provider for tests (no API key, no network). Scripted replies load
        # from the JSON file named by AI_CAD_MOCK_REPLIES (a list[str], or a {role: list[str]} map
        # consumed by a role-sniffing provider) — keeps the subprocess worker test self-contained.
        from .providers.mock_provider import MockProvider

        replies: list[str] = []
        by_role: dict | None = None
        path = os.environ.get("AI_CAD_MOCK_REPLIES")
        if path and os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                by_role = data
            elif isinstance(data, list):
                replies = data
        if by_role is not None:
            from .providers.mock_provider import RoleScriptedProvider
            return RoleScriptedProvider(by_role)
        return MockProvider(replies or [""])

    if name == "anthropic":
        from .providers.anthropic_provider import AnthropicProvider, DEFAULT_MODEL

        key = cfg.get("api_key") or os.environ.get("ANTHROPIC_API_KEY")
        return AnthropicProvider(api_key=key, model=cfg.get("model", DEFAULT_MODEL))

    if name == "openai":
        from .providers.openai_provider import OpenAIProvider, DEFAULT_MODEL

        key = cfg.get("api_key") or os.environ.get("OPENAI_API_KEY")
        oai = cfg.get("openai", {})
        return OpenAIProvider(api_key=key, model=cfg.get("model", DEFAULT_MODEL),
                              base_url=oai.get("base_url"),
                              supports_vision=oai.get("vision", True))

    raise ValueError(f"Unknown provider: {name!r}")
