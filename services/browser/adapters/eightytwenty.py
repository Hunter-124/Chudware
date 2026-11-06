"""services/browser/adapters/eightytwenty.py — 80/20 Inc. source adapter.

80/20 Inc. (8020.net) is the T-slot aluminum extrusion maker ("The Industrial
Erector Set") — profiles, brackets, and fasteners common in student-robotics
and rover frames. Anonymous search results use Magento product cards; a sign-in
dialog may also be embedded in the search page without restricting the results.
Challenges and genuine sign-in walls are reported, never bypassed.
"""
from __future__ import annotations


from services.browser import session
from ._rendered_cards import first, parse
from . import fail, source_failure, login_required, is_login_wall
from ._retail import _challenge, _is_captcha, _standalone_login

from . import register
from ._retail import RetailAdapter

SITE = "8020"
BASE_URL = "https://8020.net"
SEARCH_URL_TMPL = BASE_URL + "/catalogsearch/result/?q={query}"
LICENSE_NOTE = (
    "80/20 Inc. product data is reference-only link-out commerce data fetched "
    "under the user's own browser session for their own engineering reference; "
    "any datasheet/CAD remains the manufacturer's own document. This adapter "
    "never redistributes page content — only a small price/spec record copied "
    "with provenance (source URL + fetch timestamp). See contracts/license_debt.md."
)

# A "Sign In" header link is ordinary chrome, not a wall — keep markers narrow.
_LOGIN_WALL_MARKERS = ("Please sign in to view", "You must be logged in")
# Magento bot-protection interstitials. browser_fetch already classifies most
# challenge pages (services/browser/challenge.py) and returns them as an expected
# gated state before we get here; these are a belt-and-braces backstop.
_CHALLENGE_MARKERS = ("Access Denied", "Pardon Our Interruption", "Reference #",
                      "Request unsuccessful", "Incapsula")


class EightyTwentyAdapter(RetailAdapter):
    key = SITE
    base_url = BASE_URL
    search_url_tmpl = SEARCH_URL_TMPL
    license_note = LICENSE_NOTE
    login_wall_markers = _LOGIN_WALL_MARKERS
    challenge_markers = _CHALLENGE_MARKERS
    currency = "USD"

    def search(self, query: str) -> list[dict]:
        url = self.search_url(query)
        result = session.browser_fetch(url, self.key)
        if not result.get("ok"):
            return [source_failure(self.key, "search", result, url=url)]
        content = result.get("content", "")
        page_url = result.get("final_url") or url
        if is_login_wall(content, self.challenge_markers) or _is_captcha(page_url, content):
            return [_challenge(self.key, "search", page_url, content)]
        if self._redirected_to_login(url, page_url) or is_login_wall(content, self.login_wall_markers):
            return [source_failure(self.key, "search", login_required(self.key, page_url), url=page_url)]
        hits = self.parse_search_page(content, page_url)
        if hits:
            return [self._normalize_hit(hit, page_url) for hit in hits]
        if _standalone_login(content):
            return [source_failure(self.key, "search", login_required(self.key, page_url), url=page_url)]
        if self.is_no_results(content):
            return []
        return [fail(self.key, "search", "parse_failed", "No recognizable results on the page.", url=page_url)]

    def parse_search(self, content: str) -> list[dict]:
        hits = super().parse_search(content)
        if hits:
            return hits
        for card in parse(content).find_all(lambda n: n.tag == "li" and n.has_class("product-item")):
            link = first(card, lambda n: n.tag == "a" and n.has_class("product-item-link"))
            image = first(card, lambda n: n.tag == "img" and n.has_class("product-image-photo"))
            price_node = first(card, lambda n: n.attrs.get("data-price-amount") is not None)
            if not link or not link.attrs.get("href") or not link.text():
                continue
            price = price_node.attrs.get("data-price-amount") if price_node else None
            hits.append({"title": link.text(), "url": link.attrs["href"],
                         "thumbnail": image.attrs.get("src") if image else None, "price": price})
        return hits


ADAPTER = register(EightyTwentyAdapter())
