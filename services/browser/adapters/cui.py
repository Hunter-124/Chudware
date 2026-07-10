"""services/browser/adapters/cui.py — CUI Devices source adapter (Parts Library V2).

CUI Devices (cuidevices.com): power supplies, DC-DC converters, connectors,
and encoders. Each part page carries a datasheet PDF, so ``acquire`` uses the
inherited default ``extract_params`` — :func:`~._retail.find_datasheet_url`
grabs the datasheet/spec PDF. Challenge pages degrade to ``login_required``.

Live-verified 2026-07-10: site search is ``/SiteSearch/SearchByPartialMatch?partnumber=``
(the old ``/search?q=`` guess 404'd). Results are server-rendered ``result-link
dl-productLink`` cards carrying the product URL (CUI's storefront is now
sameskydevices.com), the ``dl-itemnumber`` part number and ``dl-itemcategories``.
They ship no JSON-LD, so this adapter overrides :meth:`parse_search`. The header
"Sign In"/name=password widget rides every page, so those false wall markers are
dropped for this public catalog.
"""
from __future__ import annotations

import re

from . import register
from ._retail import RetailAdapter

SITE = "cui"
BASE_URL = "https://www.cuidevices.com"
SEARCH_URL_TMPL = BASE_URL + "/SiteSearch/SearchByPartialMatch?partnumber={query}"
LICENSE_NOTE = (
    "CUI Devices product data: reference-only, link-out commerce data. This "
    "adapter never stores or redistributes CUI Devices' page content — only a "
    "small price/spec record copied with provenance (source URL + fetch "
    "timestamp) for the user's own sourcing decision, never LLM-generated. "
    "Datasheets remain the manufacturer's documents fetched for the user's own "
    "engineering reference. See contracts/license_debt.md."
)

# Public catalog — the header "Sign In"/name=password widget is NOT a wall marker.
_LOGIN_WALL_MARKERS: tuple[str, ...] = ()
_CHALLENGE_MARKERS = ("Access Denied", "Pardon Our Interruption")

# A site-search result card: an `<a>` with class `dl-productLink` carrying the
# product URL, the `dl-itemnumber` part number and `dl-itemcategories` breadcrumb.
_CARD_RE = re.compile(
    r'<a\b[^>]*\bhref="(?P<href>[^"]+)"[^>]*\bclass="[^"]*\bdl-productLink\b[^"]*"'
    r'[^>]*\bdl-itemnumber="(?P<pn>[^"]+)"'
    r'(?:[^>]*\bdl-itemcategories="(?P<cats>[^"]*)")?',
    re.IGNORECASE)


class CuiAdapter(RetailAdapter):
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
            pn = m.group("pn").strip()
            hit = {"ok": True, "title": pn, "url": url, "site": SITE, "part_number": pn}
            cats = m.group("cats")
            if cats:
                hit["category"] = cats.replace(",", " / ")
            hits.append(hit)
        return hits


ADAPTER = register(CuiAdapter())
