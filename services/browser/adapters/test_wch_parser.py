"""Fixture-based parser test for the WCH (Nanjing Qinheng) search adapter.

WCH_SEARCH_HTML holds two VERBATIM result blocks from a real WCH search page
fetched live 2026-07-10 from ``https://search.wch-ic.com/?q=ch340&t=all`` (HTTP
200, Vue/Element-Plus app, CEF-rendered). Results are DOCUMENT hits: a
``/downloads/<NAME>_PDF.html`` anchor followed by a ``file-content`` blurb
(datasheets, drivers). Titles carry ``<em>`` search-highlight spans that are
stripped. The regex keys on the stable ``/downloads/`` href + ``file-content``
class, NOT the per-build ``data-v-16ac75ef`` scoped-style hash present here.

Offline style mirrors test_cui_parser.py: session.browser_fetch monkeypatched.
"""
from __future__ import annotations

import importlib
from urllib.parse import parse_qs, urlsplit

import pytest


WCH_SEARCH_HTML = r"""<a data-v-16ac75ef="" href="https://www.wch-ic.com/downloads/CH340DS1_PDF.html" target="_blank"><em>CH340</em>DS1.PDF</a></div><div data-v-16ac75ef="" class="file-content"><em>CH340</em> datasheet, USB bus converter chip which converts USB to serial port, printer port, IrDA SIR etc. Integrated clock, supports various operating systems,
chip information can be customized, this datasheet is about USB to serial port and USB to Infrared Adapter SIR.</div><div data-v-16ac75ef="">3.4</div><div data-v-16ac75ef="">2025-03-12</div></div></div></div><div data-v-16ac75ef=""><!----></div><div data-v-16ac75ef=""><div data-v-16ac75ef=""><div data-v-16ac75ef="" class="download-second-title">Drivers</div><div data-v-16ac75ef="" class="download-content"><div data-v-16ac75ef=""><a data-v-16ac75ef="" href="https://www.wch-ic.com/downloads/CH341SER_EXE.html" target="_blank">CH341SER.EXE</a></div><div data-v-16ac75ef="" class="file-content"><em>CH340</em>/CH341 USB to serial port One-Key installation VCP vendor driver  for Windows, supports  Windows 11/10/8.1/8/7/VISTA/XP/2000/98.</div>"""


