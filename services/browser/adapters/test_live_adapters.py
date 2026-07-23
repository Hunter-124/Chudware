"""Live canary searches against the real vendor sites (Parts Library V2).

Every test here is ``@pytest.mark.live`` — skipped by default and enabled only
with ``pytest --live`` or ``CHUDWARE_LIVE_TESTS=1`` (root conftest.py). They also
need Playwright plus a saved per-site browser profile (``browser_login``);
missing provisioning skips with a clear reason rather than failing.

A login_required / challenge degradation from a live site is a legitimate,
expected outcome (that IS the adapter contract) — it skips, it does not fail.
Only a real parse-to-zero on a healthy page fails.
"""
from __future__ import annotations

import pytest

from services.browser import profiles
from services.browser import session as _session
from services.browser.adapters import all_adapters, get_adapter
from services.browser.health import CANARY_QUERIES

#: Keep live page loads bounded — one nav per site, 30 s max.
_NAV_TIMEOUT_S = 30.0

_SITES = sorted(a.key for a in all_adapters())


@pytest.mark.live
@pytest.mark.parametrize("site", _SITES)
def test_live_canary_search(site, monkeypatch):
    adapter = get_adapter(site)
    assert adapter is not None, f"adapter '{site}' vanished from the registry"

    if not profiles.has_profile(site):
        pytest.skip(f"no saved browser profile for '{site}' — run browser_login('{site}') first")

    # Bound the live fetch: same entry point, explicit sane nav timeout.
    real_fetch = _session.browser_fetch
    monkeypatch.setattr(
        _session, "browser_fetch",
        lambda url, s, **kw: real_fetch(url, s, nav_timeout=_NAV_TIMEOUT_S, **kw),
    )

    query = CANARY_QUERIES.get(site, site)
    hits = adapter.search(query)

    assert isinstance(hits, list), f"{site}.search returned {type(hits).__name__}"
    assert all(isinstance(h, dict) for h in hits)

    failure = next((h for h in hits if h.get("ok") is False), None)
    if failure is not None:
        reason = failure.get("reason")
        if reason in ("login_required", "no_profile", "terms_confirmation_required",
                      "challenge_page", "playwright_not_installed"):
            pytest.skip(f"{site}: expected degradation '{reason}' — provision a login/session")
        pytest.fail(f"{site}: live search failed: {failure}")

    good = [h for h in hits if h.get("title") and h.get("url")]
    assert good, f"{site}: live canary '{query}' parsed zero usable hits ({len(hits)} raw)"
