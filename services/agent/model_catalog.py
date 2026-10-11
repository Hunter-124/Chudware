"""On-connect model catalog — live discovery + models.dev baseline.

Provider setup should be as automatic as possible. This module discovers models and
capability metadata (context, max_output, vision, tools, reasoning_efforts) and merges
them with a cached models.dev snapshot. User config overrides always win.

Precedence (auto_source):
    user override > live endpoint field > models.dev > id heuristic > provider default

The models.dev snapshot is fetched at build/provision time (or first explicit refresh)
and cached under the runtime dir. It is NEVER fetched at import time.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from services import paths

__all__ = [
    "ModelRow",
    "MODELS_DEV_URL",
    "models_dev_cache_path",
    "ensure_models_dev_cache",
    "load_models_dev",
    "discover_models",
    "merge_catalog",
    "lookup_model",
    "resolve_capabilities",
    "reasoning_efforts_for",
    "heuristic_row",
]

MODELS_DEV_URL = "https://models.dev/api.json"
_CACHE_NAME = "models_dev_cache.json"
_DEFAULT_UA = "chudware-model-catalog/1.0"

# Provider name as used by config.py / agent_list_models -> models.dev provider key.
_MODELS_DEV_PROVIDER = {
    "openai": "openai",
    "anthropic": "anthropic",
    "gemini": "google",
    "google": "google",
    "xai": "xai",
    "openrouter": "openrouter",
    "llamacpp": None,  # local — never trust models.dev for ctx
    "local": None,
    "vllm": None,
}


class ModelRow(dict):
    """Per-model capability row. Dict subclass so JSON dumps stay trivial.

    Keys: id, label, context, max_output, reasoning_efforts, vision, tools, auto_source.
    """

    __slots__ = ()

    @classmethod
    def make(
        cls,
        *,
        id: str,
        label: str | None = None,
        context: int | None = None,
        max_output: int | None = None,
        reasoning_efforts: list[str] | None = None,
        vision: bool | None = None,
        tools: bool | None = None,
        auto_source: str = "heuristic",
    ) -> "ModelRow":
        return cls(
            {
                "id": id,
                "label": label or id,
                "context": int(context) if context else 0,
                "max_output": int(max_output) if max_output else 0,
                "reasoning_efforts": list(reasoning_efforts or []),
                "vision": bool(vision) if vision is not None else False,
                "tools": bool(tools) if tools is not None else True,
                "auto_source": auto_source,
            }
        )


# --------------------------------------------------------------------------- cache


def models_dev_cache_path() -> Path:
    """Runtime-dir path for the models.dev snapshot (never imported into package data)."""
    return paths.RUNTIME / _CACHE_NAME


def ensure_models_dev_cache(
    *,
    force: bool = False,
    url: str = MODELS_DEV_URL,
    opener: Callable[..., Any] | None = None,
    max_age_s: float = 7 * 24 * 3600,
) -> Path:
    """Fetch models.dev into the runtime cache if missing/stale. NEVER called at import.

    Parameters
    ----------
    force:
        Re-fetch even when a fresh cache exists.
    opener:
        Optional ``urlopen``-compatible callable (tests inject mocks).
    max_age_s:
        Refresh when the cache is older than this (default 7 days).
    """
    path = models_dev_cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if not force and path.exists():
        try:
            age = time.time() - path.stat().st_mtime
            if age < max_age_s and path.stat().st_size > 0:
                return path
        except OSError:
            pass
    open_fn = opener or urllib.request.urlopen
    req = urllib.request.Request(url, headers={"User-Agent": _DEFAULT_UA})
    with open_fn(req, timeout=30) as resp:  # type: ignore[arg-type]
        raw = resp.read()
    # Validate JSON before writing so a bad response never clobbers a good cache.
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("models.dev payload is not an object")
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    tmp.replace(path)
    return path


# In-process cache: the multi-MB models.dev snapshot is otherwise re-parsed on every
# provider resolution (config._catalog_caps -> resolve_capabilities -> lookup_model ->
# merge_catalog, at least once per run_turn). Keyed on the resolved path (tests patch
# models_dev_cache_path() to point elsewhere per-call) + mtime_ns; either changing
# invalidates and reparses. Mirrors advisor/triggers.py's _ensure_cache_fresh pattern.
# Safe to return by reference (never None once set) — every caller only reads it.
_models_dev_cache: dict[str, Any] | None = None
_models_dev_cache_path: str | None = None
_models_dev_cache_mtime_ns: int | None = None


def load_models_dev(
    *,
    cache_path: Path | None = None,
    allow_fetch: bool = False,
    opener: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Load the models.dev snapshot. Does not network unless *allow_fetch* is True."""
    global _models_dev_cache, _models_dev_cache_path, _models_dev_cache_mtime_ns
    path = cache_path or models_dev_cache_path()
    if not path.exists():
        if not allow_fetch:
            return {}
        path = ensure_models_dev_cache(opener=opener)
    path_key = str(path)
    try:
        mtime_ns = path.stat().st_mtime_ns
    except OSError:
        mtime_ns = None
    if (
        _models_dev_cache is not None
        and _models_dev_cache_path == path_key
        and _models_dev_cache_mtime_ns == mtime_ns
    ):
        return _models_dev_cache
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}
    data = data if isinstance(data, dict) else {}
    _models_dev_cache, _models_dev_cache_path, _models_dev_cache_mtime_ns = (
        data, path_key, mtime_ns,
    )
    return data


