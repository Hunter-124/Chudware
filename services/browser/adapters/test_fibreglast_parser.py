"""Fixture-based parser test for the Fibre Glast search adapter.

FIBREGLAST_SEARCH_HTML retains the two captured Shopify Dawn product anchors and
uses their actual product image URLs and card structure from a later search-page
capture. The Shopify storefront-forms captcha library is passive protection,
not a challenge wall.

Offline style mirrors test_fastenal_parser.py: session.browser_fetch monkeypatched.
"""
from __future__ import annotations

import importlib

import pytest


FIBREGLAST_SEARCH_HTML = r"""<div class="card-wrapper product-card-wrapper">
 <div class="card__media"><div class="media"><img
  src="//www.fibreglast.com/cdn/shop/files/Saertex_20830_20Stitched_20Biaxial-2_bf3b2359-981f-4bd3-96d5-841770d9a5d3.jpg?v=1729278828&amp;width=533"></div></div>
 <a href="/products/saertex-830-stitched-biaxial?_pos=1&amp;_sid=1bee920d8&amp;_ss=r" id="CardLink--8829534634221" class="full-unstyled-link" aria-labelledby="CardLink--8829534634221 Badge--8829534634221">
              Saertex 830 g/m² (24.5 oz/yd²) Stitched Biaxial (+/-45)
            </a>
</div>
<div class="card-wrapper product-card-wrapper">
 <div class="card__media"><div class="media"><img
  src="//www.fibreglast.com/cdn/shop/files/Scrim_20Fabric_20White-2_b08635b7-b23e-4e2e-a9a2-1a0b7aeb2dc0.jpg?v=1729277718&amp;width=533"></div></div>
 <a href="/products/scrim-fabric-white-246?_pos=2&amp;_sid=1bee920d8&amp;_ss=r" id="CardLink--8829521330413" class="full-unstyled-link" aria-labelledby="CardLink--8829521330413 Badge--8829521330413">
              Scrim Fabric, White
            </a>
</div>"""


@pytest.fixture()
def fibreglast_mod(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    from services import paths as engine_paths
    importlib.reload(engine_paths)
    from services.browser import profiles, session
    importlib.reload(profiles)
    importlib.reload(session)
    from services.browser import adapters as adapters_pkg
    importlib.reload(adapters_pkg)
    from services.browser.adapters import fibreglast
    importlib.reload(fibreglast)
    yield session, fibreglast
    monkeypatch.delenv("APPDATA", raising=False)
    importlib.reload(engine_paths)


def test_fibreglast_search_parses_shopify_cards(fibreglast_mod, monkeypatch):
    session, fibreglast = fibreglast_mod
    monkeypatch.setattr(session, "browser_fetch",
                        lambda url, s: {"ok": True, "content": FIBREGLAST_SEARCH_HTML})

    hits = fibreglast.ADAPTER.search("fiberglass cloth")

    assert len(hits) == 2
    assert all(h["ok"] is True and h["site"] == "fibreglast" for h in hits)

    first = hits[0]
    assert first["title"] == "Saertex 830 g/m² (24.5 oz/yd²) Stitched Biaxial (+/-45)"
    assert first["url"] == "https://www.fibreglast.com/products/saertex-830-stitched-biaxial"
    # tracking query stripped
    assert "?" not in first["url"]
    assert first["thumbnail"] == (
        "https://www.fibreglast.com/cdn/shop/files/"
        "Saertex_20830_20Stitched_20Biaxial-2_bf3b2359-981f-4bd3-96d5-841770d9a5d3.jpg"
        "?v=1729278828&width=533")
    assert first["kind"] == "product" and first["price"] is None and first["stock"] is None
    assert first["model"] == {"available": None, "formats": []}
    assert hits[1]["thumbnail"] == (
        "https://www.fibreglast.com/cdn/shop/files/"
        "Scrim_20Fabric_20White-2_b08635b7-b23e-4e2e-a9a2-1a0b7aeb2dc0.jpg"
        "?v=1729277718&width=533")


def test_fibreglast_scopes_images_prices_and_sold_out(fibreglast_mod, monkeypatch):
    session, fibreglast = fibreglast_mod
    page = """
    <div class="card-wrapper product-card-wrapper">
      <div class="card__media"><img src="/logo.png"></div>
      <a class="full-unstyled-link" href="/products/first">First &amp; best</a>
      <span class="price-item--regular">$20.00</span>
      <span class="price-item--sale">$15.00 USD</span><span class="badge">Sold out</span>
    </div>
    <div class="card-wrapper product-card-wrapper">
      <div class="card__media"><img data-src="/products/photo.jpg"></div>
      <a class="full-unstyled-link" href="/products/second">Second</a>
    </div><img src="/footer.jpg">
    <div class="card-wrapper article-card-wrapper">
      <a class="full-unstyled-link" href="/blogs/news">Article</a>
    </div>
    <script src="/storefront-forms-captcha.js"></script>
    """
    monkeypatch.setattr(session, "browser_fetch", lambda *_: {"ok": True, "content": page})
    first, second = fibreglast.ADAPTER.search("fiberglass")
    assert first["title"] == "First & best" and first["thumbnail"] is None
    assert first["price"] == 15.0 and first["currency"] == "USD" and first["stock"] == 0
    assert second["thumbnail"] == fibreglast.BASE_URL + "/products/photo.jpg"
    assert second["price"] is None and second["stock"] is None


@pytest.mark.parametrize(("content", "reason"), [
    ('<div>Search results: changed markup</div>', "parse_failed"),
    ('<script>const message = "0 results";</script><div>changed markup</div>', "parse_failed"),
    ("Access Denied", "blocked"),
    ("Pardon Our Interruption: CAPTCHA required", "captcha"),
    ("<main>Verify you are human</main>", "captcha"),
])
def test_fibreglast_reports_unparseable_and_walls(fibreglast_mod, monkeypatch, content, reason):
    session, fibreglast = fibreglast_mod
    monkeypatch.setattr(session, "browser_fetch", lambda *_: {"ok": True, "content": content})
    failure = fibreglast.ADAPTER.search("cloth & resin")[0]
    assert (failure["ok"], failure["site"], failure["op"], failure["reason"]) == (
        False, "fibreglast", "search", reason)
    assert "%26" in failure["url"]


def test_fibreglast_anchor_only_and_explicit_no_results(fibreglast_mod, monkeypatch):
    session, fibreglast = fibreglast_mod
    content = '<a href="/products/one" class="full-unstyled-link">One</a><img src="/footer.jpg">'
    monkeypatch.setattr(session, "browser_fetch", lambda *_: {"ok": True, "content": content})
    hit = fibreglast.ADAPTER.search("one")[0]
    assert hit["title"] == "One" and hit["thumbnail"] is None
    content = "<main>No results</main>"
    assert fibreglast.ADAPTER.search("not present") == []


def test_fibreglast_capabilities(fibreglast_mod):
    _, fibreglast = fibreglast_mod
    assert fibreglast.ADAPTER.result_kinds == ("product",)
    assert not fibreglast.ADAPTER.cad_acquisition and not fibreglast.ADAPTER.login_required
    assert not fibreglast.ADAPTER.api and "fibreglast.com" in fibreglast.ADAPTER.hosts
