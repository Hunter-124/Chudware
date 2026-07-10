"""services/browser/adapters/seeedstudio.py — Seeed Studio source adapter (Parts Library V2).

Seeed Studio (seeedstudio.com) is a maker / IoT hardware vendor. Much of its
catalog is open-source hardware, so CAD models are usually linked off-site
(GrabCAD) rather than hosted here; this adapter treats Seeed as a commerce +
datasheet source (price/spec record + wiki datasheet PDF), not a CAD-model
source. The default :class:`~._retail.RetailAdapter` flow applies: JSON-LD for
search, spec-table + ``find_datasheet_url`` grabbing the datasheet PDF on the
detail page. Open catalog — no login. Challenge pages degrade to
``login_required`` — never solved.

Live-verified 2026-07-10: the ``/catalogsearch/result/?q=`` endpoint is right, but
results are rendered by Algolia InstantSearch (``ais_hit_product_bar`` tiles), not
JSON-LD — so this adapter overrides :meth:`parse_search` to read the ``ais_p_title``
anchor (product URL + title) and ``ais_p_sku`` span. The header "Sign In" widget
rode every page and falsely tripped the login wall on this fully public catalog, so
those markers are dropped.
"""
from __future__ import annotations

import re

from . import register
from ._retail import RetailAdapter, _strip_tags

SITE = "seeedstudio"
BASE_URL = "https://www.seeedstudio.com"
SEARCH_URL_TMPL = BASE_URL + "/catalogsearch/result/?q={query}"
LICENSE_NOTE = (
    "Seeed Studio product data is reference-only link-out commerce data, and any "
    "datasheet remains the manufacturer's own document, fetched here for the "
    "user's own engineering reference only. This adapter never redistributes or "
    "LLM-generates page content — only a small price/spec record copied with "
    "provenance (source URL + fetch timestamp) and the manufacturer's datasheet "
    "saved for the user. See contracts/license_debt.md."
)

# Public catalog — the header "Sign In"/"Log In" widget is NOT a wall marker.
_LOGIN_WALL_MARKERS: tuple[str, ...] = ()
_CHALLENGE_MARKERS = ("Access Denied", "Pardon Our Interruption")

# Algolia InstantSearch hit: a `class="ais_p_title"` anchor carries the product URL
# + (highlight-wrapped) title; the sibling `ais_p_sku` div holds the SKU.
_CARD_RE = re.compile(
    r'<a\b[^>]*\bhref="(?P<href>[^"]+)"[^>]*\bclass="ais_p_title"[^>]*>(?P<title>.*?)</a>',
    re.IGNORECASE | re.DOTALL)
_SKU_RE = re.compile(
    r'ais_p_sku"[^>]*>\s*<span[^>]*>(?P<sku>[^<]+)', re.IGNORECASE | re.DOTALL)


class SeeedStudioAdapter(RetailAdapter):
    key = SITE
    base_url = BASE_URL
    search_url_tmpl = SEARCH_URL_TMPL
    license_note = LICENSE_NOTE
    login_wall_markers = _LOGIN_WALL_MARKERS
    challenge_markers = _CHALLENGE_MARKERS

    def parse_search(self, content: str) -> list[dict]:
        hits: list[dict] = []
        seen: set[str] = set()
        cards = list(_CARD_RE.finditer(content))
        for i, m in enumerate(cards):
            url = m.group("href")
            if url in seen:
                continue
            seen.add(url)
            end = cards[i + 1].start() if i + 1 < len(cards) else len(content)
            sku = _SKU_RE.search(content, m.end(), end)
            hit = {"ok": True, "title": _strip_tags(m.group("title")), "url": url, "site": SITE}
            if sku:
                hit["sku"] = sku.group("sku").strip()
            hits.append(hit)
        return hits


ADAPTER = register(SeeedStudioAdapter())
