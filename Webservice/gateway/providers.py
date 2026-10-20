"""Account-scoped provider catalogue for the web model picker.

Only supported tenant-broker BYOK sources are advertised. Pooled credits and
subscription links do not authenticate this release's cell transport; neither
hostd's config nor its desktop subscription store is a web credential.
"""

from __future__ import annotations

import asyncio
import os
import logging
import json
import re
import subprocess
import threading
import time
from urllib.request import Request as UrlRequest, urlopen
from typing import Any

from fastapi import APIRouter, Request

try:
    from .auth import get_current_user
    from . import deployment_drain
    from .provider_keys import list_keys
    from .provider_broker import SUPPORTED_PROVIDERS
except ImportError:  # uvicorn app:app from package dir
    from auth import get_current_user
    import deployment_drain
    from provider_keys import list_keys
    from provider_broker import SUPPORTED_PROVIDERS

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
        {"id": "gemini-3.1-pro-preview", "label": "Gemini 3.1 Pro Preview"},
        {"id": "gemini-3.5-flash", "label": "Gemini 3.5 Flash"},
    ],
    "anthropic": [
        {"id": "claude-opus-5-5", "label": "Claude Opus 5.5"},
        {"id": "claude-sonnet-5-5", "label": "Claude Sonnet 5.5"},
        {"id": "claude-opus-5", "label": "Claude Opus 5"},
        {"id": "claude-sonnet-5", "label": "Claude Sonnet 5"},
        {"id": "claude-fable-5", "label": "Claude Fable 5"},
        {"id": "claude-haiku-4-5", "label": "Claude Haiku 4.5"},
    ],
    "openai": [
        {"id": "gpt-6-sol", "label": "GPT-6 Sol"},
        {"id": "gpt-6-luna", "label": "GPT-6 Luna"},
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


# Secret-bearing keys that must never appear in a response payload.
_SECRET_KEYS = frozenset({
    "api_key", "access_token", "refresh_token", "id_token", "token",
    "fallback_api_keys", "client_secret", "password", "secret",
    "authorization", "bearer",
})



_CATALOG_TTL = 2 * 60 * 60
_catalog_lock = threading.Lock()
_catalog_next_refresh = 0.0
_catalog_rows: dict[str, list[dict[str, str]]] = {}


def _version_key(model: str) -> tuple[int, int, int, int, str]:
    if model.startswith("claude-"):
        family = 0 if model.startswith("claude-opus-") else 1
    elif model.startswith("gemini-"):
        family = 0 if "-pro" in model else 1
    elif model.startswith("gpt-"):
        family = 0
    else:
        family = 2
    version = re.search(r"(\d+)(?:[.-](\d+))?", model)
    subtype = (0 if model.endswith("-sol") else
               1 if model.endswith("-luna") else 2 if model.endswith("-astra") else 3)
    return (family, -int(version[1]) if version else 0,
            -int(version[2] or 0) if version else 0, subtype, model)


_PUBLIC_CHAT_FAMILIES = {
    "anthropic": re.compile(r"claude-(?:opus|sonnet|haiku|fable|mythos)-\d+(?:-\d+)*(?:-preview)?$"),
    "google": re.compile(r"gemini-\d+(?:\.\d+)?-(?:pro|flash)(?:-[a-z0-9.-]+)?$"),
    "openai": re.compile(r"(?:gpt-\d+(?:\.\d+)?(?:-[a-z0-9.-]+)?|o[1-9]\d*(?:-[a-z0-9.-]+)?)$"),
    "xai": re.compile(r"grok-(?:\d|build-)[a-z0-9.-]*$"),
}
_NON_CHAT_SLUG = re.compile(
    r"(?:^|[-_/])(?:image|images|live|audio|tts|embedding|embeddings|"
    r"speech|transcrib[a-z]*|whisper|moderation|dall-e|sora|video|realtime)"
    r"(?:$|[-_.:/])"
)


def _chat_model_id(model_id: str) -> bool:
    return bool(model_id) and len(model_id) <= 96 and not _NON_CHAT_SLUG.search(model_id.lower())


def _public_chat_model(provider: str, model_id: str, detail: dict[str, Any]) -> bool:
    """Public discovery only supplements known Chudcode chat families."""
    family = _PUBLIC_CHAT_FAMILIES.get(provider)
    if family is None or family.fullmatch(model_id) is None or not _chat_model_id(model_id):
        return False
    if "tool_call" in detail and detail["tool_call"] is not True:
        return False
    modalities = detail.get("modalities")
    if "modalities" in detail and not isinstance(modalities, dict):
        return False
    if modalities is not None and "output" in modalities:
        output = modalities["output"]
        if not isinstance(output, list) or "text" not in output:
            return False
    return True


def _live_catalog_rows(cli: dict[str, Any], public: dict[str, Any]) -> dict[str, list[dict[str, str]]]:
    """Use Chudcode discovery; supplement providers not signed in on the shared
    host with the public models.dev catalog Chudcode itself refreshes from."""
    by_provider: dict[str, dict[str, str]] = {p: {} for p in _KEY_PROVIDERS}
    for provider, block in public.items():
        key = "gemini" if provider == "google" else provider
        if key not in by_provider or not isinstance(block, dict):
            continue
        models = block.get("models")
        if not isinstance(models, dict):
            continue
        for mid, detail in models.items():
            if (isinstance(mid, str) and isinstance(detail, dict)
                    and not detail.get("deprecated")
                    and _public_chat_model(provider, mid, detail)):
                by_provider[key][mid] = str(detail.get("name") or mid)
    for row in cli.get("models", []):
        if not isinstance(row, dict):
            continue
        provider = "gemini" if row.get("provider") == "google" else row.get("provider")
        mid = row.get("id")
        if provider in by_provider and isinstance(mid, str) and _chat_model_id(mid):
            by_provider[provider][mid] = str(row.get("name") or mid)
    result: dict[str, list[dict[str, str]]] = {}
    for provider, models in by_provider.items():
        visible = (mid for mid in models if _chat_model_id(mid))
        rows = [{"id": mid, "label": models[mid][:128]}
                for mid in sorted(visible, key=_version_key)[:80]]
        if rows:
            result[provider] = rows
    return result


def _refresh_live_catalog() -> None:
    global _catalog_rows
    try:
        binary = os.environ.get("CHUDWARE_CHUDCODE_BIN", "chud")
        proc = subprocess.run([binary, "models", "--json"], capture_output=True,
                              text=True, timeout=8, check=True)
        cli = json.loads(proc.stdout)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        log.info("Chudcode model catalog unavailable: %s", type(exc).__name__)
        cli = {}
    try:
        request = UrlRequest("https://models.dev/api.json",
                             headers={"User-Agent": "chudware-web-catalog/1"})
        with urlopen(request, timeout=8) as response:
            public = json.loads(response.read(24_000_001).decode("utf-8"))
        if not isinstance(public, dict):
            public = {}
    except (OSError, ValueError) as exc:
        log.info("Public model catalog unavailable: %s", type(exc).__name__)
        public = {}
    rows = _live_catalog_rows(cli, public)
    if rows:
        _catalog_rows = rows


def _presets(provider: str) -> list[dict[str, str]]:
    """Never wait for network/process discovery in a /api/providers request."""
    global _catalog_next_refresh
    now = time.monotonic()
    if now >= _catalog_next_refresh and _catalog_lock.acquire(blocking=False):
        try:
            if now >= _catalog_next_refresh:
                try:
                    admission = deployment_drain.controller().acquire()
                except deployment_drain.HTTPException:
                    return list(_catalog_rows.get(provider) or _MODEL_PRESETS.get(provider, []))
                def accepted_refresh():
                    try:
                        with admission.bind():
                            _refresh_live_catalog()
                    finally:
                        admission.close()
                try:
                    threading.Thread(target=accepted_refresh, daemon=True,
                                     name="web-model-catalog").start()
                    _catalog_next_refresh = now + _CATALOG_TTL
                except BaseException:
                    admission.close()
                    raise
        finally:
            _catalog_lock.release()
    return list(_catalog_rows.get(provider) or _MODEL_PRESETS.get(provider, []))

def default_model_for(provider: str) -> str | None:
    """Current catalog default with a static offline fallback."""
    rows = _presets(provider)
    return rows[0]["id"] if rows else None


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
    try:
        own_keys = list_keys(normalized)
    except Exception as exc:  # noqa: BLE001 - a broken store must not expose host keys
        log.warning("account key catalogue unavailable: %s", type(exc).__name__)
        own_keys = []
    for row in own_keys:
        provider = str(row.get("provider") or "")
        if row.get("state") != "validated" or provider not in _KEY_PROVIDERS or provider not in SUPPORTED_PROVIDERS:
            continue
        entries.append({
            "id": provider, "name": provider,
            "label": _KEY_PROVIDERS[provider], "auth": "api_key",
            "models": _presets(provider), "metering": "provider_billed_byok",
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
