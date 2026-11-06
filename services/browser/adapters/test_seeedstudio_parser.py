"""Fixture-based parser test for the Seeed Studio search adapter.

SEEED_SEARCH_HTML is a VERBATIM slice of a real Seeed Studio search page fetched
live 2026-07-10 from ``/catalogsearch/result/?q=esp32`` (HTTP 200) — two real
Algolia InstantSearch ``ais_hit_product_bar`` tiles copied byte-for-byte. Results
are Algolia-rendered (not JSON-LD); the adapter reads the ``ais_p_title`` anchor
and ``ais_p_sku`` span. The header "Sign In" widget must not trip the login wall.

Offline style mirrors test_fastenal_parser.py: session.browser_fetch monkeypatched.
"""
from __future__ import annotations

import importlib

import pytest


SEEED_SEARCH_HTML = r"""<div id="5639" class="ais_hit_product_bar"><a href="https://www.seeedstudio.com/XIAO-ESP32S3-Sense-p-5639.html" target="_blank" class="ais_p_img"><img src="https://media-cdn.seeedstudio.com/media/catalog/product/cache/846608d0e3abbdf8f9454c4f707bfdbe/2/-/2-113991115-xiao-esp32-s3-sense_1.jpg" alt=""></a> <div class="ais_p_info"><a href="https://www.seeedstudio.com/XIAO-ESP32S3-Sense-p-5639.html" target="_blank" class="ais_p_title"><span class="ais-Highlight">Seeed Studio XIAO <mark class="ais-Highlight-highlighted">ESP32</mark>-S3 Sense </span></a> <div class="ais_p_sku"><span class="ais-Highlight">113991115</span></div> <div class="ais_p_desc"><span class="ais-Highlight">Seeed Studio XIAO <mark class="ais-Highlight-highlighted">ESP32</mark>-S3 leverages dual-core <mark class="ais-Highlight-highlighted">ESP32</mark>-S3 chip, supporting both Wi-Fi and BLE wireless connectivities, which allows battery charge. It integrates built-in camera sensor,</span></div> <div class="ais_p_price"><span class="ais_p_price_now">$13.90</span> <!----></div> <!----></div></div><div id="6401" class="ais_hit_product_bar"><a href="https://www.seeedstudio.com/Louder-ESP32-p-6401.html" target="_blank" class="ais_p_img"><img src="https://media-cdn.seeedstudio.com/media/catalog/product/cache/846608d0e3abbdf8f9454c4f707bfdbe/1/-/1-louder-esp32-wrover-n16r8-_built-in-antenna_.jpg" alt=""></a> <div class="ais_p_info"><a href="https://www.seeedstudio.com/Louder-ESP32-p-6401.html" target="_blank" class="ais_p_title"><span class="ais-Highlight">Louder <mark class="ais-Highlight-highlighted">ESP32</mark>, <mark class="ais-Highlight-highlighted">ESP32</mark> audio development board with TAS5805M I2S DAC, driving 2x 23W load (8Ω, 1% THD+N)</span></a> <div class="ais_p_sku"><span class="ais-Highlight">102991934</span></div> <div class="ais_p_desc"><span class="ais-Highlight">Louder-ESP32 combines ubiquitous <mark class="ais-Highlight-highlighted">ESP32</mark> with an I2S DAC with built-in D-class amplifier capable of driving full-size speakers. This combination provides your compact <mark class="ais-Highlight-highlighted">ESP32</mark> module</span></div> <div class="ais_p_price"><span class="ais_p_price_now">$24.00</span> <!----></div> <!----></div></div>"""


