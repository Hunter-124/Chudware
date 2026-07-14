"""Transport-error honesty for diagnostic provider calls.

Upstream 429 / 5xx / timeouts / connection failures are NOT model incapacity.
Call :func:`complete_with_retry` from each axis runner; exhausted transport
errors mark the task ``error: true`` and are excluded from the score
denominator via :func:`finalize_axis_result`.
"""
from __future__ import annotations

import random
import re
import time
from typing import Any, Callable

# 4 attempts total; sleeps between them use these bases (seconds) + jitter.
MAX_TRANSPORT_ATTEMPTS = 4
TRANSPORT_BACKOFF_BASES: tuple[float, ...] = (2.0, 8.0, 30.0)

# Exception type names that are always transport-class (retryable).
_TRANSPORT_EXC_NAMES = frozenset({
    "RateLimitError",
    "APIConnectionError",
    "APITimeoutError",
    "APIStatusError",
    "InternalServerError",
    "ServiceUnavailableError",
    "GatewayTimeoutError",
    "Timeout",
    "TimeoutError",
    "ConnectTimeout",
    "ReadTimeout",
    "ConnectError",
    "ConnectionError",
    "RemoteProtocolError",
    "HTTPStatusError",
})

# Substrings (lowercased) that mark transport / rate-limit / connectivity failures.
_TRANSPORT_MSG_SIGNS = (
    "rate limit",
    "rate-limit",
    "rate_limit",
    "ratelimit",
    "temporarily rate-limited",
    "retry shortly",
    "retry after",
    "too many requests",
    "overloaded",
    "timed out",
    "timeout",
    "did not respond within",
    "could not reach the model server",
    "connection refused",
    "connection reset",
    "connection aborted",
    "connection error",
    "temporarily unavailable",
    "service unavailable",
    "bad gateway",
    "gateway timeout",
    "server error",
    "internal server error",
)


class TransportExhausted(RuntimeError):
    """All transport-retry attempts failed; not a model-capability failure."""

    def __init__(self, detail: str, *, attempts: int, last_exc: BaseException | None = None):
        super().__init__(detail)
        self.detail = detail
        self.attempts = attempts
        self.last_exc = last_exc


def _status_code(exc: BaseException) -> int | None:
    for attr in ("status_code", "status"):
        val = getattr(exc, attr, None)
        if isinstance(val, int):
            return val
    resp = getattr(exc, "response", None)
    if resp is not None:
        val = getattr(resp, "status_code", None)
        if isinstance(val, int):
            return val
    m = re.search(r"(?:error code|status(?: code)?|http)[:\s]*(\d{3})", str(exc), re.I)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            return None
    return None


def is_transport_error(exc: BaseException) -> bool:
    """True for rate-limits, 5xx, timeouts, and connection failures.

    Capability / schema / auth failures (400, 401, 403, 404 NotFound, ValueError, …)
    return False so they surface as honest model/config results.
    """
    if isinstance(exc, TransportExhausted):
        return True

    name = type(exc).__name__
    status = _status_code(exc)
    msg = str(exc).lower()

    if name in _TRANSPORT_EXC_NAMES:
        # APIStatusError / HTTPStatusError are transport only for 429 + 5xx (+ a few).
        if name in ("APIStatusError", "HTTPStatusError"):
            if status is None:
                pass  # fall through to message heuristics
            elif status == 429 or status >= 500 or status in (408, 409, 425):
                return True
            else:
                return False
        else:
            return True

    if status is not None:
        if status == 429 or status >= 500 or status in (408, 409, 425):
            return True
        if 400 <= status < 500:
            return False

    if any(s in msg for s in _TRANSPORT_MSG_SIGNS):
        return True
    if re.search(r"\b429\b", msg) or re.search(r"\b5\d\d\b", msg):
        return True
    return False


def _retry_after_seconds(exc: BaseException) -> float | None:
    """Best-effort parse of Retry-After / retry_after_seconds from the exception."""
    for attr in ("retry_after", "retry_after_seconds"):
        val = getattr(exc, attr, None)
        if isinstance(val, (int, float)) and val >= 0:
            return float(val)
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        err = body.get("error") if isinstance(body.get("error"), dict) else body
        if isinstance(err, dict):
            meta = err.get("metadata") or {}
            for key in ("retry_after_seconds", "retry_after_seconds_raw", "retry_after"):
                val = meta.get(key)
                if isinstance(val, (int, float)) and val >= 0:
                    return float(val)
    m = re.search(
        r"retry[_ -]?after(?:_seconds(?:_raw)?)?['\":\s]+(\d+(?:\.\d+)?)",
        str(exc),
        re.I,
    )
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            return None
    return None


