"""services/browser/ratelimit.py — per-site politeness cooldown for browser_fetch.

Baked into :func:`services.browser.session.browser_fetch` itself so every
source adapter (T-X2+) gets polite per-site rate-limiting for free without
remembering to throttle. Cooldown state persists in the site's profile
metadata sidecar (:mod:`services.browser.profiles`) so it survives process
restarts, not just in-memory state.
"""
from __future__ import annotations

import time

from . import profiles

#: Default minimum seconds between two fetches of the same site.
DEFAULT_MIN_INTERVAL_S = 3.0


def seconds_until_ready(site: str, *, min_interval: float = DEFAULT_MIN_INTERVAL_S) -> float:
    """Seconds ``site`` must still cool down before another fetch (0.0 if ready now)."""
    last = profiles.load_meta(site).get("last_fetch_ts")
    if not isinstance(last, (int, float)):
        return 0.0
    remaining = min_interval - (time.time() - last)
    return remaining if remaining > 0 else 0.0


def wait(site: str, *, min_interval: float = DEFAULT_MIN_INTERVAL_S, _sleep=time.sleep) -> float:
    """Block until ``site``'s cooldown has elapsed. Returns the seconds actually waited."""
    remaining = seconds_until_ready(site, min_interval=min_interval)
    if remaining > 0:
        _sleep(remaining)
    return remaining


def mark_hit(site: str) -> None:
    """Record that ``site`` was just fetched, resetting its cooldown window."""
    profiles.save_meta(site, last_fetch_ts=time.time())
