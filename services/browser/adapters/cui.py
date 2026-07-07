"""services/browser/adapters/cui.py — CUI Devices source adapter (Parts Library V2).

CUI Devices (cuidevices.com): power supplies, DC-DC converters, connectors,
and encoders. Each part page carries a datasheet PDF, so ``acquire`` uses the
inherited default ``extract_params`` — :func:`~._retail.find_datasheet_url`
grabs the datasheet/spec PDF. Parse strategy: JSON-LD Products for search.
Challenge/login pages degrade to ``login_required`` — never solved. v1
best-effort markers, not live-verified.
"""
from __future__ import annotations

from . import register
from ._retail import RetailAdapter

SITE = "cui"
BASE_URL = "https://www.cuidevices.com"
SEARCH_URL_TMPL = BASE_URL + "/search?q={query}"
LICENSE_NOTE = (
    "CUI Devices product data: reference-only, link-out commerce data. This "
    "adapter never stores or redistributes CUI Devices' page content — only a "
    "small price/spec record copied with provenance (source URL + fetch "
    "timestamp) for the user's own sourcing decision, never LLM-generated. "
    "Datasheets remain the manufacturer's documents fetched for the user's own "
    "engineering reference. See contracts/license_debt.md."
)

_LOGIN_WALL_MARKERS = ("Sign In", "Log In", 'name="password"')
_CHALLENGE_MARKERS = ("Access Denied", "Reference #", "Pardon Our Interruption")


class CuiAdapter(RetailAdapter):
    key = SITE
    base_url = BASE_URL
    search_url_tmpl = SEARCH_URL_TMPL
    license_note = LICENSE_NOTE
    login_wall_markers = _LOGIN_WALL_MARKERS
    challenge_markers = _CHALLENGE_MARKERS


ADAPTER = register(CuiAdapter())