# --------------------------------------------------------------------------- live discovery


def _http_get_json(
    url: str,
    headers: dict[str, str] | None = None,
    *,
    opener: Callable[..., Any] | None = None,
    timeout: float = 15.0,
) -> Any:
    open_fn = opener or urllib.request.urlopen
    hdrs = {"User-Agent": _DEFAULT_UA}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, headers=hdrs)
    with open_fn(req, timeout=timeout) as resp:  # type: ignore[arg-type]
        return json.loads(resp.read().decode("utf-8"))


def _normalize_openai_base(base_url: str | None) -> str:
    if not base_url:
        return "https://api.openai.com/v1"
    u = base_url.rstrip("/")
    parsed = urlparse(u)
    if parsed.scheme and parsed.netloc and parsed.path in ("", "/"):
        u += "/v1"
    return u


def discover_openai_compatible(
    *,
    base_url: str | None = None,
    api_key: str | None = None,
    provider: str = "openai",
    opener: Callable[..., Any] | None = None,
) -> list[ModelRow]:
    """GET {base}/models — OpenAI / xAI / OpenRouter / local OpenAI-compat."""
    root = _normalize_openai_base(base_url)
    headers: dict[str, str] = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        payload = _http_get_json(f"{root}/models", headers, opener=opener)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError,
            OSError, ValueError):
        return []
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        return []
    rows: list[ModelRow] = []
    for item in data:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        mid = str(item["id"])
        context = 0
        max_output = 0
        vision: bool | None = None
        tools: bool | None = None
        efforts: list[str] = []
        # OpenRouter-style rich rows.
        if isinstance(item.get("context_length"), (int, float)):
            context = int(item["context_length"])
        top_p = item.get("top_provider") if isinstance(item.get("top_provider"), dict) else {}
        if isinstance(top_p.get("max_completion_tokens"), (int, float)):
            max_output = int(top_p["max_completion_tokens"])
        arch = item.get("architecture") if isinstance(item.get("architecture"), dict) else {}
        modality = arch.get("modality") or ""
        if isinstance(modality, str) and "image" in modality.split("->")[0]:
            vision = True
        inputs = arch.get("input_modalities")
        if isinstance(inputs, list) and any(x == "image" for x in inputs if isinstance(x, str)):
            vision = True
        # Local/OpenAI list endpoints usually only return ids.
        source = "live"
        if context or max_output or vision is not None:
            source = "live"
        else:
            source = "live"  # id-only; enrichment fills the rest
        rows.append(
            ModelRow.make(
                id=mid,
                label=str(item.get("name") or mid),
                context=context or None,
                max_output=max_output or None,
                reasoning_efforts=efforts or None,
                vision=vision,
                tools=tools if tools is not None else True,
                auto_source=source,
            )
        )
    # LM Studio loaded-state is handled by the C++ probe; Python catalog is capability-focused.
    _ = provider
    return rows


