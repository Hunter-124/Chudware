"""Offline Thomson result-card and artifact fixtures."""
from __future__ import annotations

import pytest

from services.browser import adapters, session
from services.browser.adapters import thomson


# The title-anchor shape is from the recorded rendered vendor result; outer
# cards and commerce metadata are synthetic association fixtures.
SEARCH = """
<div class="product-box" data-sku="PC-24-100" data-mpn="PC-24-100" data-price="320.25" data-currency="USD">
 <div class="image"><img data-src="/media/actuator.jpg" alt="Actuator"></div>
 <a href="/en/product/pc-24-100?config=24v&amp;stroke=100" class="product-box__title">PC Actuator</a>
 <span class="stock">6</span>
</div>
<div class="product-box"><img src="/media/logo.svg" alt="logo">
 <a class="product-box__title" href="/en/product/pc-series">PC-Series Actuator</a>
</div>
"""
STEP = b"ISO-10303-21;\nHEADER;\nENDSEC;\nDATA;\nENDSEC;\nEND-ISO-10303-21;\n"
PDF = b"%PDF-1.7\nfixture\n%%EOF\n"


@pytest.fixture
def destination(monkeypatch, tmp_path):
    monkeypatch.setattr(adapters.paths, "BROWSER_DOWNLOADS", tmp_path)
    return tmp_path


def test_search_preserves_product_configuration_and_family(monkeypatch):
    calls = []
    monkeypatch.setattr(session, "browser_fetch", lambda url, site: (
        calls.append((url, site)) or {"ok": True, "content": SEARCH}))
    hits = thomson.ADAPTER.search("linear & 24v/#")
    assert "?q=linear+%26+24v%2F%23" in calls[0][0] and calls[0][1] == "thomson"
    assert [hit["kind"] for hit in hits] == ["product", "series"]
    first, second = hits
    assert first["url"] == "https://www.thomsonlinear.com/en/product/pc-24-100?config=24v&stroke=100"
    assert first["thumbnail"] == "https://www.thomsonlinear.com/media/actuator.jpg"
    assert (first["sku"], first["mpn"], first["price"], first["currency"], first["stock"]) == (
        "PC-24-100", "PC-24-100", 320.25, "USD", 6)
    assert first["manufacturer"] == "Thomson"
    assert second["thumbnail"] is None and second["price"] is None and second["sku"] is None


@pytest.mark.parametrize(("content", "reason"), [
    ("<html>app shell</html>", "parse_failed"),
    ("<title>Access Denied</title>", "blocked"),
    ("<div>Verify you are human</div>", "captcha"),
    ("<div>Please sign in to download</div>", "login_required"),
])
def test_search_distinct_failure_types(monkeypatch, content, reason):
    monkeypatch.setattr(session, "browser_fetch", lambda *_: {"ok": True, "content": content})
    out = thomson.ADAPTER.search("actuator")[0]
    assert out["reason"] == reason and out["site"] == "thomson" and out["op"] == "search"
    if reason == "login_required":
        assert out["ask_user"] is not None


def test_no_results_and_transport_details(monkeypatch):
    monkeypatch.setattr(session, "browser_fetch", lambda *_: {"ok": True, "content": "No products found"})
    assert thomson.ADAPTER.search("nothing") == []
    monkeypatch.setattr(session, "browser_fetch", lambda *_: {
        "ok": False, "reason": "blocked", "status": 403, "retry_after_s": 10})
    out = thomson.ADAPTER.search("actuator")[0]
    assert out["reason"] == "blocked" and out["status"] == 403 and out["retry_after_s"] == 10


def test_step_chosen_over_pdf_and_validated(monkeypatch, destination):
    monkeypatch.setattr(session, "browser_fetch", lambda *_: {"ok": True, "content": (
        '<a href="/files/spec.pdf">Datasheet</a><a href="/cad/pc.step?config=24v">CAD</a>')})
    urls = []
    bodies = [STEP]
    def download(url, site, path):
        urls.append(url)
        path.write_bytes(bodies[0])
        return {"ok": True, "dest": str(path)}
    monkeypatch.setattr(session, "browser_download", download)
    hit = {"url": thomson.BASE_URL + "/en/product/pc-24-100", "kind": "product"}
    out = thomson.ADAPTER.acquire(hit)
    assert out["ok"] and out["artifact_kind"] == "cad" and out["format"] == "step"
    assert urls == [thomson.BASE_URL + "/cad/pc.step?config=24v"]
    target = destination / "thomson" / "pc.step"
    assert target.read_bytes() == STEP
    bodies[0] = b"<html>login page</html>"
    assert thomson.ADAPTER.acquire(hit)["reason"] == "parse_failed"
    assert target.read_bytes() == STEP


def test_missing_requested_format_and_ambiguous_family_do_not_fall_back(monkeypatch, destination):
    monkeypatch.setattr(session, "browser_fetch", lambda *_: {"ok": True, "content": (
        '<a href="/files/spec.pdf">Datasheet</a><a href="/cad/A.step">A</a><a href="/cad/B.step">B</a>')})
    calls = []
    monkeypatch.setattr(session, "browser_download", lambda *args: calls.append(args))
    hit = {"url": thomson.BASE_URL + "/en/product/pc-series", "kind": "series"}
    assert thomson.ADAPTER.acquire({**hit, "format": "iges"})["reason"] == "unsupported"
    assert thomson.ADAPTER.acquire({**hit, "format": "step"})["reason"] == "unsupported"
    assert calls == []


def test_pdf_remains_a_document(monkeypatch, destination):
    monkeypatch.setattr(session, "browser_fetch", lambda *_: {"ok": True, "content":
        '<a href="/datasheets/pc.pdf">Datasheet</a>'})
    def download(url, site, path):
        path.write_bytes(PDF)
        return {"ok": True, "dest": str(path)}
    monkeypatch.setattr(session, "browser_download", download)
    out = thomson.ADAPTER.acquire({"url": thomson.BASE_URL + "/en/product/pc-series", "kind": "series"})
    assert out["ok"] and out["artifact_kind"] == "document" and out["format"] == "pdf"
    assert (destination / "thomson" / "pc.pdf").read_bytes() == PDF
