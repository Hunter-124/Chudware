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

import io
import json
import zipfile

import pytest


MAXON_SEARCH_HTML = r"""<script id="__NEXT_DATA__" type="application/json">{"props": {"pageProps": {"productSearchData": {"products": [{"foundWords": [], "id": "303490", "keywords": [], "position": 1, "record": {"ZusatzDisplayAttributes": "", "__ORIG_POSITION__": "2", "ProduktStatusCode": "phaseOut", "FFAutomaticSearchOptimization": "", "ProduktStatus": "NRND", "DQAttributes": "", "FFCheckoutCount": "0", "Name": "DC Motor Cable", "Masterartikelnummer": "mmc Kabel", "Kategorie3": "", "FFAfterSearchReorder": "", "Kategorie4": "", "Kategorie1": "Accessories", "Kategorie2": "Cable", "Zusatzattribute": "|configurable=No|configurableCode=false|", "EUR~~€": "", "Beratungsintensiv": "No", "Deeplink": "/maxon/view/product/accessory/1-mmckabel/303490", "RecommendedProductCode": "false", "Beschreibung": "Length = 3 m For use with 390003", "ProductType": "accessory", "BeratungsintensivCode": "false", "ImageURL": "/medias/sys_master/root/8800509460510/Produktbild-EPOS-KABEL-303490-THUMBNAIL.jpg", "Lieferbarkeit": "2", "Artikelnummer": "303490", "Preis": "|JPY~~JPY=4352.00|EUR~~€=26.95|CHF~~CHF=25.60|USD~~$=34.56|CNY~~RMB=284.42|GBP~~£=24.99|", "FFCliplisterQuality": "", "RecommendedProduct": "No"}, "searchSimilarity": 97.98, "simiMalusAdd": 0}, {"foundWords": [], "id": "403962", "keywords": [], "position": 2, "record": {"ZusatzDisplayAttributes": "", "__ORIG_POSITION__": "1", "ProduktStatusCode": "active", "FFAutomaticSearchOptimization": "", "ProduktStatus": "Active", "DQAttributes": "", "FFCheckoutCount": "0", "Name": "DC Motor Cable", "Masterartikelnummer": "mmc Kabel", "Kategorie3": "", "FFAfterSearchReorder": "", "Kategorie4": "", "Kategorie1": "Accessories", "Kategorie2": "Cable", "Zusatzattribute": "|configurable=No|configurableCode=false|", "EUR~~€": "", "Beratungsintensiv": "No", "Deeplink": "/maxon/view/product/accessory/1-mmckabel/403962", "RecommendedProductCode": "true", "Beschreibung": "Length = 1.5 m For use with 403112", "ProductType": "accessory", "BeratungsintensivCode": "false", "ImageURL": "/medias/sys_master/root/8798223302686/PRODUKTBILD-ESCON-Digital-Kabel-403962-Thumbnail.jpg", "Lieferbarkeit": "2", "Artikelnummer": "403962", "Preis": "|JPY~~JPY=1071.00|EUR~~€=6.63|CHF~~CHF=6.30|USD~~$=8.51|CNY~~RMB=69.99|GBP~~£=6.15|", "FFCliplisterQuality": "", "RecommendedProduct": "Yes"}, "searchSimilarity": 97.98, "simiMalusAdd": 0}]}}}}</script>"""


@pytest.fixture
def maxon_mod(monkeypatch, tmp_path):
    from services.browser.adapters import maxon, paths
    monkeypatch.setattr(paths, "BROWSER_DOWNLOADS", tmp_path)
    return maxon


def _fetch(monkeypatch, module, content, *, final_url=None):
    calls = []

    def fetch(url, site):
        calls.append((url, site))
        return {"ok": True, "content": content, "final_url": final_url or url}

    monkeypatch.setattr(module._session, "browser_fetch", fetch)
    return calls


def _download(monkeypatch, module, content):
    calls = []

    def download(url, site, dest):
        calls.append((url, site))
        dest.write_bytes(content)
        return {"ok": True, "dest": str(dest), "session_profile_used": "fixture"}

    monkeypatch.setattr(module._session, "browser_download", download)
    return calls


STEP = b"ISO-10303-21;\nHEADER;\nENDSEC;\nDATA;\nENDSEC;\nEND-ISO-10303-21;\n"
PRODUCT = "https://www.maxongroup.us/maxon/view/product/motor/108828"


def _zip(files):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return stream.getvalue()


