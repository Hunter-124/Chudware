"""services/browser/adapters/fastenal.py — Fastenal source adapter (Parts Library V2).

Fastener-focused MRO catalog: product pages carry a spec table (thread size,
grade, finish, ...) which ``acquire`` surfaces verbatim under ``spec`` plus
canonical machining params where the table names them.

Search parse strategy (verified against real live markup 2026-07-07,
``https://www.fastenal.com/product?query=m3+screw``, HTTP 200): the search
results page ships **NO JSON-LD** — it is server-rendered HTML. Each product is
a ``<div class="card ... list-item-detail">`` card carrying a hidden field
``<input type="hidden" name="data-sku" value="...">`` (SKU may be numeric like
``924835414`` or alphanumeric like ``M42510040A20000``), a
``<div class="product-description-grid">`` title inside a
``href="/product/details/<sku>"`` anchor, and — for anonymous fetches — an
``Online Price`` block (``<p>Online Price</p><p>...<span>$19.83</span>...``).
Price WAS present for the anonymous fetch (all 12 cards had it), so we emit it;
we do not synthesize it when a card lacks one. Challenge/login-wall pages
degrade to ``login_required`` — never solved.
"""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import urljoin

from . import register
from ._retail import RetailAdapter, parse_jsonld_products

SITE = "fastenal"
BASE_URL = "https://www.fastenal.com"
SEARCH_URL_TMPL = BASE_URL + "/product?query={query}"
LICENSE_NOTE = (
    "Fastenal product data: reference-only, link-out commerce data. This "
    "adapter never stores or redistributes Fastenal's page content — only a "
    "small price/spec record copied with provenance (source URL + fetch "
    "timestamp) for the user's own sourcing decision, never LLM-generated. "
    "See contracts/license_debt.md."
)

_LOGIN_WALL_MARKERS = ('id="username"', "Log in to see your pricing")
_CHALLENGE_MARKERS = ("Access Denied", "Reference #", "Request unsuccessful")

# One product card carries a hidden SKU input; we split the results HTML on
# these boundaries so each card's title/href/price stay associated.
_SKU_RE = re.compile(r'name="data-sku"\s+value="([^"]+)"')
_TITLE_RE = re.compile(r'class="product-description-grid">([^<]+)</div>')
_HREF_RE = re.compile(r'href="(/product/details/[^"]+)"')
_PRICE_RE = re.compile(
    r'Online Price</p><p class="mb-0"><span[^>]*>(\$[\d.,]+)</span>')


class FastenalAdapter(RetailAdapter):
    key = SITE
    base_url = BASE_URL
    search_url_tmpl = SEARCH_URL_TMPL
    license_note = LICENSE_NOTE
    login_wall_markers = _LOGIN_WALL_MARKERS
    challenge_markers = _CHALLENGE_MARKERS

    def parse_search(self, content: str) -> list[dict]:
        """Parse the server-rendered product cards (no JSON-LD on this page).

        Returns hits in the base ``_hit_from_product`` shape:
        ``{ok, title, url, site, sku, price?}``. Price is emitted only when the
        card actually shows an anonymous ``Online Price`` — never invented.

        Falls back to JSON-LD Products if the server-rendered cards are absent,
        so any future page variant that ships the SEO layer still parses.
        """
        cards = self._hits_from_cards(content)
        if cards:
            return cards
        return [self._hit_from_product(p) for p in parse_jsonld_products(content)
                if p.get("title") and p.get("url")]

    def _hits_from_cards(self, content: str) -> list[dict]:
        text = content or ""
        boundaries = [(m.group(1), m.start()) for m in _SKU_RE.finditer(text)]
        hits: list[dict] = []
        for i, (sku, pos) in enumerate(boundaries):
            end = boundaries[i + 1][1] if i + 1 < len(boundaries) else len(text)
            seg = text[pos:end]
            tm = _TITLE_RE.search(seg)
            hm = _HREF_RE.search(seg)
            if not tm or not hm:
                continue
            hit: dict[str, Any] = {
                "ok": True,
                "title": tm.group(1).strip(),
                "url": urljoin(self.base_url, hm.group(1)),
                "site": self.key,
                "sku": sku,
            }
            pm = _PRICE_RE.search(seg)
            if pm:
                hit["price"] = pm.group(1)
            hits.append(hit)
        return hits


ADAPTER = register(FastenalAdapter())
