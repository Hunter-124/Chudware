"""services/browser/adapters/igus.py — igus source adapter (T-X7).

igus (igus.com) makes plain & linear bearings, energy chains (e-chains) and
polymer motion components, with a native "3-step" CAD downloader (STEP).
Search is public; `acquire` lands the STEP model with provenance for
part_search / part_place.

NOTE (live-probe 2026-07-08): the igus search results page is a Next.js app
whose product results are embedded server-side in a
``<script id="__NEXT_DATA__" type="application/json">`` blob. Product results
live at JSON path ``props.pageProps.initialData.products.data`` (a list of
product dicts; ~16 shown, ``total`` up to 250). Each entry carries ``title``,
``productUrl`` (already an absolute https igus URL), ``sku`` (the series/id),
and ``imageUrl``. ``search`` now parses that JSON blob (verified against the
real saved page) instead of the old speculative ``product-title`` CSS-class
regex, which never matched the live DOM.

The ``acquire`` path still parses static catalog part pages and degrades to
``login_required`` when the CAD sits behind the JS configurator; those
regexes/markers remain a best-effort shape, not live-verified.
"""
from __future__ import annotations

import json
import re

from .. import session as _session
from . import download_path, is_login_wall, login_required, provenance_envelope, register

SITE = "igus"
BASE_URL = "https://www.igus.com"
SEARCH_URL_TMPL = BASE_URL + "/search?q={query}"
LICENSE_NOTE = (
    "igus CAD models and datasheets are fetched under the user's own "
    "authenticated igus session for their own engineering use and are not "
    "redistributed. This adapter downloads only under the user's own "
    "authenticated igus session. See contracts/license_debt.md."
)

_LOGIN_WALL_MARKERS = ('Log in to igus', 'id="loginForm"', 'class="login-form"',
                       'Please sign in to download', 'Configure this part')
# Live shape (2026-07-08): results are embedded in the Next.js __NEXT_DATA__ blob.
_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(?P<json>.*?)</script>',
    re.DOTALL)
_MFR_RE = re.compile(r'<span[^>]*class="[^"]*manufacturer[^"]*"[^>]*>(?P<mfr>[^<]+)</span>',
                     re.IGNORECASE)
# Best-effort: a static catalog part page's native CAD download link (STEP model or zip).
_DOWNLOAD_RE = re.compile(
    r'href="(?P<href>[^"]+\.(?:step|stp|zip)(?:\?[^"]*)?)"', re.IGNORECASE)


def _strip_tags(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text).strip()


class IgusAdapter:
    key = SITE

    def search(self, query: str) -> list[dict]:
        url = SEARCH_URL_TMPL.format(query=query.replace(" ", "+"))
        result = _session.browser_fetch(url, SITE)
        if not result.get("ok"):
            return [result]
        content = result.get("content", "")
        if is_login_wall(content, _LOGIN_WALL_MARKERS):
            return [login_required(SITE, url)]

        # Results are embedded in the Next.js __NEXT_DATA__ JSON blob at
        # props.pageProps.initialData.products.data (live 2026-07-08).
        m = _NEXT_DATA_RE.search(content)
        if not m:
            return []
        try:
            data = json.loads(m.group("json"))
            products = (data["props"]["pageProps"]
                        ["initialData"]["products"]["data"])
        except (ValueError, KeyError, TypeError):
            return []
        if not isinstance(products, list):
            return []

        hits = []
        for entry in products:
            if not isinstance(entry, dict):
                continue
            title = (entry.get("title") or "").strip()
            prod_url = (entry.get("productUrl") or "").strip()
            if not title or not prod_url:
                continue
            if prod_url.startswith("/"):
                prod_url = BASE_URL + prod_url
            hit = {
                "title": _strip_tags(title),
                "url": prod_url,
                "site": SITE,
                "manufacturer": "igus",
            }
            sku = (entry.get("sku") or "").strip()
            if sku:
                hit["part_number"] = sku
            thumb = (entry.get("imageUrl") or "").strip()
            if thumb:
                hit["thumbnail"] = thumb
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
        # Configured parts push the CAD behind the JS configurator / login; this v1
        # parses only static catalog parts and degrades cleanly otherwise.
        if is_login_wall(content, _LOGIN_WALL_MARKERS):
            return login_required(SITE, url)

        match = _DOWNLOAD_RE.search(content)
        if not match:
            return {"ok": False, "reason": "no_download_link",
                    "hint": "could not find a static STEP/native CAD download link; the part may "
                            "require the JS configurator (not driven in v1)"}
        download_url = match.group("href")
        if download_url.startswith("/"):
            download_url = BASE_URL + download_url

        dest, refusal = download_path(SITE, download_url, default="download.step")
        if refusal is not None:
            return refusal
        outcome = _session.browser_download(download_url, SITE, dest)
        if not outcome.get("ok"):
            return outcome

        mfr_match = _MFR_RE.search(content)
        return provenance_envelope(
            source_url=url,
            session_profile_used=outcome.get("session_profile_used", SITE),
            license_note=LICENSE_NOTE,
            artifact_path=outcome["dest"],
            title=result.get("title", ""),
            manufacturer=mfr_match.group("mfr").strip() if mfr_match else result.get("manufacturer"),
        )


ADAPTER = register(IgusAdapter())
