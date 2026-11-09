"""The AI lane of a public-demo cell (the only credential route a cell ever has).

A demo container's trusted front door mounts ONE per-session unix socket into every actor
sandbox; the fixed loopback forwarder in ``web/deploy/cell/cell_forwarder.py`` exposes it as
``http://127.0.0.1:18081``: OpenAI-style ``/v1/chat/completions`` for the free models (which the
front door sends to OpenRouter or to NVIDIA's API) and the native Messages API
(``/v1/messages``) for the Anthropic ones, which it sends to the Anthropic API itself; each
upstream is its own provider, so the picker names the real one. The owner's keys and the model allowlist live only in the front door: it rejects any
other model, enforces the per-session quota and USD caps and strips client credentials. Nothing here accepts a caller-supplied
endpoint, key or provider, and a cell that is not a demo container (or whose front door
mounted no socket) keeps refusing every online credential route exactly as before.
"""
from __future__ import annotations

import json
import math
import os
import re
import stat
import threading
import time
import urllib.request
from collections.abc import Mapping
from decimal import Decimal
from typing import Any

from services.agent.request_context import in_cell

LANE_PORT = 18081
LANE_ROOT = f"http://127.0.0.1:{LANE_PORT}"
LANE_BASE_URL = f"{LANE_ROOT}/v1"
LANE_SOCKET = "/run/chudware-demo/provider.sock"
PROVIDER_ID = "openrouter"
PROVIDER_NAME = "Demo models"
NVIDIA_PROVIDER_ID = "nvidia"
NVIDIA_PROVIDER_NAME = "NVIDIA API (demo)"
ANTHROPIC_PROVIDER_ID = "anthropic"
ANTHROPIC_PROVIDER_NAME = "Anthropic API (demo)"
PROVIDER_IDS = (PROVIDER_ID, NVIDIA_PROVIDER_ID, ANTHROPIC_PROVIDER_ID)
_PROVIDER_NAMES = {PROVIDER_ID: PROVIDER_NAME, NVIDIA_PROVIDER_ID: NVIDIA_PROVIDER_NAME,
                   ANTHROPIC_PROVIDER_ID: ANTHROPIC_PROVIDER_NAME}
# Not a secret. OMP wants some key for a custom provider (an unmatched value is used literally);
# the front door ignores client credentials and adds the real one on its side of the socket.
PLACEHOLDER_KEY = "chudware-demo-lane"
REFUSAL = "credential_isolation_unsupported"
MODEL_REFUSAL = "demo_model_not_allowed"

_MAX_MODELS = 32
_MAX_BYTES = 1024 * 1024
_CACHE_SECONDS = 15.0
_MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}\Z")


class LaneError(RuntimeError):
    """The lane did not answer a usable model list."""


def enabled(environ: Mapping[str, str] | None = None, socket_path: str = LANE_SOCKET) -> bool:
    """True only inside a cell of a demo container whose front door mounted the lane socket."""
    environ = os.environ if environ is None else environ
    if not in_cell() or environ.get("CHUDWARE_WEB_DEMO") != "1":
        return False
    try:
        return stat.S_ISSOCK(os.lstat(socket_path).st_mode)
    except OSError:
        return False


# Loopback only and never through the egress proxy: the lane is the cell's own forwarder.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
_cache_lock = threading.Lock()
_cache: tuple[float, list[dict[str, Any]]] | None = None


def clear_cache() -> None:
    global _cache
    with _cache_lock:
        _cache = None


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value) if value > 0 and value == value and value < 1e9 else None


_ANTHROPIC_API = "anthropic-messages"
_COST_FIELDS = (("input", "prompt"), ("output", "completion"),
                ("cacheRead", "input_cache_read"), ("cacheWrite", "input_cache_write"))


def _per_million(value: Any) -> float:
    """USD per million tokens from an OpenRouter-style per-token price; 0 for anything unusable."""
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return 0.0
    try:
        price = float(Decimal(str(value).strip()) * 1_000_000)
    except (ArithmeticError, ValueError):  # garbage text, or a magnitude Decimal cannot hold
        return 0.0
    return price if math.isfinite(price) and price >= 0 else 0.0


