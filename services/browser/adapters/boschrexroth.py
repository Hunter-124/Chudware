"""services/browser/adapters/boschrexroth.py — Bosch Rexroth source adapter (T-X7).

Bosch Rexroth (boschrexroth.com) publishes hydraulic pumps, valves and
manifold blocks with CAD models (STEP/STP archives) through its media
directory. Search is public; downloading the CAD/datasheet artifacts
requires the user's own free Bosch Rexroth account. `acquire` lands the
STEP model or archive with provenance for parts_hub / part import.

Live-verified 2026-08-07 against Bosch Rexroth's rendered Angular search
results, whose product/media/category links use ``item-headline``.
"""
from __future__ import annotations

import re
from urllib.parse import urljoin

from .. import session as _session
from . import download_path, distributor_only, is_login_wall, provenance_envelope, register

SITE = "boschrexroth"
BASE_URL = "https://www.boschrexroth.com"
SEARCH_URL_TMPL = BASE_URL + "/en/us/search?q={query}"
LICENSE_NOTE = (
    "Bosch Rexroth CAD models and datasheets are fetched under the user's own "
    "authenticated Bosch Rexroth session for their own engineering use and are "
    "not redistributed. This adapter downloads only under that authenticated "
    "session. See contracts/license_debt.md."
)

_LOGIN_WALL_MARKERS = ('Please log in', 'id="username"', 'class="login-form"',
                       'Log in to download', 'Sign in to your account')
# Rendered Angular search results use ``item-headline`` for product, category,
# and media-detail links.
_RESULT_RE = re.compile(
    r'<a[^>]+class="[^"]*item-headline[^"]*"[^>]+href="(?P<href>[^"]+)"[^>]*>'
    r'(?P<title>.*?)</a>', re.IGNORECASE | re.DOTALL)
_MFR_RE = re.compile(r'<span[^>]*class="[^"]*manufacturer[^"]*"[^>]*>(?P<mfr>[^<]+)</span>',
                     re.IGNORECASE)
# Best-effort: a product/media page's CAD download link (STEP model or archive).
_DOWNLOAD_RE = re.compile(
    r'href="(?P<href>[^"]+\.(?:step|stp|zip)(?:\?[^"]*)?)"', re.IGNORECASE)


def _strip_tags(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text).strip()


class BoschRexrothAdapter:
    key = SITE

    def search(self, query: str) -> list[dict]:
        url = SEARCH_URL_TMPL.format(query=query.replace(" ", "+"))
        result = _session.browser_fetch(url, SITE)
        if not result.get("ok"):
            return [result]
        content = result.get("content", "")
        if is_login_wall(content, _LOGIN_WALL_MARKERS):
            return [distributor_only(SITE, url)]
        hits = []
        for m in _RESULT_RE.finditer(content):
            card = m.group("title")
            mfr_match = _MFR_RE.search(card)
            hits.append({
                "title": _strip_tags(card),
                "url": urljoin(BASE_URL, m.group("href")),
                "site": SITE,
                "manufacturer": mfr_match.group("mfr").strip() if mfr_match else "Bosch Rexroth",
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
            return distributor_only(SITE, url)

        match = _DOWNLOAD_RE.search(content)
        if not match:
            return {"ok": False, "reason": "no_download_link",
                    "hint": "could not find a CAD/datasheet download link on the product page"}
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


ADAPTER = register(BoschRexrothAdapter())
