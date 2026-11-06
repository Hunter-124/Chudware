"""Fixture-based parser test for the Datasheet4U search adapter.

DS4U_SEARCH_HTML holds two VERBATIM result rows from a real Datasheet4U search
page fetched live 2026-07-10 from ``/share_search.php?sWord=lm358`` (HTTP 200).
Results are a server-rendered table (no JSON-LD); the adapter reads the
``<a class="line" href="/datasheets/...">`` part anchor, the ``mfr-name`` span and
the Description cell.

Offline style mirrors test_fastenal_parser.py: session.browser_fetch monkeypatched.
"""
from __future__ import annotations

import importlib
from urllib.parse import parse_qs, urlsplit

import pytest


DS4U_SEARCH_HTML = r"""<tr>
      <td data-label="Manufacturer"><img src="/logo/STMicroelectronics.gif" alt="STMicroelectronics Logo" loading="lazy" style="width: min(150px, 25vw); height:auto; object-fit: contain; border:1px solid black; vertical-align:bottom;">
	  <span class="mfr-name">STMicroelectronics</span></td>
 	  <td data-label="Part Number"><a class="line" href="/datasheets/STMicroelectronics/LM358/1495631">LM358</a></td>
	  <td data-label="Description">Low-power dual operational amplifiers</td>
    </tr>
<tr>
      <td data-label="Manufacturer"><img src="/logo/ON Semiconductor.gif" alt="ON Semiconductor Logo" loading="lazy" style="width: min(150px, 25vw); height:auto; object-fit: contain; border:1px solid black; vertical-align:bottom;">
	  <span class="mfr-name">onsemi</span></td>
 	  <td data-label="Part Number"><a class="line" href="/datasheets/ON-Semiconductor/LM358N/49950">LM358N</a></td>
	  <td data-label="Description">Single Supply Dual Operational Amplifiers</td>
    </tr>"""


