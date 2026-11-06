"""Offline contract tests for official electronics search APIs."""
from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from services.browser.adapters import _vendor_api as api
from services.browser.adapters import digikey_web, mouser, nexar
from services.net import safe_http


@pytest.fixture(autouse=True)
def isolated_credentials_and_tokens(monkeypatch, tmp_path):
    from services import paths
    monkeypatch.setattr(paths, "find_config", lambda: tmp_path / "config.json")
    for name in ("DIGIKEY_CLIENT_ID", "DIGIKEY_CLIENT_SECRET", "MOUSER_API_KEY",
                 "NEXAR_CLIENT_ID", "NEXAR_CLIENT_SECRET"):
        monkeypatch.delenv(name, raising=False)
    api._TOKEN_CACHE.clear()
    yield
    api._TOKEN_CACHE.clear()


def response(payload=None, *, status=200, headers=None, raw=None):
    data = raw if raw is not None else json.dumps(payload).encode()
    return safe_http.Response(status, headers or {}, "https://example.invalid", data)


def configure(monkeypatch, site):
    if site == "mouser":
        monkeypatch.setenv("MOUSER_API_KEY", "test/key+private")
    else:
        prefix = site.upper()
        monkeypatch.setenv(prefix + "_CLIENT_ID", "test-client-id")
        monkeypatch.setenv(prefix + "_CLIENT_SECRET", "test-client-secret")


def adapter(site):
    return {"digikey": digikey_web, "mouser": mouser, "nexar": nexar}[site].ADAPTER


DIGIKEY_PRODUCT = {
    "ManufacturerProductNumber": "LM317T", "Manufacturer": {"Name": "ST"},
    "Description": {"ProductDescription": "Adjustable regulator"},
    "ProductUrl": "https://www.digikey.com/en/products/detail/st/lm317t/123",
    "PhotoUrl": "https://mm.digikey.com/photo/lm317t.jpg",
    "DatasheetUrl": "https://docs.example/LM317.pdf", "QuantityAvailable": 210,
    "ProductVariations": [{"DigiKeyProductNumber": "497-LM317T-ND",
                           "QuantityAvailableforPackageType": 200,
                           "StandardPricing": [{"BreakQuantity": 10, "UnitPrice": 0.40},
                                               {"BreakQuantity": 1, "UnitPrice": 0.63}]}],
}
MOUSER_PRODUCT = {
    "ManufacturerPartNumber": "LM317T", "MouserPartNumber": "511-LM317T",
    "Manufacturer": "ST", "Description": "Adjustable regulator",
    "ProductDetailUrl": "https://www.mouser.com/ProductDetail/ST/LM317T",
    "ImagePath": "//images.mouser.com/lm317t.jpg", "DataSheetUrl": "https://docs.example/LM317.pdf",
    "Availability": "1,234 In Stock", "FactoryStock": "999999",
    "PriceBreaks": [{"Quantity": 100, "Price": "$0.42", "Currency": "USD"},
                    {"Quantity": 1, "Price": "$0.63", "Currency": "USD"}],
}
NEXAR_PART = {
    "name": "LM317T", "mpn": "LM317T", "shortDescription": "Adjustable regulator",
    "manufacturer": {"name": "ST"}, "octopartUrl": "https://octopart.com/st/lm317t",
    "bestImage": {"url": "https://images.example/lm317t.jpg"},
    "bestDatasheet": {"url": "https://docs.example/LM317.pdf"},
    "sellers": [{"company": {"name": "Distributor"}, "offers": [{
        "sku": "SELLER-123", "clickUrl": "https://seller.example/part/123", "inventoryLevel": 999,
        "prices": [{"quantity": 10, "price": 0.42, "currency": "USD"},
                   {"quantity": 1, "price": 0.63, "currency": "USD"}]}]}],
}


def api_result(site, rows):
    if site == "digikey":
        return {"Products": rows}
    if site == "mouser":
        return {"Errors": [], "SearchResults": {"Parts": rows}}
    return {"data": {"supSearch": {"results": [{"part": row} for row in rows]}}}