def _backoff_seconds(
    attempt_index: int,
    exc: BaseException,
    *,
    rng: Callable[[], float],
) -> float:
    """Sleep before the next attempt.

    *attempt_index* is 0-based for the failure that just happened (0 → first sleep).
    Uses TRANSPORT_BACKOFF_BASES with full jitter in [0.5, 1.5)× base, and honors
    Retry-After when larger (capped) so "retry shortly" semantics are respected.
    """
    base = TRANSPORT_BACKOFF_BASES[min(attempt_index, len(TRANSPORT_BACKOFF_BASES) - 1)]
    jittered = base * (0.5 + rng())  # [0.5, 1.5) × base
    retry_after = _retry_after_seconds(exc)
    if retry_after is not None:
        # Cap so a hostile header can't freeze the suite; still prefer server advice.
        retry_after = min(max(retry_after, 0.0), 120.0)
        return max(jittered, retry_after)
    return jittered


def complete_with_retry(
    provider: Any,
    system: str,
    messages: list,
    *,
    max_tokens: int = 512,
    max_attempts: int = MAX_TRANSPORT_ATTEMPTS,
    sleep_fn: Callable[[float], None] = time.sleep,
    rng: Callable[[], float] | None = None,
) -> tuple[str, dict[str, Any]]:
    """Call ``provider.complete`` with transport-only retries.

    Returns ``(reply, meta)`` where *meta* includes ``attempts`` (1..max_attempts).
    Non-transport exceptions propagate unchanged.
    Exhausted transport retries raise :class:`TransportExhausted`.
    """
    if max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")
    _rng = rng if rng is not None else random.random
    last_transport: BaseException | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            reply = provider.complete(system, messages, max_tokens=max_tokens)
            return str(reply or ""), {
                "attempts": attempt,
                "retried": attempt > 1,
            }
        except Exception as exc:  # noqa: BLE001 — classify then decide
            if not is_transport_error(exc):
                raise
            last_transport = exc
            if attempt >= max_attempts:
                break
            delay = _backoff_seconds(attempt - 1, exc, rng=_rng)
            sleep_fn(delay)
    detail = (
        f"{type(last_transport).__name__}: {last_transport}"
        if last_transport is not None
        else "unknown transport error"
    )
    raise TransportExhausted(
        detail,
        attempts=max_attempts,
        last_exc=last_transport,
    )


def transport_error_task(
    task_id: str,
    exc: BaseException,
    *,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a task result for an exhausted transport error (excluded from scoring)."""
    if isinstance(exc, TransportExhausted):
        detail = (
            f"provider error (transport exhausted after {exc.attempts} attempts): "
            f"{exc.detail}"
        )
        attempts = exc.attempts
    else:
        detail = f"provider error: {type(exc).__name__}: {exc}"
        attempts = 1
    row: dict[str, Any] = {
        "id": task_id,
        "passed": False,
        "error": True,
        "detail": detail,
        "reply": "",
        "attempts": attempts,
    }
    if extra:
        row.update(extra)
    return row


def finalize_axis_result(
    axis: str,
    results: list[dict[str, Any]],
    *,
    extra: dict[str, Any] | None = None,
    skipped: bool = False,
    skip_reason: str | None = None,
) -> dict[str, Any]:
    """Score an axis, excluding transport-error tasks from the denominator.

    * When >50% of tasks are transport errors → ``score=None`` (untested).
    * Otherwise score = n_passed / n_scored (errors excluded).
    * Always records ``n_errors``; attaches an ``errors`` note when any exist.
    """
    if skipped:
        n_tasks = len(results)
        if extra and "n_tasks" in extra:
            n_tasks = int(extra["n_tasks"])
        out: dict[str, Any] = {
            "axis": axis,
            "score": None,
            "n_tasks": n_tasks,
            "n_passed": 0,
            "n_errors": 0,
            "tasks": results,
            "skipped": True,
        }
        if skip_reason:
            out["skip_reason"] = skip_reason
        if extra:
            for k, v in extra.items():
                if k == "n_tasks":
                    continue
                out[k] = v
        return out

    n = len(results)
    errored = [r for r in results if r.get("error")]
    scored = [r for r in results if not r.get("error")]
    n_err = len(errored)
    n_pass = sum(1 for r in scored if r.get("passed"))
    n_scored = len(scored)

    if n == 0:
        score: float | None = 0.0
    elif n_err / n > 0.5:
        score = None
    elif n_scored == 0:
        score = None
    else:
        score = round(n_pass / n_scored, 4)

    out = {
        "axis": axis,
        "score": score,
        "n_tasks": n,
        "n_passed": n_pass,
        "n_errors": n_err,
        "n_scored": n_scored,
        "tasks": results,
        "skipped": False,
    }
    if n_err:
        first = errored[0]
        out["errors"] = {
            "n_errors": n_err,
            "first_detail": first.get("detail") or "",
            "note": (
                "transport/provider errors excluded from score denominator; "
                "axis untested (null) when >50% of tasks errored"
            ),
        }
        if score is None and n and n_err / n > 0.5:
            out["errors"]["axis_null_reason"] = (
                f"{n_err}/{n} tasks transport-errored (>50%)"
            )
    if extra:
        for k, v in extra.items():
            if k not in out or k.startswith("n_"):
                out[k] = v
    return out