@pytest.fixture()
def seeed_mod(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    from services import paths as engine_paths
    importlib.reload(engine_paths)
    from services.browser import profiles, session
    importlib.reload(profiles)
    importlib.reload(session)
    from services.browser import adapters as adapters_pkg
    importlib.reload(adapters_pkg)
    from services.browser.adapters import seeedstudio
    importlib.reload(seeedstudio)
    yield session, seeedstudio
    monkeypatch.delenv("APPDATA", raising=False)
    importlib.reload(engine_paths)


def test_seeed_search_parses_algolia_hits(seeed_mod, monkeypatch):
    session, seeedstudio = seeed_mod
    monkeypatch.setattr(session, "browser_fetch",
                        lambda url, s: {"ok": True, "content": SEEED_SEARCH_HTML})

    hits = seeedstudio.ADAPTER.search("esp32")

    assert len(hits) == 2
    assert all(h["ok"] is True and h["site"] == "seeedstudio" for h in hits)

    first = hits[0]
    assert first["title"] == "Seeed Studio XIAO ESP32-S3 Sense"
    assert first["url"] == "https://www.seeedstudio.com/XIAO-ESP32S3-Sense-p-5639.html"
    assert first["sku"] == "113991115"
    assert first["kind"] == "product" and first["mpn"] is None
    assert first["price"] == 13.90 and first["currency"] is None  # bare "$" is ambiguous
    assert first["stock"] is None and first["model"] == {"available": None, "formats": []}
    assert first["thumbnail"] == (
        "https://media-cdn.seeedstudio.com/media/catalog/product/cache/"
        "846608d0e3abbdf8f9454c4f707bfdbe/2/-/2-113991115-xiao-esp32-s3-sense_1.jpg")
    assert hits[1]["price"] == 24.0 and hits[1]["currency"] is None
    assert hits[1]["thumbnail"].endswith("/1/-/1-louder-esp32-wrover-n16r8-_built-in-antenna_.jpg")


def test_current_search_card(seeed_mod, monkeypatch):
    session, seeed = seeed_mod
    # Trimmed from the rendered search page, 2026-11-04.
    page = '''<div class="group relative flex h-full flex-col bg-white border border-gray-200 transition-all duration-300 rounded-md overflow-hidden md:rounded-xl hover:border-primary hover:shadow-md">
    <a href="/XIAO-ESP32S3-Sense-p-5639.html?c_event_medium=search%3A%3Aresults_ad&amp;c_event_id=800286010%3A%3Aesp32" target="_blank" title="Seeed Studio XIAO ESP32-S3 Sense ">
    <div class="relative aspect-4/3 w-full overflow-hidden"><img alt="113991115" src="https://media-cdn.seeedstudio.com/cdn-cgi/image/format=auto%2Cwidth=3840%2Cquality=85/media/catalog/product/2/-/2-113991115-xiao-esp32-s3-sense_1.jpg"></div></a>
    <div class="relative flex flex-1 flex-col"><div class="text-[11px]">SKU 113991115</div>
    <a href="/XIAO-ESP32S3-Sense-p-5639.html"><div>Seeed Studio XIAO ESP32-S3 Sense </div></a>
    <span class="text-black text-[14px]">$13.90</span></div></div>'''
    monkeypatch.setattr(session, "browser_fetch", lambda *_: {"ok": True, "content": page})
    hit = seeed.ADAPTER.search("esp32")[0]
    assert hit["kind"] == "product" and hit["title"] == "Seeed Studio XIAO ESP32-S3 Sense"
    assert hit["url"] == ("https://www.seeedstudio.com/XIAO-ESP32S3-Sense-p-5639.html"
                          "?c_event_medium=search%3A%3Aresults_ad&c_event_id=800286010%3A%3Aesp32")
    assert hit["thumbnail"] == ("https://media-cdn.seeedstudio.com/cdn-cgi/image/"
                                "format=auto%2Cwidth=3840%2Cquality=85/media/catalog/product/2/-/"
                                "2-113991115-xiao-esp32-s3-sense_1.jpg")
    assert (hit["sku"], hit["price"], hit["currency"]) == ("113991115", 13.9, None)


def test_seeed_header_signin_is_not_a_wall(seeed_mod, monkeypatch):
    session, seeedstudio = seeed_mod
    page = "<a>Sign In</a><a>Log In</a>" + SEEED_SEARCH_HTML
    monkeypatch.setattr(session, "browser_fetch",
                        lambda url, s: {"ok": True, "content": page})
    hits = seeedstudio.ADAPTER.search("esp32")
    assert len(hits) == 2 and all(h.get("ok") for h in hits)


def test_seeed_capabilities_query_and_explicit_currency(seeed_mod, monkeypatch):
    session, seeed = seeed_mod
    assert seeed.ADAPTER.hosts == ("seeedstudio.com",)
    assert seeed.ADAPTER.result_kinds == ("product",)
    assert not seeed.ADAPTER.cad_acquisition and not seeed.ADAPTER.login_required and not seeed.ADAPTER.api
    assert seeed.ADAPTER.search_url("ESP32 & A/B #1").endswith("?q=ESP32+%26+A%2FB+%231")
    page = SEEED_SEARCH_HTML.replace("$13.90", "US$13.90")
    monkeypatch.setattr(session, "browser_fetch", lambda url, site: {"ok": True, "content": page})
    assert seeed.ADAPTER.search("esp32")[0]["currency"] == "USD"


@pytest.mark.parametrize(("display", "currency"), [
    ("US$13.90", "USD"), ("13.90 USD", "USD"), ("€13.90", "EUR"),
    ("£13.90", "GBP"), ("$13.90", None),
])
def test_seeed_card_price_currency_is_not_guessed(seeed_mod, monkeypatch, display, currency):
    session, seeed = seeed_mod
    page = SEEED_SEARCH_HTML.replace("$13.90", display)
    monkeypatch.setattr(session, "browser_fetch", lambda url, site: {"ok": True, "content": page})
    first = seeed.ADAPTER.search("esp32")[0]
    assert first["price"] == 13.9 and first["currency"] == currency


def test_seeed_card_without_image_does_not_borrow_next_image(seeed_mod, monkeypatch):
    session, seeed = seeed_mod
    page = """
    <div class="ais_hit_product_bar"><img src="/images/logo.png" alt="logo">
      <a class="ais_p_title" href="/a">First</a></div>
    <div class="ais_hit_product_bar"><img data-src="/images/second.jpg">
      <a class="ais_p_title" href="/b">Second</a></div>
    """
    monkeypatch.setattr(session, "browser_fetch", lambda url, site: {"ok": True, "content": page})
    first, second = seeed.ADAPTER.search("items")
    assert first["thumbnail"] is None and first["price"] is None and first["currency"] is None
    assert second["thumbnail"] == "https://www.seeedstudio.com/images/second.jpg"


def test_seeed_jsonld_fallback(seeed_mod, monkeypatch):
    session, seeed = seeed_mod
    page = '''<script type="application/ld+json">{
      "@type":"Product","name":"Example board","url":"/board",
      "image":"/images/board.jpg","offers":{"price":"12.50","priceCurrency":"USD"}
    }</script>'''
    monkeypatch.setattr(session, "browser_fetch", lambda url, site: {"ok": True, "content": page})
    hit = seeed.ADAPTER.search("board")[0]
    assert hit["kind"] == "product" and hit["price"] == 12.5 and hit["currency"] == "USD"
    assert hit["thumbnail"] == "https://www.seeedstudio.com/images/board.jpg"


@pytest.mark.parametrize(("page", "reason"), [
    ("<h1>Access Denied</h1>", "blocked"),
    ("<h1>Verify you are human</h1>", "captcha"),
    ("<p>Please sign in to continue</p>", "login_required"),
    ('<script>{"no_results":"No results found","captcha":"CAPTCHA"}</script><main>Loading</main>', "parse_failed"),
])
def test_seeed_failure_types(seeed_mod, monkeypatch, page, reason):
    session, seeed = seeed_mod
    monkeypatch.setattr(session, "browser_fetch", lambda url, site: {"ok": True, "content": page})
    failure = seeed.ADAPTER.search("esp32")[0]
    assert failure["ok"] is False and failure["reason"] == reason
    assert failure["site"] == "seeedstudio" and failure["op"] == "search"


def test_seeed_confirmed_empty_search(seeed_mod, monkeypatch):
    session, seeed = seeed_mod
    monkeypatch.setattr(session, "browser_fetch", lambda url, site: {
        "ok": True, "content": "<main>No results found</main>"})
    assert seeed.ADAPTER.search("no-such-item") == []


def test_seeed_transport_failure(seeed_mod, monkeypatch):
    session, seeed = seeed_mod
    monkeypatch.setattr(session, "browser_fetch", lambda url, site: {
        "ok": False, "reason": "rate_limited", "status": 429, "retry_after_s": 30})
    failure = seeed.ADAPTER.search("esp32")[0]
    assert failure["reason"] == "rate_limited" and failure["status"] == 429
    assert failure["retry_after_s"] == 30 and failure["url"] == seeed.ADAPTER.search_url("esp32")


def test_seeed_datasheet_is_a_document(seeed_mod, monkeypatch, tmp_path):
    session, seeed = seeed_mod
    from services.browser.adapters import _retail
    monkeypatch.setattr(session, "browser_fetch", lambda url, site: {
        "ok": True, "content": '<a href="/docs/spec.pdf">Datasheet</a>'})
    dest = tmp_path / "spec.pdf"
    monkeypatch.setattr(_retail, "download_path", lambda *args, **kwargs: (dest, None))

    def download(url, site, path):
        path.write_bytes(b"%PDF-1.4\n%%EOF\n")
        return {"ok": True, "dest": str(path)}

    monkeypatch.setattr(session, "browser_download", download)
    outcome = seeed.ADAPTER.acquire({"title": "Board", "url": seeed.BASE_URL + "/board"})
    assert outcome["ok"] and outcome["artifact_path"] == str(dest)
    assert outcome["artifact_kind"] == "document" and outcome["format"] == "pdf"

    def download_html(url, site, path):
        path.write_bytes(b"<html>Sign in</html>")
        return {"ok": True, "dest": str(path)}

    monkeypatch.setattr(session, "browser_download", download_html)
    rejected = seeed.ADAPTER.acquire({"title": "Board", "url": seeed.BASE_URL + "/board"})
    assert not rejected["ok"] and rejected["reason"] == "parse_failed"
    assert "artifact_path" not in rejected
