"""Fixture-based parser test for the ACP Composites search adapter.

ACP_SEARCH_HTML contains two FacetWP result blocks from a WooCommerce search.
Image URLs were updated from a later capture of the same product pages for
``/?s=carbon+fiber&post_type=product``. Results are a server-rendered
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
            <img src="https://acpcomposites.com/wp-content/uploads/2024/03/W-CF-5.7-TW-50-P_TwillCFFabric-1024x1024.jpg" class="attachment-large" alt="5.7 oz. Carbon Fiber"></a>
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
            <img src="https://acpcomposites.com/wp-content/uploads/2024/03/W-CF-3.5-PW-42-P_3.5ozCFFabric.jpg" class="attachment-large" alt="3.5 oz"></a>
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
    assert first["thumbnail"] == (
        "https://acpcomposites.com/wp-content/uploads/2024/03/"
        "W-CF-5.7-TW-50-P_TwillCFFabric-1024x1024.jpg")
    assert first["kind"] == "product"
    assert first["price"] is None and first["stock"] is None
    assert first["model"] == {"available": None, "formats": []}

    second = hits[1]
    assert second["title"] == "3.5 oz Carbon Fiber Fabric PW – Expired"
    assert second["thumbnail"] == (
        "https://acpcomposites.com/wp-content/uploads/2024/03/"
        "W-CF-3.5-PW-42-P_3.5ozCFFabric.jpg")


def test_acp_search_url_is_woocommerce_search():
    from services.browser.adapters import acpcomposites
    assert "?s={query}&post_type=product" in acpcomposites.SEARCH_URL_TMPL
    assert "%26" in acpcomposites.ADAPTER.search_url("resin & cloth")


def test_acp_header_signin_is_not_a_wall(acp_mod, monkeypatch):
    session, acp = acp_mod
    page = '<form><input name="password"></form>Sign In' + ACP_SEARCH_HTML
    monkeypatch.setattr(session, "browser_fetch",
                        lambda url, s: {"ok": True, "content": page})
    hits = acp.ADAPTER.search("carbon fiber")
    assert len(hits) == 2 and all(h.get("ok") for h in hits)


def test_acp_scopes_unknown_images_and_optional_price(acp_mod, monkeypatch):
    session, acp = acp_mod
    page = """
    <div class="acp-search-listing">
      <div class="acp-search-listing-thumbnail"><img src="/images/logo.png"></div>
      <div class="acp-search-listing-content"><h3><a href="/shop/one">First</a></h3>
        <span class="price"><span>$12.50</span></span><p class="stock">Out of stock</p>
      </div>
    </div>
    <div class="acp-search-listing">
      <div class="acp-search-listing-thumbnail"><img data-src="/images/cloth.jpg"></div>
      <div class="acp-search-listing-content"><h3><a href="/shop/two">Second</a></h3></div>
    </div><img src="/images/footer.jpg">
    """
    monkeypatch.setattr(session, "browser_fetch", lambda *_: {"ok": True, "content": page})
    first, second = acp.ADAPTER.search("cloth")
    assert first["thumbnail"] is None and first["price"] == 12.5
    assert first["currency"] == "USD" and first["stock"] == 0
    assert second["thumbnail"] == acp.BASE_URL + "/images/cloth.jpg"
    assert second["price"] is None and second["stock"] is None


@pytest.mark.parametrize(("content", "reason"), [
    ("<html>unexpected markup</html>", "parse_failed"),
    ("Access Denied", "blocked"),
    ("Access Denied: CAPTCHA required", "captcha"),
    ("<main>Verify you are human</main>", "captcha"),
])
def test_acp_search_reports_failures(acp_mod, monkeypatch, content, reason):
    session, acp = acp_mod
    monkeypatch.setattr(session, "browser_fetch", lambda *_: {"ok": True, "content": content})
    failure = acp.ADAPTER.search("cloth")[0]
    assert (failure["ok"], failure["site"], failure["op"], failure["reason"]) == (
        False, "acpcomposites", "search", reason)
    assert failure["url"] == acp.ADAPTER.search_url("cloth")


def test_acp_capabilities(acp_mod):
    _, acp = acp_mod
    assert acp.ADAPTER.result_kinds == ("product",)
    assert not acp.ADAPTER.cad_acquisition and not acp.ADAPTER.login_required and not acp.ADAPTER.api
    assert "acpcomposites.com" in acp.ADAPTER.hosts
