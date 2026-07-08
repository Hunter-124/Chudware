"""services/browser/adapters/eightytwenty.py — 80/20 Inc. source adapter.

80/20 Inc. (8020.net) is the T-slot aluminum extrusion maker ("The Industrial
Erector Set") — profiles, brackets, and fasteners common in student-robotics
and rover frames. It runs on Magento with an aggressive bot-protection layer:
a plain headless visit to the catalog-search endpoint returns a challenge
interstitial (live-probe 2026-07-08), so this adapter degrades honestly to that
challenge state — it never tries to solve or bypass it. When the site serves a
real product page (e.g. under the user's own signed-in session), the standard
:class:`~._retail.RetailAdapter` JSON-LD/spec-table flow applies.

For most T-slot needs MISUMI's extrusion catalog (see ``misumi.py``) is reachable
without a challenge and is the recommended first stop; this adapter keeps 80/20
itself on the radar and surfaces its bot-wall state truthfully rather than
silently returning zero results.
"""
from __future__ import annotations

from . import register
from ._retail import RetailAdapter

SITE = "8020"
BASE_URL = "https://8020.net"
SEARCH_URL_TMPL = BASE_URL + "/catalogsearch/result/?q={query}"
LICENSE_NOTE = (
    "80/20 Inc. product data is reference-only link-out commerce data fetched "
    "under the user's own browser session for their own engineering reference; "
    "any datasheet/CAD remains the manufacturer's own document. This adapter "
    "never redistributes page content — only a small price/spec record copied "
    "with provenance (source URL + fetch timestamp). See contracts/license_debt.md."
)

# A "Sign In" header link is ordinary chrome, not a wall — keep markers narrow.
_LOGIN_WALL_MARKERS = ("Please sign in to view", "You must be logged in")
# Magento bot-protection interstitials. browser_fetch already classifies most
# challenge pages (services/browser/challenge.py) and returns them as an expected
# gated state before we get here; these are a belt-and-braces backstop.
_CHALLENGE_MARKERS = ("Access Denied", "Pardon Our Interruption", "Reference #",
                      "Request unsuccessful", "Incapsula")


class EightyTwentyAdapter(RetailAdapter):
    key = SITE
    base_url = BASE_URL
    search_url_tmpl = SEARCH_URL_TMPL
    license_note = LICENSE_NOTE
    login_wall_markers = _LOGIN_WALL_MARKERS
    challenge_markers = _CHALLENGE_MARKERS


ADAPTER = register(EightyTwentyAdapter())
