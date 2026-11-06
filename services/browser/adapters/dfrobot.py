"""services/browser/adapters/dfrobot.py — DFRobot source adapter (Parts Library V2).

DFRobot (dfrobot.com) is a maker / robotics module vendor — SENxxxx sensors and
DFRxxxx boards/modules. Some parts link STEP archives off-site; this adapter
provides commerce data and best-effort linked PDF datasheets, not CAD models.

Live-verified 2026-07-14: the old OpenCart-style guesses
(``/search-adv/result?q=``, ``/search?q=``, ``index.php?route=product/search``)
are hard HTTP 404s. The real SSR search route is
``/search-<query>.html`` (e.g. ``/search-gravity.html``) — a Nuxt page that
server-renders a ``product-list`` of ``product-item`` cards with
``class="name"`` title, ``SKU: XXX`` and a price. The Nuxt state may also
contain the real product image when the server-rendered tile has only a lazy
placeholder. Access challenges and unparseable pages are reported distinctly.
"""
from __future__ import annotations

import json
import re
from html import escape, unescape

from . import abs_url, make_hit, quote_query, register
from ._retail import RetailAdapter, _price, _strip_tags, card_blocks, card_image, visible_text

SITE = "dfrobot"
BASE_URL = "https://www.dfrobot.com"
# Live-verified 2026-07-14: /search-<query>.html is the SSR search route.
# Spaces become hyphens to match DFRobot's slug style ("gravity sensor" →
# search-gravity-sensor.html). Reserved characters are percent-encoded.
SEARCH_URL_TMPL = BASE_URL + "/search-{query}.html"
LICENSE_NOTE = (
    "DFRobot product data is reference-only link-out commerce data, and any "
    "datasheet remains the manufacturer's own document, fetched here for the "
    "user's own engineering reference only. This adapter never redistributes or "
    "LLM-generates page content — only a small price/spec record copied with "
    "provenance (source URL + fetch timestamp) and the manufacturer's datasheet "
    "saved for the user. See contracts/license_debt.md."
)

_LOGIN_WALL_MARKERS = ("Please sign in to continue", "You must be logged in")
_CHALLENGE_MARKERS = (
    "Access Denied", "Reference #", "Pardon Our Interruption",
    "Verify you are human", "verify you are human", "<h1>CAPTCHA</h1>", "Please complete the CAPTCHA",
)

_HREF_RE = re.compile(r'''href=["'](?P<href>/product-\d+\.html)["']''', re.IGNORECASE)
_NAME_RE = re.compile(
    r'''class=["'][^"']*\bname\b[^"']*["'][^>]*>(?P<name>.*?)</''',
    re.IGNORECASE | re.DOTALL,
)
_SKU_RE = re.compile(r'SKU:\s*(?P<sku>[A-Z0-9][A-Z0-9._-]*)', re.IGNORECASE)
_PRICE_RE = re.compile(
    r'''<(?:p|span)\b[^>]*\bclass=["'][^"']*\bprice\b[^"']*["'][^>]*>(.*?)</(?:p|span)>''',
    re.IGNORECASE | re.DOTALL,
)
_CURRENCY_RE = re.compile(
    r'''<div\b[^>]*\bclass=["'][^"']*\bcurrency\b[^"']*["'][^>]*>
        \s*<button\b[^>]*>\s*[$€£]?\s*(USD|EUR|GBP|CAD|AUD)\b''', re.IGNORECASE | re.VERBOSE,
)
_NUXT_RE = re.compile(r"<script\b[^>]*>\s*window\.__NUXT__\s*=.*?</script>", re.IGNORECASE | re.DOTALL)
_NUXT_IMAGE_RE = re.compile(
    r'''\bhref\s*:\s*("(?:\\.|[^"\\])*")\s*,\s*image\s*:\s*("(?:\\.|[^"\\])*")''',
    re.IGNORECASE,
)
_POSTER_RE = re.compile(r'''<video\b[^>]*\bposter=["']([^"']+)["']''', re.IGNORECASE)
_BACKGROUND_RE = re.compile(r'''background-image\s*:\s*url\(\s*['"]?([^'")]+)''', re.IGNORECASE)
_AMOUNT_RE = re.compile(
    r"^\s*(?P<prefix>US\$|USD|EUR|GBP|CAD|AUD|\$|€|£)?\s*"
    r"(?P<value>\d[\d,]*(?:\.\d+)?)\s*(?P<suffix>USD|EUR|GBP|CAD|AUD|€|£)?\s*$",
    re.IGNORECASE,
)


