"""services/browser/adapters/meanwell.py — MEAN WELL source adapter (Parts Library V2).

MEAN WELL (meanwell.com): switching power supplies, DC-DC converters, and LED
drivers. Each model page carries a datasheet PDF, so ``acquire`` uses the
inherited default ``extract_params`` — :func:`~._retail.find_datasheet_url`
grabs the datasheet/spec PDF. Challenge/login pages degrade to
``login_required`` — never solved.

Live-verified 2026-07-10: the real search is
``/productSearch.aspx?pkeywords=<q>`` (HTTP 200; the old ``/search?q=`` guess
404'd). This is a MODEL/SERIES-indexed search — free-text ("power supply") maps
to nothing, but a MEAN WELL series token ("LRS", "RSP-320", "RS-25") returns a
server-rendered results table (confirmed rendered into ``#productSearch`` after
JS settle). Each row is a series/model whose ``<a href="/webapp/product/
search.aspx?prod=<MODEL>">`` PDF-browse anchor carries the model, echoed in the
row's model cell. There is no per-model price on this page, so hits are emitted
as ``kind="series"`` (link-out to the series/spec page). This adapter overrides
:meth:`parse_search` to read those rows (relative hrefs → joined onto base_url).
The catalog is public; the header "Sign In"/"Log In" widget is a false wall
marker and is dropped.
"""
from __future__ import annotations

import re
from urllib.parse import urljoin

from . import register
from ._retail import RetailAdapter

SITE = "meanwell"
BASE_URL = "https://www.meanwell.com"
SEARCH_URL_TMPL = BASE_URL + "/productSearch.aspx?pkeywords={query}"
LICENSE_NOTE = (
    "MEAN WELL product data: reference-only, link-out commerce data. This "
    "adapter never stores or redistributes MEAN WELL's page content — only a "
    "small price/spec record copied with provenance (source URL + fetch "
    "timestamp) for the user's own sourcing decision, never LLM-generated. "
    "Datasheets remain the manufacturer's documents fetched for the user's own "
    "engineering reference. See contracts/license_debt.md."
)

# The public catalog needs no login; the header "Sign In"/"Log In" widget rides
# every page, so it is NOT a wall marker here.
_LOGIN_WALL_MARKERS: tuple[str, ...] = ()
_CHALLENGE_MARKERS = ("Access Denied", "Pardon Our Interruption")

# Each result row's PDF-browse anchor carries the series/model in `prod=<MODEL>`.
_CARD_RE = re.compile(
    r'<a href="(?P<href>/webapp/product/search\.aspx\?prod=(?P<model>[^"&]+))"',
    re.IGNORECASE)


class MeanWellAdapter(RetailAdapter):
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
            model = m.group("model").strip()
            if not model or model in seen:
                continue
            seen.add(model)
            hits.append({
                "ok": True,
                "title": model,
                "url": urljoin(self.base_url, m.group("href")),
                "site": SITE,
                "part_number": model,
                "kind": "series",
            })
        return hits


ADAPTER = register(MeanWellAdapter())