def _row(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, Mapping):
        return None
    model_id = item.get("id")
    if not isinstance(model_id, str) or not _MODEL_ID.fullmatch(model_id):
        return None
    name = item.get("name")
    pricing = item.get("pricing")
    pricing = pricing if isinstance(pricing, Mapping) else {}
    anthropic = item.get("owned_by") == "anthropic"
    if anthropic:
        provider = ANTHROPIC_PROVIDER_ID
    elif item.get("served_by") == NVIDIA_PROVIDER_ID:
        provider = NVIDIA_PROVIDER_ID
    else:
        provider = PROVIDER_ID
    row: dict[str, Any] = {
        "id": model_id,
        "name": name.strip()[:256] if isinstance(name, str) and name.strip() else model_id,
        "provider": provider,
        "cost": {key: _per_million(pricing.get(source)) for key, source in _COST_FIELDS},
    }
    if anthropic:
        row["api"] = _ANTHROPIC_API
    context = _positive_int(item.get("context_length"))
    if context:
        row["contextWindow"] = context
    top = item.get("top_provider")
    output = _positive_int(top.get("max_completion_tokens")) if isinstance(top, Mapping) else None
    if output:
        row["maxTokens"] = output
    architecture = item.get("architecture")
    modalities = architecture.get("input_modalities") if isinstance(architecture, Mapping) else None
    if isinstance(modalities, list):
        row["input"] = [m for m in modalities[:8] if m in ("text", "image")]
    return row


def parse_models(payload: Any) -> list[dict[str, Any]]:
    """Model rows from an OpenAI-style ``{"data": [...]}`` list, bounded and de-duplicated."""
    data = payload.get("data") if isinstance(payload, Mapping) else None
    if not isinstance(data, list):
        raise LaneError("demo model list is malformed")
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in data:
        row = _row(item)
        if row is not None and row["id"] not in seen:
            seen.add(row["id"])
            rows.append(row)
        if len(rows) >= _MAX_MODELS:
            break
    return rows


def models(*, refresh: bool = False, timeout: float = 5.0) -> list[dict[str, Any]]:
    """The front door's allowlist, read from the lane's ``GET /v1/models`` (short cache)."""
    global _cache
    now = time.monotonic()
    with _cache_lock:
        if not refresh and _cache is not None and _cache[0] > now:
            return [dict(row) for row in _cache[1]]
    request = urllib.request.Request(LANE_BASE_URL + "/models", headers={"Accept": "application/json"})
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            if response.status != 200:
                raise LaneError(f"demo model lane answered HTTP {response.status}")
            raw = response.read(_MAX_BYTES + 1)
        if len(raw) > _MAX_BYTES:
            raise LaneError("demo model list is too large")
        rows = parse_models(json.loads(raw))
    except LaneError:
        raise
    except (OSError, ValueError) as exc:  # URLError, timeouts, bad JSON
        raise LaneError(f"demo model lane unavailable: {type(exc).__name__}") from None
    with _cache_lock:
        _cache = (time.monotonic() + _CACHE_SECONDS, rows)
    return [dict(row) for row in rows]


# OMP's own bookkeeping only (the provider enforces the real limits): an entry it knows nothing
# about must still carry both numbers, or the RPC client cannot decode `set_model`'s reply.
_DEFAULT_CONTEXT = 128_000
_DEFAULT_OUTPUT = 16_384


def models_config(rows: list[Mapping[str, Any]] | None) -> dict[str, Any]:
    """OMP ``models.yml`` that points its OpenRouter, NVIDIA and Anthropic providers at the lane.

    Free rows use the OpenRouter or NVIDIA provider's ``openai-completions`` transport and the
    lane's ``/v1`` base URL.
    Anthropic rows use the Anthropic provider's ``anthropic-messages`` transport and the lane ROOT
    itself: OMP's Anthropic transport appends ``/v1/messages`` (and strips a trailing ``/v1``), so
    the front door's native route is reached either way. ``auth: apiKey`` keeps OMP from shaping
    those requests as OAuth ones.

    Rows are optional: without them both overrides still reach the lane and rely on OMP's
    bundled catalogs; the front door rejects anything outside its allowlist anyway.
    """
    providers: dict[str, dict[str, Any]] = {
        PROVIDER_ID: {"baseUrl": LANE_BASE_URL, "apiKey": PLACEHOLDER_KEY,
                      "api": "openai-completions", "auth": "apiKey"},
        NVIDIA_PROVIDER_ID: {"baseUrl": LANE_BASE_URL, "apiKey": PLACEHOLDER_KEY,
                             "api": "openai-completions", "auth": "apiKey"},
        ANTHROPIC_PROVIDER_ID: {"baseUrl": LANE_ROOT, "apiKey": PLACEHOLDER_KEY,
                                "api": _ANTHROPIC_API, "auth": "apiKey"},
    }
    if rows:
        for row in rows:
            context = row.get("contextWindow") or _DEFAULT_CONTEXT
            output = min(row.get("maxTokens") or _DEFAULT_OUTPUT, context)
            cost = row.get("cost") or {}
            entry: dict[str, Any] = {
                "id": row["id"], "name": row["name"], "contextWindow": context, "maxTokens": output,
                "cost": {key: cost.get(key, 0) for key, _ in _COST_FIELDS},
            }
            anthropic = row.get("api") == _ANTHROPIC_API
            if anthropic:
                # These models think; let OMP drive their effort instead of treating them as non-reasoning.
                entry["reasoning"] = True
            if "image" in (row.get("input") or ()):
                entry["input"] = ["text", "image"]
            if anthropic:
                provider = ANTHROPIC_PROVIDER_ID
            else:
                provider = NVIDIA_PROVIDER_ID if row.get("provider") == NVIDIA_PROVIDER_ID else PROVIDER_ID
            providers[provider].setdefault("models", []).append(entry)
    return {"providers": providers}


