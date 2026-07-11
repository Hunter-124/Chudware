"""services/browser/adapters/dfrobot.py — DFRobot source adapter (Parts Library V2).

DFRobot (dfrobot.com) is a maker / robotics module vendor — SENxxxx sensors and
DFRxxxx boards/modules. Each product carries a per-product wiki datasheet, and
some parts ship STEP archives on GitHub off-site; this adapter treats DFRobot as
a commerce + datasheet source (price/spec record + the wiki/datasheet PDF), not
a CAD-model source. The default :class:`~._retail.RetailAdapter` flow applies:
JSON-LD for search, spec-table + ``find_datasheet_url`` grabbing the datasheet
PDF on the detail page. Open catalog — no login. Challenge pages degrade to
``login_required`` — never solved.

Live-investigated 2026-07-10 (STILL_BROKEN): DFRobot runs a Nuxt SPA over an
OpenCart backend. The guessed ``/search-adv/result?q=`` is a hard HTTP 404 on the
real network (WebFetch-confirmed), as is every SSR variant tried
(``/search?q=``, ``/search?keyword=``, ``index.php?route=product/search&...``).
robots.txt disallows ``/index.php?route=product/`` and sitemap.xml lists ONLY
``/product-<id>.html`` pages — there is NO server-rendered search route. Site
search is XHR-JSON only (an internal API not discoverable from the static DOM or
the ``window.__NUXT__`` payload), so this adapter cannot fetch results headless
today; ``search`` degrades to ``http_error`` until the XHR search endpoint is
captured from a live browser session. The homepage DOES server-render product
cards (``/product-<id>.html``), so a category-browse fallback is a possible
future path but is not a keyword search.
"""
from __future__ import annotations

from . import register
from ._retail import RetailAdapter

SITE = "dfrobot"
BASE_URL = "https://www.dfrobot.com"
SEARCH_URL_TMPL = BASE_URL + "/search-adv/result?q={query}"
LICENSE_NOTE = (
    "DFRobot product data is reference-only link-out commerce data, and any "
    "datasheet remains the manufacturer's own document, fetched here for the "
    "user's own engineering reference only. This adapter never redistributes or "
    "LLM-generates page content — only a small price/spec record copied with "
    "provenance (source URL + fetch timestamp) and the manufacturer's datasheet "
    "saved for the user. See contracts/license_debt.md."
)

_LOGIN_WALL_MARKERS = ("Sign In", "Log In", 'id="login"')
_CHALLENGE_MARKERS = ("Access Denied", "Reference #", "Pardon Our Interruption")


class DfrobotAdapter(RetailAdapter):
    key = SITE
    base_url = BASE_URL
    search_url_tmpl = SEARCH_URL_TMPL
    license_note = LICENSE_NOTE
    login_wall_markers = _LOGIN_WALL_MARKERS
    challenge_markers = _CHALLENGE_MARKERS


ADAPTER = register(DfrobotAdapter())
