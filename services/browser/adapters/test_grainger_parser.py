"""Offline search contract fixtures for grainger."""
import json

from services.browser import session
from services.browser.adapters import grainger as adapter_module


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
    html = '''<div data-testid="product-detail-wrapper">
      <a data-testid="product-detail-image-holder" href="/product/Aluminum-Flat-Bar-Stock-6061-2EYV4">
        <img data-testid="scaled-image" src="https://static.grainger.com/rp/s/is/image/Grainger/2EZA3-R2030-1_v1"></a>
      <a data-testid="product-detail-title" href="/product/Aluminum-Flat-Bar-Stock-6061-2EYV4">Aluminum Flat Bar Stock: 6061</a>
      <span>Web Price</span><span>$9.99</span></div>'''
    monkeypatch.setattr(session, "browser_fetch", lambda url, site: {"ok": True, "content": html})
    hit = adapter_module.ADAPTER.search("6061 aluminum bar")[0]
    assert (hit["kind"], hit["title"], hit["url"]) == ("product", "Aluminum Flat Bar Stock: 6061",
        "https://www.grainger.com/product/Aluminum-Flat-Bar-Stock-6061-2EYV4")
    assert hit["thumbnail"] == "https://static.grainger.com/rp/s/is/image/Grainger/2EZA3-R2030-1_v1"
    assert (hit["price"], hit["currency"]) == (9.99, "USD")

def test_currency_fallback_is_explicit():
    from services.browser.adapters._retail import RetailAdapter

    hit = {"title": "Part", "url": "/part", "price": "$12.50"}
    adapter = RetailAdapter()
    adapter.base_url = "https://example.com"
    assert adapter._normalize_hit(hit)["currency"] is None
    adapter.currency = "USD"
    assert adapter._normalize_hit(hit)["currency"] == "USD"
    assert adapter._normalize_hit({**hit, "currency": "CAD"})["currency"] == "CAD"


def test_wall_and_unparseable(monkeypatch):
    adapter = adapter_module.ADAPTER
    monkeypatch.setattr(session, "browser_fetch", lambda url, site:
                        {"ok": True, "content": adapter.challenge_markers[0]})
    assert adapter.search("bolt")[0]["reason"] == "blocked"
    monkeypatch.setattr(session, "browser_fetch", lambda url, site:
                        {"ok": True, "content": "<html>unrecognized</html>"})
    assert adapter.search("bolt")[0]["reason"] == "parse_failed"


def test_datasheet_is_document(monkeypatch, tmp_path):
    from services.browser.adapters import _retail
    url = "https://www.grainger.com/product/123"
    monkeypatch.setattr(session, "browser_fetch", lambda url, site:
                        {"ok": True, "content": '<a href="/datasheet.pdf">Datasheet</a>'})
    dest = str(tmp_path / "datasheet.pdf")
    (tmp_path / "datasheet.pdf").write_bytes(b"%PDF-1.4\n%%EOF\n")
    monkeypatch.setattr(_retail, "download_path", lambda *args, **kwargs: (dest, None))
    monkeypatch.setattr(session, "browser_download", lambda *args: {"ok": True, "dest": dest})
    result = adapter_module.ADAPTER.acquire({"url": url, "sku": "123"})
    assert result["artifact_kind"] == "document" and result["format"] == "pdf"
    assert result["artifact_path"] == dest
