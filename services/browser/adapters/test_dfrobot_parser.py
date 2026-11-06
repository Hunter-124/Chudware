"""Fixture-based tests for the DFRobot source adapter (dfrobot.py).

The SSR search route is ``/search-<query>.html`` (e.g. ``/search-gravity.html``).
These trimmed cards use captured product data
and image URLs; the first image is represented as a lazy-loaded image and the
second as the site's product-video poster. Fetches are monkeypatched.
"""
from __future__ import annotations

import importlib

import pytest


# Search card shapes and product/image data from /search-gravity.html.
DFROBOT_SEARCH_HTML = """
<!DOCTYPE html><html><head><title>Search - gravity - products - DFRobot</title></head>
<body>
<div class="currency"><button>$USD</button></div>
<div class="search-result"><ul class="product-list grid-product-list">
<div data-theme="pc" class="product-item appearance_3">
  <div class="img-box"><img src="data:image/gif;base64,AA"
       data-src="https://dfimg.dfrobot.com/enshop/DFR0759/DFR0759_Main_01.jpg"></div>
  <div class="d-flex align-center justify-space-between">
    <p class="sku">SKU: DFR0759</p>
    <p class="stock transparent">In Stock</p>
  </div>
  <a href="/product-2179.html"><p class="name">Gravity: I2C HUB</p></a>
  <p class="price">$1.50</p>
</div>
<div data-theme="pc" class="product-item appearance_3">
  <div class="img-box"><video poster="https://dfimg.dfrobot.com/enshop/image/data/SEN0291/SEN0291_Main_01_364x228.jpg.webp"></video></div>
  <div class="d-flex align-center justify-space-between">
    <p class="sku">SKU: SEN0291</p>
    <p class="stock transparent">In Stock</p>
  </div>
  <a href="/product-1827.html"><p class="name">Gravity: I2C Digital Wattmeter</p></a>
  <p class="price">$6.90</p>
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
    assert dfrobot.ADAPTER.search_url("Gravity & I2C/#") == (
        "https://www.dfrobot.com/search-gravity-%26-i2c%2F%23.html")


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
    assert hits[0]["mpn"] is None and hits[0]["kind"] == "product"
    assert hits[0]["price"] == 1.50 and hits[0]["currency"] == "USD"
    assert hits[0]["site"] == "dfrobot" and hits[0]["stock"] is None
    assert hits[0]["model"] == {"available": None, "formats": []}
    assert hits[0]["thumbnail"] == "https://dfimg.dfrobot.com/enshop/DFR0759/DFR0759_Main_01.jpg"
    assert hits[1]["sku"] == "SEN0291" and hits[1]["price"] == 6.90
    assert hits[1]["currency"] == "USD"
    assert hits[1]["thumbnail"].endswith("/SEN0291/SEN0291_Main_01_364x228.jpg.webp")


def test_dfrobot_challenge_is_blocked(dfrobot_mod, monkeypatch):
    session, dfrobot = dfrobot_mod
    monkeypatch.setattr(
        session, "browser_fetch",
        lambda url, site: {"ok": True, "content": DFROBOT_CHALLENGE_HTML})

    hits = dfrobot.ADAPTER.search("gravity")
    assert len(hits) == 1
    assert hits[0].get("ok") is False
    assert hits[0].get("reason") == "blocked"


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
    assert hits[0]["reason"] == "not_found" and hits[0]["status"] == 404
    assert hits[0]["site"] == "dfrobot" and hits[0]["op"] == "search"


def test_dfrobot_capabilities_and_unknown_currency(dfrobot_mod, monkeypatch):
    session, dfrobot = dfrobot_mod
    assert dfrobot.ADAPTER.hosts == ("dfrobot.com",)
    assert dfrobot.ADAPTER.result_kinds == ("product",)
    assert not dfrobot.ADAPTER.cad_acquisition and not dfrobot.ADAPTER.login_required and not dfrobot.ADAPTER.api
    page = DFROBOT_SEARCH_HTML.replace('<div class="currency"><button>$USD</button></div>', "")
    monkeypatch.setattr(session, "browser_fetch", lambda url, site: {"ok": True, "content": page})
    assert dfrobot.ADAPTER.search("gravity")[0]["currency"] is None


@pytest.mark.parametrize(("selected", "display", "currency"), [
    ("$USD", "$1.50", "USD"),
    ("EUR€", "€1.50", "EUR"),
    ("£GBP", "£1.50", "GBP"),
    ("$CAD", "$1.50", "CAD"),
])
def test_dfrobot_selected_currency(dfrobot_mod, monkeypatch, selected, display, currency):
    session, dfrobot = dfrobot_mod
    page = DFROBOT_SEARCH_HTML.replace("$USD", selected).replace("$1.50", display)
    monkeypatch.setattr(session, "browser_fetch", lambda url, site: {"ok": True, "content": page})
    first = dfrobot.ADAPTER.search("gravity")[0]
    assert first["price"] == 1.5 and first["currency"] == currency


def test_dfrobot_nuxt_images_follow_product_href(dfrobot_mod, monkeypatch):
    session, dfrobot = dfrobot_mod
    page = r'''
    <div class="product-item"><div class="img-box">
      <div style="background-image:url('/dfrobotshop/images/lazy.jpg')"></div></div>
      <a href="/product-2179.html" class="name">Gravity: I2C HUB</a>
      <p class="sku">SKU: DFR0759</p></div>
    <div class="product-item"><div class="img-box"><img src="/images/logo.png" alt="logo"></div>
      <a href="/product-42.html" class="name">Other</a></div>
    <script>window.__NUXT__=(function(){return {
      href:"https:\u002F\u002Fwww.dfrobot.com\u002Fproduct-2179.html",
      image:"https:\u002F\u002Fdfimg.dfrobot.com\u002Fenshop\u002FDFR0759\u002FDFR0759_Main_01.jpg"
    }})()</script><footer><img src="/images/other-product.jpg"></footer>
    '''
    monkeypatch.setattr(session, "browser_fetch", lambda url, site: {"ok": True, "content": page})
    first, second = dfrobot.ADAPTER.search("gravity")
    assert first["thumbnail"] == "https://dfimg.dfrobot.com/enshop/DFR0759/DFR0759_Main_01.jpg"
    assert second["thumbnail"] is None and second["price"] is None


def test_dfrobot_empty_and_unparseable_are_distinct(dfrobot_mod, monkeypatch):
    session, dfrobot = dfrobot_mod
    empty = '<p>0 item(s) found for "nothing"</p><div class="empty-result">Not finding any matches</div>'
    monkeypatch.setattr(session, "browser_fetch", lambda url, site: {"ok": True, "content": empty})
    assert dfrobot.ADAPTER.search("nothing") == []
    shell = '<div class="empty-result" style="display:none">0 item(s) found for old query</div>'
    monkeypatch.setattr(session, "browser_fetch", lambda url, site: {"ok": True, "content": shell})
    assert dfrobot.ADAPTER.search("gravity")[0]["reason"] == "parse_failed"


@pytest.mark.parametrize(("page", "reason"), [
    ("<h1>Verify you are human</h1>", "captcha"),
    ("<p>Please sign in to continue</p>", "login_required"),
])
def test_dfrobot_other_wall_types(dfrobot_mod, monkeypatch, page, reason):
    session, dfrobot = dfrobot_mod
    monkeypatch.setattr(session, "browser_fetch", lambda url, site: {"ok": True, "content": page})
    failure = dfrobot.ADAPTER.search("gravity")[0]
    assert failure["reason"] == reason and failure["site"] == "dfrobot" and failure["op"] == "search"


def test_dfrobot_datasheet_is_a_document(dfrobot_mod, monkeypatch, tmp_path):
    session, dfrobot = dfrobot_mod
    from services.browser.adapters import _retail
    monkeypatch.setattr(session, "browser_fetch", lambda url, site: {
        "ok": True, "content": '<a href="/docs/spec.pdf">Datasheet</a>'})
    dest = tmp_path / "spec.pdf"
    monkeypatch.setattr(_retail, "download_path", lambda *args, **kwargs: (dest, None))

    def download(url, site, path):
        path.write_bytes(b"%PDF-1.4\n%%EOF\n")
        return {"ok": True, "dest": str(path)}

    monkeypatch.setattr(session, "browser_download", download)
    outcome = dfrobot.ADAPTER.acquire({"title": "Board", "url": dfrobot.BASE_URL + "/product-2179.html"})
    assert outcome["ok"] and outcome["artifact_path"] == str(dest)
    assert outcome["artifact_kind"] == "document" and outcome["format"] == "pdf"

    def download_html(url, site, path):
        path.write_bytes(b"<html>Sign in</html>")
        return {"ok": True, "dest": str(path)}

    monkeypatch.setattr(session, "browser_download", download_html)
    rejected = dfrobot.ADAPTER.acquire({"title": "Board", "url": dfrobot.BASE_URL + "/product-2179.html"})
    assert not rejected["ok"] and rejected["reason"] == "parse_failed"
    assert "artifact_path" not in rejected