@pytest.mark.parametrize("site,product,sku,stock", [
    ("digikey", DIGIKEY_PRODUCT, "497-LM317T-ND", 200),
    ("mouser", MOUSER_PRODUCT, "511-LM317T", 1234),
    ("nexar", NEXAR_PART, "SELLER-123", 999),
])
def test_configured_keyword_search_maps_product_and_caches_tokens(monkeypatch, site, product, sku, stock):
    configure(monkeypatch, site)
    calls = []

    def request(url, **kwargs):
        calls.append((url, kwargs))
        assert kwargs["idle_timeout"] > 0 and kwargs["max_redirects"] == 0
        if url in (api.DIGIKEY_TOKEN, api.NEXAR_TOKEN):
            form = parse_qs(kwargs["body"].decode())
            assert form["grant_type"] == ["client_credentials"]
            if site == "nexar":
                assert form["scope"] == ["supply.domain"]
            return response({"access_token": "test-bearer-token", "expires_in": 3600})
        body = json.loads(kwargs["body"])
        if site == "digikey":
            assert body["Keywords"] == "LM317 & C++/A"
            assert kwargs["headers"]["X-DIGIKEY-Locale-Currency"] == "USD"
        elif site == "mouser":
            assert body["SearchByKeywordRequest"]["keyword"] == "LM317 & C++/A"
            assert parse_qs(urlsplit(url).query)["apiKey"] == ["test/key+private"]
        else:
            assert body["variables"]["q"] == "LM317 & C++/A"
            assert "supSearch(" in body["query"] and "bestImage" in body["query"]
        return response(api_result(site, [product]))

    monkeypatch.setattr(safe_http, "request", request)
    from services.browser import session
    monkeypatch.setattr(session, "browser_fetch", lambda *_: pytest.fail("configured search used browser"))
    hits = adapter(site).search("LM317 & C++/A")
    assert len(hits) == 1
    hit = hits[0]
    assert hit["site"] == site and hit["kind"] == "product"
    assert hit["mpn"] == "LM317T" and hit["sku"] == sku and hit["manufacturer"] == "ST"
    assert hit["price"] == 0.63 and hit["currency"] == "USD" and hit["stock"] == stock
    assert hit["thumbnail"].startswith("https://") and hit["thumbnail"].endswith("lm317t.jpg")
    assert hit["datasheet_url"] == "https://docs.example/LM317.pdf"
    assert hit["url"].startswith("https://")
    assert hit["price_breaks"][0]["quantity"] == 1
    adapter(site).search("LM317 & C++/A")
    assert len(calls) == (2 if site == "mouser" else 3)
    if site == "nexar":
        assert hit["supplier"] == "Distributor" and hit["offer_url"] == "https://seller.example/part/123"


def test_digikey_multiple_packaging_never_silently_substitutes_requested_sku():
    product = {**DIGIKEY_PRODUCT, "ProductVariations": [
        {"DigiKeyProductNumber": "CUT-TAPE", "StandardPricing": [{"BreakQuantity": 1, "UnitPrice": 1.0}]},
        {"DigiKeyProductNumber": "REEL", "StandardPricing": [{"BreakQuantity": 1000, "UnitPrice": 0.2}]},
    ]}
    unmatched = api.digikey_hit(product, query="LM317T", currency="USD")
    assert unmatched["sku"] is None and unmatched["price"] is None
    chosen = api.digikey_hit(product, query="REEL", currency="USD")
    assert chosen["sku"] == "REEL" and chosen["price"] == 0.2
    assert chosen["price_breaks"][0]["quantity"] == 1000
    product["DigiKeyProductNumber"] = "CUT-TAPE"
    chosen = api.digikey_hit(product, query="REEL", currency="USD")
    assert chosen["sku"] == "REEL" and chosen["price"] == 0.2


@pytest.mark.parametrize("site", ["digikey", "mouser", "nexar"])
@pytest.mark.parametrize("status,reason", [(401, "credentials_required"),
                                           (403, "credentials_required"), (429, "rate_limited")])
