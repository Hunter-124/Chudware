"""Fixture-based parser test for the Bailey Hydraulics search adapter.

BAILEY_SEARCH_HTML is a VERBATIM slice of a real Bailey Hydraulics search page
fetched live 2026-07-10 from the BigCommerce endpoint
``/search.php?search_query=hydraulic+cylinder`` (HTTP 200) — two real
``<article class="card">`` tiles copied byte-for-byte. Same BigCommerce platform
as Surplus Center; parsed from the card-title anchor + data-product-id + price.

Offline style mirrors test_fastenal_parser.py: session.browser_fetch monkeypatched.
"""
from __future__ import annotations

import importlib

import pytest


BAILEY_SEARCH_HTML = r"""<article class="card has-featured-specs card--cart-enabled  ">
    <div class="compare-div">
        <label class="button button--compare" for="compare-950">
            <input type="checkbox" name="products[]" value="950" id="compare-950" data-compare-id="950"> <span class="compare_text">Compare</span> 
        </label>
        </div>

    <figure class="card-figure">
        <div class="card-tags">
        </div>
            <a class="card-figure-link" href="https://www.baileyhydraulics.com/CYLINDER-WELDED-3-X-42-1.50-ROD-CHIEF-WX-3000-PSI/?searchid=3529131&amp;search_query=hydraulic+cylinder">
                <div class="card-img-container">
                    <img src="https://cdn11.bigcommerce.com/s-xyc0gzbo80/images/stencil/500x659/products/950/25345/chief-wx-welded-hydraulic-cylinder__24804.1738166441.jpg?c=1" alt="" title="" data-sizes="auto" srcset="https://cdn11.bigcommerce.com/s-xyc0gzbo80/images/stencil/80w/products/950/25345/chief-wx-welded-hydraulic-cylinder__24804.1738166441.jpg?c=1 80w, https://cdn11.bigcommerce.com/s-xyc0gzbo80/images/stencil/160w/products/950/25345/chief-wx-welded-hydraulic-cylinder__24804.1738166441.jpg?c=1 160w, https://cdn11.bigcommerce.com/s-xyc0gzbo80/images/stencil/320w/products/950/25345/chief-wx-welded-hydraulic-cylinder__24804.1738166441.jpg?c=1 320w, https://cdn11.bigcommerce.com/s-xyc0gzbo80/images/stencil/640w/products/950/25345/chief-wx-welded-hydraulic-cylinder__24804.1738166441.jpg?c=1 640w, https://cdn11.bigcommerce.com/s-xyc0gzbo80/images/stencil/960w/products/950/25345/chief-wx-welded-hydraulic-cylinder__24804.1738166441.jpg?c=1 960w, https://cdn11.bigcommerce.com/s-xyc0gzbo80/images/stencil/1280w/products/950/25345/chief-wx-welded-hydraulic-cylinder__24804.1738166441.jpg?c=1 1280w, https://cdn11.bigcommerce.com/s-xyc0gzbo80/images/stencil/1920w/products/950/25345/chief-wx-welded-hydraulic-cylinder__24804.1738166441.jpg?c=1 1920w, https://cdn11.bigcommerce.com/s-xyc0gzbo80/images/stencil/2560w/products/950/25345/chief-wx-welded-hydraulic-cylinder__24804.1738166441.jpg?c=1 2560w" data-srcset="https://cdn11.bigcommerce.com/s-xyc0gzbo80/images/stencil/80w/products/950/25345/chief-wx-welded-hydraulic-cylinder__24804.1738166441.jpg?c=1 80w, https://cdn11.bigcommerce.com/s-xyc0gzbo80/images/stencil/160w/products/950/25345/chief-wx-welded-hydraulic-cylinder__24804.1738166441.jpg?c=1 160w, https://cdn11.bigcommerce.com/s-xyc0gzbo80/images/stencil/320w/products/950/25345/chief-wx-welded-hydraulic-cylinder__24804.1738166441.jpg?c=1 320w, https://cdn11.bigcommerce.com/s-xyc0gzbo80/images/stencil/640w/products/950/25345/chief-wx-welded-hydraulic-cylinder__24804.1738166441.jpg?c=1 640w, https://cdn11.bigcommerce.com/s-xyc0gzbo80/images/stencil/960w/products/950/25345/chief-wx-welded-hydraulic-cylinder__24804.1738166441.jpg?c=1 960w, https://cdn11.bigcommerce.com/s-xyc0gzbo80/images/stencil/1280w/products/950/25345/chief-wx-welded-hydraulic-cylinder__24804.1738166441.jpg?c=1 1280w, https://cdn11.bigcommerce.com/s-xyc0gzbo80/images/stencil/1920w/products/950/25345/chief-wx-welded-hydraulic-cylinder__24804.1738166441.jpg?c=1 1920w, https://cdn11.bigcommerce.com/s-xyc0gzbo80/images/stencil/2560w/products/950/25345/chief-wx-welded-hydraulic-cylinder__24804.1738166441.jpg?c=1 2560w" class="card-image lazyautosizes lazyloaded" loading="lazy" sizes="220px">
                </div>
            </a>
        <figcaption class="card-figcaption">
            <div class="card-figcaption-body">
                                <a href="#" class="button button--small card-figcaption-button quickview" data-product-id="950">Quick view</a>
            </div>
        </figcaption>
    </figure>
    <div class="card-section card-section--body">
            <div class="card-brandwrap">
                    <p class="card-text card-text--brand" data-test-info-type="brandName">Chief</p>
                    <p class="card-text card-text--sku">SKU: 207454</p>
            </div>
        <h4 class="card-title">
                <a href="https://www.baileyhydraulics.com/CYLINDER-WELDED-3-X-42-1.50-ROD-CHIEF-WX-3000-PSI/?searchid=3529131&amp;search_query=hydraulic+cylinder">WX Welded Hydraulic Cylinder: 3 Bore x 42 Stroke: 1.5 Rod</a>
        </h4>
            <p class="card-text card-rating" data-test-info-type="productRating">
                <span class="rating--small">
                            <span class="icon icon--ratingFull">
            <svg>
                <use xlink:href="#icon-star"></use>
            </svg>
        </span>
        <span class="icon icon--ratingFull">
            <svg>
                <use xlink:href="#icon-star"></use>
            </svg>
        </span>
        <span class="icon icon--ratingFull">
            <svg>
                <use xlink:href="#icon-star"></use>
            </svg>
        </span>
        <span class="icon icon--ratingFull">
            <svg>
                <use xlink:href="#icon-star"></use>
            </svg>
        </span>
        <span class="icon icon--ratingFull">
            <svg>
                <use xlink:href="#icon-star"></use>
            </svg>
        </span>
<!-- snippet location product_rating -->
                </span>

                <span class="cardRating-count"><a href="https://www.baileyhydraulics.com/CYLINDER-WELDED-3-X-42-1.50-ROD-CHIEF-WX-3000-PSI/?searchid=3529131&amp;search_query=hydraulic+cylinder#product-reviews">3 reviews</a></span>
            </p>

</div>

        <div class="specs-wrapper">
  <div class="featured-specs-container">
    <table class="featured-specs">
        <thead>
          <tr>
                                <th class="featured-spec-name" data-product-id="8236" data-name="Custom_FeaturedSpec_1_Bore">Bore</th>
                                <th class="featured-spec-name" data-product-id="8242" data-name="Custom_FeaturedSpec_2_Stroke">Stroke</th>
                                <th class="featured-spec-name" data-product-id="8249" data-name="Custom_FeaturedSpec_3_PSI">PSI</th>
                                <th class="featured-spec-name" data-product-id="8256" data-name="Custom_FeaturedSpec_4_Pin Diameter">Pin Diameter</th>
                                <th class="featured-spec-name" data-product-id="8262" data-name="Custom_FeaturedSpec_5_Retracted">Retracted</th>
                                <th class="featured-spec-name" data-product-id="66945" data-name="Custom_FeaturedSpec_6_Warranty">Warranty</th>
          </tr>
        </thead>
        <tbody>
          <tr>
                    <td class="featured-spec-value" data-product-id="8236" data-name="Custom_FeaturedSpec_1_Bore">3 in</td>   
                    <td class="featured-spec-value" data-product-id="8242" data-name="Custom_FeaturedSpec_2_Stroke">42 in</td>   
                    <td class="featured-spec-value" data-product-id="8249" data-name="Custom_FeaturedSpec_3_PSI">3000</td>   
                    <td class="featured-spec-value" data-product-id="8256" data-name="Custom_FeaturedSpec_4_Pin Diameter">1 in</td>   
                    <td class="featured-spec-value" data-product-id="8262" data-name="Custom_FeaturedSpec_5_Retracted">50 in</td>   
                    <td class="featured-spec-value" data-product-id="66945" data-name="Custom_FeaturedSpec_6_Warranty">3 Year Limited</td>   
          </tr>
        </tbody>
    </table>
  </div>
</div>

<div class="card-section-rightWrapper">
        <div class="card-section card-section--price">
            <div class="card-text card-text--price" data-test-info-type="price">
                        
        <div class="price-section">
            <div class="price-section--label">
                MSRP:
            </div>

            <div class="price-section--wrapper">
                    <div class="price-section--withoutTax rrp-price--withoutTax">
                        <span data-product-rrp-price-without-tax="" class="price price--rrp">
                            $810.92
                        </span>
                    </div>
            </div>
        </div>
        <div class="price-section" style="display: none;">
            <div class="price-section--label price--sale-label">
                Was:
            </div>

            <div class="price-section--wrapper">
                    <div class="price-section--withoutTax non-sale-price--withoutTax">
                        <span data-product-non-sale-price-without-tax="" class="price price--non-sale">
                            
                        </span>
                    </div>
            </div>
        </div>
        <div class="price-section">
            <div class="price-section--label" style="display: none;">
                <span class="price-label">
                    Price
                </span>
                <span class="price-now-label">
                    Now
                </span>
            </div>
            <div class="price-section--wrapper">
                    <div class="price-section--withoutTax price-section--primary">
                        <span data-product-price-without-tax="" class="price price--withoutTax price-primary">$608.18</span>
                    </div>
            </div>
        </div>
            </div>
                    </div>
        <div class="card-section-rightWrapper--inner">
                    <div class="card-section--buttons card-section--quantity">
                                    <form class="form form-cardAdd" method="post" action="https://www.baileyhydraulics.com/cart.php" enctype="multipart/form-data" data-cart-item-add-from-card="">
                                        <div class="add-card-popup"></div>
                                        <label class="label-cardQuantity" for="qty[]">Quantity</label>
                                        <input type="hidden" name="action" value="add">
                                        <input type="hidden" name="product_id" value="950">
                                        <input class="form-input form-input-qty" name="qty[]" type="tel" value="1" data-quantity-min="" data-quantity-max="" min="1" pattern="[0-9]*" aria-live="polite" autocomplete="off">
                                        <input data-wait-message="Adding…" data-event-type="product-click" data-product-id="950" class="button button--primary button--cardAdd" type="submit" value="Add to Cart">
                                    </form>
                    </div>
                        <div class="card-section card-section--availability card-section--show card-section--show">
                                <div class="card-availability">In Stock and Ships Fast¹</div>
                                        <div class="card-stock card-stock--inStock">54 In Stock</div>
                                <div class="card-qtyAdded" data-qtyincart="950"></div>
                        </div>
        </div>
    </div>
</article>
        </li>
        <li class="productCard productCard--grid">
            
        
        
        
        
        
        
<article class="card has-featured-specs card--cart-enabled  ">
    <div class="compare-div">
        <label class="button button--compare" for="compare-957">
            <input type="checkbox" name="products[]" value="957" id="compare-957" data-compare-id="957"> <span class="compare_text">Compare</span> 
        </label>
        </div>

    <figure class="card-figure">
        <div class="card-tags">
        </div>
            <a class="card-figure-link" href="https://www.baileyhydraulics.com/Chief-WX-3000-PSI-Welded-Crosstubes-3-5-x-42-1-3-4-Rod-Dia-/?searchid=3529131&amp;search_query=hydraulic+cylinder">
                <div class="card-img-container">
                    <img src="https://cdn11.bigcommerce.com/s-xyc0gzbo80/images/stencil/500x659/products/957/25423/chief-wx-welded-hydraulic-cylinder__25005.1738166480.jpg?c=1" alt="WX Welded Hydraulic Cylinder: 3.5 Bore x 42 Stroke: 1.75 Rod" title="WX Welded Hydraulic Cylinder: 3.5 Bore x 42 Stroke: 1.75 Rod" data-sizes="auto" srcset="https://cdn11.bigcommerce.com/s-xyc0gzbo80/images/stencil/80w/products/957/25423/chief-wx-welded-hydraulic-cylinder__25005.1738166480.jpg?c=1 80w, https://cdn11.bigcommerce.com/s-xyc0gzbo80/images/stencil/160w/products/957/25423/chief-wx-welded-hydraulic-cylinder__25005.1738166480.jpg?c=1 160w, https://cdn11.bigcommerce.com/s-xyc0gzbo80/images/stencil/320w/products/957/25423/chief-wx-welded-hydraulic-cylinder__25005.1738166480.jpg?c=1 320w, https://cdn11.bigcommerce.com/s-xyc0gzbo80/images/stencil/640w/products/957/25423/chief-wx-welded-hydraulic-cylinder__25005.1738166480.jpg?c=1 640w, https://cdn11.bigcommerce.com/s-xyc0gzbo80/images/stencil/960w/products/957/25423/chief-wx-welded-hydraulic-cylinder__25005.1738166480.jpg?c=1 960w, https://cdn11.bigcommerce.com/s-xyc0gzbo80/images/stencil/1280w/products/957/25423/chief-wx-welded-hydraulic-cylinder__25005.1738166480.jpg?c=1 1280w, https://cdn11.bigcommerce.com/s-xyc0gzbo80/images/stencil/1920w/products/957/25423/chief-wx-welded-hydraulic-cylinder__25005.1738166480.jpg?c=1 1920w, https://cdn11.bigcommerce.com/s-xyc0gzbo80/images/stencil/2560w/products/957/25423/chief-wx-welded-hydraulic-cylinder__25005.1738166480.jpg?c=1 2560w" data-srcset="https://cdn11.bigcommerce.com/s-xyc0gzbo80/images/stencil/80w/products/957/25423/chief-wx-welded-hydraulic-cylinder__25005.1738166480.jpg?c=1 80w, https://cdn11.bigcommerce.com/s-xyc0gzbo80/images/stencil/160w/products/957/25423/chief-wx-welded-hydraulic-cylinder__25005.1738166480.jpg?c=1 160w, https://cdn11.bigcommerce.com/s-xyc0gzbo80/images/stencil/320w/products/957/25423/chief-wx-welded-hydraulic-cylinder__25005.1738166480.jpg?c=1 320w, https://cdn11.bigcommerce.com/s-xyc0gzbo80/images/stencil/640w/products/957/25423/chief-wx-welded-hydraulic-cylinder__25005.1738166480.jpg?c=1 640w, https://cdn11.bigcommerce.com/s-xyc0gzbo80/images/stencil/960w/products/957/25423/chief-wx-welded-hydraulic-cylinder__25005.1738166480.jpg?c=1 960w, https://cdn11.bigcommerce.com/s-xyc0gzbo80/images/stencil/1280w/products/957/25423/chief-wx-welded-hydraulic-cylinder__25005.1738166480.jpg?c=1 1280w, https://cdn11.bigcommerce.com/s-xyc0gzbo80/images/stencil/1920w/products/957/25423/chief-wx-welded-hydraulic-cylinder__25005.1738166480.jpg?c=1 1920w, https://cdn11.bigcommerce.com/s-xyc0gzbo80/images/stencil/2560w/products/957/25423/chief-wx-welded-hydraulic-cylinder__25005.1738166480.jpg?c=1 2560w" class="card-image lazyautosizes lazyloaded" loading="lazy" sizes="220px">
                </div>
            </a>
        <figcaption class="card-figcaption">
            <div class="card-figcaption-body">
                                <a href="#" class="button button--small card-figcaption-button quickview" data-product-id="957">Quick view</a>
            </div>
        </figcaption>
    </figure>
    <div class="card-section card-section--body">
            <div class="card-brandwrap">
                    <p class="card-text card-text--brand" data-test-info-type="brandName">Chief</p>
                    <p class="card-text card-text--sku">SKU: 207484</p>
            </div>
        <h4 class="card-title">
                <a href="https://www.baileyhydraulics.com/Chief-WX-3000-PSI-Welded-Crosstubes-3-5-x-42-1-3-4-Rod-Dia-/?searchid=3529131&amp;search_query=hydraulic+cylinder">WX Welded Hydraulic Cylinder: 3.5 Bore x 42 Stroke: 1.75 Rod</a>
        </h4>
            <p class="card-text card-rating" data-test-info-type="productRating">
                <span class="rating--small">
                            <span class="icon icon--ratingFull">
            <svg>
                <use xlink:href="#icon-star"></use>
            </svg>
        </span>
        <span class="icon icon--ratingFull">
            <svg>
                <use xlink:href="#icon-star"></use>
            </svg>
        </span>
        <span class="icon icon--ratingFull">
            <svg>
                <use xlink:href="#icon-star"></use>
            </svg>
        </span>
        <span class="icon icon--ratingFull">
            <svg>
                <use xlink:href="#icon-star"></use>
            </svg>
        </span>
        <span class="icon icon--ratingFull">
            <svg>
                <use xlink:href="#icon-star"></use>
            </svg>
        </span>
<!-- snippet location product_rating -->
                </span>

                <span class="cardRating-count"><a href="https://www.baileyhydraulics.com/Chief-WX-3000-PSI-Welded-Crosstubes-3-5-x-42-1-3-4-Rod-Dia-/?searchid=3529131&amp;search_query=hydraulic+cylinder#product-reviews">3 reviews</a></span>
            </p>

</div>

        <div class="specs-wrapper">
  <div class="featured-specs-container">
    <table class="featured-specs">
        <thead>
          <tr>
                                <th class="featured-spec-name" data-product-id="7403" data-name="Custom_FeaturedSpec_1_Bore">Bore</th>
                                <th class="featured-spec-name" data-product-id="7411" data-name="Custom_FeaturedSpec_2_Stroke">Stroke</th>
                                <th class="featured-spec-name" data-product-id="7419" data-name="Custom_FeaturedSpec_3_PSI">PSI</th>
                                <th class="featured-spec-name" data-product-id="7425" data-name="Custom_FeaturedSpec_4_Pin Diameter">Pin Diameter</th>
                                <th class="featured-spec-name" data-product-id="7431" data-name="Custom_FeaturedSpec_5_Retracted">Retracted</th>
                                <th class="featured-spec-name" data-product-id="66957" data-name="Custom_FeaturedSpec_6_Warranty">Warranty</th>
          </tr>
        </thead>
        <tbody>
          <tr>
                    <td class="featured-spec-value" data-product-id="7403" data-name="Custom_FeaturedSpec_1_Bore">3.5 in</td>   
                    <td class="featured-spec-value" data-product-id="7411" data-name="Custom_FeaturedSpec_2_Stroke">42 in</td>   
                    <td class="featured-spec-value" data-product-id="7419" data-name="Custom_FeaturedSpec_3_PSI">3000</td>   
                    <td class="featured-spec-value" data-product-id="7425" data-name="Custom_FeaturedSpec_4_Pin Diameter">1 in</td>   
                    <td class="featured-spec-value" data-product-id="7431" data-name="Custom_FeaturedSpec_5_Retracted">50 in</td>   
                    <td class="featured-spec-value" data-product-id="66957" data-name="Custom_FeaturedSpec_6_Warranty">3 Year Limited</td>   
          </tr>
        </tbody>
    </table>
  </div>
</div>

<div class="card-section-rightWrapper">
        <div class="card-section card-section--price">
            <div class="card-text card-text--price" data-test-info-type="price">
                        
        <div class="price-section">
            <div class="price-section--label">
                MSRP:
            </div>

            <div class="price-section--wrapper">
                    <div class="price-section--withoutTax rrp-price--withoutTax">
                        <span data-product-rrp-price-without-tax="" class="price price--rrp">
                            $1,001.52
                        </span>
                    </div>
            </div>
        </div>
        <div class="price-section" style="display: none;">
            <div class="price-section--label price--sale-label">
                Was:
            </div>

            <div class="price-section--wrapper">
                    <div class="price-section--withoutTax non-sale-price--withoutTax">
                        <span data-product-non-sale-price-without-tax="" class="price price--non-sale">
                            
                        </span>
                    </div>
            </div>
        </div>
        <div class="price-section">
            <div class="price-section--label" style="display: none;">
                <span class="price-label">
                    Price
                </span>
                <span class="price-now-label">
                    Now
                </span>
            </div>
            <div class="price-section--wrapper">
                    <div class="price-section--withoutTax price-section--primary">
                        <span data-product-price-without-tax="" class="price price--withoutTax price-primary">$751.14</span>
                    </div>
            </div>
        </div>
            </div>
                    </div>
        <div class="card-section-rightWrapper--inner">
                    <div class="card-section--buttons card-section--quantity">
                                    <form class="form form-cardAdd" method="post" action="https://www.baileyhydraulics.com/cart.php" enctype="multipart/form-data" data-cart-item-add-from-card="">
                                        <div class="add-card-popup"></div>
                                        <label class="label-cardQuantity" for="qty[]">Quantity</label>
                                        <input type="hidden" name="action" value="add">
                                        <input type="hidden" name="product_id" value="957">
                                        <input class="form-input form-input-qty" name="qty[]" type="tel" value="1" data-quantity-min="" data-quantity-max="" min="1" pattern="[0-9]*" aria-live="polite" autocomplete="off">
                                        <input data-wait-message="Adding…" data-event-type="product-click" data-product-id="957" class="button button--primary button--cardAdd" type="submit" value="Add to Cart">
                                    </form>
                    </div>
                        <div class="card-section card-section--availability card-section--show card-section--show">
                                <div class="card-availability">In Stock and Ships Fast¹</div>
                                        <div class="card-stock card-stock--inStock">1 In Stock</div>
                                <div class="card-qtyAdded" data-qtyincart="957"></div>
                        </div>
        </div>
    </div>
</article>
        </li>
        <li class="productCard productCard--grid">
            
        
        
        
        
        
        
"""


