"""Offline Future Electronics autocomplete and detail fixtures."""
import json
import pytest

from services.browser.adapters import _retail, futureelectronics


# Trimmed 2026-11-04 response from the public autocomplete endpoint.
LIVE_AUTOCOMPLETE = '''{"suggestions":[
{"term":"STM32C011D6Y6TR","searchSpace":"upshiftedMPN"},
{"term":"STM32F101CBU6TR","searchSpace":"upshiftedMPN"}],"products":null}'''


def test_live_suggestions_are_search_pages_not_products(monkeypatch):
    requested = []
    monkeypatch.setattr(futureelectronics._session, "browser_fetch", lambda url, site: (
        requested.append((url, site)) or {"ok": True, "content": LIVE_AUTOCOMPLETE}))
    first, second = futureelectronics.ADAPTER.search("C++ & /#")
    assert requested == [("https://www.futureelectronics.com/search/autocomplete/SearchBox_GC?term=C%2B%2B+%26+%2F%23", "futureelectronics")]
    assert (first["title"], first["kind"], first["mpn"], first["sku"]) == (
        "STM32C011D6Y6TR", "page", "STM32C011D6Y6TR", None)
    assert first["url"] == "https://www.futureelectronics.com/search?text=STM32C011D6Y6TR"
    assert first["thumbnail"] is None and first["price"] is None
    assert second["kind"] == "page"
    assert futureelectronics.ADAPTER.hosts == ("futureelectronics.com",)
    assert set(futureelectronics.ADAPTER.result_kinds) == {"product", "page"}
    assert futureelectronics.ADAPTER.api is True and futureelectronics.ADAPTER.cad_acquisition is False


def test_product_entry_is_distinct_from_suggestion(monkeypatch):
    # Synthetic product entry; autocomplete suggestions above are captured live.
    content = json.dumps({"products": [{"name": "Microcontroller", "code": "F123",
        "url": "/p/microcontroller", "manufacturerPartNumber": "ABC-1", "manufacturer": "Acme",
        "price": {"value": 1.25, "currencyIso": "USD"}, "stock": {"stockLevel": 42},
        "image": {"url": "/images/logo.png"}, "images": [{"url": "/images/microcontroller.jpg"}]}],
        "suggestions": [{"term": "ABC-1", "searchSpace": "upshiftedMPN"}]})
    monkeypatch.setattr(futureelectronics._session, "browser_fetch", lambda *_: {"ok": True, "content": content})
    product, suggestion = futureelectronics.ADAPTER.search("ABC")
    assert (product["kind"], product["sku"], product["mpn"], product["manufacturer"]) == (
        "product", "F123", "ABC-1", "Acme")
    assert (product["price"], product["currency"], product["stock"]) == (1.25, "USD", 42)
    assert product["thumbnail"] == "https://www.futureelectronics.com/images/microcontroller.jpg"
    assert suggestion["kind"] == "page" and suggestion["sku"] is None


@pytest.mark.parametrize(("content", "reason"), [
    ("Access Denied", "blocked"), ("Verify you are human", "captcha"),
    ('<form id="login"></form>', "login_required"), ("<html>broken</html>", "parse_failed"),
    ('{"suggestions":{}}', "parse_failed"), ('{"other":[]}', "parse_failed"),
    ('{"products":[{"name":"unusable"}]}', "parse_failed"),
])
def test_failures_are_classified(monkeypatch, content, reason):
    monkeypatch.setattr(futureelectronics._session, "browser_fetch", lambda *_: {"ok": True, "content": content})
    failure = futureelectronics.ADAPTER.search("chip")[0]
    assert (failure["ok"], failure["site"], failure["op"], failure["reason"]) == (
        False, "futureelectronics", "search", reason)


def test_login_redirect_is_reported(monkeypatch):
    monkeypatch.setattr(futureelectronics._session, "browser_fetch", lambda *_: {
        "ok": True, "content": "<form></form>", "final_url": "https://www.futureelectronics.com/login"})
    assert futureelectronics.ADAPTER.search("chip")[0]["reason"] == "login_required"


def test_empty_response_and_search_page_acquisition(monkeypatch):
    monkeypatch.setattr(futureelectronics._session, "browser_fetch", lambda *_: {
        "ok": True, "content": '{"suggestions":[],"products":null}'})
    assert futureelectronics.ADAPTER.search("no part") == []
    monkeypatch.setattr(futureelectronics._session, "browser_fetch", lambda *_: pytest.fail("suggestion is not product"))
    result = futureelectronics.ADAPTER.acquire({"kind": "page", "url": "https://www.futureelectronics.com/search?text=ABC"})
    assert result["reason"] == "unsupported" and result["ask_user"]


def test_product_pdf_is_a_document(monkeypatch, tmp_path):
    monkeypatch.setattr(futureelectronics._session, "browser_fetch", lambda *_: {
        "ok": True, "content": '<a href="/files/data.pdf">Datasheet</a>'})
    monkeypatch.setattr(_retail, "download_path", lambda *args, **kwargs: (tmp_path / "data.pdf", None))
    def download(url, site, dest):
        dest.write_bytes(b"%PDF-1.7\n%%EOF")
        return {"ok": True, "dest": str(dest)}
    monkeypatch.setattr(futureelectronics._session, "browser_download", download)
    result = futureelectronics.ADAPTER.acquire({"kind": "product", "url": "https://www.futureelectronics.com/p/123"})
    assert (result["ok"], result["artifact_kind"], result["format"]) == (True, "document", "pdf")
