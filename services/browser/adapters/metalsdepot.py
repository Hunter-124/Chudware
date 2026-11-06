"""services/browser/adapters/metalsdepot.py — MetalsDepot source adapter (Parts Library V2).

The vendor's terms prohibit bots and indexing agents. Automated search and
acquisition therefore return a policy refusal without contacting the site.
The card parser is retained for offline source-fixture coverage.
"""
from __future__ import annotations

import re
from html import unescape

from . import abs_url, fail, register
from ._retail import RetailAdapter, _strip_tags, card_image, parse_stock_params, stock_material

SITE = "metalsdepot"
BASE_URL = "https://www.metalsdepot.com"
SEARCH_URL_TMPL = BASE_URL + "/search?q={query}"
LICENSE_NOTE = (
    "Metals Depot's terms prohibit bots and indexing agents; automated search "
    "and acquisition are not available. See "
    "https://www.metalsdepot.com/terms-and-conditions."
)

_LOGIN_WALL_MARKERS = ('id="login-email"',)
_CHALLENGE_MARKERS = ("Access Denied", "Reference #")

_CARD_OPEN_RE = re.compile(r'<div\b[^>]*\bclass=["\'](?P<class>[^"\']*)["\'][^>]*>',
                           re.IGNORECASE)
_DIV_RE = re.compile(r'</?div\b[^>]*>', re.IGNORECASE)
_LINK_RE = re.compile(
    r'<h4\b[^>]*>\s*<a\b[^>]*href=["\'](?P<href>[^"\']+)["\'][^>]*>'
    r'(?P<title>.*?)</a>\s*</h4>', re.IGNORECASE | re.DOTALL)
_CATEGORY_RE = re.compile(
    r'<a\b[^>]*href=["\'](?P<href>[^"\']+)["\'][^>]*>'
    r'(?P<body>(?:(?!</a>).)*?\bcategory-search-result-box\b(?:(?!</a>).)*?)</a>',
    re.IGNORECASE | re.DOTALL)
_TITLE_RE = re.compile(r'<h2\b[^>]*>(.*?)</h2>', re.IGNORECASE | re.DOTALL)
_PRICE_RE = re.compile(r"\$\s*([0-9][0-9,]*(?:\.[0-9]{2})?)")


def _product_cards(content: str):
    """Yield each whole card without allowing its media or price to bleed into another."""
    for opening in _CARD_OPEN_RE.finditer(content):
        if not {"product", "product-row"} <= set(opening.group("class").split()):
            continue
        depth = 1
        for tag in _DIV_RE.finditer(content, opening.end()):
            depth += -1 if tag.group().startswith("</") else 1
            if depth == 0:
                yield content[opening.start():tag.end()]
                break


class MetalsDepotAdapter(RetailAdapter):
    key = SITE
    base_url = BASE_URL
    search_url_tmpl = SEARCH_URL_TMPL
    license_note = LICENSE_NOTE
    login_wall_markers = _LOGIN_WALL_MARKERS
    challenge_markers = _CHALLENGE_MARKERS
    result_kinds = ("product", "category")

    def search(self, query: str) -> list[dict]:
        return [fail(SITE, "search", "policy_refused", LICENSE_NOTE, url=self.search_url(query))]

    def acquire(self, result: dict) -> dict:
        return fail(SITE, "acquire", "policy_refused", LICENSE_NOTE, url=result.get("url"))

    def parse_search(self, content: str) -> list[dict]:
        hits = super().parse_search(content)
        for hit in hits:
            hit["material"] = hit.get("material") or stock_material(hit["title"])
        seen = {abs_url(BASE_URL, hit["url"]) for hit in hits}
        for card in _product_cards(content or ""):
            link = _LINK_RE.search(card)
            if link is None:
                continue
            title = re.sub(r"^(?:Copy\s+)+", "", unescape(_strip_tags(link.group("title"))))
            url = abs_url(BASE_URL, unescape(link.group("href")))
            if not title or not url or url in seen:
                continue
            seen.add(url)
            hit = {"ok": True, "title": title, "url": url, "material": stock_material(title),
                   "thumbnail": card_image(card, BASE_URL)}
            price_match = _PRICE_RE.search(card)
            if price_match:
                hit["price"] = "$" + price_match.group(1)
            hits.append(hit)
        for match in _CATEGORY_RE.finditer(content or ""):
            body = match.group("body")
            title_match = _TITLE_RE.search(body)
            url = abs_url(BASE_URL, unescape(match.group("href")))
            if not title_match or not url or url in seen:
                continue
            title = unescape(_strip_tags(title_match.group(1)))
            if not title:
                continue
            seen.add(url)
            hits.append({"ok": True, "title": title, "url": url, "kind": "category",
                         "material": stock_material(title), "thumbnail": card_image(body, BASE_URL)})
        return hits

    def extract_params(self, result: dict, spec: dict, content: str) -> dict:
        title = result.get("title") or ""
        return parse_stock_params(title, spec)


ADAPTER = register(MetalsDepotAdapter())
