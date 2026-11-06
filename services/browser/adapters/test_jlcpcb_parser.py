"""Offline JLCPCB parts-library fixtures."""
import pytest

from services.browser.adapters import jlcpcb


SEARCH_HTML = '''<table>
<tr><td><img srcset="/img/stm32.png 1x, /img/stm32@2x.png 2x"></td>
<td><a href="/partdetail/Basic_C123456" class="part-item">STM32F103</a></td>
<td><span class="lcsc-number">C123456</span><span class="mpn">STM32F103</span>
<span class="manufacturer">ST</span><span class="price">$1.23</span>
<span class="stock">4,500 in stock</span></td></tr>
<tr><td><a class="part-item" href="/partdetail/Basic_C7">Other IC
<span class="lcsc-number">C7</span><span class="price">$0.02</span>
<span class="stock">8 in stock</span></a></td></tr></table>'''

# Trimmed from public SSR 2026-11-04: category navigation, not product rows.
LIVE_SSR_SHELL = '''<div class="flex-1"><h2 class="inline">
<a href="/parts/1st/Capacitors_2">Capacitors</a></h2>
<span class="text-999999">(1012901)</span></div>'''


def test_search_preserves_lcsc_stock_price_and_image(monkeypatch):
    requested = []
    monkeypatch.setattr(jlcpcb._session, "browser_fetch", lambda url, site: (
        requested.append((url, site)) or {"ok": True, "content": SEARCH_HTML}))
    first, second = jlcpcb.ADAPTER.search("C++ & /#")
    assert requested == [("https://jlcpcb.com/parts/componentSearch?searchTxt=C%2B%2B+%26+%2F%23", "jlcpcb")]
    assert (first["kind"], first["sku"], first["mpn"], first["manufacturer"]) == (
        "product", "C123456", "STM32F103", "ST")
    assert (first["price"], first["currency"], first["stock"]) == (1.23, "USD", 4500)
    assert first["thumbnail"] == "https://jlcpcb.com/img/stm32.png"
    assert (second["title"], second["sku"], second["price"], second["stock"]) == ("Other IC", "C7", 0.02, 8)


def test_current_rendered_result(monkeypatch):
    # Trimmed rendered result row, 2026-11-04.
    page = '''USD <table><tr class="el-table__row cursor-pointer"><td><div class="cell">
    <img src="/ssr/img/img-empty.29365da.png" alt="no-image">
    <a data-v-62d4b805="" href="/partdetail/HKR_Hong_Kong_Resistors-RCT0310KJLF/C177337" class="text-primary cursor-pointer truncate no-underline f1 el-popover__reference" target="_blank"><span data-v-62d4b805="">RCT0310KJLF</span></a>
    </div></td><td><div class="cell"><div class="leading-[20px] flex items-center justify-between">
    <span data-v-05ff45c3="" class="inline mr10">1+</span> <span data-v-05ff45c3="">$0.0014</span></div></div></td></tr></table>'''
    monkeypatch.setattr(jlcpcb._session, "browser_fetch", lambda *_: {"ok": True, "content": page})
    hit = jlcpcb.ADAPTER.search("0603 resistor 10k")[0]
    assert (hit["kind"], hit["title"], hit["sku"]) == ("product", "RCT0310KJLF", "C177337")
    assert hit["url"] == "https://jlcpcb.com/partdetail/HKR_Hong_Kong_Resistors-RCT0310KJLF/C177337"
    assert hit["thumbnail"] is None
    assert (hit["price"], hit["currency"]) == (0.0014, "USD")


def test_lcsc_number_from_url_and_conflicting_number(monkeypatch):
    content = '<a class="part-item" href="/partdetail/Basic_C123">Device</a>'
    monkeypatch.setattr(jlcpcb._session, "browser_fetch", lambda *_: {"ok": True, "content": content})
    assert jlcpcb.ADAPTER.search("device")[0]["sku"] == "C123"
    conflicting = content.replace("Device</a>", 'Device<span class="lcsc-number">C456</span></a>')
    monkeypatch.setattr(jlcpcb._session, "browser_fetch", lambda *_: {"ok": True, "content": conflicting})
    assert jlcpcb.ADAPTER.search("device")[0]["reason"] == "parse_failed"


@pytest.mark.parametrize(("content", "reason"), [
    ("Access Denied", "blocked"), ("Verify you are human", "captcha"),
    ('<div id="login-email"></div>', "login_required"), (LIVE_SSR_SHELL, "parse_failed"),
])
def test_search_failures(monkeypatch, content, reason):
    monkeypatch.setattr(jlcpcb._session, "browser_fetch", lambda *_: {"ok": True, "content": content})
    failure = jlcpcb.ADAPTER.search("chip")[0]
    assert (failure["ok"], failure["site"], failure["op"], failure["reason"]) == (
        False, "jlcpcb", "search", reason)


def test_no_results_and_transport_failure(monkeypatch):
    monkeypatch.setattr(jlcpcb._session, "browser_fetch", lambda *_: {"ok": True, "content": "No products found"})
    assert jlcpcb.ADAPTER.search("no such part") == []
    monkeypatch.setattr(jlcpcb._session, "browser_fetch", lambda *_: {"ok": False, "reason": "timeout", "hint": "late"})
    failure = jlcpcb.ADAPTER.search("chip")[0]
    assert (failure["reason"], failure["site"], failure["op"]) == ("timeout", "jlcpcb", "search")


def test_acquisition_is_honest_about_no_cad_and_terms(monkeypatch):
    monkeypatch.setattr(jlcpcb._session, "browser_fetch", lambda *_: {"ok": True, "content": "<html>part</html>"})
    result = jlcpcb.ADAPTER.acquire({"url": "https://jlcpcb.com/partdetail/Basic_C123456"})
    assert result["ok"] is False and result["reason"] == "unsupported"
    assert not result.get("artifact_path")
    monkeypatch.setattr(jlcpcb._session, "browser_fetch", lambda *_: {
        "ok": True, "content": '<div class="order-terms">Accept the terms</div>'})
    result = jlcpcb.ADAPTER.acquire({"url": "https://jlcpcb.com/partdetail/Basic_C123456"})
    assert result["reason"] == "terms_required" and result["ask_user"]
