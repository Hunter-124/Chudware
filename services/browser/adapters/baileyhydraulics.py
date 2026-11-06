"""services/browser/adapters/baileyhydraulics.py — Bailey Hydraulics source adapter (Parts Library V2).

Hydraulics vendor (cylinders/pumps/valves/reservoirs) with an open e-commerce
storefront: this is a COMMERCE source (price/stock plus specs), not a CAD-model
source. ``acquire`` uses the default machining/spec-table param extraction plus
datasheet discovery, and copies only a small price/stock-spec record with
provenance. Challenges are reported as blocked or captcha, never solved.

Live-verified 2026-07-10: the storefront runs on BigCommerce (Stencil), so search
is ``/search.php?search_query=`` (the old ``/search?q=`` guess 404'd) and results
carry no JSON-LD — they are server-rendered ``<article class="card">`` tiles whose
``<h4 class="card-title"><a>`` holds the product URL + title, the displayed
``card-text--sku`` is the SKU, and a ``price--withoutTax`` span gives the price.
"""
from __future__ import annotations

import re
from html import unescape

from . import abs_url, register
from ._retail import RetailAdapter, _strip_tags, card_blocks, card_image

SITE = "baileyhydraulics"
BASE_URL = "https://www.baileyhydraulics.com"
SEARCH_URL_TMPL = BASE_URL + "/search.php?search_query={query}"
LICENSE_NOTE = (
    "Bailey Hydraulics product data: reference-only, link-out commerce data. "
    "This adapter never stores or redistributes Bailey Hydraulics' page content "
    "— only a small price/stock-spec record copied with provenance (source URL + "
    "fetch timestamp) for the user's own sourcing decision, never LLM-generated. "
    "See contracts/license_debt.md."
)

_LOGIN_WALL_MARKERS = ('id="login-email"',)
_CHALLENGE_MARKERS = ("Access Denied", "Pardon Our Interruption")

# Product IDs on quick-view buttons are distinct from the displayed SKU.
_CARD_RE = re.compile(
    r'<article\b[^>]*\bclass="card\b.*?'
    r'<h4 class="card-title">\s*<a href="(?P<href>[^"]+)"[^>]*>(?P<title>.*?)</a>',
    re.IGNORECASE | re.DOTALL)
_PID_RE = re.compile(
    r'<a\b(?=[^>]*\bclass=["\'][^"\']*\bquickview\b[^"\']*["\'])'
    r'(?=[^>]*\bdata-product-id=["\'](?P<pid>\d+)["\'])[^>]*>', re.IGNORECASE)
_SKU_RE = re.compile(
    r'<p\b[^>]*class=["\'][^"\']*\bcard-text--sku\b[^"\']*["\'][^>]*>'
    r'\s*SKU:\s*(?P<sku>[^<]+)', re.IGNORECASE)
_BRAND_RE = re.compile(
    r'<p\b[^>]*class=["\'][^"\']*\bcard-text--brand\b[^"\']*["\'][^>]*>'
    r'(?P<brand>.*?)</p>', re.IGNORECASE | re.DOTALL)
_PRICE_RE = re.compile(
    r'<span[^>]*\bclass="price price--withoutTax[^"]*"[^>]*>\s*(?P<price>\$[\d,]+\.\d\d)',
    re.IGNORECASE)


class BaileyHydraulicsAdapter(RetailAdapter):
    key = SITE
    hosts = ("www.baileyhydraulics.com", "baileyhydraulics.com")
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
        """Read each complete BigCommerce tile, not neighboring tiles."""
        hits: list[dict] = []
        seen: set[str] = set()
        for block in card_blocks(content, "article", "card"):
            match = _CARD_RE.search(block)
            if not match:
                continue
            url = abs_url(BASE_URL, unescape(match.group("href")).split("?", 1)[0])
            title = unescape(_strip_tags(match.group("title")))
            if not url or not title or url in seen:
                continue
            seen.add(url)
            media = card_blocks(block, "div", "card-img-container")
            hit = {
                "ok": True, "title": title, "url": url, "site": SITE,
                "thumbnail": card_image(media[0], BASE_URL) if media else None,
            }
            sku = _SKU_RE.search(block)
            if sku:
                hit["sku"] = unescape(sku.group("sku").strip())
            pid = _PID_RE.search(block)
            if pid:
                hit["product_id"] = pid.group("pid")
            brand = _BRAND_RE.search(block)
            if brand:
                hit["brand"] = unescape(_strip_tags(brand.group("brand")))
            price = _PRICE_RE.search(block)
            if price:
                hit.update(price=price.group("price"), currency="USD")
            stock_blocks = card_blocks(block, "div", "card-stock")
            if stock_blocks:
                stock_text = unescape(_strip_tags(stock_blocks[0]))
                stock = re.search(r"([\d,]+)\s+in stock\b", stock_text, re.I)
                if stock:
                    hit["stock"] = stock.group(1)
                elif re.search(r"\bout of stock\b", stock_text, re.I):
                    hit["stock"] = 0
            hits.append(hit)
        return hits


ADAPTER = register(BaileyHydraulicsAdapter())
