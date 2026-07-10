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
    from engine import paths as engine_paths
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


def test_seeed_header_signin_is_not_a_wall(seeed_mod, monkeypatch):
    session, seeedstudio = seeed_mod
    page = "<a>Sign In</a><a>Log In</a>" + SEEED_SEARCH_HTML
    monkeypatch.setattr(session, "browser_fetch",
                        lambda url, s: {"ok": True, "content": page})
    hits = seeedstudio.ADAPTER.search("esp32")
    assert len(hits) == 2 and all(h.get("ok") for h in hits)
