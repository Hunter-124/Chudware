"""services/browser/adapters/digikey_web.py — DigiKey WEBSITE source adapter (T-X7).

The browser fallback for DigiKey (digikey.com) parts search — DISTINCT from
the Nexar/DigiKey API path in engine_oracle.parts_providers, which is the
preferred route when API credentials exist. This adapter scrapes the public
website under the user's own saved browser session instead: search returns
part cards with price/stock; `acquire` returns a structured price/stock
record plus datasheet + ECAD (symbol/footprint) links for ecad_find_part.

NOTE: the regexes/markers below are a v1 best-effort shape, not verified
against a live DigiKey session (T-X6 gates the real per-source integration
tests behind an env flag).
"""
from __future__ import annotations

import re

from .. import session as _session
from . import is_login_wall, login_required, provenance_envelope, register

SITE = "digikey"
BASE_URL = "https://www.digikey.com"
SEARCH_URL_TMPL = BASE_URL + "/en/products/result?keywords={query}"
LICENSE_NOTE = (
    "DigiKey website parts data (price, stock, datasheet links) is copied "
    "with provenance (source URL + fetch timestamp) for the user's own "
    "BOM/sourcing decision, never redistributed in bulk and never "
    "LLM-generated. ECAD symbol/footprint links point to DigiKey's partner "
    "services (e.g. SnapEDA/Ultra Librarian), whose own download terms apply. "
    "Prefer the DigiKey/Nexar API path when credentials exist. See "
    "contracts/license_debt.md."
)

_LOGIN_WALL_MARKERS = ('Sign In', 'id="signInName"', 'class="login-form"')
# Best-effort: DigiKey search result rows link to /en/products/detail/<...>.
_RESULT_RE = re.compile(
    r'<a[^>]+class="[^"]*product-title[^"]*"[^>]+href="(?P<href>/en/products/detail/[^"?#]+)"[^>]*>'
    r'(?P<title>.*?)</a>', re.IGNORECASE | re.DOTALL)
_DKPN_RE = re.compile(
    r'<span[^>]*class="[^"]*digikey-part-number[^"]*"[^>]*>(?P<dkpn>[^<]+)</span>', re.IGNORECASE)
_PRICE_RE = re.compile(r'<span[^>]*class="[^"]*price[^"]*"[^>]*>\$(?P<price>[0-9,.]+)</span>',
                       re.IGNORECASE)
_STOCK_RE = re.compile(
    r'<span[^>]*class="[^"]*stock[^"]*"[^>]*>(?P<stock>[0-9,]+)\s*(?:in stock)?</span>',
    re.IGNORECASE)
_DATASHEET_RE = re.compile(r'href="(?P<href>[^"]+\.pdf(?:\?[^"]*)?)"', re.IGNORECASE)
_ECAD_RE = re.compile(
    r'href="(?P<href>https?://(?:www\.)?(?:snapeda|ultralibrarian)\.com/[^"]+)"', re.IGNORECASE)


def _strip_tags(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text).strip()


class DigiKeyWebAdapter:
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
            dkpn_match = _DKPN_RE.search(card)
            price_match = _PRICE_RE.search(card)
            stock_match = _STOCK_RE.search(card)
            hits.append({
                "title": _strip_tags(card),
                "url": BASE_URL + m.group("href"),
                "site": SITE,
                "digikey_pn": dkpn_match.group("dkpn").strip() if dkpn_match else None,
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

        dkpn_match = _DKPN_RE.search(content)
        price_match = _PRICE_RE.search(content)
        stock_match = _STOCK_RE.search(content)

        datasheet_url = None
        ds_match = _DATASHEET_RE.search(content)
        if ds_match:
            datasheet_url = ds_match.group("href")
            if datasheet_url.startswith("/"):
                datasheet_url = BASE_URL + datasheet_url
        ecad_match = _ECAD_RE.search(content)

        return provenance_envelope(
            source_url=url,
            session_profile_used=page.get("session_profile_used", SITE),
            license_note=LICENSE_NOTE,
            artifact_path=None,
            title=result.get("title", ""),
            digikey_pn=dkpn_match.group("dkpn").strip() if dkpn_match else result.get("digikey_pn"),
            price=("$" + price_match.group("price")) if price_match else result.get("price"),
            stock=stock_match.group("stock") if stock_match else result.get("stock"),
            datasheet_url=datasheet_url,
            ecad_url=ecad_match.group("href") if ecad_match else None,
        )


ADAPTER = register(DigiKeyWebAdapter())
