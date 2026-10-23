"""Per-call token usage stash for provider complete/complete_ex responses.

contracts/providers.md freezes ``complete_ex`` → ``(text, thinking)``, so usage
cannot ride the return value. Mirror ``logging_provider``'s context-var pattern:
providers record the last response's token accounting here; ``agent_turn`` and
``mission_dispatch`` drain it into the ledger / join result.

Never invent tokens — only stash fields the provider actually returned.
"""
from __future__ import annotations

import contextvars
import threading
from typing import Any

# Thread-local last-call usage (contextvar so nested async/tasks stay isolated).
_last_usage: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "provider_last_token_usage", default=None
)
# A worker RPC may bind a live usage sink. Desktop/local calls leave it unset,
# preserving the existing dependency-free stash behavior exactly.
_usage_sink: contextvars.ContextVar[Any | None] = contextvars.ContextVar(
    "provider_usage_sink", default=None
)
_usage_event_count: contextvars.ContextVar[int] = contextvars.ContextVar(
    "provider_usage_event_count", default=0
)


def bind_usage_sink(sink: Any):
    _usage_event_count.set(0)
    return _usage_sink.set(sink)


def reset_usage_sink(token: Any) -> None:
    _usage_sink.reset(token)
    _usage_event_count.set(0)


def usage_event_count() -> int:
    return _usage_event_count.get()



# Process-wide accumulator for a single agent turn (multi complete calls).
_turn_lock = threading.Lock()
_turn_usage: dict[str, Any] = {
    "prompt_tokens": 0,
    "completion_tokens": 0,
    "total_tokens": 0,
    "calls": 0,
    "providers": [],  # list[{provider, model, prompt, completion, total}]
}


def clear_last_usage() -> None:
    """Drop the last-call stash (does not reset turn accumulator)."""
    _last_usage.set(None)


def record_usage(
    *,
    provider: str = "",
    model: str = "",
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    total_tokens: int | None = None,
    context_tokens: int | None = None,
    raw: dict | None = None,
    source: str = "api",
) -> dict[str, Any]:
    """Normalize + stash one completion's usage; accumulate into the turn total.

    Missing fields stay 0 (honest lower bound, never fabricated).

    ``prompt_tokens`` and ``context_tokens`` are DIFFERENT questions and must not
    be conflated:

    * ``prompt_tokens`` — the BILLED, non-cached input. Prompt-cache-aware
      providers deliberately exclude cache reads here because they are priced
      separately, so this is the number cost math wants.
    * ``context_tokens`` — how full the model's window actually is, i.e. every
      input token the model saw: non-cached + cache reads + cache writes. This is
      the number a context METER wants.

    Conflating them is not a rounding error. Live 2026-07-29 (case bench, k3): the
    four completions of one turn reported prompt_tokens 8121, 719, 197, 1229 —
    the drop is the prompt cache engaging, not the context shrinking — so the
    desktop meter read "1k/1.0M" on a conversation holding well over 10k tokens.
    ``context_tokens`` defaults to ``prompt_tokens`` so providers that do not
    report cache counts stay exactly as accurate as they were.
    """
    try:
        pt = int(prompt_tokens) if prompt_tokens is not None else 0
    except (TypeError, ValueError):
        pt = 0
    try:
        ct = int(completion_tokens) if completion_tokens is not None else 0
    except (TypeError, ValueError):
        ct = 0
    try:
        tt = int(total_tokens) if total_tokens is not None else (pt + ct)
    except (TypeError, ValueError):
        tt = pt + ct
    if tt <= 0 and (pt > 0 or ct > 0):
        tt = pt + ct
    try:
        cx = int(context_tokens) if context_tokens is not None else 0
    except (TypeError, ValueError):
        cx = 0
    # Occupancy can never be LESS than the billed input it contains; a provider
    # that reports no cache counts simply has cx == pt.
    cx = max(cx, pt)

    row: dict[str, Any] = {
        "provider": provider or "",
        "model": model or "",
        "prompt_tokens": max(0, pt),
        "completion_tokens": max(0, ct),
        "total_tokens": max(0, tt),
        "context_tokens": max(0, cx),
        "source": source or "api",
    }
    if raw and isinstance(raw, dict):
        # Keep a tiny raw slice for debug; never large bodies.
        row["raw_keys"] = sorted(str(k) for k in list(raw.keys())[:12])

    _last_usage.set(dict(row))
    # Count records, not sink deliveries.  ``agent_turn`` snapshots this around
    # provider.complete() to distinguish a genuinely silent provider from one
    # that recorded usage without a worker-RPC sink bound (the normal desktop
    # path).
    _usage_event_count.set(_usage_event_count.get() + 1)
    with _turn_lock:
        _turn_usage["prompt_tokens"] += row["prompt_tokens"]
        _turn_usage["completion_tokens"] += row["completion_tokens"]
        _turn_usage["total_tokens"] += row["total_tokens"]
        _turn_usage["calls"] += 1
        provs = _turn_usage["providers"]
        if isinstance(provs, list):
            provs.append({
                "provider": row["provider"],
                "model": row["model"],
                "prompt_tokens": row["prompt_tokens"],
                "completion_tokens": row["completion_tokens"],
                "total_tokens": row["total_tokens"],
                "context_tokens": row["context_tokens"],
            })
    sink = _usage_sink.get()
    if sink is not None:
        sink({
            "provider": row["provider"],
            "model": row["model"],
            "prompt_tokens": row["prompt_tokens"],
            "completion_tokens": row["completion_tokens"],
            "total_tokens": row["total_tokens"],
            "context_tokens": row["context_tokens"],
            "source": row["source"],
        })
    return row