def discover_gemini(
    *,
    api_key: str | None = None,
    opener: Callable[..., Any] | None = None,
) -> list[ModelRow]:
    """Gemini v1beta/models — keep inputTokenLimit / outputTokenLimit."""
    key = api_key or os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or ""
    if not key:
        return []
    url = "https://generativelanguage.googleapis.com/v1beta/models"
    try:
        payload = _http_get_json(url, {"x-goog-api-key": key}, opener=opener)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError,
            OSError, ValueError):
        return []
    models = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(models, list):
        return []
    rows: list[ModelRow] = []
    for md in models:
        if not isinstance(md, dict):
            continue
        methods = md.get("supportedGenerationMethods") or []
        if not any(m == "generateContent" for m in methods if isinstance(m, str)):
            continue
        name = str(md.get("name") or "")
        if name.startswith("models/"):
            name = name[len("models/") :]
        if not name:
            continue
        ctx = md.get("inputTokenLimit")
        out = md.get("outputTokenLimit")
        # Vision: multimodal models expose image generation methods or "vision" in name/description.
        vision = True  # current Gemini chat models accept images; TTS-only filtered by method
        display = str(md.get("displayName") or name)
        rows.append(
            ModelRow.make(
                id=name,
                label=display,
                context=int(ctx) if isinstance(ctx, (int, float)) else None,
                max_output=int(out) if isinstance(out, (int, float)) else None,
                reasoning_efforts=reasoning_efforts_for("gemini", name, reasoning=True),
                vision=vision,
                tools=True,
                auto_source="live",
            )
        )
    return rows


def discover_anthropic_static() -> list[ModelRow]:
    """Minimal offline seed; the refreshed Chudcode catalog owns live availability."""
    return [
        ModelRow.make(
            id=model,
            label=label,
            context=1000000,
            max_output=128000,
            reasoning_efforts=reasoning_efforts_for("anthropic", model, reasoning=True),
            vision=True,
            tools=True,
            auto_source="heuristic",
        )
        for model, label in (
            ("claude-opus-5-5", "Claude Opus 5.5"),
            ("claude-sonnet-5-5", "Claude Sonnet 5.5"),
            ("claude-haiku-5-5", "Claude Haiku 5.5"),
        )
    ]


def discover_models(
    provider: str,
    *,
    base_url: str | None = None,
    api_key: str | None = None,
    opener: Callable[..., Any] | None = None,
) -> list[ModelRow]:
    """On-connect discovery for *provider*. Network only when called explicitly."""
    name = (provider or "").lower().strip()
    if name in ("openai", "openrouter", "xai", "llamacpp", "local", "vllm"):
        default_base = {
            "openai": "https://api.openai.com/v1",
            "openrouter": "https://openrouter.ai/api/v1",
            "xai": "https://api.x.ai/v1",
            "llamacpp": "http://localhost:1234/v1",
            "local": "http://localhost:1234/v1",
            "vllm": "http://localhost:8000/v1",
        }.get(name)
        return discover_openai_compatible(
            base_url=base_url or default_base,
            api_key=api_key,
            provider=name,
            opener=opener,
        )
    if name in ("gemini", "google"):
        return discover_gemini(api_key=api_key, opener=opener)
    if name == "anthropic":
        return discover_anthropic_static()
    return []


# --------------------------------------------------------------------------- models.dev map + merge


def _models_dev_provider_key(provider: str) -> str | None:
    return _MODELS_DEV_PROVIDER.get((provider or "").lower().strip())


