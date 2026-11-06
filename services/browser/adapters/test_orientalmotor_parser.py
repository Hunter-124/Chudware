"""Deterministic Oriental Motor search and download fixtures (no network)."""
from __future__ import annotations

import io
import zipfile

import pytest


# Synthetic card shape: live search was blocked, so this is not a captured DOM.
CARDS = '''<article class="product-card" data-sku="PKP243D23A2" data-mpn="PKP243D23A2"
 data-price="149.50" data-currency="USD" data-stock-qty="4">
 <img class="logo" src="/assets/logo.png"><img data-src="/images/pkp243.jpg">
 <a href="/products/pkp243d23a2" class="product-title">PKP243D23A2 &amp; driver</a>
 <span class="manufacturer">Oriental Motor</span></article>
<article class="product-card"><img src="/images/az-series.jpg">
 <a class="product-title" href="/products/az-series">AZ Series Motors</a></article>'''
STEP = b"ISO-10303-21;\nHEADER;\nENDSEC;\nDATA;\nENDSEC;\nEND-ISO-10303-21;\n"
PRODUCT = "https://www.orientalmotor.com/products/pkp243d23a2"


@pytest.fixture
def vendor(monkeypatch, tmp_path):
    from services.browser.adapters import orientalmotor, paths
    monkeypatch.setattr(paths, "BROWSER_DOWNLOADS", tmp_path)
    return orientalmotor


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


def test_search_cards_preserve_metadata_and_images(vendor, monkeypatch):
    calls = _fetch(monkeypatch, vendor, CARDS)
    first, series = vendor.ADAPTER.search("PKP & 1/2#")
    assert calls == [("https://www.orientalmotor.com/search?q=PKP+%26+1%2F2%23", "orientalmotor")]
    assert first["title"] == "PKP243D23A2 & driver" and first["kind"] == "product"
    assert first["sku"] == first["mpn"] == "PKP243D23A2"
    assert first["url"] == PRODUCT and first["manufacturer"] == "Oriental Motor"
    assert first["price"] == 149.5 and first["currency"] == "USD" and first["stock"] == 4
    assert first["thumbnail"] == "https://www.orientalmotor.com/images/pkp243.jpg"
    assert first["model"] == {"available": None, "formats": []}
    assert series["kind"] == "series" and series["sku"] is None and series["price"] is None
    assert series["thumbnail"] == "https://www.orientalmotor.com/images/az-series.jpg"


def test_search_deduplicates_and_legacy_anchor_has_no_borrowed_preview(vendor, monkeypatch):
    _fetch(monkeypatch, vendor, CARDS + CARDS)
    assert len(vendor.ADAPTER.search("motor")) == 2
    _fetch(monkeypatch, vendor, '<img src="/logo.png"><a class="product-title" href="/products/motor">Motor</a>')
    hit, = vendor.ADAPTER.search("motor")
    assert hit["kind"] == "product" and hit["sku"] is None and hit["thumbnail"] is None


def test_search_distinguishes_empty_page_errors_and_walls(vendor, monkeypatch):
    _fetch(monkeypatch, vendor, "No results found")
    assert vendor.ADAPTER.search("missing") == []
    _fetch(monkeypatch, vendor, "<html>Changed markup</html>")
    assert vendor.ADAPTER.search("motor")[0]["reason"] == "parse_failed"
    _fetch(monkeypatch, vendor, "<h1>Access Denied</h1>")
    assert vendor.ADAPTER.search("motor")[0]["reason"] == "blocked"
    _fetch(monkeypatch, vendor, "<h1>Verify you are human</h1>")
    assert vendor.ADAPTER.search("motor")[0]["reason"] == "captcha"
    _fetch(monkeypatch, vendor, "<h1>Log in to Oriental Motor</h1>")
    wall = vendor.ADAPTER.search("motor")[0]
    assert wall["reason"] == "login_required" and wall["ask_user"] and wall["site"] == "orientalmotor"


def test_search_preserves_transport_context(vendor, monkeypatch):
    monkeypatch.setattr(vendor._session, "browser_fetch", lambda *_: {
        "ok": False, "reason": "rate_limited", "status": 429, "retry_after_s": 12})
    failure, = vendor.ADAPTER.search("motor")
    assert failure["site"] == "orientalmotor" and failure["op"] == "search"
    assert failure["reason"] == "rate_limited" and failure["status"] == 429 and failure["retry_after_s"] == 12