def _money(text: str, selected_currency: str | None) -> tuple[float | None, str | None]:
    match = _AMOUNT_RE.fullmatch(text)
    if not match:
        return None, None
    price = _price(match.group("value"))
    units = {"US$": "USD", "€": "EUR", "£": "GBP"}
    prefix = (match.group("prefix") or "").upper()
    suffix = (match.group("suffix") or "").upper()
    before = units.get(prefix, prefix if prefix and prefix != "$" else None)
    after = units.get(suffix, suffix or None)
    currency = after or before
    if before and after and before != after:
        currency = None
    elif not currency and (not prefix or prefix == "$" and selected_currency in {"USD", "CAD", "AUD"}):
        currency = selected_currency
    return price, currency if price is not None else None


def _nuxt_images(content: str) -> dict[str, str]:
    """Read literal href/image pairs only; never execute the Nuxt script."""
    script = _NUXT_RE.search(content)
    if not script:
        return {}
    images = {}
    for match in _NUXT_IMAGE_RE.finditer(script.group()):
        try:
            url = abs_url(BASE_URL, json.loads(match.group(1)))
            image = json.loads(match.group(2))
        except (ValueError, TypeError):
            continue
        # Apply the same real-image/placeholder filtering as an HTML card.
        thumbnail = card_image(f'<img src="{escape(image, quote=True)}">', BASE_URL)
        if url and thumbnail:
            images.setdefault(url, thumbnail)
    return images


def _slug_query(query: str) -> str:
    """Map a free-text query to DFRobot's search-URL slug.

    Live shape: spaces → hyphens, lowercased; other characters percent-encoded.
    """
    q = (query or "").strip().lower()
    q = re.sub(r"\s+", "-", q)
    q = re.sub(r"-{2,}", "-", q).strip("-")
    return quote_query(q)


class DfrobotAdapter(RetailAdapter):
    key = SITE
    hosts = ("dfrobot.com",)
    result_kinds = ("product",)
    cad_acquisition = False
    login_required = False
    api = False
    base_url = BASE_URL
    search_url_tmpl = SEARCH_URL_TMPL
    license_note = LICENSE_NOTE
    login_wall_markers = _LOGIN_WALL_MARKERS
    challenge_markers = _CHALLENGE_MARKERS

    def search_url(self, query: str) -> str:
        return self.search_url_tmpl.format(query=_slug_query(query))

    def is_no_results(self, content: str) -> bool:
        # The "Not finding any matches" component also exists hidden on pages
        # with products; the actual zero item count is the reliable signal.
        return bool(re.search(r"\b0\s+item\(s\)\s+found\s+for\b", visible_text(content), re.I)) or super().is_no_results(content)

    def parse_search(self, content: str) -> list[dict]:
        """Parse SSR ``product-item`` cards (live 2026-07-14 search-*.html).

        Falls back to the shared JSON-LD Product walk when the card markup is
        absent (future theme change) so we still return real hits when present.
        """
        hits: list[dict] = []
        seen: set[str] = set()

        images = _nuxt_images(content)
        currency_match = _CURRENCY_RE.search(content)
        selected_currency = currency_match.group(1).upper() if currency_match else None
        for chunk in card_blocks(content, "div", "product-item"):
            href_m = _HREF_RE.search(chunk)
            name_m = _NAME_RE.search(chunk)
            if not href_m or not name_m:
                continue
            url = abs_url(self.base_url, href_m.group("href"))
            title = unescape(_strip_tags(name_m.group("name"))).strip()
            if not url or not title or url in seen:
                continue
            seen.add(url)
            sku_m = _SKU_RE.search(chunk)
            price_m = _PRICE_RE.search(chunk)
            price_text = unescape(_strip_tags(price_m.group(1))).strip() if price_m else ""
            price, currency = _money(price_text, selected_currency)

            thumbnail = card_image(chunk, self.base_url) or images.get(url)
            if not thumbnail:
                poster = _POSTER_RE.search(chunk)
                background = _BACKGROUND_RE.search(unescape(chunk))
                candidate = unescape(poster.group(1)) if poster else background.group(1) if background else ""
                if candidate:
                    thumbnail = card_image(f'<img src="{escape(candidate, quote=True)}">', self.base_url)
            hits.append(make_hit(
                self.key, title=title, url=url, sku=sku_m.group("sku").strip() if sku_m else None,
                price=price, currency=currency, thumbnail=thumbnail, extra={"ok": True},
            ))

        if hits:
            return hits
        # Fallback: JSON-LD Products if the theme ships them later.
        return super().parse_search(content)


ADAPTER = register(DfrobotAdapter())
