"""services/browser/adapters/lcsc.py — LCSC source adapter (T-X7).

LCSC (lcsc.com) is the electronic-components distributor behind JLCPCB's
parts library. Search/pricing/stock pages are public (no login for browse);
bulk pricing/order flows need the user's own account. Like jlcpcb.py, this
adapter's `acquire` returns a STRUCTURED price/stock record for ECAD BOM
matching — plus the part's datasheet PDF when the detail page links one
(the one downloadable artifact LCSC exposes here).

NOTE: the regexes/markers below are a v1 best-effort shape, not verified
against a live LCSC session (T-X6 gates the real per-source integration
tests behind an env flag).
"""
from __future__ import annotations

import re

from .. import session as _session
from . import download_dir, is_login_wall, login_required, provenance_envelope, register

SITE = "lcsc"
BASE_URL = "https://www.lcsc.com"
SEARCH_URL_TMPL = BASE_URL + "/search?q={query}"
LICENSE_NOTE = (
    "LCSC parts data (price, stock) is copied with provenance (source URL + "
    "fetch timestamp) for the user's own BOM/sourcing decision, never "
    "redistributed in bulk and never LLM-generated. Datasheets remain the "
    "component manufacturer's documents, fetched for the user's own "
    "engineering reference. See contracts/license_debt.md."
)

_LOGIN_WALL_MARKERS = ('Sign In', 'id="login-email"', 'class="login-dialog"')
# Best-effort: LCSC search result cards link to /product-detail/<slug>.html.
_RESULT_RE = re.compile(
    r'<a[^>]+class="[^"]*product-item[^"]*"[^>]+href="(?P<href>/product-detail/[^"?#]+)"[^>]*>'
    r'(?P<title>.*?)</a>', re.IGNORECASE | re.DOTALL)
_LCSC_RE = re.compile(r'<span[^>]*class="[^"]*lcsc-number[^"]*"[^>]*>(?P<lcsc>C\d+)</span>',
                      re.IGNORECASE)
_PRICE_RE = re.compile(r'<span[^>]*class="[^"]*price[^"]*"[^>]*>\$(?P<price>[0-9,.]+)</span>',
                       re.IGNORECASE)
_STOCK_RE = re.compile(
    r'<span[^>]*class="[^"]*stock[^"]*"[^>]*>(?P<stock>[0-9,]+)\s*(?:in stock)?</span>',
    re.IGNORECASE)
_DATASHEET_RE = re.compile(r'href="(?P<href>[^"]+\.pdf(?:\?[^"]*)?)"', re.IGNORECASE)


def _strip_tags(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text).strip()


class LcscAdapter:
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

        lcsc_match = _LCSC_RE.search(content)
        price_match = _PRICE_RE.search(content)
        stock_match = _STOCK_RE.search(content)

        # Datasheet PDF is the one downloadable artifact; its absence is not an
        # error — the structured price/stock record is still the deliverable.
        artifact_path = None
        datasheet_url = None
        ds_match = _DATASHEET_RE.search(content)
        if ds_match:
            datasheet_url = ds_match.group("href")
            if datasheet_url.startswith("/"):
                datasheet_url = BASE_URL + datasheet_url
            filename = datasheet_url.rsplit("/", 1)[-1].split("?", 1)[0] or "datasheet.pdf"
            dest = download_dir(SITE) / filename
            outcome = _session.browser_download(datasheet_url, SITE, dest)
            if outcome.get("ok"):
                artifact_path = outcome["dest"]

        return provenance_envelope(
            source_url=url,
            session_profile_used=page.get("session_profile_used", SITE),
            license_note=LICENSE_NOTE,
            artifact_path=artifact_path,
            title=result.get("title", ""),
            lcsc=lcsc_match.group("lcsc") if lcsc_match else result.get("lcsc"),
            price=("$" + price_match.group("price")) if price_match else result.get("price"),
            stock=stock_match.group("stock") if stock_match else result.get("stock"),
            datasheet_url=datasheet_url,
        )


ADAPTER = register(LcscAdapter())
