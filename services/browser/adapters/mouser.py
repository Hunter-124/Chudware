"""Mouser's official Search API, with a public JSON-LD website fallback.

The anonymous website has returned an Akamai Access Denied wall; it is not a
customer sign-in page. Datasheets are documents, not CAD models.
"""
from __future__ import annotations

from . import fail, register
from . import _vendor_api
from ._retail import RetailAdapter

SITE = "mouser"
BASE_URL = "https://www.mouser.com"
# Mouser's catalog keyword search. Live-verified Akamai-walled 2026-07-11; kept as
# the canonical search URL so that, under a user's own (challenge-cleared) session,
# a reachable catalog page parses via JSON-LD rather than a speculative CSS regex.
SEARCH_URL_TMPL = BASE_URL + "/c/?q={query}"
LICENSE_NOTE = (
    "Mouser product data (price, stock) is copied with provenance (source URL + "
    "fetch timestamp) for the user's own BOM/sourcing decision, never "
    "redistributed in bulk and never LLM-generated. Datasheets remain the "
    "component manufacturer's own documents, fetched for the user's own "
    "engineering reference. ECAD/3D model links point to Mouser's partner "
    "services (SnapMagic/Ultra Librarian), whose own download terms apply. "
    "See contracts/license_debt.md."
)

# Actual login forms only; a navigation link is not a sign-in wall.
_LOGIN_WALL_MARKERS = ('id="signInName"', 'class="login-form"')
# The Akamai Access Denied shell is a block, not a login request.
_CHALLENGE_MARKERS = (
    "Access to this page has been denied",
    "Access Denied",
    'id="m-captcha"',
)


class MouserAdapter(RetailAdapter):
    key = SITE
    hosts = ("mouser.com",)
    result_kinds = ("product",)
    cad_acquisition = False
    login_required = False
    api = True
    base_url = BASE_URL
    search_url_tmpl = SEARCH_URL_TMPL
    license_note = LICENSE_NOTE
    login_wall_markers = _LOGIN_WALL_MARKERS
    challenge_markers = _CHALLENGE_MARKERS

    def search(self, query: str) -> list[dict]:
        creds = _vendor_api.credentials("mouser")
        if creds:
            return _vendor_api.search(SITE, query, creds)
        return super().search(query)

    def acquire(self, result: dict) -> dict:
        if result.get("kind") not in (None, "product"):
            return fail(SITE, "acquire", "unsupported", "Select a specific product first.",
                        url=result.get("url"))
        if result.get("datasheet_url") or result.get("datasheet"):
            return _vendor_api.acquire_datasheet(SITE, result, LICENSE_NOTE)
        outcome = super().acquire(result)
        if outcome.get("ok") and not outcome.get("artifact_path"):
            return fail(SITE, "acquire", "unsupported", "No product datasheet was found.",
                        url=result.get("url"))
        return outcome


ADAPTER = register(MouserAdapter())
