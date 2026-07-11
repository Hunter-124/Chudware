"""services/browser/adapters/applerubber.py — Apple Rubber source adapter (Parts Library V2).

Apple Rubber (applerubber.com): O-rings, seals, and custom rubber components.
Product pages carry spec/size charts plus datasheets, so ``acquire`` uses the
inherited default ``extract_params`` — :func:`~._retail.find_datasheet_url`
grabs the datasheet/spec PDF.

Live-verified 2026-07-10: Apple Rubber is a ColdFusion site; the sanctioned
site search is ``/search.cfm?keyword=`` (HTTP 200). The old RetailAdapter default
``/search?q=`` guess 404'd (the 404 handler served the homepage, so nothing
parsed). The results page ships NO JSON-LD Product records — it is a
category/content search: an ``<ol id="SearchResults">`` list of ``<li><a
href=...>Title</a><p>snippet</p></li>`` rows linking to product-category pages,
size-search tools and design-guide pages (Apple Rubber sells configured/custom
seals, not per-SKU line items). So this adapter overrides :meth:`parse_search`
to read that list and emits each row as an honest ``kind="page"`` hit (a
navigable catalog/reference page, not a purchasable SKU) — mirroring the
category-navigation shape of the McMaster/DigiKey adapters. The public catalog
carries no sign-in gate, so the speculative ``Sign In``/``name=password`` wall
markers are dropped (they were never present and would only risk a false wall).
"""
from __future__ import annotations

import html
import re

from . import register
from ._retail import RetailAdapter

SITE = "applerubber"
BASE_URL = "https://www.applerubber.com"
SEARCH_URL_TMPL = BASE_URL + "/search.cfm?keyword={query}"
LICENSE_NOTE = (
    "Apple Rubber product data: reference-only, link-out commerce data. This "
    "adapter never stores or redistributes Apple Rubber's page content — only a "
    "small price/spec record copied with provenance (source URL + fetch "
    "timestamp) for the user's own sourcing decision, never LLM-generated. "
    "Datasheets remain the manufacturer's documents fetched for the user's own "
    "engineering reference. See contracts/license_debt.md."
)

# Public catalog — no sign-in gate rides these pages, so no login-wall markers.
_LOGIN_WALL_MARKERS: tuple[str, ...] = ()
_CHALLENGE_MARKERS = ("Access Denied", "Reference #", "Pardon Our Interruption")

# A site-search result row: <li ...><a href="...">Title</a><p>snippet</p></li>
# inside <ol id="SearchResults">.
_RESULTS_BLOCK_RE = re.compile(
    r'<ol\b[^>]*\bid="SearchResults"[^>]*>(?P<body>.*?)</ol>', re.IGNORECASE | re.DOTALL)
_ROW_RE = re.compile(
    r'<li\b[^>]*>\s*<a\b[^>]*\bhref="(?P<href>[^"]+)"[^>]*>(?P<title>.*?)</a>'
    r'(?:\s*<p>(?P<desc>.*?)</p>)?',
    re.IGNORECASE | re.DOTALL)


def _strip_tags(text: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", text)).strip()


class AppleRubberAdapter(RetailAdapter):
    key = SITE
    base_url = BASE_URL
    search_url_tmpl = SEARCH_URL_TMPL
    license_note = LICENSE_NOTE
    login_wall_markers = _LOGIN_WALL_MARKERS
    challenge_markers = _CHALLENGE_MARKERS

    def parse_search(self, content: str) -> list[dict]:
        block = _RESULTS_BLOCK_RE.search(content)
        if not block:
            return []
        hits: list[dict] = []
        seen: set[str] = set()
        for m in _ROW_RE.finditer(block.group("body")):
            href = m.group("href").strip()
            title = _strip_tags(m.group("title"))
            if not href or not title or href in seen:
                continue
            seen.add(href)
            url = href if href.startswith("http") else BASE_URL + href
            hit = {
                "ok": True,
                "title": title,
                "url": url,
                "site": SITE,
                "kind": "page",  # navigable catalog/reference page, not a purchasable SKU
            }
            desc = m.group("desc")
            if desc:
                desc = _strip_tags(desc)
                if desc:
                    hit["description"] = desc
            hits.append(hit)
        return hits


ADAPTER = register(AppleRubberAdapter())
