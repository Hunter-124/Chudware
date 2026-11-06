"""Offline Anaheim fixtures, including condensed live Magento card markup."""
from __future__ import annotations

import io
import zipfile

import pytest


# Fields and image URLs from the live 2026-11-04 Magento search response;
# repeated wrapper/price markup is condensed to keep the fixture readable.
CARDS = '''<ol class="product-items">
<li class="item product product-item"><div class="product-item-info">
 <a href="https://anaheimautomation.com/gbphr-0902-ss-100-aa341-500.html">
  <img class="product-image-photo" data-src="https://anaheimautomation.com/media/catalog/product/cache/c5b0e6136a6dd7f7d91d8b889ed40f35/G/B/GBPHR-SS__300x200__1_53.png"></a>
 <strong class="product-item-name"><a class="product-item-link"
  href="https://anaheimautomation.com/gbphr-0902-ss-100-aa341-500.html">GBPHR-0902-SS-100-AA341-500</a></strong>
 <span id="product-price-36162" data-price-amount="1329" data-price-type="finalPrice"><span class="price">$1,329.00</span></span>
 <span data-price-amount="1078.53">$1,078.53</span></div></li>
<li class="item product product-item"><div class="product-item-info">
 <img data-src="/media/catalog/product/GBPHR-SS__300x200__1_52.png">
 <a href="/gbphr-0902-ss-070-aa341-500.html" class="product-item-link">GBPHR-0902-SS-070-AA341-500</a>
 <span data-price-type="finalPrice" data-price-amount="1329"><span class="price">$1,329.00</span></span></div></li>
</ol>'''
# The live search includes this checkout login form alongside public results.
LOGIN_DIALOG = '''<div class="block block-customer-login" data-label="or">
 <strong id="block-customer-login-heading" role="heading" aria-level="2">Checkout using your account</strong>
 <div class="block-content" aria-labelledby="block-customer-login-heading">
  <form class="form form-login" method="post" data-bind="event: {submit: login }" id="login-form">
   <input name="username" id="customer-email" type="email">
   <input name="password" type="password" id="pass">
  </form></div></div>'''
STEP = b"ISO-10303-21;\nHEADER;\nENDSEC;\nDATA;\nENDSEC;\nEND-ISO-10303-21;\n"
PRODUCT = "https://anaheimautomation.com/gbphr-0902-ss-100-aa341-500.html"


@pytest.fixture
def vendor(monkeypatch, tmp_path):
    from services.browser.adapters import anaheimautomation, paths
    monkeypatch.setattr(paths, "BROWSER_DOWNLOADS", tmp_path)
    return anaheimautomation


def _fetch(monkeypatch, vendor, content, final_url=None):
    calls = []

    def fetch(url, site):
        calls.append((url, site))
        return {"ok": True, "content": content, "final_url": final_url or url}

    monkeypatch.setattr(vendor._session, "browser_fetch", fetch)
    return calls


def _download(monkeypatch, vendor, content):
    calls = []

    def download(url, site, dest):
        calls.append(url)
        dest.write_bytes(content)
        return {"ok": True, "dest": str(dest), "session_profile_used": "fixture"}

    monkeypatch.setattr(vendor._session, "browser_download", download)
    return calls


def _zip(files):
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return data.getvalue()


def test_search_live_magento_cards_preserve_identity_and_commerce(vendor, monkeypatch):
    calls = _fetch(monkeypatch, vendor, CARDS)
    first, second = vendor.ADAPTER.search("stepper & #1/2")
    assert calls == [("https://anaheimautomation.com/catalogsearch/result/?q=stepper+%26+%231%2F2",
                      "anaheimautomation")]
    assert first["site"] == "anaheimautomation" and first["kind"] == "product"
    assert first["title"] == first["sku"] == first["mpn"] == "GBPHR-0902-SS-100-AA341-500"
    assert first["url"] == PRODUCT and first["manufacturer"] == "Anaheim Automation"
    assert first["price"] == 1329.0 and first["currency"] == "USD"
    assert first["thumbnail"].endswith("GBPHR-SS__300x200__1_53.png")
    assert first["model"] == {"available": None, "formats": []} and first["stock"] is None
    assert second["thumbnail"] == "https://anaheimautomation.com/media/catalog/product/GBPHR-SS__300x200__1_52.png"
    assert second["sku"] == "GBPHR-0902-SS-070-AA341-500" and second["price"] == 1329.0

def test_search_public_results_with_embedded_login_dialog(vendor, monkeypatch):
    _fetch(monkeypatch, vendor, "<title>Search results for: 'stepper motor'</title>" + LOGIN_DIALOG + CARDS)
    hits = vendor.ADAPTER.search("stepper motor")
    assert len(hits) == 2 and hits[0]["url"] == PRODUCT
    _fetch(monkeypatch, vendor, "<title>Search results</title>" + LOGIN_DIALOG + "Your search returned no results.")
    assert vendor.ADAPTER.search("unknown") == []



def test_search_deduplicates_and_leaves_unknowns_unknown(vendor, monkeypatch):
    card = '''<li class="product-item" data-sku="ABC-123" data-stock-qty="7">
      <img class="logo" src="/logo.png"><img src="data:image/gif,x" data-src="/products/abc.jpg">
      <a class="product-item-link" href="/abc.html">Example motor</a></li>'''
    _fetch(monkeypatch, vendor, card + card)
    hit, = vendor.ADAPTER.search("abc")
    assert hit["sku"] == "ABC-123" and hit["mpn"] is None and hit["stock"] == 7
    assert hit["price"] is None and hit["currency"] is None
    assert hit["thumbnail"] == "https://anaheimautomation.com/products/abc.jpg"


