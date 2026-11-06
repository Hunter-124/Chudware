"""Offline search contract fixtures for acehardware."""
import json

from services.browser import session
from services.browser.adapters import acehardware as adapter_module


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
