"""services/browser/adapters/speedymetals.py — SpeedyMetals source adapter (Parts Library V2).

Stock-metal vendor with an open storefront and public per-length pricing:
titles are richly structured (``6061-T6 Aluminum Round Tube 1" OD x 0.049"
wall``), so ``acquire`` derives canonical stock params
(:func:`~._retail.parse_stock_params`) — alloy, profile, od_mm/wall_mm/
thickness_mm — from the title plus any spec table, instead of machining
params. Challenge pages degrade to ``login_required`` — never solved.

Live-verified 2026-07-10: the real ASP.NET search is
``/search.aspx?SearchTerm=<q>`` (HTTP 200; the old ``/search?q=`` guess 404'd).
Results are a server-rendered table — NOT JSON-LD Products, so the default parse
found nothing. Each row is ``<td><a href="p-<id>.aspx">Title</a></td>`` followed
by Material and Shape cells; this adapter overrides :meth:`parse_search` to read
them (hrefs are relative → joined onto ``base_url``). The catalog is browsable
with no login; the header "Sign In" widget is a false wall marker and is dropped.
"""
from __future__ import annotations

import re
from urllib.parse import urljoin

from . import register
from ._retail import RetailAdapter, _strip_tags, parse_stock_params

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
_ROW_RE = re.compile(
    r'<a href="(?P<href>p-\d+[^"]*\.aspx)">(?P<title>[^<]+)</a>\s*</td>\s*'
    r'<td[^>]*>(?P<material>[^<]*?)</td>\s*<td[^>]*>(?P<shape>[^<]*?)</td>',
    re.IGNORECASE | re.DOTALL)


class SpeedyMetalsAdapter(RetailAdapter):
    key = SITE
    base_url = BASE_URL
    search_url_tmpl = SEARCH_URL_TMPL
    license_note = LICENSE_NOTE
    login_wall_markers = _LOGIN_WALL_MARKERS
    challenge_markers = _CHALLENGE_MARKERS

    def parse_search(self, content: str) -> list[dict]:
        hits: list[dict] = []
        seen: set[str] = set()
        for m in _ROW_RE.finditer(content):
            url = urljoin(self.base_url + "/", m.group("href"))
            if url in seen:
                continue
            seen.add(url)
            title = _strip_tags(m.group("title"))
            if not title:
                continue
            hit = {"ok": True, "title": title, "url": url, "site": SITE}
            material = _strip_tags(m.group("material"))
            shape = _strip_tags(m.group("shape"))
            if material or shape:
                hit["category"] = " / ".join(x for x in (material, shape) if x)
            hits.append(hit)
        return hits

    def extract_params(self, result: dict, spec: dict, content: str) -> dict:
        title = result.get("title") or ""
        return parse_stock_params(title, spec)


ADAPTER = register(SpeedyMetalsAdapter())