def test_search_legacy_title_only_does_not_borrow_images(vendor, monkeypatch):
    _fetch(monkeypatch, vendor, '<img src="/logo.png"><a class="product-item-link" href="/a.html">A-123</a>')
    hit, = vendor.ADAPTER.search("motor")
    assert hit["sku"] == "A-123" and hit["thumbnail"] is None


def test_search_empty_parse_and_challenge_outcomes(vendor, monkeypatch):
    _fetch(monkeypatch, vendor, "Your search returned no results.")
    assert vendor.ADAPTER.search("none") == []
    _fetch(monkeypatch, vendor, "<html>Changed Magento markup</html>")
    assert vendor.ADAPTER.search("motor")[0]["reason"] == "parse_failed"
    _fetch(monkeypatch, vendor, "<h1>Access Denied</h1>")
    assert vendor.ADAPTER.search("motor")[0]["reason"] == "blocked"
    _fetch(monkeypatch, vendor, "<h1>Verify you are human</h1>")
    assert vendor.ADAPTER.search("motor")[0]["reason"] == "captcha"
    _fetch(monkeypatch, vendor, '<h1>Customer Login</h1><form id="login-form">Customer Login</form>')
    failure = vendor.ADAPTER.search("motor")[0]
    assert failure["reason"] == "login_required" and failure["ask_user"]


def test_transport_failure_keeps_context(vendor, monkeypatch):
    monkeypatch.setattr(vendor._session, "browser_fetch", lambda *_: {
        "ok": False, "reason": "timeout", "hint": "timed out", "status": 504})
    failure, = vendor.ADAPTER.search("motor")
    assert failure["reason"] == "timeout" and failure["status"] == 504
    assert failure["site"] == "anaheimautomation" and failure["op"] == "search"
    assert failure["url"].endswith("?q=motor")


def test_acquire_valid_step_and_preserve_session(vendor, monkeypatch):
    _fetch(monkeypatch, vendor, '<a href="/media/3D/part.step?download=1">STEP</a>')
    calls = _download(monkeypatch, vendor, STEP)
    out = vendor.ADAPTER.acquire({"url": PRODUCT, "sku": "GBPHR-0902-SS-100-AA341-500"})
    assert out["ok"] and out["artifact_kind"] == "cad" and out["format"] == "step"
    assert out["session_profile_used"] == "fixture"
    assert calls == ["https://anaheimautomation.com/media/3D/part.step?download=1"]


def test_acquire_rar_is_unsupported_and_pdf_is_a_document(vendor, monkeypatch):
    # The actual product page offers a series-level .rar and a series spec PDF.
    _fetch(monkeypatch, vendor,
           '<a href="/media/3D/GBPHR-090x-SS.rar">STEP file (RAR)</a>'
           '<a href="/media/manuals/GBPHR-Spec-Sheet.pdf">Download</a>')
    assert vendor.ADAPTER.acquire({"url": PRODUCT})["reason"] == "unsupported"
    _download(monkeypatch, vendor, b"%PDF-1.7\nfixture\n%%EOF")
    doc = vendor.ADAPTER.acquire({"url": PRODUCT, "format": "pdf"})
    assert doc["ok"] and doc["artifact_kind"] == "document" and doc["format"] == "pdf"


@pytest.mark.parametrize("content", [b"<html>login</html>", b"%PDF-1.7\nnot step", b""])
def test_acquire_rejects_false_cad(vendor, monkeypatch, content):
    _fetch(monkeypatch, vendor, '<a href="/file.step">STEP</a>')
    _download(monkeypatch, vendor, content)
    failure = vendor.ADAPTER.acquire({"url": PRODUCT})
    assert failure["reason"] == "parse_failed" and failure["op"] == "acquire"


def test_acquire_zip_never_chooses_another_variant(vendor, monkeypatch):
    _fetch(monkeypatch, vendor, '<a href="/3d/series.zip">STEP</a>')
    _download(monkeypatch, vendor, _zip({"one.step": STEP, "two.step": STEP}))
    out = vendor.ADAPTER.acquire({"url": PRODUCT, "sku": "one"})
    assert out["ok"] and out["artifact_kind"] == "archive" and out["format"] == "zip"
    assert "configuration" in out["archive_note"]
    _download(monkeypatch, vendor, _zip({"only.step": STEP}))
    out = vendor.ADAPTER.acquire({"url": PRODUCT})
    assert out["ok"] and out["artifact_kind"] == "cad" and out["archive_member"] == "only.step"


def test_acquire_requires_specific_choice_and_propagates_failure(vendor, monkeypatch):
    _fetch(monkeypatch, vendor, '<a href="/one.step">one</a><a href="/two.step">two</a>')
    calls = _download(monkeypatch, vendor, STEP)
    assert vendor.ADAPTER.acquire({"url": PRODUCT})["reason"] == "unsupported" and calls == []
    assert vendor.ADAPTER.acquire({"url": PRODUCT, "kind": "series"})["reason"] == "unsupported"
    selected = vendor.ADAPTER.acquire({"url": PRODUCT, "cad_url": "/two.step"})
    assert selected["ok"] and calls == ["https://anaheimautomation.com/two.step"]
    monkeypatch.setattr(vendor._session, "browser_download", lambda *_: {
        "ok": False, "reason": "blocked", "status": 403})
    failed = vendor.ADAPTER.acquire({"url": PRODUCT, "cad_url": "/two.step"})
    assert failed["reason"] == "blocked" and failed["status"] == 403 and failed["op"] == "acquire"


def test_native_login_hooks_remain_available(vendor):
    assert vendor.NATIVE_CHROME_LOGIN and vendor.ADAPTER.NATIVE_CHROME_LOGIN
    assert vendor.is_logged_in("", vendor.POST_LOGIN_URL)
    assert not vendor.is_logged_in("Customer Login", vendor.LOGIN_URL)
