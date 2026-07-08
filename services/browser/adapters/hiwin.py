"""services/browser/adapters/hiwin.py — HIWIN source adapter (T-X7).

HIWIN (hiwin.us) makes linear guideways, ball screws and linear actuators.
Search is public; per-part STEP downloads sit behind the user's own HIWIN
account. `acquire` lands the STEP model with provenance for part_search /
part_place.

NOTE: the regexes/markers below are a v1 best-effort shape, not verified
against a live logged-in HIWIN session (T-X6 gates the real per-source
integration tests behind an env flag).
"""
from __future__ import annotations

import re

from .. import session as _session
from . import download_dir, distributor_only, is_login_wall, provenance_envelope, register

SITE = "hiwin"
BASE_URL = "https://www.hiwin.us"
DISTRIBUTORS_URL = "https://hiwin.com/company/distributors/"  # sells via distributors, no account
SEARCH_URL_TMPL = BASE_URL + "/search?q={query}"
LICENSE_NOTE = (
    "HIWIN CAD models and datasheets are fetched under the user's own "
    "authenticated HIWIN session for their own engineering use and are not "
    "redistributed. This adapter downloads only under the user's own "
    "authenticated HIWIN session. See contracts/license_debt.md."
)

_LOGIN_WALL_MARKERS = ('Log in to HIWIN', 'id="loginForm"', 'class="login-form"',
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


class HiwinAdapter:
    key = SITE

    def search(self, query: str) -> list[dict]:
        url = SEARCH_URL_TMPL.format(query=query.replace(" ", "+"))
        result = _session.browser_fetch(url, SITE)
        if not result.get("ok"):
            return [result]
        content = result.get("content", "")
        if is_login_wall(content, _LOGIN_WALL_MARKERS):
            return [distributor_only(SITE, url, distributors_url=DISTRIBUTORS_URL)]
        hits = []
        for m in _RESULT_RE.finditer(content):
            card = m.group("title")
            mfr_match = _MFR_RE.search(card)
            hits.append({
                "title": _strip_tags(card),
                "url": BASE_URL + m.group("href"),
                "site": SITE,
                "manufacturer": mfr_match.group("mfr").strip() if mfr_match else "HIWIN",
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
            return distributor_only(SITE, url, distributors_url=DISTRIBUTORS_URL)

        match = _DOWNLOAD_RE.search(content)
        if not match:
            return {"ok": False, "reason": "no_download_link",
                    "hint": "could not find a STEP/native CAD download link on the product page"}
        download_url = match.group("href")
        if download_url.startswith("/"):
            download_url = BASE_URL + download_url

        filename = download_url.rsplit("/", 1)[-1].split("?", 1)[0] or "download.step"
        dest = download_dir(SITE) / filename
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


ADAPTER = register(HiwinAdapter())
