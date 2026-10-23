"""services/browser/adapters/cadenas.py — CADENAS PARTcommunity / 3Dfindit adapter.

3Dfindit.com / PARTcommunity is the CADENAS white-label CAD portal engine behind
thousands of manufacturer catalogs — the STEP/native geometry only emerges after
driving a JavaScript part configurator (and usually a login). This v1 CANNOT drive
that interactive configurator headlessly, so it is HONEST about the limitation:

- `search` parses the server-rendered result grid: each product is an
  `<a class="gridCardBody" href="/en/digitaltwin/...">` card carrying a
  `.part-nn-name` id, a `.text` human title, a `img.nodeImage` thumbnail and a
  `img.supplierImage title="<Manufacturer>"`. Manufacturer/directory index cards
  (`/en/cad-bim-library/manufacturer/...`) are also surfaced as candidate links.
- `acquire` does NOT attempt to fake a download: it degrades to login_required
  (or terms_confirmation_required) because the actual STEP artifact is gated
  behind interactive configuration this adapter cannot perform. When and if a
  configurator-driving capability lands, this is the seam to upgrade.

NOTE: the search selectors were verified 2026-07-08 against a saved live
3dfindit.com results page (query "bearing", HTTP 200). The product data is
present in the initial HTML (no __NEXT_DATA__; the JSON-LD block is only site
metadata) as `.gridCardBody` cards inside the `resultNodes` grid.
"""
from __future__ import annotations

import re

from .. import session as _session
from . import (is_login_wall, login_required, register, terms_confirmation_required)

SITE = "cadenas"
BASE_URL = "https://www.3dfindit.com"
SEARCH_URL_TMPL = BASE_URL + "/en/search?q={query}"
LICENSE_NOTE = (
    "CADENAS PARTcommunity / 3Dfindit CAD models are supplied by manufacturer "
    "catalogs to registered users under each manufacturer's terms, produced on "
    "demand by an interactive configurator. This adapter records provenance but "
    "does not download: the STEP only emerges after interactive configuration "
    "and login that this v1 cannot perform. See contracts/license_debt.md."
)
_TERMS_SUMMARY = (
    "the STEP model is generated only after you drive the manufacturer's part "
    "configurator and accept its terms; this adapter cannot do that on your behalf"
)

_LOGIN_WALL_MARKERS = ('Please log in', 'id="id_username"', 'class="login-form"',
                       'Sign in to download', 'Login required')
# Server-rendered result grid: each hit is <a class="gridCardBody" href="...">...</a>.
# Product cards link to /en/digitaltwin/<slug>?path=...; manufacturer/directory
# index cards link to /en/cad-bim-library/manufacturer/<slug>. Both are useful
# candidate links.
_CARD_RE = re.compile(
    r'<a\b[^>]*class="[^"]*gridCardBody[^"]*"[^>]*href="(?P<href>/en/[^"?#]+(?:\?[^"]*)?)"[^>]*>'
    r'(?P<card>.*?)</a>', re.IGNORECASE | re.DOTALL)
# Human-friendly title inside a product card, then the machine part-id fallback.
_TEXT_RE = re.compile(r'<div[^>]*class="[^"]*\btext\b[^"]*"[^>]*>(?P<t>.*?)</div>',
                      re.IGNORECASE | re.DOTALL)
_NNNAME_RE = re.compile(r'<div[^>]*class="[^"]*part-nn-name[^"]*"[^>]*>(?P<t>.*?)</div>',
                        re.IGNORECASE | re.DOTALL)
_THUMB_RE = re.compile(r'<img[^>]*class="[^"]*nodeImage[^"]*"[^>]*src="(?P<src>[^"]+)"',
                       re.IGNORECASE)
_SUPPLIER_RE = re.compile(r'<img[^>]*class="[^"]*supplierImage[^"]*"[^>]*title="(?P<mfr>[^"]+)"',
                          re.IGNORECASE)


def _strip_tags(text: str) -> str:
    # Drop <mark> search-highlight and any other markup, unescape common entities.
    text = re.sub(r"<[^>]+>", "", text)
    for a, b in (("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"),
                 ("&quot;", '"'), ("&#39;", "'"), ("&nbsp;", " ")):
        text = text.replace(a, b)
    return text.strip()


class CadenasAdapter:
    key = SITE

    def search(self, query: str) -> list[dict]:
        url = SEARCH_URL_TMPL.format(query=query.replace(" ", "+"))
        result = _session.browser_fetch(url, SITE)
        if not result.get("ok"):
            return [result]
        content = result.get("content", "")
        if is_login_wall(content, _LOGIN_WALL_MARKERS):
            return [login_required(SITE, url)]
        hits = []
        for m in _CARD_RE.finditer(content):
            href = m.group("href")
            card = m.group("card")
            is_product = "/digitaltwin/" in href
            # Title: human .text for products, else the machine part-id, else slug.
            title = ""
            tm = _TEXT_RE.search(card) or _NNNAME_RE.search(card)
            if tm:
                title = _strip_tags(tm.group("t"))
            if not title:
                # Manufacturer/directory index cards carry their name as plain text.
                title = _strip_tags(card)
            if not title:
                title = href.rsplit("/", 1)[-1].split("?", 1)[0]

            hit = {
                "title": title,
                "url": BASE_URL + href,
                "site": SITE,
                "kind": "part" if is_product else "catalog",
            }
            thumb = _THUMB_RE.search(card)
            if thumb:
                hit["thumbnail"] = _strip_tags(thumb.group("src"))
            sup = _SUPPLIER_RE.search(card)
            if sup:
                hit["manufacturer"] = _strip_tags(sup.group("mfr"))
            nn = _NNNAME_RE.search(card)
            if nn:
                pn = _strip_tags(nn.group("t"))
                if pn:
                    hit["part_number"] = pn
            hits.append(hit)
        return hits

    def acquire(self, result: dict) -> dict:
        url = result.get("url", "")
        if not url:
            return {"ok": False, "reason": "bad_result", "hint": "result has no 'url'"}

        page = _session.browser_fetch(url, SITE)
        if page.get("ok"):
            content = page.get("content", "")
            if is_login_wall(content, _LOGIN_WALL_MARKERS):
                return login_required(SITE, url)

        # Honest degradation: the STEP artifact is gated behind an interactive
        # JS configurator (and terms click-through) this v1 cannot drive.
        return terms_confirmation_required(SITE, url, _TERMS_SUMMARY)


ADAPTER = register(CadenasAdapter())
