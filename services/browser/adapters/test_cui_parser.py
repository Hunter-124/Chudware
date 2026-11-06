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
import json
from urllib.parse import parse_qs, urlsplit

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
    assert first["mpn"] == "CFM-3010B-0130-373"
    assert first["title"] == "CFM-3010B-0130-373"
    assert first["url"] == (
        "https://www.sameskydevices.com/product/thermal-management/"
        "dc-fans/axial-fans/cfm-3010b-0130-373")
    assert first["category"] == "Thermal Management / Dc Fans / Axial Fans"
    assert first["kind"] == "product"
    assert first["thumbnail"] is None
    assert first["price"] is None and first["currency"] is None
    assert first["model"] == {"available": None, "formats": []}


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


def test_cui_capabilities_and_encoded_query(cui_mod, monkeypatch):
    session, cui = cui_mod
    seen = []
    monkeypatch.setattr(session, "browser_fetch",
                        lambda url, site: seen.append(url) or {"ok": True, "content": CUI_SEARCH_HTML})
    query = "CFM & 3/4+#"
    cui.ADAPTER.search(query)
    assert parse_qs(urlsplit(seen[0]).query)["partnumber"] == [query]
    assert cui.ADAPTER.hosts == ("cuidevices.com", "sameskydevices.com")
    assert cui.ADAPTER.result_kinds == ("product",)
    assert not cui.ADAPTER.cad_acquisition and not cui.ADAPTER.login_required and not cui.ADAPTER.api


def test_cui_card_media_uses_redirected_search_host(cui_mod, monkeypatch):
    session, cui = cui_mod
    html = """<a dl-itemnumber="FAN-1" class="dl-productLink" href="/product/fan-1">
      <img src="/assets/placeholder.png" data-src="/images/fan-1.jpg" alt="Fan 1"></a>
      <a href="/product/fan-1" dl-itemnumber="FAN-1" class="dl-productLink">duplicate</a>"""
    monkeypatch.setattr(session, "browser_fetch", lambda *_: {
        "ok": True, "final_url": "https://www.sameskydevices.com/sitesearch/searchbypartialmatch",
        "content": html})
    hit, = cui.ADAPTER.search("fan")
    assert hit["url"] == "https://www.sameskydevices.com/product/fan-1"
    assert hit["thumbnail"] == "https://www.sameskydevices.com/images/fan-1.jpg"
    assert hit["mpn"] == "FAN-1" and hit["sku"] is None


def test_cui_exact_match_redirect_is_the_product(cui_mod, monkeypatch):
    session, cui = cui_mod
    monkeypatch.setattr(session, "browser_fetch", lambda *_: {
        "ok": True, "final_url": "https://www.sameskydevices.com/product/fan-1",
        "content": """<h1>FAN-1</h1><img alt="Fan" src="/products/image/getproductimage/517046">
                      <a class="dl-productLink" dl-itemnumber="OTHER" href="/product/other"></a>"""})
    hit, = cui.ADAPTER.search("FAN-1")
    assert hit["url"] == "https://www.sameskydevices.com/product/fan-1"
    assert hit["mpn"] == "FAN-1" and hit["kind"] == "product"
    assert hit["thumbnail"] == "https://www.sameskydevices.com/products/image/getproductimage/517046"


def test_cui_exact_product_preserves_explicit_price(cui_mod, monkeypatch):
    session, cui = cui_mod
    product = {"@type": "Product", "name": "FAN-1", "url": "/product/fan-1",
               "mpn": "FAN-1", "image": "/images/fan-1.jpg",
               "offers": {"price": "12.50", "priceCurrency": "EUR"}}
    content = '<h1>FAN-1</h1><script type="application/ld+json">' + json.dumps(product) + "</script>"
    monkeypatch.setattr(session, "browser_fetch", lambda *_: {
        "ok": True, "content": content, "final_url": "https://www.sameskydevices.com/product/fan-1"})
    hit, = cui.ADAPTER.search("FAN-1")
    assert hit["price"] == 12.50 and hit["currency"] == "EUR"
    assert hit["thumbnail"] == "https://www.sameskydevices.com/images/fan-1.jpg"
    assert hit["mpn"] == "FAN-1" and hit["sku"] is None


