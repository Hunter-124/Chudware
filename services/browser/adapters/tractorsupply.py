"""services/browser/adapters/tractorsupply.py — Tractor Supply source adapter (Parts Library V2).

Commerce-only (see amazon.py): link-out + a copied-with-provenance price/spec
record. Parse strategy: JSON-LD Product records. Challenge pages degrade to
``login_required`` — never solved.
"""
from __future__ import annotations

from . import register
from ._retail import RetailAdapter

SITE = "tractorsupply"
BASE_URL = "https://www.tractorsupply.com"
SEARCH_URL_TMPL = BASE_URL + "/tsc/search/{query}"
LICENSE_NOTE = (
    "Tractor Supply product data: reference-only, link-out commerce data. This "
    "adapter never stores or redistributes Tractor Supply's page content — "
    "only a small price/spec record copied with provenance (source URL + fetch "
    "timestamp) for the user's own sourcing decision, never LLM-generated. "
    "See contracts/license_debt.md."
)

_LOGIN_WALL_MARKERS = ("Sign In", 'id="logonId"')
_CHALLENGE_MARKERS = ("Access Denied", "Reference #", "Pardon Our Interruption")


class TractorSupplyAdapter(RetailAdapter):
    key = SITE
    base_url = BASE_URL
    search_url_tmpl = SEARCH_URL_TMPL
    license_note = LICENSE_NOTE
    login_wall_markers = _LOGIN_WALL_MARKERS
    challenge_markers = _CHALLENGE_MARKERS


ADAPTER = register(TractorSupplyAdapter())
