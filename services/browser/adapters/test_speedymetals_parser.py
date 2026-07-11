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
    from engine import paths as engine_paths
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
