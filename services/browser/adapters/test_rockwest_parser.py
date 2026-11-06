"""Fixture-based parser test for the Rock West Composites search adapter.

The original captured Demandware PDP-link snippets are extended with their
corresponding image, price and availability markup from a later search-page
capture, using the same product-tile nesting as the storefront.

Offline style mirrors test_fastenal_parser.py: ``session.browser_fetch`` is
monkeypatched; no real browser, no network.
"""
from __future__ import annotations

import importlib

import pytest


# Captured product identities, with corresponding Demandware tile markup.
ROCKWEST_SEARCH_HTML = r"""<div class="product-tile">
<div class="image-container"><a href="/t-rnd-078-l12.html"><img class="tile-image"
 src="https://www.rockwestcomposites.com/dw/image/v2/bkvx_prd/on/demandware.static/-/Sites-rwc-master-catalog/default/dwde3e7a8c/hi-res/round-tubes/RT-S-PT-UNI-B.jpg?sw=417&amp;sh=510"></a></div>
<div class="pdp-link">
    <a class="link" style="color:black;" href="/t-rnd-078-l12.html">
        <p class="product-name">
            Tube - Round - Pultruded Uni - 0.039 x 0.078 x 12"
        </p>
    </a>
    <p class="product-id product-sku availability">
        <span class="productSkuNumber">
            P/N:
            
                T-RND-078-L12
            
        </span><span class="availabilityProduct">Available Now: 4</span></p>
</div><div class="price"><span class="sales default-price" data-price="5.99" data-price-formatted="$5.99"><span class="value" content="5.99"></span> $5.99</span></div>
</div>
<div class="product-tile">
<div class="image-container"><a href="/25600-5cm.html"><img class="tile-image"
 src="https://www.rockwestcomposites.com/dw/image/v2/bkvx_prd/on/demandware.static/-/Sites-rwc-master-catalog/default/dwbc0a1cb9/hi-res/tube/CF-Hexagonal_1f.jpg?sw=417&amp;sh=510"></a></div>
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
            
        </span><span class="availabilityProduct">Available On Backorder</span></p>
</div><div class="price"><span class="sales default-price" data-price="8.19" data-price-formatted="$8.19"><span class="value" content="8.19"></span> $8.19</span></div>
</div>"""


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
    assert first["kind"] == "product" and first["price"] == 5.99 and first["currency"] == "USD"
    assert first["stock"] == 4 and first["model"] == {"available": None, "formats": []}
    assert first["thumbnail"].endswith("round-tubes/RT-S-PT-UNI-B.jpg?sw=417&sh=510")

    second = hits[1]
    assert second["url"] == "https://www.rockwestcomposites.com/25600-5cm.html"
    assert second["sku"] == "25600-5CM"
    assert second["thumbnail"].endswith("tube/CF-Hexagonal_1f.jpg?sw=417&sh=510")
    assert second["price"] == 8.19 and second["stock"] is None


def test_rockwest_search_url_is_demandware_endpoint():
    from services.browser.adapters import rockwest
    assert "Search-Show" in rockwest.SEARCH_URL_TMPL
    assert "%26" in rockwest.ADAPTER.search_url("carbon & tube")


def test_rockwest_sale_price_and_missing_image_are_scoped(rockwest_mod, monkeypatch):
    session, rockwest = rockwest_mod
    page = """
    <div class="product-tile"><div class="image-container"><img src="/logo.png"></div>
      <div class="pdp-link"><a class="link" href="/one.html"><p class="product-name">One</p></a>
        <span class="productSkuNumber">P/N: ONE</span></div>
      <div class="price"><del>$29.99</del><span class="sales default-price"
         data-price="9.99" data-price-formatted="$9.99">$9.99</span></div>
    </div>
    <div class="product-tile"><div class="image-container"><img data-src="/two.jpg"></div>
      <div class="pdp-link"><a class="link" href="/two.html"><p class="product-name">Two</p></a>
        <span class="productSkuNumber">P/N: TWO</span></div>
    </div><img src="/footer.jpg">
    """
    monkeypatch.setattr(session, "browser_fetch", lambda *_: {"ok": True, "content": page})
    first, second = rockwest.ADAPTER.search("tube")
    assert first["thumbnail"] is None and first["price"] == 9.99
    assert second["thumbnail"] == rockwest.BASE_URL + "/two.jpg" and second["price"] is None


@pytest.mark.parametrize(("content", "reason"), [
    ("<html>unexpected markup</html>", "parse_failed"),
    ("Access Denied", "blocked"),
    ("Pardon Our Interruption: CAPTCHA", "captcha"),
    ("<main>Verify you are human</main>", "captcha"),
])
def test_rockwest_reports_failures(rockwest_mod, monkeypatch, content, reason):
    session, rockwest = rockwest_mod
    monkeypatch.setattr(session, "browser_fetch", lambda *_: {"ok": True, "content": content})
    failure = rockwest.ADAPTER.search("tube")[0]
    assert (failure["ok"], failure["site"], failure["op"], failure["reason"]) == (
        False, "rockwest", "search", reason)


def test_rockwest_capabilities(rockwest_mod):
    _, rockwest = rockwest_mod
    assert rockwest.ADAPTER.result_kinds == ("product",)
    assert not rockwest.ADAPTER.cad_acquisition and not rockwest.ADAPTER.login_required
    assert not rockwest.ADAPTER.api and "rockwestcomposites.com" in rockwest.ADAPTER.hosts
