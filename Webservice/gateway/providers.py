"""Account-scoped provider catalogue for the web model picker.

Only sources that a customer turn can authenticate are advertised: validated
BYOK keys, usable personal OAuth links, and a configured metered pool. Neither
hostd's config nor its desktop subscription store is a web credential.
"""

from __future__ import annotations

import asyncio
import os
import logging
from typing import Any

from fastapi import APIRouter, Request

try:
    from .auth import get_current_user
    from .credits import actor_for
    from .provider_accounts import resolve_subscription_token
    from .provider_keys import list_keys
except ImportError:  # uvicorn app:app from package dir
    from auth import get_current_user
    from credits import actor_for
    from provider_accounts import resolve_subscription_token
    from provider_keys import list_keys

log = logging.getLogger("chudware-web.providers")

router = APIRouter(tags=["providers"])


# Validated web BYOK provider ids that Chudcode can isolate via a dedicated env
# variable. Moonshot's validated key is not an online Chudcode env credential.
_KEY_PROVIDERS: dict[str, str] = {
    "gemini": "Google Gemini",
    "anthropic": "Anthropic (Claude)",
    "openai": "OpenAI",
    "xai": "xAI (Grok)",
    "openrouter": "OpenRouter",
}

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
    ],
    "xai": [
        {"id": "grok-4.5", "label": "Grok 4.5 (default, 500k)"},
        {"id": "grok-4-1", "label": "Grok 4.1"},
        {"id": "grok-4.20-0309-reasoning", "label": "Grok 4.20 Reasoning"},
        {"id": "grok-build-0.1", "label": "Grok Build 0.1"},
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

# Only Claude's personal subscription bearer has a supported isolated
# Chudcode injection path. Linked Codex is intentionally absent from this
# picker until it can run without the operator's ambient login.
_SUB_LABELS = {"anthropic-sub": "Anthropic Claude (Pro/Max)"}

# Secret-bearing keys that must never appear in a response payload.
_SECRET_KEYS = frozenset({
    "api_key", "access_token", "refresh_token", "id_token", "token",
    "fallback_api_keys", "client_secret", "password", "secret",
    "authorization", "bearer",
})



def _account_subscriptions(email: str) -> dict[str, dict[str, Any]]:
    """Only links supported by isolated Chudcode auth enter the picker."""
    try:
        grant = resolve_subscription_token(email, "anthropic")
    except Exception as exc:  # noqa: BLE001 - catalogue degrades if a link is unreadable
        log.warning("account subscription lookup failed: %s", type(exc).__name__)
        return {}
    return {"anthropic-sub": grant} if grant is not None else {}


def _scrub(obj: Any) -> Any:
    """Defense-in-depth: drop any secret-looking keys from nested dicts."""
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for k, v in obj.items():
            key = str(k).lower()
            if key in _SECRET_KEYS:
                continue
            if any(s in key for s in (
                "token", "secret", "password", "api_key", "apikey", "credential",
            )):
                continue
            out[k] = _scrub(v)
        return out
    if isinstance(obj, list):
        return [_scrub(x) for x in obj]
    return obj


def list_available_providers(*, email: str | None = None) -> dict[str, Any]:
    """List only credential sources this account can actually use for a turn."""
    normalized = str(email or "").strip().lower()
    if not normalized:
        return {"providers": [], "default_provider": None,
                "default_model": None, "shared_host_accounts": False}

    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    try:
        own_keys = list_keys(normalized)
    except Exception as exc:  # noqa: BLE001 - a broken store must not expose host keys
        log.warning("account key catalogue unavailable: %s", type(exc).__name__)
        own_keys = []
    for row in own_keys:
        provider = str(row.get("provider") or "")
        if row.get("state") != "validated" or provider not in _KEY_PROVIDERS:
            continue
        seen.add(provider)
        entries.append({
            "id": provider, "name": provider,
            "label": _KEY_PROVIDERS[provider], "auth": "api_key",
            "models": list(_MODEL_PRESETS.get(provider, [])),
        })

    # Only personal Claude OAuth can be injected into an isolated Chudcode
    # process. A linked Codex bearer and every host/operator OAuth account are
    # deliberately absent: those turns refuse rather than use ambient auth.
    for wire in _account_subscriptions(normalized):
        if wire in seen:
            continue
        seen.add(wire)
        entries.append({
            "id": wire, "name": wire, "label": _SUB_LABELS[wire],
            "auth": "subscription", "models": list(_MODEL_PRESETS[wire]),
        })

    try:
        pool_granted = actor_for(normalized)["key_source"] == "pool"
    except Exception as exc:  # noqa: BLE001 - catalogue degrades if billing DB is down
        log.warning("pool entitlement unavailable: %s", type(exc).__name__)
        pool_granted = False
    pool_key = os.environ.get("CHUDWARE_WEB_POOL_API_KEY", "").strip()
    pool_provider = os.environ.get("CHUDWARE_WEB_POOL_PROVIDER", "openrouter").strip().lower()
    pool_model = os.environ.get("CHUDWARE_WEB_POOL_MODEL", "").strip()
    if pool_granted and pool_key and pool_provider in _KEY_PROVIDERS and pool_provider not in seen:
        models = (
            [{"id": pool_model, "label": pool_model}]
            if pool_model else list(_MODEL_PRESETS.get(pool_provider, []))
        )
        entries.append({
            "id": pool_provider, "name": pool_provider,
            "label": _KEY_PROVIDERS[pool_provider], "auth": "pool",
            "models": models,
        })

    default = entries[0] if entries else None
    return _scrub({
        "providers": entries,
        "default_provider": default["id"] if default else None,
        "default_model": default["models"][0]["id"] if default and default["models"] else None,
        "shared_host_accounts": False,
    })


@router.get("/api/providers")
async def api_providers(request: Request) -> dict[str, Any]:
    """List this web account's available providers without credential bytes."""
    user = get_current_user(request)  # 401 if unauthenticated
    return await asyncio.to_thread(
        list_available_providers, email=getattr(user, "email", "") or ""
    )


__all__ = ["router", "list_available_providers"]
