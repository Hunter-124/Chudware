"""Fixture-based parser test for the BatterySpace search adapter.

BATTERYSPACE_SEARCH_HTML holds two ``product-list-item`` result cards captured
from a real BatterySpace search for ``/search.aspx?find=lithium+battery``; the
product image tags were added from a later capture of the same products. The storefront runs on
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
    <a href="/button-cell-5-pcs-cr11108-cr1-3n-3-0-v-lithium-batteries-non-rechargeable-0-24---un-38-3-passed.aspx">
      <img class="product-list-img" src="/ProductImages/primary/thumb_2923.jpg" alt="">
    </a>
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
    <a href="/lithium-ncm-14500-rechargeable-cell-3-7v-800mah-aa-size-2-96wh---un38-3-passed-and-iec-listed--.aspx">
      <img class="product-list-img" src="/images/products/thumb/3953.png" alt="">
    </a>
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
    assert first["price"] == 14.89 and first["currency"] == "USD"
    assert first["sku"] == "2923"
    assert first["thumbnail"] == "https://www.batteryspace.com/ProductImages/primary/thumb_2923.jpg"
    assert first["kind"] == "product" and first["stock"] is None
    assert first["model"] == {"available": None, "formats": []}

    second = hits[1]
    assert second["sku"] == "3953"
    assert second["price"] is None and second["currency"] == "USD"
    assert second["price_range"] == {"min": 4.08, "max": 7.08, "currency": "USD"}
    assert second["price_display"] == "From $4.08 to $7.08"
    assert second["thumbnail"] == "https://www.batteryspace.com/images/products/thumb/3953.png"


def test_batteryspace_search_url_is_ameri_search_endpoint():
    from services.browser.adapters import batteryspace
    assert "search.aspx?find=" in batteryspace.SEARCH_URL_TMPL
    assert "%26" in batteryspace.ADAPTER.search_url("battery & charger")


def test_batteryspace_public_signin_is_not_a_wall(bs_mod, monkeypatch):
    session, batteryspace = bs_mod
    page = ('<a>Sign In</a><form><input name="password"></form>'
            + BATTERYSPACE_SEARCH_HTML)
    monkeypatch.setattr(session, "browser_fetch",
                        lambda url, s: {"ok": True, "content": page})
    hits = batteryspace.ADAPTER.search("lithium battery")
    assert len(hits) == 2 and all(h.get("ok") for h in hits)


def test_batteryspace_ignores_rating_and_cart_images(bs_mod, monkeypatch):
    session, batteryspace = bs_mod
    page = """
    <div class="product-list-item">
      <div class="product-list-options"><h5><a href="/one.aspx">One</a></h5>
        <div class="product-list-qty">20 in stock</div>
        <img src="/themes/images/rating-full.gif"><img src="/themes/images/cart_btn_view.gif">
      </div>
    </div>
    <div class="product-list-item">
      <img class="product-list-img" data-src="/images/two.png">
      <div class="product-list-options"><h5><a href="/two.aspx">Two</a></h5></div>
    </div><img src="/footer.jpg">
    """
    monkeypatch.setattr(session, "browser_fetch", lambda *_: {"ok": True, "content": page})
    first, second = batteryspace.ADAPTER.search("battery")
    assert first["thumbnail"] is None and first["stock"] == 20
    assert second["thumbnail"] == batteryspace.BASE_URL + "/images/two.png"


@pytest.mark.parametrize(("content", "reason"), [
    ("<html>unexpected markup</html>", "parse_failed"),
    ("Access Denied", "blocked"),
    ("Access Denied: CAPTCHA", "captcha"),
    ("<main>Verify you are human</main>", "captcha"),
])
def test_batteryspace_reports_failures(bs_mod, monkeypatch, content, reason):
    session, batteryspace = bs_mod
    monkeypatch.setattr(session, "browser_fetch", lambda *_: {"ok": True, "content": content})
    failure = batteryspace.ADAPTER.search("battery")[0]
    assert (failure["ok"], failure["site"], failure["op"], failure["reason"]) == (
        False, "batteryspace", "search", reason)


def test_batteryspace_datasheet_is_document_not_cad(bs_mod, monkeypatch):
    session, batteryspace = bs_mod
    monkeypatch.setattr(session, "browser_fetch", lambda *_: {
        "ok": True, "content": '<a href="/docs/spec.pdf">Datasheet</a>'})

    def download(url, site, dest):
        assert url == batteryspace.BASE_URL + "/docs/spec.pdf" and site == "batteryspace"
        dest.write_bytes(b"%PDF-1.4\n%%EOF\n")
        return {"ok": True, "dest": str(dest)}

    monkeypatch.setattr(session, "browser_download", download)
    result = batteryspace.ADAPTER.acquire({"url": batteryspace.BASE_URL + "/one.aspx", "title": "One"})
    assert result["ok"] and result["artifact_kind"] == "document" and result["format"] == "pdf"
    assert result["artifact_path"].endswith(".pdf")


def test_batteryspace_capabilities(bs_mod):
    _, batteryspace = bs_mod
    assert batteryspace.ADAPTER.result_kinds == ("product",)
    assert not batteryspace.ADAPTER.cad_acquisition and not batteryspace.ADAPTER.login_required
    assert not batteryspace.ADAPTER.api and "batteryspace.com" in batteryspace.ADAPTER.hosts
