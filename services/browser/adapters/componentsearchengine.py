"""services/browser/adapters/componentsearchengine.py — Component Search Engine adapter (T-X7).

Component Search Engine (componentsearchengine.com), the SamacSys / Supplyframe
library portal, hosts schematic symbols, PCB footprints and 3D STEP models per
manufacturer part number (MPN). Search/browse is public; downloading the library
archive requires the user's own free account. `acquire` lands the ECAD archive
(zip) or 3D model (STEP) with provenance for ecad_find_part / ecad_place_symbol.

Search results are a public HTML <table class="search-results-table">: each
row is <tr id="row-N" data-uid=...> whose part-number cell holds
<a href="/part-view/<mpn>/<manufacturer>" class="part-number">MPN</a> followed
by <span class="grey-text">Manufacturer</span> and a
<span class="td-description">Description</span>. The MODEL download is gated
behind the user's own free account, but the listing itself is public — a
"Sign In" header link is not a login wall.
"""
from __future__ import annotations

import re

from .. import session as _session
from . import download_path, is_login_wall, login_required, provenance_envelope, register

SITE = "componentsearchengine"
BASE_URL = "https://componentsearchengine.com"
SEARCH_URL_TMPL = BASE_URL + "/search?term={query}"
LICENSE_NOTE = (
    "Component Search Engine (SamacSys/Supplyframe) symbols/footprints/3D models "
    "are provided to registered account holders for use in the user's own "
    "designs under the portal's terms of use (parts are generally usable in "
    "commercial designs; redistribution of the library files themselves is not "
    "granted). This adapter downloads only under the user's own authenticated "
    "session. See contracts/license_debt.md."
)

_LOGIN_WALL_MARKERS = ('Log in to download', 'id="id_username"', 'class="login-form"',
                       'Sign up to download', 'Please login or register')
# Each result row links to /part-view/<mpn>/<manufacturer> via an <a class="part-number">;
# the manufacturer follows in a <span class="grey-text">.
_RESULT_RE = re.compile(
    r'<a[^>]+href="(?P<href>/part-view/[^"?#]+)"[^>]*class="[^"]*part-number[^"]*"[^>]*>'
    r'(?P<title>.*?)</a>'
    r'(?:\s*<span[^>]*class="[^"]*grey-text[^"]*"[^>]*>(?P<mfr>[^<]*)</span>)?',
    re.IGNORECASE | re.DOTALL)
# On a part-view page the manufacturer still shows in a grey-text span.
_MFR_RE = re.compile(r'<span[^>]*class="[^"]*grey-text[^"]*"[^>]*>(?P<mfr>[^<]+)</span>',
                     re.IGNORECASE)
# Best-effort: a part page's ECAD/3D download link (zip archive or STEP model).
_DOWNLOAD_RE = re.compile(
    r'href="(?P<href>[^"]+\.(?:zip|step|stp)(?:\?[^"]*)?)"', re.IGNORECASE)


def _strip_tags(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text).strip()


class ComponentSearchEngineAdapter:
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
            title = _strip_tags(m.group("title"))
            if not title:
                continue
            mfr = (m.group("mfr") or "").strip() or None
            hit = {
                "title": title,
                "url": BASE_URL + m.group("href"),
                "site": SITE,
                "part_number": title,
                "kind": "ecad_model",
            }
            if mfr:
                hit["manufacturer"] = mfr
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
                    "hint": "could not find a symbol/footprint/3D download link on the part page"}
        download_url = match.group("href")
        if download_url.startswith("/"):
            download_url = BASE_URL + download_url

        dest, refusal = download_path(SITE, download_url, default="download.zip")
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


ADAPTER = register(ComponentSearchEngineAdapter())
