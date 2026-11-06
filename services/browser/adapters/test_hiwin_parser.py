"""Offline coverage for HIWIN's page/series sitemaps and static CAD downloads."""
from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

from services.browser import adapters, session
from services.browser.adapters import hiwin


PAGES = """<?xml version="1.0"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://hiwin.com/products/linear-guideways/</loc></url>
  <url><loc>https://hiwin.com/products/linear-guideways/hg15ca/</loc></url>
  <url><loc>https://hiwin.com/company/distributors/</loc></url>
</urlset>"""
# The series URL and nested product image follow the live WordPress sitemap.
SERIES = """<?xml version="1.0"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"
        xmlns:image="http://www.google.com/schemas/sitemap-image/1.1">
  <url><loc>https://hiwin.com/products/linear-guideways/rg-series/</loc>
    <image:image><image:loc>https://hiwin.com/wp-content/uploads/2026/08/linear-guideway-rg.png</image:loc></image:image>
  </url>
  <url><loc>https://hiwin.com/products/ballscrews/super-z-series/</loc></url>
</urlset>"""
EMPTY = '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"></urlset>'
STEP = b"ISO-10303-21;\nHEADER;\nENDSEC;\nEND-ISO-10303-21;\n"


def sitemaps(monkeypatch, pages=PAGES, series=SERIES, seen=None):
    def fetch(url, site):
        if seen is not None:
            seen.append((url, site))
        content = pages if url == hiwin.SEARCH_URL else series
        return content if isinstance(content, dict) else {"ok": True, "content": content}
    monkeypatch.setattr(session, "browser_fetch", fetch)


def page(monkeypatch, content, **fields):
    monkeypatch.setattr(session, "browser_fetch", lambda *_: {"ok": True, "content": content, **fields})


def download(monkeypatch, data, seen):
    def save(url, site, dest):
        seen.append((url, site, dest))
        dest.write_bytes(data)
        return {"ok": True, "dest": str(dest), "session_profile_used": "hiwin-profile"}
    monkeypatch.setattr(session, "browser_download", save)


@pytest.fixture
def downloads(monkeypatch, tmp_path):
    monkeypatch.setattr(adapters.paths, "BROWSER_DOWNLOADS", tmp_path)
    return tmp_path


def test_search_distinguishes_categories_products_and_series_with_real_images(monkeypatch):
    seen = []
    sitemaps(monkeypatch, seen=seen)
    category, product, series = hiwin.ADAPTER.search("linear guide")
    assert seen == [(hiwin.SEARCH_URL, "hiwin"), (hiwin.SERIES_URL, "hiwin")]
    assert [hit["kind"] for hit in (category, product, series)] == ["category", "product", "series"]
    assert category["title"] == "Linear Guideways" and category["thumbnail"] is None
    assert product["url"] == "https://hiwin.com/products/linear-guideways/hg15ca/"
    assert series["url"] == "https://hiwin.com/products/linear-guideways/rg-series/"
    assert series["thumbnail"] == "https://hiwin.com/wp-content/uploads/2026/08/linear-guideway-rg.png"
    assert all(hit["manufacturer"] == "HIWIN" and hit["price"] is None for hit in (category, product, series))
    assert all(hit["sku"] is None and hit["model"]["available"] is None for hit in (category, product, series))


def test_search_supports_browser_rendered_sitemap_html(monkeypatch):
    rendered = '''<table><tr><td><a href="https://hiwin.com/products/linear-guideways/">Linear Guideways</a></td></tr>
      <tr><td><a href="https://hiwin.com/company/distributors/">Distributors</a></td></tr></table>'''
    sitemaps(monkeypatch, pages=rendered, series=EMPTY)
    hits = hiwin.ADAPTER.search("linear guide")
    assert len(hits) == 1 and hits[0]["kind"] == "category"
    assert hits[0]["url"] == "https://hiwin.com/products/linear-guideways/"


def test_no_matches_is_not_a_parser_failure(monkeypatch):
    sitemaps(monkeypatch)
    assert hiwin.ADAPTER.search("zz-no-match") == []
    sitemaps(monkeypatch, pages=EMPTY, series=EMPTY)
    assert hiwin.ADAPTER.search("guide") == []


def test_bad_or_partial_sitemap_reports_failure(monkeypatch):
    sitemaps(monkeypatch, pages="<html>changed markup</html>", series="broken")
    failure = hiwin.ADAPTER.search("guide")[0]
    assert (failure["site"], failure["op"], failure["reason"]) == ("hiwin", "search", "parse_failed")
    sitemaps(monkeypatch, pages="<urlset><url><missing/></url></urlset>", series=EMPTY)
    assert hiwin.ADAPTER.search("guide")[0]["reason"] == "parse_failed"
    sitemaps(monkeypatch, pages=PAGES, series={"ok": False, "reason": "rate_limited",
                                              "status": 429, "retry_after_s": 30})
    hits = hiwin.ADAPTER.search("linear guide")
    assert hits[0]["kind"] == "category" and hits[-1]["reason"] == "rate_limited"
    assert hits[-1]["status"] == 429 and hits[-1]["retry_after_s"] == 30


def test_sitemap_image_is_not_a_separate_result_or_logo(monkeypatch):
    logo_series = SERIES.replace("linear-guideway-rg.png", "logo.png")
    sitemaps(monkeypatch, pages=EMPTY, series=logo_series)
    hits = hiwin.ADAPTER.search("linear guide")
    assert len(hits) == 1 and hits[0]["kind"] == "series" and hits[0]["thumbnail"] is None


