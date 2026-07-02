"""Provider configuration — resolve which backend + key to use.

Order of precedence: explicit arg → AI_CAD_PROVIDER env var → %APPDATA%/AI-CAD/config.json →
default. The env var outranks config.json on purpose: it is a per-process override (tests use
AI_CAD_PROVIDER=mock to guarantee no live model is called), while config.json is ambient user
state. config.json shape:

    {
      "provider": "anthropic",          // or "openai" / "llamacpp"
      "model": "claude-opus-4-8",
      "api_key": "sk-ant-...",          // optional; else read from env
      "openai": {"base_url": "http://localhost:1234/v1"},
      "llamacpp": {"model_path": "%APPDATA%/AI-CAD/models/model.gguf"}
    }
"""
from __future__ import annotations

import json
import os
from urllib.parse import urlparse

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


# --------------------------------------------------------------------------- offline mode (C35)
# LOCAL-ONLY / OFFLINE MODE — the ITAR-compliance feature. When config.json "offline_mode" is
# true NO technical data may leave the machine: only providers whose endpoint is a loopback host
# (localhost / 127.0.0.1 / ::1) are allowed; cloud providers (Anthropic, OpenAI) are refused
# outright. Enforced here at provider resolution AND re-asserted in the agent loop before every
# run (defense in depth). The error always names the setting so the user knows what to change.

_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]", "0.0.0.0"}
OFFLINE_SETTING = "offline_mode"


def offline_enabled(cfg: dict | None = None) -> bool:
    cfg = cfg if cfg is not None else load_config()
    return bool(cfg.get(OFFLINE_SETTING, False))


def _host_is_loopback(base_url: str | None) -> bool:
    """True if *base_url*'s host is a loopback address (the only egress allowed offline)."""
    if not base_url:
        return False
    host = (urlparse(base_url).hostname or "").lower()
    return host in _LOOPBACK_HOSTS


class OfflineModeError(RuntimeError):
    """Raised when offline mode would be violated by the resolved provider/endpoint."""


def provider_offline_safe(provider: Provider) -> bool:
    """True if *provider* keeps all traffic on the local machine (loopback endpoint).

    Cloud SDK providers (Anthropic, OpenAI cloud) talk to remote APIs and are never offline-safe.
    A local OpenAI-compatible endpoint (LM Studio etc.) is safe only when its base_url is loopback.
    The mock provider is always safe (it makes no network calls — used by tests).
    """
    # A provider may explicitly declare it makes no network calls (test doubles, in-process
    # backends). This is the general, name-independent escape hatch for offline-safety.
    if getattr(provider, "offline_safe", False):
        return True
    name = getattr(provider, "name", "")
    if name in ("mock", "mock-roles", "base"):
        return True
    if name in ("anthropic", "gemini", "openrouter"):
        return False  # always cloud
    base_url = getattr(provider, "base_url", None)
    if name == "openai":
        # No base_url means the real OpenAI cloud endpoint; otherwise check the host.
        return _host_is_loopback(base_url)
    if name == "llamacpp":
        return _host_is_loopback(base_url or DEFAULT_LOCAL_BASE_URL)
    # Unknown provider with a loopback base_url is allowed; anything else is refused.
    return _host_is_loopback(base_url)


def assert_offline_safe(provider: Provider, cfg: dict | None = None) -> None:
    """Defense-in-depth guard the agent loop calls before every run when offline mode is set.

    No-op when offline mode is off. When on, raises :class:`OfflineModeError` (naming the setting)
    if *provider* is not offline-safe.
    """
    if not offline_enabled(cfg):
        return
    if not provider_offline_safe(provider):
        name = getattr(provider, "name", "?")
        base = getattr(provider, "base_url", None) or "(cloud endpoint)"
        raise OfflineModeError(
            f"offline mode is enabled (config '{OFFLINE_SETTING}': true): the resolved provider "
            f"{name!r} -> {base} is NOT local-only, so the run is refused. Switch to a local "
            f"(loopback) model server or disable offline mode in Settings."
        )


def get_provider(name: str | None = None, cfg: dict | None = None) -> Provider:
    """Resolve the configured provider, enforcing offline mode (C35) at two points: cloud
    providers are refused by name BEFORE construction, and the resolved provider is re-checked
    against ``provider_offline_safe`` AFTER (catches the empty-name local default + edge cases)."""
    cfg = cfg if cfg is not None else load_config()
    provider = _resolve_provider(name, cfg)
    assert_offline_safe(provider, cfg)
    return provider


