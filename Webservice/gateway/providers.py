"""Read-only host AI-provider availability for the web client.

GET /api/providers lists which providers the host is signed into (config.json
``keys.*`` non-empty and/or ``subscription_auth`` active accounts) plus model
presets by id/label only. Credentials stay 100% server-side — never returned.

Multi-tenant caveat: all web agent turns still spend the host user's accounts;
per-user billing isolation is out of scope.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request

try:
    from .auth import get_current_user
except ImportError:  # uvicorn app:app from package dir
    from auth import get_current_user

log = logging.getLogger("billet-web.providers")

router = APIRouter(tags=["providers"])

# Repo root (…/CAD) so we can import services.agent without requiring a site install.
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# keys.* slot → wire provider name + human label.
# cloud_compat is the GUI's shared OpenAI-compatible cloud slot (OpenRouter / DashScope / …).
_KEY_PROVIDERS: tuple[tuple[str, str, str], ...] = (
    ("gemini", "gemini", "Google Gemini"),
    ("anthropic", "anthropic", "Anthropic (Claude)"),
    ("openai", "openai", "OpenAI"),
    ("xai", "xai", "xAI (Grok)"),
    ("moonshot", "moonshot", "Moonshot (Kimi)"),
    ("openrouter", "openrouter", "OpenRouter"),
    ("cloud_compat", "openai", "OpenRouter / Cloud API"),
)

# Static model presets (id, label) per wire provider name — mirrors host model_registry
# builtins / cloud presets so the web picker has something without probing live.
_MODEL_PRESETS: dict[str, list[dict[str, str]]] = {
    "gemini": [
        {"id": "gemini-2.5-pro", "label": "Gemini 2.5 Pro"},
        {"id": "gemini-2.5-flash", "label": "Gemini 2.5 Flash"},
        {"id": "gemini-2.0-flash", "label": "Gemini 2.0 Flash"},
    ],
    "anthropic": [
        {"id": "claude-opus-4-8", "label": "Claude Opus 4.8"},
        {"id": "claude-sonnet-5", "label": "Claude Sonnet 5"},
        {"id": "claude-fable-5", "label": "Claude Fable 5"},
        {"id": "claude-haiku-4-5", "label": "Claude Haiku 4.5"},
    ],
    "anthropic-sub": [
        {"id": "claude-opus-4-8", "label": "Claude Opus 4.8"},
        {"id": "claude-sonnet-5", "label": "Claude Sonnet 5"},
        {"id": "claude-fable-5", "label": "Claude Fable 5"},
        {"id": "claude-haiku-4-5", "label": "Claude Haiku 4.5"},
    ],
    "openai": [
        {"id": "gpt-5.6-sol", "label": "GPT-5.6 Sol"},
        {"id": "o3", "label": "OpenAI o3"},
    ],
    "codex-sub": [
        {"id": "gpt-5-codex", "label": "GPT-5 Codex"},
        {"id": "gpt-5.6-sol", "label": "GPT-5.6 Sol"},
    ],
    "kimi-sub": [
        {"id": "kimi-for-coding", "label": "Moonshot · Kimi For Coding (subscription)"},
    ],
    "xai": [
        {"id": "grok-4.5", "label": "Grok 4.5 (default, 500k)"},
        {"id": "grok-4-1", "label": "Grok 4.1"},
        {"id": "grok-4", "label": "Grok 4"},
        {"id": "grok-4.20-0309-reasoning", "label": "Grok 4.20 Reasoning"},
        {"id": "grok-build-0.1", "label": "Grok Build 0.1"},
    ],
    "xai-sub": [
        {"id": "grok-4.5", "label": "Grok 4.5 (default, 500k)"},
        {"id": "grok-4-1", "label": "Grok 4.1"},
        {"id": "grok-4", "label": "Grok 4"},
        {"id": "grok-4.20-0309-reasoning", "label": "Grok 4.20 Reasoning"},
        {"id": "grok-build-0.1", "label": "Grok Build 0.1"},
    ],
    "cursor-sub": [
        {"id": "composer-2.5", "label": "Cursor Composer 2.5"},
        {"id": "cursor/claude-opus-4-8", "label": "Claude Opus 4.8 (Cursor)"},
        {"id": "cursor/claude-sonnet-5", "label": "Claude Sonnet 5 (Cursor)"},
        {"id": "cursor/gpt-5.6-sol", "label": "GPT-5.6 Sol (Cursor)"},
    ],
    "moonshot": [
        {"id": "kimi-k3", "label": "Moonshot · Kimi K3 (1M)"},
        {"id": "kimi-k2.6", "label": "Moonshot · Kimi K2.6 (256K)"},
    ],
    "openrouter": [
        {"id": "qwen/qwen3-coder:free", "label": "OpenRouter · Qwen3-Coder (free)"},
        {"id": "qwen/qwen3-coder", "label": "OpenRouter · Qwen3-Coder (paid)"},
        {"id": "qwen/qwen3-vl-32b-instruct", "label": "OpenRouter · Qwen3-VL 32B"},
        {"id": "qwen/qwen3-max", "label": "OpenRouter · Qwen3-Max"},
        {"id": "deepseek/deepseek-chat", "label": "OpenRouter · DeepSeek V3"},
        {"id": "openai/gpt-oss-120b:free", "label": "OpenRouter · GPT-OSS 120B (free)"},
        {"id": "openrouter/auto", "label": "OpenRouter Auto"},
    ],
}

# Secret-bearing keys that must never appear in a response payload.
_SECRET_KEYS = frozenset({
    "api_key", "access_token", "refresh_token", "id_token", "token",
    "fallback_api_keys", "client_secret", "password", "secret",
    "authorization", "bearer",
})


def _key_present(value: Any) -> bool:
    """True when a config keys.* slot holds a non-empty string (presence only)."""
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    # Never treat non-string blobs as presence (avoid leaking structure).
    return False


def _load_host_config() -> dict[str, Any]:
    """Load host config.json via the agent config helper (read-only)."""
    try:
        from services.agent.config import load_config  # noqa: PLC0415
        cfg = load_config()
        return cfg if isinstance(cfg, dict) else {}
    except Exception as exc:  # noqa: BLE001 — endpoint must stay available
        log.warning("host config load failed: %s", exc)
        return {}


def _subscription_status() -> dict[str, dict]:
    """Per-provider subscription connection state (no tokens)."""
    try:
        from services.agent.providers import subscription_login as sl  # noqa: PLC0415
        st = sl.status()
        return st if isinstance(st, dict) else {}
    except Exception as exc:  # noqa: BLE001
        log.warning("subscription status failed: %s", exc)
        return {}


def _scrub(obj: Any) -> Any:
    """Defense-in-depth: drop any secret-looking keys from nested dicts."""
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for k, v in obj.items():
            if str(k).lower() in _SECRET_KEYS:
                continue
            if any(s in str(k).lower() for s in ("token", "secret", "password", "api_key")):
                continue
            out[k] = _scrub(v)
        return out
    if isinstance(obj, list):
        return [_scrub(x) for x in obj]
    return obj


def list_available_providers(cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build the signed-in provider list from host config (names/labels/models only).

    A provider is available when:
      - ``keys.<slot>`` is a non-empty string, or
      - ``subscription_auth`` has a connected account for that provider
        (via subscription_login.status / list_accounts — no tokens).
    """
    cfg = cfg if cfg is not None else _load_host_config()
    keys = cfg.get("keys") if isinstance(cfg.get("keys"), dict) else {}
    active_provider = str(cfg.get("provider") or "").strip().lower() or None
    active_model = str(cfg.get("model") or "").strip() or None

    providers: list[dict[str, Any]] = []
    seen: set[str] = set()

    # API-key providers from keys.*
    for slot, wire_name, label in _KEY_PROVIDERS:
        if not _key_present(keys.get(slot)):
            continue
        # cloud_compat shares the openai wire name; only emit once, prefer openrouter
        # when keys.openrouter is also set, else report as openrouter-compatible openai.
        if wire_name in seen and slot == "cloud_compat":
            continue
        if wire_name in seen and slot != "cloud_compat":
            continue
        # Prefer dedicated openrouter slot label when present.
        if slot == "cloud_compat" and _key_present(keys.get("openrouter")):
            continue
        entry_id = wire_name
        if slot == "cloud_compat" and not _key_present(keys.get("openrouter")):
            # Generic cloud OpenAI-compatible key without a dedicated openrouter slot.
            entry_id = "openai"
            label = "OpenRouter / Cloud API"
        if entry_id in seen:
            continue
        seen.add(entry_id)
        models = list(_MODEL_PRESETS.get(entry_id, _MODEL_PRESETS.get(wire_name, [])))
        providers.append({
            "id": entry_id,
            "name": entry_id,
            "label": label,
            "auth": "api_key",
            "models": models,
        })

    # Subscription-auth providers (OAuth) — contract-shaped status, no tokens.
    sub = _subscription_status()
    # friendly id -> wire provider name used by resolve_provider / agent turns
    _sub_wire = {
        "claude": ("anthropic-sub", "Anthropic Claude (Pro/Max)"),
        "codex": ("codex-sub", "OpenAI Codex (ChatGPT Plus/Pro)"),
        "cursor": ("cursor-sub", "Cursor (Pro)"),
        "xai": ("xai-sub", "xAI Grok (SuperGrok / X Premium+)"),
        "kimi": ("kimi-sub", "Moonshot Kimi (Kimi For Coding)"),
    }
    for friendly, info in sub.items():
        if not isinstance(info, dict):
            continue
        connected = bool(info.get("connected") or info.get("usable"))
        if not connected:
            continue
        wire, default_label = _sub_wire.get(friendly, (friendly, friendly))
        # If API-key xai/anthropic already listed, still expose the -sub wire name
        # so the client can select subscription path explicitly.
        if wire in seen:
            continue
        seen.add(wire)
        accounts = info.get("accounts") if isinstance(info.get("accounts"), list) else []
        # Strip any residual secret fields from account summaries (list_accounts is
        # already token-free; scrub is defense-in-depth).
        safe_accounts = []
        for acct in accounts:
            if not isinstance(acct, dict):
                continue
            safe_accounts.append({
                "account_id": str(acct.get("account_id") or ""),
                "label": str(acct.get("label") or acct.get("email") or acct.get("account_id") or ""),
                "email": str(acct.get("email") or "") or None,
                "active": bool(acct.get("active")),
            })
        providers.append({
            "id": wire,
            "name": wire,
            "label": str(info.get("label") or default_label),
            "auth": "subscription",
            "email": str(info.get("email") or "") or None,
            "account_id": str(info.get("account_id") or "") or None,
            "accounts": safe_accounts,
            "models": list(_MODEL_PRESETS.get(wire, [])),
        })

    # When xAI has both API key and subscription, prefer listing both (xai + xai-sub).
    # API-key path already added as "xai"; subscription as "xai-sub" above.

    payload = {
        "providers": providers,
        "default_provider": active_provider,
        "default_model": active_model,
        # Multi-tenant caveat (documented for clients): turns spend host accounts.
        "shared_host_accounts": True,
    }
    return _scrub(payload)


@router.get("/api/providers")
async def api_providers(request: Request) -> dict[str, Any]:
    """List host-signed-in AI providers + model presets (names only, no secrets)."""
    get_current_user(request)  # 401 if unauthenticated (AuthMiddleware already gates)
    return list_available_providers()


__all__ = ["router", "list_available_providers"]
