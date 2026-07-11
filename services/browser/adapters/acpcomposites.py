"""services/browser/adapters/acpcomposites.py — ACP Composites source adapter (Parts Library V2).

Composites vendor on WooCommerce with an open storefront, live prices and a
mechanical-properties PDF: ``acquire`` uses the default machining/spec-table
param extraction plus datasheet discovery, and copies only a small
price/stock-spec record with provenance. Challenge pages degrade to
``login_required`` — never solved.

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

from . import register
from ._retail import RetailAdapter, _strip_tags

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

# FacetWP results grid: one hit per `acp-search-listing-content` block, whose
# `<h3><a href="/shop/<slug>">Title</a></h3>` gives the product URL + title.
_CARD_RE = re.compile(
    r'<div class="acp-search-listing-content">.*?'
    r'<h3><a href="(?P<href>https?://[^"]*/shop/[^"]+)">(?P<title>[^<]+)</a>',
    re.IGNORECASE | re.DOTALL)


class AcpCompositesAdapter(RetailAdapter):
    key = SITE
    base_url = BASE_URL
    search_url_tmpl = SEARCH_URL_TMPL
    license_note = LICENSE_NOTE
    login_wall_markers = _LOGIN_WALL_MARKERS
    challenge_markers = _CHALLENGE_MARKERS

    def parse_search(self, content: str) -> list[dict]:
        hits: list[dict] = []
        seen: set[str] = set()
        for m in _CARD_RE.finditer(content):
            url = m.group("href")
            if url in seen:
                continue
            seen.add(url)
            title = _strip_tags(m.group("title"))
            if not title:
                continue
            hits.append({"ok": True, "title": title, "url": url, "site": SITE})
        return hits


ADAPTER = register(AcpCompositesAdapter())
