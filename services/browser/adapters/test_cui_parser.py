"""Fixture-based parser test for the CUI Devices search adapter.

CUI_SEARCH_HTML holds two VERBATIM ``dl-productLink`` result anchors from a real
CUI site-search page fetched live 2026-07-10 from
``/SiteSearch/SearchByPartialMatch?partnumber=dc+fan`` (HTTP 200). Results are
server-rendered cards (no JSON-LD); the adapter reads the product URL (CUI's
storefront is now sameskydevices.com), the dl-itemnumber part number and
dl-itemcategories breadcrumb. The header "Sign In" widget must not trip the wall.

Offline style mirrors test_fastenal_parser.py: session.browser_fetch monkeypatched.
"""
from __future__ import annotations

import importlib

import pytest


CUI_SEARCH_HTML = r"""<a href="https://www.sameskydevices.com/product/thermal-management/dc-fans/axial-fans/cfm-3010b-0130-373" id="f0f21e8e994dd2e79ed16615773c1742" class="result-link bg-white dl-productLink dataLayerClickEvent" dl-itemnumber="CFM-3010B-0130-373" dl-itemcategories="Thermal Management,Dc Fans,Axial Fans" dl-eventname="select_item" dl-listname="site_search">
<a href="https://www.sameskydevices.com/product/thermal-management/dc-fans/axial-fans/cfm-8025bg68-250-438-22" id="e3969b3580a6a7750610f3501bef4abb" class="result-link bg-white dl-productLink dataLayerClickEvent" dl-itemnumber="CFM-8025BG68-250-438-22" dl-itemcategories="Thermal Management,Dc Fans,Axial Fans" dl-eventname="select_item" dl-listname="site_search">"""


@pytest.fixture()
def cui_mod(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    from services import paths as engine_paths
    importlib.reload(engine_paths)
    from services.browser import profiles, session
    importlib.reload(profiles)
    importlib.reload(session)
    from services.browser import adapters as adapters_pkg
    importlib.reload(adapters_pkg)
    from services.browser.adapters import cui
    importlib.reload(cui)
    yield session, cui
    monkeypatch.delenv("APPDATA", raising=False)
    importlib.reload(engine_paths)


def test_cui_search_parses_result_cards(cui_mod, monkeypatch):
    session, cui = cui_mod
    monkeypatch.setattr(session, "browser_fetch",
                        lambda url, s: {"ok": True, "content": CUI_SEARCH_HTML})

    hits = cui.ADAPTER.search("dc fan")

    assert len(hits) == 2
    assert all(h["ok"] is True and h["site"] == "cui" for h in hits)

    first = hits[0]
    assert first["part_number"] == "CFM-3010B-0130-373"
    assert first["title"] == "CFM-3010B-0130-373"
    assert first["url"] == (
        "https://www.sameskydevices.com/product/thermal-management/"
        "dc-fans/axial-fans/cfm-3010b-0130-373")
    assert first["category"] == "Thermal Management / Dc Fans / Axial Fans"


def test_cui_search_url_is_sitesearch_endpoint():
    from services.browser.adapters import cui
    assert "SiteSearch/SearchByPartialMatch?partnumber=" in cui.SEARCH_URL_TMPL


def test_cui_header_signin_is_not_a_wall(cui_mod, monkeypatch):
    session, cui = cui_mod
    page = '<form><input name="password"></form>Sign In' + CUI_SEARCH_HTML
    monkeypatch.setattr(session, "browser_fetch",
                        lambda url, s: {"ok": True, "content": page})
    hits = cui.ADAPTER.search("dc fan")
    assert len(hits) == 2 and all(h.get("ok") for h in hits)
