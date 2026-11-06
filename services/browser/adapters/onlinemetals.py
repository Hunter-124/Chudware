"""services/browser/adapters/onlinemetals.py — OnlineMetals source adapter (Parts Library V2).

Stock-metal vendor: titles are richly structured (``6061-T6 Aluminum Round
Tube 1" OD x 0.049" wall``), so ``acquire`` derives canonical stock params
(:func:`~._retail.parse_stock_params`) — alloy, profile, od_mm/wall_mm/
thickness_mm — from the title plus any spec table, instead of machining
params. Search reads JSON-LD or rendered product cards; challenges are reported.
"""
from __future__ import annotations
import re

from ._rendered_cards import first, parse

from . import register
from ._retail import RetailAdapter, parse_stock_params, stock_material

SITE = "onlinemetals"
BASE_URL = "https://www.onlinemetals.com"
SEARCH_URL_TMPL = BASE_URL + "/en/search?q={query}"
LICENSE_NOTE = (
    "OnlineMetals product data: reference-only, link-out commerce data. This "
    "adapter never stores or redistributes OnlineMetals' page content — only a "
    "small price/stock-spec record copied with provenance (source URL + fetch "
    "timestamp) for the user's own sourcing decision, never LLM-generated. "
    "See contracts/license_debt.md."
)

_LOGIN_WALL_MARKERS = ('id="login-email"',)
_CHALLENGE_MARKERS = ("Access Denied", "Reference #", "Pardon Our Interruption")


class OnlineMetalsAdapter(RetailAdapter):
    key = SITE
    base_url = BASE_URL
    search_url_tmpl = SEARCH_URL_TMPL
    license_note = LICENSE_NOTE
    login_wall_markers = _LOGIN_WALL_MARKERS
    challenge_markers = _CHALLENGE_MARKERS
    currency = "USD"

    def parse_search(self, content: str) -> list[dict]:
        hits = super().parse_search(content)
        if not hits:
            for card in parse(content).find_all(lambda n: n.tag == "div" and n.has_class("product-item")):
                link = first(card, lambda n: n.tag == "a" and n.attrs.get("data-qa-id") == "plp-item-image")
                if not link or not link.attrs.get("href") or not link.attrs.get("title"):
                    continue
                image = first(link, lambda n: n.tag == "img")
                # Some cards carry a different product's stock image.
                if image and image.attrs.get("alt", "").strip().casefold() != link.attrs["title"].strip().casefold():
                    image = None
                price_node = first(card, lambda n: n.attrs.get("data-qa-id") == "plp-item-price")
                price = re.search(r"\$\s*([\d,]+(?:\.\d{2})?)", price_node.text()) if price_node else None
                hits.append({"title": link.attrs["title"], "url": link.attrs["href"],
                             "thumbnail": image.attrs.get("src") if image else None,
                             "price": price.group(1) if price else None})
        for hit in hits:
            hit["material"] = hit.get("material") or stock_material(hit["title"])
        return hits

    def extract_params(self, result: dict, spec: dict, content: str) -> dict:
        title = result.get("title") or ""
        return parse_stock_params(title, spec)


ADAPTER = register(OnlineMetalsAdapter())