def test_http_auth_quota_and_no_secret_leak(monkeypatch, site, status, reason):
    configure(monkeypatch, site)
    def request(url, **kwargs):
        if url in (api.DIGIKEY_TOKEN, api.NEXAR_TOKEN):
            return response({"access_token": "test-bearer-token", "expires_in": 3600})
        return response({"error": "test-client-secret test/key+private test-bearer-token"},
                        status=status, headers={"Retry-After": "42"})
    monkeypatch.setattr(safe_http, "request", request)
    out = adapter(site).search("LM317T")[0]
    assert out["reason"] == reason and out["status"] == status
    if status == 429:
        assert out["retry_after_s"] == 42
    else:
        assert "Settings" in out["hint"]
    serialized = json.dumps(out)
    for secret in ("test-client-secret", "test/key+private", "test-bearer-token", "apiKey"):
        assert secret not in serialized


@pytest.mark.parametrize("site", ["digikey", "mouser", "nexar"])
def test_empty_bad_json_bad_shape_and_api_error(monkeypatch, site):
    configure(monkeypatch, site)
    outcomes = [response(api_result(site, [])), response(raw=b"<not json>"), response({"unexpected": []}),
                response({"errors": [{"message": "quota exhausted and test/key+private"}]})]
    def request(url, **kwargs):
        if url in (api.DIGIKEY_TOKEN, api.NEXAR_TOKEN):
            return response({"access_token": "token", "expires_in": 3600})
        return outcomes.pop(0)
    monkeypatch.setattr(safe_http, "request", request)
    assert adapter(site).search("x") == []
    assert adapter(site).search("x")[0]["reason"] == "parse_failed"
    assert adapter(site).search("x")[0]["reason"] == "parse_failed"
    result = adapter(site).search("x")[0]
    assert result["reason"] == "rate_limited" and "private" not in json.dumps(result)


def test_credentials_use_gui_config_precedence_and_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("DIGIKEY_CLIENT_ID", "environment-id")
    monkeypatch.setenv("DIGIKEY_CLIENT_SECRET", "environment-secret")
    (tmp_path / "config.json").write_text(json.dumps({"parts_providers": {
        "digikey": {"client_id": "configuration-id"}, "mouser": {"api_key": "configured-key"}}}))
    assert api.credentials("digikey_api") == {"client_id": "configuration-id", "client_secret": "environment-secret"}
    assert api.credentials("mouser") == {"api_key": "configured-key"}
    assert api.credentials("nexar") is None


def test_unconfigured_web_fallback_and_nexar_notice(monkeypatch):
    from services.browser import session
    calls = []
    monkeypatch.setattr(safe_http, "request", lambda *_args, **_kwargs: pytest.fail("unconfigured API request"))
    monkeypatch.setattr(session, "browser_fetch", lambda url, site: (
        calls.append((url, site)) or {"ok": True, "content": "<html>No results</html>"}))
    assert digikey_web.ADAPTER.search("a & b") == []
    assert mouser.ADAPTER.search("a & b") == []
    assert len(calls) == 2 and all("a+%26+b" in url for url, _ in calls)
    missing = nexar.ADAPTER.search("a")[0]
    assert missing["reason"] == "credentials_required" and "Settings" in missing["hint"]


@pytest.mark.parametrize("site", ["digikey", "mouser", "nexar"])
def test_datasheet_acquisition_is_document_and_preserves_previous_file_on_bad_content(monkeypatch, tmp_path, site):
    from services.browser import adapters
    monkeypatch.setattr(adapters.paths, "BROWSER_DOWNLOADS", tmp_path)
    hit = {"url": "https://catalog.example/part", "kind": "product", "sku": "sku-1",
           "datasheet_url": "https://docs.example/part.pdf"}
    def download(url, destination, **kwargs):
        Path(destination).write_bytes(b"%PDF-1.4\nfixture")
        return response({}, status=200)
    monkeypatch.setattr(safe_http, "download", download)
    result = adapter(site).acquire(hit)
    assert result["ok"] and result["artifact_kind"] == "document" and result["format"] == "pdf"
    assert Path(result["artifact_path"]).read_bytes().startswith(b"%PDF-")
    def bad_download(url, destination, **kwargs):
        Path(destination).write_bytes(b"<html>login</html>")
        return response({}, status=200)
    monkeypatch.setattr(safe_http, "download", bad_download)
    bad = adapter(site).acquire(hit)
    assert bad["reason"] == "parse_failed"
    assert Path(result["artifact_path"]).read_bytes().startswith(b"%PDF-")
    assert not list(tmp_path.rglob(".datasheet-*"))


