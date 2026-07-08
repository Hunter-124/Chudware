"""services/browser/adapters/servocity.py — ServoCity source adapter (Parts Library V2).

ServoCity (servocity.com) is the maker of the goBILDA and Actobotics structural +
motion robotics ecosystems — channel, brackets, hubs, shafts, gearmotors, servos —
and a key student-robotics (FTC/FRC) structural + motion sourcing site. This
adapter treats ServoCity as a commerce + reference source (price/link-out record
with provenance), not a CAD-model source. Open catalog — no login for search.
Challenge pages degrade to ``login_required`` — never solved.

INVESTIGATION (search "gearmotor", HTTP 200, ~568KB static HTML): ServoCity is a
BigCommerce (Stencil) store whose search box is skinned by the Searchanise
("snize") widget. The static HTML carries BOTH ~100 ``card-title`` elements AND a
``snize-no-products-found`` marker — so the honest question is which is real. It
resolves cleanly in favour of the server HTML:

  * The 100 product cards are the ACTUAL server-rendered BigCommerce Stencil
    search grid — each a ``<li class="product">`` wrapping
    ``<a class="card" data-card-type="product" href=".../slug/?searchid=...&search_query=gearmotor" title="Premium N20 Gear Motor ...">``.
    The hrefs are search-scoped product URLs and the ``title=`` attribute is the
    real product name (the ``card-title`` div holds only the variant label,
    e.g. "900 RPM"). Every card names a real gearmotor matching the query, and
    all 100 carry a ``.price--withoutTax`` price ($12.99, …).
  * ``snize-no-products-found`` appears ONLY inside a Searchanise JS template
    string (``$(...).append(`<li class="snize-no-products-found">…`)``) — a dormant
    autocomplete-dropdown placeholder, never rendered on this page.

So :meth:`parse_search` is overridden to read the Stencil product grid directly
(no JS render needed). We deliberately do NOT lean on the default JSON-LD path:
this search page ships zero JSON-LD ``Product`` blocks.

This adapter never redistributes or LLM-generates page content — only a small
price/link record copied with provenance (source URL + fetch timestamp). See
LICENSE_NOTE.

v1 best-effort; parser verified offline against a saved live search page.
"""
from __future__ import annotations

import re
from urllib.parse import urljoin

from ._retail import RetailAdapter, _strip_tags
from . import register

SITE = "servocity"
BASE_URL = "https://www.servocity.com"
SEARCH_URL_TMPL = BASE_URL + "/search.php?search_query={query}"
LICENSE_NOTE = (
    "ServoCity (goBILDA / Actobotics) product data is reference-only link-out "
    "commerce data. This adapter never redistributes or LLM-generates page "
    "content — only a small price/link record copied with provenance (source URL "
    "+ fetch timestamp) for the user's own engineering reference. See "
    "contracts/license_debt.md."
)

# Narrow markers: ServoCity's public search page shows a "Sign in" account link
# in its header chrome, so we key off login-FORM / hard-wall markers only, never
# the generic word — otherwise every open catalog page would false-wall.
_LOGIN_WALL_MARKERS = ('id="login"', "Please sign in to view", "You must be logged in")
_CHALLENGE_MARKERS = ("Access Denied", "Reference #", "Pardon Our Interruption",
                      "Attention Required", "cf-error-details")

# Each server-rendered BigCommerce Stencil product card. The whole card is an
# <a class="card" ...> anchor; the real product name lives in its title= attr,
# not the card-title <div> (which holds only the variant label like "900 RPM").
_CARD_RE = re.compile(
    r'<a\b[^>]*\bclass="[^"]*\bcard\b[^"]*"[^>]*>', re.IGNORECASE | re.DOTALL
)
_HREF_RE = re.compile(r'\bhref="([^"]+)"', re.IGNORECASE)
_TITLE_ATTR_RE = re.compile(r'\btitle="([^"]+)"', re.IGNORECASE)
# li.product block boundaries — one product card each.
_LI_SPLIT_RE = re.compile(r'(?=<li\b[^>]*class="[^"]*\bproduct\b)', re.IGNORECASE)
_PRICE_RE = re.compile(
    r'class="price\s+price--withoutTax\s*"[^>]*>(.*?)</span>',
    re.IGNORECASE | re.DOTALL,
)
_CARD_TITLE_RE = re.compile(r'card-title[^>]*>(.*?)</', re.IGNORECASE | re.DOTALL)


class ServocityAdapter(RetailAdapter):
    key = SITE
    base_url = BASE_URL
    search_url_tmpl = SEARCH_URL_TMPL
    license_note = LICENSE_NOTE
    login_wall_markers = _LOGIN_WALL_MARKERS
    challenge_markers = _CHALLENGE_MARKERS

    def parse_search(self, content: str) -> list[dict]:
        """Server-rendered BigCommerce Stencil product grid (no JSON-LD here).

        Walks each ``<li class="product">`` block, pulls the ``<a class="card">``
        anchor's ``href`` + ``title`` (real product name) and the
        ``.price--withoutTax`` figure. Relative hrefs are made absolute. Emits
        ``ok:True`` hits carrying title, absolute url, site and price-when-present.
        """
        hits: list[dict] = []
        seen: set[str] = set()
        for block in _LI_SPLIT_RE.split(content or ""):
            if not block.lstrip().lower().startswith("<li"):
                continue
            card = _CARD_RE.search(block)
            if not card:
                continue
            tag = card.group(0)
            href_m = _HREF_RE.search(tag)
            if not href_m:
                continue
            url = urljoin(self.base_url, _strip_tags(href_m.group(1)).replace("&amp;", "&"))

            title_m = _TITLE_ATTR_RE.search(tag)
            title = _strip_tags(title_m.group(1)) if title_m else ""
            if not title:  # fall back to the variant-label card-title div
                ct = _CARD_TITLE_RE.search(block)
                title = _strip_tags(ct.group(1)) if ct else ""
            if not title:
                continue

            key = url.split("?", 1)[0]
            if key in seen:
                continue
            seen.add(key)

            hit: dict = {"ok": True, "title": title, "url": url, "site": self.key}
            pm = _PRICE_RE.search(block)
            if pm:
                price = _strip_tags(pm.group(1))
                if price and any(c.isdigit() for c in price):
                    hit["price"] = price
            hits.append(hit)
        return hits


ADAPTER = register(ServocityAdapter())