def _row_from_models_dev(mid: str, entry: dict[str, Any], *, provider: str) -> ModelRow:
    limit = entry.get("limit") if isinstance(entry.get("limit"), dict) else {}
    context = int(limit["context"]) if isinstance(limit.get("context"), (int, float)) else 0
    max_output = int(limit["output"]) if isinstance(limit.get("output"), (int, float)) else 0
    modalities = entry.get("modalities") if isinstance(entry.get("modalities"), dict) else {}
    inputs = modalities.get("input") if isinstance(modalities.get("input"), list) else []
    vision = any(x == "image" for x in inputs if isinstance(x, str)) or bool(entry.get("attachment"))
    tools = bool(entry.get("tool_call", True))
    reasoning = bool(entry.get("reasoning"))
    efforts: list[str] = []
    for opt in entry.get("reasoning_options") or []:
        if isinstance(opt, dict) and opt.get("type") == "effort" and isinstance(opt.get("values"), list):
            efforts = [str(v) for v in opt["values"]]
            break
    if reasoning and not efforts:
        efforts = reasoning_efforts_for(provider, mid, reasoning=True)
    elif not reasoning:
        efforts = []
    return ModelRow.make(
        id=mid,
        label=str(entry.get("name") or mid),
        context=context or None,
        max_output=max_output or None,
        reasoning_efforts=efforts or None,
        vision=vision,
        tools=tools,
        auto_source="models.dev",
    )


def models_dev_rows_for_provider(
    provider: str,
    models_dev: dict[str, Any] | None = None,
) -> dict[str, ModelRow]:
    """Index models.dev rows for *provider* by model id."""
    data = models_dev if models_dev is not None else load_models_dev()
    key = _models_dev_provider_key(provider)
    if not key:
        return {}
    block = data.get(key) if isinstance(data, dict) else None
    models = block.get("models") if isinstance(block, dict) else None
    if not isinstance(models, dict):
        return {}
    out: dict[str, ModelRow] = {}
    for mid, entry in models.items():
        if not isinstance(entry, dict):
            continue
        rid = str(entry.get("id") or mid)
        out[rid] = _row_from_models_dev(rid, entry, provider=provider)
    return out


def _prefer_int(live: int, base: int) -> int:
    return live if live and live > 0 else (base if base and base > 0 else 0)


def _merge_row(base: ModelRow | None, live: ModelRow | None) -> ModelRow:
    """Merge live over models.dev/heuristic. Live fields win when present/non-zero."""
    if base is None and live is None:
        raise ValueError("empty merge")
    if base is None:
        assert live is not None
        return ModelRow(dict(live))
    if live is None:
        return ModelRow(dict(base))
    sources = []
    for s in (live.get("auto_source"), base.get("auto_source")):
        if s and s not in sources:
            sources.append(str(s))
    vision = live["vision"] if live.get("auto_source") == "live" and (
        live.get("vision") or live.get("context") or live.get("max_output")
    ) else (live["vision"] if "vision" in live else base.get("vision", False))
    # Live id-only rows still inherit vision/tools from base.
    if live.get("auto_source") == "live" and not live.get("context") and not live.get("max_output"):
        vision = base.get("vision", live.get("vision", False))
        tools = base.get("tools", live.get("tools", True))
        efforts = base.get("reasoning_efforts") or live.get("reasoning_efforts") or []
        context = base.get("context") or 0
        max_output = base.get("max_output") or 0
        auto = "live+models.dev" if base.get("auto_source") == "models.dev" else (
            "live+" + str(base.get("auto_source") or "heuristic")
        )
    else:
        tools = live.get("tools", base.get("tools", True))
        efforts = live.get("reasoning_efforts") or base.get("reasoning_efforts") or []
        context = _prefer_int(int(live.get("context") or 0), int(base.get("context") or 0))
        max_output = _prefer_int(int(live.get("max_output") or 0), int(base.get("max_output") or 0))
        auto = "+".join(sources) if sources else "live"
        vision = live.get("vision") if live.get("vision") is not None else base.get("vision", False)
        if live.get("context") or live.get("max_output"):
            # live had real limits
            if base.get("auto_source") == "models.dev" and not (
                live.get("context") and live.get("max_output")
            ):
                auto = "live+models.dev"
    return ModelRow.make(
        id=str(live.get("id") or base.get("id")),
        label=str(live.get("label") or base.get("label") or live.get("id") or base.get("id")),
        context=context or None,
        max_output=max_output or None,
        reasoning_efforts=list(efforts) or None,
        vision=bool(vision),
        tools=bool(tools),
        auto_source=auto,
    )


