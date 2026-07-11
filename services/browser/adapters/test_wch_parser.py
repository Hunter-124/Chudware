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

import pytest


WCH_SEARCH_HTML = r"""<a data-v-16ac75ef="" href="https://www.wch-ic.com/downloads/CH340DS1_PDF.html" target="_blank"><em>CH340</em>DS1.PDF</a></div><div data-v-16ac75ef="" class="file-content"><em>CH340</em> datasheet, USB bus converter chip which converts USB to serial port, printer port, IrDA SIR etc. Integrated clock, supports various operating systems,
chip information can be customized, this datasheet is about USB to serial port and USB to Infrared Adapter SIR.</div><div data-v-16ac75ef="">3.4</div><div data-v-16ac75ef="">2025-03-12</div></div></div></div><div data-v-16ac75ef=""><!----></div><div data-v-16ac75ef=""><div data-v-16ac75ef=""><div data-v-16ac75ef="" class="download-second-title">Drivers</div><div data-v-16ac75ef="" class="download-content"><div data-v-16ac75ef=""><a data-v-16ac75ef="" href="https://www.wch-ic.com/downloads/CH341SER_EXE.html" target="_blank">CH341SER.EXE</a></div><div data-v-16ac75ef="" class="file-content"><em>CH340</em>/CH341 USB to serial port One-Key installation VCP vendor driver  for Windows, supports  Windows 11/10/8.1/8/7/VISTA/XP/2000/98.</div>"""


@pytest.fixture()
def wch_mod(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    from engine import paths as engine_paths
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

    second = hits[1]
    assert second["title"] == "CH341SER.EXE"
    assert second["url"] == "https://www.wch-ic.com/downloads/CH341SER_EXE.html"


def test_wch_search_url_is_search_subdomain():
    from services.browser.adapters import wch
    assert wch.SEARCH_URL_TMPL.startswith("https://search.wch-ic.com/?q=")
    assert "t=all" in wch.SEARCH_URL_TMPL
