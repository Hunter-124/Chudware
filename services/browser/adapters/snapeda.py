"""services/browser/adapters/snapeda.py — SnapEDA source adapter.

SnapEDA (snapeda.com) hosts ECAD symbols, footprints and 3D models for
electronic parts. Search is public; downloads require the user's own free
SnapEDA account. `acquire` lands the symbol/footprint archive (zip) or 3D
model (STEP) with provenance for ecad_find_part / ecad_place_symbol.

NOTE: the regexes/markers below are a v1 best-effort shape, not verified
against a live logged-in SnapEDA session (the gated integration suite covers the real per-source
integration tests behind an env flag).
"""
from __future__ import annotations

import re

from .. import session as _session
from . import download_path, is_login_wall, login_required, provenance_envelope, register

SITE = "snapeda"
BASE_URL = "https://www.snapeda.com"
SEARCH_URL_TMPL = BASE_URL + "/search/?q={query}"
LICENSE_NOTE = (
    "SnapEDA symbols/footprints/3D models are provided to registered account "
    "holders for use in the user's own designs under SnapEDA's terms of use "
    "(free parts are generally usable in commercial designs; redistribution "
    "of the library files themselves is not granted). This adapter downloads "
    "only under the user's own authenticated SnapEDA session. See "
    "contracts/license_debt.md."
)

_LOGIN_WALL_MARKERS = ('Log in to SnapEDA', 'id="id_username"', 'class="login-form"',
                       'Sign up to download')
# Best-effort: SnapEDA search result cards link to /parts/<pn>/<manufacturer>/....
_RESULT_RE = re.compile(
    r'<a[^>]+class="[^"]*part-title[^"]*"[^>]+href="(?P<href>/parts/[^"?#]+)"[^>]*>'
    r'(?P<title>.*?)</a>', re.IGNORECASE | re.DOTALL)
_MFR_RE = re.compile(r'<span[^>]*class="[^"]*manufacturer[^"]*"[^>]*>(?P<mfr>[^<]+)</span>',
                     re.IGNORECASE)
# Best-effort: a part page's ECAD/3D download link (zip archive or STEP model).
_DOWNLOAD_RE = re.compile(
    r'href="(?P<href>[^"]+\.(?:zip|step|stp)(?:\?[^"]*)?)"', re.IGNORECASE)


def _strip_tags(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text).strip()


class SnapEdaAdapter:
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
            mfr_match = _MFR_RE.search(card)
            hits.append({
                "title": _strip_tags(card),
                "url": BASE_URL + m.group("href"),
                "site": SITE,
                "manufacturer": mfr_match.group("mfr").strip() if mfr_match else None,
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


ADAPTER = register(SnapEdaAdapter())
