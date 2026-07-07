"""services/browser/adapters/acpcomposites.py — ACP Composites source adapter (Parts Library V2).

Composites vendor on WooCommerce with an open storefront, live prices and a
mechanical-properties PDF: ``acquire`` uses the default machining/spec-table
param extraction plus datasheet discovery, and copies only a small
price/stock-spec record with provenance. Parse strategy: JSON-LD Products for
search. Challenge pages degrade to ``login_required`` — never solved.
"""
from __future__ import annotations

from . import register
from ._retail import RetailAdapter

SITE = "acpcomposites"
BASE_URL = "https://www.acpcomposites.com"
SEARCH_URL_TMPL = BASE_URL + "/?s={query}&post_type=product"
LICENSE_NOTE = (
    "ACP Composites product data: reference-only, link-out commerce data. This "
    "adapter never stores or redistributes ACP Composites' page content — only "
    "a small price/stock-spec record copied with provenance (source URL + fetch "
    "timestamp) for the user's own sourcing decision, never LLM-generated. "
    "See contracts/license_debt.md."
)

_LOGIN_WALL_MARKERS = ("Sign In", 'id="login-email"')
_CHALLENGE_MARKERS = ("Access Denied", "Reference #", "Pardon Our Interruption")


class AcpCompositesAdapter(RetailAdapter):
    key = SITE
    base_url = BASE_URL
    search_url_tmpl = SEARCH_URL_TMPL
    license_note = LICENSE_NOTE
    login_wall_markers = _LOGIN_WALL_MARKERS
    challenge_markers = _CHALLENGE_MARKERS


ADAPTER = register(AcpCompositesAdapter())
