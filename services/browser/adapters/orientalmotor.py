"""services/browser/adapters/orientalmotor.py — orientalmotor source adapter.

Oriental Motor (orientalmotor.com) makes stepper motors, BLDC motors,
gearmotors and drivers. Search and most STEP/native CAD downloads are open;
`acquire` lands the STEP model with provenance for part_search / part_place.

NOTE: the regexes/markers below are a v1 best-effort shape, not verified
against a live orientalmotor session (the gated integration suite covers the real per-source
integration tests behind an env flag).
"""
from __future__ import annotations

import re

from .. import session as _session
from . import download_path, is_login_wall, login_required, provenance_envelope, register

SITE = "orientalmotor"
BASE_URL = "https://www.orientalmotor.com"
SEARCH_URL_TMPL = BASE_URL + "/search?q={query}"
LICENSE_NOTE = (
    "Oriental Motor CAD models and datasheets are fetched under the user's "
    "own authenticated Oriental Motor session for their own engineering use "
    "and are not redistributed. This adapter downloads only under the user's "
    "own authenticated Oriental Motor session. See contracts/license_debt.md."
)

_LOGIN_WALL_MARKERS = ('Log in to Oriental Motor', 'id="loginForm"', 'class="login-form"',
                       'Please sign in to download')
# Best-effort: search result cards link to a product detail page.
_RESULT_RE = re.compile(
    r'<a[^>]+class="[^"]*product-title[^"]*"[^>]+href="(?P<href>/products/[^"?#]+)"[^>]*>'
    r'(?P<title>.*?)</a>', re.IGNORECASE | re.DOTALL)
_MFR_RE = re.compile(r'<span[^>]*class="[^"]*manufacturer[^"]*"[^>]*>(?P<mfr>[^<]+)</span>',
                     re.IGNORECASE)
# Best-effort: a product page's native CAD download link (STEP model or zip).
_DOWNLOAD_RE = re.compile(
    r'href="(?P<href>[^"]+\.(?:step|stp|zip)(?:\?[^"]*)?)"', re.IGNORECASE)


def _strip_tags(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text).strip()


class OrientalMotorAdapter:
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
                "manufacturer": mfr_match.group("mfr").strip() if mfr_match else "Oriental Motor",
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

        mfr_match = _MFR_RE.search(content)
        return provenance_envelope(
            source_url=url,
            session_profile_used=outcome.get("session_profile_used", SITE),
            license_note=LICENSE_NOTE,
            artifact_path=outcome["dest"],
            title=result.get("title", ""),
            manufacturer=mfr_match.group("mfr").strip() if mfr_match else result.get("manufacturer"),
        )


ADAPTER = register(OrientalMotorAdapter())
