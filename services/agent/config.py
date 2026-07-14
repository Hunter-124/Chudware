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


def save_config(cfg: dict) -> None:
    """Read-merge-write a partial update into config.json (creating it on first run).

    Merges shallowly into whatever is already on disk so one caller (e.g. a first-run
    consent prompt setting telemetry_consent) never clobbers keys another caller owns
    (provider/api_key/etc.) — the same non-destructive contract the C++ GUI's first-run
    dialog follows when it writes this file directly (ui/imgui/src/ui/first_run.cpp).
    """
    current = load_config()
    current.update(cfg)
    paths.CONFIG.parent.mkdir(parents=True, exist_ok=True)
    paths.CONFIG.write_text(json.dumps(current, indent=2), encoding="utf-8")


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


_VALID_CONSENT_TIERS = {"off", "meta", "full"}


def consent_tier(cfg: dict) -> str:
    """Return the telemetry consent tier for the given config.

    - "off"  — no telemetry uploaded (events still written locally)
    - "meta" — metadata only (timings, model name, hashes; no geometry)
    - "full" — full telemetry including geometry references

    Offline mode hard-overrides to "off" regardless of any configured value.
    Unknown values default to "off" (external-default, safe).
    """
    if cfg.get(OFFLINE_SETTING):
        return "off"
    tier = cfg.get("telemetry_consent", "off")
    return tier if tier in _VALID_CONSENT_TIERS else "off"


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
    if name in ("anthropic", "gemini", "openrouter", "xai",
                "anthropic-sub", "codex-sub", "cursor-sub", "xai-sub"):
        return False  # always cloud (incl. subscription-auth providers, §4.4)
    base_url = getattr(provider, "base_url", None)
    if name == "openai":
        # No base_url means the real OpenAI cloud endpoint; otherwise check the host.
        return _host_is_loopback(base_url)
    if name == "llamacpp":
        return _host_is_loopback(base_url or DEFAULT_LOCAL_BASE_URL)
    if name == "vllm":
        # vLLM serves LoRA adapters locally (T-H9); offline-safe only when its endpoint is
        # loopback — same rule as openai/llamacpp, NOT the never-offline cloud set.
        return _host_is_loopback(base_url or "http://localhost:8000/v1")
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


def get_provider(name: str | None = None, cfg: dict | None = None,
                 role: str | None = None) -> Provider:
    """Resolve the configured provider, enforcing offline mode (C35) at two points: cloud
    providers are refused by name BEFORE construction, and the resolved provider is re-checked
    against ``provider_offline_safe`` AFTER (catches the empty-name local default + edge cases).

    *role* (optional) is a routing.yaml role tag (e.g. ``"overseer"``, ``"fixer"``, or a DAG
    node name) — see ``_resolve_via_routing``. When *name* / ``AI_CAD_PROVIDER`` is not given,
    routing.yaml's role->source assignment (contract §14) is consulted BEFORE falling back to
    config.json's flat ``provider`` field, so a source registered into routing.yaml (e.g. the
    bundled llama.cpp runtime's ``register_routing_source``) actually gets picked up here."""
    cfg = cfg if cfg is not None else load_config()
    provider = _resolve_provider(name, cfg, role=role)
    assert_offline_safe(provider, cfg)
    return provider


# Routing-source `kind` -> provider `name` understood by `_resolve_provider` below.
# lmstudio/ollama have no dedicated provider class: both speak the OpenAI-compatible
# /v1/chat/completions protocol, so they route through the generic "openai" provider
# with the source's own base_url.
_ROUTING_KIND_TO_PROVIDER_NAME = {
    "anthropic": "anthropic",
    "openai": "openai",
    "openrouter": "openrouter",
    "gemini": "gemini",
    "xai": "xai",
    "llamacpp": "llamacpp",
    "vllm": "vllm",
    "mock": "mock",
    "lmstudio": "openai",
    "ollama": "openai",
}

# cfg sub-key each provider name reads its base_url from (see _resolve_provider below).
_PROVIDER_BASE_URL_CFG_KEY = {
    "openai": "openai",
    "llamacpp": "llamacpp",
    "vllm": "vllm",
    "openrouter": "openrouter",
}


def _resolve_via_routing(role: str | None, cfg: dict) -> tuple[str, str | None, str | None] | None:
    """Resolve *role* against routing.yaml (contract §14). Returns
    ``(provider_name, model_id, base_url)`` or ``None`` when routing.yaml has no
    configured sources / can't resolve the role — the caller falls back to
    config.json in that case. Never raises: routing.yaml is a best-effort,
    user-editable file and a malformed/missing one must not break provider
    resolution."""
    try:
        from .routing import load_routing  # noqa: PLC0415 — avoid import cost/cycles
        # Re-parse routing.yaml from disk each call (not the process-lifetime cache
        # in get_routing_table()) — the file is user-editable and can also be mutated
        # at runtime by local_runtime.register_routing_source(), so a stale cache would
        # silently ignore a source registered after the first resolution in-process.
        table = load_routing()
        resolution = table.resolve(role or "default")
        if resolution is None:
            return None
        source = table.get_source(resolution.source_id)
        kind = source.kind if source is not None else ""
        provider_name = _ROUTING_KIND_TO_PROVIDER_NAME.get(kind)
        if provider_name is None:
            return None
        base_url = source.base_url if source is not None else None
        return provider_name, resolution.model_id, base_url
    except Exception:  # noqa: BLE001 — routing.yaml must never block provider resolution
        return None


