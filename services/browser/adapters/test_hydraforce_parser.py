"""HydraForce rendered-card fixtures, including live-observed symbol images."""
from __future__ import annotations

import pytest

from services.browser import adapters, session
from services.browser.adapters import hydraforce


# Search-card structure and first title/image follow the public page observed
# 2026-11-04; the additional cards exercise other possible result types.
SEARCH = """
<div class="row search-page-result">
  <div class="image-wrapper"><img src="https://www.hydraforce.com/globalassets/product-images/hsv10-28_sym_2023-11-22.svg"></div>
  <div class="result-details"><div class="row result-title-wrapper">
    <h4 class="mb-3 result-title">HSV10 28</h4>
    <h6 class="search-category"><span class="result-badge">Products</span></h6></div>
    <p class="result-description">Solenoid cartridge valve</p>
    <div class="col-md-5 result-links"><a href="https://www.hydraforce.com/products/cartridge-valves/solenoid-valves/hsv10-28/?_t_id=abc&amp;_t_q=valve">See Details</a></div>
  </div>
</div>
<div class="row search-page-result" data-sku="SV-123" data-mpn="SV-123" data-price="42.50" data-currency="USD" data-stock="8">
 <img data-src="/globalassets/product-images/valve-photo.jpg" alt="Valve">
 <h4 class="result-title">SV-123</h4><span class="result-badge">Products</span>
 <div class="result-links"><a href="/products/valves/sv-123/?variant=24v&amp;_t_id=abc">Details</a></div>
</div>
<div class="row search-page-result"><h4 class="result-title">Solenoid Valves</h4>
 <span class="result-badge">Products</span>
 <div class="result-links"><a href="/products/cartridge-valves/solenoid-valves/">Details</a></div></div>
<div class="row search-page-result"><h4 class="result-title">Valve Service Guide</h4>
 <span class="result-badge">Documents</span>
 <div class="result-links"><a href="/resources/valve.pdf">Download</a></div></div>
<div class="row search-page-result"><h4 class="result-title">Broken link</h4>
 <div class="result-links"><a href="https://[broken">Details</a></div></div>
"""
STEP = b"ISO-10303-21;\nHEADER;\nENDSEC;\nDATA;\nENDSEC;\nEND-ISO-10303-21;\n"
PDF = b"%PDF-1.7\nfixture\n%%EOF\n"


@pytest.fixture
def destination(monkeypatch, tmp_path):
    monkeypatch.setattr(adapters.paths, "BROWSER_DOWNLOADS", tmp_path)
    return tmp_path


def test_search_distinguishes_family_category_documents_and_images(monkeypatch):
    calls = []
    monkeypatch.setattr(session, "browser_fetch", lambda url, site: (
        calls.append(url) or {"ok": True, "content": SEARCH}))
    hits = hydraforce.ADAPTER.search("solenoid & 24v/#")
    assert "?q=solenoid+%26+24v%2F%23" in calls[0]
    assert [hit["kind"] for hit in hits] == ["series", "series", "category", "document"]
    first, second = hits[:2]
    assert first["title"] == "HSV10 28"
    assert first["url"] == "https://www.hydraforce.com/products/cartridge-valves/solenoid-valves/hsv10-28/"
    assert first["thumbnail"] is None  # The real site serves a circuit symbol, not a photo.
    assert first["sku"] is None and first["price"] is None
    assert second["url"].endswith("/sv-123/?variant=24v")  # Keep configuration; discard tracking.
    assert second["thumbnail"] == "https://www.hydraforce.com/globalassets/product-images/valve-photo.jpg"
    assert (second["sku"], second["mpn"], second["price"], second["currency"], second["stock"]) == (
        "SV-123", "SV-123", 42.50, "USD", 8)
    assert all(hit["manufacturer"] == "HydraForce" for hit in hits)


