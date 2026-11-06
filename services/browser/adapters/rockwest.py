"""services/browser/adapters/rockwest.py — Rock West Composites source adapter (Parts Library V2).

Composites vendor (tubes/sheets) with an open storefront, live prices and spec
PDFs: ``acquire`` uses the default machining/spec-table param extraction plus
datasheet discovery, and copies only a small price/stock-spec record with
provenance. Challenges are reported as blocked or captcha, never solved.

Live-verified 2026-07-10: the storefront runs on Salesforce Commerce Cloud
(Demandware), so search is ``/on/demandware.store/Sites-RWC-Site/default/Search-Show?q=``
(the old ``/search?q=`` guess 404'd). Results carry no JSON-LD Product records —
they are server-rendered ``product-tile`` cards whose ``pdp-link`` block holds the
product URL, ``product-name`` title and a ``productSkuNumber`` P/N — so this
adapter overrides :meth:`parse_search` to read that markup directly.
"""
from __future__ import annotations

import re
from html import unescape

from . import abs_url, register
from ._retail import RetailAdapter, _strip_tags, card_blocks, card_image

SITE = "rockwest"
BASE_URL = "https://www.rockwestcomposites.com"
SEARCH_URL_TMPL = (
    BASE_URL + "/on/demandware.store/Sites-RWC-Site/default/Search-Show?q={query}")
LICENSE_NOTE = (
    "Rock West Composites product data: reference-only, link-out commerce data. "
    "This adapter never stores or redistributes Rock West Composites' page "
    "content — only a small price/stock-spec record copied with provenance "
    "(source URL + fetch timestamp) for the user's own sourcing decision, never "
    "LLM-generated. See contracts/license_debt.md."
)

_LOGIN_WALL_MARKERS = ('id="login-email"',)
_CHALLENGE_MARKERS = ("Access Denied", "Pardon Our Interruption")

# A Demandware search result card: the `pdp-link` block wraps an `<a class="link">`
# carrying the product URL and a `<p class="product-name">` title. The SKU lives in a
# sibling `<span class="productSkuNumber">…P/N: <sku></span>` (optional — grid tiles
# for series/landing rows omit it).
_CARD_RE = re.compile(
    r'<div class="pdp-link">\s*'
    r'<a\b[^>]*\bclass="link"[^>]*\bhref="(?P<href>[^"]+)"[^>]*>\s*'
    r'<p class="product-name">\s*(?P<title>.*?)\s*</p>',
    re.IGNORECASE | re.DOTALL)
_SKU_RE = re.compile(
    r'<span class="productSkuNumber">.*?P/N:\s*(?P<sku>[^\s<]+)', re.IGNORECASE | re.DOTALL)
_SALES_RE = re.compile(
    r'<span\b(?=[^>]*\bclass=["\'][^"\']*\bsales\b[^"\']*["\'])'
    r'(?=[^>]*\bdata-price-formatted=["\'](?P<price>[^"\']+)["\'])[^>]*>',
    re.IGNORECASE)
_AVAILABLE_RE = re.compile(r'\bAvailable\s+Now\s*:\s*([\d,]+)', re.IGNORECASE)


class RockWestAdapter(RetailAdapter):
    key = SITE
    hosts = ("www.rockwestcomposites.com", "rockwestcomposites.com")
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
        """Read each Demandware product tile without borrowing adjacent data."""
        hits: list[dict] = []
        seen: set[str] = set()
        blocks = card_blocks(content, "div", "product-tile")
        scoped = bool(blocks)
        if not scoped:
            # Historical saved snippets have PDP links and SKUs but no tile wrapper.
            cards = list(_CARD_RE.finditer(content))
            blocks = [content[m.start():cards[i + 1].start() if i + 1 < len(cards) else len(content)]
                      for i, m in enumerate(cards)]
        for block in blocks:
            match = _CARD_RE.search(block)
            if not match:
                continue
            url = abs_url(BASE_URL, unescape(match.group("href")))
            title = unescape(_strip_tags(match.group("title")))
            if not url or not title or url in seen:
                continue
            seen.add(url)
            hit = {"ok": True, "title": title, "url": url, "site": SITE}
            sku = _SKU_RE.search(block)
            if sku:
                hit["sku"] = unescape(sku.group("sku").strip())
            if scoped:
                media = card_blocks(block, "div", "image-container")
                hit["thumbnail"] = card_image(media[0], BASE_URL) if media else None
                price = _SALES_RE.search(block)
                if price:
                    hit.update(price=unescape(price.group("price")), currency="USD")
                availability = card_blocks(block, "span", "availabilityProduct")
                if availability:
                    stock = _AVAILABLE_RE.search(_strip_tags(availability[0]))
                    if stock:
                        hit["stock"] = stock.group(1)
            hits.append(hit)
        return hits


ADAPTER = register(RockWestAdapter())