@pytest.mark.parametrize(("content", "reason"), [
    ("<title>Access Denied</title>", "blocked"),
    ("<div>verify you are human</div>", "captcha"),
    ('<form id="loginForm"></form>', "login_required"),
])
def test_wall_types_and_distributor_only_login(monkeypatch, content, reason):
    sitemaps(monkeypatch, pages=content, series=content)
    assert hiwin.ADAPTER.search("guide")[0]["reason"] == reason
    page(monkeypatch, content)
    out = hiwin.ADAPTER.acquire({"url": "https://hiwin.com/products/example/"})
    assert out["reason"] == reason
    if reason == "login_required":
        assert out["ask_user"] is None and out["distributors_url"] == hiwin.DISTRIBUTORS_URL


def test_acquire_valid_step_and_relative_link(monkeypatch, downloads):
    page(monkeypatch, '<a href="../cad/HG15CA.step?download=1">CAD</a>',
         final_url="https://hiwin.com/catalog/product/")
    seen = []
    download(monkeypatch, STEP, seen)
    out = hiwin.ADAPTER.acquire({"url": "https://hiwin.com/products/hg15ca/", "kind": "product"})
    assert out["ok"] and (out["artifact_kind"], out["format"]) == ("cad", "step")
    assert out["session_profile_used"] == "hiwin-profile"
    assert seen[0][0] == "https://hiwin.com/catalog/cad/HG15CA.step?download=1"
    assert Path(out["artifact_path"]) == downloads / "hiwin" / "HG15CA.step"
    assert Path(out["artifact_path"]).read_bytes() == STEP


@pytest.mark.parametrize("invalid", [b"<html>login</html>", b"%PDF-1.7 document", b""])
def test_invalid_download_preserves_existing_file(monkeypatch, downloads, invalid):
    existing = downloads / "hiwin" / "part.step"
    existing.parent.mkdir()
    existing.write_bytes(STEP)
    page(monkeypatch, '<a href="/part.step">CAD</a>')
    download(monkeypatch, invalid, [])
    out = hiwin.ADAPTER.acquire({"url": "https://hiwin.com/products/example/"})
    assert out["reason"] == "parse_failed" and existing.read_bytes() == STEP


def test_zip_stays_archive_instead_of_choosing_variant(monkeypatch, downloads):
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("HG15CA.step", STEP)
        zf.writestr("HG20CA.step", STEP)
    page(monkeypatch, '<a href="/models.zip">CAD</a>')
    download(monkeypatch, archive.getvalue(), [])
    out = hiwin.ADAPTER.acquire({"url": "https://hiwin.com/products/example/"})
    assert out["ok"] and (out["artifact_kind"], out["format"]) == ("archive", "zip")
    assert Path(out["artifact_path"]).read_bytes() == archive.getvalue()


def test_unconfigured_and_ambiguous_acquisition_requires_selection(monkeypatch, downloads):
    page(monkeypatch, '<a href="/a.step">A</a><a href="/b.step">B</a>')
    monkeypatch.setattr(session, "browser_download", lambda *_: pytest.fail("ambiguous download"))
    url = "https://hiwin.com/products/example/"
    assert hiwin.ADAPTER.acquire({"url": url, "kind": "category"})["reason"] == "unsupported"
    assert hiwin.ADAPTER.acquire({"url": url, "kind": "series"})["reason"] == "unsupported"
    assert hiwin.ADAPTER.acquire({"url": url, "requested_format": "step"})["reason"] == "unsupported"
    assert hiwin.ADAPTER.acquire({"url": url, "download_url": "/missing.step"})["reason"] == "not_found"
    assert hiwin.ADAPTER.acquire({"url": url, "download_url": "javascript:alert(1)"})["reason"] == "not_found"
    seen = []
    download(monkeypatch, STEP, seen)
    out = hiwin.ADAPTER.acquire({"url": url, "download_url": "/b.step"})
    assert out["ok"] and seen[0][0].endswith("/b.step")


def test_rejects_mismatched_format_and_invalid_archive(monkeypatch, downloads):
    url = "https://hiwin.com/products/example/"
    assert hiwin.ADAPTER.acquire({})["reason"] == "not_found"
    page(monkeypatch, '<a href="/part.step">CAD</a>')
    monkeypatch.setattr(session, "browser_download", lambda *_: pytest.fail("wrong format"))
    assert hiwin.ADAPTER.acquire({"url": url, "requested_format": "iges"})["reason"] == "unsupported"
    existing = downloads / "hiwin" / "models.zip"
    existing.parent.mkdir()
    existing.write_bytes(b"previous archive")
    page(monkeypatch, '<a href="/models.zip">CAD</a>')
    download(monkeypatch, b"<html>not a zip</html>", [])
    assert hiwin.ADAPTER.acquire({"url": url})["reason"] == "parse_failed"
    assert existing.read_bytes() == b"previous archive"


def test_download_failure_retains_detail(monkeypatch, downloads):
    page(monkeypatch, '<a href="/part.step">CAD</a>')
    monkeypatch.setattr(session, "browser_download", lambda *_: {
        "ok": False, "reason": "timeout", "hint": "timed out", "status": 504})
    out = hiwin.ADAPTER.acquire({"url": "https://hiwin.com/products/example/"})
    assert (out["site"], out["op"], out["reason"], out["status"]) == ("hiwin", "acquire", "timeout", 504)
