"""services/browser/adapters/digikey_web.py — DigiKey WEBSITE source adapter.

The browser fallback for DigiKey (digikey.com) parts search — DISTINCT from
the native sourcing API path in ``core/sourcing/``, which is the
preferred route when API credentials exist. This adapter browses the public
website under the user's own saved browser session instead: search returns
part cards with price/stock; `acquire` returns a structured price/stock
record plus datasheet + ECAD (symbol/footprint) links for ecad_find_part.

NOTE (live-verified 2026-07-07, fetch of
``/en/products/result?keywords=resistor``): DigiKey's results page is a
Next.js app that embeds its data in
``<script id="__NEXT_DATA__" type="application/json">``. The verified live
shape is ``props.pageProps.envelope.data.topResults`` — for a broad/ambiguous
keyword this is a list of ~22 CATEGORY tiles
(``categoryName``/``categoryUrl``/``parentCategory``/``productCount``/``id``),
i.e. a disambiguation page, NOT product cards. There are no manufacturer /
DigiKey part-number / price fields anywhere in the JSON for this page; the old
speculative CSS-class regexes (``product-title``, ``digikey-part-number``,
``price``) never matched the live DOM ("no hits parsed" bug). ``search`` now
parses the embedded JSON and returns those tiles as typed ``kind="category"``
results (honest: navigable catalog sections to drill into, not purchasable
SKUs) — mirroring the McMaster adapter's identical category-navigation case.

Narrow MPN queries (e.g. ``LM317T``) land on a product-results envelope that
carries actual SKUs under ``envelope.data.products`` (or a single product
detail under ``envelope.data.product``). Those are returned as
``kind="product"`` hits with title/url/mpn/price/datasheet when present.
``acquire`` remains best-effort detail-page shape.
"""
from __future__ import annotations

import html
import json
import re
from typing import Any
from urllib.parse import quote

from .. import session as _session
from . import is_login_wall, login_required, provenance_envelope, register

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

_LOGIN_WALL_MARKERS = ('Sign In', 'id="signInName"', 'class="login-form"')
# Live-verified 2026-07-07: DigiKey embeds its results data in a Next.js
# __NEXT_DATA__ JSON blob. Extract the raw JSON here, then read
# props.pageProps.envelope.data.topResults from it (see module docstring).
_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__"[^>]*>(?P<json>.*?)</script>', re.DOTALL)
# --- acquire() detail-page markers (best-effort, unchanged / not rewritten) ---
_DKPN_RE = re.compile(
    r'<span[^>]*class="[^"]*digikey-part-number[^"]*"[^>]*>(?P<dkpn>[^<]+)</span>', re.IGNORECASE)
_PRICE_RE = re.compile(r'<span[^>]*class="[^"]*price[^"]*"[^>]*>\$(?P<price>[0-9,.]+)</span>',
                       re.IGNORECASE)
_STOCK_RE = re.compile(
    r'<span[^>]*class="[^"]*stock[^"]*"[^>]*>(?P<stock>[0-9,]+)\s*(?:in stock)?</span>',
    re.IGNORECASE)
_DATASHEET_RE = re.compile(r'href="(?P<href>[^"]+\.pdf(?:\?[^"]*)?)"', re.IGNORECASE)
_ECAD_RE = re.compile(
    r'href="(?P<href>https?://(?:www\.)?(?:snapeda|ultralibrarian)\.com/[^"]+)"', re.IGNORECASE)


def _strip_tags(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text).strip()


