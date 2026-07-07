"""services/browser/adapters/futureelectronics.py — Future Electronics source adapter (Parts Library V2).

Future Electronics (futureelectronics.com) is a broadline electronics
distributor with an open catalog. This adapter captures a datasheet + price/stock
record; CAD models are available via a SnapMagic follow, but that pipeline is out
of scope here — this adapter only captures the manufacturer datasheet and the
commerce (price/stock) record. The default :class:`~._retail.RetailAdapter` flow
applies: JSON-LD for search, spec-table + ``find_datasheet_url`` grabbing the
datasheet PDF on the detail page. Challenge pages degrade to ``login_required`` —
never solved.

v1 best-effort, not live-verified (T-X6 gates live tests).
"""
from __future__ import annotations

from . import register
from ._retail import RetailAdapter

SITE = "futureelectronics"
BASE_URL = "https://www.futureelectronics.com"
SEARCH_URL_TMPL = BASE_URL + "/search/{query}"
LICENSE_NOTE = (
    "Future Electronics product data is reference-only link-out commerce data, "
    "and any datasheet remains the manufacturer's own document, fetched here for "
    "the user's own engineering reference only. This adapter never redistributes "
    "or LLM-generates page content — only a small price/stock/spec record copied "
    "with provenance (source URL + fetch timestamp) and the manufacturer's "
    "datasheet saved for the user. See contracts/license_debt.md."
)

_LOGIN_WALL_MARKERS = ("Sign In", "Log In", 'id="login"')
_CHALLENGE_MARKERS = ("Access Denied", "Reference #", "Pardon Our Interruption")


class FutureElectronicsAdapter(RetailAdapter):
    key = SITE
    base_url = BASE_URL
    search_url_tmpl = SEARCH_URL_TMPL
    license_note = LICENSE_NOTE
    login_wall_markers = _LOGIN_WALL_MARKERS
    challenge_markers = _CHALLENGE_MARKERS


ADAPTER = register(FutureElectronicsAdapter())
