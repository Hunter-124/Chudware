"""Fixture-based parser test for the SpeedyMetals search adapter.

SM_SEARCH_HTML holds two VERBATIM result-table rows from a real ASP.NET
site-search page fetched live 2026-07-10 from
``/search.aspx?SearchTerm=6061+aluminum`` (HTTP 200). Results are a
server-rendered table (no JSON-LD); the adapter reads each
``<td><a href="p-<id>.aspx">Title</a></td>`` plus the Material and Shape cells.
Relative hrefs are joined onto the base URL. The header "Sign In" widget must
not trip the wall.

Offline style mirrors test_cui_parser.py: session.browser_fetch monkeypatched.
"""
from __future__ import annotations

import importlib

import pytest


SM_SEARCH_HTML = r"""<table>
  <tr>
    <td valign="middle" align="left"><a href="p-79-61sh032.aspx">.032" 6061-T6 Aluminum Sheet</a></td>
    <td align="center">Aluminum</td>
    <td align="center">Sheet</td>
  </tr>
  <tr>
    <td valign="middle" align="left"><a href="p-80-61sh040.aspx">.040" 6061-T6 Aluminum Sheet</a></td>
    <td align="center">Aluminum</td>
    <td align="center">Sheet</td>
  </tr>
</table>"""


@pytest.fixture()
def sm_mod(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    from services import paths as engine_paths
    importlib.reload(engine_paths)
    from services.browser import profiles, session
    importlib.reload(profiles)
    importlib.reload(session)
    from services.browser import adapters as adapters_pkg
    importlib.reload(adapters_pkg)
    from services.browser.adapters import speedymetals
    importlib.reload(speedymetals)
    yield session, speedymetals
    monkeypatch.delenv("APPDATA", raising=False)
    importlib.reload(engine_paths)


def test_sm_search_parses_result_rows(sm_mod, monkeypatch):
    session, sm = sm_mod
    monkeypatch.setattr(session, "browser_fetch",
                        lambda url, s: {"ok": True, "content": SM_SEARCH_HTML})

    hits = sm.ADAPTER.search("6061 aluminum")

    assert len(hits) == 2
    assert all(h["ok"] is True and h["site"] == "speedymetals" for h in hits)

    first = hits[0]
    assert first["title"] == '.032" 6061-T6 Aluminum Sheet'
    assert first["url"] == "https://www.speedymetals.com/p-79-61sh032.aspx"
    assert first["category"] == "Aluminum / Sheet"
    assert first["kind"] == "product" and first["material"] == "Aluminum"
    assert first["thumbnail"] is None and first["price"] is None and first["stock"] is None
    assert first["model"] == {"available": None, "formats": []}
    assert sm.ADAPTER.result_kinds == ("product",) and not sm.ADAPTER.cad_acquisition


def test_sm_search_url_is_search_aspx():
    from services.browser.adapters import speedymetals
    assert "search.aspx?SearchTerm=" in speedymetals.SEARCH_URL_TMPL


def test_sm_header_signin_is_not_a_wall(sm_mod, monkeypatch):
    session, sm = sm_mod
    page = '<form><input name="password"></form>Sign In' + SM_SEARCH_HTML
    monkeypatch.setattr(session, "browser_fetch",
                        lambda url, s: {"ok": True, "content": page})
    hits = sm.ADAPTER.search("6061 aluminum")
    assert len(hits) == 2 and all(h.get("ok") for h in hits)


def test_sm_image_price_and_quantity_belong_to_their_row(sm_mod, monkeypatch):
    session, sm = sm_mod
    html = '''<table>
    <tr><td><img src="/logo.png" alt="logo"><img data-src="/products/angle.jpg">
    <a class="result" href="p-123-angle.aspx"><span>6061 Angle</span></a></td>
    <td>Aluminum</td><td>Angle</td><td class="price">$12.40</td><td class="stock">7</td></tr>
    <tr><td><a href="p-124-bar.aspx">Stainless Bar</a></td>
    <td>Stainless Steel</td><td>Bar</td></tr></table>
    <img src="/outside.jpg">$999.00'''
    requested = []
    def fetch(url, site):
        requested.append((url, site))
        return {"ok": True, "content": html}
    monkeypatch.setattr(session, "browser_fetch", fetch)
    first, second = sm.ADAPTER.search("6061 & angle")
    assert requested == [(sm.BASE_URL + "/search.aspx?SearchTerm=6061+%26+angle", sm.SITE)]
    assert first["thumbnail"] == sm.BASE_URL + "/products/angle.jpg"
    assert (first["price"], first["currency"], first["stock"]) == (12.4, None, 7)
    assert first["material"] == "Aluminum" and first["category"] == "Aluminum / Angle"
    assert second["material"] == "Stainless Steel"
    assert second["thumbnail"] is None and second["price"] is None and second["stock"] is None


def test_sm_search_failures_are_not_silent(sm_mod, monkeypatch):
    session, sm = sm_mod
    page = {"html": "<html>changed layout</html>"}
    monkeypatch.setattr(session, "browser_fetch", lambda *_: {"ok": True, "content": page["html"]})
    assert sm.ADAPTER.search("sheet")[0]["reason"] == "parse_failed"
    page["html"] = "Pardon Our Interruption"
    assert sm.ADAPTER.search("sheet")[0]["reason"] == "blocked"
    page["html"] = "No results"
    assert sm.ADAPTER.search("sheet") == []


def test_live_unprocessed_search_form_is_not_an_empty_result(sm_mod, monkeypatch):
    session, sm = sm_mod
    # Trimmed from the 2026-11-04 response to the populated search URL.
    page = '''<form method="GET" action="search.aspx" onsubmit="return validateForm(this)" name="SearchForm2">
    <table><tbody><tr><td>What are you searching for?
    <input type="text" name="SearchTerm" size="25" maxlength="70" value="">
    <input type="submit" value="Search" name="B1"></td></tr></tbody></table></form>
    <div><hr></div>Do you have a comment on the site or an idea about how we can make the site better?'''
    monkeypatch.setattr(session, "browser_fetch", lambda *_: {"ok": True, "content": page})
    failure = sm.ADAPTER.search("6061 aluminum")[0]
    assert failure["reason"] == "unsupported"
    assert "blank search form" in failure["hint"]