def test_maxon_search_parses_real_next_data(maxon_mod, monkeypatch):
    calls = _fetch(monkeypatch, maxon_mod, MAXON_SEARCH_HTML)
    hits = maxon_mod.ADAPTER.search("dc motor & 24V/#")
    assert calls == [("https://www.maxongroup.com/en-us/search?q=dc+motor+%26+24V%2F%23", "maxon")]
    assert len(hits) == 2
    first, second = hits
    assert first["site"] == "maxon" and first["kind"] == "product"
    assert "maxongroup.us" in maxon_mod.ADAPTER.hosts
    assert first["sku"] == first["mpn"] == "303490"
    assert first["manufacturer"] == "maxon"
    assert first["title"] == "DC Motor Cable"
    assert first["url"] == "https://www.maxongroup.us/maxon/view/product/accessory/1-mmckabel/303490"
    assert first["category"] == "Accessories / Cable"
    assert first["price"] == 34.56 and first["currency"] == "USD"
    assert first["thumbnail"] == ("https://www.maxongroup.us/medias/sys_master/root/"
                                  "8800509460510/Produktbild-EPOS-KABEL-303490-THUMBNAIL.jpg")
    assert first["model"] == {"available": None, "formats": []}
    assert second["sku"] == "403962" and second["price"] == 8.51
    assert second["thumbnail"].endswith("PRODUKTBILD-ESCON-Digital-Kabel-403962-Thumbnail.jpg")


def test_maxon_unknowns_and_zero_price_are_preserved(maxon_mod, monkeypatch):
    records = [{"record": {"Name": "Motor &amp; gear", "Deeplink": "/p/one",
                           "Artikelnummer": "A", "ImageURL": "/img/logo.png",
                           "Preis": "|EUR~~€=1.25|USD~~$=0.00|"}},
               {"record": {"Name": "Motor B", "Deeplink": "/p/two",
                           "Preis": "|EUR~~€=22.00|", "ImageURL": "data:image/png,x"}}]
    data = {"props": {"pageProps": {"productSearchData": {"products": records}}}}
    _fetch(monkeypatch, maxon_mod, "<script type='application/json' id='__NEXT_DATA__'>"
           + json.dumps(data) + "</script>")
    one, two = maxon_mod.ADAPTER.search("gear")
    assert one["title"] == "Motor & gear" and one["price"] == 0.0 and one["currency"] == "USD"
    assert one["thumbnail"] is None
    assert two["price"] is None and two["currency"] is None and two["thumbnail"] is None
    assert two["sku"] is None and two["mpn"] is None


@pytest.mark.parametrize("content", [
    "<html>unrecognized page</html>",
    "<script id='__NEXT_DATA__'>{bad json}</script>",
    "<script id='__NEXT_DATA__'>{\"props\":{}}</script>",
    "<script id='__NEXT_DATA__'>{\"props\":{\"pageProps\":{\"productSearchData\":{\"products\":[{}]}}}}</script>",
])
def test_maxon_invalid_results_are_not_empty(maxon_mod, monkeypatch, content):
    _fetch(monkeypatch, maxon_mod, content)
    failure, = maxon_mod.ADAPTER.search("motor")
    assert failure["site"] == "maxon" and failure["op"] == "search"
    assert failure["reason"] == "parse_failed"


def test_maxon_explicit_empty_and_search_walls(maxon_mod, monkeypatch):
    _fetch(monkeypatch, maxon_mod,
           '<script id="__NEXT_DATA__">{"props":{"pageProps":{"productSearchData":{"products":[]}}}}</script>')
    assert maxon_mod.ADAPTER.search("none") == []
    _fetch(monkeypatch, maxon_mod, "<h1>Verify you are human</h1>")
    assert maxon_mod.ADAPTER.search("motor")[0]["reason"] == "captcha"
    _fetch(monkeypatch, maxon_mod, "<h1>Log in to maxon</h1>")
    assert maxon_mod.ADAPTER.search("motor")[0]["reason"] == "login_required"


def test_maxon_transport_failure_keeps_context(maxon_mod, monkeypatch):
    monkeypatch.setattr(maxon_mod._session, "browser_fetch", lambda *_: {
        "ok": False, "reason": "rate_limited", "status": 429, "retry_after_s": 30})
    failure, = maxon_mod.ADAPTER.search("motor")
    assert failure["reason"] == "rate_limited" and failure["status"] == 429
    assert failure["retry_after_s"] == 30 and failure["site"] == "maxon"
    assert failure["op"] == "search" and failure["url"].endswith("?q=motor")


