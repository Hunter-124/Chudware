"""Offline Bosch Rexroth rendered-search and download fixtures."""
from __future__ import annotations

import pytest

from services.browser import adapters, session
from services.browser.adapters import boschrexroth as bosch


# item-headline comes from the previously recorded rendered search shape;
# container/metadata are synthetic fixtures for result-type and scoping coverage.
SEARCH = """
<div class="search-result-item" data-sku="R1623" data-mpn="R1623" data-price="89.10" data-currency="EUR" data-stock="4">
 <img src="/images/runner.jpg" alt="Runner block">
 <a href="/en/us/product/runner-block/" class="item-headline">Ball Rail Runner Block</a>
</div>
<div class="search-result-item"><img src="/brand-logo.svg" alt="logo">
 <a class="item-headline" href="/en/us/category/linear-motion/">Linear Motion</a></div>
<div class="search-result-item"><a class="item-headline" href="/en/us/product/valves/">
  4WE Series</a></div>
<div class="search-result-item"><a href="/en/us/media-details/guide" class="item-headline">
  Hydraulic Datasheet</a></div>
<div class="search-result-item"><a href="/en/us/company/about/" class="item-headline">About Us</a></div>
<div class="search-result-item"><a href="https://[broken" class="item-headline">Broken link</a></div>
"""
STEP = b"ISO-10303-21;\nHEADER;\nENDSEC;\nDATA;\nENDSEC;\nEND-ISO-10303-21;\n"
PDF = b"%PDF-1.7\nfixture\n%%EOF\n"


@pytest.fixture
def destination(monkeypatch, tmp_path):
    monkeypatch.setattr(adapters.paths, "BROWSER_DOWNLOADS", tmp_path)
    return tmp_path


def test_search_classifies_results_and_keeps_metadata(monkeypatch):
    calls = []
    monkeypatch.setattr(session, "browser_fetch", lambda url, site: (
        calls.append(url) or {"ok": True, "content": SEARCH}))
    hits = bosch.ADAPTER.search("rail & 3/8 #1")
    assert "?q=rail+%26+3%2F8+%231" in calls[0]
    assert [hit["kind"] for hit in hits] == ["product", "category", "series", "document", "page"]
    first = hits[0]
    assert first["title"] == "Ball Rail Runner Block"
    assert first["url"] == "https://www.boschrexroth.com/en/us/product/runner-block/"
    assert first["thumbnail"] == "https://www.boschrexroth.com/images/runner.jpg"
    assert (first["sku"], first["mpn"], first["manufacturer"]) == ("R1623", "R1623", "Bosch Rexroth")
    assert (first["price"], first["currency"], first["stock"]) == (89.10, "EUR", 4)
    assert hits[1]["thumbnail"] is None and hits[1]["price"] is None
    assert hits[3]["model"]["available"] is None


def test_jsonld_fallback_keeps_product_fields(monkeypatch):
    content = '''<script type="application/ld+json">{"@type":"Product", "name":"Runner",
      "url":"/en/us/product/runner", "sku":"R1", "mpn":"M1", "image":"/img/runner.jpg",
      "brand":{"name":"Bosch Rexroth"}, "offers":{"price":"32.75", "priceCurrency":"USD"}}</script>'''
    monkeypatch.setattr(session, "browser_fetch", lambda *_: {"ok": True, "content": content})
    hit = bosch.ADAPTER.search("runner")[0]
    assert hit["kind"] == "product" and hit["sku"] == "R1" and hit["mpn"] == "M1"
    assert hit["price"] == 32.75 and hit["currency"] == "USD"
    assert hit["thumbnail"] == bosch.BASE_URL + "/img/runner.jpg"


@pytest.mark.parametrize(("content", "reason"), [
    ("<html>unrecognized</html>", "parse_failed"),
    ("<title>Access Denied</title>", "blocked"),
    ("<div>Verify you are human</div>", "captcha"),
    ("<div>Log in to download</div>", "unsupported"),
])
def test_search_failures_are_not_disguised_as_login(monkeypatch, content, reason):
    monkeypatch.setattr(session, "browser_fetch", lambda *_: {"ok": True, "content": content})
    result = bosch.ADAPTER.search("rail")[0]
    assert result["reason"] == reason and result["site"] == "boschrexroth" and result["op"] == "search"
    if reason == "unsupported":
        assert result["ask_user"] is None


def test_acquire_validates_step_and_preserves_prior_file(monkeypatch, destination):
    monkeypatch.setattr(session, "browser_fetch", lambda *_: {"ok": True, "content":
        '<a href="../files/runner.step?config=R1">STEP</a>'})
    bodies = [STEP]
    def download(url, site, path):
        bodies_url.append(url)
        path.write_bytes(bodies[0])
        return {"ok": True, "dest": str(path)}
    bodies_url = []
    monkeypatch.setattr(session, "browser_download", download)
    hit = {"url": bosch.BASE_URL + "/en/us/product/runner/", "kind": "product"}
    result = bosch.ADAPTER.acquire(hit)
    assert result["ok"] and result["artifact_kind"] == "cad" and result["format"] == "step"
    assert bodies_url == [bosch.BASE_URL + "/en/us/product/files/runner.step?config=R1"]
    target = destination / "boschrexroth" / "runner.step"
    assert target.read_bytes() == STEP
    bodies[0] = b"%PDF-1.7 not CAD"
    assert bosch.ADAPTER.acquire(hit)["reason"] == "parse_failed"
    assert target.read_bytes() == STEP


def test_document_and_ambiguous_links_are_not_geometry(monkeypatch, destination):
    page = ['<a href="/media/spec.pdf">Datasheet</a>']
    monkeypatch.setattr(session, "browser_fetch", lambda *_: {"ok": True, "content": page[0]})
    calls = []
    def download(url, site, path):
        calls.append(url)
        path.write_bytes(PDF)
        return {"ok": True, "dest": str(path)}
    monkeypatch.setattr(session, "browser_download", download)
    hit = {"url": bosch.BASE_URL + "/en/us/media-details/guide", "kind": "document"}
    out = bosch.ADAPTER.acquire(hit)
    assert out["ok"] and out["artifact_kind"] == "document" and out["format"] == "pdf"
    assert (destination / "boschrexroth" / "spec.pdf").read_bytes() == PDF
    assert bosch.ADAPTER.acquire({**hit, "format": "step"})["reason"] == "unsupported"
    page[0] = '<a href="/cad/one.step">One</a><a href="/cad/two.step">Two</a>'
    before = len(calls)
    out = bosch.ADAPTER.acquire({"url": bosch.BASE_URL + "/en/us/product/family", "kind": "product"})
    assert out["reason"] == "unsupported" and len(calls) == before


def test_transport_failure_and_empty(monkeypatch):
    monkeypatch.setattr(session, "browser_fetch", lambda *_: {"ok": True, "content": "0 results"})
    assert bosch.ADAPTER.search("nothing") == []
    monkeypatch.setattr(session, "browser_fetch", lambda *_: {
        "ok": False, "reason": "timeout", "status": 504, "hint": "deadline"})
    out = bosch.ADAPTER.search("rail")[0]
    assert out["reason"] == "timeout" and out["status"] == 504 and out["hint"] == "deadline"
