"""Offline search contract fixtures for eightytwenty."""
import json

from services.browser import session
from services.browser.adapters import eightytwenty as adapter_module


def test_jsonld_search_contract(monkeypatch):
    adapter = adapter_module.ADAPTER
    urls = []
    product = {"@type": "Product", "name": "Fixture part", "url": "/part/123",
               "sku": "123", "image": "/images/part.jpg", "brand": {"name": "Maker"},
               "offers": {"price": "12.50", "priceCurrency": "USD"}}
    html = '<script type="application/ld+json">' + json.dumps(product) + '</script>'
    monkeypatch.setattr(session, "browser_fetch", lambda url, site: urls.append(url) or
                        {"ok": True, "content": html})
    hit = adapter.search("M3 & #1")[0]
    assert hit["kind"] == "product"
    assert hit["sku"] == "123"
    assert hit["manufacturer"] == "Maker"
    assert hit["price"] == 12.50 and hit["currency"] == "USD"
    assert hit["thumbnail"].endswith("/images/part.jpg")
    assert "%26" in urls[0] and "%23" in urls[0]

def test_anonymous_results_with_embedded_sign_in(monkeypatch):
    html = '''<title>Search results for: '2020 extrusion'</title>
      <li class="item product product-item"><div class="product-item-info">
      <a class="product photo product-item-photo" href="https://8020.net/2020.html">
      <img class="product-image-photo" src="https://8020.net/media/catalog/product/cache/c1de1d5641616e2fa80b1a002d783f40/8/0/8020_aluminum_t-slot_profile_2020.jpg"></a>
      <strong class="product name product-item-name"><a class="product-item-link" href="https://8020.net/2020.html">2020</a></strong>
      <span class="price-wrapper" data-price-amount="1.13"><span class="price">$1.13</span></span>
      </div></li><h1>Sign In</h1><form><input type="password"></form>'''
    monkeypatch.setattr(session, "browser_fetch", lambda url, site: {"ok": True, "content": html})
    hit = adapter_module.ADAPTER.search("2020 extrusion")[0]
    assert (hit["kind"], hit["title"], hit["url"]) == ("product", "2020", "https://8020.net/2020.html")
    assert hit["thumbnail"] == "https://8020.net/media/catalog/product/cache/c1de1d5641616e2fa80b1a002d783f40/8/0/8020_aluminum_t-slot_profile_2020.jpg"
    assert (hit["price"], hit["currency"]) == (1.13, "USD")


def test_wall_and_unparseable(monkeypatch):
    adapter = adapter_module.ADAPTER
    monkeypatch.setattr(session, "browser_fetch", lambda url, site:
                        {"ok": True, "content": adapter.challenge_markers[0]})
    assert adapter.search("bolt")[0]["reason"] == "blocked"
    monkeypatch.setattr(session, "browser_fetch", lambda url, site:
                        {"ok": True, "content": "<html>unrecognized</html>"})
    assert adapter.search("bolt")[0]["reason"] == "parse_failed"
