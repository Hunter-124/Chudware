"""Fixture-based parser test for the Global O-Ring search adapter.

GLOBALORING_SEARCH_HTML is a VERBATIM slice of a real Global O-Ring search page
fetched live 2026-07-10 from the WooCommerce endpoint
``/?s=o-ring&post_type=product`` (HTTP 200) — two real product ``<tr>`` rows copied
byte-for-byte. The catalog is browsable with no login; each result is a
``/product/<slug>/`` anchor whose text is the O-ring part number.

Offline style mirrors test_fastenal_parser.py: session.browser_fetch monkeypatched.
"""
from __future__ import annotations

import importlib

import pytest


GLOBALORING_SEARCH_HTML = r"""<tr class="product type-product post-428967 status-publish first instock product_cat-metric-epdm-o-rings product_cat-o-rings-metric-sizes taxable shipping-taxable product-type-simple">
                            <td style="text-align: center" class="cell-md ">
                            <a class="_thumbnail" href="https://www.globaloring.com/product/e3-00x019ul/">
                    </a>
                        </td>
                                <td center="" class=" ">
                            <a href="https://www.globaloring.com/product/e3-00x019ul/">
            E3.00X019UL        </a>
                        </td>
                                <td style="font-size: 14px;" class="cell-md ">
                    3X19 METRIC UL O-RING<br>
EPDM<br>
70 DUROMETER BLACK<br>
CS: 3.00 MM (0.118 IN)<br>
ID: 19.00 MM (0.748 IN)                </td>
                                <td style="text-align: center;" data-price-column="" class=" ">
                    $ -<i class="fal fa-info-circle tooltipstered" data-tooltipster=""></i>                 </td>
                                <td style="text-align: right;" data-stock-column="" class="cell-sm ">
                            <div class="_inner tooltipstered">
            0 In Stock                            <br>
                <a href="#" data-stock-need-more="428967">Need More?</a>
                        </div>
                        </td>
                                <td style="text-align: right" class="cell-md ">
                                    </td>
                                <td style="text-align: center; line-height: 1;" class=" ">
                                <a href="https://www.globaloring.com/contact-us/">Call to Place Order</a>
                            </td>
                        </tr>
<tr class="product type-product post-428966 status-publish instock product_cat-metric-viton-75-duro-o-rings product_cat-o-rings-metric-sizes taxable shipping-taxable product-type-simple">
                            <td style="text-align: center" class="cell-md ">
                            <a class="_thumbnail" href="https://www.globaloring.com/product/bv2-65x056/">
                    </a>
                        </td>
                                <td center="" class=" ">
                            <a href="https://www.globaloring.com/product/bv2-65x056/">
            BV2.65X056        </a>
                        </td>
                                <td style="font-size: 14px;" class="cell-md ">
                    2.65X56.00 METRIC O-RING<br>
VITON (FKM) 75 DURO BROWN<br>
CS: 2.65 MM (0.104 IN)<br>
ID: 56.00 MM (2.205 IN)                </td>
                                <td style="text-align: center;" data-price-column="" class=" ">
                    $ -<i class="fal fa-info-circle tooltipstered" data-tooltipster=""></i>                 </td>
                                <td style="text-align: right;" data-stock-column="" class="cell-sm ">
                            <div class="_inner tooltipstered">
            0 In Stock                            <br>
                <a href="#" data-stock-need-more="428966">Need More?</a>
                        </div>
                        </td>
                                <td style="text-align: right" class="cell-md ">
                                    </td>
                                <td style="text-align: center; line-height: 1;" class=" ">
                                <a href="https://www.globaloring.com/contact-us/">Call to Place Order</a>
                            </td>
                        </tr>"""


@pytest.fixture()
def globaloring_mod(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    from engine import paths as engine_paths
    importlib.reload(engine_paths)
    from services.browser import profiles, session
    importlib.reload(profiles)
    importlib.reload(session)
    from services.browser import adapters as adapters_pkg
    importlib.reload(adapters_pkg)
    from services.browser.adapters import globaloring
    importlib.reload(globaloring)
    yield session, globaloring
    monkeypatch.delenv("APPDATA", raising=False)
    importlib.reload(engine_paths)


def test_globaloring_search_parses_woocommerce_rows(globaloring_mod, monkeypatch):
    session, globaloring = globaloring_mod
    monkeypatch.setattr(session, "browser_fetch",
                        lambda url, s: {"ok": True, "content": GLOBALORING_SEARCH_HTML})

    hits = globaloring.ADAPTER.search("o-ring")

    assert len(hits) == 2
    assert all(h["ok"] is True and h["site"] == "globaloring" for h in hits)

    first = hits[0]
    assert first["title"] == "E3.00X019UL"
    assert first["part_number"] == "E3.00X019UL"
    assert first["url"] == "https://www.globaloring.com/product/e3-00x019ul/"


def test_globaloring_search_url_is_woocommerce_endpoint():
    from services.browser.adapters import globaloring
    assert "?s=" in globaloring.SEARCH_URL_TMPL and "post_type=product" in globaloring.SEARCH_URL_TMPL


def test_globaloring_header_login_widget_is_not_a_wall(globaloring_mod, monkeypatch):
    # The page-wide login widget (Log In / name="password") must not trip the wall.
    session, globaloring = globaloring_mod
    page = ('<form><input name="password"></form>Log In'
            + GLOBALORING_SEARCH_HTML)
    monkeypatch.setattr(session, "browser_fetch",
                        lambda url, s: {"ok": True, "content": page})
    hits = globaloring.ADAPTER.search("o-ring")
    assert len(hits) == 2 and all(h.get("ok") for h in hits)
