"""services/browser/adapters/grabcad.py — GrabCAD source adapter (T-X2).

GrabCAD Community requires a free account to search/download most models.
This adapter is DATA-heavy: the site config below (URLs, result/download
regexes, login-wall markers) is what would change if GrabCAD's markup
changes; control flow is shared with TraceParts via the small helpers in
``services.browser.adapters``.

NOTE: the regexes below are a v1 best-effort shape based on GrabCAD's public
(logged-out) page structure, not verified against a live logged-in session —
that requires a real account and is exactly what T-X6's env-flag-gated
integration tests exist to prove. Treat them as the seam to update once a
developer runs those tests, not as verified-correct markup matching.
"""
from __future__ import annotations

import re

from .. import session as _session
from . import download_dir, is_login_wall, login_required, provenance_envelope, register

SITE = "grabcad"
BASE_URL = "https://grabcad.com"
SEARCH_URL_TMPL = BASE_URL + "/library?query={query}"
LICENSE_NOTE = (
    "GrabCAD Community models: each model carries its own uploader-set license "
    "(commonly CC or GrabCAD's community terms). This adapter does not verify "
    "per-model licensing — read the model's page before reuse. See "
    "contracts/license_debt.md."
)

_LOGIN_WALL_MARKERS = ('Log in to GrabCAD', 'id="user_email"', 'class="login-form"')
# Best-effort: GrabCAD library result cards link to /library/<slug>.
_RESULT_RE = re.compile(
    r'<a[^>]+class="[^"]*card-title[^"]*"[^>]+href="(?P<href>/library/[^"?#]+)"[^>]*>'
    r'(?P<title>.*?)</a>', re.IGNORECASE | re.DOTALL)
# Best-effort: a model detail page's direct CAD download link.
_DOWNLOAD_RE = re.compile(
    r'href="(?P<href>[^"]+\.(?:step|stp|iges|igs)(?:\?[^"]*)?)"', re.IGNORECASE)


def _strip_tags(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text).strip()


class GrabCADAdapter:
    key = SITE

    def search(self, query: str) -> list[dict]:
        url = SEARCH_URL_TMPL.format(query=query.replace(" ", "+"))
        result = _session.browser_fetch(url, SITE)
        if not result.get("ok"):
            return [result]
        content = result.get("content", "")
        if is_login_wall(content, _LOGIN_WALL_MARKERS):
            return [login_required(SITE, url)]
        return [
            {"title": _strip_tags(m.group("title")), "url": BASE_URL + m.group("href"), "site": SITE}
            for m in _RESULT_RE.finditer(content)
        ]

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
                    "hint": "could not find a CAD download link on the model page"}
        download_url = match.group("href")
        if download_url.startswith("/"):
            download_url = BASE_URL + download_url

        filename = download_url.rsplit("/", 1)[-1].split("?", 1)[0] or "download.step"
        dest = download_dir(SITE) / filename
        outcome = _session.browser_download(download_url, SITE, dest)
        if not outcome.get("ok"):
            return outcome

        return provenance_envelope(
            source_url=url,
            session_profile_used=outcome.get("session_profile_used", SITE),
            license_note=LICENSE_NOTE,
            artifact_path=outcome["dest"],
            title=result.get("title", ""),
        )


ADAPTER = register(GrabCADAdapter())
