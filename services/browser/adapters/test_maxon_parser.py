"""Fixture-based parser test for the maxon search adapter.

MAXON_SEARCH_HTML holds a VERBATIM 2-product slice of the ``__NEXT_DATA__`` JSON
blob from a real maxon shop search page fetched live 2026-07-10 from
``/en-us/search?q=dc+motor`` (HTTP 200). The old ``/maxon/view/search?q=`` guess
404'd; the shop is a Next.js app that embeds its results at
``props.pageProps.productSearchData.products[].record`` — clean structured
records (``Name``/``Artikelnummer``/``Deeplink``/``Kategorie1..4``/``Preis``), no
server-rendered product cards. The public catalog search parses without any login.

Offline style mirrors test_cui_parser.py: session.browser_fetch monkeypatched.
"""
from __future__ import annotations

import importlib

import pytest


MAXON_SEARCH_HTML = r"""<script id="__NEXT_DATA__" type="application/json">{"props": {"pageProps": {"productSearchData": {"products": [{"foundWords": [], "id": "303490", "keywords": [], "position": 1, "record": {"ZusatzDisplayAttributes": "", "__ORIG_POSITION__": "2", "ProduktStatusCode": "phaseOut", "FFAutomaticSearchOptimization": "", "ProduktStatus": "NRND", "DQAttributes": "", "FFCheckoutCount": "0", "Name": "DC Motor Cable", "Masterartikelnummer": "mmc Kabel", "Kategorie3": "", "FFAfterSearchReorder": "", "Kategorie4": "", "Kategorie1": "Accessories", "Kategorie2": "Cable", "Zusatzattribute": "|configurable=No|configurableCode=false|", "EUR~~€": "", "Beratungsintensiv": "No", "Deeplink": "/maxon/view/product/accessory/1-mmckabel/303490", "RecommendedProductCode": "false", "Beschreibung": "Length = 3 m For use with 390003", "ProductType": "accessory", "BeratungsintensivCode": "false", "ImageURL": "/medias/sys_master/root/8800509460510/Produktbild-EPOS-KABEL-303490-THUMBNAIL.jpg", "Lieferbarkeit": "2", "Artikelnummer": "303490", "Preis": "|JPY~~JPY=4352.00|EUR~~€=26.95|CHF~~CHF=25.60|USD~~$=34.56|CNY~~RMB=284.42|GBP~~£=24.99|", "FFCliplisterQuality": "", "RecommendedProduct": "No"}, "searchSimilarity": 97.98, "simiMalusAdd": 0}, {"foundWords": [], "id": "403962", "keywords": [], "position": 2, "record": {"ZusatzDisplayAttributes": "", "__ORIG_POSITION__": "1", "ProduktStatusCode": "active", "FFAutomaticSearchOptimization": "", "ProduktStatus": "Active", "DQAttributes": "", "FFCheckoutCount": "0", "Name": "DC Motor Cable", "Masterartikelnummer": "mmc Kabel", "Kategorie3": "", "FFAfterSearchReorder": "", "Kategorie4": "", "Kategorie1": "Accessories", "Kategorie2": "Cable", "Zusatzattribute": "|configurable=No|configurableCode=false|", "EUR~~€": "", "Beratungsintensiv": "No", "Deeplink": "/maxon/view/product/accessory/1-mmckabel/403962", "RecommendedProductCode": "true", "Beschreibung": "Length = 1.5 m For use with 403112", "ProductType": "accessory", "BeratungsintensivCode": "false", "ImageURL": "/medias/sys_master/root/8798223302686/PRODUKTBILD-ESCON-Digital-Kabel-403962-Thumbnail.jpg", "Lieferbarkeit": "2", "Artikelnummer": "403962", "Preis": "|JPY~~JPY=1071.00|EUR~~€=6.63|CHF~~CHF=6.30|USD~~$=8.51|CNY~~RMB=69.99|GBP~~£=6.15|", "FFCliplisterQuality": "", "RecommendedProduct": "Yes"}, "searchSimilarity": 97.98, "simiMalusAdd": 0}]}}}}</script>"""


@pytest.fixture()
def maxon_mod(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    from engine import paths as engine_paths
    importlib.reload(engine_paths)
    from services.browser import profiles, session
    importlib.reload(profiles)
    importlib.reload(session)
    from services.browser import adapters as adapters_pkg
    importlib.reload(adapters_pkg)
    from services.browser.adapters import maxon
    importlib.reload(maxon)
    yield session, maxon
    monkeypatch.delenv("APPDATA", raising=False)
    importlib.reload(engine_paths)


def test_maxon_search_parses_next_data(maxon_mod, monkeypatch):
    session, maxon = maxon_mod
    monkeypatch.setattr(session, "browser_fetch",
                        lambda url, s: {"ok": True, "content": MAXON_SEARCH_HTML})

    hits = maxon.ADAPTER.search("dc motor")

    assert len(hits) == 2
    assert all(h["ok"] is True and h["site"] == "maxon" for h in hits)
    assert all(h["manufacturer"] == "maxon" for h in hits)

    first = hits[0]
    assert first["part_number"] == "303490"
    assert first["title"] == "DC Motor Cable"
    assert first["url"] == (
        "https://www.maxongroup.com/maxon/view/product/accessory/1-mmckabel/303490")
    assert first["category"] == "Accessories / Cable"
    assert first["price"] == "34.56"

    assert hits[1]["part_number"] == "403962"
    assert hits[1]["price"] == "8.51"


def test_maxon_search_url_is_en_us_search_endpoint():
    from services.browser.adapters import maxon
    assert maxon.SEARCH_URL_TMPL == "https://www.maxongroup.com/en-us/search?q={query}"


def test_maxon_empty_next_data_is_honest(maxon_mod, monkeypatch):
    session, maxon = maxon_mod
    monkeypatch.setattr(session, "browser_fetch",
                        lambda url, s: {"ok": True, "content": "<html>no data</html>"})
    assert maxon.ADAPTER.search("dc motor") == []
