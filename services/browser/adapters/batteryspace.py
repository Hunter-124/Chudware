"""services/browser/adapters/batteryspace.py — BatterySpace source adapter (Parts Library V2).

BatterySpace / AA Portable Power (batteryspace.com): battery cells, packs, and
chargers sold e-commerce with price/stock. ``acquire`` uses the inherited
default ``extract_params`` — :func:`~._retail.find_datasheet_url` grabs the
datasheet/spec PDF when present.

Live-verified 2026-07-10: the storefront runs on AmeriCommerce (.aspx). The old
``/search?q=`` guess 404'd — the real product search is
``/search.aspx?find=<query>`` (the header search form posts to ``search.aspx``
with the ``find`` param; a live ``search.aspx?find=CR123A`` link is baked into
the site chrome). Results carry NO JSON-LD — they are server-rendered
``product-list-item`` cards whose ``product-list-options`` block holds an
``<h5><a href>`` product URL + title, a ``product-list-cost-value`` price, and a
``product-list-text`` "Product ID: N" SKU — so this adapter overrides
:meth:`parse_search` to read that markup directly. The header "Sign In" and the
newsletter ``name="password"`` field are NOT a wall on this public catalog, so
the login markers are dropped (challenge markers stay for a real bot wall).
"""
from __future__ import annotations

import re
from html import unescape

from . import abs_url, register
from ._retail import RetailAdapter, _price, _strip_tags, card_blocks, card_image

SITE = "batteryspace"
BASE_URL = "https://www.batteryspace.com"
SEARCH_URL_TMPL = BASE_URL + "/search.aspx?find={query}"
LICENSE_NOTE = (
    "BatterySpace product data: reference-only, link-out commerce data. This "
    "adapter never stores or redistributes BatterySpace's page content — only a "
    "small price/spec record copied with provenance (source URL + fetch "
    "timestamp) for the user's own sourcing decision, never LLM-generated. "
    "Datasheets remain the manufacturer's documents fetched for the user's own "
    "engineering reference. See contracts/license_debt.md."
)

# A header "Sign In" link and a newsletter password field appear on the PUBLIC
# product search — neither is a login wall. Drop the false markers; a genuine bot
# block still degrades via the challenge markers (Akamai/PX/captcha interstitials).
_LOGIN_WALL_MARKERS = ()
_CHALLENGE_MARKERS = ("Access Denied", "Reference #", "Pardon Our Interruption")

# The product image precedes the options; rating and cart button images are not
# product previews, even though they occur in the same product-list-item.
_CARD_RE = re.compile(
    r'<div class="product-list-options">\s*'
    r'<h5>\s*<a\b[^>]*\bhref="(?P<href>[^"]+)"[^>]*>\s*(?P<title>.*?)\s*</a>',
    re.IGNORECASE | re.DOTALL)
_PRICE_RE = re.compile(
    r'<span class="product-list-cost-value">\s*(?P<price>[^<]+?)\s*</span>',
    re.IGNORECASE | re.DOTALL)
_PID_RE = re.compile(r'Product ID:\s*(?P<pid>\d+)', re.IGNORECASE)
_IMAGE_RE = re.compile(
    r'<img\b(?=[^>]*\bclass=["\'][^"\']*\bproduct-list-img\b[^"\']*["\'])[^>]*>',
    re.IGNORECASE)
_PRICE_RANGE_RE = re.compile(
    r'From\s+(\$[\d,]+(?:\.\d+)?)\s+to\s+(\$[\d,]+(?:\.\d+)?)', re.IGNORECASE)


class BatterySpaceAdapter(RetailAdapter):
    key = SITE
    currency = "USD"
    hosts = ("www.batteryspace.com", "batteryspace.com")
    result_kinds = ("product",)
    cad_acquisition = False
    login_required = False
    api = False
    base_url = BASE_URL
    search_url_tmpl = SEARCH_URL_TMPL
    license_note = LICENSE_NOTE
    login_wall_markers = _LOGIN_WALL_MARKERS
    challenge_markers = _CHALLENGE_MARKERS

    def parse_search(self, content: str) -> list[dict]:
        """Read the AmeriCommerce product cards and their own imagery."""
        hits: list[dict] = []
        seen: set[str] = set()
        for block in card_blocks(content, "div", "product-list-item"):
            match = _CARD_RE.search(block)
            if not match:
                continue
            url = abs_url(BASE_URL, unescape(match.group("href")))
            title = unescape(_strip_tags(match.group("title")))
            if not url or not title or url in seen:
                continue
            seen.add(url)
            image = _IMAGE_RE.search(block)
            hit = {
                "ok": True, "title": title, "url": url, "site": SITE,
                "thumbnail": card_image(image.group(0), BASE_URL) if image else None,
            }
            price = _PRICE_RE.search(block)
            if price:
                value = unescape(_strip_tags(price.group("price")))
                price_range = _PRICE_RANGE_RE.fullmatch(value)
                if price_range:
                    # The low end is not the price of an unspecified variant.
                    hit["price_range"] = {"min": _price(price_range.group(1)),
                                          "max": _price(price_range.group(2)), "currency": "USD"}
                    hit["currency"] = "USD"
                else:
                    hit["price"] = value
                hit["price_display"] = value
            pid = _PID_RE.search(block)
            if pid:
                hit["sku"] = pid.group("pid")
            qty = card_blocks(block, "div", "product-list-qty")
            if qty:
                value = unescape(_strip_tags(qty[0]))
                count = re.search(r"([\d,]+)\s+(?:items?\s+)?in stock\b", value, re.I)
                if count:
                    hit["stock"] = count.group(1)
                elif re.search(r"\bout of stock\b", value, re.I):
                    hit["stock"] = 0
            hits.append(hit)
        return hits


ADAPTER = register(BatterySpaceAdapter())
