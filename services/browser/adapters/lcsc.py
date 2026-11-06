"""LCSC electronic-component search and manufacturer datasheet acquisition.

The search results identify LCSC SKUs; this adapter downloads datasheets, not CAD.
"""
from __future__ import annotations

import html
import re

from .. import challenge, session as _session
from ..cad_validate import classify_artifact
from . import (download_path, fail, is_login_wall, login_required, make_hit,
               provenance_envelope, quote_query, register, source_failure)
from ._retail import RetailAdapter, _challenge, _price, _stock, card_image, find_datasheet_url, visible_text

SITE = "lcsc"
BASE_URL = "https://www.lcsc.com"
SEARCH_URL_TMPL = BASE_URL + "/search?q={query}"
LICENSE_NOTE = (
    "LCSC parts data (price, stock) is copied with provenance (source URL + "
    "fetch timestamp) for the user's own BOM/sourcing decision, never "
    "redistributed in bulk and never LLM-generated. Datasheets remain the "
    "component manufacturer's documents, fetched for the user's own "
    "engineering reference. See contracts/license_debt.md."
)

_LOGIN_WALL_MARKERS = ('id="login-email"', 'class="login-dialog"')
_CHALLENGE_MARKERS = ("Access Denied", "Pardon Our Interruption", "Just a moment")
# The product cell of the rendered result table identifies the LCSC SKU.
_RESULT_RE = re.compile(
    r'''<\w+\b(?=[^>]*\baria-label=["'](?P<title>[^"']+?)\s+detail page["'])'''
    r'''(?=[^>]*\btitle=["'](?P<lcsc>C\d+)["'])[^>]*>''', re.IGNORECASE)
_ROW_RE = re.compile(r'<tr\b[^>]*>.*?</tr>', re.IGNORECASE | re.DOTALL)
_LCSC_RE = re.compile(r'<span[^>]*class="[^"]*lcsc-number[^"]*"[^>]*>(?P<lcsc>C\d+)</span>',
                      re.IGNORECASE)
_PRICE_RE = re.compile(r'<span[^>]*class="[^"]*price[^"]*"[^>]*>\s*\$(?P<price>[0-9,.]+)\s*</span>',
                       re.IGNORECASE)
_STOCK_RE = re.compile(
    r'<span[^>]*class="[^"]*stock[^"]*"[^>]*>\s*(?P<stock>[0-9,]+)\s*(?:in stock)?\s*</span>',
    re.IGNORECASE)
_MPN_RE = re.compile(r'<span[^>]*class="[^"]*\bmpn\b[^"]*"[^>]*>([^<]+)</span>', re.IGNORECASE)
_MANUFACTURER_RE = re.compile(
    r'<span[^>]*class="[^"]*\bmanufacturer\b[^"]*"[^>]*>([^<]+)</span>', re.IGNORECASE)
_SKU_IN_URL_RE = re.compile(r'(?:^|[/_-])(C\d+)(?:\.html)?(?:[?#]|$)', re.IGNORECASE)
_NO_RESULTS_RE = re.compile(r'\b(?:no results(?: found)?|no products found|0 results)\b', re.IGNORECASE)


