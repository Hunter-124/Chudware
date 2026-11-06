"""services/browser/adapters/seeedstudio.py — Seeed Studio source adapter (Parts Library V2).

Seeed Studio (seeedstudio.com) is a maker / IoT hardware vendor. Much of its
catalog is open-source hardware, so CAD models are usually linked off-site
(GrabCAD) rather than hosted here; this adapter treats Seeed as a commerce and
linked-datasheet source, not a CAD-model source.
The shared retail flow fetches spec tables and linked datasheet PDFs as
documents, not CAD. The public catalog needs no login; challenges are reported
and never solved.

Current rendered product cards contain a titled product image link, SKU and
price. Older Algolia InstantSearch tiles and JSON-LD remain fallbacks. An
unrecognized or unsettled page is a parse failure, not an empty catalog.
Ordinary header sign-in links are not a login wall.
"""
from __future__ import annotations

import re
from html import unescape

from . import abs_url, make_hit, register
from ._retail import RetailAdapter, _price, _strip_tags, card_blocks, card_image

SITE = "seeedstudio"
BASE_URL = "https://www.seeedstudio.com"
SEARCH_URL_TMPL = BASE_URL + "/catalogsearch/result/?q={query}"
LICENSE_NOTE = (
    "Seeed Studio product data is reference-only link-out commerce data, and any "
    "datasheet remains the manufacturer's own document, fetched here for the "
    "user's own engineering reference only. This adapter never redistributes or "
    "LLM-generates page content — only a small price/spec record copied with "
    "provenance (source URL + fetch timestamp) and the manufacturer's datasheet "
    "saved for the user. See contracts/license_debt.md."
)

# Header sign-in links are not walls; explicit access-gate copy is.
_LOGIN_WALL_MARKERS = ("Please sign in to continue", "You must be logged in")
_CHALLENGE_MARKERS = (
    "Access Denied", "Pardon Our Interruption", "Verify you are human",
    "verify you are human", "<h1>CAPTCHA</h1>", "Please complete the CAPTCHA",
)

_TITLE_RE = re.compile(
    r'''<a\b(?=[^>]*\bclass=["'][^"']*\bais_p_title\b[^"']*["'])
        (?=[^>]*\bhref=["'](?P<href>[^"']+)["'])[^>]*>(?P<title>.*?)</a>''',
    re.IGNORECASE | re.DOTALL | re.VERBOSE,
)
_SKU_RE = re.compile(
    r'''<div\b[^>]*\bclass=["'][^"']*\bais_p_sku\b[^"']*["'][^>]*>(.*?)</div>''',
    re.IGNORECASE | re.DOTALL,
)
_PRICE_RE = re.compile(
    r'''<span\b[^>]*\bclass=["'][^"']*\bais_p_price_now\b[^"']*["'][^>]*>(.*?)</span>''',
    re.IGNORECASE | re.DOTALL,
)
_CURRENT_IMAGE_LINK = re.compile(
    r'''<a\b(?=[^>]*\bhref=["'](?P<href>[^"']+-p-\d+\.html(?:\?[^"']*)?)["'])
        (?=[^>]*\btitle=["'](?P<title>[^"']+)["'])[^>]*>.*?</a>''',
    re.IGNORECASE | re.DOTALL | re.VERBOSE,
)
_CURRENT_SKU = re.compile(r'>\s*SKU\s+([^<]+)</div>', re.IGNORECASE)
_CURRENT_PRICE = re.compile(r'<span\b[^>]*>\s*((?:US\$|USD|EUR|GBP|CAD|AUD|\$|€|£)\s*[\d,.]+)\s*</span>', re.IGNORECASE)


_AMOUNT_RE = re.compile(
    r"^\s*(?P<prefix>US\$|USD|EUR|GBP|CAD|AUD|\$|€|£)?\s*"
    r"(?P<value>\d[\d,]*(?:\.\d+)?)\s*(?P<suffix>USD|EUR|GBP|CAD|AUD|€|£)?\s*$",
    re.IGNORECASE,
)


def _money(text: str) -> tuple[float | None, str | None]:
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
    return price, currency if price is not None else None


class SeeedStudioAdapter(RetailAdapter):
    key = SITE
    hosts = ("seeedstudio.com",)
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
        for card in card_blocks(content, "div", "hover:shadow-md"):
            match = _CURRENT_IMAGE_LINK.search(card)
            if not match:
                continue
            url = abs_url(self.base_url, unescape(match.group("href")))
            title = unescape(match.group("title")).strip()
            if not url or not title or url in seen:
                continue
            seen.add(url)
            sku_match = _CURRENT_SKU.search(card)
            price_match = _CURRENT_PRICE.search(card)
            price, currency = _money(unescape(price_match.group(1))) if price_match else (None, None)
            hits.append(make_hit(
                self.key, title=title, url=url, sku=unescape(sku_match.group(1)).strip() if sku_match else None,
                price=price, currency=currency, thumbnail=card_image(match.group(), self.base_url),
                extra={"ok": True},
            ))
        for card in card_blocks(content, "div", "ais_hit_product_bar"):
            match = _TITLE_RE.search(card)
            if not match:
                continue
            url = abs_url(self.base_url, unescape(match.group("href")))
            title = unescape(_strip_tags(match.group("title"))).strip()
            if not url or not title or url in seen:
                continue
            seen.add(url)
            sku_match = _SKU_RE.search(card)
            price_match = _PRICE_RE.search(card)
            sku = unescape(_strip_tags(sku_match.group(1))).strip() if sku_match else None
            price_text = unescape(_strip_tags(price_match.group(1))).strip() if price_match else ""
            price, currency = _money(price_text)
            hits.append(make_hit(
                self.key, title=title, url=url, sku=sku or None, price=price,
                currency=currency,
                thumbnail=card_image(card, self.base_url), extra={"ok": True},
            ))
        return hits or super().parse_search(content)


ADAPTER = register(SeeedStudioAdapter())
