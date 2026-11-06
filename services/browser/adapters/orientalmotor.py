"""Oriental Motor product search and product-page CAD/datasheet downloads.

Search-card and download markup is best effort: the public site may block
browser requests, and series pages require an explicit product selection.
"""
from __future__ import annotations

import html
import re

from .. import session as _session
from . import abs_url, fail, make_hit, quote_query, register, source_failure
from ._retail import _price, _stock, card_image
from .maxon import MotorAdapter, _card_fragments, _strip_tags

SITE = "orientalmotor"
BASE_URL = "https://www.orientalmotor.com"
SEARCH_URL_TMPL = BASE_URL + "/search?q={query}"
LICENSE_NOTE = (
    "Oriental Motor CAD models and datasheets are fetched under the user's "
    "own authenticated Oriental Motor session for their own engineering use "
    "and are not redistributed. This adapter downloads only under the user's "
    "own authenticated Oriental Motor session. See contracts/license_debt.md."
)
_LOGIN_WALL_MARKERS = ('Log in to Oriental Motor', 'id="loginForm"', 'class="login-form"',
                       'Please sign in to download')
_RESULT_RE = re.compile(
    r'<a\b(?=[^>]*\bclass\s*=\s*["\'][^"\']*\bproduct-title\b)'
    r'(?=[^>]*\bhref\s*=\s*["\'](?P<href>[^"\']+)["\'])[^>]*>(?P<title>.*?)</a\s*>',
    re.IGNORECASE | re.DOTALL)
_MFR_RE = re.compile(r'<span\b[^>]*class=["\'][^"\']*manufacturer[^"\']*["\'][^>]*>(?P<mfr>.*?)</span>',
                     re.IGNORECASE | re.DOTALL)
_SKU_RE = re.compile(r'\bdata-(?:product-)?sku\s*=\s*["\']([^"\']+)["\']', re.I)
_MPN_RE = re.compile(r'\bdata-(?:product-)?mpn\s*=\s*["\']([^"\']+)["\']', re.I)
_PRICE_RE = re.compile(r'\bdata-price(?:-amount)?\s*=\s*["\']([^"\']+)["\']', re.I)
_PRICE_TEXT_RE = re.compile(r'<[^>]*class=["\'][^"\']*\bprice\b[^"\']*["\'][^>]*>(.*?)</[^>]+>',
                            re.I | re.S)
_CURRENCY_RE = re.compile(r'\bdata-(?:price-)?currency(?:-code)?\s*=\s*["\']([A-Z]{3})["\']', re.I)
_STOCK_RE = re.compile(r'\bdata-stock-qty\s*=\s*["\']([^"\']+)["\']', re.I)


class OrientalMotorAdapter(MotorAdapter):
    key = SITE
    license_note = LICENSE_NOTE
    login_markers = _LOGIN_WALL_MARKERS
    result_kinds = ("product", "series")

    def search(self, query: str) -> list[dict]:
        url = SEARCH_URL_TMPL.format(query=quote_query(query))
        result = _session.browser_fetch(url, SITE)
        if not result.get("ok"):
            return [source_failure(SITE, "search", result, url=url)]
        content = result.get("content") or ""
        base = result.get("final_url") or url
        wall = self._page_failure("search", base, content)
        if wall:
            return [wall]
        cards = _card_fragments(content)
        fragments = cards or [m.group(0) for m in _RESULT_RE.finditer(content)]
        hits = []
        seen = set()
        for card in fragments:
            match = _RESULT_RE.search(card)
            if not match:
                continue
            product_url = abs_url(base, html.unescape(match.group("href")))
            title = _strip_tags(match.group("title"))
            if not product_url or not title or product_url in seen:
                continue
            seen.add(product_url)
            sku_match, mpn_match = _SKU_RE.search(card), _MPN_RE.search(card)
            sku = html.unescape(sku_match.group(1)).strip() if sku_match else None
            mpn = html.unescape(mpn_match.group(1)).strip() if mpn_match else None
            kind = "series" if not (sku or mpn) and re.search(r"\bseries\b", title, re.I) else "product"
            mfr_match = _MFR_RE.search(card)
            manufacturer = _strip_tags(mfr_match.group("mfr")) if mfr_match else "Oriental Motor"
            amount = _PRICE_RE.search(card)
            price_text = _PRICE_TEXT_RE.search(card)
            raw_price = amount.group(1) if amount else (_strip_tags(price_text.group(1)) if price_text else None)
            price = _price(raw_price)
            curr = _CURRENCY_RE.search(card)
            currency = curr.group(1).upper() if curr else ("USD" if price is not None and "$" in card else None)
            stock = _STOCK_RE.search(card)
            hits.append(make_hit(SITE, title=title, url=product_url, kind=kind, sku=sku, mpn=mpn,
                                 manufacturer=manufacturer, price=price, currency=currency,
                                 stock=_stock(stock.group(1)) if stock else None,
                                 thumbnail=card_image(card, base)))
        if not hits and not re.search(r"(?:no results|no products found|search returned no results|0 results)",
                                      content, re.I):
            return [fail(SITE, "search", "parse_failed", "No recognizable product cards were found.", url=url)]
        return hits


ADAPTER = register(OrientalMotorAdapter())