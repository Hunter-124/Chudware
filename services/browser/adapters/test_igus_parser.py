"""Offline coverage for igus search and selected static CAD downloads."""
from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest

from services.browser import adapters, session
from services.browser.adapters import igus


# Product fields and the two article counts are from the live Next.js shape.
PRODUCTS = [
    {"title": "drylin® N low-profile linear guide installation size 40 sliding element NW iglidur® J",
     "productUrl": "https://www.igus.com/product/NW-22-40?artnr=NW-22-40",
     "sku": "NW-22-40", "articleCount": 1,
     "imageUrl": "https://igus.widen.net/content/fibkrbscsc/jpeg/DRL_PROD_NW-22-40_CN_1.jpeg"},
    {"title": "drylin® W linear guide, complete system with digital measuring system",
     "productUrl": "/product/1049?artNr=WKM2-10-80-15-01-L",
     "sku": "drylin_WKM_10_20", "articleCount": 4, "imageUrl": "/images/wkm.jpg"},
]
STEP = b"ISO-10303-21;\nHEADER;\nENDSEC;\nEND-ISO-10303-21;\n"


def search_page(entries):
    data = {"props": {"pageProps": {"initialData": {"products": {"data": entries}}}}}
    return "<script type='application/json' id='__NEXT_DATA__'>" + json.dumps(data) + "</script>"


def page(monkeypatch, content, **fields):
    monkeypatch.setattr(session, "browser_fetch", lambda *_: {"ok": True, "content": content, **fields})


def download(monkeypatch, data, seen):
    def save(url, site, dest):
        seen.append((url, site, dest))
        dest.write_bytes(data)
        return {"ok": True, "dest": str(dest), "session_profile_used": "igus-profile"}
    monkeypatch.setattr(session, "browser_download", save)


@pytest.fixture
def downloads(monkeypatch, tmp_path):
    monkeypatch.setattr(adapters.paths, "BROWSER_DOWNLOADS", tmp_path)
    return tmp_path


def test_search_live_shape_preserves_identity_images_and_kind(monkeypatch):
    seen = []
    def fetch(url, site):
        seen.append((url, site))
        return {"ok": True, "content": search_page(PRODUCTS)}
    monkeypatch.setattr(session, "browser_fetch", fetch)

    product, series = igus.ADAPTER.search("linear & guide/#")

    assert seen == [("https://www.igus.com/search?q=linear+%26+guide%2F%23", "igus")]
    assert product["kind"] == "product" and product["sku"] == "NW-22-40"
    assert product["url"] == PRODUCTS[0]["productUrl"]
    assert product["thumbnail"] == PRODUCTS[0]["imageUrl"]
    assert product["manufacturer"] == "igus" and product["price"] is None
    assert product["model"] == {"available": None, "formats": []}
    assert series["kind"] == "series" and series["sku"] == "drylin_WKM_10_20"
    assert series["url"] == "https://www.igus.com/product/1049?artNr=WKM2-10-80-15-01-L"
    assert series["thumbnail"] == "https://www.igus.com/images/wkm.jpg"


def test_search_carries_present_commerce_and_rejects_placeholder(monkeypatch):
    entry = {**PRODUCTS[0], "price": {"value": "1,234.50", "currency": "EUR"},
             "mpn": "NW-22-40", "stock": "12", "material": "polymer",
             "imageUrl": "/assets/logo.svg", "modelFormats": ["step"], "modelAvailable": True}
    page(monkeypatch, search_page([entry]))
    hit = igus.ADAPTER.search("NW-22-40")[0]
    assert (hit["price"], hit["currency"], hit["stock"]) == (1234.5, "EUR", 12)
    assert hit["mpn"] == "NW-22-40" and hit["material"] == "polymer"
    assert hit["thumbnail"] is None
    assert hit["model"] == {"available": True, "formats": ["step"]}


def test_ordinary_login_navigation_is_not_a_wall(monkeypatch):
    page(monkeypatch, "<nav>Log in to igus</nav>" + search_page(PRODUCTS))
    assert len(igus.ADAPTER.search("guide")) == 2


@pytest.mark.parametrize("content", ["<html></html>", "<script id='__NEXT_DATA__'>{broken}</script>",
                                    search_page([{"title": "incomplete"}])])
def test_search_unrecognized_content_is_parse_failure(monkeypatch, content):
    page(monkeypatch, content)
    failure = igus.ADAPTER.search("bearing")[0]
    assert (failure["site"], failure["op"], failure["reason"]) == ("igus", "search", "parse_failed")


def test_search_distinguishes_empty_results_from_failures(monkeypatch):
    page(monkeypatch, search_page([]))
    assert igus.ADAPTER.search("no match") == []
    monkeypatch.setattr(session, "browser_fetch", lambda *_: {
        "ok": False, "reason": "rate_limited", "status": 429, "retry_after_s": 45})
    failure = igus.ADAPTER.search("bearing")[0]
    assert (failure["reason"], failure["status"], failure["retry_after_s"]) == ("rate_limited", 429, 45)
    assert failure["site"] == "igus" and failure["op"] == "search"