@pytest.mark.parametrize("site", ["digikey", "nexar"])
@pytest.mark.parametrize("status,body,reason", [
    (400, {"error": "invalid_client"}, "credentials_required"),
    (401, {"error": "unauthorized"}, "credentials_required"),
    (429, {"error": "quota"}, "rate_limited"),
])
def test_oauth_errors_are_classified_without_leaking(monkeypatch, site, status, body, reason):
    configure(monkeypatch, site)
    monkeypatch.setattr(safe_http, "request", lambda *_, **__: response(
        body, status=status, headers={"retry-after": "7"}))
    out = adapter(site).search("LM317T")[0]
    assert out["reason"] == reason and out["status"] == status
    assert "test-client-secret" not in json.dumps(out)
    if status == 429:
        assert out["retry_after_s"] == 7


@pytest.mark.parametrize("value,expected", [("$1,234.56", 1234.56), ("€1.234,56", 1234.56),
                                             ("EUR 0,42", 0.42), ("$0.63", 0.63)])
def test_price_formats_keep_currency_values(value, expected):
    assert api._price(value) == expected


def test_digikey_exact_matches_are_not_lost_or_duplicated(monkeypatch):
    configure(monkeypatch, "digikey")
    def request(url, **kwargs):
        if url == api.DIGIKEY_TOKEN:
            return response({"access_token": "token", "expires_in": 3600})
        return response({"ExactMatches": [DIGIKEY_PRODUCT], "Products": [DIGIKEY_PRODUCT]})
    monkeypatch.setattr(safe_http, "request", request)
    hits = digikey_web.ADAPTER.search("LM317T")
    assert len(hits) == 1 and hits[0]["mpn"] == "LM317T"


def test_token_cache_replaced_after_credential_change(monkeypatch):
    configure(monkeypatch, "digikey")
    grants = []
    def request(url, **kwargs):
        if url == api.DIGIKEY_TOKEN:
            grants.append(parse_qs(kwargs["body"].decode())["client_secret"][0])
            return response({"access_token": "token", "expires_in": 3600})
        return response(api_result("digikey", []))
    monkeypatch.setattr(safe_http, "request", request)
    assert digikey_web.ADAPTER.search("x") == []
    assert digikey_web.ADAPTER.search("x") == []
    monkeypatch.setenv("DIGIKEY_CLIENT_SECRET", "rotated-secret")
    assert digikey_web.ADAPTER.search("x") == []
    assert grants == ["test-client-secret", "rotated-secret"]


def test_nexar_registered_and_routed():
    from services.browser import adapters
    from services.parts import taxonomy
    assert adapters.get_adapter("nexar").key == "nexar"
    assert {"nexar", "mouser", "digikey"} <= set(taxonomy.route("electronics")["sites"])


def test_unknown_thumbnails_and_prices_remain_unknown():
    part = {**MOUSER_PRODUCT, "ImagePath": "https://mouser.com/images/logo.svg",
            "PriceBreaks": [], "Availability": "Contact Mouser"}
    hit = api._mouser_hit(part)
    assert hit["thumbnail"] is None and hit["price"] is None and hit["stock"] is None


@pytest.mark.parametrize("site", ["digikey", "mouser", "nexar"])
def test_timeout_and_transport_exception_do_not_leak_credentials(monkeypatch, site):
    configure(monkeypatch, site)
    def request(*_args, **_kwargs):
        raise TimeoutError("test-client-secret test/key+private")
    monkeypatch.setattr(safe_http, "request", request)
    out = adapter(site).search("x")[0]
    assert out["reason"] == "timeout" and "private" not in json.dumps(out)


@pytest.mark.parametrize("message,expected", [
    ("API key is invalid", "credentials_required"),
    ("InvalidApiKey", "credentials_required"),
    ("RateLimitExceeded", "rate_limited"),
])
def test_embedded_api_errors_are_classified_and_redacted(message, expected):
    out = api._api_errors("mouser", api.MOUSER_SEARCH, [{"message": message + " private"}])
    assert out["reason"] == expected and "private" not in json.dumps(out)