@pytest.fixture()
def ds4u_mod(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    from services import paths as engine_paths
    importlib.reload(engine_paths)
    from services.browser import profiles, session
    importlib.reload(profiles)
    importlib.reload(session)
    from services.browser import adapters as adapters_pkg
    importlib.reload(adapters_pkg)
    from services.browser.adapters import datasheet4u
    importlib.reload(datasheet4u)
    yield session, datasheet4u
    monkeypatch.delenv("APPDATA", raising=False)
    importlib.reload(engine_paths)


def test_datasheet4u_search_parses_table_rows(ds4u_mod, monkeypatch):
    session, datasheet4u = ds4u_mod
    monkeypatch.setattr(session, "browser_fetch",
                        lambda url, s: {"ok": True, "content": DS4U_SEARCH_HTML})

    hits = datasheet4u.ADAPTER.search("lm358")

    assert len(hits) == 2
    assert all(h["ok"] is True and h["site"] == "datasheet4u" for h in hits)

    first = hits[0]
    assert first["mpn"] == "LM358" and first["sku"] is None
    assert first["manufacturer"] == "STMicroelectronics"
    assert first["url"] == "https://www.datasheet4u.com/datasheets/STMicroelectronics/LM358/1495631"
    assert first["description"] == "Low-power dual operational amplifiers"
    assert first["kind"] == "document" and first["thumbnail"] is None
    assert first["price"] is None and first["currency"] is None and first["stock"] is None
    assert first["model"] == {"available": None, "formats": []}


def test_datasheet4u_search_url_is_share_search_endpoint():
    from services.browser.adapters import datasheet4u
    assert "share_search.php?sWord=" in datasheet4u.SEARCH_URL_TMPL


def test_datasheet4u_capabilities_and_query_encoding(ds4u_mod, monkeypatch):
    session, adapter = ds4u_mod
    seen = []
    monkeypatch.setattr(session, "browser_fetch",
                        lambda url, site: seen.append(url) or {"ok": True, "content": DS4U_SEARCH_HTML})
    query = "LM358 & A/B+#"
    adapter.ADAPTER.search(query)
    assert parse_qs(urlsplit(seen[0]).query)["sWord"] == [query]
    assert adapter.ADAPTER.hosts == ("datasheet4u.com",)
    assert adapter.ADAPTER.result_kinds == ("document",)
    assert not adapter.ADAPTER.cad_acquisition and not adapter.ADAPTER.login_required
    assert not adapter.ADAPTER.api


def test_datasheet4u_card_uses_part_photo_not_manufacturer_logo(ds4u_mod, monkeypatch):
    session, adapter = ds4u_mod
    html = """<tr><td><img src="/logo/vendor.gif" alt="Vendor Logo">
      <span class="mfr-name">Vendor &amp; Co</span>
      <img alt="LM358 package" data-src="/parts/lm358.jpg"></td>
      <td><a href="/datasheets/Vendor/LM358/42" class="line highlighted">LM358</a></td>
      <td data-label="Description">Operational &amp; amplifier</td></tr>"""
    monkeypatch.setattr(session, "browser_fetch", lambda *_: {"ok": True, "content": html})
    hit, = adapter.ADAPTER.search("LM358")
    assert hit["manufacturer"] == "Vendor & Co"
    assert hit["description"] == "Operational & amplifier"
    assert hit["thumbnail"] == "https://www.datasheet4u.com/parts/lm358.jpg"
    assert hit["kind"] == "document"


def test_datasheet4u_pdf_preview_is_not_a_part_photo(ds4u_mod, monkeypatch):
    session, adapter = ds4u_mod
    html = """<tr><td><img alt="PDF" src="/images/pdf.png">
      <img alt="page preview" src="/pdfhtml/1/page-000001.png"></td>
      <td><a class="line" href="/datasheets/Vendor/LM358/42">LM358</a></td></tr>"""
    monkeypatch.setattr(session, "browser_fetch", lambda *_: {"ok": True, "content": html})
    hit, = adapter.ADAPTER.search("LM358")
    assert hit["thumbnail"] is None


@pytest.mark.parametrize(("payload", "reason"), [
    ({"ok": True, "content": "<table><tr><td>Total 0 results</td></tr></table>"}, None),
    ({"ok": True, "content": "<script>var text='0 results';</script>changed"}, "parse_failed"),
    ({"ok": True, "content": "<h1>Access Denied</h1>"}, "blocked"),
    ({"ok": True, "content": '<h1>Access Denied</h1><div class="g-recaptcha"></div>'}, "captcha"),
    ({"ok": True, "content": '<div class="g-recaptcha"></div>'}, "captcha"),
    ({"ok": True, "content": '<h1>Sign In</h1><form><input type="password"></form>'}, "login_required"),
    ({"ok": True, "content": "", "final_url": "https://datasheet4u.com/login"}, "login_required"),
    ({"ok": False, "reason": "timeout", "status": 504, "hint": "timed out"}, "timeout"),
])
def test_datasheet4u_empty_and_failure_semantics(ds4u_mod, monkeypatch, payload, reason):
    session, adapter = ds4u_mod
    monkeypatch.setattr(session, "browser_fetch", lambda *_: payload)
    hits = adapter.ADAPTER.search("unknown")
    if reason is None:
        assert hits == []
    else:
        assert len(hits) == 1 and hits[0]["ok"] is False
        assert hits[0]["site"] == "datasheet4u" and hits[0]["op"] == "search"
        assert hits[0]["reason"] == reason


def test_datasheet4u_acquires_pdf_via_download_page(ds4u_mod, monkeypatch):
    session, adapter = ds4u_mod
    source = "https://www.datasheet4u.com/datasheets/STMicroelectronics/LM358/1495631"
    detail = '<a href="/download/1495631/LM358.html">Download PDF</a>'
    download_page = '<a href="/pdf/1495631/LM358.pdf">Download LM358 Datasheet PDF</a>'
    seen = []

    def fetch(url, site):
        seen.append(url)
        if url == source:
            return {"ok": True, "content": detail,
                    "final_url": "https://datasheet4u.com/datasheets/STMicroelectronics/LM358/1495631"}
        assert url == "https://datasheet4u.com/download/1495631/LM358.html"
        return {"ok": True, "content": download_page, "final_url": url}

    def download(url, site, dest):
        seen.append(url)
        dest.write_bytes(b"%PDF-1.4\nfixture")
        return {"ok": True, "dest": str(dest)}

    monkeypatch.setattr(session, "browser_fetch", fetch)
    monkeypatch.setattr(session, "browser_download", download)
    result = adapter.ADAPTER.acquire({"url": source, "title": "LM358", "mpn": "LM358"})
    assert seen[-1] == "https://datasheet4u.com/pdf/1495631/LM358.pdf"
    assert result["source_url"] == source and result["mpn"] == "LM358"
    assert result["artifact_kind"] == "document" and result["format"] == "pdf"
    assert result["artifact_path"].endswith("LM358.pdf")


@pytest.mark.parametrize(("download_result", "data", "reason"), [
    ({"ok": False, "reason": "timeout", "status": 504}, None, "timeout"),
    ({"ok": True}, b"<html>not a PDF</html>", "parse_failed"),
])
def test_datasheet4u_rejects_failed_or_html_download(ds4u_mod, monkeypatch, download_result, data, reason):
    session, adapter = ds4u_mod
    monkeypatch.setattr(session, "browser_fetch", lambda *_: {
        "ok": True, "content": '<a href="/files/datasheet.pdf">Datasheet</a>'})

    def download(url, site, dest):
        if data is not None:
            dest.write_bytes(data)
            return {**download_result, "dest": str(dest)}
        return download_result

    monkeypatch.setattr(session, "browser_download", download)
    result = adapter.ADAPTER.acquire({"url": "https://datasheet4u.com/datasheets/vendor/x/42"})
    assert result["ok"] is False and result["reason"] == reason
    assert result["op"] == "acquire" and result["site"] == "datasheet4u"


def test_datasheet4u_intermediate_failure_is_not_success(ds4u_mod, monkeypatch):
    session, adapter = ds4u_mod
    pages = iter([{"ok": True, "content": '<a href="/download/42/X.html">PDF</a>'},
                  {"ok": False, "reason": "timeout", "status": 504}])
    monkeypatch.setattr(session, "browser_fetch", lambda *_: next(pages))
    result = adapter.ADAPTER.acquire({"url": "https://datasheet4u.com/datasheets/vendor/x/42"})
    assert result["ok"] is False and result["reason"] == "timeout"
    assert result["status"] == 504 and result["op"] == "acquire"


def test_datasheet4u_intermediate_captcha_is_not_a_pdf(ds4u_mod, monkeypatch):
    session, adapter = ds4u_mod
    pages = iter([{"ok": True, "content": '<a href="/download/42/X.html">PDF</a>'},
                  {"ok": True, "content": "<h1>Captcha required</h1>"}])
    monkeypatch.setattr(session, "browser_fetch", lambda *_: next(pages))
    monkeypatch.setattr(session, "browser_download",
                        lambda *_: pytest.fail("a captcha is not a download"))
    result = adapter.ADAPTER.acquire({"url": "https://datasheet4u.com/datasheets/vendor/x/42"})
    assert result["ok"] is False and result["reason"] == "captcha"
    assert result["op"] == "acquire" and result["site"] == "datasheet4u"


def test_datasheet4u_intermediate_login_is_not_a_pdf(ds4u_mod, monkeypatch):
    session, adapter = ds4u_mod
    pages = iter([{"ok": True, "content": '<a href="/download/42/X.html">PDF</a>'},
                  {"ok": True, "content": '<h1>Sign In</h1><form><input type="password"></form>'}])
    monkeypatch.setattr(session, "browser_fetch", lambda *_: next(pages))
    monkeypatch.setattr(session, "browser_download",
                        lambda *_: pytest.fail("a login page is not a download"))
    result = adapter.ADAPTER.acquire({"url": "https://datasheet4u.com/datasheets/vendor/x/42"})
    assert result["ok"] is False and result["reason"] == "login_required"
    assert result["op"] == "acquire" and result["site"] == "datasheet4u"