@pytest.fixture()
def wch_mod(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    from services import paths as engine_paths
    importlib.reload(engine_paths)
    from services.browser import profiles, session
    importlib.reload(profiles)
    importlib.reload(session)
    from services.browser import adapters as adapters_pkg
    importlib.reload(adapters_pkg)
    from services.browser.adapters import wch
    importlib.reload(wch)
    yield session, wch
    monkeypatch.delenv("APPDATA", raising=False)
    importlib.reload(engine_paths)


def test_wch_search_parses_document_results(wch_mod, monkeypatch):
    session, wch = wch_mod
    monkeypatch.setattr(session, "browser_fetch",
                        lambda url, s: {"ok": True, "content": WCH_SEARCH_HTML})

    hits = wch.ADAPTER.search("ch340")

    assert len(hits) == 2
    assert all(h["ok"] is True and h["site"] == "wch" for h in hits)
    assert all(h["kind"] == "document" for h in hits)

    first = hits[0]
    assert first["title"] == "CH340DS1.PDF"   # <em> highlight stripped
    assert first["url"] == "https://www.wch-ic.com/downloads/CH340DS1_PDF.html"
    assert first["description"].startswith("CH340 datasheet")
    assert first["thumbnail"] is None
    assert first["price"] is None and first["currency"] is None and first["sku"] is None
    assert first["manufacturer"] is None and first["mpn"] is None
    assert first["model"] == {"available": None, "formats": []}

    second = hits[1]
    assert second["title"] == "CH341SER.EXE"
    assert second["url"] == "https://www.wch-ic.com/downloads/CH341SER_EXE.html"


def test_wch_search_url_is_search_subdomain():
    from services.browser.adapters import wch
    assert wch.SEARCH_URL_TMPL.startswith("https://search.wch-ic.com/?q=")
    assert "t=all" in wch.SEARCH_URL_TMPL


def test_wch_query_and_capabilities(wch_mod, monkeypatch):
    session, wch = wch_mod
    seen = []
    monkeypatch.setattr(session, "browser_fetch",
                        lambda url, site: seen.append(url) or {"ok": True, "content": WCH_SEARCH_HTML})
    query = "CH340 & A/B+#"
    wch.ADAPTER.search(query)
    assert parse_qs(urlsplit(seen[0]).query) == {"q": [query], "t": ["all"]}
    assert wch.ADAPTER.hosts == ("wch-ic.com", "search.wch-ic.com")
    assert wch.ADAPTER.result_kinds == ("document",)
    assert not wch.ADAPTER.cad_acquisition and not wch.ADAPTER.login_required and not wch.ADAPTER.api


def test_wch_relative_document_with_real_part_image(wch_mod, monkeypatch):
    session, wch = wch_mod
    page = """<div><a href="/downloads/CH340DS1_PDF.html">
    <img alt="CH340 chip" src="/images/ch340.jpg">CH340DS1.PDF</a></div>
    <div class="file-content">USB &amp; serial</div>"""
    monkeypatch.setattr(session, "browser_fetch", lambda *_: {"ok": True, "content": page})
    hit, = wch.ADAPTER.search("CH340")
    assert hit["url"] == "https://www.wch-ic.com/downloads/CH340DS1_PDF.html"
    assert hit["thumbnail"] == "https://www.wch-ic.com/images/ch340.jpg"
    assert hit["description"] == "USB & serial"
    assert hit["kind"] == "document" and hit["price"] is None


def test_wch_document_icon_is_not_product_image(wch_mod, monkeypatch):
    session, wch = wch_mod
    page = '<a href="/downloads/CH340DS1_PDF.html"><img src="/images/pdf.png" alt="PDF">CH340DS1</a>'
    monkeypatch.setattr(session, "browser_fetch", lambda *_: {"ok": True, "content": page})
    hit, = wch.ADAPTER.search("CH340")
    assert hit["thumbnail"] is None


@pytest.mark.parametrize(("payload", "reason"), [
    ({"ok": True, "content": '<div class="el-empty__description"><p>No Data</p></div>'}, None),
    ({"ok": True, "content": '<html><div id="app"></div><script src="app.js"></script></html>'}, "parse_failed"),
    ({"ok": True, "content": '<div>No Data</div>'}, "parse_failed"),
    ({"ok": True, "content": "<h1>Access Denied</h1>"}, "blocked"),
    ({"ok": True, "content": '<h1>Access Denied</h1><div class="g-recaptcha"></div>'}, "captcha"),
    ({"ok": True, "content": '<div class="g-recaptcha"></div>'}, "captcha"),
    ({"ok": True, "content": '<h1>Sign In</h1><form><input type="password"></form>'}, "login_required"),
    ({"ok": True, "content": "", "final_url": "https://search.wch-ic.com/login"}, "login_required"),
    ({"ok": False, "reason": "timeout", "status": 504, "hint": "timed out"}, "timeout"),
])
def test_wch_empty_and_failure_semantics(wch_mod, monkeypatch, payload, reason):
    session, wch = wch_mod
    monkeypatch.setattr(session, "browser_fetch", lambda *_: payload)
    hits = wch.ADAPTER.search("unknown")
    if reason is None:
        assert hits == []
    else:
        assert len(hits) == 1 and hits[0]["ok"] is False
        assert hits[0]["site"] == "wch" and hits[0]["op"] == "search"
        assert hits[0]["reason"] == reason


def test_wch_header_login_text_is_not_wall(wch_mod, monkeypatch):
    session, wch = wch_mod
    monkeypatch.setattr(session, "browser_fetch",
                        lambda *_: {"ok": True, "content": "Sign In " + WCH_SEARCH_HTML})
    assert len(wch.ADAPTER.search("CH340")) == 2


def test_wch_available_pdf_is_a_document_not_cad(wch_mod, monkeypatch):
    session, wch = wch_mod
    monkeypatch.setattr(session, "browser_fetch", lambda *_: {
        "ok": True, "content": '<a href="/files/CH340_datasheet.pdf">Datasheet</a>'})

    def download(url, site, dest):
        assert url == "https://www.wch-ic.com/files/CH340_datasheet.pdf"
        dest.write_bytes(b"%PDF-1.4\nfixture")
        return {"ok": True, "dest": str(dest)}

    monkeypatch.setattr(session, "browser_download", download)
    result = wch.ADAPTER.acquire({"url": "https://www.wch-ic.com/downloads/CH340DS1_PDF.html"})
    assert result["ok"] is True and result["artifact_kind"] == "document"
    assert result["format"] == "pdf"


def test_wch_driver_is_not_misidentified_as_pdf_or_cad(wch_mod, monkeypatch):
    session, wch = wch_mod
    monkeypatch.setattr(session, "browser_fetch", lambda *_: {
        "ok": True, "content": '<a href="/drivers/CH341SER.exe">Windows driver</a>'})
    monkeypatch.setattr(session, "browser_download",
                        lambda *_: pytest.fail("driver must not be downloaded as a PDF"))
    result = wch.ADAPTER.acquire({"url": "https://www.wch-ic.com/downloads/CH341SER_EXE.html"})
    assert result["ok"] is False and result["reason"] == "not_found"


@pytest.mark.parametrize(("state", "reason"), [("download", "timeout"), ("html", "parse_failed")])
def test_wch_pdf_acquisition_failure(wch_mod, monkeypatch, state, reason):
    session, wch = wch_mod
    monkeypatch.setattr(session, "browser_fetch", lambda *_: {
        "ok": True, "content": '<a href="/files/ch340-datasheet.pdf">Datasheet</a>'})

    def download(url, site, dest):
        if state == "download":
            return {"ok": False, "reason": "timeout"}
        dest.write_bytes(b"<html>not PDF</html>")
        return {"ok": True, "dest": str(dest)}

    monkeypatch.setattr(session, "browser_download", download)
    result = wch.ADAPTER.acquire({"url": "https://www.wch-ic.com/downloads/CH340DS1_PDF.html"})
    assert result["ok"] is False and result["reason"] == reason
    assert result["site"] == "wch" and result["op"] == "acquire"
