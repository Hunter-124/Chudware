"""Official electronics keyword searches shared by the distributor adapters.

Credentials use the same config-first resolver as the Settings pane. API errors
are deliberately reduced to fixed hints: neither request URLs containing keys nor
server-provided error text may escape into logs or result notices.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
import threading
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlencode

from services.net import safe_http

from . import abs_url, download_path, fail, make_hit, provenance_envelope, quote_query

DIGIKEY_BASE = "https://www.digikey.com"
DIGIKEY_TOKEN = "https://api.digikey.com/v1/oauth2/token"
DIGIKEY_SEARCH = "https://api.digikey.com/products/v4/search/keyword"
MOUSER_BASE = "https://www.mouser.com"
MOUSER_SEARCH = "https://api.mouser.com/api/v2/search/keyword"
NEXAR_BASE = "https://octopart.com"
NEXAR_TOKEN = "https://identity.nexar.com/connect/token"
NEXAR_SEARCH = "https://api.nexar.com/graphql"

_NEXAR_QUERY = """query PartsKeyword($q: String!) {
  supSearch(q: $q, limit: 10) {
    results { part {
      name mpn shortDescription octopartUrl manufacturer { name }
      bestDatasheet { url } bestImage { url }
      sellers(includeBrokers: false) { company { name }
        offers { sku clickUrl inventoryLevel prices { quantity price currency } }
      }
    } }
  }
}"""

# Only the digest of the credentials is retained as the cache key. Serialize token
# refreshes to avoid consuming a token grant for each concurrent library search.
_TOKEN_LOCK = threading.Lock()
_TOKEN_CACHE: dict[str, tuple[bytes, str, float]] = {}
_TIMEOUT_S = 8.0


def credentials(provider: str) -> dict[str, str] | None:
    """Resolve exactly the same settings slots and precedence as the GUI."""
    from services.agent.request_context import in_cell
    if in_cell():
        # The Settings credential store is not tenant-isolated.
        return None
    from services.parts.gui_rpc import _API_PROVIDERS, _resolve_creds
    spec = _API_PROVIDERS[provider]
    values = _resolve_creds(spec.get("config_key", provider), spec["env"])
    return values if all(values.get(field) for field in spec["fields"]) else None


def _retry_after(headers: dict) -> float | None:
    value = next((v for k, v in headers.items() if k.lower() == "retry-after"), None)
    if value is None:
        return None
    try:
        delay = float(value)
        return max(0.0, delay) if math.isfinite(delay) else None
    except (TypeError, ValueError):
        try:
            then = parsedate_to_datetime(str(value))
            if then.tzinfo is None:
                then = then.replace(tzinfo=timezone.utc)
            return max(0.0, (then - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None


def _status_failure(site: str, op: str, url: str, response) -> dict:
    status = response.status_code
    if status == 429:
        return fail(site, op, "rate_limited", "The provider's API quota or rate limit was reached.",
                    url=url, status=status, retry_after_s=_retry_after(response.headers))
    if status in (401, 403):
        if op == "acquire":
            reason = "login_required" if status == 401 else "blocked"
            return fail(site, op, reason, "The datasheet provider denied access.", url=url, status=status)
        return fail(site, op, "credentials_required", "Check the API credentials in Settings.",
                    url=url, status=status)
    if status == 400 and op == "search":
        try:
            data = response.json()
            classified = _api_errors(site, url, data)
            if classified["reason"] != "parse_failed":
                return {**classified, "status": status}
        except (ValueError, UnicodeError, TypeError):
            pass
    if status == 404:
        return fail(site, op, "not_found", "The requested resource was not found.", url=url, status=status)
    return fail(site, op, "transport_failed", "The provider request failed.", url=url, status=status)


def _request_json(site: str, url: str, *, body: bytes, headers: dict, request_url: str | None = None):
    try:
        # Never follow a redirect with credentials, even to a different path on
        # the same host. request_url is used only for Mouser's required query key.
        response = safe_http.request(request_url or url, method="POST", body=body, headers=headers,
                                     idle_timeout=_TIMEOUT_S, max_bytes=8 * 1024 * 1024,
                                     max_redirects=0, schemes=("https",))
    except TimeoutError:
        return None, fail(site, "search", "timeout", "The provider request timed out.", url=url)
    except safe_http.OutboundPolicyError:
        return None, fail(site, "search", "policy_refused", "The provider request was refused by network policy.", url=url)
    except (OSError, RuntimeError, ValueError):
        return None, fail(site, "search", "transport_failed", "The provider request failed.", url=url)
    if not 200 <= response.status_code < 300:
        return None, _status_failure(site, "search", url, response)
    try:
        payload = response.json()
    except (ValueError, UnicodeError, TypeError):
        return None, fail(site, "search", "parse_failed", "The provider returned invalid JSON.", url=url)
    if not isinstance(payload, dict):
        return None, fail(site, "search", "parse_failed", "The provider returned an unexpected response.", url=url)
    return payload, None


def _token(site: str, creds: dict[str, str]):
    token_url = DIGIKEY_TOKEN if site == "digikey" else NEXAR_TOKEN
    digest = hashlib.sha256((creds["client_id"] + "\0" + creds["client_secret"]).encode()).digest()
    with _TOKEN_LOCK:
        cached = _TOKEN_CACHE.get(site)
        if cached and cached[0] == digest and cached[2] > time.monotonic():
            return cached[1], None
        form = {"grant_type": "client_credentials", "client_id": creds["client_id"],
                "client_secret": creds["client_secret"]}
        if site == "nexar":
            form["scope"] = "supply.domain"
        data, error = _request_json(site, token_url, body=urlencode(form).encode(),
                                    headers={"Content-Type": "application/x-www-form-urlencoded",
                                             "Accept": "application/json"})
        if error:
            return None, error
        token = data.get("access_token")
        if not isinstance(token, str) or not token.strip():
            return None, fail(site, "search", "parse_failed", "The authorization response had no access token.", url=token_url)
        try:
            lifetime = float(data.get("expires_in", 0))
        except (TypeError, ValueError):
            lifetime = 0
        if math.isfinite(lifetime) and lifetime > 30:
            _TOKEN_CACHE[site] = (digest, token, time.monotonic() + lifetime - min(60.0, lifetime / 10))
        else:
            _TOKEN_CACHE.pop(site, None)
        return token, None


def _forget_token(site: str):
    with _TOKEN_LOCK:
        _TOKEN_CACHE.pop(site, None)


def _pick(data: dict, *keys):
    return next((data[k] for k in keys if k in data and data[k] is not None), None)


def _text(value) -> str | None:
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        return None
    text = str(value).strip()
    return text or None


def _price(value) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    text = str(value).strip()
    text = re.sub(r"^(?:[A-Za-z]{3}\s*|[$£€¥]\s*)", "", text)
    if "," in text and "." in text:
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif "," in text:
        if re.fullmatch(r"\d{1,3}(?:,\d{3})+", text):
            text = text.replace(",", "")
        else:
            text = text.replace(",", ".")
    try:
        number = float(text)
    except ValueError:
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _integer(value) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    text = str(value).strip().replace(",", "")
    return int(text) if text.isdecimal() else None


def _stock(value) -> int | None:
    direct = _integer(value)
    if direct is not None:
        return direct
    match = re.match(r"^\s*([\d,]+)\s+in\s+stock\b", str(value or ""), re.I)
    return _integer(match.group(1)) if match else None


def _image(base: str, value) -> str | None:
    if isinstance(value, dict):
        value = _pick(value, "url", "Url")
    if not isinstance(value, str) or re.search(r"(?:^|[/_.-])(?:logo|sprite|favicon|placeholder|icon)(?:[/_.?-]|$)", value, re.I):
        return None
    return abs_url(base, value)


def _tiers(rows, quantity_keys, price_keys, currency_keys=()):
    tiers = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        qty = _integer(_pick(row, *quantity_keys))
        price = _price(_pick(row, *price_keys))
        if qty and price is not None:
            tiers.append({"quantity": qty, "unit_price": price,
                          "currency": _text(_pick(row, *currency_keys)) if currency_keys else None})
    return sorted(tiers, key=lambda tier: tier["quantity"])


def digikey_hit(product: dict, *, query: str = "", currency: str | None = None) -> dict | None:
    if not isinstance(product, dict):
        return None
    url = abs_url(DIGIKEY_BASE, _text(_pick(product, "ProductUrl", "productUrl", "DetailUrl", "detailUrl", "url", "Url")))
    if not url:
        return None
    mpn = _text(_pick(product, "ManufacturerProductNumber", "manufacturerProductNumber", "manufacturerPartNumber", "ManufacturerPartNumber"))
    sku = _text(_pick(product, "DigiKeyProductNumber", "digiKeyProductNumber", "DigiKeyPartNumber", "digiKeyPartNumber"))
    manufacturer = _pick(product, "ManufacturerName", "manufacturerName", "Manufacturer", "manufacturer")
    if isinstance(manufacturer, dict):
        manufacturer = _pick(manufacturer, "Name", "name")
    manufacturer = _text(manufacturer)
    description = _pick(product, "Description", "description", "ProductDescription", "productDescription", "DetailedDescription", "detailedDescription")
    if isinstance(description, dict):
        description = _pick(description, "ProductDescription", "productDescription", "DetailedDescription", "detailedDescription")
    description = _text(description)
    title = " ".join(value for value in (manufacturer, mpn or sku) if value)
    if description:
        title = f"{title} — {description}" if title else description
    if not title:
        return None
    variations = _pick(product, "ProductVariations", "productVariations")
    variations = [v for v in variations if isinstance(v, dict)] if isinstance(variations, list) else []
    # A multi-packaging product is not silently assigned a different SKU.
    selected = None
    for requested in (query.strip(), sku or ""):
        selected = next((v for v in variations if requested and requested.casefold() == str(
            _pick(v, "DigiKeyProductNumber", "digiKeyProductNumber") or "").casefold()), None)
        if selected is not None:
            break
    if selected is None and len(variations) == 1:
        selected = variations[0]
    selected = selected or {}
    sku = _text(_pick(selected, "DigiKeyProductNumber", "digiKeyProductNumber")) or sku
    tiers = _tiers(_pick(selected, "StandardPricing", "standardPricing"),
                   ("BreakQuantity", "breakQuantity", "Quantity", "quantity"),
                   ("UnitPrice", "unitPrice", "Price", "price"))
    price = tiers[0]["unit_price"] if tiers else None
    if price is None and (not sku or len(variations) <= 1):
        price = _price(_pick(product, "UnitPrice", "unitPrice", "price", "Price"))
    stock = _integer(_pick(selected, "QuantityAvailableforPackageType", "quantityAvailableforPackageType"))
    if stock is None and (not sku or len(variations) <= 1):
        stock = _stock(_pick(product, "QuantityAvailable", "quantityAvailable", "stock", "Stock"))
    currency = _text(_pick(product, "Currency", "currency")) or currency
    if not currency and str(_pick(product, "UnitPrice", "unitPrice", "price", "Price") or "").strip().startswith("$"):
        currency = "USD"
    for tier in tiers:
        tier["currency"] = currency
    datasheet = abs_url(DIGIKEY_BASE, _text(_pick(product, "DatasheetUrl", "datasheetUrl", "dataSheetUrl", "DataSheetUrl")))
    extra = {"datasheet_url": datasheet, "datasheet": datasheet, "digikey_pn": sku}
    if tiers:
        extra["price_breaks"] = tiers
    return make_hit("digikey", title=title, url=url, kind="product", sku=sku, mpn=mpn,
                    manufacturer=manufacturer, price=price, currency=currency if price is not None else None,
                    stock=stock, thumbnail=_image(url, _pick(product, "PhotoUrl", "photoUrl", "imageUrl", "ImageUrl")),
                    extra=extra)


def _mouser_hit(part: dict) -> dict | None:
    if not isinstance(part, dict):
        return None
    url = abs_url(MOUSER_BASE, _text(_pick(part, "ProductDetailUrl", "productDetailUrl")))
    if not url:
        return None
    mpn = _text(_pick(part, "ManufacturerPartNumber", "manufacturerPartNumber"))
    sku = _text(_pick(part, "MouserPartNumber", "mouserPartNumber"))
    manufacturer = _text(_pick(part, "Manufacturer", "manufacturer"))
    desc = _text(_pick(part, "Description", "description"))
    title = " ".join(x for x in (manufacturer, mpn or sku) if x)
    if desc:
        title = f"{title} — {desc}" if title else desc
    if not title:
        return None
    tiers = _tiers(_pick(part, "PriceBreaks", "priceBreaks"), ("Quantity", "quantity"),
                   ("Price", "price"), ("Currency", "currency"))
    datasheet = abs_url(MOUSER_BASE, _text(_pick(part, "DataSheetUrl", "dataSheetUrl")))
    return make_hit("mouser", title=title, url=url, sku=sku, mpn=mpn, manufacturer=manufacturer,
                    price=tiers[0]["unit_price"] if tiers else None,
                    currency=tiers[0]["currency"] if tiers else None,
                    stock=_stock(_pick(part, "Availability", "availability")),
                    thumbnail=_image(url, _pick(part, "ImagePath", "imagePath")),
                    extra={"datasheet_url": datasheet, "datasheet": datasheet, "price_breaks": tiers})


def _nexar_hit(entry: dict, *, query: str = "") -> dict | None:
    part = entry.get("part") if isinstance(entry, dict) else None
    if not isinstance(part, dict):
        return None
    url = abs_url(NEXAR_BASE, _text(part.get("octopartUrl")))
    if not url:
        return None
    mpn = _text(part.get("mpn"))
    manufacturer = part.get("manufacturer") or {}
    manufacturer = _text(manufacturer.get("name")) if isinstance(manufacturer, dict) else _text(manufacturer)
    description = _text(part.get("shortDescription")) or _text(part.get("name"))
    title = " ".join(x for x in (manufacturer, mpn) if x)
    if description and description not in (title, mpn):
        title = f"{title} — {description}" if title else description
    if not title:
        return None
    choices = []
    exact_choices = []
    for seller in part.get("sellers") or []:
        if not isinstance(seller, dict):
            continue
        company = seller.get("company") or {}
        supplier = _text(company.get("name")) if isinstance(company, dict) else None
        for offer in seller.get("offers") or []:
            if not isinstance(offer, dict):
                continue
            tiers = _tiers(offer.get("prices"), ("quantity",), ("price",), ("currency",))
            choice = (tiers[0]["quantity"] if tiers else float("inf"),
                      0 if tiers and tiers[0]["currency"] == "USD" else 1,
                      offer, tiers, supplier)
            if tiers:
                choices.append(choice)
            if query.strip() and query.strip().casefold() == str(offer.get("sku") or "").casefold():
                exact_choices.append(choice)
    # An exact SKU match takes precedence, even when it has no price. Otherwise
    # show a disclosed priced offer; never compare prices in different currencies.
    candidates = exact_choices or choices
    choice = min(candidates, key=lambda x: (x[0], x[1])) if candidates else None
    offer, tiers, supplier = (choice[2], choice[3], choice[4]) if choice else ({}, [], None)
    datasheet = abs_url(NEXAR_BASE, _text((part.get("bestDatasheet") or {}).get("url"))) if isinstance(part.get("bestDatasheet") or {}, dict) else None
    return make_hit("nexar", title=title, url=url, sku=_text(offer.get("sku")), mpn=mpn,
                    manufacturer=manufacturer, price=tiers[0]["unit_price"] if tiers else None,
                    currency=tiers[0]["currency"] if tiers else None, stock=_integer(offer.get("inventoryLevel")),
                    thumbnail=_image(url, part.get("bestImage")),
                    extra={"datasheet_url": datasheet, "datasheet": datasheet, "price_breaks": tiers,
                           "supplier": supplier, "offer_url": abs_url(url, _text(offer.get("clickUrl")))})


def _api_errors(site: str, url: str, errors) -> dict:
    # Classify error text internally, never relay it; APIs may echo credentials.
    text = json.dumps(errors).lower()
    if any(word in text for word in ("rate limit", "rate_limit", "ratelimit", "request limit",
                                     "limit_exceeded", "quota", "too many requests")):
        return fail(site, "search", "rate_limited", "The provider's API quota or rate limit was reached.", url=url)
    key_error = ("api key" in text or "apikey" in text) and any(
        word in text for word in ("invalid", "not valid", "missing", "expired", "incorrect"))
    if key_error or any(word in text for word in ("unauthorized", "unauthorised", "forbidden",
                                                  "invalid token", "invalid_client", "invalid client",
                                                  "invalid_grant", "access_denied", "authentication")):
        return fail(site, "search", "credentials_required", "Check the API credentials in Settings.", url=url)
    return fail(site, "search", "parse_failed", "The provider returned an API error.", url=url)


def search(site: str, query: str, creds: dict[str, str]) -> list[dict]:
    if site in ("digikey", "nexar"):
        token, error = _token(site, creds)
        if error:
            return [error]
        headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json", "Accept": "application/json"}
        if site == "digikey":
            url = DIGIKEY_SEARCH
            headers.update({"X-DIGIKEY-Client-Id": creds["client_id"], "X-DIGIKEY-Locale-Site": "US",
                            "X-DIGIKEY-Locale-Language": "en", "X-DIGIKEY-Locale-Currency": "USD"})
            payload = {"Keywords": query, "RecordCount": 50, "RecordStartPosition": 0}
        else:
            url = NEXAR_SEARCH
            payload = {"query": _NEXAR_QUERY, "variables": {"q": query}}
        data, error = _request_json(site, url, body=json.dumps(payload).encode(), headers=headers)
        if error:
            if error.get("status") in (401, 403):
                _forget_token(site)
            return [error]
    else:
        url = MOUSER_SEARCH
        payload = {"SearchByKeywordRequest": {"keyword": query, "records": 50,
                                               "startingRecord": 0, "searchOptions": "None"}}
        data, error = _request_json(site, url, body=json.dumps(payload).encode(),
                                    headers={"Content-Type": "application/json", "Accept": "application/json"},
                                    request_url=url + "?apiKey=" + quote_query(creds["api_key"]))
        if error:
            return [error]
    if data.get("errors") or data.get("Errors"):
        return [_api_errors(site, url, data.get("errors") or data.get("Errors"))]
    if site == "digikey":
        rows = _pick(data, "Products", "products")
        exact = _pick(data, "ExactMatches", "exactMatches")
        if isinstance(exact, list):
            rows = exact + (rows if isinstance(rows, list) else [])
        if rows is None and _integer(_pick(data, "ProductsCount", "productsCount")) == 0:
            rows = []
        mapper = lambda row: digikey_hit(row, query=query, currency="USD")
    elif site == "mouser":
        root = _pick(data, "SearchResults", "searchResults")
        rows = _pick(root, "Parts", "parts") if isinstance(root, dict) else None
        if rows is None and isinstance(root, dict) and _integer(_pick(root, "NumberOfResult", "numberOfResult")) == 0:
            rows = []
        mapper = _mouser_hit
    else:
        root = data.get("data")
        root = root.get("supSearch") if isinstance(root, dict) else None
        rows = root.get("results") if isinstance(root, dict) else None
        mapper = lambda row: _nexar_hit(row, query=query)
    if not isinstance(rows, list):
        return [fail(site, "search", "parse_failed", "The provider returned an unexpected result structure.", url=url)]
    hits = [hit for row in rows if (hit := mapper(row))]
    if site == "digikey":
        unique = {}
        for hit in hits:
            unique.setdefault((hit["url"], hit["sku"]), hit)
        hits = list(unique.values())
    if rows and not hits:
        return [fail(site, "search", "parse_failed", "The provider returned no usable product records.", url=url)]
    return hits


def acquire_datasheet(site: str, result: dict, license_note: str) -> dict:
    """Download a known product datasheet, never presenting it as CAD geometry."""
    source = _text(result.get("url"))
    url = abs_url(source or "", _text(result.get("datasheet_url") or result.get("datasheet")))
    if result.get("kind") not in (None, "product"):
        return fail(site, "acquire", "unsupported", "Select a specific product before acquiring its datasheet.", url=source)
    if not url:
        return fail(site, "acquire", "unsupported", "No downloadable datasheet was provided for this product.", url=source)
    dest, refusal = download_path(site, url, default="datasheet.pdf")
    if refusal:
        return {**fail(site, "acquire", "policy_refused", "The datasheet filename is unsafe.", url=url),
                "detail": "unsafe_filename"}
    staging = None
    try:
        with tempfile.NamedTemporaryFile(dir=dest.parent, prefix=".datasheet-", suffix=".pdf", delete=False) as temp:
            staging = Path(temp.name)
        response = safe_http.download(url, staging, idle_timeout=_TIMEOUT_S, max_bytes=32 * 1024 * 1024,
                                      max_redirects=6, schemes=("https",))
        if not 200 <= response.status_code < 300:
            return _status_failure(site, "acquire", url, response)
        with staging.open("rb") as stream:
            if stream.read(5) != b"%PDF-":
                return fail(site, "acquire", "parse_failed", "The downloaded datasheet is not a PDF.", url=url)
        os.replace(staging, dest)
        return provenance_envelope(source_url=source or url, session_profile_used="public_api",
                                   license_note=license_note, artifact_path=str(dest), artifact_kind="document",
                                   format="pdf", title=result.get("title"), sku=result.get("sku"),
                                   mpn=result.get("mpn"), datasheet_url=url, datasheet_path=str(dest))
    except TimeoutError:
        return fail(site, "acquire", "timeout", "The datasheet download timed out.", url=url)
    except safe_http.OutboundPolicyError:
        return fail(site, "acquire", "policy_refused", "The datasheet URL was refused by network policy.", url=url)
    except (OSError, RuntimeError, ValueError):
        return fail(site, "acquire", "transport_failed", "The datasheet download failed.", url=url)
    finally:
        if staging is not None:
            staging.unlink(missing_ok=True)
