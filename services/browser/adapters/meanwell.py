"""services/browser/adapters/meanwell.py — MEAN WELL source adapter (Parts Library V2).

MEAN WELL (meanwell.com): switching power supplies, DC-DC converters, and LED
drivers. Search returns series, not configured products. The inherited retail
acquisition can fetch a linked datasheet as a document, not CAD. Access
challenges are reported and never solved.

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
from html import unescape
from urllib.parse import unquote

from . import abs_url, fail, make_hit, register
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

# The public header has sign-in links; only explicit access-gate copy counts.
_LOGIN_WALL_MARKERS = ("Please sign in to continue", "You must be logged in")
_CHALLENGE_MARKERS = (
    "Access Denied", "Pardon Our Interruption", "Verify you are human",
    "verify you are human", "<h1>CAPTCHA</h1>", "Please complete the CAPTCHA",
)

# Each result row's PDF-browse anchor carries the series/model in `prod=<MODEL>`.
_CARD_RE = re.compile(
    r'''<a\b[^>]*\bhref=["'](?P<href>/webapp/product/search\.aspx\?prod=(?P<model>[^"'&]+))["']''',
    re.IGNORECASE)
_EMPTY_RESULTS_RE = re.compile(
    r'''<div\b[^>]*\bid=["']productSearch["'][^>]*>\s*
        <div\b[^>]*\bclass=["'][^"']*\bheader\b[^"']*["'][^>]*>\s*
        <h2\b[^>]*>\s*Search\s+Results\s*</h2>\s*</div>\s*</div>''',
    re.IGNORECASE | re.VERBOSE,
)


class MeanWellAdapter(RetailAdapter):
    key = SITE
    hosts = ("meanwell.com",)
    result_kinds = ("series",)
    cad_acquisition = False
    login_required = False
    api = False
    base_url = BASE_URL
    search_url_tmpl = SEARCH_URL_TMPL
    license_note = LICENSE_NOTE
    login_wall_markers = _LOGIN_WALL_MARKERS
    challenge_markers = _CHALLENGE_MARKERS

    def is_no_results(self, content: str) -> bool:
        # The live site renders just this header when there are no matches.
        # A table or any other content in the results container is not proof
        # of an empty search and must still be parsed.
        return bool(_EMPTY_RESULTS_RE.search(content)) or super().is_no_results(content)

    def search(self, query: str) -> list[dict]:
        hits = super().search(query)
        if not hits:
            return [fail(
                self.key, "search", "not_found",
                "No MEAN WELL series matched. Search by a series or model token "
                "(for example, LRS or RSP-320); free-text product descriptions may not match.",
                url=self.search_url(query),
            )]
        return hits

    def parse_search(self, content: str) -> list[dict]:
        hits: list[dict] = []
        seen: set[str] = set()
        for m in _CARD_RE.finditer(content):
            model = unquote(unescape(m.group("model"))).strip()
            url = abs_url(self.base_url, unescape(m.group("href")))
            if not model or not url or model in seen:
                continue
            seen.add(model)
            hits.append(make_hit(
                self.key, title=model, url=url, kind="series", manufacturer="MEAN WELL",
                extra={"ok": True},
            ))
        return hits


ADAPTER = register(MeanWellAdapter())
