"""services/browser/adapters/homedepot.py — The Home Depot source adapter (Parts Library V2).

Commerce-only, like amazon.py: link-out + a small price/spec record copied with
provenance, never a downloadable CAD file. Home Depot fronts with Akamai bot
protection (HIGH risk): an "Access Denied" / "Reference #" interstitial is
treated exactly like a login wall — degrade to ``login_required``, never
solve or bypass.

Parse strategy: JSON-LD Product records (search + product pages both ship
them); the model number doubles as the SKU; product images live on
thdstatic.com.
"""
from __future__ import annotations

from . import register
from ._retail import RetailAdapter

SITE = "homedepot"
BASE_URL = "https://www.homedepot.com"
SEARCH_URL_TMPL = BASE_URL + "/s/{query}"
LICENSE_NOTE = (
    "Home Depot product data: reference-only, link-out commerce data. This "
    "adapter never stores or redistributes Home Depot's page content — only a "
    "small price/spec record copied with provenance (source URL + fetch "
    "timestamp) for the user's own sourcing decision, never LLM-generated. "
    "See contracts/license_debt.md."
)

_LOGIN_WALL_MARKERS = ('id="username"', "Sign in to your account", "Sign In or Create Account")
# Akamai bot wall — never bypass, treat exactly like a login wall.
_CHALLENGE_MARKERS = ("Access Denied", "Reference #", "Request unsuccessful")


class HomeDepotAdapter(RetailAdapter):
    key = SITE
    base_url = BASE_URL
    search_url_tmpl = SEARCH_URL_TMPL
    license_note = LICENSE_NOTE
    login_wall_markers = _LOGIN_WALL_MARKERS
    challenge_markers = _CHALLENGE_MARKERS


ADAPTER = register(HomeDepotAdapter())
