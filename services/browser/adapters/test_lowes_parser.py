"""Offline search contract fixtures for lowes."""
import json

from services.browser import session
from services.browser.adapters import lowes as adapter_module


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


def test_wall_and_unparseable(monkeypatch):
    adapter = adapter_module.ADAPTER
    monkeypatch.setattr(session, "browser_fetch", lambda url, site:
                        {"ok": True, "content": adapter.challenge_markers[0]})
    assert adapter.search("bolt")[0]["reason"] == "blocked"
    monkeypatch.setattr(session, "browser_fetch", lambda url, site:
                        {"ok": True, "content": "<html>unrecognized</html>"})
    assert adapter.search("bolt")[0]["reason"] == "parse_failed"


def test_preloaded_relative_image_uses_final_page_url(monkeypatch):
    state = {"title": "Drill", "pdURL": "/pd/drill", "imageUrl": "images/drill.jpg", "price": "$9.95"}
    html = '<script>window.__PRELOADED_STATE__ = ' + json.dumps(state) + ';</script>'
    monkeypatch.setattr(session, "browser_fetch", lambda url, site:
                        {"ok": True, "content": html, "final_url": "https://www.lowes.com/catalog/results"})
    hit = adapter_module.ADAPTER.search("drill")[0]
    assert hit["thumbnail"] == "https://www.lowes.com/catalog/images/drill.jpg"
    assert hit["price"] == 9.95 and hit["currency"] == "USD"
