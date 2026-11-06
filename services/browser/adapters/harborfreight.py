"""services/browser/adapters/harborfreight.py — Harbor Freight source adapter (Parts Library V2).

Commerce-only (see amazon.py). Search uses JSON-LD or rendered product cards;
product pages link owner's-manual PDFs, which ``acquire`` downloads when the
generic datasheet/manual matcher hits. Challenges are reported, never solved.
"""
from __future__ import annotations
import re

from ._rendered_cards import first, parse

from . import register
from ._retail import RetailAdapter

SITE = "harborfreight"
BASE_URL = "https://www.harborfreight.com"
SEARCH_URL_TMPL = BASE_URL + "/search?q={query}"
LICENSE_NOTE = (
    "Harbor Freight product data: reference-only, link-out commerce data. This "
    "adapter never stores or redistributes Harbor Freight's page content — "
    "only a small price/spec record copied with provenance (source URL + fetch "
    "timestamp), plus vendor-published manual PDFs fetched under the user's "
    "own session for their own use. See contracts/license_debt.md."
)

_LOGIN_WALL_MARKERS = ('id="customer-email"', "Please sign in to view")
_CHALLENGE_MARKERS = ("Access Denied", "Reference #", "Pardon Our Interruption")


class HarborFreightAdapter(RetailAdapter):
    key = SITE
    base_url = BASE_URL
    search_url_tmpl = SEARCH_URL_TMPL
    license_note = LICENSE_NOTE
    login_wall_markers = _LOGIN_WALL_MARKERS
    challenge_markers = _CHALLENGE_MARKERS
    currency = "USD"

    def parse_search(self, content: str) -> list[dict]:
        hits = super().parse_search(content)
        if hits:
            return hits
        for card in parse(content).find_all(lambda n: n.tag == "li" and n.has_class("product-grid-item")):
            link = first(card, lambda n: n.tag == "a" and "item__itemTop--" in n.attrs.get("class", ""))
            name = first(card, lambda n: n.tag == "p" and "item__name--" in n.attrs.get("class", ""))
            image = first(card, lambda n: n.tag == "img" and n.has_class("productImage") and n.attrs.get("src"))
            if not link or not name or not link.attrs.get("href") or not name.text():
                continue
            price_node = first(card, lambda n: n.tag == "span" and
                               "styled-price__styledPrice--" in n.attrs.get("class", ""))
            price_text = price_node.text() if price_node else ""
            price = re.search(r"(\d[\d,]*) dollars? and (\d{1,2}) cents?", price_text)
            hits.append({"title": name.text(), "url": link.attrs["href"],
                         "thumbnail": image.attrs.get("src") if image else None,
                         "price": f"{price.group(1)}.{int(price.group(2)):02d}" if price else None})
        return hits


ADAPTER = register(HarborFreightAdapter())