@pytest.mark.parametrize(("content", "reason"), [
    ("<html>unrecognized</html>", "parse_failed"),
    ("<title>Access Denied</title>", "blocked"),
    ("<div>Verify you are human</div>", "captcha"),
    ("<div>Log in to download</div>", "unsupported"),
])
def test_search_reports_failures_without_fake_login(monkeypatch, content, reason):
    monkeypatch.setattr(session, "browser_fetch", lambda *_: {"ok": True, "content": content})
    failure = hydraforce.ADAPTER.search("valve")[0]
    assert failure["reason"] == reason and failure["site"] == "hydraforce" and failure["op"] == "search"
    if reason == "unsupported":
        assert failure["distributors_url"] == hydraforce.DISTRIBUTORS_URL
        assert failure["ask_user"] is None


def test_series_never_downloads_an_unspecified_configuration(monkeypatch, destination):
    monkeypatch.setattr(session, "browser_fetch", lambda *_: {"ok": True, "content": (
        '<a href="/cad/A.step">A</a><a href="/cad/B.step">B</a>')})
    calls = []
    def download(url, site, path):
        calls.append(url)
        path.write_bytes(STEP)
        return {"ok": True, "dest": str(path)}
    monkeypatch.setattr(session, "browser_download", download)
    hit = {"url": hydraforce.BASE_URL + "/products/valve/", "kind": "series", "format": "step"}
    assert hydraforce.ADAPTER.acquire(hit)["reason"] == "unsupported" and not calls
    assert hydraforce.ADAPTER.acquire({**hit, "sku": "missing"})["reason"] == "unsupported" and not calls
    out = hydraforce.ADAPTER.acquire({**hit, "sku": "B"})
    assert out["ok"] and out["artifact_kind"] == "cad" and out["format"] == "step"
    assert calls == [hydraforce.BASE_URL + "/cad/B.step"]
    assert (destination / "hydraforce" / "B.step").read_bytes() == STEP
    calls.clear()
    monkeypatch.setattr(session, "browser_fetch", lambda *_: {
        "ok": True, "content": '<a href="/cad/A-12V.step">A-12V</a>'})
    assert hydraforce.ADAPTER.acquire({**hit, "sku": "A"})["reason"] == "unsupported"
    assert not calls


def test_current_page_datasheet_is_document_not_related_part(monkeypatch, destination):
    monkeypatch.setattr(session, "browser_fetch", lambda *_: {"ok": True, "content": (
        '<a href="/files/current.pdf">Download PDF of Page</a>'
        '<div class="related-products"><a href="/files/other.pdf">Datasheet</a></div>')})
    def download(url, site, path):
        path.write_bytes(PDF)
        return {"ok": True, "dest": str(path)}
    monkeypatch.setattr(session, "browser_download", download)
    out = hydraforce.ADAPTER.acquire({"url": hydraforce.BASE_URL + "/products/hsv10-28/", "kind": "series"})
    assert out["ok"] and out["artifact_kind"] == "document" and out["format"] == "pdf"
    assert (destination / "hydraforce" / "current.pdf").read_bytes() == PDF


def test_malformed_pdf_and_transport_failure(monkeypatch, destination):
    monkeypatch.setattr(session, "browser_fetch", lambda *_: {
        "ok": True, "content": '<a href="/files/spec.pdf">Datasheet</a>'})
    def bad_download(url, site, path):
        path.write_bytes(b"<html>not a pdf</html>")
        return {"ok": True, "dest": str(path)}
    monkeypatch.setattr(session, "browser_download", bad_download)
    hit = {"url": hydraforce.BASE_URL + "/products/valve/", "kind": "series"}
    assert hydraforce.ADAPTER.acquire(hit)["reason"] == "parse_failed"
    monkeypatch.setattr(session, "browser_fetch", lambda *_: {
        "ok": False, "reason": "rate_limited", "status": 429, "retry_after_s": 30})
    failure = hydraforce.ADAPTER.acquire(hit)
    assert failure["reason"] == "rate_limited" and failure["status"] == 429 and failure["retry_after_s"] == 30
