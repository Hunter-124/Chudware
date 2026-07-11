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
    assert all(h["kind"] == "family" for h in hits)
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
    assert "sku" not in hits[0]
    assert "part_number" not in hits[0]

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
    assert hits[0]["kind"] == "family"
    assert hits[0]["title"] == "Ball Bearings"
    assert hits[0]["url"] == "https://www.mcmaster.com/products/bearings/ball-bearings-2~~/"


def test_mcmaster_search_no_breadcrumb_returns_empty(mcmaster_mods, monkeypatch):
    # Result tiles but no product-family breadcrumb → no category to build a
    # renderable URL from → honest empty (never a speculative URL).
    mcmaster, session = mcmaster_mods
    tiles_only = (
        '<div><a class="_selectable_srkjh_1" href="steel-socket-head-screws~~/?s=x">'
        '<img alt="Steel Socket Head Screws"></a></div>'
    )
    monkeypatch.setattr(session, "browser_fetch",
                        lambda url, site: {"ok": True, "content": tiles_only})

    assert mcmaster.ADAPTER.search("x") == []


def test_mcmaster_search_login_wall_surfaces_ask_user(mcmaster_mods, monkeypatch):
    mcmaster, session = mcmaster_mods
    monkeypatch.setattr(session, "browser_fetch",
                        lambda url, site: {"ok": True, "content": MCMASTER_LOGIN_WALL_HTML})

    hits = mcmaster.ADAPTER.search("m8 socket head cap screw")

    assert len(hits) == 1
    assert hits[0]["ok"] is False
    assert hits[0]["reason"] == "login_required"
