"""JLCPCB parts-library search.

Search results preserve the associated LCSC number and available price and stock.
This adapter does not download CAD or place orders.
"""
from __future__ import annotations

import html
import re

from .. import challenge, session as _session
from . import (abs_url, fail, is_login_wall, login_required, make_hit, quote_query,
               register, source_failure)
from ._retail import RetailAdapter, _challenge, _price, _stock, _strip_tags, card_image, visible_text

SITE = "jlcpcb"
BASE_URL = "https://jlcpcb.com"
SEARCH_URL_TMPL = BASE_URL + "/parts/componentSearch?searchTxt={query}"
LICENSE_NOTE = (
    "JLCPCB/LCSC parts-library data (price, stock) is copied with provenance "
    "(source URL + fetch timestamp) for the user's own BOM/sourcing decision, "
    "never redistributed in bulk and never LLM-generated. Quote/order terms "
    "click-throughs are never auto-accepted. See contracts/license_debt.md."
)

_LOGIN_WALL_MARKERS = ('id="login-email"', 'class="login-dialog"')
_CHALLENGE_MARKERS = ("Access Denied", "Pardon Our Interruption", "Just a moment")
_TERMS_MARKERS = ('Accept the terms', 'class="order-terms"', 'agree to the Terms')
_RESULT_RE = re.compile(
    r'''<a\b(?=[^>]*\bhref=["'](?P<href>/partdetail/[^"'?#]+)(?:[?#][^"']*)?["'])'''
    r'''[^>]*>(?P<title>.*?)</a>''', re.IGNORECASE | re.DOTALL)
_ROW_RE = re.compile(r'<(?P<tag>tr|article)\b[^>]*>.*?</(?P=tag)>', re.IGNORECASE | re.DOTALL)
_LCSC_RE = re.compile(r'<span[^>]*class="[^"]*lcsc-number[^"]*"[^>]*>\s*(?P<lcsc>C\d+)\s*</span>', re.IGNORECASE)
_LCSC_IN_HREF_RE = re.compile(r'(?:/|_|-)(C\d+)(?:\.html)?$', re.IGNORECASE)
_PRICE_RE = re.compile(r'<span[^>]*class="[^"]*price[^"]*"[^>]*>\s*\$(?P<price>[0-9,.]+)\s*</span>', re.IGNORECASE)
_TIER_PRICE_RE = re.compile(
    r'<span\b[^>]*>\s*1\+\s*</span>\s*<span\b[^>]*>\s*\$(?P<price>[0-9,.]+)\s*</span>',
    re.IGNORECASE)
_EMPTY_IMAGE_RE = re.compile(r'<img\b[^>]*(?:img-empty|no-image|placeholder-image)[^>]*>', re.IGNORECASE)
_STOCK_RE = re.compile(r'<span[^>]*class="[^"]*stock[^"]*"[^>]*>\s*(?P<stock>[0-9,]+)\s*(?:in stock)?\s*</span>',
                       re.IGNORECASE)
_META_SPAN_RE = re.compile(
    r'<span[^>]*class="[^"]*(?:lcsc-number|price|stock)[^"]*"[^>]*>.*?</span>',
    re.IGNORECASE | re.DOTALL)
_MPN_RE = re.compile(r'<span[^>]*class="[^"]*\bmpn\b[^"]*"[^>]*>([^<]+)</span>', re.IGNORECASE)
_MANUFACTURER_RE = re.compile(
    r'<span[^>]*class="[^"]*\bmanufacturer\b[^"]*"[^>]*>([^<]+)</span>', re.IGNORECASE)
_NO_RESULTS_RE = re.compile(r'\b(?:no results(?: found)?|no products found|0 results)\b', re.IGNORECASE)


class JlcpcbAdapter:
    key = SITE
    hosts = ("jlcpcb.com",)
    result_kinds = ("product",)
    cad_acquisition = False
    login_required = False
    api = False

    def search(self, query: str) -> list[dict]:
        url = SEARCH_URL_TMPL.format(query=quote_query(query))
        response = _session.browser_fetch(url, SITE)
        if not response.get("ok"):
            return [source_failure(SITE, "search", response, url=url)]
        content = response.get("content", "")
        page_url = response.get("final_url") or url
        if challenge.classify_challenge(content, page_url) == "captcha" or is_login_wall(content, _CHALLENGE_MARKERS):
            return [_challenge(SITE, "search", page_url, content)]
        if RetailAdapter._redirected_to_login(url, page_url) or (
                is_login_wall(content, _LOGIN_WALL_MARKERS) and not _RESULT_RE.search(content)):
            return [source_failure(SITE, "search", login_required(SITE, page_url), url=page_url)]

        rows = list(_ROW_RE.finditer(content))
        matches = [(m, row.group()) for row in rows for m in _RESULT_RE.finditer(row.group())]
        if not matches:
            matches = [(m, m.group()) for m in _RESULT_RE.finditer(content)]
        hits, seen = [], set()
        usd_declared = bool(re.search(r'\bUSD\b', visible_text(content)))
        for match, card in matches:
            product_url = abs_url(response.get("final_url") or url, html.unescape(match.group("href")))
            title = html.unescape(_strip_tags(_META_SPAN_RE.sub("", match.group("title")))).strip()
            if not product_url or not title or product_url in seen:
                continue
            lcsc, price, stock = _LCSC_RE.search(card), _PRICE_RE.search(card), _STOCK_RE.search(card)
            tier_price = _TIER_PRICE_RE.search(card)
            price = price or tier_price
            href_sku = _LCSC_IN_HREF_RE.search(match.group("href"))
            if lcsc and href_sku and lcsc.group("lcsc").upper() != href_sku.group(1).upper():
                continue
            seen.add(product_url)
            mpn, manufacturer = _MPN_RE.search(card), _MANUFACTURER_RE.search(card)
            hits.append(make_hit(
                SITE, title=title, url=product_url, kind="product",
                sku=(lcsc.group("lcsc") if lcsc else href_sku.group(1) if href_sku else "").upper() or None,
                mpn=html.unescape(mpn.group(1)).strip() if mpn else None,
                manufacturer=html.unescape(manufacturer.group(1)).strip() if manufacturer else None,
                price=_price(price.group("price")) if price else None,
                currency=("USD" if price and (not tier_price or usd_declared) else None),
                stock=_stock(stock.group("stock")) if stock else None,
                thumbnail=card_image(_EMPTY_IMAGE_RE.sub("", card), response.get("final_url") or url),
            ))
        if not hits and not _NO_RESULTS_RE.search(visible_text(content)):
            return [fail(SITE, "search", "parse_failed", "No recognizable JLCPCB results were rendered.", url=url)]
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
        if RetailAdapter._redirected_to_login(url, page_url) or (
                is_login_wall(content, _LOGIN_WALL_MARKERS) and not _LCSC_RE.search(content)):
            return source_failure(SITE, "acquire", login_required(SITE, page_url), url=page_url)
        if is_login_wall(content, _TERMS_MARKERS):
            return fail(SITE, "acquire", "terms_required",
                        "JLCPCB requires accepting order or quote terms.", url=url,
                        ask_user="Review and accept the terms on JLCPCB before continuing.")
        return fail(SITE, "acquire", "unsupported",
                    "JLCPCB parts-library search does not provide a downloadable CAD artifact.", url=url)


ADAPTER = register(JlcpcbAdapter())
