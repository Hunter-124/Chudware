"""Fixture-based parser test for the MEAN WELL search adapter.

MW_SEARCH_HTML holds two VERBATIM result rows from a real ASP.NET site-search
page fetched live 2026-07-10 from ``/productSearch.aspx?pkeywords=LRS`` (HTTP
200). MEAN WELL's search is model/series-indexed: each result row's PDF-browse
anchor carries the series/model in ``/webapp/product/search.aspx?prod=<MODEL>``.
There is no per-model price on this page, so hits are ``kind="series"``. The
header "Sign In"/"Log In" widget must not trip the wall.

Offline style mirrors test_cui_parser.py: session.browser_fetch monkeypatched.
"""
from __future__ import annotations

import importlib

import pytest


MW_SEARCH_HTML = r"""<div id="productSearch">
  <table>
    <tr>
      <td class="text-center">
        <a href="/webapp/product/search.aspx?prod=LRS-350" class="icon-file-pdf t-red" title="檔案瀏覽" target="_blank">
          <span class="sr-only">檔案瀏覽</span>
        </a>
      </td>
      <td class="text-center">LRS-350</td>
      <td class="text-center">350</td>
    </tr>
    <tr>
      <td class="text-center">
        <a href="/webapp/product/search.aspx?prod=LRS-450" class="icon-file-pdf t-red" title="檔案瀏覽" target="_blank">
          <span class="sr-only">檔案瀏覽</span>
        </a>
      </td>
      <td class="text-center">LRS-450</td>
      <td class="text-center">450</td>
    </tr>
  </table>
</div>"""


@pytest.fixture()
def mw_mod(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    from services import paths as engine_paths
    importlib.reload(engine_paths)
    from services.browser import profiles, session
    importlib.reload(profiles)
    importlib.reload(session)
    from services.browser import adapters as adapters_pkg
    importlib.reload(adapters_pkg)
    from services.browser.adapters import meanwell
    importlib.reload(meanwell)
    yield session, meanwell
    monkeypatch.delenv("APPDATA", raising=False)
    importlib.reload(engine_paths)


def test_mw_search_parses_series_rows(mw_mod, monkeypatch):
    session, mw = mw_mod
    monkeypatch.setattr(session, "browser_fetch",
                        lambda url, s: {"ok": True, "content": MW_SEARCH_HTML})

    hits = mw.ADAPTER.search("LRS")

    assert len(hits) == 2
    assert all(h["ok"] is True and h["site"] == "meanwell" for h in hits)
    assert all(h["kind"] == "series" for h in hits)

    first = hits[0]
    assert first["title"] == "LRS-350"
    assert first["part_number"] == "LRS-350"
    assert first["url"] == (
        "https://www.meanwell.com/webapp/product/search.aspx?prod=LRS-350")

    assert hits[1]["title"] == "LRS-450"


def test_mw_search_url_is_productsearch_aspx():
    from services.browser.adapters import meanwell
    assert "productSearch.aspx?pkeywords=" in meanwell.SEARCH_URL_TMPL


def test_mw_header_signin_is_not_a_wall(mw_mod, monkeypatch):
    session, mw = mw_mod
    page = '<form><input name="password"></form>Sign In Log In' + MW_SEARCH_HTML
    monkeypatch.setattr(session, "browser_fetch",
                        lambda url, s: {"ok": True, "content": page})
    hits = mw.ADAPTER.search("LRS")
    assert len(hits) == 2 and all(h.get("ok") for h in hits)
