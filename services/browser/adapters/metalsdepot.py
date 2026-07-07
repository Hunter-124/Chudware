"""services/browser/adapters/metalsdepot.py — MetalsDepot source adapter (Parts Library V2).

Stock-metal vendor with simple, older markup: JSON-LD is tried first, then a
plain product-anchor regex fallback for the legacy list pages. Like
onlinemetals, ``acquire`` derives stock params (alloy, profile, dimensions)
from the title + spec table. Challenge pages degrade to ``login_required`` —
never solved.
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

# Legacy list-page markup: plain anchors with a product-ish class.
_RESULT_RE = re.compile(
    r'<a\b(?=[^>]*class="[^"]*product[^"]*")[^>]*href="(?P<href>[^"]+)"[^>]*>'
    r"(?P<title>.*?)</a>",
    re.IGNORECASE | re.DOTALL,
)
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
        for m in _RESULT_RE.finditer(content or ""):
            raw = m.group("title")
            title = _strip_tags(raw)
            if not title:
                continue
            hit = {"ok": True, "title": title,
                   "url": urljoin(BASE_URL, m.group("href")), "site": SITE}
            price_match = _PRICE_RE.search(raw)
            if price_match:
                hit["price"] = "$" + price_match.group(1)
            hits.append(hit)
        return hits

    def extract_params(self, result: dict, spec: dict, content: str) -> dict:
        title = result.get("title") or ""
        return parse_stock_params(title, spec)


ADAPTER = register(MetalsDepotAdapter())
