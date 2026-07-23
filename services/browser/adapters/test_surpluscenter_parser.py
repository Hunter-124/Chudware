"""Fixture-based parser test for the Surplus Center search adapter.

SURPLUSCENTER_SEARCH_HTML is a VERBATIM slice of a real Surplus Center search
page fetched live 2026-07-10 from the BigCommerce endpoint
``/search.php?search_query=hydraulic+pump`` (HTTP 200). It holds two real
``<article class="card">`` tiles copied byte-for-byte. Surplus Center ships NO
JSON-LD on its SRP, so the adapter parses the card-title anchor (URL + title),
the quick-view ``data-product-id`` (SKU) and the ``price--withoutTax`` span.

Cloudflare injects a passive ``challenge-platform`` script into these pages; that
alone is NOT a wall (see test_hardening.py) — the page is real product content.

Offline style mirrors test_fastenal_parser.py: session.browser_fetch monkeypatched.
"""
from __future__ import annotations

import importlib

import pytest


SURPLUSCENTER_SEARCH_HTML = r"""<article class="card compare-disabled price-visibility brand-hidden brand-sku-active card-title-fourteen ratings-disabled">
    <figure class="card-figure  multiple-images ">

        <a class="image-link desktop" href="https://surpluscenter.com/viking-houdaille-hyd-gear-pump-fv-638/?searchid=1338894&amp;search_query=hydraulic+pump">
            <img class="card-image primary lazyload" data-sizes="auto" src="https://cdn11.bigcommerce.com/s-3nwcvn3gzw/stencil/fe19d3d0-9e39-013e-5e91-6a84a51c32ae/e/d0878f30-5ebc-013f-d9ef-7208a72043e5/img/loading.svg" data-src="https://cdn11.bigcommerce.com/s-3nwcvn3gzw/images/stencil/500x659/products/13906/119678/WC9-617__06984.1762811085.JPG?c=1" alt="Viking Pump Houdaille FV 638 Hydraulic Gear Pump" title="Viking Pump Houdaille FV 638 Hydraulic Gear Pump">
          </a>

        <figcaption class="card-figcaption quick-view active">
            <div class="card-figcaption-body">
                          <a href="#" class="button button--small card-figcaption-button quickview" data-product-id="13906">Quick view</a>
            </div>
        </figcaption>
    </figure>

    <hr class="card-img-underscore">


          <div class="card-body purchase-ability withoutTax ratings-disabled" data-test-info-type="price">

              <h4 class="card-title">
                  <a href="https://surpluscenter.com/viking-houdaille-hyd-gear-pump-fv-638/?searchid=1338894&amp;search_query=hydraulic+pump">Viking Pump Houdaille FV 638 Hydraulic Gear Pump</a>
              </h4>

                <p class="product-card-stock-level">5 In Stock</p>


              <div class="card-text" data-test-info-type="price">
                  <div class="price-visibility">
                      
      <div class="msrp-sale-regular-price-section withoutTax">
        <div class="price-section price-section--withoutTax rrp-price--withoutTax" style="display: none;">
            <span class="price-label">Was:</span>
            <span data-product-rrp-price-without-tax="" class="price price--rrp">
                
            </span>
        </div>
        <div class="price-section price-section--withoutTax non-sale-price--withoutTax" style="display: none;">
            <span class="price-label">Was:</span>
            <span data-product-non-sale-price-without-tax="" class="price price--non-sale">
                
            </span>
        </div>
        </div>
        <div class="price-section price-section--withoutTax current-price regular-price">
            <span class="price-label">
                
            </span>

              <span class="price-label price-now-label" style="display: none;">
                Now:
              </span>

            <span data-product-price-without-tax="" class="price price--withoutTax">$79.95</span>
            

    
                  </div>
              </div>
                            <div class="card-text add-to-cart-button">
                              <a href="https://surpluscenter.com/cart.php?action=add&amp;product_id=13906&amp;searchid=1338894&amp;search_query=hydraulic+pump" class="button button--small card-figcaption-button">Add to Cart</a>

              </div>
          </div>

        </div></article>
        </li>
        <li class="product is-root">
            <article class="card compare-disabled price-visibility brand-hidden brand-sku-active card-title-fourteen ratings-disabled">
    <figure class="card-figure  multiple-images ">

        <a class="image-link desktop" href="https://surpluscenter.com/hp61-da80-brand-hand-hyd-pump/?searchid=1338894&amp;search_query=hydraulic+pump">
            <img class="card-image primary lazyload" data-sizes="auto" src="https://cdn11.bigcommerce.com/s-3nwcvn3gzw/stencil/fe19d3d0-9e39-013e-5e91-6a84a51c32ae/e/d0878f30-5ebc-013f-d9ef-7208a72043e5/img/loading.svg" data-src="https://cdn11.bigcommerce.com/s-3nwcvn3gzw/images/stencil/500x659/products/687/107295/9-4144__83974.1762808031.jpg?c=1" alt="Hydraulic Hand Pump Brand Hydraulics HP61-DA80" title="Hydraulic Hand Pump Brand Hydraulics HP61-DA80">
          </a>

        <figcaption class="card-figcaption quick-view active">
            <div class="card-figcaption-body">
                          <a href="#" class="button button--small card-figcaption-button quickview" data-product-id="687">Quick view</a>
            </div>
        </figcaption>
    </figure>

    <hr class="card-img-underscore">


          <div class="card-body purchase-ability withoutTax ratings-disabled" data-test-info-type="price">

              <h4 class="card-title">
                  <a href="https://surpluscenter.com/hp61-da80-brand-hand-hyd-pump/?searchid=1338894&amp;search_query=hydraulic+pump">Hydraulic Hand Pump Brand Hydraulics HP61-DA80</a>
              </h4>

                <p class="product-card-stock-level">55 In Stock</p>


              <div class="card-text" data-test-info-type="price">
                  <div class="price-visibility">
                      
      <div class="msrp-sale-regular-price-section withoutTax">
        <div class="price-section price-section--withoutTax rrp-price--withoutTax" style="display: none;">
            <span class="price-label">Was:</span>
            <span data-product-rrp-price-without-tax="" class="price price--rrp">
                
            </span>
        </div>
        <div class="price-section price-section--withoutTax non-sale-price--withoutTax" style="display: none;">
            <span class="price-label">Was:</span>
            <span data-product-non-sale-price-without-tax="" class="price price--non-sale">
                
            </span>
        </div>
        </div>
        <div class="price-section price-section--withoutTax current-price regular-price">
            <span class="price-label">
                
            </span>

              <span class="price-label price-now-label" style="display: none;">
                Now:
              </span>

            <span data-product-price-without-tax="" class="price price--withoutTax">$299.80</span>
            

    
                  </div>
              </div>
                            <div class="card-text add-to-cart-button">
                              <a href="https://surpluscenter.com/cart.php?action=add&amp;product_id=687&amp;searchid=1338894&amp;search_query=hydraulic+pump" class="button button--small card-figcaption-button">Add to Cart</a>

              </div>
          </div>

        </div></article>
        </li>
        <li class="product is-root">
            """


