"""Fixture-based parser test for the Rock West Composites search adapter.

The ROCKWEST_SEARCH_HTML fixture below is a VERBATIM slice of a real Rock West
search page fetched live 2026-07-10 from the Salesforce Commerce Cloud
(Demandware) search endpoint
``/on/demandware.store/Sites-RWC-Site/default/Search-Show?q=carbon+fiber+tube``
(HTTP 200). It contains two real ``product-tile`` ``pdp-link`` cards copied
byte-for-byte. Rock West ships NO JSON-LD Product records on its SRP, so the
adapter parses these server-rendered cards directly (product URL + product-name
title + productSkuNumber P/N).

Offline style mirrors test_fastenal_parser.py: ``session.browser_fetch`` is
monkeypatched; no real browser, no network.
"""
from __future__ import annotations

import importlib

import pytest


# --- VERBATIM real slice (2 product cards) from the saved search page --------
ROCKWEST_SEARCH_HTML = r"""<div class="pdp-link">
    <a class="link" style="color:black;" href="/t-rnd-078-l12.html">
        <p class="product-name">
            Tube - Round - Pultruded Uni - 0.039 x 0.078 x 12"
        </p>
    </a>
    <p class="product-id product-sku availability">
        <span class="productSkuNumber">
            P/N:
            
                T-RND-078-L12
            
        </span>
<div class="pdp-link">
    <a class="link" style="color:black;" href="/25600-5cm.html">
        <p class="product-name">
            TUBE - HEXAGON - 1.00 X 1.10 X 1.968  IN (5 CM LENGTH)
        </p>
    </a>
    <p class="product-id product-sku availability">
        <span class="productSkuNumber">
            P/N:
            
                25600-5CM
            
        </span>"""


@pytest.fixture()
def rockwest_mod(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    from services import paths as engine_paths
    importlib.reload(engine_paths)
    from services.browser import profiles, session
    importlib.reload(profiles)
    importlib.reload(session)
    from services.browser import adapters as adapters_pkg
    importlib.reload(adapters_pkg)
    from services.browser.adapters import rockwest
    importlib.reload(rockwest)
    yield session, rockwest
    monkeypatch.delenv("APPDATA", raising=False)
    importlib.reload(engine_paths)


def test_rockwest_search_parses_demandware_cards(rockwest_mod, monkeypatch):
    session, rockwest = rockwest_mod
    monkeypatch.setattr(session, "browser_fetch",
                        lambda url, s: {"ok": True, "content": ROCKWEST_SEARCH_HTML})

    hits = rockwest.ADAPTER.search("carbon fiber tube")

    assert len(hits) == 2
    assert all(h["ok"] is True and h["site"] == "rockwest" for h in hits)

    first = hits[0]
    assert first["title"] == 'Tube - Round - Pultruded Uni - 0.039 x 0.078 x 12"'
    assert first["url"] == "https://www.rockwestcomposites.com/t-rnd-078-l12.html"
    assert first["sku"] == "T-RND-078-L12"

    second = hits[1]
    assert second["url"] == "https://www.rockwestcomposites.com/25600-5cm.html"
    assert second["sku"] == "25600-5CM"


def test_rockwest_search_url_is_demandware_endpoint():
    from services.browser.adapters import rockwest
    assert "Search-Show" in rockwest.SEARCH_URL_TMPL