@pytest.fixture()
def bailey_mod(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    from engine import paths as engine_paths
    importlib.reload(engine_paths)
    from services.browser import profiles, session
    importlib.reload(profiles)
    importlib.reload(session)
    from services.browser import adapters as adapters_pkg
    importlib.reload(adapters_pkg)
    from services.browser.adapters import baileyhydraulics
    importlib.reload(baileyhydraulics)
    yield session, baileyhydraulics
    monkeypatch.delenv("APPDATA", raising=False)
    importlib.reload(engine_paths)


def test_bailey_search_parses_bigcommerce_cards(bailey_mod, monkeypatch):
    session, baileyhydraulics = bailey_mod
    monkeypatch.setattr(session, "browser_fetch",
                        lambda url, s: {"ok": True, "content": BAILEY_SEARCH_HTML})

    hits = baileyhydraulics.ADAPTER.search("hydraulic cylinder")

    assert len(hits) == 2
    assert all(h["ok"] is True and h["site"] == "baileyhydraulics" for h in hits)

    first = hits[0]
    assert first["title"] == "WX Welded Hydraulic Cylinder: 3 Bore x 42 Stroke: 1.5 Rod"
    assert first["url"] == (
        "https://www.baileyhydraulics.com/"
        "CYLINDER-WELDED-3-X-42-1.50-ROD-CHIEF-WX-3000-PSI/")
    assert first["sku"] == "950"
    assert first["price"] == "$608.18"


def test_bailey_search_url_is_bigcommerce_endpoint():
    from services.browser.adapters import baileyhydraulics
    assert "search.php?search_query=" in baileyhydraulics.SEARCH_URL_TMPL
