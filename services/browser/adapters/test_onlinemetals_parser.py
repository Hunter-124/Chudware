"""Offline OnlineMetals JSON-LD and acquisition fixtures."""
import json

import pytest

from services.browser import session
from services.browser.adapters import onlinemetals as online


def _page(product):
    return '<a href="/login">Sign In</a><script type="application/ld+json">' + json.dumps(product) + '</script>'


def test_jsonld_preserves_search_data(monkeypatch):
    obj = {"@context": "https://schema.org", "@type": "ItemList", "itemListElement": [
        {"@type": "ListItem", "item": {"@type": "Product", "name": "6061-T6 Aluminum Round Tube",
         "url": "/en/buy/aluminum/tube", "sku": "1109", "mpn": "TUBE-1",
         "brand": {"name": "Mill"}, "image": {"url": "/images/tube.jpg"},
         "offers": {"price": "8.42", "priceCurrency": "USD", "inventoryLevel": {"value": 0}}}}]}
    requests = []
    def fetch(url, site):
        requests.append((url, site))
        return {"ok": True, "content": _page(obj)}
    monkeypatch.setattr(session, "browser_fetch", fetch)
    hit = online.ADAPTER.search("tube 1/2 & 6061")[0]
    assert requests == [(online.BASE_URL + "/en/search?q=tube+1%2F2+%26+6061", online.SITE)]
    assert hit["kind"] == "product" and hit["url"] == online.BASE_URL + "/en/buy/aluminum/tube"
    assert (hit["sku"], hit["mpn"], hit["manufacturer"]) == ("1109", "TUBE-1", "Mill")
    assert (hit["price"], hit["currency"], hit["stock"], hit["material"]) == (8.42, "USD", 0, "Aluminum")
    assert hit["thumbnail"] == online.BASE_URL + "/images/tube.jpg"
    assert hit["model"] == {"available": None, "formats": []}
    assert online.ADAPTER.result_kinds == ("product",) and not online.ADAPTER.cad_acquisition

def test_rendered_search_card_without_price(monkeypatch):
    html = '''<div class="product-item">
      <a class="thumb" data-qa-id="plp-item-image" href="/en/buy/copper/1-16-3-32-bendable-copper-rod-assortment-110-12-long-2-pieces-each/pid/mp-00000527"
         title="(1/16&quot;, 3/32&quot;) Bendable Copper Rod Assortment 110 - 12&quot; Long (2 Pieces Each)">
        <img src="/medias/300Wx300H-null?context=bWFzdGVyfHJvb3R8NTkzNHxpbWFnZS9qcGVnfGFHUXlMMmhrWkM4eE1EQXhOekUxTlRNeE56YzVNQzh6TURCWGVETXdNRWhmYm5Wc2JBfDU5MDljZDk2ZWM3Y2IzMzdlZDFkZTk4Mjc5ODAzNjI1OGQxOGM0N2Q3YTU2NmRkMjJiM2YyZTJmZGY2NTQzN2U" alt="(1/16&quot;, 3/32&quot;) Bendable Copper Rod Assortment 110 - 12&quot; Long (2 Pieces Each)" data-qa-attribute="comparison-item-img"></a>
      <a class="name" data-qa-id="plp-item-name" href="/en/buy/copper/1-16-3-32-bendable-copper-rod-assortment-110-12-long-2-pieces-each/pid/mp-00000527">Bendable Copper...</a>
      <div class="price" data-qa-id="plp-item-price"></div></div>'''
    monkeypatch.setattr(session, "browser_fetch", lambda *_: {"ok": True, "content": html})
    hit = online.ADAPTER.search("6061 round tube")[0]
    assert (hit["kind"], hit["title"], hit["url"]) == ("product",
        '(1/16", 3/32") Bendable Copper Rod Assortment 110 - 12" Long (2 Pieces Each)',
        "https://www.onlinemetals.com/en/buy/copper/1-16-3-32-bendable-copper-rod-assortment-110-12-long-2-pieces-each/pid/mp-00000527")
    assert hit["thumbnail"] == ("https://www.onlinemetals.com/medias/300Wx300H-null?context="
        "bWFzdGVyfHJvb3R8NTkzNHxpbWFnZS9qcGVnfGFHUXlMMmhrWkM4eE1EQXhOekUxTlRNeE56YzVNQzh6TURCWGVETXdNRWhmYm5Wc2JBfDU5MDljZDk2ZWM3Y2IzMzdlZDFkZTk4Mjc5ODAzNjI1OGQxOGM0N2Q3YTU2NmRkMjJiM2YyZTJmZGY2NTQzN2U")
    assert hit["price"] is None
    mismatched = html.replace('alt="(1/16&quot;, 3/32&quot;) Bendable Copper Rod Assortment 110 - 12&quot; Long (2 Pieces Each)"',
                              'alt="aluminum-sheet-7075-t6-bare-1superZoom"')
    monkeypatch.setattr(session, "browser_fetch", lambda *_: {"ok": True, "content": mismatched})
    assert online.ADAPTER.search("6061 round tube")[0]["thumbnail"] is None


def test_structured_material_takes_precedence_and_logo_is_not_preview(monkeypatch):
    obj = {"@type": "Product", "name": "Aluminum Sheet", "url": "/sheet", "material": "5052-H32",
           "image": "/images/logo.png"}
    monkeypatch.setattr(session, "browser_fetch", lambda *_: {"ok": True, "content": _page(obj)})
    hit = online.ADAPTER.search("sheet")[0]
    assert hit["material"] == "5052-H32" and hit["thumbnail"] is None
    assert hit["price"] is None and hit["stock"] is None


def test_failures_and_genuine_empty_search(monkeypatch):
    page = {"text": "<html>unexpected page</html>"}
    monkeypatch.setattr(session, "browser_fetch", lambda *_: {"ok": True, "content": page["text"]})
    fail = online.ADAPTER.search("aluminum")[0]
    assert fail["site"] == online.SITE and fail["op"] == "search" and fail["reason"] == "parse_failed"
    page["text"] = "Pardon Our Interruption"
    assert online.ADAPTER.search("aluminum")[0]["reason"] == "blocked"
    page["text"] = "No results"
    assert online.ADAPTER.search("aluminum") == []


def test_stock_dimensions_and_document_acquisition(monkeypatch, tmp_path):
    url = online.BASE_URL + "/en/buy/aluminum/tube"
    page = '<table><tr><th>Alloy</th><td>6061-T6</td></tr></table><a href="/docs/tube-datasheet.pdf">Datasheet</a>'
    monkeypatch.setattr(session, "browser_fetch", lambda *_: {"ok": True, "content": page})
    pdf = tmp_path / "tube-datasheet.pdf"
    pdf.write_bytes(b"%PDF-1.4\nfixture")
    dest = str(pdf)
    monkeypatch.setattr(session, "browser_download", lambda *_args: {"ok": True, "dest": dest})
    result = online.ADAPTER.acquire({"url": url, "title": '6061-T6 Aluminum Round Tube 1" OD x 0.049" wall'})
    assert result["ok"] and result["artifact_kind"] == "document" and result["format"] == "pdf"
    assert result["artifact_path"] == dest and result["datasheet_path"] == dest
    assert result["params"]["profile"] == "round_tube"
    assert result["params"]["od_mm"] == pytest.approx(25.4)
    assert result["params"]["wall_mm"] == pytest.approx(1.2446)
