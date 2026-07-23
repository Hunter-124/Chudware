"""Fixture-based parser test for the BatterySpace search adapter.

BATTERYSPACE_SEARCH_HTML holds two VERBATIM ``product-list-item`` result cards
from a real BatterySpace site-search page fetched live 2026-07-10 from
``/search.aspx?find=lithium+battery`` (HTTP 200). The storefront runs on
AmeriCommerce (.aspx); results are server-rendered cards (no JSON-LD). The
adapter reads the ``<h5><a href>`` product URL + title, the
``product-list-cost-value`` price and the ``product-list-text`` "Product ID: N"
SKU. The public catalog's header "Sign In" / newsletter password field must not
trip a login wall (markers dropped).

Offline style mirrors test_cui_parser.py: session.browser_fetch monkeypatched.
"""
from __future__ import annotations

import importlib

import pytest


BATTERYSPACE_SEARCH_HTML = r"""<div class="product-list-item">
	<div class="product-list-options">
	  <h5><a href="/button-cell-5-pcs-cr11108-cr1-3n-3-0-v-lithium-batteries-non-rechargeable-0-24---un-38-3-passed.aspx">Button Cell: 5 Pcs CR11108 (CR1/3N ) 3V Lithium Batteries (Non-Rechargeable) - UN 38.3 Passed (#2923)</a></h5>
	  <div class="product-list-price">
	    <div class="product-list-cost"><span class="product-list-cost-label">Your Price:</span> <span class="product-list-cost-value">$14.89</span></div>
	  </div>
	  <div class="product-list-text">
		Product ID: 2923<br><br>
	  </div>
	</div>
</div>
<div class="product-list-item">
	<div class="product-list-options">
	  <h5><a href="/lithium-ncm-14500-rechargeable-cell-3-7v-800mah-aa-size-2-96wh---un38-3-passed-and-iec-listed--.aspx">Lithium (NCM) 14500 Rechargeable Cell: 3.7V 800mAh (AA size, 2.96Wh) - UN38.3 Passed -</a></h5>
	  <div class="product-list-price">
	    <div class="product-list-cost"><span class="product-list-cost-label">Your Price:</span> <span class="product-list-cost-value">From $4.08 to $7.08</span></div>
	  </div>
	  <div class="product-list-text">
		Product ID: 3953<br><br>
	  </div>
	</div>
</div>"""


@pytest.fixture()
def bs_mod(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    from services import paths as engine_paths
    importlib.reload(engine_paths)
    from services.browser import profiles, session
    importlib.reload(profiles)
    importlib.reload(session)
    from services.browser import adapters as adapters_pkg
    importlib.reload(adapters_pkg)
    from services.browser.adapters import batteryspace
    importlib.reload(batteryspace)
    yield session, batteryspace
    monkeypatch.delenv("APPDATA", raising=False)
    importlib.reload(engine_paths)


def test_batteryspace_search_parses_result_cards(bs_mod, monkeypatch):
    session, batteryspace = bs_mod
    monkeypatch.setattr(session, "browser_fetch",
                        lambda url, s: {"ok": True, "content": BATTERYSPACE_SEARCH_HTML})

    hits = batteryspace.ADAPTER.search("lithium battery")

    assert len(hits) == 2
    assert all(h["ok"] is True and h["site"] == "batteryspace" for h in hits)

    first = hits[0]
    assert first["title"].startswith("Button Cell: 5 Pcs CR11108")
    assert first["url"] == (
        "https://www.batteryspace.com/button-cell-5-pcs-cr11108-cr1-3n-3-0-v-"
        "lithium-batteries-non-rechargeable-0-24---un-38-3-passed.aspx")
    assert first["price"] == "$14.89"
    assert first["sku"] == "2923"

    second = hits[1]
    assert second["sku"] == "3953"
    assert second["price"] == "From $4.08 to $7.08"


def test_batteryspace_search_url_is_ameri_search_endpoint():
    from services.browser.adapters import batteryspace
    assert "search.aspx?find=" in batteryspace.SEARCH_URL_TMPL


def test_batteryspace_public_signin_is_not_a_wall(bs_mod, monkeypatch):
    session, batteryspace = bs_mod
    page = ('<a>Sign In</a><form><input name="password"></form>'
            + BATTERYSPACE_SEARCH_HTML)
    monkeypatch.setattr(session, "browser_fetch",
                        lambda url, s: {"ok": True, "content": page})
    hits = batteryspace.ADAPTER.search("lithium battery")
    assert len(hits) == 2 and all(h.get("ok") for h in hits)
