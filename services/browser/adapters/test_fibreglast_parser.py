"""Fixture-based parser test for the Fibre Glast search adapter.

FIBREGLAST_SEARCH_HTML holds two VERBATIM Shopify Dawn product-card anchors from a
real Fibre Glast search page fetched live 2026-07-10 from ``/search?q=fiberglass+cloth``
(HTTP 200, "Search: 320 results found"). The endpoint was already right; results
ship no JSON-LD, so the adapter reads the ``full-unstyled-link`` card anchor
(/products/<handle> URL + title). The page's Shopify storefront-forms captcha
library is passive form protection, not a wall (see test_hardening.py).

Offline style mirrors test_fastenal_parser.py: session.browser_fetch monkeypatched.
"""
from __future__ import annotations

import importlib

import pytest


FIBREGLAST_SEARCH_HTML = r"""<a href="/products/saertex-830-stitched-biaxial?_pos=1&amp;_sid=1bee920d8&amp;_ss=r" id="CardLink--8829534634221" class="full-unstyled-link" aria-labelledby="CardLink--8829534634221 Badge--8829534634221">
              Saertex 830 g/m² (24.5 oz/yd²) Stitched Biaxial (+/-45)
            </a>
<a href="/products/scrim-fabric-white-246?_pos=2&amp;_sid=1bee920d8&amp;_ss=r" id="CardLink--8829521330413" class="full-unstyled-link" aria-labelledby="CardLink--8829521330413 Badge--8829521330413">
              Scrim Fabric, White
            </a>"""


@pytest.fixture()
def fibreglast_mod(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    from engine import paths as engine_paths
    importlib.reload(engine_paths)
    from services.browser import profiles, session
    importlib.reload(profiles)
    importlib.reload(session)
    from services.browser import adapters as adapters_pkg
    importlib.reload(adapters_pkg)
    from services.browser.adapters import fibreglast
    importlib.reload(fibreglast)
    yield session, fibreglast
    monkeypatch.delenv("APPDATA", raising=False)
    importlib.reload(engine_paths)


def test_fibreglast_search_parses_shopify_cards(fibreglast_mod, monkeypatch):
    session, fibreglast = fibreglast_mod
    monkeypatch.setattr(session, "browser_fetch",
                        lambda url, s: {"ok": True, "content": FIBREGLAST_SEARCH_HTML})

    hits = fibreglast.ADAPTER.search("fiberglass cloth")

    assert len(hits) == 2
    assert all(h["ok"] is True and h["site"] == "fibreglast" for h in hits)

    first = hits[0]
    assert first["title"] == "Saertex 830 g/m² (24.5 oz/yd²) Stitched Biaxial (+/-45)"
    assert first["url"] == "https://www.fibreglast.com/products/saertex-830-stitched-biaxial"
    # tracking query stripped
    assert "?" not in first["url"]
