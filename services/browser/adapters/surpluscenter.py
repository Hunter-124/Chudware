"""services/browser/adapters/surpluscenter.py — Surplus Center source adapter (Parts Library V2).

Discount hydraulics vendor (pumps/motors/cylinders) with an open storefront:
this is a COMMERCE source (price/stock plus thin specs), not a CAD-model
source. ``acquire`` uses the default machining/spec-table param extraction plus
datasheet discovery, and copies only a small price/stock-spec record with
provenance. Challenge pages degrade to ``login_required`` — never solved.

Live-verified 2026-07-10: the storefront runs on BigCommerce (Stencil), so search
is ``/search.php?search_query=`` (the old ``/search?q=`` guess 404'd) and results
carry no JSON-LD — they are server-rendered ``<article class="card">`` tiles whose
``<h4 class="card-title"><a>`` holds the product URL + title, ``data-product-id``
the SKU and a ``price--withoutTax`` span the price. So this adapter overrides
:meth:`parse_search` to read that markup directly.
"""
from __future__ import annotations

import re

from . import register
from ._retail import RetailAdapter, _strip_tags

SITE = "surpluscenter"
BASE_URL = "https://www.surpluscenter.com"
SEARCH_URL_TMPL = BASE_URL + "/search.php?search_query={query}"
LICENSE_NOTE = (
    "Surplus Center product data: reference-only, link-out commerce data. This "
    "adapter never stores or redistributes Surplus Center's page content — only "
    "a small price/stock-spec record copied with provenance (source URL + fetch "
    "timestamp) for the user's own sourcing decision, never LLM-generated. "
    "See contracts/license_debt.md."
)

_LOGIN_WALL_MARKERS = ('id="login-email"',)
_CHALLENGE_MARKERS = ("Access Denied", "Pardon Our Interruption")

# A BigCommerce Stencil search card: the title anchor inside `<h4 class="card-title">`
# carries the absolute product URL + title; the sibling quick-view button carries the
# numeric `data-product-id` (SKU); the price sits in a `price--withoutTax` span.
_CARD_RE = re.compile(
    r'<article\b[^>]*\bclass="card\b.*?'
    r'<h4 class="card-title">\s*<a href="(?P<href>[^"]+)"[^>]*>(?P<title>.*?)</a>',
    re.IGNORECASE | re.DOTALL)
_PID_RE = re.compile(r'data-product-id="(?P<pid>\d+)"', re.IGNORECASE)
_PRICE_RE = re.compile(
    r'<span[^>]*\bclass="price price--withoutTax[^"]*"[^>]*>\s*(?P<price>\$[\d,]+\.\d\d)',
    re.IGNORECASE)


class SurplusCenterAdapter(RetailAdapter):
    key = SITE
    base_url = BASE_URL
    search_url_tmpl = SEARCH_URL_TMPL
    license_note = LICENSE_NOTE
    login_wall_markers = _LOGIN_WALL_MARKERS
    challenge_markers = _CHALLENGE_MARKERS

    def parse_search(self, content: str) -> list[dict]:
        """BigCommerce ships no JSON-LD on its SRP; parse the server-rendered
        ``card`` tiles. Each card is sliced up to the next ``<article`` so a
        neighbour's product-id/price can't leak onto the previous tile."""
        hits: list[dict] = []
        seen: set[str] = set()
        cards = list(_CARD_RE.finditer(content))
        for i, m in enumerate(cards):
            href = m.group("href")
            url = href.split("?", 1)[0] if href.startswith("http") else BASE_URL + href
            if url in seen:
                continue
            seen.add(url)
            end = cards[i + 1].start() if i + 1 < len(cards) else len(content)
            block = content[m.start():end]
            hit = {
                "ok": True,
                "title": _strip_tags(m.group("title")),
                "url": url,
                "site": SITE,
            }
            pid = _PID_RE.search(block)
            if pid:
                hit["sku"] = pid.group("pid")
            price = _PRICE_RE.search(block)
            if price:
                hit["price"] = price.group("price")
            hits.append(hit)
        return hits


ADAPTER = register(SurplusCenterAdapter())