def get_last_usage() -> dict[str, Any] | None:
    """Return a copy of the last-call usage, or None if none recorded."""
    u = _last_usage.get()
    return dict(u) if isinstance(u, dict) else None


def begin_turn_usage() -> None:
    """Reset the multi-call turn accumulator (call at start of run_turn)."""
    with _turn_lock:
        _turn_usage["prompt_tokens"] = 0
        _turn_usage["completion_tokens"] = 0
        _turn_usage["total_tokens"] = 0
        _turn_usage["calls"] = 0
        _turn_usage["providers"] = []
    clear_last_usage()


def get_turn_usage() -> dict[str, Any]:
    """Snapshot of accumulated usage for the current agent turn."""
    with _turn_lock:
        return {
            "prompt_tokens": int(_turn_usage.get("prompt_tokens") or 0),
            "completion_tokens": int(_turn_usage.get("completion_tokens") or 0),
            "total_tokens": int(_turn_usage.get("total_tokens") or 0),
            "calls": int(_turn_usage.get("calls") or 0),
            "providers": list(_turn_usage.get("providers") or []),
        }


def extract_openai_usage(resp: Any) -> dict[str, int | None]:
    """Pull prompt/completion/total from an OpenAI SDK response or dict."""
    usage = None
    if isinstance(resp, dict):
        usage = resp.get("usage")
    else:
        usage = getattr(resp, "usage", None)
        if usage is None:
            extra = getattr(resp, "model_extra", None) or {}
            if isinstance(extra, dict):
                usage = extra.get("usage")
    if usage is None:
        return {"prompt_tokens": None, "completion_tokens": None, "total_tokens": None}

    def _get(obj: Any, *keys: str) -> int | None:
        for k in keys:
            if isinstance(obj, dict):
                v = obj.get(k)
            else:
                v = getattr(obj, k, None)
            if v is None:
                continue
            try:
                return int(v)
            except (TypeError, ValueError):
                continue
        return None

    return {
        "prompt_tokens": _get(usage, "prompt_tokens", "input_tokens", "prompt"),
        "completion_tokens": _get(
            usage, "completion_tokens", "output_tokens", "completion"),
        "total_tokens": _get(usage, "total_tokens", "total"),
    }


def extract_anthropic_usage(resp: Any) -> dict[str, int | None]:
    """Pull input/output tokens from an Anthropic messages response."""
    usage = getattr(resp, "usage", None)
    if usage is None and isinstance(resp, dict):
        usage = resp.get("usage")
    if usage is None:
        return {"prompt_tokens": None, "completion_tokens": None, "total_tokens": None}

    def _get(obj: Any, *keys: str) -> int | None:
        for k in keys:
            if isinstance(obj, dict):
                v = obj.get(k)
            else:
                v = getattr(obj, k, None)
            if v is None:
                continue
            try:
                return int(v)
            except (TypeError, ValueError):
                continue
        return None

    pt = _get(usage, "input_tokens", "prompt_tokens")
    ct = _get(usage, "output_tokens", "completion_tokens")
    tt = _get(usage, "total_tokens")
    if tt is None and pt is not None and ct is not None:
        tt = pt + ct
    return {
        "prompt_tokens": pt,
        "completion_tokens": ct,
        "total_tokens": tt,
    }