@pytest.fixture()
def surpluscenter_mod(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    from services import paths as engine_paths
    importlib.reload(engine_paths)
    from services.browser import profiles, session
    importlib.reload(profiles)
    importlib.reload(session)
    from services.browser import adapters as adapters_pkg
    importlib.reload(adapters_pkg)
    from services.browser.adapters import surpluscenter
    importlib.reload(surpluscenter)
    yield session, surpluscenter
    monkeypatch.delenv("APPDATA", raising=False)
    importlib.reload(engine_paths)


def test_surpluscenter_search_parses_bigcommerce_cards(surpluscenter_mod, monkeypatch):
    session, surpluscenter = surpluscenter_mod
    monkeypatch.setattr(session, "browser_fetch",
                        lambda url, s: {"ok": True, "content": SURPLUSCENTER_SEARCH_HTML})

    hits = surpluscenter.ADAPTER.search("hydraulic pump")

    assert len(hits) == 2
    assert all(h["ok"] is True and h["site"] == "surpluscenter" for h in hits)

    first = hits[0]
    assert first["title"] == "Viking Pump Houdaille FV 638 Hydraulic Gear Pump"
    assert first["url"] == "https://surpluscenter.com/viking-houdaille-hyd-gear-pump-fv-638/"
    assert first["sku"] == "13906"
    assert first["price"] == "$79.95"


def test_surpluscenter_search_url_is_bigcommerce_endpoint():
    from services.browser.adapters import surpluscenter
    assert "search.php?search_query=" in surpluscenter.SEARCH_URL_TMPL
