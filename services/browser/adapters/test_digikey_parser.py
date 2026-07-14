"""services/browser/adapters/test_digikey_parser.py

Fixture-based test for the DigiKey WEBSITE adapter's ``search()`` parser
(digikey_web.py). Mirrors the McMaster adapter tests in test_adapters.py:
``_session.browser_fetch`` is monkeypatched to return canned HTML and
``digikey_web.ADAPTER.search(...)`` is asserted against real values.

The fixture below is a VERBATIM slice of the real page fetched live on
2026-07-07 from ``https://www.digikey.com/en/products/result?keywords=resistor``.
DigiKey embeds its results in a Next.js ``<script id="__NEXT_DATA__">`` blob;
for a broad keyword the live shape is
``props.pageProps.envelope.data.topResults`` = a list of CATEGORY tiles (a
disambiguation page), NOT product cards — so the parser returns
``kind="category"`` results. The two ``topResults`` entries below are copied
verbatim (real field names + values) from that saved page, trimmed to two
entries. The two entries are verbatim (real field names + values); the wrapper
mirrors the real ``props.pageProps.envelope.data.topResults`` nesting, trimmed.
"""
from __future__ import annotations

import json

from services.browser import session
from services.browser.adapters import digikey_web

# --- VERBATIM real slice: two real topResults entries from the saved page ---
_TOP_RESULTS = [
    {
        "categoryUrl": "/en/products/filter/chip-resistor-surface-mount/52",
        "imageUrl": "//mm.digikey.com/Volume0/opasdata/d220001/derivates/3/002/683/989/MFG_RMC%20SERIES_tmb%2864x64%29.jpg",
        "categoryName": "Chip Resistor - Surface Mount",
        "parentCategory": "Resistors",
        "productCount": "1,204,216",
        "id": "52",
    },
    {
        "categoryUrl": "/en/products/filter/through-hole-resistors/53",
        "imageUrl": "//mm.digikey.com/Volume0/opasdata/d220001/derivates/3/001/165/374/OX470KE_tmb%2864x64%29.jpg",
        "categoryName": "Through Hole Resistors",
        "parentCategory": "Resistors",
        "productCount": "533,076",
        "id": "53",
    },
]

# The real page nests topResults at props.pageProps.envelope.data.topResults,
# wrapped in the same <script id="__NEXT_DATA__" type="application/json"> tag.
DIGIKEY_SEARCH_HTML = (
    '<!DOCTYPE html><html><head><title>Resistors | DigiKey</title></head>'
    '<body><div id="__next"></div>'
    '<script id="__NEXT_DATA__" type="application/json">'
    + json.dumps(
        {
            "props": {
                "pageProps": {
                    "envelope": {
                        "data": {
                            "productCount": "2,520,769",
                            "topResults": _TOP_RESULTS,
                        }
                    }
                }
            },
        }
    )
    + "</script></body></html>"
)

DIGIKEY_LOGIN_WALL_HTML = (
    '<html><body><form class="login-form"><h1>Sign In</h1>'
    '<input id="signInName"/></form></body></html>'
)


def test_digikey_search_parses_category_tiles(monkeypatch):
    monkeypatch.setattr(
        session, "browser_fetch",
        lambda url, site: {"ok": True, "content": DIGIKEY_SEARCH_HTML})

    hits = digikey_web.ADAPTER.search("resistor")

    assert len(hits) == 2
    assert hits[0]["title"] == "Chip Resistor - Surface Mount"
    assert hits[0]["url"] == (
        "https://www.digikey.com/en/products/filter/chip-resistor-surface-mount/52")
    assert hits[0]["site"] == "digikey"
    assert hits[0]["kind"] == "category"
    assert hits[0]["parent_category"] == "Resistors"
    assert hits[0]["product_count"] == "1,204,216"
    assert hits[0]["category_id"] == "52"

    assert hits[1]["title"] == "Through Hole Resistors"
    assert hits[1]["url"] == (
        "https://www.digikey.com/en/products/filter/through-hole-resistors/53")


def test_digikey_search_login_wall_surfaces_ask_user(monkeypatch):
    monkeypatch.setattr(
        session, "browser_fetch",
        lambda url, site: {"ok": True, "content": DIGIKEY_LOGIN_WALL_HTML})

    hits = digikey_web.ADAPTER.search("resistor")

    assert len(hits) == 1
    assert hits[0]["ok"] is False
    assert hits[0]["reason"] == "login_required"


