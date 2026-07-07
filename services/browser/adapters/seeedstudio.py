"""services/browser/adapters/seeedstudio.py — Seeed Studio source adapter (Parts Library V2).

Seeed Studio (seeedstudio.com) is a maker / IoT hardware vendor. Much of its
catalog is open-source hardware, so CAD models are usually linked off-site
(GrabCAD) rather than hosted here; this adapter treats Seeed as a commerce +
datasheet source (price/spec record + wiki datasheet PDF), not a CAD-model
source. The default :class:`~._retail.RetailAdapter` flow applies: JSON-LD for
search, spec-table + ``find_datasheet_url`` grabbing the datasheet PDF on the
detail page. Open catalog — no login. Challenge pages degrade to
``login_required`` — never solved.

v1 best-effort, not live-verified (T-X6 gates live tests).
"""
from __future__ import annotations

from . import register
from ._retail import RetailAdapter

SITE = "seeedstudio"
BASE_URL = "https://www.seeedstudio.com"
SEARCH_URL_TMPL = BASE_URL + "/catalogsearch/result/?q={query}"
LICENSE_NOTE = (
    "Seeed Studio product data is reference-only link-out commerce data, and any "
    "datasheet remains the manufacturer's own document, fetched here for the "
    "user's own engineering reference only. This adapter never redistributes or "
    "LLM-generates page content — only a small price/spec record copied with "
    "provenance (source URL + fetch timestamp) and the manufacturer's datasheet "
    "saved for the user. See contracts/license_debt.md."
)

_LOGIN_WALL_MARKERS = ("Sign In", "Log In", 'id="login"')
_CHALLENGE_MARKERS = ("Access Denied", "Reference #", "Pardon Our Interruption")


class SeeedStudioAdapter(RetailAdapter):
    key = SITE
    base_url = BASE_URL
    search_url_tmpl = SEARCH_URL_TMPL
    license_note = LICENSE_NOTE
    login_wall_markers = _LOGIN_WALL_MARKERS
    challenge_markers = _CHALLENGE_MARKERS


ADAPTER = register(SeeedStudioAdapter())
