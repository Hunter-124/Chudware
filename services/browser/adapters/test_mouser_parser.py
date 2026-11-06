"""Real-HTML bot-wall regression test for the Mouser electronics adapter
(Parts Library V2, 2026-07-11).

Mouser's real catalog keyword search (``/c/?q=...``) is Akamai bot-walled: an
anonymous CEF-headless fetch returns HTTP 200 with the "Access to this page has
been denied." block shell (``<h1>Access Denied</h1>`` + an ``m-captcha`` div,
"we believe you are using automation tools to browse the website"). The alternate
``/ProductSearch/Refine`` endpoint returns only a JS chrome shell (results load
via XHR), so neither path yields server-rendered product rows headless.

The wall must be reported as blocked, never as login and never as empty results.

``MOUSER_ACCESS_DENIED_HTML`` below is a VERBATIM slice of the real block page
returned on 2026-07-11, reproduced with the markers that trip the challenge
detector. Offline style mirrors test_walled_sites.py: no real CEF/network —
``session.browser_fetch`` is monkeypatched.
"""
from __future__ import annotations

import importlib

import pytest


# --- verbatim real block-page capture (2026-07-11, CEF headless) -------------
# https://www.mouser.com/c/?q=heatsink  → Akamai "Access Denied" shell, HTTP 200.
MOUSER_ACCESS_DENIED_HTML = (
    '<!DOCTYPE html><html lang="en"><head>\n'
    ' <meta charset="utf-8">\n'
    ' <meta name="viewport" content="width=device-width, initial-scale=1">\n'
    " <title>\n"
    "Access to this page has been denied.</title>\n"
    "</head><body>\n"
    ' <div class="container">\n'
    '  <div class="page-title-wrapper">\n'
    '   <div class="page-title">\n'
    "    <h1>Access Denied</h1>\n"
    "   </div>\n"
    "  </div>\n"
    '  <div class="content-wrapper">\n'
    '   <div class="content">\n'
    '    <div id="m-captcha">\n'
    "    </div>\n"
    "    <p>\n"
    "     Access to this page has been denied either because we believe you are"
    " using automation tools to browse the website, or something in the request"
    " did not pass security restrictions.  </p>\n"
    "   </div>\n"
    "  </div>\n"
    " </div>\n"
    "</body></html>"
)


@pytest.fixture()
def mouser_mod(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    from services import paths as engine_paths
    importlib.reload(engine_paths)
    from services.browser import profiles, session
    importlib.reload(profiles)
    importlib.reload(session)
    from services.browser import adapters as adapters_pkg
    importlib.reload(adapters_pkg)
    from services.browser.adapters import mouser
    importlib.reload(mouser)
    monkeypatch.setattr(mouser._vendor_api, "credentials", lambda _: None)
    yield session, mouser
    monkeypatch.delenv("APPDATA", raising=False)
    importlib.reload(engine_paths)


def test_mouser_search_url_is_catalog_keyword_endpoint():
    from services.browser.adapters import mouser
    assert mouser.SEARCH_URL_TMPL == "https://www.mouser.com/c/?q={query}"


def test_real_bot_wall_is_blocked(mouser_mod, monkeypatch):
    """The Akamai denial is not a sign-in wall and must not be bypassed."""
    session, mouser = mouser_mod
    monkeypatch.setattr(
        session, "browser_fetch",
        lambda url, s: {"ok": True, "content": MOUSER_ACCESS_DENIED_HTML,
                        "session_profile_used": s})

    hits = mouser.ADAPTER.search("heatsink")

    assert len(hits) == 1
    assert hits[0]["ok"] is False
    assert hits[0]["reason"] == "blocked"
    assert not hits[0].get("ask_user")


def test_real_bot_wall_never_reaches_parse(mouser_mod, monkeypatch):
    """Guard against a regression where a challenge marker is dropped and the
    block shell silently parses to zero hits (the original "no hits parsed" bug).
    """
    session, mouser = mouser_mod
    monkeypatch.setattr(
        session, "browser_fetch",
        lambda url, s: {"ok": True, "content": MOUSER_ACCESS_DENIED_HTML,
                        "session_profile_used": s})

    hits = mouser.ADAPTER.search("anything")
    # A silent empty list (or a fabricated product hit) would both fail here.
    assert hits and hits[0].get("reason") == "blocked"


def test_jsonld_products_parse_when_reachable(mouser_mod, monkeypatch):
    """When the catalog IS reachable (user's own challenge-cleared session), a
    JSON-LD Product page parses to a real hit — proving the parser is wired, not
    dead scaffold behind the wall."""
    session, mouser = mouser_mod
    page = (
        '<html><head><script type="application/ld+json">'
        '{"@type":"Product","name":"Heatsink 30x30x10mm",'
        '"url":"/ProductDetail/heatsink-30","sku":"532102B02500G",'
        '"image":"//images.mouser.com/heatsink.jpg",'
        '"offers":{"@type":"Offer","price":"2.34","priceCurrency":"USD"}}'
        "</script></head><body>results</body></html>"
    )
    monkeypatch.setattr(
        session, "browser_fetch",
        lambda url, s: {"ok": True, "content": page, "session_profile_used": s})

    hits = mouser.ADAPTER.search("heatsink")

    assert len(hits) == 1
    assert hits[0]["ok"] is True and hits[0]["site"] == "mouser"
    assert hits[0]["title"] == "Heatsink 30x30x10mm"
    assert hits[0]["url"] == "https://www.mouser.com/ProductDetail/heatsink-30"
    assert hits[0]["sku"] == "532102B02500G"
    assert hits[0]["price"] == 2.34 and hits[0]["currency"] == "USD"
    assert hits[0]["kind"] == "product"
    assert hits[0]["thumbnail"] == "https://images.mouser.com/heatsink.jpg"


def test_mouser_web_encodes_and_distinguishes_empty_from_unparseable(mouser_mod, monkeypatch):
    session, mouser = mouser_mod
    seen = []
    monkeypatch.setattr(session, "browser_fetch", lambda url, site: (
        seen.append(url) or {"ok": True, "content": "<html>No results</html>"}))
    assert mouser.ADAPTER.search("C++ & A/B#1") == []
    assert seen[0].endswith("?q=C%2B%2B+%26+A%2FB%231")
    monkeypatch.setattr(session, "browser_fetch", lambda *_: {"ok": True, "content": "<html>shell</html>"})
    assert mouser.ADAPTER.search("heatsink")[0]["reason"] == "parse_failed"
