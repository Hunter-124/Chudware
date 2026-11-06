"""Fixture-based parser test for the MEAN WELL search adapter.

MW_SEARCH_HTML contains trimmed result rows from the real ASP.NET site-search
page ``/productSearch.aspx?pkeywords=LRS`` (HTTP
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
      <td class="text-center"><img src="https://www.meanwell.com//Upload/71fc3ceb-fec4-4684-9f82-5b139c0e158b.png?w=40&h=30&mode=crop" alt="1.CUL"></td>
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
    assert first["sku"] is None and first["mpn"] is None
    assert first["manufacturer"] == "MEAN WELL"
    assert first["price"] is None and first["currency"] is None
    assert first["stock"] is None and first["thumbnail"] is None
    assert first["model"] == {"available": None, "formats": []}
    assert first["url"] == (
        "https://www.meanwell.com/webapp/product/search.aspx?prod=LRS-350")

    assert hits[1]["title"] == "LRS-450"


def test_mw_search_url_is_productsearch_aspx():
    from services.browser.adapters import meanwell
    assert "productSearch.aspx?pkeywords=" in meanwell.SEARCH_URL_TMPL
    assert meanwell.ADAPTER.search_url("LRS 350 &/#") == (
        "https://www.meanwell.com/productSearch.aspx?pkeywords=LRS+350+%26%2F%23")


def test_mw_header_signin_is_not_a_wall(mw_mod, monkeypatch):
    session, mw = mw_mod
    page = '<form><input name="password"></form>Sign In Log In' + MW_SEARCH_HTML
    monkeypatch.setattr(session, "browser_fetch",
                        lambda url, s: {"ok": True, "content": page})
    hits = mw.ADAPTER.search("LRS")
    assert len(hits) == 2 and all(h.get("ok") for h in hits)


def test_mw_capabilities_and_empty_series_explanation(mw_mod, monkeypatch):
    session, mw = mw_mod
    assert (mw.ADAPTER.hosts, mw.ADAPTER.result_kinds) == (("meanwell.com",), ("series",))
    assert not mw.ADAPTER.cad_acquisition and not mw.ADAPTER.login_required and not mw.ADAPTER.api
    empty = '<div id="productSearch"><div class="header"><h2 class="h5">Search Results</h2></div></div>'
    monkeypatch.setattr(session, "browser_fetch", lambda url, site: {"ok": True, "content": empty})
    notice = mw.ADAPTER.search("power supply")[0]
    assert notice["reason"] == "not_found" and notice["site"] == "meanwell"
    assert notice["op"] == "search" and "series" in notice["hint"].lower()
    assert notice["url"].endswith("pkeywords=power+supply")

    changed = empty.replace("</h2></div></div>", "</h2></div><table><tr><td>unknown</td></tr></table></div>")
    monkeypatch.setattr(session, "browser_fetch", lambda url, site: {"ok": True, "content": changed})
    assert mw.ADAPTER.search("LRS")[0]["reason"] == "parse_failed"


@pytest.mark.parametrize(("page", "reason"), [
    ("<h1>Access Denied</h1>", "blocked"),
    ("<h1>Verify you are human</h1>", "captcha"),
    ("<p>Please sign in to continue</p>", "login_required"),
])
def test_mw_wall_types(mw_mod, monkeypatch, page, reason):
    session, mw = mw_mod
    monkeypatch.setattr(session, "browser_fetch", lambda url, site: {"ok": True, "content": page})
    failure = mw.ADAPTER.search("LRS")[0]
    assert failure["ok"] is False and failure["reason"] == reason
    assert failure["site"] == "meanwell" and failure["op"] == "search"


def test_mw_transport_failure_preserves_status(mw_mod, monkeypatch):
    session, mw = mw_mod
    monkeypatch.setattr(session, "browser_fetch",
                        lambda url, site: {"ok": False, "reason": "http_error", "status": 404})
    failure = mw.ADAPTER.search("LRS")[0]
    assert failure["reason"] == "not_found" and failure["status"] == 404
    assert failure["url"] == mw.ADAPTER.search_url("LRS")


def test_mw_datasheet_is_a_document(mw_mod, monkeypatch, tmp_path):
    session, mw = mw_mod
    from services.browser.adapters import _retail
    monkeypatch.setattr(session, "browser_fetch", lambda url, site: {
        "ok": True, "content": '<a href="/pdf/lrs.pdf">Datasheet</a>'})
    dest = tmp_path / "lrs.pdf"
    monkeypatch.setattr(_retail, "download_path", lambda *args, **kwargs: (dest, None))

    def download(url, site, path):
        path.write_bytes(b"%PDF-1.4\n%%EOF\n")
        return {"ok": True, "dest": str(path)}

    monkeypatch.setattr(session, "browser_download", download)
    outcome = mw.ADAPTER.acquire({"title": "LRS-350", "url": mw.BASE_URL + "/series/LRS"})
    assert outcome["ok"] and outcome["artifact_path"] == str(dest)
    assert outcome["artifact_kind"] == "document" and outcome["format"] == "pdf"

    def download_html(url, site, path):
        path.write_bytes(b"<html>Sign in</html>")
        return {"ok": True, "dest": str(path)}

    monkeypatch.setattr(session, "browser_download", download_html)
    rejected = mw.ADAPTER.acquire({"title": "LRS-350", "url": mw.BASE_URL + "/series/LRS"})
    assert not rejected["ok"] and rejected["reason"] == "parse_failed"
    assert "artifact_path" not in rejected