def merge_catalog(
    provider: str,
    live_rows: list[ModelRow] | None = None,
    *,
    models_dev: dict[str, Any] | None = None,
    include_models_dev_only: bool = True,
) -> list[ModelRow]:
    """Merge live discovery with models.dev baseline for *provider*.

    When *include_models_dev_only* is True (cloud providers), models present only in
    models.dev are kept so the picker still has a catalog offline after a keyed connect
    that somehow returned an empty list. Local providers never pull models.dev ids.
    """
    live = {str(r["id"]): ModelRow(dict(r)) for r in (live_rows or []) if r.get("id")}
    base = models_dev_rows_for_provider(provider, models_dev)
    is_local = _models_dev_provider_key(provider) is None

    out: dict[str, ModelRow] = {}
    # Live ids first (authoritative presence for open endpoints).
    for mid, row in live.items():
        out[mid] = _merge_row(base.get(mid), row)
    if include_models_dev_only and not is_local:
        for mid, row in base.items():
            if mid not in out:
                out[mid] = ModelRow(dict(row))
    # Anthropic: always ensure static seed if nothing else.
    if (provider or "").lower() == "anthropic" and not out:
        for row in discover_anthropic_static():
            out[str(row["id"])] = row
    # Fill remaining empty capability holes with heuristics.
    filled: list[ModelRow] = []
    for mid, row in out.items():
        if not row.get("context") or not row.get("reasoning_efforts") and row.get("reasoning_efforts") != []:
            h = heuristic_row(provider, mid)
            if not row.get("context") and h.get("context"):
                row["context"] = h["context"]
                if "heuristic" not in str(row.get("auto_source")):
                    row["auto_source"] = str(row.get("auto_source") or "") + "+heuristic"
            if not row.get("max_output") and h.get("max_output"):
                row["max_output"] = h["max_output"]
            if not row.get("reasoning_efforts") and h.get("reasoning_efforts"):
                # Only fill efforts when heuristic says the model reasons; empty list is intentional.
                if h.get("reasoning_efforts"):
                    row["reasoning_efforts"] = list(h["reasoning_efforts"])
            if not row.get("label") or row.get("label") == mid:
                row["label"] = h.get("label") or mid
        filled.append(ModelRow(dict(row)))
    filled.sort(key=lambda r: str(r.get("id") or ""))
    return filled


# --------------------------------------------------------------------------- heuristics / efforts


