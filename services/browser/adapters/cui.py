"""services/browser/adapters/cui.py — CUI Devices source adapter (Parts Library V2).

CUI Devices is now Same Sky. Search uses its server-rendered product cards,
and an exact part-number search can redirect directly to the product detail.
Datasheets are acquired by the shared retail adapter. The public site's header
sign-in widget is not a login wall.
"""
from __future__ import annotations

import html
import re
from urllib.parse import urlparse

from . import abs_url, make_hit, register
from ._retail import RetailAdapter, _strip_tags, card_image, parse_jsonld_products

SITE = "cui"
BASE_URL = "https://www.sameskydevices.com"
SEARCH_URL_TMPL = BASE_URL + "/SiteSearch/SearchByPartialMatch?partnumber={query}"
LICENSE_NOTE = (
    "CUI Devices product data: reference-only, link-out commerce data. This "
    "adapter never stores or redistributes CUI Devices' page content — only a "
    "small price/spec record copied with provenance (source URL + fetch "
    "timestamp) for the user's own sourcing decision, never LLM-generated. "
    "Datasheets remain the manufacturer's documents fetched for the user's own "
    "engineering reference. See contracts/license_debt.md."
)

# Public catalog — the header "Sign In"/name=password widget is NOT a wall marker.
_LOGIN_WALL_MARKERS: tuple[str, ...] = ()
_CHALLENGE_MARKERS = ("Access Denied", "Pardon Our Interruption")

_ANCHOR_RE = re.compile(r"<a\b[^>]*>", re.IGNORECASE | re.DOTALL)
_ANCHOR_END_RE = re.compile(r"</a\s*>|<a\b", re.IGNORECASE)
_H1_RE = re.compile(r"<h1\b[^>]*>(.*?)</h1\s*>", re.IGNORECASE | re.DOTALL)
_PRODUCT_IMG_RE = re.compile(r"<img\b[^>]*getproductimage[^>]*>", re.IGNORECASE | re.DOTALL)


def _attribute(tag: str, name: str) -> str:
    match = re.search(r"(?<![\w-])" + re.escape(name) + r"""\s*=\s*(["'])(.*?)\1""",
                      tag, re.IGNORECASE | re.DOTALL)
    return html.unescape(match.group(2)).strip() if match else ""


class CuiAdapter(RetailAdapter):
    key = SITE
    hosts = ("cuidevices.com", "sameskydevices.com")
    result_kinds = ("product",)
    cad_acquisition = False
    login_required = False
    api = False
    base_url = BASE_URL
    search_url_tmpl = SEARCH_URL_TMPL
    license_note = LICENSE_NOTE
    login_wall_markers = _LOGIN_WALL_MARKERS
    challenge_markers = _CHALLENGE_MARKERS
    no_results_markers = ("No match found for",)

    def parse_search(self, content: str) -> list[dict]:
        return self._parse_cards(content, self.base_url)

    def parse_search_page(self, content: str, page_url: str) -> list[dict]:
        parsed = urlparse(page_url)
        if (parsed.hostname or "").lower().removeprefix("www.") == "sameskydevices.com" \
                and parsed.path.lower().startswith("/product/"):
            # Exact-match searches redirect to a product, not a results list.
            heading = _H1_RE.search(content)
            title = html.unescape(_strip_tags(heading.group(1))) if heading else ""
            if not title:
                return []
            image = _PRODUCT_IMG_RE.search(content)
            product = next((p for p in parse_jsonld_products(content)
                            if (p.get("url") and abs_url(page_url, p["url"]) == page_url)
                            or p.get("mpn") == title or p.get("sku") == title), None)
            if product:
                hit = self._hit_from_product(product)
                hit.update({"title": title, "url": page_url, "mpn": product.get("mpn") or title})
                if not hit.get("thumbnail") and image:
                    hit["thumbnail"] = card_image(image.group(), page_url)
                return [hit]
            return [make_hit(
                SITE, title=title, url=page_url, kind="product", mpn=title,
                thumbnail=card_image(image.group(), page_url) if image else None,
                extra={"ok": True},
            )]
        return self._parse_cards(content, page_url)

    def _parse_cards(self, content: str, page_url: str) -> list[dict]:
        hits: list[dict] = []
        seen: set[str] = set()
        for match in _ANCHOR_RE.finditer(content):
            tag = match.group()
            if "dl-productlink" not in _attribute(tag, "class").lower().split():
                continue
            pn = _attribute(tag, "dl-itemnumber")
            url = abs_url(page_url, _attribute(tag, "href"))
            if not pn or not url or url in seen:
                continue
            seen.add(url)
            end = _ANCHOR_END_RE.search(content, match.end())
            card = content[match.start():end.start() if end else match.end()]
            category = _attribute(tag, "dl-itemcategories")
            extra = {"ok": True}
            if category:
                extra["category"] = " / ".join(part.strip() for part in category.split(","))
            hits.append(make_hit(SITE, title=pn, url=url, kind="product", mpn=pn,
                                 thumbnail=card_image(card, page_url), extra=extra))
        return hits


ADAPTER = register(CuiAdapter())
