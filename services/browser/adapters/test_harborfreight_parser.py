"""Offline search contract fixtures for harborfreight."""
import json

from services.browser import session
from services.browser.adapters import harborfreight as adapter_module


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

def test_rendered_search_card(monkeypatch):
    html = '''<li class="item__wrap--QA10jp product-grid-item"><div>
      <a class="item__itemTop--0tIhL9" href="/6-in-composite-digital-caliper-63586.html">
      <img class="productImage" src="https://www.harborfreight.com/media/catalog/product/cache/87bb6fe55719635edbf9f739e6fd104e/6/3/63586_W7.jpg?auto=webp&amp;width=425&amp;height=425&amp;canvas=425,425&amp;fit=bounds">
      <p class="item__name--17mqeF"><span class="item__brand--PkHELw">PITTSBURGH</span> 6 in. Composite Digital Caliper</p></a>
      <span class="styled-price__styledPrice--Lhb-uR"><span class="sr-only">11 dollars and 99 cents</span>
      <span aria-hidden="true"><sup>$</sup>11<span class="sr-only">.</span><sup>99</sup></span></span></div></li>'''
    monkeypatch.setattr(session, "browser_fetch", lambda url, site: {"ok": True, "content": html})
    hit = adapter_module.ADAPTER.search("digital caliper")[0]
    assert (hit["kind"], hit["title"], hit["url"]) == ("product", "PITTSBURGH 6 in. Composite Digital Caliper",
        "https://www.harborfreight.com/6-in-composite-digital-caliper-63586.html")
    assert hit["thumbnail"] == ("https://www.harborfreight.com/media/catalog/product/cache/87bb6fe55719635edbf9f739e6fd104e/6/3/63586_W7.jpg"
                                "?auto=webp&width=425&height=425&canvas=425,425&fit=bounds")
    assert (hit["price"], hit["currency"]) == (11.99, "USD")


def test_wall_and_unparseable(monkeypatch):
    adapter = adapter_module.ADAPTER
    monkeypatch.setattr(session, "browser_fetch", lambda url, site:
                        {"ok": True, "content": adapter.challenge_markers[0]})
    assert adapter.search("bolt")[0]["reason"] == "blocked"
    monkeypatch.setattr(session, "browser_fetch", lambda url, site:
                        {"ok": True, "content": "<html>unrecognized</html>"})
    assert adapter.search("bolt")[0]["reason"] == "parse_failed"
