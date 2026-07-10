"""services/browser/adapters/rockwest.py — Rock West Composites source adapter (Parts Library V2).

Composites vendor (tubes/sheets) with an open storefront, live prices and spec
PDFs: ``acquire`` uses the default machining/spec-table param extraction plus
datasheet discovery, and copies only a small price/stock-spec record with
provenance. Challenge pages degrade to ``login_required`` — never solved.

Live-verified 2026-07-10: the storefront runs on Salesforce Commerce Cloud
(Demandware), so search is ``/on/demandware.store/Sites-RWC-Site/default/Search-Show?q=``
(the old ``/search?q=`` guess 404'd). Results carry no JSON-LD Product records —
they are server-rendered ``product-tile`` cards whose ``pdp-link`` block holds the
product URL, ``product-name`` title and a ``productSkuNumber`` P/N — so this
adapter overrides :meth:`parse_search` to read that markup directly.
"""
from __future__ import annotations

import re

from . import register
from ._retail import RetailAdapter, _strip_tags

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


class RockWestAdapter(RetailAdapter):
    key = SITE
    base_url = BASE_URL
    search_url_tmpl = SEARCH_URL_TMPL
    license_note = LICENSE_NOTE
    login_wall_markers = _LOGIN_WALL_MARKERS
    challenge_markers = _CHALLENGE_MARKERS

    def parse_search(self, content: str) -> list[dict]:
        """Rock West ships no JSON-LD on its Demandware SRP; parse the server
        -rendered ``product-tile`` cards directly. The SKU span follows the
        ``pdp-link`` block, so slice each card up to the next ``pdp-link`` to keep a
        card's P/N from leaking onto the previous tile."""
        hits: list[dict] = []
        seen: set[str] = set()
        cards = list(_CARD_RE.finditer(content))
        for i, m in enumerate(cards):
            href = m.group("href")
            url = href if href.startswith("http") else BASE_URL + href
            if url in seen:
                continue
            seen.add(url)
            end = cards[i + 1].start() if i + 1 < len(cards) else len(content)
            sku_m = _SKU_RE.search(content, m.end(), end)
            hit = {
                "ok": True,
                "title": _strip_tags(m.group("title")),
                "url": url,
                "site": SITE,
            }
            if sku_m:
                hit["sku"] = sku_m.group("sku").strip()
            hits.append(hit)
        return hits


ADAPTER = register(RockWestAdapter())
