"""Live canary searches against the real vendor sites (Parts Library).

Every test here is ``@pytest.mark.live`` — skipped by default and enabled only
with ``pytest --live`` or ``CHUDWARE_LIVE_TESTS=1`` (root conftest.py). Searches run
anonymously, including sources whose CAD downloads need a sign-in; a source that
walls search itself reports ``login_required`` and skips.

A typed degradation from a live site (sign-in wall, bot wall, captcha, missing API
credentials, rate limit, missing backend) is a legitimate outcome of the adapter
contract — it skips, it does not fail. A parse failure or a page that yields no
usable hit fails, and so does a hit outside the normalized schema.
"""
from __future__ import annotations

import pytest

from services.browser.adapters import HIT_KINDS, all_adapters, get_adapter
from services.browser.health import CANARY_QUERIES


_SITES = sorted(a.key for a in all_adapters())

# Outcomes that need provisioning (a login, API keys, a browser backend) or reflect
# the vendor refusing automated access; none of them is an adapter defect.
_EXPECTED_DEGRADATIONS = frozenset({
    "login_required", "credentials_required", "terms_required", "blocked", "captcha",
    "rate_limited", "policy_refused", "unsupported", "no_profile", "playwright_not_installed",
})


@pytest.mark.live
@pytest.mark.parametrize("site", _SITES)
def test_live_canary_search(site):
    adapter = get_adapter(site)
    assert adapter is not None, f"adapter '{site}' vanished from the registry"

    # Searches run anonymously even for sources whose downloads need a sign-in; a
    # source that walls search itself reports login_required and skips below.
    query = CANARY_QUERIES.get(site, site)
    hits = adapter.search(query)

    assert isinstance(hits, list), f"{site}.search returned {type(hits).__name__}"
    assert all(isinstance(h, dict) for h in hits)

    failure = next((h for h in hits if h.get("ok") is False), None)
    if failure is not None:
        if failure.get("reason") in _EXPECTED_DEGRADATIONS:
            pytest.skip(f"{site}: expected degradation '{failure.get('reason')}' — {failure.get('hint', '')}")
        pytest.fail(f"{site}: live search failed: {failure}")

    good = [h for h in hits if h.get("title") and h.get("url")]
    assert good, f"{site}: live canary '{query}' parsed zero usable hits ({len(hits)} raw)"
    for hit in good:
        assert hit.get("kind") in HIT_KINDS, f"{site}: hit kind {hit.get('kind')!r} outside HIT_KINDS"
        thumb = hit.get("thumbnail")
        assert thumb is None or str(thumb).startswith(("http://", "https://")), \
            f"{site}: non-absolute thumbnail {thumb!r}"