class LcscAdapter:
    key = SITE
    hosts = ("lcsc.com",)
    result_kinds = ("product",)
    cad_acquisition = False
    login_required = False
    api = False

    def search(self, query: str) -> list[dict]:
        url = SEARCH_URL_TMPL.format(query=quote_query(query))
        result = _session.browser_fetch(url, SITE)
        if not result.get("ok"):
            return [source_failure(SITE, "search", result, url=url)]
        content = result.get("content", "")
        page_url = result.get("final_url") or url
        if challenge.classify_challenge(content, page_url) == "captcha" or is_login_wall(content, _CHALLENGE_MARKERS):
            return [_challenge(SITE, "search", page_url, content)]
        if RetailAdapter._redirected_to_login(url, page_url) or (
                is_login_wall(content, _LOGIN_WALL_MARKERS) and not _RESULT_RE.search(content)):
            return [source_failure(SITE, "search", login_required(SITE, page_url), url=page_url)]

        hits = []
        seen: set[str] = set()
        rows = list(_ROW_RE.finditer(content))
        matches = [(m, row.group()) for row in rows for m in _RESULT_RE.finditer(row.group())]
        if not matches:
            matches = [(m, m.group()) for m in _RESULT_RE.finditer(content)]
        for match, row in matches:
            sku = match.group("lcsc").upper()
            if sku in seen:
                continue
            seen.add(sku)
            price, stock = _PRICE_RE.search(row), _STOCK_RE.search(row)
            mpn, manufacturer = _MPN_RE.search(row), _MANUFACTURER_RE.search(row)
            hits.append(make_hit(
                SITE, title=html.unescape(match.group("title")).strip(),
                url=f"{BASE_URL}/product-detail/{sku}.html", kind="product", sku=sku,
                mpn=html.unescape(mpn.group(1)).strip() if mpn else None,
                manufacturer=html.unescape(manufacturer.group(1)).strip() if manufacturer else None,
                price=_price(price.group("price")) if price else None,
                currency="USD" if price else None, stock=_stock(stock.group("stock")) if stock else None,
                thumbnail=card_image(row, result.get("final_url") or url),
            ))
        if not hits and not _NO_RESULTS_RE.search(visible_text(content)):
            return [fail(SITE, "search", "parse_failed", "No recognizable LCSC results were rendered.", url=url)]
        return hits

    def acquire(self, result: dict) -> dict:
        url = result.get("url", "")
        if not url:
            return fail(SITE, "acquire", "not_found", "Result has no product URL.")
        page = _session.browser_fetch(url, SITE)
        if not page.get("ok"):
            return source_failure(SITE, "acquire", page, url=url)
        content = page.get("content", "")
        page_url = page.get("final_url") or url
        if challenge.classify_challenge(content, page_url) == "captcha" or is_login_wall(content, _CHALLENGE_MARKERS):
            return _challenge(SITE, "acquire", page_url, content)
        datasheet_url = find_datasheet_url(content, page_url)
        if RetailAdapter._redirected_to_login(url, page_url) or (
                is_login_wall(content, _LOGIN_WALL_MARKERS) and not _LCSC_RE.search(content)
                and not datasheet_url):
            return source_failure(SITE, "acquire", login_required(SITE, page_url), url=page_url)

        lcsc_match = _LCSC_RE.search(content)
        path_matches = [_SKU_IN_URL_RE.search(candidate) for candidate in
                        (url, page.get("final_url") or url)]
        identified = [str(result["sku"]).upper()] if result.get("sku") else []
        identified.extend(match.group(1).upper() for match in path_matches if match)
        if lcsc_match:
            identified.append(lcsc_match.group("lcsc").upper())
        expected = identified[0] if identified else None
        actual = lcsc_match.group("lcsc").upper() if lcsc_match else None
        if len(set(identified)) > 1:
            return fail(SITE, "acquire", "unsupported", "The detail page identifies a different LCSC SKU.",
                        url=url, ask_user="Select the desired LCSC SKU explicitly.")

        if not datasheet_url:
            return fail(SITE, "acquire", "not_found", "No datasheet PDF was found on this product page.", url=url)
        dest, refusal = download_path(SITE, datasheet_url, default="datasheet.pdf")
        if refusal is not None:
            return {**fail(SITE, "acquire", "policy_refused", refusal.get("hint", "Invalid destination."),
                           url=datasheet_url), "detail": refusal.get("reason")}
        outcome = _session.browser_download(datasheet_url, SITE, dest)
        if not outcome.get("ok"):
            return source_failure(SITE, "acquire", outcome, url=datasheet_url)
        artifact_path = outcome.get("dest")
        if not artifact_path:
            return fail(SITE, "acquire", "parse_failed", "Download returned no file.", url=datasheet_url)
        checked = classify_artifact(artifact_path, "document")
        if not checked["ok"] or checked["format"] != "pdf":
            return fail(SITE, "acquire", "parse_failed", checked["reason"] or "Invalid PDF.", url=datasheet_url)

        price, stock = _PRICE_RE.search(content), _STOCK_RE.search(content)
        return provenance_envelope(
            source_url=page.get("final_url") or url,
            session_profile_used=page.get("session_profile_used", SITE),
            license_note=LICENSE_NOTE, artifact_path=outcome["dest"], artifact_kind="document", format="pdf",
            title=result.get("title", ""), sku=actual or expected,
            price=_price(price.group("price")) if price else result.get("price"),
            currency="USD" if price else result.get("currency"),
            stock=_stock(stock.group("stock")) if stock else result.get("stock"),
            datasheet_url=datasheet_url, datasheet_path=outcome["dest"],
        )


ADAPTER = register(LcscAdapter())
