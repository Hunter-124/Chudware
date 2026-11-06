"""services/browser/adapters/test_mcmaster_parser.py — McMaster search() parser.

Fixture-based test for McMasterAdapter.search() against a VERBATIM slice of a
real McMaster-Carr search-results page, fetched live 2026-07-11 from
``https://www.mcmaster.com/m8-socket-head-cap-screw/`` via the CEF headless
daemon (the flat "/{query}/" search URL the adapter uses).

The bug this covers: the old adapter parsed the top-nav mega-menu DEPARTMENT
links (``/products/fastening-and-joining/`` …) — the "26 category landing
pages" — so ``acquire`` never had a product page. The real fix reads the
breadcrumb's canonical product-family link for the CATEGORY slug
(``product-family~~socket-head-screws~~<hash>``) and the result tiles
(``<a class="_selectable_<hash>" href="<family-slug>~~/?s=...">`` with the title
in ``<img alt>``), then rebuilds each into
``/products/<CATEGORY>/<family-slug>~~/`` — the ONLY URL that renders the per-SKU
table headless (live-verified: 735 KB / 266 part numbers cold; the bare slug and
the breadcrumb URL both render only a ~360 KB nav shell).

MCMASTER_SEARCH_HTML below is a verbatim slice of that saved page: the real
breadcrumb ``product-family`` anchor and the first three real result tiles
(real ``_selectable_`` class, real ``~~/?s=`` hrefs, real ``<img alt>`` titles),
plus one ``~/`` SUBCATEGORY tile that must NOT be emitted as a product family.
Nothing here is invented.

``browser_fetch`` is monkeypatched directly on the session module the adapter
imports (it calls ``_session.browser_fetch(url, site)`` positionally), mirroring
the MISUMI parser test.
"""
from __future__ import annotations

import importlib

import pytest


