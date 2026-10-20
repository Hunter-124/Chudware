"""services/browser/adapters/maxon.py — maxon source adapter (T-X7).

maxon (maxongroup.com) makes precision DC and BLDC motors, gearheads,
encoders and controllers. Search is public; per-motor STEP/native CAD
downloads sit behind the user's own maxon account (light login). `acquire`
lands the STEP model with provenance for part_search / part_place.

Live-verified 2026-07-10: the real search route is the Next.js shop endpoint
``/en-us/search?q=`` (HTTP 200; the old ``/maxon/view/search?q=`` guess 404'd).
The results page embeds its data in ``<script id="__NEXT_DATA__">`` at
``props.pageProps.productSearchData.products[].record`` — clean structured
records carrying ``Name``, ``Artikelnummer`` (part number), ``Deeplink``
(``/maxon/view/product/...``), ``Kategorie1..4`` and a ``Preis`` string
(``|USD~~$=34.56|...``). We parse that JSON (mirrors digikey_web.py + misumi.py),
not speculative CSS-class regex. The product CATALOG search is public — only the
per-product STEP/native CAD download needs the user's own maxon login — so the
login-wall markers apply to ``acquire`` (product/download pages), not the public
search JSON.
"""
from __future__ import annotations

import html
import json
import re

from .. import session as _session
from . import download_path, is_login_wall, login_required, provenance_envelope, register

SITE = "maxon"
BASE_URL = "https://www.maxongroup.com"
SEARCH_URL_TMPL = BASE_URL + "/en-us/search?q={query}"
LICENSE_NOTE = (
    "maxon CAD models and datasheets are fetched under the user's own "
    "authenticated maxon session for their own engineering use and are not "
    "redistributed. This adapter downloads only under the user's own "
    "authenticated maxon session. See contracts/license_debt.md."
)

# Login markers apply to acquire()'s product/download pages, not the public
# catalog search JSON.
_LOGIN_WALL_MARKERS = ('Log in to maxon', 'id="loginForm"', 'class="login-form"',
                       'Please sign in to download')

_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__"[^>]*>(?P<json>.*?)</script>', re.DOTALL)

# A product page's native CAD download link (STEP model or zip) — best-effort,
# used by acquire() only.
_DOWNLOAD_RE = re.compile(
    r'href="(?P<href>[^"]+\.(?:step|stp|zip)(?:\?[^"]*)?)"', re.IGNORECASE)


def _strip_tags(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text).strip()


def _usd_price(preis: str) -> str | None:
    """maxon ``Preis`` is a ``|CCY~~sym=amount|...`` string; pull the USD amount."""
    if not preis:
        return None
    m = re.search(r"USD~~\$?=([0-9][0-9,.]*)", preis)
    return m.group(1) if m else None


def _category(record: dict) -> str | None:
    cats = [record.get("Kategorie1"), record.get("Kategorie2"),
            record.get("Kategorie3"), record.get("Kategorie4")]
    cats = [c.strip() for c in cats if c and c.strip()]
    return " / ".join(cats) if cats else None


class MaxonAdapter:
    key = SITE

    def search(self, query: str) -> list[dict]:
        url = SEARCH_URL_TMPL.format(query=query.replace(" ", "+"))
        result = _session.browser_fetch(url, SITE)
        if not result.get("ok"):
            return [result]
        content = result.get("content", "")

        # Live-verified shape: results live in the embedded __NEXT_DATA__ JSON at
        # props.pageProps.productSearchData.products[].record. Parse the JSON; no
        # speculative CSS-class regex. Empty is honest.
        m = _NEXT_DATA_RE.search(content)
        if not m:
            return []
        try:
            data = json.loads(m.group("json"))
        except (ValueError, TypeError):
            return []
        products = (
            data.get("props", {})
            .get("pageProps", {})
            .get("productSearchData", {})
            .get("products", [])
        )
        hits: list[dict] = []
        for entry in products:
            record = entry.get("record") if isinstance(entry, dict) else None
            if not isinstance(record, dict):
                continue
            deeplink = record.get("Deeplink", "")
            name = record.get("Name")
            if not deeplink or not name:
                continue
            hit = {
                "ok": True,
                "title": html.unescape(name).strip(),
                "url": BASE_URL + deeplink if deeplink.startswith("/") else deeplink,
                "site": SITE,
                "manufacturer": "maxon",
            }
            pn = (record.get("Artikelnummer") or "").strip()
            if pn:
                hit["part_number"] = pn
            price = _usd_price(record.get("Preis", ""))
            if price:
                hit["price"] = price
            cat = _category(record)
            if cat:
                hit["category"] = cat
            hits.append(hit)
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

        match = _DOWNLOAD_RE.search(content)
        if not match:
            return {"ok": False, "reason": "no_download_link",
                    "hint": "could not find a STEP/native CAD download link on the product page"}
        download_url = match.group("href")
        if download_url.startswith("/"):
            download_url = BASE_URL + download_url

        dest, refusal = download_path(SITE, download_url, default="download.step")
        if refusal is not None:
            return refusal
        outcome = _session.browser_download(download_url, SITE, dest)
        if not outcome.get("ok"):
            return outcome

        return provenance_envelope(
            source_url=url,
            session_profile_used=outcome.get("session_profile_used", SITE),
            license_note=LICENSE_NOTE,
            artifact_path=outcome["dest"],
            title=result.get("title", ""),
            manufacturer=result.get("manufacturer", "maxon"),
        )


ADAPTER = register(MaxonAdapter())
