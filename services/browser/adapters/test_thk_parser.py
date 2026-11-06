"""Offline coverage for THK's rendered popfind cards and static downloads."""
from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

from services.browser import adapters, session
from services.browser.adapters import thk


# The _item/_thumbnail/_title structure is the rendered popfind widget shape.
CARDS = """
<div class="_item" data-part-number="HSR25A" data-mpn="HSR25A" data-price="125.50"
     data-currency="USD" data-stock="3" data-material="steel">
  <div class="_thumbnail"><a href="/product/hsr25a"><img src="/images/hsr.jpg"></a></div>
  <div class="_title"><a href="/product/hsr25a">HSR25A <em>LM Guide</em></a></div>
</div>
<div class="_item extra">
  <div class="_thumbnail"><img src="data:image/gif;base64,AA" data-src="//cdn.thk.com/guide.jpg"></div>
  <div class="_title"><a href="https://www.thk.com/us/en/products/lm_guide/">LM Guide</a></div>
</div>
<div class="_item"><div class="_title"><a href="/us/en/products/lm_guide/hsr/">HSR Series</a></div></div>
<div class="_item"><div class="_title"><a href="/us/en/journal/article/">Applications</a></div></div>
<div class="_item"><div class="_title"><a href="/catalog.pdf">Catalog</a></div></div>
<footer data-sku="UNRELATED" data-price="999"><img src="/logo.svg"></footer>
"""
STEP = b"ISO-10303-21;\nHEADER;\nENDSEC;\nEND-ISO-10303-21;\n"


def page(monkeypatch, content, **fields):
    monkeypatch.setattr(session, "browser_fetch", lambda *_: {"ok": True, "content": content, **fields})


def download(monkeypatch, data, seen):
    def save(url, site, dest):
        seen.append((url, site, dest))
        dest.write_bytes(data)
        return {"ok": True, "dest": str(dest), "session_profile_used": "thk-profile"}
    monkeypatch.setattr(session, "browser_download", save)


@pytest.fixture
def downloads(monkeypatch, tmp_path):
    monkeypatch.setattr(adapters.paths, "BROWSER_DOWNLOADS", tmp_path)
    return tmp_path


def test_search_card_metadata_kinds_and_encoded_query(monkeypatch):
    seen = []
    def fetch(url, site):
        seen.append((url, site))
        return {"ok": True, "content": CARDS}
    monkeypatch.setattr(session, "browser_fetch", fetch)
    product, category, series, page_hit, document = thk.ADAPTER.search("HSR & 25/#")
    assert seen == [("https://www.thk.com/us/en/search/?q=HSR+%26+25%2F%23", "thk")]
    assert [hit["kind"] for hit in (product, category, series, page_hit, document)] == [
        "product", "category", "series", "page", "document"]
    assert product["title"] == "HSR25A LM Guide" and product["sku"] == product["mpn"] == "HSR25A"
    assert product["url"] == "https://www.thk.com/product/hsr25a"
    assert product["thumbnail"] == "https://www.thk.com/images/hsr.jpg"
    assert (product["price"], product["currency"], product["stock"], product["material"]) == (
        125.5, "USD", 3, "steel")
    assert product["manufacturer"] == "THK" and product["model"]["available"] is None
    assert category["thumbnail"] == "https://cdn.thk.com/guide.jpg"
    assert document["thumbnail"] is None and document["sku"] is None and document["price"] is None


def test_search_explicit_part_number_label_and_logo_filter(monkeypatch):
    page(monkeypatch, '''<div class="_item"><div class="_thumbnail"><img src="/images/logo.svg"></div>
      <div class="_title"><a href="/us/en/products/lm_guide/hsr/">HSR bearing</a></div>
      <p>Part No: HSR25A</p></div>''')
    hit = thk.ADAPTER.search("HSR25A")[0]
    assert hit["sku"] == "HSR25A" and hit["kind"] == "product"
    assert hit["thumbnail"] is None


def test_search_empty_and_unrendered_widget_are_distinct(monkeypatch):
    page(monkeypatch, '<div class="_status _nohits">No results</div>')
    assert thk.ADAPTER.search("none") == []
    page(monkeypatch, '<div class="no-results" hidden>No pages matching your search criteria</div>')
    assert thk.ADAPTER.search("guide")[0]["reason"] == "parse_failed"
    page(monkeypatch, '<div class="_item"><span>changed markup</span></div>')
    assert thk.ADAPTER.search("guide")[0]["reason"] == "parse_failed"


def test_search_propagates_transport_failure(monkeypatch):
    monkeypatch.setattr(session, "browser_fetch", lambda *_: {
        "ok": False, "reason": "rate_limited", "status": 429, "retry_after_s": 25})
    failure = thk.ADAPTER.search("guide")[0]
    assert (failure["site"], failure["op"], failure["reason"]) == ("thk", "search", "rate_limited")
    assert failure["status"] == 429 and failure["retry_after_s"] == 25


