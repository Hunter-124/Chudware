"""Firecrawl API-key pool with automatic rotation — research tooling (``contracts/research_tools.md``).

One Firecrawl key rate-limits (HTTP 429) or runs out of credits (HTTP 402) long before a
deep research run is done. This module holds a *pool* of keys and rotates automatically:
a key that 429s is cooled down (honouring ``Retry-After``) and the request immediately
retries with the next ready key; a key that 401/402/403s is parked for 24 h; transport
and 5xx failures cool a key briefly. When every key is cooling the caller sleeps until
the soonest key recovers (capped by ``max_wait_s``) instead of giving up — research stops
when it is *done*, not when one key is throttled.

Key sources (merged, deduped, first occurrence wins):

1. ``FIRECRAWL_API_KEYS`` — comma/whitespace/newline-separated list (per-process override).
2. ``FIRECRAWL_API_KEY`` — single key (legacy contract spelling).
3. ``keys.firecrawl`` in the app ``config.json`` — a JSON list, or one string with the
   same separators. This is where the GUI keys map lives (``keys.openrouter`` etc.), so a
   GUI settings save merge-overlays and never clobbers it.
   Tenant cells use only their actor's BYOK environment keys, never config merging.

Cooldown state is persisted to a small JSON file (keyed by *hash* of the key, never the
key itself) so separate processes — the GUI agent and the MCP server — share one view
of which keys are hot. ``FIRECRAWL_POOL_STATE`` overrides the
state-file path (tests); ``FIRECRAWL_POOL_MAX_WAIT_S`` overrides the default wait cap.

Stdlib-only on purpose: scripts and custom tools can import it without the runtime
venv. ``request()`` takes an injectable
``transport`` so tests never touch the network.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Callable, Optional

API_BASE = "https://api.firecrawl.dev"
SCRAPE_PATH = "/v1/scrape"
SEARCH_PATH = "/v2/search"

ENV_KEYS = "FIRECRAWL_API_KEYS"
ENV_KEY = "FIRECRAWL_API_KEY"
ENV_STATE = "FIRECRAWL_POOL_STATE"
ENV_CONFIG = "FIRECRAWL_POOL_CONFIG"
ENV_MAX_WAIT = "FIRECRAWL_POOL_MAX_WAIT_S"

DEFAULT_MAX_WAIT_S = 180.0
COOLDOWN_RATE_LIMIT_S = 60.0        # 429 without a Retry-After header
COOLDOWN_DEAD_S = 24 * 3600.0       # 401/402/403 — invalid key or credits exhausted
COOLDOWN_SERVER_S = 30.0            # 5xx / transport failure
_MAX_ATTEMPTS_FACTOR = 3            # per-key attempt budget before giving up

_SPLIT_RE = re.compile(r"[\s,]+")

# transport(key, path, payload, timeout_s) -> (http_status, retry_after_s|None, body_bytes)
# status 0 = transport-level failure (DNS/connect/read timeout) with no HTTP response.
Transport = Callable[[str, str, dict, float], "tuple[int, Optional[float], bytes]"]


class PoolExhaustedError(RuntimeError):
    """Every key is cooling and the wait cap ran out (or no keys are configured)."""


class FirecrawlAPIError(RuntimeError):
    """A non-key-related API failure (4xx other than auth/rate/credits). Rotating keys
    cannot fix these, so they surface immediately."""

    def __init__(self, status: int, body: bytes):
        self.status = status
        snippet = body[:300].decode("utf-8", errors="replace")
        super().__init__(f"firecrawl API error {status}: {snippet}")


# --------------------------------------------------------------------------- key loading


def _split_keys(raw: str) -> list[str]:
    return [tok for tok in _SPLIT_RE.split(raw.strip()) if tok]


def _default_config_path() -> Optional[Path]:
    override = os.environ.get(ENV_CONFIG)
    if override:
        return Path(override)
    try:
        from services import paths  # app configuration path when running in the repo package

        return paths.CONFIG
    except Exception:  # noqa: BLE001 — running outside the repo tree (e.g. deployed tool)
        legacy = Path.home() / ".local" / "share" / "Chudware" / "config.json"
        return legacy if legacy.exists() else None


def load_keys(config_path: Optional[Path] = None) -> list[str]:
    """Merge + dedup keys from env vars and ``config.json`` (``keys.firecrawl``)."""
    out: list[str] = []

    def _add(candidates: list[str]) -> None:
        for key in candidates:
            if key and key not in out:
                out.append(key)

    _add(_split_keys(os.environ.get(ENV_KEYS, "")))
    single = os.environ.get(ENV_KEY, "").strip()
    if single:
        _add([single])

    from services.agent.request_context import in_cell
    if in_cell():
        return out  # tenant BYOK only; never merge desktop/operator config keys

    cfg_path = config_path or _default_config_path()
    if cfg_path and Path(cfg_path).exists():
        try:
            cfg = json.loads(Path(cfg_path).read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 — malformed config must not break research
            cfg = {}
        fc = (cfg.get("keys") or {}).get("firecrawl")
        if isinstance(fc, str):
            _add(_split_keys(fc))
        elif isinstance(fc, list):
            _add([str(k).strip() for k in fc if str(k).strip()])
    return out


# --------------------------------------------------------------------------- state file


def _state_path() -> Path:
    override = os.environ.get(ENV_STATE)
    if override:
        return Path(override)
    cfg = _default_config_path()
    if cfg is not None:
        return cfg.parent / "firecrawl_pool_state.json"
    return Path.home() / ".local" / "share" / "Chudware" / "firecrawl_pool_state.json"


def _kid(key: str) -> str:
    """State entries are keyed by hash — never persist raw keys in the state file."""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def _update_state(mutator: Callable[[dict], None]) -> dict:
    """Locked read-modify-write of the cooldown state (best-effort cross-process)."""
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        import fcntl  # POSIX
    except ImportError:  # pragma: no cover — Windows: best-effort without a lock
        fcntl = None  # type: ignore[assignment]

    with open(path, "a+", encoding="utf-8") as fh:
        if fcntl is not None:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            fh.seek(0)
            raw = fh.read()
            state = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError:
            state = {}
        mutator(state)
        fh.seek(0)
        fh.truncate()
        fh.write(json.dumps(state))
        fh.flush()
        if fcntl is not None:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    return state


def _cooldowns() -> dict:
    return _update_state(lambda s: None).get("cooldowns") or {}


def _set_cooldown(key: str, seconds: float, reason: str, status: int) -> None:
    until = time.time() + max(0.0, seconds)

    def _mut(state: dict) -> None:
        state.setdefault("cooldowns", {})[_kid(key)] = {
            "until": until, "reason": reason, "status": status,
        }

    _update_state(_mut)


def _record_success(key: str) -> None:
    kid = _kid(key)

    def _mut(state: dict) -> None:
        state.get("cooldowns", {}).pop(kid, None)
        state.setdefault("last_ok", {})[kid] = time.time()

    _update_state(_mut)


# --------------------------------------------------------------------------- transport


def _post(key: str, path: str, payload: dict, timeout: float) -> tuple[int, Optional[float], bytes]:
    from services.net.safe_http import request
    try:
        resp = request(API_BASE + path, method="POST",
                       body=json.dumps(payload).encode("utf-8"),
                       headers={"Authorization": f"Bearer {key}",
                                "Content-Type": "application/json",
                                "User-Agent": "Chudware-research/1.1"},
                       idle_timeout=timeout, max_bytes=8 * 1024 * 1024,
                       max_redirects=0, schemes=("https",))
        try:
            retry_after = float(resp.headers["retry-after"]) if "retry-after" in resp.headers else None
        except ValueError:
            retry_after = None
        return resp.status_code, retry_after, resp.content
    except (ValueError, RuntimeError, OSError):
        return 0, None, b""


# --------------------------------------------------------------------------- rotation


def request(
    path: str,
    payload: dict,
    *,
    timeout: float = 60.0,
    max_wait_s: Optional[float] = None,
    transport: Optional[Transport] = None,
    keys: Optional[list[str]] = None,
) -> dict:
    """POST ``payload`` to ``path``, rotating through the key pool until one succeeds.

    Raises :class:`PoolExhaustedError` when no key can serve within ``max_wait_s``
    (default ``FIRECRAWL_POOL_MAX_WAIT_S`` or 180 s) and :class:`FirecrawlAPIError`
    for request-level 4xx failures that no key rotation would fix.
    """
    pool = list(keys) if keys is not None else load_keys()
    if not pool:
        raise PoolExhaustedError(
            "no Firecrawl API keys configured (FIRECRAWL_API_KEYS / FIRECRAWL_API_KEY / "
            "config.json keys.firecrawl)"
        )
    if max_wait_s is None:
        try:
            max_wait_s = float(os.environ.get(ENV_MAX_WAIT, "") or DEFAULT_MAX_WAIT_S)
        except ValueError:
            max_wait_s = DEFAULT_MAX_WAIT_S
    deadline = time.monotonic() + max_wait_s
    do_request = transport or _post
    max_attempts = max(4, len(pool) * _MAX_ATTEMPTS_FACTOR)
    attempts = 0
    last_failure = "unknown failure"

    while attempts < max_attempts:
        now = time.time()
        cooling = _cooldowns()
        ready = [
            k for k in pool
            if float((cooling.get(_kid(k)) or {}).get("until") or 0.0) <= now
        ]
        if not ready:
            soonest = min(
                float((cooling.get(_kid(k)) or {}).get("until") or 0.0) for k in pool
            )
            wait = max(0.0, soonest - now)
            remaining = deadline - time.monotonic()
            if wait <= 0.0:
                continue
            if wait > remaining:
                raise PoolExhaustedError(
                    f"all {len(pool)} firecrawl keys cooling; soonest ready in "
                    f"{wait:.0f}s exceeds wait cap ({last_failure})"
                )
            time.sleep(wait)
            continue

        for key in ready:
            attempts += 1
            status, retry_after, body = do_request(key, path, payload, timeout)
            if 200 <= status < 300:
                _record_success(key)
                try:
                    return json.loads(body.decode("utf-8", errors="replace"))
                except json.JSONDecodeError as exc:
                    raise FirecrawlAPIError(status, body) from exc
            if status == 429:
                last_failure = "rate limited (429)"
                _set_cooldown(key, retry_after or COOLDOWN_RATE_LIMIT_S, "rate_limited", status)
            elif status in (401, 402, 403):
                last_failure = f"key rejected ({status})"
                _set_cooldown(key, COOLDOWN_DEAD_S, "key_dead", status)
            elif status in (400, 404, 405, 413, 422):
                raise FirecrawlAPIError(status, body)
            else:  # 0 transport failure, 5xx, anything unexpected
                last_failure = f"server/transport failure ({status})"
                _set_cooldown(key, COOLDOWN_SERVER_S, "server_error", status)

    raise PoolExhaustedError(
        f"firecrawl pool made {attempts} attempts across {len(pool)} keys without "
        f"success ({last_failure})"
    )


def scrape(url: str, *, timeout: float = 60.0, **kwargs: Any) -> tuple[str, dict]:
    """Scrape ``url`` via Firecrawl ``/v1/scrape``. Returns ``(markdown, metadata)``."""
    body = request(SCRAPE_PATH, {"url": url, "formats": ["markdown"]}, timeout=timeout, **kwargs)
    data = body.get("data") or {}
    return str(data.get("markdown") or ""), dict(data.get("metadata") or {})


def search(query: str, *, limit: int = 10, timeout: float = 60.0, **kwargs: Any) -> dict:
    """Web search via Firecrawl ``/v2/search``. Returns the decoded response body."""
    limit = max(1, min(100, int(limit)))
    payload = {"query": query, "limit": limit, "sources": [{"type": "web"}]}
    return request(SEARCH_PATH, payload, timeout=timeout, **kwargs)


def pool_status() -> list[dict]:
    """Per-key status for diagnostics/CLI: masked key, cooldown, last success."""
    now = time.time()
    cooling = _cooldowns()

    def _last_ok() -> dict:
        return _update_state(lambda s: None).get("last_ok") or {}

    last_ok = _last_ok()
    rows = []
    for i, key in enumerate(load_keys()):
        kid = _kid(key)
        cd = cooling.get(kid) or {}
        until = float(cd.get("until") or 0.0)
        rows.append({
            "index": i,
            "key": f"{key[:6]}…{key[-4:]}" if len(key) > 10 else "***",
            "ready": until <= now,
            "cooldown_remaining_s": max(0, int(until - now)),
            "reason": cd.get("reason"),
            "last_ok": last_ok.get(kid),
        })
    return rows
