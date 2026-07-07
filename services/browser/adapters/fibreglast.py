"""services/browser/adapters/fibreglast.py — Fibre Glast source adapter (Parts Library V2).

Composites/fiberglass vendor on Shopify with an open storefront, plus public
PDS + SDS PDFs: ``acquire`` uses the default machining/spec-table param
extraction plus datasheet discovery, and copies only a small price/stock-spec
record with provenance. Parse strategy: JSON-LD Products for search. Challenge
pages degrade to ``login_required`` — never solved.
"""
from __future__ import annotations

from . import register
from ._retail import RetailAdapter

SITE = "fibreglast"
BASE_URL = "https://www.fibreglast.com"
SEARCH_URL_TMPL = BASE_URL + "/search?q={query}"
LICENSE_NOTE = (
    "Fibre Glast product data: reference-only, link-out commerce data. This "
    "adapter never stores or redistributes Fibre Glast's page content — only a "
    "small price/stock-spec record copied with provenance (source URL + fetch "
    "timestamp) for the user's own sourcing decision, never LLM-generated. "
    "See contracts/license_debt.md."
)

_LOGIN_WALL_MARKERS = ("Sign In", 'id="login-email"')
_CHALLENGE_MARKERS = ("Access Denied", "Reference #", "Pardon Our Interruption")


class FibreGlastAdapter(RetailAdapter):
    key = SITE
    base_url = BASE_URL
    search_url_tmpl = SEARCH_URL_TMPL
    license_note = LICENSE_NOTE
    login_wall_markers = _LOGIN_WALL_MARKERS
    challenge_markers = _CHALLENGE_MARKERS


ADAPTER = register(FibreGlastAdapter())
