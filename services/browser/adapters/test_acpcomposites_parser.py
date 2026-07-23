"""Fixture-based parser test for the ACP Composites search adapter.

ACP_SEARCH_HTML holds two VERBATIM ``acp-search-listing`` result blocks from a
real WooCommerce site-search page fetched live 2026-07-10 from
``/?s=carbon+fiber&post_type=product`` (HTTP 200). Results are a server-rendered
FacetWP grid (no JSON-LD Products); the adapter reads the ``<h3><a href="/shop/
<slug>">Title</a></h3>`` inside each ``acp-search-listing-content`` block. The
header "Sign In" widget must not trip the wall.

Offline style mirrors test_cui_parser.py: session.browser_fetch monkeypatched.
"""
from __future__ import annotations

import importlib

import pytest


ACP_SEARCH_HTML = r"""<div class="acp-search-listing">
    <div class="acp-search-listing-thumbnail">
        <a href="https://acpcomposites.com/shop/clearance/5-7-oz-carbon-fiber-fabric-2x2-twill-clearance">
            <img src="https://acpcomposites.com/wp-content/uploads/2024/03/W-CF.jpg" class="attachment-large" alt="5.7 oz. Carbon Fiber"></a>
    </div>
    <div class="acp-search-listing-content">
        <div class="post-type product">Product</div>
        <h3><a href="https://acpcomposites.com/shop/clearance/5-7-oz-carbon-fiber-fabric-2x2-twill-clearance">5.7 oz Carbon Fiber Fabric 2×2 Twill – Clearance</a></h3>
        The 5.7 oz. Carbon Fiber, Style 284, is a high strength two-by-two weave fabric...
        <a class="read-more" href="https://acpcomposites.com/shop/clearance/5-7-oz-carbon-fiber-fabric-2x2-twill-clearance">Read More</a>
    </div>
</div>
<div class="acp-search-listing">
    <div class="acp-search-listing-thumbnail">
        <a href="https://acpcomposites.com/shop/carbon-fiber/carbon-woven-fabrics/3-5-oz-carbon-fiber-fabric-pw-expired">
            <img src="https://acpcomposites.com/wp-content/uploads/2024/03/TUR.jpg" class="attachment-large" alt="3.5 oz"></a>
    </div>
    <div class="acp-search-listing-content">
        <div class="post-type product">Product</div>
        <h3><a href="https://acpcomposites.com/shop/carbon-fiber/carbon-woven-fabrics/3-5-oz-carbon-fiber-fabric-pw-expired">3.5 oz Carbon Fiber Fabric PW – Expired</a></h3>
        A plain weave carbon fiber fabric...
        <a class="read-more" href="https://acpcomposites.com/shop/carbon-fiber/carbon-woven-fabrics/3-5-oz-carbon-fiber-fabric-pw-expired">Read More</a>
    </div>
</div>"""


@pytest.fixture()
def acp_mod(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    from services import paths as engine_paths
    importlib.reload(engine_paths)
    from services.browser import profiles, session
    importlib.reload(profiles)
    importlib.reload(session)
    from services.browser import adapters as adapters_pkg
    importlib.reload(adapters_pkg)
    from services.browser.adapters import acpcomposites
    importlib.reload(acpcomposites)
    yield session, acpcomposites
    monkeypatch.delenv("APPDATA", raising=False)
    importlib.reload(engine_paths)


def test_acp_search_parses_result_cards(acp_mod, monkeypatch):
    session, acp = acp_mod
    monkeypatch.setattr(session, "browser_fetch",
                        lambda url, s: {"ok": True, "content": ACP_SEARCH_HTML})

    hits = acp.ADAPTER.search("carbon fiber")

    assert len(hits) == 2
    assert all(h["ok"] is True and h["site"] == "acpcomposites" for h in hits)

    first = hits[0]
    assert first["title"] == "5.7 oz Carbon Fiber Fabric 2×2 Twill – Clearance"
    assert first["url"] == (
        "https://acpcomposites.com/shop/clearance/"
        "5-7-oz-carbon-fiber-fabric-2x2-twill-clearance")

    second = hits[1]
    assert second["title"] == "3.5 oz Carbon Fiber Fabric PW – Expired"


def test_acp_search_url_is_woocommerce_search():
    from services.browser.adapters import acpcomposites
    assert "?s={query}&post_type=product" in acpcomposites.SEARCH_URL_TMPL


def test_acp_header_signin_is_not_a_wall(acp_mod, monkeypatch):
    session, acp = acp_mod
    page = '<form><input name="password"></form>Sign In' + ACP_SEARCH_HTML
    monkeypatch.setattr(session, "browser_fetch",
                        lambda url, s: {"ok": True, "content": page})
    hits = acp.ADAPTER.search("carbon fiber")
    assert len(hits) == 2 and all(h.get("ok") for h in hits)
