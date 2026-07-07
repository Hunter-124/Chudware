"""services/browser/adapters/acehardware.py — Ace Hardware source adapter (Parts Library V2).

Commerce-only (see amazon.py): link-out + a copied-with-provenance price/spec
record. Parse strategy: JSON-LD Product records on both search and product
pages. Challenge pages degrade to ``login_required`` — never solved.
"""
from __future__ import annotations

from . import register
from ._retail import RetailAdapter

SITE = "acehardware"
BASE_URL = "https://www.acehardware.com"
SEARCH_URL_TMPL = BASE_URL + "/search?query={query}"
LICENSE_NOTE = (
    "Ace Hardware product data: reference-only, link-out commerce data. This "
    "adapter never stores or redistributes Ace's page content — only a small "
    "price/spec record copied with provenance (source URL + fetch timestamp) "
    "for the user's own sourcing decision, never LLM-generated. See "
    "contracts/license_debt.md."
)

_LOGIN_WALL_MARKERS = ("Sign In to Your Account", 'id="login-email"')
_CHALLENGE_MARKERS = ("Access Denied", "Reference #", "Pardon Our Interruption")


class AceHardwareAdapter(RetailAdapter):
    key = SITE
    base_url = BASE_URL
    search_url_tmpl = SEARCH_URL_TMPL
    license_note = LICENSE_NOTE
    login_wall_markers = _LOGIN_WALL_MARKERS
    challenge_markers = _CHALLENGE_MARKERS


ADAPTER = register(AceHardwareAdapter())
