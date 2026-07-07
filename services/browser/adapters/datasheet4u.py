"""services/browser/adapters/datasheet4u.py — Datasheet4U source adapter (Parts Library V2).

Datasheet4U (datasheet4u.com) is a pure datasheet DATABASE — roughly 900k parts
served as static, ad-heavy HTML with direct PDF downloads. There is no commerce
layer: ``search`` returns part rows and ``acquire`` downloads the datasheet PDF.
This source feeds the reverse-engineering spec pipeline (a manufacturer datasheet
is often the only formal geometry/pinout reference for an unknown part).

The default :class:`~._retail.RetailAdapter` JSON-LD parse will usually find
nothing here — that is expected and degrades to an empty record. The key
deliverable is ``find_datasheet_url`` grabbing the PDF on the detail page. Open
catalog — no login. Challenge pages degrade to ``login_required`` — never solved.

v1 best-effort, not live-verified (T-X6 gates live tests).
"""
from __future__ import annotations

from . import register
from ._retail import RetailAdapter

SITE = "datasheet4u"
BASE_URL = "https://www.datasheet4u.com"
SEARCH_URL_TMPL = BASE_URL + "/search?q={query}"
LICENSE_NOTE = (
    "Datasheets served by Datasheet4U remain the respective manufacturers' own "
    "documents, fetched here for the user's own engineering reference only. This "
    "adapter never redistributes or LLM-generates page content — only a small "
    "part record copied with provenance (source URL + fetch timestamp) and the "
    "manufacturer's datasheet saved for the user. See contracts/license_debt.md."
)

_LOGIN_WALL_MARKERS = ("Sign In", "Log In", 'id="login"')
_CHALLENGE_MARKERS = ("Access Denied", "Reference #", "Pardon Our Interruption")


class Datasheet4uAdapter(RetailAdapter):
    key = SITE
    base_url = BASE_URL
    search_url_tmpl = SEARCH_URL_TMPL
    license_note = LICENSE_NOTE
    login_wall_markers = _LOGIN_WALL_MARKERS
    challenge_markers = _CHALLENGE_MARKERS


ADAPTER = register(Datasheet4uAdapter())
