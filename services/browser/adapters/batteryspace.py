"""services/browser/adapters/batteryspace.py — BatterySpace source adapter (Parts Library V2).

BatterySpace / AA Portable Power (batteryspace.com): battery cells, packs, and
chargers sold e-commerce with price/stock. ``acquire`` uses the inherited
default ``extract_params`` — :func:`~._retail.find_datasheet_url` grabs the
datasheet/spec PDF when present.

Live-verified 2026-07-10: the storefront runs on AmeriCommerce (.aspx). The old
``/search?q=`` guess 404'd — the real product search is
``/search.aspx?find=<query>`` (the header search form posts to ``search.aspx``
with the ``find`` param; a live ``search.aspx?find=CR123A`` link is baked into
the site chrome). Results carry NO JSON-LD — they are server-rendered
``product-list-item`` cards whose ``product-list-options`` block holds an
``<h5><a href>`` product URL + title, a ``product-list-cost-value`` price, and a
``product-list-text`` "Product ID: N" SKU — so this adapter overrides
:meth:`parse_search` to read that markup directly. The header "Sign In" and the
newsletter ``name="password"`` field are NOT a wall on this public catalog, so
the login markers are dropped (challenge markers stay for a real bot wall).
"""
from __future__ import annotations

import re

from . import register
from ._retail import RetailAdapter, _strip_tags

SITE = "batteryspace"
BASE_URL = "https://www.batteryspace.com"
SEARCH_URL_TMPL = BASE_URL + "/search.aspx?find={query}"
LICENSE_NOTE = (
    "BatterySpace product data: reference-only, link-out commerce data. This "
    "adapter never stores or redistributes BatterySpace's page content — only a "
    "small price/spec record copied with provenance (source URL + fetch "
    "timestamp) for the user's own sourcing decision, never LLM-generated. "
    "Datasheets remain the manufacturer's documents fetched for the user's own "
    "engineering reference. See contracts/license_debt.md."
)

# A header "Sign In" link and a newsletter password field appear on the PUBLIC
# product search — neither is a login wall. Drop the false markers; a genuine bot
# block still degrades via the challenge markers (Akamai/PX/captcha interstitials).
_LOGIN_WALL_MARKERS = ()
_CHALLENGE_MARKERS = ("Access Denied", "Reference #", "Pardon Our Interruption")

# One AmeriCommerce search-result card: the `product-list-options` block wraps an
# `<h5><a href="…">Title</a>` (product URL + name). The price lives in a sibling
# `product-list-cost-value` span; the SKU/product id in a `product-list-text`
# "Product ID: N" line. Slice each card up to the next `product-list-options` so a
# later card's price/id never leaks onto an earlier one.
_CARD_RE = re.compile(
    r'<div class="product-list-options">\s*'
    r'<h5>\s*<a\b[^>]*\bhref="(?P<href>[^"]+)"[^>]*>\s*(?P<title>.*?)\s*</a>',
    re.IGNORECASE | re.DOTALL)
_PRICE_RE = re.compile(
    r'<span class="product-list-cost-value">\s*(?P<price>[^<]+?)\s*</span>',
    re.IGNORECASE | re.DOTALL)
_PID_RE = re.compile(r'Product ID:\s*(?P<pid>\d+)', re.IGNORECASE)


class BatterySpaceAdapter(RetailAdapter):
    key = SITE
    base_url = BASE_URL
    search_url_tmpl = SEARCH_URL_TMPL
    license_note = LICENSE_NOTE
    login_wall_markers = _LOGIN_WALL_MARKERS
    challenge_markers = _CHALLENGE_MARKERS

    def parse_search(self, content: str) -> list[dict]:
        """BatterySpace ships no JSON-LD on its AmeriCommerce SRP; parse the
        server-rendered ``product-list-item`` cards directly."""
        hits: list[dict] = []
        seen: set[str] = set()
        cards = list(_CARD_RE.finditer(content))
        for i, m in enumerate(cards):
            href = m.group("href")
            url = href if href.startswith("http") else BASE_URL + href
            if url in seen:
                continue
            seen.add(url)
            end = cards[i + 1].start() if i + 1 < len(cards) else len(content)
            hit = {
                "ok": True,
                "title": _strip_tags(m.group("title")),
                "url": url,
                "site": SITE,
            }
            price_m = _PRICE_RE.search(content, m.end(), end)
            if price_m:
                hit["price"] = _strip_tags(price_m.group("price"))
            pid_m = _PID_RE.search(content, m.end(), end)
            if pid_m:
                hit["sku"] = pid_m.group("pid")
            hits.append(hit)
        return hits


ADAPTER = register(BatterySpaceAdapter())
