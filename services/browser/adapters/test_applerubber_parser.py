"""Fixture-based parser test for the Apple Rubber search adapter.

APPLERUBBER_SEARCH_HTML holds two VERBATIM ``<li>`` result rows (wrapped in
their real ``<ol id="SearchResults">`` container) from a real Apple Rubber
site-search page fetched live 2026-07-10 from ``/search.cfm?keyword=o-ring``
(HTTP 200). The old RetailAdapter default ``/search?q=`` guess 404'd. The results
page ships NO JSON-LD; it is a category/content search whose rows link to
product-category pages, size-search tools and design-guide pages, so the adapter
overrides :meth:`parse_search` and emits each row as an honest ``kind="page"``
hit. No sign-in gate rides these pages.

Offline style mirrors test_cui_parser.py: session.browser_fetch monkeypatched.
"""
from __future__ import annotations

import importlib

import pytest


APPLERUBBER_SEARCH_HTML = r"""<ol id="SearchResults" start="1">
				<li>
					<a href="/seal-design-guide/technical-summary/oring.cfm">O-Ring</a>
					<p>A doughnut-shaped object, or torus, that functions as a seal blocking the passage of liquids or gases. This seal is energized by being compressed between two mating surfaces comprising the walls of th</p>
				</li><li class="gray">
					<a href="/products/o-rings.cfm">O-Rings</a>
					<p>AS568 &amp; ISO 3601 Standard Sizes; 300M+ In-Stock; Viton / FKM, Nitrile / NBR, Ethylene-Propylene / EPDM, Fluorsilicone, Silicone, Neoprene O-Rings...</p>
				</li>
</ol>"""


@pytest.fixture()
def applerubber_mod(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    from engine import paths as engine_paths
    importlib.reload(engine_paths)
    from services.browser import profiles, session
    importlib.reload(profiles)
    importlib.reload(session)
    from services.browser import adapters as adapters_pkg
    importlib.reload(adapters_pkg)
    from services.browser.adapters import applerubber
    importlib.reload(applerubber)
    yield session, applerubber
    monkeypatch.delenv("APPDATA", raising=False)
    importlib.reload(engine_paths)


def test_applerubber_search_parses_result_rows(applerubber_mod, monkeypatch):
    session, applerubber = applerubber_mod
    monkeypatch.setattr(session, "browser_fetch",
                        lambda url, s: {"ok": True, "content": APPLERUBBER_SEARCH_HTML})

    hits = applerubber.ADAPTER.search("o-ring")

    assert len(hits) == 2
    assert all(h["ok"] is True and h["site"] == "applerubber" for h in hits)
    assert all(h["kind"] == "page" for h in hits)

    first = hits[0]
    assert first["title"] == "O-Ring"
    assert first["url"] == (
        "https://www.applerubber.com/seal-design-guide/technical-summary/oring.cfm")
    assert first["description"].startswith("A doughnut-shaped object")

    second = hits[1]
    assert second["title"] == "O-Rings"
    assert second["url"] == "https://www.applerubber.com/products/o-rings.cfm"
    assert "AS568 & ISO 3601" in second["description"]


def test_applerubber_search_url_is_cfm_keyword_endpoint():
    from services.browser.adapters import applerubber
    assert applerubber.SEARCH_URL_TMPL == (
        "https://www.applerubber.com/search.cfm?keyword={query}")


def test_applerubber_no_results_block_is_honest(applerubber_mod, monkeypatch):
    session, applerubber = applerubber_mod
    monkeypatch.setattr(session, "browser_fetch",
                        lambda url, s: {"ok": True, "content": "<html>homepage</html>"})
    assert applerubber.ADAPTER.search("o-ring") == []