def _abs_url(rel: str | None) -> str:
    if not rel:
        return ""
    rel = str(rel).strip()
    if not rel:
        return ""
    if rel.startswith("//"):
        return "https:" + rel
    if rel.startswith("/"):
        return BASE_URL + rel
    return rel


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
    """Map a product-shaped NEXT_DATA entry to a search hit, or None if unusable."""
    if not isinstance(entry, dict) or not _looks_like_product(entry):
        return None

    mpn = _pick(entry, "manufacturerProductNumber", "ManufacturerProductNumber",
                "manufacturerPartNumber", "ManufacturerPartNumber")
    dkpn = _pick(entry, "digiKeyProductNumber", "DigiKeyProductNumber",
                 "digiKeyPartNumber", "DigiKeyPartNumber")
    desc = _pick(entry, "productDescription", "ProductDescription",
                 "detailedDescription", "DetailedDescription", "description", "Description")
    manufacturer = _pick(entry, "manufacturerName", "ManufacturerName")
    mfr_obj = _pick(entry, "manufacturer", "Manufacturer")
    if not manufacturer and isinstance(mfr_obj, dict):
        manufacturer = _pick(mfr_obj, "name", "Name")
    elif not manufacturer and isinstance(mfr_obj, str):
        manufacturer = mfr_obj

    title_bits = [str(x).strip() for x in (manufacturer, mpn or dkpn, desc) if x]
    # Prefer "MFR MPN — description" when we have pieces; fall back sensibly.
    if manufacturer and (mpn or dkpn) and desc:
        title = f"{manufacturer} {mpn or dkpn} — {_strip_tags(str(desc))}"
    elif title_bits:
        title = " ".join(title_bits[:2]) if len(title_bits) > 1 else title_bits[0]
        title = _strip_tags(str(title))
    else:
        return None

    url = _abs_url(str(_pick(entry, "productUrl", "ProductUrl", "detailUrl", "DetailUrl",
                             "url", "Url") or ""))
    if not url and dkpn:
        # Detail-page path when only the DKPN is known (common on result rows).
        url = f"{BASE_URL}/en/products/detail/{quote(str(dkpn), safe='')}"
    if not url:
        return None

    hit: dict[str, Any] = {
        "title": html.unescape(title).strip(),
        "url": url,
        "site": SITE,
        "kind": "product",
    }
    if mpn:
        hit["mpn"] = str(mpn).strip()
    if dkpn:
        hit["digikey_pn"] = str(dkpn).strip()
    if manufacturer:
        hit["manufacturer"] = str(manufacturer).strip()

    price = _pick(entry, "unitPrice", "UnitPrice", "price", "Price")
    if price is not None:
        try:
            hit["price"] = f"${float(str(price).replace('$', '').replace(',', '')):.4g}"
        except (TypeError, ValueError):
            ps = str(price).strip()
            if ps:
                hit["price"] = ps if ps.startswith("$") else f"${ps}"

    stock = _pick(entry, "quantityAvailable", "QuantityAvailable", "stock", "Stock")
    if stock is not None:
        hit["stock"] = str(stock).strip()

    datasheet = _pick(entry, "datasheetUrl", "DatasheetUrl", "dataSheetUrl", "DataSheetUrl")
    if datasheet:
        hit["datasheet"] = _abs_url(str(datasheet))

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
                "productsList", "ProductsList", "items", "Items"):
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
        hits.append({
            "title": html.unescape(name).strip(),
            "url": BASE_URL + rel if str(rel).startswith("/") else rel,
            "site": SITE,
            "kind": "category",   # navigable catalog section, not a purchasable SKU
            "parent_category": entry.get("parentCategory"),
            "product_count": entry.get("productCount"),
            "category_id": entry.get("id"),
        })
    return hits


class DigiKeyWebAdapter:
    key = SITE

    def search(self, query: str) -> list[dict]:
        url = SEARCH_URL_TMPL.format(query=query.replace(" ", "+"))
        result = _session.browser_fetch(url, SITE)
        if not result.get("ok"):
            return [result]
        content = result.get("content", "")
        if is_login_wall(content, _LOGIN_WALL_MARKERS):
            return [login_required(SITE, url)]

        # Live-verified shape: results live in the embedded __NEXT_DATA__ JSON at
        # props.pageProps.envelope.data. Parse products first (narrow MPN), then
        # fall back to category tiles (broad keyword disambiguation). No
        # speculative CSS-class regex. Empty is honest.
        m = _NEXT_DATA_RE.search(content)
        if not m:
            return []
        try:
            data = json.loads(m.group("json"))
        except (ValueError, TypeError):
            return []
        data_root = (
            data.get("props", {})
            .get("pageProps", {})
            .get("envelope", {})
            .get("data", {})
        )
        products = _extract_products(data_root)
        if products:
            return products
        return _extract_categories(data_root)

    def acquire(self, result: dict) -> dict:
        url = result.get("url", "")
        if not url:
            return {"ok": False, "reason": "bad_result", "hint": "result has no 'url'"}

        page = _session.browser_fetch(url, SITE)
        if not page.get("ok"):
            return page
        content = page.get("content", "")
        if is_login_wall(content, _LOGIN_WALL_MARKERS):
            return login_required(SITE, url)

        dkpn_match = _DKPN_RE.search(content)
        price_match = _PRICE_RE.search(content)
        stock_match = _STOCK_RE.search(content)

        datasheet_url = None
        ds_match = _DATASHEET_RE.search(content)
        if ds_match:
            datasheet_url = ds_match.group("href")
            if datasheet_url.startswith("/"):
                datasheet_url = BASE_URL + datasheet_url
        # Prefer datasheet already parsed on the search hit when the detail page
        # does not expose a PDF link in static HTML.
        if not datasheet_url:
            datasheet_url = result.get("datasheet")
        ecad_match = _ECAD_RE.search(content)

        return provenance_envelope(
            source_url=url,
            session_profile_used=page.get("session_profile_used", SITE),
            license_note=LICENSE_NOTE,
            artifact_path=None,
            title=result.get("title", ""),
            digikey_pn=dkpn_match.group("dkpn").strip() if dkpn_match else result.get("digikey_pn"),
            mpn=result.get("mpn"),
            price=("$" + price_match.group("price")) if price_match else result.get("price"),
            stock=stock_match.group("stock") if stock_match else result.get("stock"),
            datasheet_url=datasheet_url,
            ecad_url=ecad_match.group("href") if ecad_match else None,
        )


ADAPTER = register(DigiKeyWebAdapter())