def test_maxon_acquire_direct_cad_and_pdf(maxon_mod, monkeypatch):
    _fetch(monkeypatch, maxon_mod, '<a href="/files/108828.step">CAD model</a>')
    calls = _download(monkeypatch, maxon_mod, STEP)
    out = maxon_mod.ADAPTER.acquire({"url": PRODUCT, "sku": "108828", "manufacturer": "maxon"})
    assert out["ok"] and out["artifact_kind"] == "cad" and out["format"] == "step"
    assert out["sku"] == "108828" and out["session_profile_used"] == "fixture"
    assert calls == [("https://www.maxongroup.us/files/108828.step", "maxon")]
    _fetch(monkeypatch, maxon_mod, '<a href="/files/catalog-108828.pdf">Catalog page</a>')
    _download(monkeypatch, maxon_mod, b"%PDF-1.7\nfixture\n%%EOF")
    doc = maxon_mod.ADAPTER.acquire({"url": PRODUCT})
    assert doc["ok"] and doc["artifact_kind"] == "document" and doc["format"] == "pdf"


@pytest.mark.parametrize("extension,content", [
    ("step", b"<html>login form</html>"), ("step", b"%PDF-1.7\nnot CAD"),
    ("step", b""), ("pdf", b"<html>error</html>"),
    ("zip", _zip({"readme.txt": b"no geometry"})),
    ("zip", _zip({"../part.step": STEP})),
])
def test_maxon_rejects_invalid_artifacts(maxon_mod, monkeypatch, extension, content):
    _fetch(monkeypatch, maxon_mod, f'<a href="/file.{extension}">Datasheet CAD</a>')
    _download(monkeypatch, maxon_mod, content)
    out = maxon_mod.ADAPTER.acquire({"url": PRODUCT})
    assert out["ok"] is False and out["reason"] == "parse_failed"


def test_maxon_single_and_ambiguous_archives(maxon_mod, monkeypatch):
    _fetch(monkeypatch, maxon_mod, '<a href="/file.zip">CAD model (STEP)</a>')
    _download(monkeypatch, maxon_mod, _zip({"motor.step": STEP, "readme.txt": b"fixture"}))
    one = maxon_mod.ADAPTER.acquire({"url": PRODUCT})
    assert one["ok"] and one["artifact_kind"] == "cad" and one["format"] == "step"
    assert one["artifact_path"].endswith(".step") and one["archive_member"] == "motor.step"
    requested = maxon_mod.ADAPTER.acquire({"url": PRODUCT, "format": "zip"})
    assert requested["ok"] and requested["artifact_kind"] == "archive" and requested["format"] == "zip"
    _download(monkeypatch, maxon_mod, _zip({"A.step": STEP, "B.step": STEP}))
    many = maxon_mod.ADAPTER.acquire({"url": PRODUCT, "sku": "A"})
    assert many["ok"] and many["artifact_kind"] == "archive" and many["format"] == "zip"
    assert "configuration" in many["archive_note"] and many["artifact_path"].endswith(".zip")
    requested_model = maxon_mod.ADAPTER.acquire({"url": PRODUCT, "sku": "A", "format": "step"})
    assert not requested_model["ok"] and requested_model["reason"] == "unsupported"


def test_maxon_does_not_guess_configuration_or_requested_format(maxon_mod, monkeypatch):
    _fetch(monkeypatch, maxon_mod,
           '<a href="/files/108828.step">108828 STEP</a><a href="/files/other.step">other</a>')
    calls = _download(monkeypatch, maxon_mod, STEP)
    assert maxon_mod.ADAPTER.acquire({"url": PRODUCT})["reason"] == "unsupported"
    out = maxon_mod.ADAPTER.acquire({"url": PRODUCT, "sku": "108828"})
    assert out["ok"] and calls[-1][0].endswith("/108828.step")
    assert maxon_mod.ADAPTER.acquire({"url": PRODUCT, "format": "iges"})["reason"] == "not_found"
    assert maxon_mod.ADAPTER.acquire({"url": PRODUCT, "kind": "series"})["reason"] == "unsupported"


def test_maxon_sku_prefix_never_matches_another_variant(maxon_mod, monkeypatch):
    _fetch(monkeypatch, maxon_mod,
           '<a href="/files/108828-extra.step">108828-extra</a><a href="/files/other.step">other</a>')
    calls = _download(monkeypatch, maxon_mod, STEP)
    out = maxon_mod.ADAPTER.acquire({"url": PRODUCT, "sku": "108828"})
    assert not out["ok"] and out["reason"] == "unsupported" and not calls


def test_maxon_explicit_document_choice_is_respected(maxon_mod, monkeypatch):
    _fetch(monkeypatch, maxon_mod,
           '<a href="/files/catalog.pdf">Catalog</a><a href="/files/manual.pdf">Manual</a>')
    calls = _download(monkeypatch, maxon_mod, b"%PDF-1.7\nfixture\n%%EOF")
    out = maxon_mod.ADAPTER.acquire({"url": PRODUCT, "format": "pdf", "download_url": "/files/manual.pdf"})
    assert out["ok"] and out["artifact_kind"] == "document"
    assert calls[-1][0].endswith("/manual.pdf")