@pytest.mark.parametrize(("content", "reason"), [
    ("<h1>Access Denied</h1>", "blocked"),
    ("<div>verify you are human</div>", "captcha"),
    ('<form id="loginForm"></form>', "login_required"),
])
def test_search_wall_reasons(monkeypatch, content, reason):
    page(monkeypatch, content)
    assert igus.ADAPTER.search("guide")[0]["reason"] == reason


def test_acquire_valid_step_is_typed_and_staged(monkeypatch, downloads):
    page(monkeypatch, '<a href="/cad/NW-22-40.step?download=1">CAD</a>')
    seen = []
    download(monkeypatch, STEP, seen)
    out = igus.ADAPTER.acquire({"url": PRODUCTS[0]["productUrl"], "kind": "product"})
    assert out["ok"] and out["artifact_kind"] == "cad" and out["format"] == "step"
    assert out["session_profile_used"] == "igus-profile"
    assert Path(out["artifact_path"]).read_bytes() == STEP
    assert Path(out["artifact_path"]) == downloads / "igus" / "NW-22-40.step"
    assert seen[0][0] == "https://www.igus.com/cad/NW-22-40.step?download=1"
    assert seen[0][2] != Path(out["artifact_path"])


@pytest.mark.parametrize("invalid", [b"<html>sign in</html>", b"%PDF-1.7 document", b""])
def test_acquire_invalid_cad_preserves_existing_file(monkeypatch, downloads, invalid):
    existing = downloads / "igus" / "part.step"
    existing.parent.mkdir()
    existing.write_bytes(STEP)
    page(monkeypatch, '<a href="/part.step">CAD</a>')
    download(monkeypatch, invalid, [])
    out = igus.ADAPTER.acquire({"url": "https://www.igus.com/product/example"})
    assert out["ok"] is False and out["reason"] == "parse_failed"
    assert existing.read_bytes() == STEP


def test_archive_is_not_silently_imported_or_chosen(monkeypatch, downloads):
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("first.step", STEP)
        zf.writestr("other-configuration.step", STEP)
    page(monkeypatch, '<a href="/models.zip">CAD</a>')
    download(monkeypatch, archive.getvalue(), [])
    out = igus.ADAPTER.acquire({"url": "https://www.igus.com/product/example"})
    assert out["ok"] and (out["artifact_kind"], out["format"]) == ("archive", "zip")
    assert Path(out["artifact_path"]).read_bytes() == archive.getvalue()


def test_acquire_requires_explicit_configuration_and_link(monkeypatch, downloads):
    page(monkeypatch, '<a href="/a.step">A</a><a href="/b.step">B</a>')
    monkeypatch.setattr(session, "browser_download", lambda *_: pytest.fail("ambiguous download"))
    url = "https://www.igus.com/product/example"
    assert igus.ADAPTER.acquire({"url": url, "kind": "series"})["reason"] == "unsupported"
    assert igus.ADAPTER.acquire({"url": url, "requested_format": "step"})["reason"] == "unsupported"
    assert igus.ADAPTER.acquire({"url": url, "download_url": "/other.step"})["reason"] == "not_found"
    assert igus.ADAPTER.acquire({"url": url, "download_url": "javascript:alert(1)"})["reason"] == "not_found"
    seen = []
    download(monkeypatch, STEP, seen)
    out = igus.ADAPTER.acquire({"url": url, "download_url": "/b.step"})
    assert out["ok"] and seen[0][0].endswith("/b.step")


def test_rejects_mismatched_format_and_invalid_archive(monkeypatch, downloads):
    url = "https://www.igus.com/product/example"
    assert igus.ADAPTER.acquire({})["reason"] == "not_found"
    page(monkeypatch, '<a href="/part.step">CAD</a>')
    monkeypatch.setattr(session, "browser_download", lambda *_: pytest.fail("wrong format"))
    assert igus.ADAPTER.acquire({"url": url, "requested_format": "iges"})["reason"] == "unsupported"
    existing = downloads / "igus" / "models.zip"
    existing.parent.mkdir()
    existing.write_bytes(b"previous archive")
    page(monkeypatch, '<a href="/models.zip">CAD</a>')
    download(monkeypatch, b"<html>not a zip</html>", [])
    assert igus.ADAPTER.acquire({"url": url})["reason"] == "parse_failed"
    assert existing.read_bytes() == b"previous archive"


def test_configurator_is_not_mistaken_for_login(monkeypatch):
    page(monkeypatch, "<p>Configure this part</p>")
    out = igus.ADAPTER.acquire({"url": "https://www.igus.com/product/example"})
    assert out["reason"] == "unsupported" and out["ask_user"] is None


def test_download_failure_keeps_transport_details(monkeypatch, downloads):
    page(monkeypatch, '<a href="/part.step">CAD</a>')
    monkeypatch.setattr(session, "browser_download", lambda *_: {
        "ok": False, "reason": "timeout", "hint": "timed out", "status": 504})
    out = igus.ADAPTER.acquire({"url": "https://www.igus.com/product/example"})
    assert (out["site"], out["op"], out["reason"], out["status"]) == ("igus", "acquire", "timeout", 504)
