"""services/browser/adapters/dfrobot.py — DFRobot source adapter (Parts Library V2).

DFRobot (dfrobot.com) is a maker / robotics module vendor — SENxxxx sensors and
DFRxxxx boards/modules. Each product carries a per-product wiki datasheet, and
some parts ship STEP archives on GitHub off-site; this adapter treats DFRobot as
a commerce + datasheet source (price/spec record + the wiki/datasheet PDF), not
a CAD-model source.

Live-verified 2026-07-14: the old OpenCart-style guesses
(``/search-adv/result?q=``, ``/search?q=``, ``index.php?route=product/search``)
are hard HTTP 404s. The real SSR search route is
``/search-<query>.html`` (e.g. ``/search-gravity.html``) — a Nuxt page that
server-renders a ``product-list`` of ``product-item`` cards with
``class="name"`` title, ``SKU: XXX`` and ``$price``. Challenge pages still
degrade to ``login_required`` — never solved. Empty parse is honest (no hits),
never a silent empty 404.
"""
from __future__ import annotations

import re
from urllib.parse import quote

from .. import session as _session
from . import is_login_wall, login_required, register
from ._retail import RetailAdapter, _strip_tags

SITE = "dfrobot"
BASE_URL = "https://www.dfrobot.com"
# Live-verified 2026-07-14: /search-<query>.html is the SSR search route.
# Spaces become hyphens to match DFRobot's slug style ("gravity sensor" →
# search-gravity-sensor.html). quote() still escapes other reserved chars.
SEARCH_URL_TMPL = BASE_URL + "/search-{query}.html"
LICENSE_NOTE = (
    "DFRobot product data is reference-only link-out commerce data, and any "
    "datasheet remains the manufacturer's own document, fetched here for the "
    "user's own engineering reference only. This adapter never redistributes or "
    "LLM-generates page content — only a small price/spec record copied with "
    "provenance (source URL + fetch timestamp) and the manufacturer's datasheet "
    "saved for the user. See contracts/license_debt.md."
)

_LOGIN_WALL_MARKERS = ("Sign In", "Log In", 'id="login"')
_CHALLENGE_MARKERS = ("Access Denied", "Reference #", "Pardon Our Interruption")

# Card boundary + field markers from the live /search-gravity.html capture
# (2026-07-14). Each product-item carries name / SKU / $price + product-N.html.
_PRODUCT_ITEM_RE = re.compile(
    r'<div[^>]*class="[^"]*product-item[^"]*"[^>]*>(?P<body>.*?)</div>\s*</div>\s*</div>',
    re.IGNORECASE | re.DOTALL,
)
_HREF_RE = re.compile(r'href="(?P<href>/product-\d+\.html)"', re.IGNORECASE)
_NAME_RE = re.compile(
    r'class="[^"]*\bname\b[^"]*"[^>]*>(?P<name>.*?)</',
    re.IGNORECASE | re.DOTALL,
)
_SKU_RE = re.compile(r'SKU:\s*(?P<sku>[A-Z0-9][A-Z0-9._-]*)', re.IGNORECASE)
_PRICE_RE = re.compile(r'\$\s*(?P<price>\d+(?:\.\d+)?)')


def _slug_query(query: str) -> str:
    """Map a free-text query to DFRobot's search-URL slug.

    Live shape: spaces → hyphens, lowercased; other characters percent-encoded.
    """
    q = (query or "").strip().lower()
    q = re.sub(r"\s+", "-", q)
    q = re.sub(r"-{2,}", "-", q).strip("-")
    return quote(q, safe="-._~")


class DfrobotAdapter(RetailAdapter):
    key = SITE
    base_url = BASE_URL
    search_url_tmpl = SEARCH_URL_TMPL
    license_note = LICENSE_NOTE
    login_wall_markers = _LOGIN_WALL_MARKERS
    challenge_markers = _CHALLENGE_MARKERS

    def search_url(self, query: str) -> str:
        return self.search_url_tmpl.format(query=_slug_query(query))

    def search(self, query: str) -> list[dict]:
        """Fetch the SSR search page; 404/http errors surface as structured
        failures (never a silent empty list for a broken endpoint)."""
        url = self.search_url(query)
        result = _session.browser_fetch(url, self.key)
        if not result.get("ok"):
            # browser_fetch already returns a structured failure dict (http_error,
            # login_required, …). Propagate it — never collapse a 404 to [].
            return [result]
        content = result.get("content", "")
        if is_login_wall(content, self.challenge_markers):
            return [login_required(self.key, url)]
        if is_login_wall(content, self.login_wall_markers):
            return [login_required(self.key, url)]
        # Soft 404 / SPA shell that still returns HTTP 200 with no product cards:
        # if the page title/body clearly says nothing was found, empty is honest.
        return self.parse_search(content)

    def parse_search(self, content: str) -> list[dict]:
        """Parse SSR ``product-item`` cards (live 2026-07-14 search-*.html).

        Falls back to the shared JSON-LD Product walk when the card markup is
        absent (future theme change) so we still return real hits when present.
        """
        hits: list[dict] = []
        seen: set[str] = set()

        # Prefer walking product-item chunks. The non-greedy multi-div regex can
        # miss nested closers, so also split on the product-item opener.
        chunks = re.split(r'(?=<div[^>]*class="[^"]*product-item[^"]*")', content,
                          flags=re.IGNORECASE)
        for chunk in chunks[1:]:  # skip preamble before first card
            href_m = _HREF_RE.search(chunk)
            name_m = _NAME_RE.search(chunk)
            if not href_m or not name_m:
                continue
            href = href_m.group("href")
            title = _strip_tags(name_m.group("name"))
            if not title or href in seen:
                continue
            seen.add(href)
            hit: dict = {
                "ok": True,
                "title": title,
                "url": self.base_url + href,
                "site": self.key,
            }
            sku_m = _SKU_RE.search(chunk)
            if sku_m:
                hit["sku"] = sku_m.group("sku").strip()
                hit["mpn"] = hit["sku"]
            price_m = _PRICE_RE.search(chunk)
            if price_m:
                hit["price"] = f"${price_m.group('price')}"
            hits.append(hit)

        if hits:
            return hits
        # Fallback: JSON-LD Products if the theme ships them later.
        return super().parse_search(content)


ADAPTER = register(DfrobotAdapter())
