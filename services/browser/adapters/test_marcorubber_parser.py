"""Fixture-based parser test for the Marco Rubber search adapter.

MR_SEARCH_HTML holds two VERBATIM result anchors from the Angular search-results
route ``/search-results?search=viton`` fetched live 2026-07-10 (HTTP 200). The
SPA renders each hit as ``<a class="search-header" href="…">Title</a>`` (query
highlighted with ``<b>`` — the adapter strips tags); relative hrefs are joined
onto the base URL. The header "Sign In" link (no password field) is a confirmed
false login-wall positive and must NOT trip the wall.

Offline style mirrors test_cui_parser.py: session.browser_fetch monkeypatched.
"""
from __future__ import annotations

import importlib

import pytest


MR_SEARCH_HTML = r"""<div class="column result-wrapper">
  <a _ngcontent-exp-web-ui-c3674750892="" class="search-header pointer" href="/store?searchQuery=viton"><b>Viton</b> O-ring Products - Check Stock and Buy Online</a>
  <div class="search-link"><a class="search-link-color pointer" href="/store?searchQuery=viton">https://www.marcorubber.com/store/</a></div>
</div>
<div class="column result-wrapper">
  <a _ngcontent-exp-web-ui-c3674750892="" class="search-header pointer" href="/viton-o-rings.htm"><b>Viton</b>® (FKM) Compounds &amp; Reference</a>
  <div class="search-link"><a class="search-link-color pointer" href="/viton-o-rings.htm">https://www.marcorubber.com/viton-o-rings.htm</a></div>
</div>"""


@pytest.fixture()
def mr_mod(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    from engine import paths as engine_paths
    importlib.reload(engine_paths)
    from services.browser import profiles, session
    importlib.reload(profiles)
    importlib.reload(session)
    from services.browser import adapters as adapters_pkg
    importlib.reload(adapters_pkg)
    from services.browser.adapters import marcorubber
    importlib.reload(marcorubber)
    yield session, marcorubber
    monkeypatch.delenv("APPDATA", raising=False)
    importlib.reload(engine_paths)


def test_mr_search_parses_result_anchors(mr_mod, monkeypatch):
    session, mr = mr_mod
    monkeypatch.setattr(session, "browser_fetch",
                        lambda url, s: {"ok": True, "content": MR_SEARCH_HTML})

    hits = mr.ADAPTER.search("viton")

    assert len(hits) == 2
    assert all(h["ok"] is True and h["site"] == "marcorubber" for h in hits)

    first = hits[0]
    assert first["title"] == "Viton O-ring Products - Check Stock and Buy Online"
    assert first["url"] == "https://www.marcorubber.com/store?searchQuery=viton"

    second = hits[1]
    assert second["title"] == "Viton® (FKM) Compounds & Reference"
    assert second["url"] == "https://www.marcorubber.com/viton-o-rings.htm"


def test_mr_search_url_is_search_results_route():
    from services.browser.adapters import marcorubber
    assert "search-results?search=" in marcorubber.SEARCH_URL_TMPL


def test_mr_header_signin_is_not_a_wall(mr_mod, monkeypatch):
    session, mr = mr_mod
    page = "Sign In" + MR_SEARCH_HTML
    monkeypatch.setattr(session, "browser_fetch",
                        lambda url, s: {"ok": True, "content": page})
    hits = mr.ADAPTER.search("viton")
    assert len(hits) == 2 and all(h.get("ok") for h in hits)
