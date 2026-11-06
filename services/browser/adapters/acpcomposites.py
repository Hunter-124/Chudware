"""services/browser/adapters/acpcomposites.py — ACP Composites source adapter (Parts Library V2).

Composites vendor on WooCommerce with an open storefront, live prices and a
mechanical-properties PDF: ``acquire`` uses the default machining/spec-table
param extraction plus datasheet discovery, and copies only a small
price/stock-spec record with provenance. Challenge pages are reported as
blocked or captcha, never solved.

Live-verified 2026-07-10: the WooCommerce search ``/?s=<q>&post_type=product``
(HTTP 200) renders a server-side FacetWP results grid — NOT JSON-LD Products, so
the default parse found nothing. Each hit is an ``acp-search-listing`` div whose
``acp-search-listing-content`` block carries a ``<h3><a href="/shop/<slug>">Title
</a></h3>`` (no per-listing price — prices are per-variant on the product page).
This adapter overrides :meth:`parse_search` to read those title anchors. The
open catalog needs no login; the header "Sign In" widget was a false wall marker
and is dropped.
"""
from __future__ import annotations

import re
from html import unescape
from urllib.parse import urlparse

from . import abs_url, register
from ._retail import RetailAdapter, _strip_tags, card_blocks, card_image

SITE = "acpcomposites"
BASE_URL = "https://www.acpcomposites.com"
SEARCH_URL_TMPL = BASE_URL + "/?s={query}&post_type=product"
LICENSE_NOTE = (
    "ACP Composites product data: reference-only, link-out commerce data. This "
    "adapter never stores or redistributes ACP Composites' page content — only "
    "a small price/stock-spec record copied with provenance (source URL + fetch "
    "timestamp) for the user's own sourcing decision, never LLM-generated. "
    "See contracts/license_debt.md."
)

# The public catalog needs no login; the header "Sign In" widget rides every
# page, so it is NOT a wall marker here.
_LOGIN_WALL_MARKERS: tuple[str, ...] = ()
_CHALLENGE_MARKERS = ("Access Denied", "Pardon Our Interruption")

# FacetWP listings contain a thumbnail sibling before the title.
_TITLE_RE = re.compile(
    r'<h3\b[^>]*>\s*<a\b[^>]*\bhref=["\'](?P<href>[^"\']+)["\'][^>]*>'
    r'(?P<title>.*?)</a>', re.IGNORECASE | re.DOTALL)


class AcpCompositesAdapter(RetailAdapter):
    key = SITE
    hosts = ("www.acpcomposites.com", "acpcomposites.com")
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
        hits: list[dict] = []
        seen: set[str] = set()
        for block in card_blocks(content, "div", "acp-search-listing"):
            title_match = _TITLE_RE.search(block)
            if not title_match:
                continue
            url = abs_url(BASE_URL, unescape(title_match.group("href")))
            title = unescape(_strip_tags(title_match.group("title")))
            if not url or not urlparse(url).path.startswith("/shop/") or not title or url in seen:
                continue
            seen.add(url)
            image_blocks = card_blocks(block, "div", "acp-search-listing-thumbnail")
            hit = {
                "ok": True, "site": SITE, "title": title, "url": url,
                "thumbnail": card_image(image_blocks[0], BASE_URL) if image_blocks else None,
            }
            prices = card_blocks(block, "span", "price")
            if prices:
                value = unescape(_strip_tags(prices[0]))
                # A range or a crossed-out sale price is not one exact price.
                if re.fullmatch(r"\$\s*[\d,]+(?:\.\d{2})?", value):
                    hit.update(price=value, currency="USD")
            stocks = card_blocks(block, "p", "stock")
            if stocks:
                value = unescape(_strip_tags(stocks[0]))
                count = re.search(r"([\d,]+)\s+(?:items?\s+)?in stock\b", value, re.I)
                if count:
                    hit["stock"] = count.group(1)
                elif re.search(r"\bout of stock\b", value, re.I):
                    hit["stock"] = 0
            hits.append(hit)
        return hits


ADAPTER = register(AcpCompositesAdapter())