def _resolve_provider(name: str | None, cfg: dict, role: str | None = None) -> Provider:
    routed_model: str | None = None
    routed_base_url: str | None = None
    if not name and not os.environ.get("AI_CAD_PROVIDER"):
        routed = _resolve_via_routing(role, cfg)
        if routed is not None:
            name, routed_model, routed_base_url = routed

    if routed_base_url or routed_model:
        # Overlay the routing.yaml decision onto a shallow cfg copy so the existing
        # per-provider branches below (which read cfg.get("model") / cfg[kind]["base_url"])
        # pick it up without needing their own routing-awareness.
        cfg = dict(cfg)
        if routed_model:
            cfg["model"] = routed_model
        sub_key = _PROVIDER_BASE_URL_CFG_KEY.get((name or "").lower())
        if routed_base_url and sub_key:
            cfg[sub_key] = dict(cfg.get(sub_key) or {})
            cfg[sub_key].setdefault("base_url", routed_base_url)

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
    if offline and name == "xai":
        raise OfflineModeError(
            f"offline mode is enabled (config '{OFFLINE_SETTING}': true): the xAI "
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
    if offline and name == "vllm":
        vc = dict(cfg.get("vllm", {}) or {})
        base_url = vc.get("base_url") or "http://localhost:8000/v1"
        if not _host_is_loopback(base_url):
            raise OfflineModeError(
                f"offline mode is enabled (config '{OFFLINE_SETTING}': true): the vLLM serving "
                f"endpoint {base_url!r} is not loopback, so it is refused before construction.")
    if offline and name in ("anthropic-sub", "codex-sub", "cursor-sub", "xai-sub"):
        # Subscription-auth providers (§4.4) always talk to a vendor cloud endpoint.
        raise OfflineModeError(
            f"offline mode is enabled (config '{OFFLINE_SETTING}': true): the subscription "
            f"provider {name!r} uses a vendor cloud endpoint and is refused. Use a local "
            f"(loopback) model server instead.")

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

    if name == "xai":
        # Classic API-key path (console.x.ai). OpenAI-compatible at https://api.x.ai/v1.
        from .providers.xai_provider import XAIProvider, DEFAULT_MODEL, BASE_URL

        key = cfg.get("api_key") or os.environ.get("XAI_API_KEY")
        xc = cfg.get("xai", {}) or {}
        return XAIProvider(
            api_key=key,
            model=cfg.get("model", DEFAULT_MODEL),
            supports_vision=xc.get("vision", True),
            timeout=float(xc.get("timeout", 120.0)),
            base_url=xc.get("base_url") or BASE_URL,
        )

    if name == "anthropic-sub":
        # Subscription auth (§4.4): uses the user's Claude Pro/Max login, not an api_key.
        from .providers.anthropic_sub_provider import AnthropicSubProvider, DEFAULT_MODEL

        return AnthropicSubProvider(model=cfg.get("model", DEFAULT_MODEL))

    if name == "codex-sub":
        # Subscription auth (§4.4): uses the user's ChatGPT Plus/Pro login.
        from .providers.codex_sub_provider import CodexSubProvider, DEFAULT_MODEL

        return CodexSubProvider(model=cfg.get("model", DEFAULT_MODEL))

    if name == "cursor-sub":
        # Subscription auth (§4.4): uses the user's Cursor Pro login.
        from .providers.cursor_sub_provider import CursorSubProvider, DEFAULT_MODEL

        return CursorSubProvider(model=cfg.get("model", DEFAULT_MODEL))

    if name == "xai-sub":
        # Subscription auth (§4.4): SuperGrok / X Premium+ device-code OAuth.
        from .providers.xai_sub_provider import XAISubProvider, DEFAULT_MODEL

        return XAISubProvider(model=cfg.get("model", DEFAULT_MODEL))
    if name in ("llamacpp", "llama.cpp", "llama-cpp"):
        from .providers.llamacpp_provider import LlamaCppProvider, DEFAULT_MODEL

        lcfg = dict(cfg.get("llamacpp", {}) or {})
        lcfg.setdefault("model", cfg.get("model") or DEFAULT_MODEL)
        # Keep the endpoint explicit in the provider object so offline-safety and diagnostics name it.
        lcfg.setdefault("base_url", DEFAULT_LOCAL_BASE_URL)
        return LlamaCppProvider(lcfg, model=lcfg["model"])

    if name == "vllm":
        # T-H9: per-task LoRA serving. OpenAI-compatible vLLM instance; the served LoRA is
        # picked by the `adapter` field. Enters as just another providers/1 adapter.
        from .providers.vllm_provider import (VLLMProvider, DEFAULT_BASE_URL,
                                              DEFAULT_MODEL as VLLM_DEFAULT_MODEL)

        vc = dict(cfg.get("vllm", {}) or {})
        return VLLMProvider(
            base_model=vc.get("model") or cfg.get("model") or VLLM_DEFAULT_MODEL,
            adapter=vc.get("adapter"),
            api_key=cfg.get("api_key") or os.environ.get("OPENAI_API_KEY"),
            base_url=vc.get("base_url") or DEFAULT_BASE_URL,
            supports_vision=vc.get("vision", False),
            timeout=float(vc.get("timeout", 120.0)),
        )

    raise ValueError(f"Unknown provider: {name!r}")