def test_digikey_search_propagates_fetch_failure(monkeypatch):
    monkeypatch.setattr(
        session, "browser_fetch",
        lambda url, site: {"ok": False, "reason": "no_profile", "hint": "x"})

    hits = digikey_web.ADAPTER.search("resistor")
    assert hits == [{"ok": False, "reason": "no_profile", "hint": "x"}]


def test_digikey_search_no_next_data_returns_empty(monkeypatch):
    monkeypatch.setattr(
        session, "browser_fetch",
        lambda url, site: {"ok": True, "content": "<html><body>nope</body></html>"})

    assert digikey_web.ADAPTER.search("resistor") == []


# --- Narrow-MPN product-results fixture (synthetic but field-faithful) ---------
# DigiKey's product-results envelope carries SKUs under envelope.data.products
# (and a single-product detail under envelope.data.product). Field names match
# the Product Information API / Next.js payload (camelCase on the web, with
# PascalCase accepted by the parser too).
_PRODUCT_RESULTS = [
    {
        "manufacturerProductNumber": "LM317T",
        "digiKeyProductNumber": "LM317TFS-ND",
        "productDescription": "IC REG LIN POS ADJ 1.5A TO220-3",
        "manufacturerName": "STMicroelectronics",
        "productUrl": "/en/products/detail/stmicroelectronics/LM317T/604171",
        "unitPrice": 0.63,
        "quantityAvailable": 12345,
        "datasheetUrl": "https://mm.digikey.com/Volume0/opasdata/d220001/datasheets/123/LM317T.pdf",
    },
    {
        "ManufacturerProductNumber": "LM317T",
        "DigiKeyPartNumber": "497-1572-5-ND",
        "ProductDescription": "IC REG LIN POS ADJ 1.5A TO220AB",
        "Manufacturer": {"Name": "STMicroelectronics"},
        "ProductUrl": "/en/products/detail/stmicroelectronics/LM317T/605123",
        "UnitPrice": 0.71,
        "QuantityAvailable": 800,
        "DatasheetUrl": "https://mm.digikey.com/Volume0/opasdata/d220001/datasheets/456/LM317T.pdf",
    },
]

DIGIKEY_PRODUCT_SEARCH_HTML = (
    '<!DOCTYPE html><html><head><title>LM317T | DigiKey</title></head>'
    '<body><div id="__next"></div>'
    '<script id="__NEXT_DATA__" type="application/json">'
    + json.dumps(
        {
            "props": {
                "pageProps": {
                    "envelope": {
                        "data": {
                            "productCount": 2,
                            "products": _PRODUCT_RESULTS,
                            # topResults may still be present on mixed pages;
                            # product hits must win over category tiles.
                            "topResults": _TOP_RESULTS,
                        }
                    }
                }
            },
        }
    )
    + "</script></body></html>"
)

DIGIKEY_SINGLE_PRODUCT_HTML = (
    '<!DOCTYPE html><html><head><title>LM317T Detail | DigiKey</title></head>'
    '<body><div id="__next"></div>'
    '<script id="__NEXT_DATA__" type="application/json">'
    + json.dumps(
        {
            "props": {
                "pageProps": {
                    "envelope": {
                        "data": {
                            "product": _PRODUCT_RESULTS[0],
                        }
                    }
                }
            },
        }
    )
    + "</script></body></html>"
)


def test_digikey_narrow_mpn_parses_kind_product_hits(monkeypatch):
    monkeypatch.setattr(
        session, "browser_fetch",
        lambda url, site: {"ok": True, "content": DIGIKEY_PRODUCT_SEARCH_HTML})

    hits = digikey_web.ADAPTER.search("LM317T")

    assert len(hits) == 2
    assert all(h["kind"] == "product" for h in hits)
    assert hits[0]["mpn"] == "LM317T"
    assert hits[0]["digikey_pn"] == "LM317TFS-ND"
    assert hits[0]["site"] == "digikey"
    assert hits[0]["url"].startswith("https://www.digikey.com/en/products/detail/")
    assert "LM317T" in hits[0]["title"]
    assert hits[0]["price"].startswith("$")
    assert hits[0]["datasheet"].endswith(".pdf")
    # Category tiles must NOT leak into a product-results response.
    assert all(h.get("kind") != "category" for h in hits)


def test_digikey_single_product_envelope_parses(monkeypatch):
    monkeypatch.setattr(
        session, "browser_fetch",
        lambda url, site: {"ok": True, "content": DIGIKEY_SINGLE_PRODUCT_HTML})

    hits = digikey_web.ADAPTER.search("LM317T")
    assert len(hits) == 1
    assert hits[0]["kind"] == "product"
    assert hits[0]["mpn"] == "LM317T"
    assert hits[0]["price"] == "$0.63"