def _resolve_provider(name: str | None, cfg: dict) -> Provider:
    name = (name or os.environ.get("AI_CAD_PROVIDER") or cfg.get("provider") or "").lower()
    offline = offline_enabled(cfg)

    # Offline mode: refuse cloud providers OUTRIGHT before constructing them (no client, no key
    # lookup). A local OpenAI-compatible endpoint is checked against its base_url below.
    if offline and name == "gemini":
        raise OfflineModeError(
            f"offline mode is enabled (config '{OFFLINE_SETTING}': true): the Google Gemini "
            f"cloud provider is refused. Use a local (loopback) model server instead.")
    if offline and name == "openrouter":
        raise OfflineModeError(
            f"offline mode is enabled (config '{OFFLINE_SETTING}': true): the OpenRouter "
            f"cloud provider is refused. Use a local (loopback) model server instead.")
    if offline and name in ("anthropic", "openai") and name != "":
        if name == "anthropic":
            raise OfflineModeError(
                f"offline mode is enabled (config '{OFFLINE_SETTING}': true): the Anthropic "
                f"cloud provider is refused. Use a local (loopback) model server instead.")
        # openai: only allowed if a loopback base_url is configured.
        if not _host_is_loopback((cfg.get("openai", {}) or {}).get("base_url")):
            raise OfflineModeError(
                f"offline mode is enabled (config '{OFFLINE_SETTING}': true): the OpenAI cloud "
                f"provider is refused. Configure a local (loopback) base_url, or disable offline "
                f"mode in Settings.")
    if offline and name in ("llamacpp", "llama.cpp", "llama-cpp"):
        lcfg = dict(cfg.get("llamacpp", {}) or {})
        host = str(lcfg.get("host") or "127.0.0.1")
        port = int(lcfg.get("port") or 1234)
        base_url = lcfg.get("base_url") or f"http://{host}:{port}/v1"
        if not _host_is_loopback(base_url):
            raise OfflineModeError(
                f"offline mode is enabled (config '{OFFLINE_SETTING}': true): the managed "
                f"llama.cpp endpoint {base_url!r} is not loopback, so it is refused before "
                f"probing the server.")

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
            timeout=float(oai.get("timeout", 120.0)),
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

    if name == "gemini":
        from .providers.gemini_provider import GeminiProvider, DEFAULT_MODEL

        key = cfg.get("api_key") or os.environ.get("GEMINI_API_KEY") or os.environ.get(
            "GOOGLE_API_KEY")
        return GeminiProvider(api_key=key, model=cfg.get("model", DEFAULT_MODEL),
                              reasoning_effort=cfg.get("reasoning_effort"))

    if name == "openai":
        from .providers.openai_provider import OpenAIProvider, DEFAULT_MODEL

        key = cfg.get("api_key") or os.environ.get("OPENAI_API_KEY")
        oai = cfg.get("openai", {})
        return OpenAIProvider(api_key=key, model=cfg.get("model", DEFAULT_MODEL),
                              base_url=oai.get("base_url"),
                              supports_vision=oai.get("vision", True),
                              timeout=float(oai.get("timeout", 120.0)))

    if name == "openrouter":
        # OpenAI-compatible endpoint pinned to OpenRouter. Key is BYO (OPENROUTER_API_KEY)
        # or minted by the blessed PKCE login and stored as api_key (contract §4.1).
        from .providers.openrouter_provider import OpenRouterProvider, DEFAULT_MODEL

        key = cfg.get("api_key") or os.environ.get("OPENROUTER_API_KEY")
        oc = cfg.get("openrouter", {}) or {}
        return OpenRouterProvider(api_key=key, model=cfg.get("model", DEFAULT_MODEL),
                                  supports_vision=oc.get("vision", True),
                                  timeout=float(oc.get("timeout", 120.0)))

    if name in ("llamacpp", "llama.cpp", "llama-cpp"):
        from .providers.llamacpp_provider import LlamaCppProvider, DEFAULT_MODEL

        lcfg = dict(cfg.get("llamacpp", {}) or {})
        lcfg.setdefault("model", cfg.get("model") or DEFAULT_MODEL)
        # Keep the endpoint explicit in the provider object so offline-safety and diagnostics name it.
        lcfg.setdefault("base_url", DEFAULT_LOCAL_BASE_URL)
        return LlamaCppProvider(lcfg, model=lcfg["model"])

    raise ValueError(f"Unknown provider: {name!r}")
