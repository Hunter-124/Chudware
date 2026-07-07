"""services/browser/adapters/fastenal.py — Fastenal source adapter (Parts Library V2).

Fastener-focused MRO catalog: product pages carry a spec table (thread size,
grade, finish, ...) which ``acquire`` surfaces verbatim under ``spec`` plus
canonical machining params where the table names them. Parse strategy: JSON-LD
Products for search. Challenge pages degrade to ``login_required`` — never
solved.
"""
from __future__ import annotations

from . import register
from ._retail import RetailAdapter

SITE = "fastenal"
BASE_URL = "https://www.fastenal.com"
SEARCH_URL_TMPL = BASE_URL + "/product?query={query}"
LICENSE_NOTE = (
    "Fastenal product data: reference-only, link-out commerce data. This "
    "adapter never stores or redistributes Fastenal's page content — only a "
    "small price/spec record copied with provenance (source URL + fetch "
    "timestamp) for the user's own sourcing decision, never LLM-generated. "
    "See contracts/license_debt.md."
)

_LOGIN_WALL_MARKERS = ("Sign In", 'id="username"', "Log in to see your pricing")
_CHALLENGE_MARKERS = ("Access Denied", "Reference #", "Request unsuccessful")


class FastenalAdapter(RetailAdapter):
    key = SITE
    base_url = BASE_URL
    search_url_tmpl = SEARCH_URL_TMPL
    license_note = LICENSE_NOTE
    login_wall_markers = _LOGIN_WALL_MARKERS
    challenge_markers = _CHALLENGE_MARKERS


ADAPTER = register(FastenalAdapter())
