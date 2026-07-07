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
    assert hits[0]["part_number"] == "M0801010000_threaded-rotary-shafts"
    assert hits[0]["kind"] == "series"
    # sku is empty on the real results page — no invented value is emitted.
    assert "sku" not in hits[0]

    assert hits[1]["title"] == "Threaded Linear Shafts"
    assert hits[1]["url"] == (
        "https://us.misumi-ec.com/vona2/mech/M0100000000/M0101000000/threaded-linear-shafts/")
    assert hits[1]["part_number"] == "M0101000000_threaded-linear-shafts"


def test_misumi_search_login_wall_surfaces_ask_user(misumi_mods, monkeypatch):
    misumi, session = misumi_mods
    monkeypatch.setattr(session, "browser_fetch",
                        lambda url, site: {"ok": True, "content": MISUMI_LOGIN_WALL_HTML})

    hits = misumi.ADAPTER.search("shaft")

    assert len(hits) == 1
    assert hits[0]["ok"] is False
    assert hits[0]["reason"] == "login_required"


def test_misumi_search_no_next_data_returns_empty(misumi_mods, monkeypatch):
    # Honest empty: no __NEXT_DATA__ blob → [], never speculative regex.
    misumi, session = misumi_mods
    monkeypatch.setattr(session, "browser_fetch",
                        lambda url, site: {"ok": True, "content": "<html><body>nope</body></html>"})

    assert misumi.ADAPTER.search("shaft") == []


def test_misumi_search_propagates_fetch_failure(misumi_mods, monkeypatch):
    misumi, session = misumi_mods
    monkeypatch.setattr(session, "browser_fetch",
                        lambda url, site: {"ok": False, "reason": "no_profile", "hint": "x"})

    hits = misumi.ADAPTER.search("shaft")
    assert hits == [{"ok": False, "reason": "no_profile", "hint": "x"}]
