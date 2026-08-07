"""services/browser/adapters/metalsdepot.py — MetalsDepot source adapter (Parts Library V2).

Stock-metal vendor with a rendered product-grid search page. JSON-LD is tried
first, then the current ``product product-row`` card markup. As with
OnlineMetals, ``acquire`` derives stock params from the title.
"""
from __future__ import annotations

import re
from urllib.parse import urljoin

from . import register
from ._retail import RetailAdapter, _strip_tags, parse_jsonld_products, parse_stock_params

SITE = "metalsdepot"
BASE_URL = "https://www.metalsdepot.com"
SEARCH_URL_TMPL = BASE_URL + "/search?q={query}"
LICENSE_NOTE = (
    "MetalsDepot product data: reference-only, link-out commerce data. This "
    "adapter never stores or redistributes MetalsDepot's page content — only a "
    "small price/stock-spec record copied with provenance (source URL + fetch "
    "timestamp) for the user's own sourcing decision, never LLM-generated. "
    "See contracts/license_debt.md."
)

_LOGIN_WALL_MARKERS = ("Sign In", 'id="login-email"')
_CHALLENGE_MARKERS = ("Access Denied", "Reference #")

_CARD_RE = re.compile(
    r'<div\b(?=[^>]*class="[^"]*\bproduct\b)'
    r'(?=[^>]*class="[^"]*\bproduct-row\b)[^>]*>(?P<card>.*?)'
    r'(?=<div\b[^>]*class="[^"]*\brow\s+product-row\b|\Z)',
    re.IGNORECASE | re.DOTALL)
_LINK_RE = re.compile(
    r'<h4\b[^>]*>\s*<a[^>]+href="(?P<href>[^"]+)"[^>]*>'
    r'(?P<title>.*?)</a>\s*</h4>', re.IGNORECASE | re.DOTALL)
_PRICE_RE = re.compile(r"\$\s*([0-9][0-9,]*(?:\.[0-9]{2})?)")


class MetalsDepotAdapter(RetailAdapter):
    key = SITE
    base_url = BASE_URL
    search_url_tmpl = SEARCH_URL_TMPL
    license_note = LICENSE_NOTE
    login_wall_markers = _LOGIN_WALL_MARKERS
    challenge_markers = _CHALLENGE_MARKERS

    def parse_search(self, content: str) -> list[dict]:
        hits = [self._hit_from_product(p) for p in parse_jsonld_products(content)
                if p.get("title") and p.get("url")]
        if hits:
            return hits
        for match in _CARD_RE.finditer(content or ""):
            card = match.group("card")
            link = _LINK_RE.search(card)
            if link is None:
                continue
            title = re.sub(r"^(?:Copy\s+)+", "", _strip_tags(link.group("title")))
            if not title:
                continue
            hit = {"ok": True, "title": title,
                   "url": urljoin(BASE_URL, link.group("href")), "site": SITE}
            price_match = _PRICE_RE.search(card)
            if price_match:
                hit["price"] = "$" + price_match.group(1)
            hits.append(hit)
        return hits

    def extract_params(self, result: dict, spec: dict, content: str) -> dict:
        title = result.get("title") or ""
        return parse_stock_params(title, spec)


ADAPTER = register(MetalsDepotAdapter())
