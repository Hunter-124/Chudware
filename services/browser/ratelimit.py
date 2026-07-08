"""services/browser/ratelimit.py — per-site politeness for the headless browser.

Every vendor fetch runs through :mod:`services.browser.session`, and every one
of those calls funnels through the helpers here so a single human-guided search
can only ever make the requests that search actually needs — never a runaway
loop hammering a vendor. This is politeness, not stealth: the goal is to behave
like one considerate visitor (one request at a time, spaced out, backing off
when a site signals "slow down"), which is exactly what keeps a user's own IP
and account in good standing.

Four layers, all persisted in the site's profile-metadata sidecar
(:mod:`services.browser.profiles`) so they survive process restarts:

1. **Minimum inter-request delay** — at least ``min_interval`` seconds between
   two fetches of the same site (default 3s).
2. **Published crawl-delay** — if the site's ``robots.txt`` publishes a
   ``Crawl-delay``, we honour it when it is *longer* than our default. (We read
   only ``Crawl-delay``; path ``Disallow`` rules are intentionally NOT enforced
   here — that would silently disable adapters for the ``/search`` paths many
   vendors list, and this layer is about pace, not access policy.)
3. **Exponential backoff on 429/403** — when a site returns a rate-limit or
   bot-wall status, its cooldown grows exponentially (``base * 2**(n-1)``,
   capped) and resets on the next clean fetch. Prevents a polite retry from
   turning into a hammer against a site that is already pushing back.
4. **Per-session request ceilings** — a per-site and a whole-process hard cap on
   requests per run, a backstop so an accidental loop (e.g. a bug calling
   part_search hundreds of times) trips a ceiling and degrades cleanly instead
   of flooding a vendor. Sized as a runaway-guard, not a normal-use limit.

Concurrency is capped at 1 per site elsewhere — :func:`profiles.profile_lock`
serialises every persistent-context open for a site — so this module does not
re-implement it.

All knobs read conservative defaults that can be raised or lowered via env vars
(``AI_CAD_BROWSER_*``); nothing here ever raises — ceiling/cooldown states are
reported back as data for the caller to degrade on.
"""
from __future__ import annotations

import os
import threading
import time
import urllib.request
from urllib.parse import urlsplit