def reasoning_efforts_for(provider: str, model_id: str, *, reasoning: bool | None = None) -> list[str]:
    """Identity/family effort ladders (omp model-thinking style, baked not scraped)."""
    p = (provider or "").lower()
    m = (model_id or "").lower()
    if reasoning is False:
        return []
    # Explicit non-reasoning ids.
    if "non-reasoning" in m or m.endswith("-tts") or "tts" in m:
        return []
    if p in ("gemini", "google") or "gemini" in m:
        if "2.0" in m and "flash" in m:
            return []
        if "3.1-pro" in m:
            return ["low", "high"]
        if re.search(r"gemini-3(?:\.|-\d)", m):
            return ["minimal", "low", "medium", "high"]
        return ["minimal", "low", "medium", "high"]
    if p in ("anthropic", "anthropic-sub") or "claude" in m:
        if "-4-5" in m:
            return ["minimal", "low", "medium", "high", "xhigh"]
        if "sonnet-4-6" in m:
            return ["low", "medium", "high"]
        if "-4-6" in m:
            return ["low", "medium", "high", "max"]
        return ["low", "medium", "high", "xhigh", "max"]
    if p == "xai" or "grok" in m:
        # Only effort-capable Grok variants expose the dial; others think natively.
        if re.search(r"reasoning|grok-4\.5|grok-4-5|grok-4\.20", m):
            return ["off", "low", "medium", "high"]
        if "build" in m:
            return []
        return ["off", "low", "medium", "high"]
    # Moonshot / Kimi Code. Router catalogs (zenmux, openrouter) ship the SAME model as
    # `moonshotai/kimi-k3`, so this one arm matches on the bare id; every other family
    # keeps matching the full id exactly as before. `kimi-k2-thinking` is a distinct id
    # that keeps the generic think ladder below, so it is excluded here.
    kimi = m.rsplit("/", 1)[-1]
    # The Kimi Code subscription endpoint's own wire ids are bare `k3` / `k3-256k`
    # (plus `kimi-for-coding*`, caught by the startswith arm below).
    if kimi == "k3" or kimi.startswith("k3-"):
        return ["low", "high", "max"]
    if kimi.startswith("kimi") and "think" not in kimi:
        # K3's real wire ladder is low|high|max — there is NO medium rung, so the GUI
        # offers exactly the three the endpoint accepts rather than a fourth that would
        # 400. wire_reasoning_effort maps a medium inherited from a shared role config
        # onto "high", the nearest real rung above low.
        if "kimi-k3" in kimi:
            return ["low", "high", "max"]
        # K2.x (k2.6 / k2.7 / -code) think by default and expose no effort dial.
        return []
    if p in ("openai", "openai-codex") or m.startswith(("gpt", "o1", "o3", "o4")):
        if re.match(r"gpt-(?:6|[7-9]|5\.6)", m):
            return ["low", "medium", "high", "xhigh", "max"]
        if re.match(r"gpt-5|o[134]", m):
            return ["low", "medium", "high"]
        return []
    if reasoning is True:
        return ["off", "low", "medium", "high"]
    # Local think models.
    if any(x in m for x in ("r1", "reason", "think", "qwen3")):
        return ["off", "low", "medium", "high"]
    return []


def heuristic_row(provider: str, model_id: str) -> ModelRow:
    """Id-substring context / vision defaults when live + models.dev leave gaps."""
    m = (model_id or "").lower()
    p = (provider or "").lower()
    context = 8192
    max_output = 0
    vision = False
    label = model_id
    if (p == "gemini" or "gemini" in m) and "1.5-pro" in m:
        context = 2097152
    elif (p == "gemini" or "gemini" in m) and any(x in m for x in ("1.5-flash", "2.0", "2.5", "flash")):
        context = 1048576
    elif any(x in m for x in ("claude", "opus", "sonnet", "haiku")):
        if re.search(r"haiku-5[-.]5(?:$|[-:@])", m):
            context = 1000000
            max_output = 128000
            label = "Claude Haiku 5.5"
        else:
            context = 1000000 if re.search(r"(?:opus|sonnet)-5(?:-|$)", m) else 200000
    elif "gpt-4.1" in m:
        context = 1047576
    elif any(x in m for x in ("o1", "o3", "o4", "reasoner", "r1")):
        context = 200000
    elif any(x in m for x in ("gpt-4o", "gpt-4-turbo", "4o-mini")):
        context = 128000
    elif "gpt-3.5" in m:
        context = 16385
    elif any(x in m for x in ("qwen", "deepseek")):
        context = 128000
    elif "grok-4.5" in m or "grok-4-5" in m:
        context = 500000
    elif "grok" in m:
        context = 131072
    else:
        context = {
            "gemini": 1048576,
            "anthropic": 200000,
            "openai": 128000,
            "openrouter": 128000,
            "xai": 128000,
        }.get(p, 8192)

    if any(x in m for x in ("vision", "-vl", "gpt-4o", "gemini", "claude", "grok-4", "4o")):
        vision = True
    if p in ("gemini", "anthropic", "xai"):
        vision = True
    if p == "openai" and not any(x in m for x in ("o1", "o3", "o4")):
        vision = "4o" in m or "4.1" in m or "vision" in m or "gpt-4o" in m

    efforts = reasoning_efforts_for(p, model_id)
    return ModelRow.make(
        id=model_id,
        label=label,
        context=context,
        max_output=max_output or None,
        reasoning_efforts=efforts or None,
        vision=vision,
        tools=True,
        auto_source="heuristic",
    )


