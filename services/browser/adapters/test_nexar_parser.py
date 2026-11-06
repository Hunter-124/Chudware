"""Nexar product/offer parsing without network access."""
from services.browser.adapters import _vendor_api, nexar


PART = {
    "mpn": "ABC123", "name": "ABC123", "manufacturer": {"name": "Maker"},
    "octopartUrl": "https://octopart.com/maker/abc123", "bestImage": {"url": "//img.example/abc.jpg"},
    "bestDatasheet": {"url": "https://maker.example/abc.pdf"},
    "sellers": [{"company": {"name": "Vendor A"}, "offers": [
        {"sku": "WRONG", "inventoryLevel": 500, "prices": [{"quantity": 1, "price": 1.0, "currency": "USD"}]},
        {"sku": "EXACT", "inventoryLevel": 25, "prices": []},
    ]}],
}


def test_exact_sku_is_not_replaced_by_another_offer():
    hit = _vendor_api._nexar_hit({"part": PART}, query="exact")
    assert hit["kind"] == "product" and hit["sku"] == "EXACT"
    assert hit["mpn"] == "ABC123" and hit["manufacturer"] == "Maker"
    assert hit["price"] is None and hit["currency"] is None and hit["stock"] == 25
    assert hit["thumbnail"] == "https://img.example/abc.jpg"
    assert hit["datasheet_url"] == "https://maker.example/abc.pdf"


def test_no_offers_do_not_invent_price_or_sku():
    hit = _vendor_api._nexar_hit({"part": {**PART, "sellers": [], "bestImage": None}})
    assert hit["sku"] is None and hit["price"] is None and hit["stock"] is None
    assert hit["thumbnail"] is None


def test_nexar_cannot_claim_cad_or_acquire_a_category():
    assert nexar.ADAPTER.cad_acquisition is False
    out = nexar.ADAPTER.acquire({"url": "https://octopart.com/categories", "kind": "category"})
    assert out["ok"] is False and out["reason"] == "unsupported"
