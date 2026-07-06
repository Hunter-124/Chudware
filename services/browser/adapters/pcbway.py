"""services/browser/adapters/pcbway.py — PCBWay source adapter (T-X7).

PCBWay is a PCB fab + parts source, same shape as jlcpcb.py: no CAD file for
a parts-library hit, only a STRUCTURED price/stock record for ECAD BOM
matching, copied with provenance. Quote/order flows gate behind a
per-order terms click-through; this adapter never accepts that on the
user's behalf and surfaces :func:`terms_confirmation_required` instead.

NOTE: the regexes/markers below are a v1 best-effort shape, not verified
against a live logged-in PCBWay session (T-X6 gates the real per-source
integration tests behind an env flag).
"""
from __future__ import annotations

import re

from .. import session as _session
from . import is_login_wall, login_required, provenance_envelope, register, terms_confirmation_required

SITE = "pcbway"
BASE_URL = "https://www.pcbway.com"
SEARCH_URL_TMPL = BASE_URL + "/project/shopindex/{query}.html"
LICENSE_NOTE = (
    "PCBWay parts/fab data (price, stock) is copied with provenance (source "
    "URL + fetch timestamp) for the user's own BOM/sourcing decision, never "
    "redistributed in bulk and never LLM-generated. Quote/order terms "
    "click-throughs are never auto-accepted. See contracts/license_debt.md."
)

_LOGIN_WALL_MARKERS = ('Sign In', 'id="login-email"', 'class="login-panel"')
_TERMS_MARKERS = ('Accept the terms', 'class="order-terms"', 'agree to the Terms')
# Best-effort: PCBWay parts/shop result cards.
_RESULT_RE = re.compile(
    r'<a[^>]+class="[^"]*shop-item[^"]*"[^>]+href="(?P<href>/project/[^"?#]+)"[^>]*>'
    r'(?P<card>.*?)</a>', re.IGNORECASE | re.DOTALL)
_TITLE_RE = re.compile(r'^(?P<title>.*?)(?:<span|$)', re.IGNORECASE | re.DOTALL)
_PRICE_RE = re.compile(r'<span[^>]*class="[^"]*price[^"]*"[^>]*>\$(?P<price>[0-9,.]+)</span>', re.IGNORECASE)
_STOCK_RE = re.compile(r'<span[^>]*class="[^"]*stock[^"]*"[^>]*>(?P<stock>[0-9,]+)\s*(?:in stock)?</span>',
                       re.IGNORECASE)


def _strip_tags(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text).strip()


class PcbwayAdapter:
    key = SITE

    def search(self, query: str) -> list[dict]:
        url = SEARCH_URL_TMPL.format(query=query.replace(" ", "-"))
        result = _session.browser_fetch(url, SITE)
        if not result.get("ok"):
            return [result]
        content = result.get("content", "")
        if is_login_wall(content, _LOGIN_WALL_MARKERS):
            return [login_required(SITE, url)]
        hits = []
        for m in _RESULT_RE.finditer(content):
            card = m.group("card")
            title_match = _TITLE_RE.match(card)
            price_match = _PRICE_RE.search(card)
            stock_match = _STOCK_RE.search(card)
            hits.append({
                "title": _strip_tags(title_match.group("title") if title_match else card),
                "url": BASE_URL + m.group("href"),
                "site": SITE,
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
                SITE, url, "PCBWay requires accepting the order/quote terms for this item")

        price_match = _PRICE_RE.search(content)
        stock_match = _STOCK_RE.search(content)

        return provenance_envelope(
            source_url=url,
            session_profile_used=page.get("session_profile_used", SITE),
            license_note=LICENSE_NOTE,
            artifact_path=None,
            title=result.get("title", ""),
            price=("$" + price_match.group("price")) if price_match else result.get("price"),
            stock=stock_match.group("stock") if stock_match else result.get("stock"),
        )


ADAPTER = register(PcbwayAdapter())
