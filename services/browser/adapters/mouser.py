"""services/browser/adapters/mouser.py — Mouser Electronics source adapter (Parts Library V2).

Mouser (mouser.com) is a broadline electronics distributor with datasheets,
price/stock, and ECAD/3D models (via SnapMagic/Ultra Librarian follows). The
parts taxonomy routes ``electronics.thermal`` (and, more broadly, electronics
sourcing) to this adapter, so it must exist as a registered adapter — the
``test_every_routed_site_is_a_registered_adapter`` guard fails otherwise.

LIVE-VERIFIED 2026-07-11 (CEF headless, ``settle_ms=10000``):

* The real catalog keyword search — ``/c/?q=heatsink`` — is Akamai bot-walled.
  An anonymous headless fetch returns HTTP 200 with
  ``<title>Access to this page has been denied.</title>`` +
  ``<h1>Access Denied</h1>`` and an ``m-captcha`` div ("we believe you are using
  automation tools to browse the website"). This is the same Akamai edge layer
  the retail wave hits (lowes/tractorsupply), so the sanctioned, honest behavior
  is to degrade to ``login_required`` — NEVER solve or bypass the challenge, and
  never silently parse the block shell to an empty result list ("no hits parsed").
* The alternate ``/ProductSearch/Refine?Keyword=...`` endpoint returns a ~190 KB
  JS chrome shell (mini-cart table headers, no server-rendered product rows —
  results are loaded via XHR after the page's JS runs), so it yields no parseable
  hits from a static fetch either.

This adapter therefore reuses the shared :class:`~._retail.RetailAdapter` flow:
fetch → challenge/login-wall degradation (to ``login_required``) → JSON-LD parse
for the day the catalog is reachable under the user's own session. ``acquire``
inherits the datasheet-only behavior (JSON-LD price/stock + the manufacturer
datasheet PDF); no CAD/model-download path is attempted here (out of scope —
the SnapMagic/Ultra Librarian ECAD follow is a separate pipeline).
"""
from __future__ import annotations

from . import register
from ._retail import RetailAdapter, parse_jsonld_products

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

# Mouser has individual customer accounts (a real login exists), so a sign-in wall
# degrades to login_required — same as the challenge wall below.
_LOGIN_WALL_MARKERS = ("Sign In", "Log In", 'id="signInName"')
# Live-verified 2026-07-11: Akamai "Access Denied" block shell served with HTTP 200
# for anonymous headless catalog fetches. Treated exactly like a login wall.
_CHALLENGE_MARKERS = (
    "Access to this page has been denied",
    "Access Denied",
    'id="m-captcha"',
)


class MouserAdapter(RetailAdapter):
    key = SITE
    base_url = BASE_URL
    search_url_tmpl = SEARCH_URL_TMPL
    license_note = LICENSE_NOTE
    login_wall_markers = _LOGIN_WALL_MARKERS
    challenge_markers = _CHALLENGE_MARKERS

    def parse_search(self, content: str) -> list[dict]:
        """Parse Mouser catalog results from JSON-LD ``Product`` records.

        Mouser (when reachable under the user's own session) ships JSON-LD for
        SEO — the same stable, sanctioned machine-readable layer the retail base
        parses. A speculative CSS-class regex is deliberately avoided (the
        digikey_web "no hits parsed" lesson). An empty list is honest: no
        products found, not a fabricated hit. The Akamai challenge is caught in
        :meth:`RetailAdapter.search` before this is ever reached.
        """
        return [self._hit_from_product(p) for p in parse_jsonld_products(content)
                if p.get("title") and p.get("url")]


ADAPTER = register(MouserAdapter())
