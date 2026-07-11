"""services/browser/adapters/fibreglast.py — Fibre Glast source adapter (Parts Library V2).

Composites/fiberglass vendor on Shopify with an open storefront, plus public
PDS + SDS PDFs: ``acquire`` uses the default machining/spec-table param
extraction plus datasheet discovery, and copies only a small price/stock-spec
record with provenance. Challenge pages degrade to ``login_required``.

Live-verified 2026-07-10: the Shopify ``/search?q=`` endpoint is right, but the
results page ships no JSON-LD — products are Shopify Dawn cards whose
``full-unstyled-link`` anchor holds the ``/products/<handle>`` URL + title. So this
adapter overrides :meth:`parse_search` to read those cards. (The page's Shopify
storefront-forms captcha library is passive form protection, not a wall — see
services/browser/challenge.py.)
"""
from __future__ import annotations

import re

from . import register
from ._retail import RetailAdapter, _strip_tags

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
    r'<a\b[^>]*\bhref="(?P<href>/products/[^"]+)"[^>]*\bclass="[^"]*full-unstyled-link[^"]*"'
    r'[^>]*>(?P<title>.*?)</a>',
    re.IGNORECASE | re.DOTALL)


class FibreGlastAdapter(RetailAdapter):
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
            url = BASE_URL + m.group("href").split("?", 1)[0]  # drop Shopify tracking query
            if url in seen:
                continue
            seen.add(url)
            title = _strip_tags(m.group("title"))
            if not title:
                continue
            hits.append({"ok": True, "title": title, "url": url, "site": SITE})
        return hits


ADAPTER = register(FibreGlastAdapter())
