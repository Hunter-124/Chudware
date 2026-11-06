"""services/browser/adapters/speedymetals.py — SpeedyMetals source adapter (Parts Library V2).

Stock-metal vendor with an open storefront and public per-length pricing:
titles are richly structured (``6061-T6 Aluminum Round Tube 1" OD x 0.049"
wall``), so ``acquire`` derives canonical stock params
(:func:`~._retail.parse_stock_params`) — alloy, profile, od_mm/wall_mm/
thickness_mm — from the title plus any spec table, instead of machining
params. Search rows do not necessarily carry a price or quantity.

The public search uses ``/search.aspx?SearchTerm=<q>`` and historically
rendered product rows in a table. If it instead returns its blank search form
without processing the supplied query, free-text search is unavailable; report
that explicitly rather than pretending the query had no matches.
The catalog is browsable without login; the header sign-in widget is not a wall.
"""
from __future__ import annotations

import re
from html import unescape

from . import abs_url, fail, register
from ._retail import RetailAdapter, _strip_tags, card_image, parse_stock_params

SITE = "speedymetals"
BASE_URL = "https://www.speedymetals.com"
SEARCH_URL_TMPL = BASE_URL + "/search.aspx?SearchTerm={query}"
LICENSE_NOTE = (
    "SpeedyMetals product data: reference-only, link-out commerce data. This "
    "adapter never stores or redistributes SpeedyMetals' page content — only a "
    "small price/stock-spec record copied with provenance (source URL + fetch "
    "timestamp) for the user's own sourcing decision, never LLM-generated. "
    "See contracts/license_debt.md."
)

# The public catalog needs no login; the header "Sign In" widget rides every
# page, so it is NOT a wall marker here.
_LOGIN_WALL_MARKERS: tuple[str, ...] = ()
_CHALLENGE_MARKERS = ("Access Denied", "Pardon Our Interruption")

# Results table row: title anchor + Material cell + Shape cell.
_ROW_RE = re.compile(r'<tr\b[^>]*>(?P<row>.*?)</tr>', re.IGNORECASE | re.DOTALL)
_CELL_RE = re.compile(r'<td\b(?P<attrs>[^>]*)>(?P<body>.*?)</td>', re.IGNORECASE | re.DOTALL)
_PART_RE = re.compile(
    r'<a\b(?=[^>]*\bhref=["\'](?P<href>(?:https?://[^/]+/|/)?'
    r'p-\d+[^"\']*?\.aspx(?:\?[^"\']*)?)["\'])[^>]*>(?P<title>.*?)</a>',
    re.IGNORECASE | re.DOTALL)
_PRICE_RE = re.compile(r'^\s*\$\s*([0-9][0-9,]*(?:\.[0-9]{2})?)\s*$')
_STOCK_CLASS_RE = re.compile(r'\bclass=["\'][^"\']*\bstock\b[^"\']*["\']', re.IGNORECASE)
_BLANK_SEARCH_FORM = re.compile(
    r'''<form\b(?=[^>]*\bname=["']SearchForm2["'])[^>]*>.*?'''
    r'''<input\b(?=[^>]*\bname=["']SearchTerm["'])(?=[^>]*\bvalue=["']["'])[^>]*>''',
    re.IGNORECASE | re.DOTALL,
)


class SpeedyMetalsAdapter(RetailAdapter):
    key = SITE
    base_url = BASE_URL
    search_url_tmpl = SEARCH_URL_TMPL
    license_note = LICENSE_NOTE
    login_wall_markers = _LOGIN_WALL_MARKERS
    challenge_markers = _CHALLENGE_MARKERS

    def parse_search_page(self, content: str, final_url: str) -> list[dict]:
        hits = self.parse_search(content)
        if not hits and not self.is_no_results(content) and _BLANK_SEARCH_FORM.search(content):
            return [fail(self.key, "search", "unsupported",
                         "Speedy Metals returned its blank search form instead of processing the search query.",
                         url=final_url)]
        return hits

    def parse_search(self, content: str) -> list[dict]:
        hits: list[dict] = []
        seen: set[str] = set()
        for row_match in _ROW_RE.finditer(content or ""):
            row = row_match.group("row")
            cells = list(_CELL_RE.finditer(row))
            for index, cell in enumerate(cells):
                part = _PART_RE.search(cell.group("body"))
                if part:
                    break
            else:
                continue
            if len(cells) <= index + 2:
                continue
            url = abs_url(BASE_URL + "/", unescape(part.group("href")))
            title = unescape(_strip_tags(part.group("title")))
            if not url or not title or url in seen:
                continue
            seen.add(url)
            material = unescape(_strip_tags(cells[index + 1].group("body")))
            shape = unescape(_strip_tags(cells[index + 2].group("body")))
            hit = {"ok": True, "title": title, "url": url, "material": material or None,
                   "thumbnail": card_image(row, BASE_URL)}
            if material or shape:
                hit["category"] = " / ".join(x for x in (material, shape) if x)
            for cell in cells[index + 3:]:
                value = unescape(_strip_tags(cell.group("body")))
                price = _PRICE_RE.fullmatch(value)
                if price:
                    hit["price"] = "$" + price.group(1)
                elif _STOCK_CLASS_RE.search(cell.group("attrs")) and value.isdigit():
                    hit["stock"] = int(value)
            hits.append(hit)
        return hits

    def extract_params(self, result: dict, spec: dict, content: str) -> dict:
        title = result.get("title") or ""
        return parse_stock_params(title, spec)


ADAPTER = register(SpeedyMetalsAdapter())