from . import profiles


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ[name])
    except (KeyError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return default


#: Default minimum seconds between two fetches of the same site.
DEFAULT_MIN_INTERVAL_S = _env_float("AI_CAD_BROWSER_MIN_INTERVAL_S", 3.0)
#: Exponential-backoff base (first 429/403 cooldown) and ceiling.
BACKOFF_BASE_S = _env_float("AI_CAD_BROWSER_BACKOFF_BASE_S", 30.0)
BACKOFF_MAX_S = _env_float("AI_CAD_BROWSER_BACKOFF_MAX_S", 900.0)
#: Runaway-guard ceilings: max fetches per site and per whole process, per run.
MAX_REQ_PER_SITE = _env_int("AI_CAD_BROWSER_MAX_REQ_PER_SITE", 120)
MAX_REQ_PER_PROCESS = _env_int("AI_CAD_BROWSER_MAX_REQ_PER_PROCESS", 600)
#: How long a fetched robots.txt crawl-delay is cached before we re-read it.
_ROBOTS_TTL_S = 24 * 3600.0
#: HTTP statuses that mean "the site is asking us to slow down / backing us off".
_BACKOFF_STATUSES = frozenset({403, 429, 503})


# ---------------------------------------------------------------------------
# 4. Per-session request ceilings (in-process, reset on restart or explicitly).
# ---------------------------------------------------------------------------

_counts_lock = threading.Lock()
_per_site_counts: dict[str, int] = {}
_process_count = 0


def reset_session_counts() -> None:
    """Zero the per-session request ceilings (test hook / new-run reset)."""
    global _process_count
    with _counts_lock:
        _per_site_counts.clear()
        _process_count = 0


def reserve_request(site: str) -> str | None:
    """Count one request against ``site``'s and the process's session ceiling.

    Returns ``None`` when the request is allowed, or a short reason string when a
    ceiling is already reached (the caller should degrade, not fetch). Counting
    happens here (before the fetch) so a tight loop trips the guard immediately.
    """
    global _process_count
    key = profiles.slug(site)
    with _counts_lock:
        if _process_count >= MAX_REQ_PER_PROCESS:
            return "request_ceiling_process"
        if _per_site_counts.get(key, 0) >= MAX_REQ_PER_SITE:
            return "request_ceiling_site"
        _per_site_counts[key] = _per_site_counts.get(key, 0) + 1
        _process_count += 1
        return None


def session_counts() -> dict[str, object]:
    """Snapshot of the current per-session request counters (for status/tests)."""
    with _counts_lock:
        return {"process": _process_count, "per_site": dict(_per_site_counts),
                "max_per_site": MAX_REQ_PER_SITE, "max_per_process": MAX_REQ_PER_PROCESS}


# ---------------------------------------------------------------------------
# 2. Published crawl-delay from robots.txt (delay only — never Disallow).
# ---------------------------------------------------------------------------

def _fetch_robots_crawl_delay(host: str) -> float | None:
    """Read ``https://<host>/robots.txt`` and return the ``Crawl-delay`` that
    applies to us (the ``*`` group), or ``None`` when none is published or the
    file can't be read. Best-effort and short-timeout; never raises."""
    try:
        req = urllib.request.Request(
            f"https://{host}/robots.txt",
            headers={"User-Agent": "Mozilla/5.0 (AI-CAD parts browser)"})
        with urllib.request.urlopen(req, timeout=5.0) as resp:  # noqa: S310 — https host only
            text = resp.read(65536).decode("utf-8", "replace")
    except Exception:  # noqa: BLE001 — no robots / network error → no published delay
        return None
    # Parse the '*' user-agent group's Crawl-delay (simple, tolerant scan).
    applies = False
    delay: float | None = None
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        field, _, value = line.partition(":")
        field = field.strip().lower()
        value = value.strip()
        if field == "user-agent":
            applies = value == "*"
        elif field == "crawl-delay" and applies:
            try:
                delay = float(value)
            except ValueError:
                pass
    return delay


def crawl_delay(site: str, url: str | None) -> float | None:
    """Published crawl-delay (seconds) for ``site``, cached in profile meta.

    Re-reads ``robots.txt`` at most once per :data:`_ROBOTS_TTL_S`. ``url`` gives
    the host to read from; when absent (or no host) we reuse any cached value.
    ``None`` means "no crawl-delay published" (fall back to ``min_interval``)."""
    meta = profiles.load_meta(site)
    fetched = meta.get("robots_fetched_ts")
    if isinstance(fetched, (int, float)) and (time.time() - fetched) < _ROBOTS_TTL_S:
        cached = meta.get("robots_crawl_delay")
        return float(cached) if isinstance(cached, (int, float)) else None
    host = urlsplit(url).hostname if url else None
    if not host:
        cached = meta.get("robots_crawl_delay")
        return float(cached) if isinstance(cached, (int, float)) else None
    delay = _fetch_robots_crawl_delay(host)
    profiles.save_meta(site, robots_fetched_ts=time.time(),
                       robots_crawl_delay=delay if delay is not None else 0.0)
    return delay


# ---------------------------------------------------------------------------
# 1 + 3. Inter-request delay and 429/403 backoff cooldown.
# ---------------------------------------------------------------------------

def seconds_until_ready(site: str, *, min_interval: float = DEFAULT_MIN_INTERVAL_S,
                        url: str | None = None) -> float:
    """Seconds ``site`` must still wait before the next fetch (0.0 if ready now).

    The wait is the max of (a) the inter-request delay since the last fetch —
    itself the larger of ``min_interval`` and any published crawl-delay — and
    (b) any active 429/403 backoff cooldown."""
    meta = profiles.load_meta(site)
    now = time.time()

    effective_interval = min_interval
    published = crawl_delay(site, url)
    if published and published > effective_interval:
        effective_interval = published

    remaining = 0.0
    last = meta.get("last_fetch_ts")
    if isinstance(last, (int, float)):
        remaining = max(remaining, effective_interval - (now - last))

    cooldown_until = meta.get("rl_cooldown_until")
    if isinstance(cooldown_until, (int, float)):
        remaining = max(remaining, cooldown_until - now)

    return remaining if remaining > 0 else 0.0


def wait(site: str, *, min_interval: float = DEFAULT_MIN_INTERVAL_S,
         url: str | None = None, _sleep=time.sleep) -> float:
    """Block until ``site`` is ready to fetch again. Returns seconds waited."""
    remaining = seconds_until_ready(site, min_interval=min_interval, url=url)
    if remaining > 0:
        _sleep(remaining)
    return remaining


def mark_hit(site: str) -> None:
    """Record that ``site`` was just fetched, resetting its inter-request window."""
    profiles.save_meta(site, last_fetch_ts=time.time())


def note_result(site: str, status: int | None) -> None:
    """Update backoff state from a fetch's HTTP status.

    A 429/403/503 grows the site's cooldown exponentially (capped); any other
    outcome clears it. Also stamps ``last_fetch_ts`` (subsumes :func:`mark_hit`).
    """
    now = time.time()
    if status is not None and status in _BACKOFF_STATUSES:
        fails = profiles.load_meta(site).get("rl_consecutive_fails")
        fails = (fails if isinstance(fails, int) and fails > 0 else 0) + 1
        cooldown = min(BACKOFF_BASE_S * (2 ** (fails - 1)), BACKOFF_MAX_S)
        profiles.save_meta(site, last_fetch_ts=now, rl_consecutive_fails=fails,
                           rl_cooldown_until=now + cooldown)
    else:
        profiles.save_meta(site, last_fetch_ts=now, rl_consecutive_fails=0,
                           rl_cooldown_until=0.0)
