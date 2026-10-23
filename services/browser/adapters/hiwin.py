"""services/browser/adapters/hiwin.py — HIWIN source adapter.

HIWIN (hiwin.com) makes linear guideways, ball screws and linear actuators.
The site's Google CSE results render in a cross-origin iframe that CEF's
main-frame source cannot capture. Search therefore uses HIWIN's public
WordPress page sitemap, cached by the browser layer, and filters product URLs
locally. This is faster, avoids repeated vendor requests, and keeps category
discovery working without scraping Google.
"""
from __future__ import annotations

import html
import re
from urllib.parse import unquote, urlsplit

from .. import session as _session
from . import download_path, distributor_only, is_login_wall, provenance_envelope, register

SITE = "hiwin"
BASE_URL = "https://hiwin.com"
DISTRIBUTORS_URL = BASE_URL + "/company/distributors/"  # sells via distributors, no account
SEARCH_URL = BASE_URL + "/page-sitemap.xml"
LICENSE_NOTE = (
    "HIWIN CAD models and datasheets are fetched under the user's own "
    "authenticated HIWIN session for their own engineering use and are not "
    "redistributed. This adapter downloads only under the user's own "
    "authenticated HIWIN session. See contracts/license_debt.md."
)

_LOGIN_WALL_MARKERS = ('Log in to HIWIN', 'id="loginForm"', 'class="login-form"',
                       'Please sign in to download')
_URL_RE = re.compile(
    r'(?:<loc>|href=["\'])(?P<url>https?://[^<"\']+)(?:</loc>|["\'])',
    re.IGNORECASE,
)
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
        result = _session.browser_fetch(SEARCH_URL, SITE)
        if not result.get("ok"):
            return [result]
        content = result.get("content", "")
        terms = re.findall(r"[a-z0-9]+", query.lower())
        hits: list[dict] = []
        for match in _URL_RE.finditer(content):
            url = unquote(html.unescape(match.group("url")))
            path = urlsplit(url).path
            if "/products/" not in path:
                continue
            words = re.findall(r"[a-z0-9]+", path.lower())
            if terms and not all(any(word.startswith(term) or term.startswith(word)
                                     for word in words) for term in terms):
                continue
            slug = path.rstrip("/").rsplit("/", 1)[-1]
            title = slug.replace("-", " ").strip().title()
            if title:
                hits.append({
                    "title": title,
                    "url": url,
                    "site": SITE,
                    "manufacturer": "HIWIN",
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


ADAPTER = register(HiwinAdapter())