def test_acquire_valid_step_and_relative_url(vendor, monkeypatch):
    _fetch(monkeypatch, vendor, '<a href="../files/PKP243D23A2.step">STEP</a>')
    calls = _download(monkeypatch, vendor, STEP)
    out = vendor.ADAPTER.acquire({"url": PRODUCT, "sku": "PKP243D23A2"})
    assert out["ok"] and out["artifact_kind"] == "cad" and out["format"] == "step"
    assert out["sku"] == "PKP243D23A2" and out["session_profile_used"] == "fixture"
    assert calls == ["https://www.orientalmotor.com/files/PKP243D23A2.step"]


def test_acquire_datasheet_is_not_geometry(vendor, monkeypatch):
    _fetch(monkeypatch, vendor, '<a href="/files/pkp-data-sheet.pdf">Data sheet</a>')
    _download(monkeypatch, vendor, b"%PDF-1.7\nfixture\n%%EOF")
    out = vendor.ADAPTER.acquire({"url": PRODUCT})
    assert out["ok"] and out["artifact_kind"] == "document" and out["format"] == "pdf"
    _download(monkeypatch, vendor, b"<html>login</html>")
    assert vendor.ADAPTER.acquire({"url": PRODUCT})["reason"] == "parse_failed"


@pytest.mark.parametrize("data", [b"", b"<html>denied</html>", b"%PDF-1.7\nnot a model"])
def test_acquire_rejects_unusable_cad(vendor, monkeypatch, data):
    _fetch(monkeypatch, vendor, '<a href="/files/model.step">STEP</a>')
    _download(monkeypatch, vendor, data)
    out = vendor.ADAPTER.acquire({"url": PRODUCT})
    assert not out["ok"] and out["reason"] == "parse_failed"


def test_acquire_only_unambiguous_archive_as_cad(vendor, monkeypatch):
    _fetch(monkeypatch, vendor, '<a href="/files/series.zip">CAD</a>')
    _download(monkeypatch, vendor, _zip({"motor.step": STEP, "readme.txt": b"model"}))
    one = vendor.ADAPTER.acquire({"url": PRODUCT})
    assert one["ok"] and one["artifact_kind"] == "cad" and one["archive_member"] == "motor.step"
    _download(monkeypatch, vendor, _zip({"PKP.step": STEP, "AZ.step": STEP}))
    many = vendor.ADAPTER.acquire({"url": PRODUCT, "sku": "PKP"})
    assert many["ok"] and many["artifact_kind"] == "archive" and many["format"] == "zip"
    _download(monkeypatch, vendor, _zip({"../escape.step": STEP}))
    assert vendor.ADAPTER.acquire({"url": PRODUCT})["reason"] == "parse_failed"


def test_acquire_requires_explicit_configuration_or_download(vendor, monkeypatch):
    _fetch(monkeypatch, vendor, '<a href="/files/a.step">A</a><a href="/files/b.step">B</a>')
    calls = _download(monkeypatch, vendor, STEP)
    assert vendor.ADAPTER.acquire({"url": PRODUCT, "kind": "series"})["reason"] == "unsupported"
    assert vendor.ADAPTER.acquire({"url": PRODUCT})["reason"] == "unsupported" and not calls
    assert vendor.ADAPTER.acquire({"url": PRODUCT, "format": "iges"})["reason"] == "not_found"
    out = vendor.ADAPTER.acquire({"url": PRODUCT, "cad_url": "/files/b.step"})
    assert out["ok"] and calls == ["https://www.orientalmotor.com/files/b.step"]


def test_acquire_propagates_download_failure_and_login(vendor, monkeypatch):
    _fetch(monkeypatch, vendor, '<a href="/files/a.step">A</a>')
    monkeypatch.setattr(vendor._session, "browser_download", lambda *_: {
        "ok": False, "reason": "blocked", "status": 403})
    failed = vendor.ADAPTER.acquire({"url": PRODUCT})
    assert failed["reason"] == "blocked" and failed["status"] == 403 and failed["op"] == "acquire"
    _fetch(monkeypatch, vendor, "<h1>Log in to Oriental Motor</h1>")
    failed = vendor.ADAPTER.acquire({"url": PRODUCT})
    assert failed["reason"] == "login_required" and failed["ask_user"]
