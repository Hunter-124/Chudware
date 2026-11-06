"""Offline fixtures for CoreMark's search and source-hit contract."""
import json

import pytest

from services.browser import session
from services.browser.adapters import coremarkmetals as coremark


SEARCH_HTML = '''
<header><a href="/account/login.cfm">Sign In</a>
  <a class="zoom" href="/navigation"><h2>Not a result</h2></a></header>
<div class="category search-results">
 <div class="tab-pane" id="nav-home">
  <a class="zoom" href="/6061-t6-aluminum-sheet">
   <div class="card"><img src="/files/6061.jpg" alt="6061 sheet">
    <h2>6061 Aluminum Sheet</h2><p>See <a href="/related">related</a></p></div></a>
  <a href="/brass-sheet" class="zoom"><div class="card">
   <img class="logo" src="/logo.png"><h2>Brass Sheet</h2></div></a>
 </div>
 <div class="tab-pane" id="nav-profile">
  <a class="zoom" href="/aluminum-sheet"><h2>Aluminum Sheet</h2>
   <img data-src="/files/category.jpg" alt="Aluminum sheet"></a>
 </div>
</div><footer><a class="zoom" href="/footer"><h2>Not a result</h2></a></footer>
'''


def test_live_markup_families_categories_and_header(monkeypatch):
    requests = []
    def fetch(url, site):
        requests.append((url, site))
        return {"ok": True, "content": SEARCH_HTML}
    monkeypatch.setattr(session, "browser_fetch", fetch)
    hits = coremark.ADAPTER.search("6061 & sheet")
    assert requests == [(coremark.BASE_URL + "/search/index.cfm?string=6061+%26+sheet", coremark.SITE)]
    assert [h["kind"] for h in hits] == ["series", "series", "category"]
    assert [h["title"] for h in hits] == ["6061 Aluminum Sheet", "Brass Sheet", "Aluminum Sheet"]
    assert hits[0]["thumbnail"] == coremark.BASE_URL + "/files/6061.jpg"
    assert hits[1]["thumbnail"] is None
    assert hits[2]["thumbnail"] == coremark.BASE_URL + "/files/category.jpg"
    assert hits[0]["material"] == "Aluminum"
    assert hits[0]["price"] is None and hits[0]["stock"] is None
    assert hits[0]["model"] == {"available": None, "formats": []}
    assert not coremark.ADAPTER.cad_acquisition
    assert set(coremark.ADAPTER.result_kinds) == {"product", "series", "category"}


def test_jsonld_metadata_and_numeric_zero_stock(monkeypatch):
    product = {"@type": "Product", "name": "6061 Aluminum Tube", "url": "/product/tube",
               "sku": "CM-1", "mpn": "T-1", "brand": {"name": "Mills"},
               "material": "6061-T6", "image": "/files/tube.jpg",
               "offers": {"price": "18.25", "priceCurrency": "USD", "inventoryLevel": 0}}
    page = '<script type="application/ld+json">' + json.dumps(product) + '</script>'
    monkeypatch.setattr(session, "browser_fetch", lambda *_: {"ok": True, "content": page})
    hit = coremark.ADAPTER.search("tube")[0]
    assert (hit["kind"], hit["sku"], hit["mpn"], hit["manufacturer"]) == ("product", "CM-1", "T-1", "Mills")
    assert (hit["price"], hit["currency"], hit["stock"], hit["material"]) == (18.25, "USD", 0, "6061-T6")
    assert hit["thumbnail"] == coremark.BASE_URL + "/files/tube.jpg"


def test_login_challenge_empty_and_unrecognized(monkeypatch):
    page = {"html": '<form id="signin"><input id="signin_password"></form>'}
    monkeypatch.setattr(session, "browser_fetch", lambda *_: {"ok": True, "content": page["html"]})
    assert coremark.ADAPTER.search("tube")[0]["reason"] == "login_required"
    page["html"] = "Access Denied: verify you are human"
    assert coremark.ADAPTER.search("tube")[0]["reason"] == "captcha"
    page["html"] = "No Item results <br> No Category results"
    assert coremark.ADAPTER.search("tube") == []
    page["html"] = "No Item results <div>unknown category markup</div>"
    assert coremark.ADAPTER.search("tube")[0]["reason"] == "parse_failed"


def test_acquire_derives_stock_with_document_not_cad(monkeypatch, tmp_path):
    page = {"html": '<a href="/docs/tube-datasheet.pdf">Datasheet</a>'}
    monkeypatch.setattr(session, "browser_fetch", lambda *_: {"ok": True, "content": page["html"]})
    pdf = tmp_path / "tube.pdf"
    pdf.write_bytes(b"%PDF-1.4\nfixture")
    monkeypatch.setattr(session, "browser_download", lambda *_: {"ok": True, "dest": str(pdf)})
    hit = {"url": coremark.BASE_URL + "/tube", "title": '6061-T6 Aluminum Round Tube 1" OD x 0.049" wall'}
    result = coremark.ADAPTER.acquire(hit)
    assert result["ok"] and result["artifact_path"] == str(pdf)
    assert result["artifact_kind"] == "document" and result["format"] == "pdf"
    assert result["params"]["alloy"] == "6061-T6"
    assert result["params"]["profile"] == "round_tube"
    assert result["params"]["od_mm"] == pytest.approx(25.4)
    assert result["params"]["wall_mm"] == pytest.approx(1.2446)
    page["html"] = "<html>no datasheet here</html>"
    failure = coremark.ADAPTER.acquire(hit)
    assert not failure["ok"] and failure["reason"] == "not_found"
