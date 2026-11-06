"""services/browser/adapters/coremarkmetals.py — CoreMark Metals source adapter (Parts Library V2).

Search combines JSON-LD products with the site's item-family and category
cards.  The public catalog has a Sign In link in its header; it is not a wall.
Stock dimensions are derived from the selected result's title and spec table.
"""
from __future__ import annotations

import re
from html import unescape

from . import abs_url, register
from ._retail import RetailAdapter, _strip_tags, card_image, parse_stock_params, stock_material

SITE = "coremarkmetals"
BASE_URL = "https://www.coremarkmetals.com"
SEARCH_URL_TMPL = BASE_URL + "/search/index.cfm?string={query}"
LICENSE_NOTE = (
    "CoreMark Metals product data: reference-only, link-out commerce data. This "
    "adapter never stores or redistributes CoreMark Metals' page content — only "
    "a small price/stock-spec record copied with provenance (source URL + fetch "
    "timestamp) for the user's own sourcing decision, never LLM-generated. "
    "See contracts/license_debt.md."
)

_LOGIN_WALL_MARKERS = ('id="signin_password"', 'id="login-email"')
_CHALLENGE_MARKERS = (
    "Access Denied", "Reference #", "Pardon Our Interruption",
    "cf-browser-verification",
)

# Search cards are scoped to the two results tabs, not the similar navigation cards.
_TAB_RE = re.compile(r'<div\b[^>]*\bid=["\']nav-(home|profile)["\'][^>]*>',
                     re.IGNORECASE)
_ZOOM_RE = re.compile(
    r'<a\b(?=[^>]*\bclass=["\'][^"\']*\bzoom\b[^"\']*["\'])'
    r'(?=[^>]*\bhref=["\'](?P<href>[^"\']+)["\'])[^>]*>(?P<body>.*?)</a>',
    re.IGNORECASE | re.DOTALL)
_TITLE_RE = re.compile(r'<h2\b[^>]*>(.*?)</h2>', re.IGNORECASE | re.DOTALL)


class CoreMarkMetalsAdapter(RetailAdapter):
    key = SITE
    base_url = BASE_URL
    search_url_tmpl = SEARCH_URL_TMPL
    license_note = LICENSE_NOTE
    login_wall_markers = _LOGIN_WALL_MARKERS
    challenge_markers = _CHALLENGE_MARKERS
    result_kinds = ("product", "series", "category")

    def is_no_results(self, content: str) -> bool:
        return (super().is_no_results(content) or
                ("No Item results" in content and "No Category results" in content))

    def parse_search(self, content: str) -> list[dict]:
        hits = super().parse_search(content)
        for hit in hits:
            hit["material"] = hit.get("material") or stock_material(hit["title"])

        tabs = list(_TAB_RE.finditer(content))
        seen = {abs_url(BASE_URL, hit["url"]) for hit in hits}
        for index, tab in enumerate(tabs):
            end = tabs[index + 1].start() if index + 1 < len(tabs) else len(content)
            section = content[tab.end():end].split("<footer", 1)[0]
            kind = "series" if tab.group(1).lower() == "home" else "category"
            for match in _ZOOM_RE.finditer(section):
                body = match.group("body")
                title_match = _TITLE_RE.search(body)
                url = abs_url(BASE_URL, unescape(match.group("href")))
                if not title_match or not url or url in seen:
                    continue
                title = unescape(_strip_tags(title_match.group(1)))
                if not title:
                    continue
                seen.add(url)
                hits.append({"ok": True, "title": title, "url": url, "kind": kind,
                             "material": stock_material(title),
                             "thumbnail": card_image(body, BASE_URL)})
        return hits

    def extract_params(self, result: dict, spec: dict, content: str) -> dict:
        title = result.get("title") or ""
        return parse_stock_params(title, spec)


ADAPTER = register(CoreMarkMetalsAdapter())
