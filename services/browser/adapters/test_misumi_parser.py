"""services/browser/adapters/test_misumi_parser.py — MISUMI search() parser.

Fixture-based test for MisumiAdapter.search(). The MISUMI results page is a
Next.js app that embeds search results server-side in a
``<script id="__NEXT_DATA__" type="application/json">`` blob (live-verified
2026-07-07 against a real saved page fetched from
``https://us.misumi-ec.com/vona2/result/?Keyword=shaft``).

MISUMI_NEXT_DATA_HTML below is a VERBATIM slice of that saved page: the real
``<script id="__NEXT_DATA__">`` wrapper and the real
``props.pageProps.hawksearchKeywordSearchResponse.Results`` path, trimmed to the
first two real ``Results`` entries (real Document field names and values — the
single-element-list shape, the empty ``sku``, the leading-slash-relative
``url_detail`` and the ``unique_id`` series code). Nothing here is invented.

``browser_fetch`` is monkeypatched directly on the session module the adapter
imports (it calls ``_session.browser_fetch(url, site)`` positionally), mirroring
the McMaster tests in test_adapters.py.
"""
from __future__ import annotations

import importlib

import pytest


@pytest.fixture()
def misumi_mods(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    import services.browser.session as session
    from services.browser.adapters import misumi
    importlib.reload(misumi)
    yield misumi, session
    monkeypatch.delenv("APPDATA", raising=False)


# --- VERBATIM slice of the real saved page (2 real Results entries) -----------
MISUMI_NEXT_DATA_HTML = (
    '<script id="__NEXT_DATA__" type="application/json">'
    '{"props":{"pageProps":{"hawksearchKeywordSearchResponse":{"Results":['
    '{"DocId":"M0801010000_threaded-rotary-shafts","Score":100.0,"Document":{'
    '"image":["/web/ec_ctgr/M0801010000/threaded-rotary-shafts.jpg?_=1097"],'
    '"url_detail":["/vona2/mech/M0800000000/M0801000000/M0801010000/threaded-rotary-shafts/"],'
    '"unique_id":["M0801010000_threaded-rotary-shafts"],'
    '"sys_item_type":["npnl"],"stock_item_flag":["0"],"brandname":["Approved Vendor"],'
    '"material":["1045 Carbon Steel","304 Stainless Steel","4137 Alloy Steel"],'
    '"cad_type":["2D","3D"],"name":["Threaded Rotary Shafts"],'
    '"sku":[""],"category":["M0801010000","M0801000000","M0800000000","mech","9999999999999"]}},'
    '{"DocId":"M0101000000_threaded-linear-shafts","Score":98.0,"Document":{'
    '"image":["/web/ec_ctgr/M0101000000/threaded-linear-shafts.jpg?_=1097"],'
    '"url_detail":["/vona2/mech/M0100000000/M0101000000/threaded-linear-shafts/"],'
    '"unique_id":["M0101000000_threaded-linear-shafts"],'
    '"cad_type":["2D","3D"],"name":["Threaded Linear Shafts"],'
    '"sku":[""],"category":["M0101000000","M0100000000","mech","9999999999999"]}}'
    ']}}},"page":"/result"}'
    '</script>'
)

MISUMI_LOGIN_WALL_HTML = '<html><body><div class="login-form">Please log in</div></body></html>'


def test_misumi_search_parses_next_data_results(misumi_mods, monkeypatch):
    misumi, session = misumi_mods
    monkeypatch.setattr(session, "browser_fetch",
                        lambda url, site: {"ok": True, "content": MISUMI_NEXT_DATA_HTML})

    hits = misumi.ADAPTER.search("shaft")

    assert len(hits) == 2
    assert hits[0]["title"] == "Threaded Rotary Shafts"
    assert hits[0]["url"] == (
        "https://us.misumi-ec.com"
        "/vona2/mech/M0800000000/M0801000000/M0801010000/threaded-rotary-shafts/")
    assert hits[0]["site"] == "misumi"
    assert hits[0]["series_id"] == "M0801010000_threaded-rotary-shafts"
    assert hits[0]["kind"] == "series"
    assert hits[0]["sku"] is None
    assert hits[0]["price"] is None and hits[0]["currency"] is None
    assert hits[0]["thumbnail"] == (
        "https://us.misumi-ec.com/web/ec_ctgr/M0801010000/threaded-rotary-shafts.jpg?_=1097")
    assert hits[0]["model"]["available"] is True

    assert hits[1]["title"] == "Threaded Linear Shafts"
    assert hits[1]["url"] == (
        "https://us.misumi-ec.com/vona2/mech/M0100000000/M0101000000/threaded-linear-shafts/")
    assert hits[1]["series_id"] == "M0101000000_threaded-linear-shafts"


# Trimmed from the current live shaft search's first Document (2026-11-04).
MISUMI_CURRENT_SEARCH_HTML = (
    '<script id="__NEXT_DATA__" type="application/json">'
    '{"props":{"pageProps":{"hawksearchKeywordSearchResponse":{"Results":[{"Document":{'
    '"image":["/web/ec_ctgr/M0101000000/threaded-linear-shafts.jpg?_=1705"],'
    '"url_detail":["/vona2/mech/M0100000000/M0101000000/threaded-linear-shafts/"],'
    '"unique_id":["M0101000000__threaded-linear-shafts"],"brandname":["Approved Vendor"],'
    '"cad_type":["2D","3D"],"name":["Threaded Linear Shafts"],"sku":[""]}}]}}}}</script>'
)


def test_misumi_current_live_result_slice(misumi_mods, monkeypatch):
    misumi, session = misumi_mods
    monkeypatch.setattr(session, "browser_fetch",
                        lambda *a: {"ok": True, "content": MISUMI_CURRENT_SEARCH_HTML})
    hit = misumi.ADAPTER.search("shaft")[0]
    assert hit["series_id"] == "M0101000000__threaded-linear-shafts"
    assert hit["kind"] == "series" and hit["sku"] is None
    assert hit["thumbnail"] == (
        "https://us.misumi-ec.com/web/ec_ctgr/M0101000000/threaded-linear-shafts.jpg?_=1705")


def test_misumi_search_login_wall_surfaces_ask_user(misumi_mods, monkeypatch):
    misumi, session = misumi_mods
    monkeypatch.setattr(session, "browser_fetch",
                        lambda url, site: {"ok": True, "content": MISUMI_LOGIN_WALL_HTML})

    hits = misumi.ADAPTER.search("shaft")

    assert len(hits) == 1
    assert hits[0]["ok"] is False
    assert hits[0]["reason"] == "login_required"


def test_misumi_search_missing_data_is_parse_failure(misumi_mods, monkeypatch):
    misumi, session = misumi_mods
    monkeypatch.setattr(session, "browser_fetch",
                        lambda url, site: {"ok": True, "content": "<html><body>nope</body></html>"})

    assert misumi.ADAPTER.search("shaft")[0]["reason"] == "parse_failed"


def test_misumi_search_propagates_fetch_failure(misumi_mods, monkeypatch):
    misumi, session = misumi_mods
    monkeypatch.setattr(session, "browser_fetch",
                        lambda url, site: {"ok": False, "reason": "transport_failed", "hint": "x"})

    hits = misumi.ADAPTER.search("shaft")
    assert hits[0]["reason"] == "transport_failed"
    assert hits[0]["hint"] == "x"
    assert hits[0]["site"] == "misumi" and hits[0]["op"] == "search"


def test_misumi_query_encoding_and_product_json(misumi_mods, monkeypatch):
    misumi, session = misumi_mods
    seen = {}
    content = ('<script type="application/json" id="__NEXT_DATA__">'
               '{"props":{"pageProps":{"hawksearchKeywordSearchResponse":{"Results":['
               '{"Document":{"name":["Guide"],"url_detail":["/product/g1"],"sku":["G1"],'
               '"brandname":["ACME"],"image":["//cdn.example.com/g1.png"],'
               '"material":["Steel"],"price":["1,234.50"],"currency":["USD"]}}'
               ']}}}}</script>')
    def fetch(url, site):
        seen["url"] = url
        return {"ok": True, "content": content}
    monkeypatch.setattr(session, "browser_fetch", fetch)
    hit = misumi.ADAPTER.search("M4 / steel & nut +")[0]
    assert seen["url"].endswith("Keyword=M4+%2F+steel+%26+nut+%2B")
    assert (hit["kind"], hit["sku"], hit["manufacturer"]) == ("product", "G1", "ACME")
    assert hit["thumbnail"] == "https://cdn.example.com/g1.png"
    assert (hit["price"], hit["currency"], hit["material"]) == (1234.5, "USD", "Steel")


@pytest.mark.parametrize(("content", "reason"), [
    ('<title>Access Denied</title>', "blocked"),
    ('<html>Please complete the security check</html>', "captcha"),
    ('<script id="__NEXT_DATA__">invalid json</script>', "parse_failed"),
    ('<script id="__NEXT_DATA__">{"props":{"pageProps":{"npnlCategoryData":{}}}}</script>', "unsupported"),
])
def test_misumi_reports_unusable_pages(misumi_mods, monkeypatch, content, reason):
    misumi, session = misumi_mods
    monkeypatch.setattr(session, "browser_fetch", lambda *a: {"ok": True, "content": content})
    failure = misumi.ADAPTER.search("shaft")[0]
    assert (failure["site"], failure["op"], failure["reason"]) == ("misumi", "search", reason)


def test_misumi_confirmed_empty_results(misumi_mods, monkeypatch):
    misumi, session = misumi_mods
    content = ('<script id="__NEXT_DATA__">'
               '{"props":{"pageProps":{"hawksearchKeywordSearchResponse":{"Results":[]}}}}</script>')
    monkeypatch.setattr(session, "browser_fetch", lambda *a: {"ok": True, "content": content})
    assert misumi.ADAPTER.search("unknown") == []


MISUMI_CONFIGURED_HTML = (
    '<span class="part-number">PSFJ6-100</span><span class="price">$4.56</span>'
    '<a href="/cad/PSFJ6-100.step?token=1">STEP</a>'
)


def test_misumi_acquire_only_selected_configured_sku(misumi_mods, monkeypatch):
    misumi, session = misumi_mods
    monkeypatch.setattr(session, "browser_fetch",
                        lambda *a: {"ok": True, "content": MISUMI_CONFIGURED_HTML})
    def download(url, site, dest):
        assert url == "https://us.misumi-ec.com/cad/PSFJ6-100.step?token=1"
        dest.write_bytes(b"ISO-10303-21;\nHEADER;\nENDSEC;\nEND-ISO-10303-21;")
        return {"ok": True, "dest": str(dest)}
    monkeypatch.setattr(session, "browser_download", download)
    result = {"url": "https://us.misumi-ec.com/vona2/detail/example/", "sku": "PSFJ6-100"}
    out = misumi.ADAPTER.acquire(result)
    assert out["ok"] is True
    assert (out["sku"], out["artifact_kind"], out["format"]) == ("PSFJ6-100", "cad", "step")
    assert (out["price"], out["currency"]) == (4.56, "USD")


def test_misumi_acquire_requires_selection_and_rejects_mismatch(misumi_mods, monkeypatch):
    misumi, session = misumi_mods
    monkeypatch.setattr(session, "browser_fetch",
                        lambda *a: {"ok": True, "content": MISUMI_CONFIGURED_HTML})
    monkeypatch.setattr(session, "browser_download", lambda *a: pytest.fail("wrong SKU downloaded"))
    base = {"url": "https://us.misumi-ec.com/vona2/detail/example/", "kind": "series"}
    unselected = misumi.ADAPTER.acquire(base)
    assert unselected["reason"] == "unsupported" and unselected["ask_user"]["required"]
    mismatch = misumi.ADAPTER.acquire({**base, "sku": "PSFJ6-101"})
    assert mismatch["reason"] == "unsupported"


def test_misumi_rejects_non_cad_response(misumi_mods, monkeypatch):
    misumi, session = misumi_mods
    monkeypatch.setattr(session, "browser_fetch",
                        lambda *a: {"ok": True, "content": MISUMI_CONFIGURED_HTML})
    def download(url, site, dest):
        dest.write_bytes(b"%PDF-1.7 not CAD")
        return {"ok": True, "dest": str(dest)}
    monkeypatch.setattr(session, "browser_download", download)
    out = misumi.ADAPTER.acquire({"url": "https://us.misumi-ec.com/vona2/detail/example/",
                                  "sku": "PSFJ6-100"})
    assert out["reason"] == "parse_failed" and "artifact_path" not in out
