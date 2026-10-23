"""services/browser/adapters/jlcpcb.py — JLCPCB source adapter.

JLCPCB's parts library (LCSC-sourced parts used in JLC's SMT assembly
service) has no downloadable CAD file here — this adapter returns a
STRUCTURED price/stock record for ECAD BOM matching (feeds
``ecad_find_part``), copied from the parts-library page with provenance,
mirroring the amazon.py commerce shape. Quote/order flows on JLCPCB gate
behind a per-order terms click-through; this adapter never accepts that on
the user's behalf and surfaces :func:`terms_confirmation_required` instead.

NOTE: the regexes/markers below are a v1 best-effort shape, not verified
against a live logged-in JLCPCB session (the gated integration suite covers the real per-source
integration tests behind an env flag).
"""
from __future__ import annotations

import re

from .. import session as _session
from . import is_login_wall, login_required, provenance_envelope, register, terms_confirmation_required

SITE = "jlcpcb"
BASE_URL = "https://jlcpcb.com"
SEARCH_URL_TMPL = BASE_URL + "/parts/all-electronic-components?searchTxt={query}"
LICENSE_NOTE = (
    "JLCPCB/LCSC parts-library data (price, stock) is copied with provenance "
    "(source URL + fetch timestamp) for the user's own BOM/sourcing decision, "
    "never redistributed in bulk and never LLM-generated. Quote/order terms "
    "click-throughs are never auto-accepted. See contracts/license_debt.md."
)

_LOGIN_WALL_MARKERS = ('Sign In', 'id="login-email"', 'class="login-dialog"')
_TERMS_MARKERS = ('Accept the terms', 'class="order-terms"', 'agree to the Terms')
# Best-effort: JLCPCB parts-library result cards.
_RESULT_RE = re.compile(
    r'<a[^>]+class="[^"]*part-item[^"]*"[^>]+href="(?P<href>/partdetail/[^"?#]+)"[^>]*>'
    r'(?P<title>.*?)</a>', re.IGNORECASE | re.DOTALL)
_LCSC_RE = re.compile(r'<span[^>]*class="[^"]*lcsc-number[^"]*"[^>]*>(?P<lcsc>C\d+)</span>', re.IGNORECASE)
_PRICE_RE = re.compile(r'<span[^>]*class="[^"]*price[^"]*"[^>]*>\$(?P<price>[0-9,.]+)</span>', re.IGNORECASE)
_STOCK_RE = re.compile(r'<span[^>]*class="[^"]*stock[^"]*"[^>]*>(?P<stock>[0-9,]+)\s*(?:in stock)?</span>',
                       re.IGNORECASE)


def _strip_tags(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text).strip()


class JlcpcbAdapter:
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
        for m in _RESULT_RE.finditer(content):
            card = m.group("title")
            lcsc_match = _LCSC_RE.search(card)
            price_match = _PRICE_RE.search(card)
            stock_match = _STOCK_RE.search(card)
            hits.append({
                "title": _strip_tags(card),
                "url": BASE_URL + m.group("href"),
                "site": SITE,
                "lcsc": lcsc_match.group("lcsc") if lcsc_match else None,
                "price": ("$" + price_match.group("price")) if price_match else None,
                "stock": stock_match.group("stock") if stock_match else None,
            })
        return hits

    def acquire(self, result: dict) -> dict:
        url = result.get("url", "")
        if not url:
            return {"ok": False, "reason": "bad_result", "hint": "result has no 'url'"}

        page = _session.browser_fetch(url, SITE)
        if not page.get("ok"):
            return page
        content = page.get("content", "")
        if is_login_wall(content, _LOGIN_WALL_MARKERS):
            return login_required(SITE, url)
        if is_login_wall(content, _TERMS_MARKERS):
            return terms_confirmation_required(
                SITE, url, "JLCPCB requires accepting the order/quote terms for this part")

        lcsc_match = _LCSC_RE.search(content)
        price_match = _PRICE_RE.search(content)
        stock_match = _STOCK_RE.search(content)

        return provenance_envelope(
            source_url=url,
            session_profile_used=page.get("session_profile_used", SITE),
            license_note=LICENSE_NOTE,
            artifact_path=None,
            title=result.get("title", ""),
            lcsc=lcsc_match.group("lcsc") if lcsc_match else result.get("lcsc"),
            price=("$" + price_match.group("price")) if price_match else result.get("price"),
            stock=stock_match.group("stock") if stock_match else result.get("stock"),
        )


ADAPTER = register(JlcpcbAdapter())
