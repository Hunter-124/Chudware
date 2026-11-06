"""DigiKey official keyword search with a typed public-website fallback.

The captured Next.js result envelope contains product records and/or category
tiles. Category tiles remain navigable categories, not purchasable SKUs.
"""
from __future__ import annotations

import html
import json
import re
from typing import Any
from .. import session as _session
from ..challenge import classify_challenge, is_login_page
from . import abs_url, fail, is_login_wall, login_required, make_hit, quote_query, register, source_failure
from . import _vendor_api
from ._retail import find_datasheet_url

SITE = "digikey"
BASE_URL = "https://www.digikey.com"
SEARCH_URL_TMPL = BASE_URL + "/en/products/result?keywords={query}"
LICENSE_NOTE = (
    "DigiKey website parts data (price, stock, datasheet links) is copied "
    "with provenance (source URL + fetch timestamp) for the user's own "
    "BOM/sourcing decision, never redistributed in bulk and never "
    "LLM-generated. Datasheets remain the component manufacturer's own "
    "documents. ECAD links point to DigiKey partner services "
    "(SnapEDA/Ultra Librarian) whose own download terms apply. "
    "See contracts/license_debt.md."
)

_LOGIN_WALL_MARKERS = ('id="signInName"', 'class="login-form"')
# Live-verified 2026-07-07: DigiKey embeds its results data in a Next.js
# __NEXT_DATA__ JSON blob. Extract the raw JSON here, then read
# props.pageProps.envelope.data.topResults from it (see module docstring).
_NEXT_DATA_RE = re.compile(
    r"""<script\b(?=[^>]*\bid\s*=\s*["']__NEXT_DATA__["'])[^>]*>(?P<json>.*?)</script>""",
    re.IGNORECASE | re.DOTALL)
_BLOCK_MARKERS = ("access denied", "access to this page has been denied")
_SKU_RE = re.compile(r"""<span\b[^>]*class=["'][^"']*digikey-part-number[^"']*["'][^>]*>([^<]+)</span>""",
                     re.IGNORECASE)


def _strip_tags(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text).strip()


def _wall(content: str, op: str, url: str) -> dict | None:
    challenge = classify_challenge(content, url)
    if challenge == "captcha":
        return fail(SITE, op, "captcha", "DigiKey returned a CAPTCHA challenge.", url=url)
    if any(marker in content.lower() for marker in _BLOCK_MARKERS):
        return fail(SITE, op, "blocked", "DigiKey blocked the browser request.", url=url)
    if challenge == "2fa" or is_login_page(content, url) or is_login_wall(content, _LOGIN_WALL_MARKERS):
        return {**login_required(SITE, url), "site": SITE, "op": op, "url": url}
    return None

def _pick(d: dict, *keys: str) -> Any:
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return None


def _looks_like_product(entry: dict) -> bool:
    """True when a NEXT_DATA dict carries product-SKU fields (not a category tile)."""
    if _pick(entry, "categoryName", "categoryUrl") and not _pick(
            entry, "manufacturerProductNumber", "ManufacturerProductNumber",
            "digiKeyProductNumber", "DigiKeyProductNumber", "digiKeyPartNumber",
            "DigiKeyPartNumber", "productDescription", "ProductDescription"):
        return False
    return bool(_pick(
        entry,
        "manufacturerProductNumber", "ManufacturerProductNumber",
        "digiKeyProductNumber", "DigiKeyProductNumber",
        "digiKeyPartNumber", "DigiKeyPartNumber",
        "productDescription", "ProductDescription",
        "productUrl", "ProductUrl", "detailUrl", "DetailUrl",
    ))


def _product_hit(entry: dict) -> dict | None:
    if not isinstance(entry, dict) or not _looks_like_product(entry):
        return None
    hit = _vendor_api.digikey_hit(entry)
    if hit:
        hit["title"] = html.unescape(_strip_tags(hit["title"]))
    return hit


def _extract_products(data_root: Any) -> list[dict]:
    """Collect product-shaped dicts from the envelope (list or single product)."""
    if not isinstance(data_root, dict):
        return []

    out: list[dict] = []
    seen: set[str] = set()

    def _add(entry: Any) -> None:
        hit = _product_hit(entry) if isinstance(entry, dict) else None
        if not hit:
            return
        key = hit.get("url") or hit.get("mpn") or hit.get("digikey_pn") or hit["title"]
        if key in seen:
            return
        seen.add(str(key))
        out.append(hit)

    # Preferred explicit product collections on result pages.
    for key in ("products", "Products", "productResults", "ProductResults",
                "productsList", "ProductsList", "items", "Items", "topResults", "TopResults"):
        val = data_root.get(key)
        if isinstance(val, list):
            for entry in val:
                _add(entry)
        elif isinstance(val, dict):
            # Sometimes wrapped: {products: [...]} or {items: [...]}
            for sub in ("products", "Products", "items", "Items", "results", "Results"):
                if isinstance(val.get(sub), list):
                    for entry in val[sub]:
                        _add(entry)
            _add(val)

    # Single-product detail page payload.
    for key in ("product", "Product", "productDetails", "ProductDetails"):
        val = data_root.get(key)
        if isinstance(val, dict):
            _add(val)

    return out