def lookup_model(
    provider: str,
    model_id: str,
    *,
    live_rows: list[ModelRow] | None = None,
    models_dev: dict[str, Any] | None = None,
) -> ModelRow:
    """Single-model lookup via merge_catalog (live ∩ models.dev ∩ heuristic)."""
    mid = model_id or ""
    catalog = merge_catalog(
        provider,
        live_rows,
        models_dev=models_dev,
        include_models_dev_only=True,
    )
    for row in catalog:
        if row.get("id") == mid:
            return ModelRow(dict(row))
    # Not in live/dev lists — pure heuristic (local custom ids etc.).
    return heuristic_row(provider, mid)


def resolve_capabilities(
    provider: str,
    model_id: str,
    cfg: dict[str, Any] | None = None,
    *,
    live_rows: list[ModelRow] | None = None,
    models_dev: dict[str, Any] | None = None,
    catalog_row: ModelRow | None = None,
) -> dict[str, Any]:
    """Resolve final capabilities with user config overrides always winning.

    Returns dict: context, max_output, vision, tools, reasoning_efforts, auto_source, label.
    """
    cfg = cfg or {}
    row = catalog_row or lookup_model(
        provider, model_id, live_rows=live_rows, models_dev=models_dev
    )
    p = (provider or "").lower()
    # Per-provider nested vision key (openai/xai/openrouter/vllm/llamacpp).
    sub_key = {
        "openai": "openai",
        "xai": "xai",
        "openrouter": "openrouter",
        "vllm": "vllm",
        "llamacpp": "llamacpp",
        "local": "openai",
    }.get(p)
    sub = dict(cfg.get(sub_key) or {}) if sub_key else {}

    # vision: user nested key wins when present; else top-level; else catalog.
    if sub_key and "vision" in sub:
        vision = bool(sub["vision"])
        source = "user"
    elif "vision" in cfg:
        vision = bool(cfg["vision"])
        source = "user"
    else:
        vision = bool(row.get("vision", False))
        source = str(row.get("auto_source") or "heuristic")

    # context_window: top-level >0 wins; 0/absent = catalog.
    cw_cfg = cfg.get("context_window")
    if isinstance(cw_cfg, (int, float)) and int(cw_cfg) > 0:
        context = int(cw_cfg)
        source = "user"
    else:
        context = int(row.get("context") or 0) or heuristic_row(provider, model_id)["context"]

    # max_output_tokens: top-level >0 wins.
    mo_cfg = cfg.get("max_output_tokens")
    if isinstance(mo_cfg, (int, float)) and int(mo_cfg) > 0:
        max_output = int(mo_cfg)
        source = "user"
    else:
        max_output = int(row.get("max_output") or 0)

    # reasoning_effort preference is user-owned; option *set* comes from catalog unless overridden.
    efforts = list(row.get("reasoning_efforts") or [])
    if isinstance(cfg.get("reasoning_efforts"), list) and cfg["reasoning_efforts"]:
        efforts = [str(x) for x in cfg["reasoning_efforts"]]
        source = "user"

    return {
        "id": model_id,
        "label": row.get("label") or model_id,
        "context": int(context),
        "max_output": int(max_output),
        "vision": bool(vision),
        "tools": bool(row.get("tools", True)),
        "reasoning_efforts": efforts,
        "auto_source": source if source == "user" else str(row.get("auto_source") or "heuristic"),
    }
