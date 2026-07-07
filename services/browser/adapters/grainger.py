"""services/browser/adapters/grainger.py — Grainger source adapter (Parts Library V2).

Commerce/MRO catalog with the richest spec tables of the retail wave: product
pages carry full ``<th>/<td>`` spec grids (milling dia, flutes, coating, ...)
plus datasheet/SDS PDF links. ``acquire`` therefore emits canonical machining
params (:func:`~._retail.normalize_machining_params`) for cutting tools and
downloads the first qualifying datasheet/SDS PDF alongside the record.

Parse strategy: JSON-LD Products for search; spec table + JSON-LD on the
product page. Challenge pages degrade to ``login_required`` — never solved.
"""
from __future__ import annotations

from . import register
from ._retail import RetailAdapter

SITE = "grainger"
BASE_URL = "https://www.grainger.com"
SEARCH_URL_TMPL = BASE_URL + "/search?searchQuery={query}"
LICENSE_NOTE = (
    "Grainger product data: reference-only, link-out commerce data. This "
    "adapter never stores or redistributes Grainger's page content — only a "
    "small price/spec record copied with provenance (source URL + fetch "
    "timestamp), plus vendor-published datasheet/SDS PDFs fetched under the "
    "user's own session for their own use. See contracts/license_debt.md."
)

_LOGIN_WALL_MARKERS = ("Sign In", 'id="signInEmail"', "Create Account to See Your Price")
# "Whoops, we couldn't find that." is Grainger's edge/bot-block page (served with
# HTTP 200 for the homepage and every search URL alike when traffic looks
# non-residential/automated) — confirmed via a live probe 2026-07-07. Without this
# marker the block page silently parses to zero JSON-LD products and reports a
# confusing "no hits parsed" error instead of a clean, expected bot-wall degradation.
_CHALLENGE_MARKERS = ("Access Denied", "Reference #", "Pardon Our Interruption",
                      "Whoops, we couldn't find that", "Whoops, we couldn&#39;t find that")


class GraingerAdapter(RetailAdapter):
    key = SITE
    base_url = BASE_URL
    search_url_tmpl = SEARCH_URL_TMPL
    license_note = LICENSE_NOTE
    login_wall_markers = _LOGIN_WALL_MARKERS
    challenge_markers = _CHALLENGE_MARKERS


ADAPTER = register(GraingerAdapter())
