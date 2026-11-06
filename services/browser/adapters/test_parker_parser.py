"""Offline Parker card and artifact fixtures; no vendor requests."""
from __future__ import annotations

import zipfile

import pytest

from services.browser import session
from services.browser import adapters
from services.browser.adapters import parker


SEARCH = """
<div class="product-card" data-sku="D1VW-001" data-mpn="D1VW-001" data-stock="12" data-currency="USD">
  <img data-src="/assets/d1vw.jpg" alt="Valve">
  <a href="/us/en/product/d1vw" class="nav-link-text">
    <div class="SelectorContainerInner-1">Parker D1VW Valve</div>
  </a><span class="price">$125.50</span><a href="/cad/D1VW-001.step">STEP</a>
</div>
<div class="product-card">
  <img src="/assets/logo.svg" alt="Parker logo">
  <a class="nav-link-text" href="/us/en/product/family">
    <div class="SelectorContainerInner-1">D3W Series</div>
  </a>
</div>
"""
STEP = b"ISO-10303-21;\nHEADER;\nENDSEC;\nDATA;\nENDSEC;\nEND-ISO-10303-21;\n"
PDF = b"%PDF-1.7\nfixture\n%%EOF\n"


@pytest.fixture
def destination(monkeypatch, tmp_path):
    monkeypatch.setattr(adapters.paths, "BROWSER_DOWNLOADS", tmp_path)
    return tmp_path


def test_search_cards_encode_query_and_keep_scoped_metadata(monkeypatch):
    calls = []
    monkeypatch.setattr(session, "browser_fetch", lambda url, site: (
        calls.append((url, site)) or {"ok": True, "content": SEARCH}))
    hits = parker.ADAPTER.search("valve & #1 / steel")
    assert "searchbox=valve+%26+%231+%2F+steel" in calls[0][0]
    assert calls[0][1] == "parker"
    assert [hit["kind"] for hit in hits] == ["product", "series"]
    first, second = hits
    assert first["url"] == "https://www.parker.com/us/en/product/d1vw"
    assert first["sku"] == first["mpn"] == "D1VW-001"
    assert first["price"] == 125.50 and first["currency"] == "USD" and first["stock"] == 12
    assert first["manufacturer"] == "Parker"
    assert first["thumbnail"] == "https://www.parker.com/assets/d1vw.jpg"
    assert second["thumbnail"] is None and second["price"] is None and second["sku"] is None
    assert first["model"] == {"available": True, "formats": ["step"]}
    assert second["model"] == {"available": None, "formats": []}


def test_site_chrome_login_form_does_not_hide_results(monkeypatch):
    content = SEARCH + '<form><input type="password">Sign in to your account</form>'
    monkeypatch.setattr(session, "browser_fetch", lambda *_: {"ok": True, "content": content})
    hits = parker.ADAPTER.search("valve")
    assert len(hits) == 2 and hits[0]["kind"] == "product"


def test_jsonld_does_not_invent_sku_or_currency(monkeypatch):
    content = '''<script type="application/ld+json">{"@type":"Product", "name":"Valve",
      "url":"/us/en/product/valve", "mpn":"V-1", "image":"/media/valve.jpg",
      "offers":{"price":"$12.50"}}</script>'''
    monkeypatch.setattr(session, "browser_fetch", lambda *_: {"ok": True, "content": content})
    hit = parker.ADAPTER.search("valve")[0]
    assert hit["sku"] is None and hit["mpn"] == "V-1"
    assert hit["price"] == 12.50 and hit["currency"] is None
    assert hit["thumbnail"] == parker.BASE_URL + "/media/valve.jpg"


@pytest.mark.parametrize(("content", "reason"), [
    ("<html>unrecognized markup</html>", "parse_failed"),
    ('<script>const emptyMessage = "No results";</script><div>app shell</div>', "parse_failed"),
    ("<title>Access Denied</title>", "blocked"),
    ("<div>Verify you are human</div>", "captcha"),
    ("<div>Captcha required</div>", "captcha"),
    ("<div>Log in to download</div>", "login_required"),
])
def test_search_failures_are_distinct(monkeypatch, content, reason):
    monkeypatch.setattr(session, "browser_fetch", lambda *_: {"ok": True, "content": content})
    out = parker.ADAPTER.search("valve")[0]
    assert out["ok"] is False and out["reason"] == reason
    assert out["site"] == "parker" and out["op"] == "search" and out["url"]


