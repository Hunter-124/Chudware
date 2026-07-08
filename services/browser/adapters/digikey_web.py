"""services/browser/adapters/digikey_web.py — DigiKey WEBSITE source adapter (T-X7).

The browser fallback for DigiKey (digikey.com) parts search — DISTINCT from
the Nexar/DigiKey API path in engine_oracle.parts_providers, which is the
preferred route when API credentials exist. This adapter browses the public
website under the user's own saved browser session instead: search returns
part cards with price/stock; `acquire` returns a structured price/stock
record plus datasheet + ECAD (symbol/footprint) links for ecad_find_part.

NOTE (live-verified 2026-07-07, fetch of
``/en/products/result?keywords=resistor``): DigiKey's results page is a
Next.js app that embeds its data in
``<script id="__NEXT_DATA__" type="application/json">``. The verified live
shape is ``props.pageProps.envelope.data.topResults`` — for a broad/ambiguous
keyword this is a list of ~22 CATEGORY tiles
(``categoryName``/``categoryUrl``/``parentCategory``/``productCount``/``id``),
i.e. a disambiguation page, NOT product cards. There are no manufacturer /
DigiKey part-number / price fields anywhere in the JSON for this page; the old
speculative CSS-class regexes (``product-title``, ``digikey-part-number``,
``price``) never matched the live DOM ("no hits parsed" bug). ``search`` now
parses the embedded JSON and returns those tiles as typed ``kind="category"``
results (honest: navigable catalog sections to drill into, not purchasable
SKUs) — mirroring the McMaster adapter's identical category-navigation case.
Reaching actual SKUs (and their part numbers/price/datasheet/ECAD links)
requires following ``categoryUrl`` — a per-site follow-up tracked separately.
Whether a NARROW query (a specific MPN) returns a product-results array under a
different ``envelope.data`` key was NOT observed in this fixture. ``acquire``
below is left as-is (best-effort detail-page shape, not part of this rewrite).
"""
from __future__ import annotations

import html
import json
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
# Live-verified 2026-07-07: DigiKey embeds its results data in a Next.js
# __NEXT_DATA__ JSON blob. Extract the raw JSON here, then read
# props.pageProps.envelope.data.topResults from it (see module docstring).
_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__"[^>]*>(?P<json>.*?)</script>', re.DOTALL)
# --- acquire() detail-page markers (best-effort, unchanged / not rewritten) ---
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

        # Live-verified shape: results live in the embedded __NEXT_DATA__ JSON at
        # props.pageProps.envelope.data.topResults (a list of category tiles for a
        # broad/ambiguous keyword — a disambiguation page). Parse the JSON; no
        # speculative CSS-class regex. Empty is honest.
        m = _NEXT_DATA_RE.search(content)
        if not m:
            return []
        try:
            data = json.loads(m.group("json"))
        except (ValueError, TypeError):
            return []
        top_results = (
            data.get("props", {})
            .get("pageProps", {})
            .get("envelope", {})
            .get("data", {})
            .get("topResults", [])
        )
        hits = []
        for entry in top_results:
            if not isinstance(entry, dict):
                continue
            rel = entry.get("categoryUrl", "")
            name = entry.get("categoryName")
            if not rel or not name:
                continue
            hits.append({
                "title": html.unescape(name).strip(),
                "url": BASE_URL + rel if rel.startswith("/") else rel,
                "site": SITE,
                "kind": "category",   # navigable catalog section, not a purchasable SKU
                "parent_category": entry.get("parentCategory"),
                "product_count": entry.get("productCount"),
                "category_id": entry.get("id"),
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
