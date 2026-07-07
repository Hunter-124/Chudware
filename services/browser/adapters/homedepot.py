"""services/browser/adapters/homedepot.py — The Home Depot source adapter (Parts Library V2).

Commerce-only, like amazon.py: link-out + a small price/spec record copied with
provenance, never a downloadable CAD file. Home Depot fronts with Akamai bot
protection (HIGH risk): a bot interstitial is treated exactly like a login wall
— degrade to ``login_required``, never solve or bypass.

NOTE (live-probe 2026-07-07): an anonymous headless fetch of ``/s/{query}`` does
NOT reach a product-results page — Home Depot's bot management serves a 200 OK
error shell titled "Error Page" whose body reads "Oops!! Something went wrong.
Please refresh page" (setting ``_bman_adv`` / ``_abck`` bot cookies), and it
carries NONE of the classic Akamai "Access Denied" / "Reference #" markers.
Without a marker for this specific shell the page parsed to zero JSON-LD
products and reported a confusing "no hits parsed" instead of a clean bot-wall
degradation, so the challenge markers below include it. Reaching real products
needs a signed-in / less-bot-flagged session (browser_login).

Parse strategy: JSON-LD Product records (search + product pages both ship
them) once a real page IS reached; the model number doubles as the SKU;
product images live on thdstatic.com.
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
# Akamai / bot-management walls — never bypass, treat exactly like a login wall.
# "Oops!! Something went wrong. Please refresh page" is the 200-OK bot-management
# error shell served to anonymous headless traffic (live-probe 2026-07-07); the
# other markers cover the classic Akamai interstitial variants.
_CHALLENGE_MARKERS = ("Access Denied", "Reference #", "Request unsuccessful",
                      "Oops!! Something went wrong", "Please refresh page")


class HomeDepotAdapter(RetailAdapter):
    key = SITE
    base_url = BASE_URL
    search_url_tmpl = SEARCH_URL_TMPL
    license_note = LICENSE_NOTE
    login_wall_markers = _LOGIN_WALL_MARKERS
    challenge_markers = _CHALLENGE_MARKERS


ADAPTER = register(HomeDepotAdapter())
