"""services/browser/adapters/fibreglast.py — Fibre Glast source adapter (Parts Library V2).

Composites/fiberglass vendor on Shopify with an open storefront, plus public
PDS + SDS PDFs: ``acquire`` uses the default machining/spec-table param
extraction plus datasheet discovery, and copies only a small price/stock-spec
record with provenance. Challenges are reported as blocked or captcha.

Live-verified 2026-07-10: the Shopify ``/search?q=`` endpoint is right, but the
results page ships no JSON-LD — products are Shopify Dawn cards whose
``full-unstyled-link`` anchor holds the ``/products/<handle>`` URL + title. So this
adapter overrides :meth:`parse_search` to read those cards. (The page's Shopify
storefront-forms captcha library is passive form protection, not a wall — see
services/browser/challenge.py.)
"""
from __future__ import annotations

import re
from html import unescape
from urllib.parse import urlparse

from . import abs_url, register
from ._retail import RetailAdapter, _strip_tags, card_blocks, card_image

SITE = "fibreglast"
BASE_URL = "https://www.fibreglast.com"
SEARCH_URL_TMPL = BASE_URL + "/search?q={query}"
LICENSE_NOTE = (
    "Fibre Glast product data: reference-only, link-out commerce data. This "
    "adapter never stores or redistributes Fibre Glast's page content — only a "
    "small price/stock-spec record copied with provenance (source URL + fetch "
    "timestamp) for the user's own sourcing decision, never LLM-generated. "
    "See contracts/license_debt.md."
)

_LOGIN_WALL_MARKERS = ('id="login-email"',)
_CHALLENGE_MARKERS = ("Access Denied", "Pardon Our Interruption")

# Shopify Dawn product card: a `full-unstyled-link` anchor whose href is the
# `/products/<handle>` URL (with tracking query) and whose text is the product title.
_CARD_RE = re.compile(
    r'<a\b(?=[^>]*\bclass=["\'][^"\']*\bfull-unstyled-link\b[^"\']*["\'])'
    r'(?=[^>]*\bhref=["\'](?P<href>[^"\']+)["\'])[^>]*>(?P<title>.*?)</a>',
    re.IGNORECASE | re.DOTALL)


class FibreGlastAdapter(RetailAdapter):
    key = SITE
    hosts = ("www.fibreglast.com", "fibreglast.com")
    result_kinds = ("product",)
    cad_acquisition = False
    login_required = False
    api = False
    base_url = BASE_URL
    search_url_tmpl = SEARCH_URL_TMPL
    license_note = LICENSE_NOTE
    login_wall_markers = _LOGIN_WALL_MARKERS
    challenge_markers = _CHALLENGE_MARKERS

    def parse_search(self, content: str) -> list[dict]:
        hits: list[dict] = []
        seen: set[str] = set()
        blocks = card_blocks(content, "div", "product-card-wrapper")
        if not blocks:
            # Anchor-only snippets contain no reliable card-level image or price.
            blocks = [match.group(0) for match in _CARD_RE.finditer(content)]
        for block in blocks:
            match = _CARD_RE.search(block)
            if not match:
                continue
            href = unescape(match.group("href")).split("?", 1)[0]
            url = abs_url(BASE_URL, href)
            title = unescape(_strip_tags(match.group("title")))
            if not url or not urlparse(url).path.startswith("/products/") or not title or url in seen:
                continue
            seen.add(url)
            media = card_blocks(block, "div", "card__media")
            hit = {
                "ok": True, "site": SITE, "title": title, "url": url,
                "thumbnail": card_image(media[0], BASE_URL) if media else None,
            }
            # Shopify's sale amount supersedes its displayed regular price.
            prices = (card_blocks(block, "span", "price-item--sale") or
                      card_blocks(block, "span", "price-item--regular"))
            if prices:
                value = unescape(_strip_tags(prices[0])).strip()
                exact = re.fullmatch(r"(\$\s*[\d,]+(?:\.\d{2})?)(?:\s+USD)?", value, re.I)
                if exact:
                    hit.update(price=exact.group(1), currency="USD")
            badges = card_blocks(block, "span", "badge")
            if any(unescape(_strip_tags(badge)).strip().lower() == "sold out" for badge in badges):
                hit["stock"] = 0
            hits.append(hit)
        return hits


ADAPTER = register(FibreGlastAdapter())
