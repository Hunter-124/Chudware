"""Offline search fixtures for Metals Depot product and category cards."""
import json

from services.browser import session
from services.browser.adapters import metalsdepot as depot


CARDS = '''
<header><a href="/login">Sign In</a><img src="/logo.png"></header>
<div class="product product-row card-hover-popout">
 <img src="/assets/logo.svg" alt="logo"><img data-src="/assets/angle.jpg" alt="Aluminum angle">
 <h4><a href="/steel-products/steel-angle/a36-angle">Copy 6061-T6 Aluminum Angle</a></h4>
 <div class="price"><span>$1,018.40</span></div>
</div>
<div class="product product-row">
 <h4><a href="/products/brass">Brass Bar</a></h4>
 <div>No public price</div>
</div>
<a href="/aluminum-products/aluminum-angle-6061">
 <div class="category-search-result-box"><img src="/assets/6061.webp">
  <h2>Aluminum Angle - 6061</h2></div></a>
<div class="row product-row" data-limited-stock="0" id="8">
 <span>6061-T6 Aluminum Angle</span><span class="select-a-size-zeros">0.00</span>
</div>
<footer><img src="/assets/footer.jpg">$999.00</footer>
'''


def test_product_and_category_cards_do_not_share_data():
    hits = [depot.ADAPTER._normalize_hit(hit) for hit in depot.ADAPTER.parse_search(CARDS)]
    assert [h["kind"] for h in hits] == ["product", "product", "category"]
    first, second, category = hits
    assert first["title"] == "6061-T6 Aluminum Angle"
    assert first["price"] == 1018.40 and first["currency"] is None  # "$" alone is ambiguous
    assert first["thumbnail"] == depot.BASE_URL + "/assets/angle.jpg"
    assert first["material"] == "Aluminum" and first["stock"] is None
    assert second["material"] == "Brass" and second["price"] is None
    assert second["thumbnail"] is None and second["stock"] is None
    assert category["url"] == depot.BASE_URL + "/aluminum-products/aluminum-angle-6061"
    assert category["thumbnail"] == depot.BASE_URL + "/assets/6061.webp"
    assert category["price"] is None and category["stock"] is None
    assert category["model"] == {"available": None, "formats": []}
    assert set(depot.ADAPTER.result_kinds) == {"product", "category"}
    assert not depot.ADAPTER.cad_acquisition


def test_jsonld_preserves_identity_and_material():
    obj = {"@type": "Product", "name": "Steel Angle", "url": "/angle", "sku": "A-01",
           "material": "A36", "image": "/assets/angle.png",
           "offers": {"price": "12.50", "priceCurrency": "USD", "inventoryLevel": 3}}
    page = '<script type="application/ld+json">' + json.dumps(obj) + '</script>'
    hit = depot.ADAPTER._normalize_hit(depot.ADAPTER.parse_search(page)[0])
    assert (hit["sku"], hit["material"], hit["price"], hit["currency"], hit["stock"]) == ("A-01", "A36", 12.5, "USD", 3)
    assert hit["thumbnail"] == depot.BASE_URL + "/assets/angle.png"


def test_policy_refuses_search_and_acquisition_without_network(monkeypatch):
    def unexpected(*_args, **_kwargs):
        raise AssertionError("Metals Depot requests are prohibited")
    monkeypatch.setattr(session, "browser_fetch", unexpected)
    monkeypatch.setattr(session, "browser_download", unexpected)
    notice = depot.ADAPTER.search("aluminum & angle")[0]
    assert notice["ok"] is False and notice["reason"] == "policy_refused"
    assert notice["site"] == depot.SITE and notice["op"] == "search"
    assert notice["url"] == depot.BASE_URL + "/search?q=aluminum+%26+angle"
    assert "terms-and-conditions" in notice["hint"]
    url = depot.BASE_URL + "/angle"
    failure = depot.ADAPTER.acquire({"url": url})
    assert failure["ok"] is False and failure["reason"] == "policy_refused"
    assert failure["site"] == depot.SITE and failure["op"] == "acquire" and failure["url"] == url
    assert depot.ADAPTER.acquire({})["reason"] == "policy_refused"