@pytest.mark.parametrize(("payload", "reason"), [
    ({"ok": True, "content": "<h1>No match found for unknown</h1>"}, None),
    ({"ok": True, "content": "<script>var message='no results';</script><main>changed</main>"}, "parse_failed"),
    ({"ok": True, "content": "<h1>Access Denied</h1>"}, "blocked"),
    ({"ok": True, "content": '<h1>Access Denied</h1><div class="g-recaptcha"></div>'}, "captcha"),
    ({"ok": True, "content": '<div class="g-recaptcha"></div>'}, "captcha"),
    ({"ok": True, "content": '<h1>Sign In</h1><form><input type="password"></form>'}, "login_required"),
    ({"ok": True, "content": "", "final_url": "https://www.sameskydevices.com/login"}, "login_required"),
    ({"ok": False, "reason": "timeout", "status": 504, "hint": "timed out"}, "timeout"),
])
def test_cui_empty_and_failure_semantics(cui_mod, monkeypatch, payload, reason):
    session, cui = cui_mod
    monkeypatch.setattr(session, "browser_fetch", lambda *_: payload)
    hits = cui.ADAPTER.search("unknown")
    if reason is None:
        assert hits == []
    else:
        assert len(hits) == 1 and hits[0]["ok"] is False
        assert hits[0]["site"] == "cui" and hits[0]["op"] == "search"
        assert hits[0]["reason"] == reason


def test_cui_acquire_resolves_detail_media_after_redirect(cui_mod, monkeypatch):
    session, cui = cui_mod
    legacy = "https://www.cuidevices.com/product/fan-1"
    current = "https://www.sameskydevices.com/product/fan-1"
    product = {"@type": "Product", "name": "FAN-1", "url": "/product/fan-1",
               "image": "/images/fan-1.jpg"}
    page = ('<script type="application/ld+json">' + json.dumps(product) +
            '</script><a href="/product/resource/fan-1.pdf">Datasheet</a>')
    monkeypatch.setattr(session, "browser_fetch",
                        lambda *_: {"ok": True, "content": page, "final_url": current})
    downloads = []

    def download(url, site, dest):
        downloads.append(url)
        dest.write_bytes(b"%PDF-1.4\nfixture")
        return {"ok": True, "dest": str(dest)}

    monkeypatch.setattr(session, "browser_download", download)
    outcome = cui.ADAPTER.acquire({"url": legacy, "title": "FAN-1"})
    assert downloads == ["https://www.sameskydevices.com/product/resource/fan-1.pdf"]
    assert outcome["ok"] is True and outcome["artifact_kind"] == "document"
    assert outcome["format"] == "pdf"
    assert outcome["thumbnail"] == "https://www.sameskydevices.com/images/fan-1.jpg"
    assert outcome["currency"] is None


@pytest.mark.parametrize(("state", "reason"), [
    ("fetch", "transport_failed"), ("download", "timeout"), ("html", "parse_failed"),
])
def test_cui_acquire_failures_remain_failures(cui_mod, monkeypatch, state, reason):
    session, cui = cui_mod

    def fetch(*_):
        if state == "fetch":
            return {"ok": False, "reason": "browser_fetch_failed", "hint": "offline"}
        return {"ok": True, "content": '<a href="/resource/fan-datasheet.pdf">Datasheet</a>'}

    def download(url, site, dest):
        if state == "download":
            return {"ok": False, "reason": "timeout"}
        dest.write_bytes(b"<html>login page</html>")
        return {"ok": True, "dest": str(dest)}

    monkeypatch.setattr(session, "browser_fetch", fetch)
    monkeypatch.setattr(session, "browser_download", download)
    result = cui.ADAPTER.acquire({"url": "https://www.sameskydevices.com/product/fan-1"})
    assert result["ok"] is False and result["reason"] == reason
    assert result["site"] == "cui" and result["op"] == "acquire"