def pin_model(params: Mapping[str, Any]) -> dict[str, Any]:
    """Turn params with provider/model forced into the allowlist.

    A turn naming nothing gets the first allowed model, so a stale desktop-style default
    (another provider's model) can never reach OMP. Naming anything else is refused.
    """
    allowed = {row["id"]: row for row in models()}
    if not allowed:
        raise LaneError("no demo models are available")
    provider = params.get("provider")
    model = params.get("model")
    provider = provider.strip() if isinstance(provider, str) else ""
    model = model.strip() if isinstance(model, str) else ""
    if not provider and not model:
        model = next(iter(allowed))
        provider = allowed[model]["provider"]
    if model not in allowed or provider != allowed[model]["provider"]:
        raise ValueError(MODEL_REFUSAL)
    return {**params, "provider": provider, "model": model}


def provider_descriptors(rows: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Settings → Providers in a demo cell: the OpenRouter row, plus the NVIDIA and Anthropic rows
    when the lane serves models through them. Ready, nothing to configure."""
    counts = {provider_id: sum(1 for row in rows if row.get("provider") == provider_id)
              for provider_id in PROVIDER_IDS}
    return [{"id": provider_id, "name": _PROVIDER_NAMES[provider_id], "available": count > 0,
             "authenticated": True, "authKinds": [], "modelsAvailable": count,
             "state": "configured", "origin": "demo"}
            for provider_id, count in counts.items() if count or provider_id == PROVIDER_ID]


def auth_state(provider_id: str) -> dict[str, Any]:
    """``omp_auth_state`` for a demo cell, answered without starting an OMP process."""
    ready = provider_id in PROVIDER_IDS
    return {"ok": True, "id": provider_id, "name": _PROVIDER_NAMES.get(provider_id, provider_id),
            "state": "configured" if ready else "signed_out", "authenticated": ready,
            "hasApiKey": False, "hasOAuth": False, "accounts": [], "disabled": [],
            "usage": {"notes": [], "windows": []}, **({"origin": "demo"} if ready else {})}


# The front door answers HTTP 429 {"error": {"code": ..., "message": ...}} on the chat route and
# {"type": "error", "error": {"type": <code>, "message": ...}} on the Messages route once a
# visitor's session quota or the shared daily AI budget is spent. Both carry the code and the
# message under ``error``, so one parser serves them. Neither is transient, so OMP's
# automatic 429 retry (up to ten attempts) must not run: it only delays the explanation.
QUOTA_CODES = ("demo_ai_daily_limit", "demo_quota_exhausted")
_QUOTA_FALLBACK = {
    "demo_ai_daily_limit": "The demo's AI limit for today has been reached. Please try again tomorrow.",
    "demo_quota_exhausted": "This demo session has used up its AI allowance.",
}
_QUOTA_CODE = re.compile(r"\b(" + "|".join(QUOTA_CODES) + r")\b")


# OMP words the same refusal two ways: "<status> <json body>" while it is still retrying and
# "<status> <message> (type=<code>)" once it gives up, possibly inside a longer sentence.
_QUOTA_TEXT = re.compile(r"\b\d{3}\s+([^\n(]+?)\s*\((?:type|code)=(?:" + "|".join(QUOTA_CODES) + r")\)")


def quota_message(raw: Any) -> str | None:
    """The lane's own readable message when ``raw`` is OMP's text for a demo quota refusal."""
    if not isinstance(raw, str):
        return None
    match = _QUOTA_CODE.search(raw)
    if match is None:
        return None
    start = raw.find("{")
    if start >= 0:
        try:
            body, _ = json.JSONDecoder().raw_decode(raw[start:])
        except ValueError:
            body = None
        error = body.get("error") if isinstance(body, Mapping) else None
        text = error.get("message") if isinstance(error, Mapping) else None
        if isinstance(text, str) and text.strip():
            return text.strip()[:600]
    worded = _QUOTA_TEXT.search(raw)
    if worded is not None and worded.group(1).strip():
        return worded.group(1).strip()[:600]
    return _QUOTA_FALLBACK[match.group(1)]


def frame_quota_message(frame: Mapping[str, Any]) -> str | None:
    """``quota_message`` for an OMP ``auto_retry_start`` frame (either key spelling)."""
    if frame.get("type") != "auto_retry_start":
        return None
    for key in ("errorMessage", "error_message", "error"):
        message = quota_message(frame.get(key))
        if message is not None:
            return message
    return None
