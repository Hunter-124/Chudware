"""services/browser/adapters/harborfreight.py — Harbor Freight source adapter (Parts Library V2).

Commerce-only (see amazon.py). Parse strategy: JSON-LD Products for search;
product pages link owner's-manual PDFs, which ``acquire`` downloads when the
generic datasheet/manual matcher hits. Challenge pages degrade to
``login_required`` — never solved.
"""
from __future__ import annotations

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

_LOGIN_WALL_MARKERS = ("Sign In", 'id="customer-email"')
_CHALLENGE_MARKERS = ("Access Denied", "Reference #", "Pardon Our Interruption")


class HarborFreightAdapter(RetailAdapter):
    key = SITE
    base_url = BASE_URL
    search_url_tmpl = SEARCH_URL_TMPL
    license_note = LICENSE_NOTE
    login_wall_markers = _LOGIN_WALL_MARKERS
    challenge_markers = _CHALLENGE_MARKERS


ADAPTER = register(HarborFreightAdapter())
