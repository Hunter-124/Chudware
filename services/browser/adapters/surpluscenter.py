"""services/browser/adapters/surpluscenter.py — Surplus Center source adapter (Parts Library V2).

Discount hydraulics vendor (pumps/motors/cylinders) with an open storefront:
this is a COMMERCE source (price/stock plus thin specs), not a CAD-model
source. ``acquire`` uses the default machining/spec-table param extraction plus
datasheet discovery, and copies only a small price/stock-spec record with
provenance. Parse strategy: JSON-LD Products for search. Challenge pages
degrade to ``login_required`` — never solved.
"""
from __future__ import annotations

from . import register
from ._retail import RetailAdapter

SITE = "surpluscenter"
BASE_URL = "https://www.surpluscenter.com"
SEARCH_URL_TMPL = BASE_URL + "/search?q={query}"
LICENSE_NOTE = (
    "Surplus Center product data: reference-only, link-out commerce data. This "
    "adapter never stores or redistributes Surplus Center's page content — only "
    "a small price/stock-spec record copied with provenance (source URL + fetch "
    "timestamp) for the user's own sourcing decision, never LLM-generated. "
    "See contracts/license_debt.md."
)

_LOGIN_WALL_MARKERS = ("Sign In", 'id="login-email"')
_CHALLENGE_MARKERS = ("Access Denied", "Reference #", "Pardon Our Interruption")


class SurplusCenterAdapter(RetailAdapter):
    key = SITE
    base_url = BASE_URL
    search_url_tmpl = SEARCH_URL_TMPL
    license_note = LICENSE_NOTE
    login_wall_markers = _LOGIN_WALL_MARKERS
    challenge_markers = _CHALLENGE_MARKERS


ADAPTER = register(SurplusCenterAdapter())
