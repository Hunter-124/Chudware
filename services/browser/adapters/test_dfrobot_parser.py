"""Fixture-based tests for the DFRobot source adapter (dfrobot.py).

Live-verified 2026-07-14: the real SSR search route is ``/search-<query>.html``
(e.g. ``/search-gravity.html``). The old OpenCart-style guesses
(``/search-adv/result?q=`` etc.) hard-404. The fixture below is a trimmed but
field-faithful slice of the real product-item card markup from that page
(name / SKU / $price / /product-N.html). ``session.browser_fetch`` is
monkeypatched — no CEF/network in CI.

Acceptance: search returns real hits from the correct endpoint, OR an honest
login_required/challenge/http_error — never a silent empty list for a 404.
"""
from __future__ import annotations

import importlib

import pytest


# Trimmed real card markup from /search-gravity.html (2026-07-14).
DFROBOT_SEARCH_HTML = """
<!DOCTYPE html><html><head><title>Search - gravity - products - DFRobot</title></head>
<body>
<div class="search-result"><ul class="product-list grid-product-list">
<div data-theme="pc" class="product-item appearance_3">
  <div class="img-box"></div>
  <div class="d-flex align-center justify-space-between">
    <p class="sku">SKU: DFR0759</p>
    <p class="stock transparent">In Stock</p>
  </div>
  <a href="/product-2179.html"><p class="name">Gravity: I2C HUB</p></a>
  <p class="price">$1.50</p>
</div>
<div data-theme="pc" class="product-item appearance_3">
  <div class="img-box"></div>
  <div class="d-flex align-center justify-space-between">
    <p class="sku">SKU: SEN0290</p>
    <p class="stock transparent">In Stock</p>
  </div>
  <a href="/product-1828.html"><p class="name">Gravity: Analog TDS Sensor</p></a>
  <p class="price">$21.90</p>
</div>
</ul></div>
</body></html>
"""

DFROBOT_CHALLENGE_HTML = """
<html><head><title>Access Denied</title></head>
<body><h1>Access Denied</h1><p>Reference #18.abc123.1</p>
<p>Pardon Our Interruption</p></body></html>
"""


@pytest.fixture()
def dfrobot_mod(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    import services.paths as engine_paths
    import services.browser.session as session
    import services.browser.adapters.dfrobot as dfrobot
    importlib.reload(engine_paths)
    importlib.reload(session)
    importlib.reload(dfrobot)
    return session, dfrobot


def test_dfrobot_search_url_uses_search_slug_route(dfrobot_mod):
    _session, dfrobot = dfrobot_mod
    url = dfrobot.ADAPTER.search_url("gravity sensor")
    assert url == "https://www.dfrobot.com/search-gravity-sensor.html"
    # Old 404 path must NOT be the template.
    assert "search-adv" not in dfrobot.SEARCH_URL_TMPL
    assert "route=product/search" not in dfrobot.SEARCH_URL_TMPL


def test_dfrobot_search_parses_product_item_hits(dfrobot_mod, monkeypatch):
    session, dfrobot = dfrobot_mod
    monkeypatch.setattr(
        session, "browser_fetch",
        lambda url, site: {"ok": True, "content": DFROBOT_SEARCH_HTML})

    hits = dfrobot.ADAPTER.search("gravity")
    assert len(hits) == 2
    assert hits[0]["title"] == "Gravity: I2C HUB"
    assert hits[0]["url"] == "https://www.dfrobot.com/product-2179.html"
    assert hits[0]["sku"] == "DFR0759"
    assert hits[0]["mpn"] == "DFR0759"
    assert hits[0]["price"] == "$1.50"
    assert hits[0]["site"] == "dfrobot"
    assert hits[1]["sku"] == "SEN0290"
    assert hits[1]["price"] == "$21.90"


def test_dfrobot_challenge_degrades_to_login_required(dfrobot_mod, monkeypatch):
    session, dfrobot = dfrobot_mod
    monkeypatch.setattr(
        session, "browser_fetch",
        lambda url, site: {"ok": True, "content": DFROBOT_CHALLENGE_HTML})

    hits = dfrobot.ADAPTER.search("gravity")
    assert len(hits) == 1
    assert hits[0].get("ok") is False
    assert hits[0].get("reason") == "login_required"


def test_dfrobot_http_404_is_not_silent_empty(dfrobot_mod, monkeypatch):
    """A hard 404 from browser_fetch must surface as a structured failure, never []."""
    session, dfrobot = dfrobot_mod
    monkeypatch.setattr(
        session, "browser_fetch",
        lambda url, site: {
            "ok": False,
            "reason": "http_error",
            "status": 404,
            "hint": f"GET {url} → 404",
        })

    hits = dfrobot.ADAPTER.search("gravity")
    assert hits, "404 must not collapse to a silent empty list"
    assert hits[0].get("ok") is False
    assert hits[0].get("reason") in ("http_error", "login_required", "no_profile")
