"""Anaheim Automation's Magento product search and CAD/datasheet downloads.

The canonical host is non-www. Customer login uses the user's native Chrome
session; public search results and direct downloads do not require a login.
"""
from __future__ import annotations

import html
import re

from .. import session as _session
from . import abs_url, fail, login_required, make_hit, quote_query, register, source_failure
from ._retail import _price, _stock, card_image
from .maxon import MotorAdapter, _card_fragments, _strip_tags

SITE = "anaheimautomation"
BASE_URL = "https://anaheimautomation.com"
POST_LOGIN_URL = BASE_URL + "/customer/account/"
LOGIN_URL = BASE_URL + "/customer/account/login/"
# Magento's reCAPTCHA requires an unautomated, user-driven sign-in.
NATIVE_CHROME_LOGIN = True
SEARCH_URL_TMPL = BASE_URL + "/catalogsearch/result/?q={query}"
LICENSE_NOTE = (
    "Anaheim Automation CAD models and datasheets are fetched under the "
    "user's own authenticated Anaheim Automation session for their own "
    "engineering use and are not redistributed. This adapter downloads only "
    "under the user's own authenticated Anaheim Automation session. See "
    "contracts/license_debt.md."
)
_LOGIN_WALL_MARKERS = (
    "Customer Login", 'id="login-form"', 'class="form form-login"',
    "Please sign in to download", "reCAPTCHA verification failed", "Invalid Form Key",
)


def is_logged_in(content: str | None, url: str = "") -> bool:
    """Recognize the account dashboard or Magento's signed-in customer menu."""
    u = (url or "").lower()
    if any(frag in u for frag in (
        "/customer/account/login", "/customer/account/create",
        "/customer/account/forgotpassword", "/customer/ajax/login",
    )):
        return False
    if "/customer/account" in u and "/login" not in u:
        return True
    hay = content or ""
    return bool(re.search(r'class="[^"]*customer-welcome', hay, re.I)
                or re.search(r'href="[^"]*customer/account/logout', hay, re.I)
                or "Log Out" in hay or "Sign Out" in hay)


_RESULT_RE = re.compile(
    r'<a\b(?=[^>]*\bclass\s*=\s*["\'][^"\']*\bproduct-item-link\b)'
    r'(?=[^>]*\bhref\s*=\s*["\'](?P<href>[^"\']+)["\'])[^>]*>(?P<title>.*?)</a\s*>',
    re.IGNORECASE | re.DOTALL)
_PRICE_RE = re.compile(
    r'<[^>]*(?=[^>]*\bdata-price-type\s*=\s*["\']finalPrice["\'])'
    r'(?=[^>]*\bdata-price-amount\s*=\s*["\'](?P<price>[\d.,]+)["\'])[^>]*>', re.I)
_SKU_RE = re.compile(r'\bdata-(?:product-)?sku\s*=\s*["\']([^"\']+)["\']', re.I)
_MPN_RE = re.compile(r'\bdata-(?:product-)?mpn\s*=\s*["\']([^"\']+)["\']', re.I)
_STOCK_RE = re.compile(r'\bdata-stock-qty\s*=\s*["\']([^"\']+)["\']', re.I)
_CURRENCY_RE = re.compile(r'\bdata-(?:price-)?currency(?:-code)?\s*=\s*["\']([A-Z]{3})["\']', re.I)


class AnaheimAutomationAdapter(MotorAdapter):
    key = SITE
    license_note = LICENSE_NOTE
    login_markers = _LOGIN_WALL_MARKERS
    LOGIN_URL = LOGIN_URL
    BASE_URL = BASE_URL
    NATIVE_CHROME_LOGIN = NATIVE_CHROME_LOGIN
    _LOGIN_WALL_MARKERS = _LOGIN_WALL_MARKERS

    def is_logged_in(self, content: str | None, url: str = "") -> bool:
        return is_logged_in(content, url)

    def search(self, query: str) -> list[dict]:
        url = SEARCH_URL_TMPL.format(query=quote_query(query))
        result = _session.browser_fetch(url, SITE)
        if not result.get("ok"):
            return [source_failure(SITE, "search", result, url=url)]
        content = result.get("content") or ""
        base = result.get("final_url") or url
        # Magento embeds a login dialog even on public search pages.
        wall = self._page_failure("search", base, content, check_login=False)
        if wall:
            return [wall]
        # A card owns its own image and price; the fallback accepts old title-only markup.
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
            # Magento's live cards use the manufacturer part number as the complete title.
            if re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', title) and re.search(r'\d', title):
                sku, mpn = sku or title, mpn or title
            amount = _PRICE_RE.search(card)
            price = _price(amount.group("price")) if amount else None
            curr = _CURRENCY_RE.search(card)
            currency = curr.group(1).upper() if curr else ("USD" if price is not None and "$" in card else None)
            stock_match = _STOCK_RE.search(card)
            hits.append(make_hit(SITE, title=title, url=product_url, kind="product",
                                 sku=sku, mpn=mpn, manufacturer="Anaheim Automation",
                                 price=price, currency=currency,
                                 stock=_stock(stock_match.group(1)) if stock_match else None,
                                 thumbnail=card_image(card, base)))
        if not hits and not re.search(r"(?:no results|no products found|search returned no results|0 results)",
                                      content, re.I):
            if (re.search(r"/customer/(?:account|ajax)/login(?:[/?#]|$)", base, re.I)
                    or re.search(r"<(?:title|h1)\b[^>]*>\s*(?:customer\s+login|sign\s+in|log\s+in)\b",
                                 content, re.I)
                    or re.search(r"please sign in to (?:view|continue|download)|you must be logged in",
                                 content, re.I)):
                question = login_required(SITE, base)
                return [fail(SITE, "search", "login_required", question["hint"], url=base,
                             ask_user=question.get("ask_user"))]
            return [fail(SITE, "search", "parse_failed", "No recognizable product cards were found.", url=url)]
        return hits


ADAPTER = register(AnaheimAutomationAdapter())