@pytest.fixture()
def mcmaster_mods(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    import services.browser.session as session
    from services.browser.adapters import mcmaster
    importlib.reload(mcmaster)
    yield mcmaster, session
    monkeypatch.delenv("APPDATA", raising=False)


# --- VERBATIM slice of the real saved search page (2026-07-11) ----------------
MCMASTER_SEARCH_HTML = (
    '<nav aria-label="Breadcrumb">'
    '<a href="/products/product-category~~fastening-and-joining~~2q3Rr7zd/?s=m8-socket-head-cap-screw">Fastening &amp; Joining</a>'
    '<a href="/products/product-line~~screws-and-bolts~~2q3Rr7zj/?s=m8-socket-head-cap-screw">Screws &amp; Bolts</a>'
    '<a href="/products/product-family~~socket-head-screws~~2q3Rqx2P/?s=m8-socket-head-cap-screw">Socket Head Screws</a>'
    '</nav>'
    '<div><a class="_selectable_srkjh_1" href="steel-socket-head-screws~~/?s=m8-socket-head-cap-screw">'
    '<div class="_outerContainer_t8yku_1"><div class="_imageContainer_t8yku_19">'
    '<img alt="Steel Socket Head Screws" title="" srcset="/mvC/Contents/gfx/ImageCache/912/91290A432_x@100p.png 1x"></div></div></a></div>'
    '<div><a class="_selectable_srkjh_1" href="stainless-steel-socket-head-screws~~/?s=m8-socket-head-cap-screw">'
    '<div class="_imageContainer_t8yku_19"><img alt="Stainless Steel Socket Head Screws" title=""></div></a></div>'
    '<div><a class="_selectable_srkjh_1" href="low-profile-steel-socket-head-screws~~/?s=m8-socket-head-cap-screw">'
    '<div class="_imageContainer_t8yku_19"><img alt="Low-Profile Steel Socket Head Screws" title=""></div></a></div>'
    # A "~/" SUBCATEGORY tile (single tilde) — NOT a product family; must be skipped.
    '<div><a class="_selectable_srkjh_1" href="flat-head-screws-2~/?s=m8-socket-head-cap-screw">'
    '<div class="_imageContainer_t8yku_19"><img alt="Flat Head Screws" title=""></div></a></div>'
)

MCMASTER_LOGIN_WALL_HTML = '<html><body>Sign In <div id="SignInEmail"></div></body></html>'


def test_mcmaster_search_yields_product_family_urls(mcmaster_mods, monkeypatch):
    mcmaster, session = mcmaster_mods
    monkeypatch.setattr(session, "browser_fetch",
                        lambda url, site: {"ok": True, "content": MCMASTER_SEARCH_HTML})

    hits = mcmaster.ADAPTER.search("m8 socket head cap screw")

    # Only the three "~~/" family tiles — the "~/" subcategory is not emitted.
    assert len(hits) == 3
    assert all(h["kind"] == "series" for h in hits)
    assert all(h["site"] == "mcmaster" for h in hits)

    # URLs are real product-family SKU-TABLE pages (rebuilt with the CATEGORY
    # slug from the breadcrumb), NOT department landing pages like
    # "/products/fastening-and-joining/".
    assert hits[0]["title"] == "Steel Socket Head Screws"
    assert hits[0]["url"] == (
        "https://www.mcmaster.com/products/socket-head-screws/steel-socket-head-screws~~/")
    assert hits[1]["url"] == (
        "https://www.mcmaster.com"
        "/products/socket-head-screws/stainless-steel-socket-head-screws~~/")
    assert hits[2]["title"] == "Low-Profile Steel Socket Head Screws"

    # Honest: a family is a configurable per-SKU table — no single sku/part_number
    # is invented at the search level.
    assert hits[0]["sku"] is None
    assert hits[0]["thumbnail"] == (
        "https://www.mcmaster.com/mvC/Contents/gfx/ImageCache/912/91290A432_x@100p.png")
    assert hits[1]["thumbnail"] is None

    # None of the emitted URLs is a bare department landing page.
    assert not any(h["url"].rstrip("/").endswith("fastening-and-joining") for h in hits)


def test_mcmaster_search_uses_product_line_crumb_when_no_family(mcmaster_mods, monkeypatch):
    # Direct-category queries (e.g. "ball bearing", "hex nut") land on a search
    # page with family tiles but NO product-family breadcrumb — only product-line.
    # McMaster's router accepts the product-line slug as the category segment
    # (live-verified: /products/bearings/ball-bearings-2~~/ renders 53 SKUs), so
    # the adapter must fall back to it rather than dropping to the department grid.
    mcmaster, session = mcmaster_mods
    # Verbatim shape (2026-07-11): product-line crumb + one "~~/" family tile.
    line_html = (
        '<a href="/products/product-category~~power-transmission~~2q3Rr7zd/?s=ball-bearing">Power Transmission</a>'
        '<a href="/products/product-line~~bearings~~2q3Rr8AA/?s=ball-bearing">Bearings</a>'
        '<div><a class="_selectable_srkjh_1" href="ball-bearings-2~~/?s=ball-bearing">'
        '<img alt="Ball Bearings" title=""></a></div>'
    )
    monkeypatch.setattr(session, "browser_fetch",
                        lambda url, site: {"ok": True, "content": line_html})

    hits = mcmaster.ADAPTER.search("ball bearing")

    assert len(hits) == 1
    assert hits[0]["kind"] == "series"
    assert hits[0]["title"] == "Ball Bearings"
    assert hits[0]["url"] == "https://www.mcmaster.com/products/bearings/ball-bearings-2~~/"


def test_mcmaster_search_no_breadcrumb_reports_parse_failure(mcmaster_mods, monkeypatch):
    # Without the breadcrumb there is no reliable family URL to construct.
    mcmaster, session = mcmaster_mods
    tiles_only = (
        '<div><a class="_selectable_srkjh_1" href="steel-socket-head-screws~~/?s=x">'
        '<img alt="Steel Socket Head Screws"></a></div>'
    )
    monkeypatch.setattr(session, "browser_fetch",
                        lambda url, site: {"ok": True, "content": tiles_only})

    assert mcmaster.ADAPTER.search("x")[0]["reason"] == "parse_failed"


def test_mcmaster_search_login_wall_surfaces_ask_user(mcmaster_mods, monkeypatch):
    mcmaster, session = mcmaster_mods
    monkeypatch.setattr(session, "browser_fetch",
                        lambda url, site: {"ok": True, "content": MCMASTER_LOGIN_WALL_HTML})

    hits = mcmaster.ADAPTER.search("m8 socket head cap screw")

    assert len(hits) == 1
    assert hits[0]["ok"] is False
    assert hits[0]["reason"] == "login_required"


# --- acquire(): client-generated STEP FilePath via browser_interact --------------
# The STEP model URL is minted client-side (no static .step href on the HTML); the
# adapter drives page JS to fetch McMaster's product-data endpoint and reads the
# "3-D STEP" FilePath out of a sentinel div. These tests stub browser_interact to
# return that sentinel DOM (the shape the real page JS publishes) and assert the
# adapter extracts the FilePath, downloads it, and returns a provenance envelope.
def _sentinel_dom(status: str, fp: str) -> str:
    return (f'<html><body><div id="__aicad_capture" data-status="{status}" '
            f'data-fp="{fp}">done</div></body></html>')


def test_mcmaster_acquire_downloads_client_generated_step(mcmaster_mods, monkeypatch):
    mcmaster, session = mcmaster_mods
    fp = "/mvC/Library/CAD2/20260528/920DEA54/91290A115_Socket Head Screw.STEP"
    seen = {}

    def fake_interact(url, site, script, **kw):
        seen["interact_url"] = url
        seen["script"] = script
        return {"ok": True, "content": _sentinel_dom("ok", fp), "final_url": url}

    def fake_download(url, site, dest, **kw):
        seen["download_url"] = url
        dest.write_bytes(b"ISO-10303-21;\nHEADER;\nENDSEC;\nEND-ISO-10303-21;")
        return {"ok": True, "dest": str(dest), "session_profile_used": "mcmaster"}

    monkeypatch.setattr(session, "browser_interact", fake_interact)
    monkeypatch.setattr(session, "browser_download", fake_download)

    out = mcmaster.ADAPTER.acquire({
        "url": "https://www.mcmaster.com/products/socket-head-screws/steel-socket-head-screws~~/",
        "title": "Steel Socket Head Screws", "kind": "series",
        "part_number": "91290A115"})

    assert out["ok"] is True
    assert out["sku"] == "91290A115"
    # interact ran against the concrete SKU page, and the script fetches the
    # product-data endpoint for that part number.
    assert seen["interact_url"] == "https://www.mcmaster.com/91290A115/"
    assert "partNumber=91290A115" in seen["script"]
    # The downloaded URL is BASE + the (percent-encoded) FilePath from the sentinel.
    assert seen["download_url"].startswith("https://www.mcmaster.com/mvC/Library/CAD2/")
    assert seen["download_url"].endswith(".STEP")
    assert out["artifact_path"].endswith(".STEP")
    assert out["artifact_kind"] == "cad"
    assert out["format"] == "step"


def test_mcmaster_acquire_derives_part_number_from_url(mcmaster_mods, monkeypatch):
    # A /<PN>/ product URL carries the SKU — no part_number field needed.
    mcmaster, session = mcmaster_mods
    fp = "/mvC/Library/CAD2/x/90044A203_Screw.STEP"
    monkeypatch.setattr(session, "browser_interact",
                        lambda url, site, script, **kw: {
                            "ok": True, "content": _sentinel_dom("ok", fp), "final_url": url})
    def fake_download(url, site, dest, **kw):
        dest.write_bytes(b"ISO-10303-21;\nHEADER;\nENDSEC;\nEND-ISO-10303-21;")
        return {"ok": True, "dest": str(dest), "session_profile_used": "mcmaster"}
    monkeypatch.setattr(session, "browser_download", fake_download)

    out = mcmaster.ADAPTER.acquire({"url": "https://www.mcmaster.com/90044A203/"})
    assert out["ok"] is True
    assert out["sku"] == "90044A203"


def test_mcmaster_acquire_login_status_degrades(mcmaster_mods, monkeypatch):
    # The product-data endpoint reports PromptLoginInd → sentinel data-status="login".
    mcmaster, session = mcmaster_mods
    monkeypatch.setattr(session, "browser_interact",
                        lambda url, site, script, **kw: {
                            "ok": True, "content": _sentinel_dom("login", ""), "final_url": url})

    out = mcmaster.ADAPTER.acquire({"url": "https://www.mcmaster.com/91290A115/"})
    assert out["ok"] is False
    assert out["reason"] == "login_required"


def test_mcmaster_acquire_no_step_model_is_honest(mcmaster_mods, monkeypatch):
    # A part with no 3-D model reports an unavailable CAD format.
    mcmaster, session = mcmaster_mods
    monkeypatch.setattr(session, "browser_interact",
                        lambda url, site, script, **kw: {
                            "ok": True, "content": _sentinel_dom("nofile", ""), "final_url": url})

    out = mcmaster.ADAPTER.acquire({"url": "https://www.mcmaster.com/91290A115/"})
    assert out["ok"] is False
    assert out["reason"] == "unsupported"


def test_mcmaster_family_requires_explicit_sku(mcmaster_mods, monkeypatch):
    mcmaster, session = mcmaster_mods
    table = '<table><span class="part-number">91251A051</span> more 91251A052</table>'
    monkeypatch.setattr(session, "browser_fetch",
                        lambda url, site: {"ok": True, "content": table})
    monkeypatch.setattr(session, "browser_interact",
                        lambda *a, **kw: pytest.fail("no SKU was selected"))
    monkeypatch.setattr(session, "browser_download",
                        lambda *a, **kw: pytest.fail("no SKU was selected"))
    out = mcmaster.ADAPTER.acquire({
        "url": "https://www.mcmaster.com/products/socket-head-screws/steel-socket-head-screws~~/",
        "kind": "series"})
    assert out["reason"] == "unsupported"
    assert out["ask_user"]["required"] is True
    assert "91251A051" in out["ask_user"]["prompt"]
    assert "91251A052" in out["ask_user"]["prompt"]


def test_mcmaster_search_encodes_query_and_preserves_product_data(mcmaster_mods, monkeypatch):
    mcmaster, session = mcmaster_mods
    seen = {}
    card = ('<a class="product-card" href="/91290A115/">Screw '
            '<img data-src="/images/screw.jpg"><span class="part-number">91290A115</span>'
            '<span class="price">$1,234.50</span></a>')
    def fetch(url, site):
        seen["url"] = url
        return {"ok": True, "content": card}
    monkeypatch.setattr(session, "browser_fetch", fetch)
    hit = mcmaster.ADAPTER.search("1/4 bolt & nut +")[0]
    assert seen["url"].endswith("/1%2F4-bolt-%26-nut-%2B/")
    assert hit["kind"] == "product"
    assert hit["sku"] == "91290A115"
    assert hit["thumbnail"] == "https://www.mcmaster.com/images/screw.jpg"
    assert (hit["price"], hit["currency"]) == (1234.50, "USD")


@pytest.mark.parametrize(("content", "reason"), [
    ("<title>Access Denied</title>", "blocked"),
    ("<html>Please complete the security check</html>", "captcha"),
    ("<html>unknown search layout</html>", "parse_failed"),
])
def test_mcmaster_search_distinguishes_unusable_pages(mcmaster_mods, monkeypatch, content, reason):
    mcmaster, session = mcmaster_mods
    monkeypatch.setattr(session, "browser_fetch", lambda *a: {"ok": True, "content": content})
    failure = mcmaster.ADAPTER.search("bolt")[0]
    assert (failure["site"], failure["op"], failure["reason"]) == ("mcmaster", "search", reason)


def test_mcmaster_initial_live_shell_is_not_a_confirmed_empty_search(mcmaster_mods, monkeypatch):
    # Trimmed from the 2026-11-04 raw HTTP response: the browse-catalog
    # navigation is present in the SPA shell but is not a search result.
    shell = ('<title>McMaster-Carr</title><base href="/" />'
             '<section class="browse-catalog-menu"><ul class="category-list"><li>'
             '<a id="catg0_Abrading-Polishing" class="category-tile" '
             'href="/products/abrading-and-polishing/" tabIndex="-1">'
             '<span class="category-name">Abrading &amp; Polishing</span>'
             '</a></li></ul></section>')
    mcmaster, session = mcmaster_mods
    monkeypatch.setattr(session, "browser_fetch", lambda *a: {"ok": True, "content": shell})
    assert mcmaster.ADAPTER.search("ball bearings")[0]["reason"] == "parse_failed"


def test_mcmaster_confirmed_empty_search(mcmaster_mods, monkeypatch):
    mcmaster, session = mcmaster_mods
    monkeypatch.setattr(session, "browser_fetch",
                        lambda *a: {"ok": True, "content": "<p>No results found</p>"})
    assert mcmaster.ADAPTER.search("obscure") == []


def test_mcmaster_preserves_unsupported_interaction_backend(mcmaster_mods, monkeypatch):
    mcmaster, session = mcmaster_mods
    monkeypatch.setattr(session, "browser_interact",
                        lambda *a, **kw: {"ok": False, "reason": "unsupported",
                                         "hint": "Selected backend cannot interact"})
    out = mcmaster.ADAPTER.acquire({"url": "https://www.mcmaster.com/91290A115/"})
    assert out["reason"] == "unsupported"
    assert out["site"] == "mcmaster" and out["op"] == "acquire"
    assert "backend" in out["hint"]


def test_mcmaster_rejects_invalid_cad_and_conflicting_sku(mcmaster_mods, monkeypatch):
    mcmaster, session = mcmaster_mods
    fp = "/models/91290A115.STEP"
    monkeypatch.setattr(session, "browser_interact",
                        lambda *a, **kw: {"ok": True, "content": _sentinel_dom("ok", fp)})
    def download(url, site, dest):
        dest.write_bytes(b"<html>Sign in</html>")
        return {"ok": True, "dest": str(dest)}
    monkeypatch.setattr(session, "browser_download", download)
    out = mcmaster.ADAPTER.acquire({"url": "https://www.mcmaster.com/91290A115/"})
    assert out["ok"] is False and out["reason"] == "parse_failed"
    assert "artifact_path" not in out
    conflict = mcmaster.ADAPTER.acquire({"url": "https://www.mcmaster.com/91290A115/",
                                          "sku": "91290A116"})
    assert conflict["reason"] == "unsupported"
