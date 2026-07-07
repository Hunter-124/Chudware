"""services/browser/adapters/coremarkmetals.py — CoreMark Metals source adapter (Parts Library V2).

Stock-metal vendor (formerly DiscountSteel): titles are richly structured
(``6061-T6 Aluminum Round Tube 1" OD x 0.049" wall``), so ``acquire`` derives
canonical stock params (:func:`~._retail.parse_stock_params`) — alloy, profile,
od_mm/wall_mm/thickness_mm — from the title plus any spec table, instead of
machining params. Parse strategy: JSON-LD Products for search. Mild bot
friction: challenge pages degrade to ``login_required`` — never solved.
"""
from __future__ import annotations

from . import register
from ._retail import RetailAdapter, parse_stock_params

SITE = "coremarkmetals"
BASE_URL = "https://www.coremarkmetals.com"
SEARCH_URL_TMPL = BASE_URL + "/search?q={query}"
LICENSE_NOTE = (
    "CoreMark Metals product data: reference-only, link-out commerce data. This "
    "adapter never stores or redistributes CoreMark Metals' page content — only "
    "a small price/stock-spec record copied with provenance (source URL + fetch "
    "timestamp) for the user's own sourcing decision, never LLM-generated. "
    "See contracts/license_debt.md."
)

_LOGIN_WALL_MARKERS = ("Sign In", 'id="login-email"')
_CHALLENGE_MARKERS = (
    "Access Denied", "Reference #", "Pardon Our Interruption",
    "cf-browser-verification",
)


class CoreMarkMetalsAdapter(RetailAdapter):
    key = SITE
    base_url = BASE_URL
    search_url_tmpl = SEARCH_URL_TMPL
    license_note = LICENSE_NOTE
    login_wall_markers = _LOGIN_WALL_MARKERS
    challenge_markers = _CHALLENGE_MARKERS

    def extract_params(self, result: dict, spec: dict, content: str) -> dict:
        title = result.get("title") or ""
        return parse_stock_params(title, spec)


ADAPTER = register(CoreMarkMetalsAdapter())