def _extract_categories(data_root: Any) -> list[dict]:
    if not isinstance(data_root, dict):
        return []
    top_results = data_root.get("topResults") or data_root.get("TopResults") or []
    hits: list[dict] = []
    if not isinstance(top_results, list):
        return hits
    for entry in top_results:
        if not isinstance(entry, dict):
            continue
        # Skip product-shaped entries that may land in topResults on mixed pages.
        if _looks_like_product(entry) and not entry.get("categoryName"):
            continue
        rel = entry.get("categoryUrl", "")
        name = entry.get("categoryName")
        if not rel or not name:
            continue
        url = abs_url(BASE_URL, str(rel))
        if not url:
            continue
        hits.append(make_hit(
            SITE, title=html.unescape(str(name)).strip(), url=url, kind="category",
            thumbnail=_vendor_api._image(url, entry.get("imageUrl")),
            extra={"parent_category": entry.get("parentCategory"),
                   "product_count": entry.get("productCount"),
                   "category_id": entry.get("id")},
        ))
    return hits


class DigiKeyWebAdapter:
    key = SITE
    hosts = ("digikey.com",)
    result_kinds = ("product", "category")
    cad_acquisition = False
    login_required = False
    api = True

    def search(self, query: str) -> list[dict]:
        creds = _vendor_api.credentials("digikey_api")
        if creds:
            return _vendor_api.search(SITE, query, creds)
        url = SEARCH_URL_TMPL.format(query=quote_query(query))
        result = _session.browser_fetch(url, SITE)
        if not result.get("ok"):
            return [source_failure(SITE, "search", result, url=url)]
        content = result.get("content", "")
        wall = _wall(content, "search", result.get("final_url") or url)
        if wall:
            return [wall]
        match = _NEXT_DATA_RE.search(content)
        if not match:
            if re.search(r"\b(?:no results|no products found|0 results)\b", content, re.I):
                return []
            return [fail(SITE, "search", "parse_failed", "No recognizable DigiKey results on the page.", url=url)]
        try:
            data = json.loads(match.group("json"))
            data_root = data["props"]["pageProps"]["envelope"]["data"]
        except (ValueError, TypeError, KeyError):
            return [fail(SITE, "search", "parse_failed", "The DigiKey result data was invalid.", url=url)]
        if not isinstance(data_root, dict):
            return [fail(SITE, "search", "parse_failed", "The DigiKey result structure was invalid.", url=url)]
        products = _extract_products(data_root)
        if products:
            return products
        categories = _extract_categories(data_root)
        if categories:
            return categories
        keys = ("topResults", "TopResults", "products", "Products", "productResults", "ProductResults",
                "productsList", "ProductsList", "items", "Items")
        count = _vendor_api._integer(data_root.get("productCount"))
        collections = [data_root[key] for key in keys if key in data_root]
        if (count == 0 or (collections and all(value == [] for value in collections))) and not count:
            return []
        return [fail(SITE, "search", "parse_failed", "No usable DigiKey results could be parsed.", url=url)]

    def acquire(self, result: dict) -> dict:
        url = result.get("url", "")
        if not url:
            return fail(SITE, "acquire", "not_found", "The result has no product URL.")
        if result.get("kind") not in (None, "product"):
            return fail(SITE, "acquire", "unsupported", "Select a specific product first.", url=url)
        if result.get("datasheet_url") or result.get("datasheet"):
            return _vendor_api.acquire_datasheet(SITE, result, LICENSE_NOTE)
        page = _session.browser_fetch(url, SITE)
        if not page.get("ok"):
            return source_failure(SITE, "acquire", page, url=url)
        content = page.get("content", "")
        wall = _wall(content, "acquire", page.get("final_url") or url)
        if wall:
            return wall
        sku = result.get("sku") or result.get("digikey_pn")
        displayed_sku = _SKU_RE.search(content)
        if sku and displayed_sku and str(sku).strip().casefold() != html.unescape(
                displayed_sku.group(1)).strip().casefold():
            return fail(SITE, "acquire", "unsupported",
                        "The detail page identifies a different SKU; select the intended product.", url=url)
        datasheet = find_datasheet_url(content, page.get("final_url") or url)
        if not datasheet:
            return fail(SITE, "acquire", "unsupported", "No product datasheet was found.", url=url)
        return _vendor_api.acquire_datasheet(SITE, {**result, "datasheet_url": datasheet}, LICENSE_NOTE)


ADAPTER = register(DigiKeyWebAdapter())