def test_search_empty_and_transport_failure(monkeypatch):
    monkeypatch.setattr(session, "browser_fetch", lambda *_: {"ok": True, "content": "No results found"})
    assert parker.ADAPTER.search("nothing") == []
    monkeypatch.setattr(session, "browser_fetch", lambda *_: {
        "ok": False, "reason": "rate_limited", "status": 429, "retry_after_s": 12})
    failure = parker.ADAPTER.search("x")[0]
    assert failure["reason"] == "rate_limited" and failure["status"] == 429
    assert failure["retry_after_s"] == 12 and failure["op"] == "search"


def test_selected_step_validated_and_invalid_does_not_replace(monkeypatch, destination):
    monkeypatch.setattr(session, "browser_fetch", lambda *_: {
        "ok": True, "content": '<a href="/cad/D1.step?token=1">STEP</a>'})
    body = [STEP]
    def download(url, site, path):
        path.write_bytes(body[0])
        return {"ok": True, "dest": str(path), "session_profile_used": site}
    monkeypatch.setattr(session, "browser_download", download)
    hit = {"url": parker.BASE_URL + "/us/en/product/d1", "title": "D1", "kind": "product"}
    result = parker.ADAPTER.acquire(hit)
    target = destination / "parker" / "D1.step"
    assert result["ok"] and result["artifact_kind"] == "cad" and result["format"] == "step"
    assert result["artifact_path"] == str(target) and target.read_bytes() == STEP
    body[0] = b"<html>expired login</html>"
    failure = parker.ADAPTER.acquire(hit)
    assert failure["reason"] == "parse_failed" and failure["op"] == "acquire"
    assert target.read_bytes() == STEP


def test_pdf_and_zip_are_not_geometry(monkeypatch, destination):
    links = ['<a href="/files/spec.pdf">Datasheet</a>']
    monkeypatch.setattr(session, "browser_fetch", lambda *_: {"ok": True, "content": links[0]})
    def download(url, site, path):
        if str(path).endswith(".pdf"):
            path.write_bytes(PDF)
        else:
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("A.step", STEP)
                archive.writestr("B.step", STEP)
        return {"ok": True, "dest": str(path)}
    monkeypatch.setattr(session, "browser_download", download)
    hit = {"url": parker.BASE_URL + "/product/one", "kind": "product"}
    pdf = parker.ADAPTER.acquire(hit)
    assert pdf["ok"] and pdf["artifact_kind"] == "document" and pdf["format"] == "pdf"
    assert (destination / "parker" / "spec.pdf").read_bytes() == PDF
    links[0] = '<a href="/files/configurations.zip">CAD</a>'
    archive = parker.ADAPTER.acquire(hit)
    assert archive["ok"] and archive["artifact_kind"] == "archive" and archive["format"] == "zip"


def test_multiple_downloads_require_explicit_selection(monkeypatch, destination):
    monkeypatch.setattr(session, "browser_fetch", lambda *_: {"ok": True, "content": (
        '<a href="/cad/A.step">A</a><a href="/cad/B.step">B</a>')})
    calls = []
    def download(url, site, path):
        calls.append(url)
        path.write_bytes(STEP)
        return {"ok": True, "dest": str(path)}
    monkeypatch.setattr(session, "browser_download", download)
    hit = {"url": parker.BASE_URL + "/product/family", "kind": "series", "format": "step"}
    assert parker.ADAPTER.acquire(hit)["reason"] == "unsupported" and not calls
    assert parker.ADAPTER.acquire({**hit, "sku": "C"})["reason"] == "unsupported" and not calls
    out = parker.ADAPTER.acquire({**hit, "download_url": "/cad/B.step"})
    assert out["ok"] and calls == [parker.BASE_URL + "/cad/B.step"]
    calls.clear()
    monkeypatch.setattr(session, "browser_fetch", lambda *_: {
        "ok": True, "content": '<a href="/cad/A.step">A</a>'})
    product = {"url": parker.BASE_URL + "/product/valves", "kind": "product", "sku": "B"}
    assert parker.ADAPTER.acquire(product)["reason"] == "unsupported" and not calls