@pytest.mark.parametrize(("content", "reason"), [
    ("<title>Access Denied</title>", "blocked"),
    ("<div>verify you are human</div>", "captcha"),
    ('<form id="loginForm"></form>', "login_required"),
])
def test_wall_types(monkeypatch, content, reason):
    page(monkeypatch, content)
    assert thk.ADAPTER.search("guide")[0]["reason"] == reason
    assert thk.ADAPTER.acquire({"url": "https://www.thk.com/product/x"})["reason"] == reason


def test_acquire_valid_step_and_relative_link(monkeypatch, downloads):
    page(monkeypatch, '<a href="../files/HSR25A.stp?download=1">CAD</a>',
         final_url="https://www.thk.com/catalog/product/")
    seen = []
    download(monkeypatch, STEP, seen)
    out = thk.ADAPTER.acquire({"url": "https://www.thk.com/product/hsr25a", "kind": "product"})
    assert out["ok"] and (out["artifact_kind"], out["format"]) == ("cad", "step")
    assert out["session_profile_used"] == "thk-profile"
    assert seen[0][0] == "https://www.thk.com/catalog/files/HSR25A.stp?download=1"
    assert Path(out["artifact_path"]) == downloads / "thk" / "HSR25A.stp"
    assert Path(out["artifact_path"]).read_bytes() == STEP


@pytest.mark.parametrize("invalid", [b"<html>login</html>", b"%PDF-1.7 document", b""])
def test_invalid_download_does_not_overwrite_existing(monkeypatch, downloads, invalid):
    existing = downloads / "thk" / "part.step"
    existing.parent.mkdir()
    existing.write_bytes(STEP)
    page(monkeypatch, '<a href="/part.step">CAD</a>')
    download(monkeypatch, invalid, [])
    out = thk.ADAPTER.acquire({"url": "https://www.thk.com/product/x"})
    assert out["reason"] == "parse_failed" and existing.read_bytes() == STEP


def test_zip_stays_archive_and_never_selects_variant(monkeypatch, downloads):
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("HSR25A.step", STEP)
        zf.writestr("HSR30A.step", STEP)
    page(monkeypatch, '<a href="/models.zip">CAD</a>')
    download(monkeypatch, archive.getvalue(), [])
    out = thk.ADAPTER.acquire({"url": "https://www.thk.com/product/x"})
    assert out["ok"] and (out["artifact_kind"], out["format"]) == ("archive", "zip")
    assert Path(out["artifact_path"]).read_bytes() == archive.getvalue()


def test_unconfigured_and_ambiguous_acquisition_requires_selection(monkeypatch, downloads):
    page(monkeypatch, '<a href="/a.step">A</a><a href="/b.step">B</a>')
    monkeypatch.setattr(session, "browser_download", lambda *_: pytest.fail("ambiguous download"))
    url = "https://www.thk.com/product/x"
    assert thk.ADAPTER.acquire({"url": url, "kind": "category"})["reason"] == "unsupported"
    assert thk.ADAPTER.acquire({"url": url, "kind": "series"})["reason"] == "unsupported"
    assert thk.ADAPTER.acquire({"url": url, "requested_format": "step"})["reason"] == "unsupported"
    assert thk.ADAPTER.acquire({"url": url, "download_url": "/missing.step"})["reason"] == "not_found"
    assert thk.ADAPTER.acquire({"url": url, "download_url": "javascript:alert(1)"})["reason"] == "not_found"
    seen = []
    download(monkeypatch, STEP, seen)
    out = thk.ADAPTER.acquire({"url": url, "download_url": "/b.step"})
    assert out["ok"] and seen[0][0].endswith("/b.step")


def test_rejects_mismatched_format_and_invalid_archive(monkeypatch, downloads):
    url = "https://www.thk.com/product/x"
    assert thk.ADAPTER.acquire({})["reason"] == "not_found"
    page(monkeypatch, '<a href="/part.step">CAD</a>')
    monkeypatch.setattr(session, "browser_download", lambda *_: pytest.fail("wrong format"))
    assert thk.ADAPTER.acquire({"url": url, "requested_format": "iges"})["reason"] == "unsupported"
    existing = downloads / "thk" / "models.zip"
    existing.parent.mkdir()
    existing.write_bytes(b"previous archive")
    page(monkeypatch, '<a href="/models.zip">CAD</a>')
    download(monkeypatch, b"<html>not a zip</html>", [])
    assert thk.ADAPTER.acquire({"url": url})["reason"] == "parse_failed"
    assert existing.read_bytes() == b"previous archive"


def test_download_failure_retains_detail(monkeypatch, downloads):
    page(monkeypatch, '<a href="/part.step">CAD</a>')
    monkeypatch.setattr(session, "browser_download", lambda *_: {
        "ok": False, "reason": "timeout", "hint": "timed out", "status": 504})
    out = thk.ADAPTER.acquire({"url": "https://www.thk.com/product/x"})
    assert (out["site"], out["op"], out["reason"], out["status"]) == ("thk", "acquire", "timeout", 504)
