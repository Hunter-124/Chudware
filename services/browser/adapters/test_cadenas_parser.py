"""Offline fixtures for the 3Dfindit adapter contract."""
import pytest

from services.browser import session
from services.browser.adapters import cadenas


CARDS = '''
<a href="/en/digitaltwin/bearing?path=demo%2Fid" class="tile gridCardBody">
  <img title="Maker &amp; Co" src="/logos/maker.svg" class="supplierImage">
  <img src="/images/bearing.png" class="nodeImage">
  <div class="part-nn-name">family-608</div><div class="text">Bearing <mark>608</mark></div>
</a>
<a class="gridCardBody" href="/en/cad-bim-library/manufacturer/maker">
  <img class="supplierImage" src="/logos/maker.svg" title="Maker &amp; Co">Maker
</a>
'''

# Trimmed rendered CEF search results for "hex bolt", November 2026.
RENDERED_CARDS = '''
<a rel="nofollow" class="grid-card-wrapper" href="/en/cad-bim-library/manufacturer/beltpdf-catalog?path=pdfcatalog_belt">
 <div class="grid-card body"><div class="image-container">
  <img class="node-image" src="https://www.3dfindit.com/FileService/CatalogImage?PreviewPath=pdfcatalog_belt%2Fcatalog.png&amp;width=100" alt="Belt - PDF Catalog">
 </div><div class="catalog-name" style="overflow-wrap: break-word;">Belt - PDF Catalog</div></div>
</a>
<a rel="nofollow" class="grid-card-wrapper" href="/en/digitaltwin/hex-bolt/washer-type?path=iwata%2Fcaps%2Fhex_bolt_washer_type.prj">
 <div class="grid-card header"><div class="supplier-info" title="IWATA MFG. CO., LTD.">
  <img class="supplier-image" src="https://www.3dfindit.com/FileService/CatalogImage?PreviewPath=iwata%2Fcatalog.png">
  <span>IWATA MFG. CO., LTD.</span></div></div>
 <div class="grid-card body"><div class="image-container">
  <img class="node-image" src="https://www.3dfindit.com/FileService/CatalogImage?PreviewPath=iwata%2Fcaps%2Fhex_bolt_washer_type.png&amp;width=100" alt="">
  <img class="supplier-image-overlay" src="https://www.3dfindit.com/FileService/CatalogImage?PreviewPath=iwata%2Fcatalog.png">
 </div><div class="part-nn-name" style="overflow-wrap: break-word;"><mark>Hex</mark>. <mark>bolt</mark>/Washer type</div></div>
 <div class="grid-card footer"><div class="overview">10 products</div></div>
</a>
'''


def test_rendered_cards(monkeypatch):
    monkeypatch.setattr(session, "browser_fetch", lambda *_: {"ok": True, "content": RENDERED_CARDS})
    category, series = cadenas.ADAPTER.search("hex bolt")
    assert category["kind"] == "category" and category["title"] == "Belt - PDF Catalog"
    assert category["thumbnail"] is None
    assert series["kind"] == "series" and series["title"] == "Hex . bolt /Washer type"
    assert series["manufacturer"] == "IWATA MFG. CO., LTD."
    assert series["thumbnail"].endswith("hex_bolt_washer_type.png&width=100")
    assert "catalog_id" not in series


def test_search_cards_keep_kinds_images_and_metadata(monkeypatch):
    seen = []
    monkeypatch.setattr(session, "browser_fetch", lambda url, site: (
        seen.append((url, site)) or {"ok": True, "content": CARDS}))
    hits = cadenas.ADAPTER.search("bearing A&B #1/2")
    assert "A%26B+%231%2F2" in seen[0][0]
    assert [hit["kind"] for hit in hits] == ["series", "category"]
    assert hits[0]["title"] == "Bearing 608"
    assert hits[0]["manufacturer"] == "Maker & Co"
    assert hits[0]["catalog_id"] == "family-608"
    assert hits[0]["thumbnail"] == "https://www.3dfindit.com/images/bearing.png"
    assert hits[0]["url"].endswith("?path=demo%2Fid")
    assert hits[0]["price"] is None and hits[0]["model"]["available"] is None
    assert hits[1]["thumbnail"] is None  # supplier logo is not a part preview
    assert cadenas.ADAPTER.result_kinds == ("series", "category")
    assert not cadenas.ADAPTER.cad_acquisition


@pytest.mark.parametrize("content, reason", [
    ("<html>unrecognized results layout</html>", "parse_failed"),
    ('<div class="login-form">Please log in</div>', "login_required"),
    ('<div class="g-recaptcha"></div>', "captcha"),
])
def test_search_classifies_page_failures(monkeypatch, content, reason):
    monkeypatch.setattr(session, "browser_fetch", lambda *_: {"ok": True, "content": content})
    out = cadenas.ADAPTER.search("bearing")[0]
    assert out["ok"] is False and out["site"] == "cadenas"
    assert out["op"] == "search" and out["reason"] == reason


def test_search_explicit_no_results(monkeypatch):
    monkeypatch.setattr(session, "browser_fetch", lambda *_: {"ok": True, "content": "No results"})
    assert cadenas.ADAPTER.search("unknown") == []


@pytest.mark.parametrize("op", ["search", "acquire"])
def test_fetch_failure_is_not_terms(monkeypatch, op):
    failure = {"ok": False, "reason": "timeout", "hint": "timed out", "status": 503,
               "retry_after_s": 17, "detail": "socket timed out"}
    monkeypatch.setattr(session, "browser_fetch", lambda *_: failure)
    if op == "search":
        out = cadenas.ADAPTER.search("bearing")[0]
    else:
        out = cadenas.ADAPTER.acquire({"url": "https://www.3dfindit.com/en/digitaltwin/x"})
    assert out["reason"] == "timeout" and out["op"] == op and out["site"] == "cadenas"
    assert out["status"] == 503 and out["retry_after_s"] == 17
    assert out["detail"] == "socket timed out" and out["ask_user"] is None


def test_configured_acquisition_is_explicitly_unsupported(monkeypatch):
    monkeypatch.setattr(session, "browser_fetch", lambda *_: {"ok": True, "content": "<h1>Part</h1>"})
    monkeypatch.setattr(session, "browser_download", lambda *_: pytest.fail("unexpected download"))
    out = cadenas.ADAPTER.acquire({"url": "https://www.3dfindit.com/en/digitaltwin/x?part=608",
                                   "sku": "608", "format": "step"})
    assert not out["ok"] and out["reason"] == "unsupported"
    assert out["ask_user"] is None and "configuration" in out["hint"]


def test_actual_terms_prompt_is_preserved(monkeypatch):
    monkeypatch.setattr(session, "browser_fetch", lambda *_: {
        "ok": True, "content": '<div class="supplier-terms">Accept the terms</div>'})
    out = cadenas.ADAPTER.acquire({"url": "https://www.3dfindit.com/en/digitaltwin/x"})
    assert out["reason"] == "terms_required"
    assert out["ask_user"]["kind"] == "approval" and not out["ask_user"]["suggested"]


def test_search_manufacturer_name_may_contain_apostrophe(monkeypatch):
    content = ('<a class="gridCardBody" href="/en/digitaltwin/test">'
               '<img class="supplierImage" title="Maker\'s Company" src="/logo.png">'
               '<div class="text">Part</div></a>')
    monkeypatch.setattr(session, "browser_fetch", lambda *_: {"ok": True, "content": content})
    assert cadenas.ADAPTER.search("part")[0]["manufacturer"] == "Maker's Company"
