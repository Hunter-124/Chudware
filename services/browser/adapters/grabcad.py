"""services/browser/adapters/grabcad.py — GrabCAD source adapter (T-X2).

GrabCAD Community requires a free account to search/download most models.
This adapter is DATA-heavy: the site config below (URLs, result/download
regexes, login-wall markers) is what would change if GrabCAD's markup
changes; control flow is shared with TraceParts via the small helpers in
``services.browser.adapters``.

LIVE SHAPE (verified 2026-07-07 against a real fetch of
``/library?query=gear``): library result cards are anchors of the form
``<a href="/library/<slug>" class="modelLink">`` that wrap an
``<img class="previewImage" ... alt="<Model Title>" ...>``. The model title
is the img ``alt`` attribute; the model URL is ``BASE_URL`` + the
``/library/<slug>`` href. The page carries ~24 such cards per query. We
dedupe by href and skip the ``/library/new`` upload utility link (which uses
a ``btn`` class, not ``modelLink``). The download regex on the detail page
(``acquire``) is still a best-effort seam pending a live logged-in fetch.
"""
from __future__ import annotations

import html as _html
import re

from .. import session as _session
from . import download_path, is_login_wall, login_required, provenance_envelope, register

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
# Verified 2026-07-07: library result cards are `modelLink` anchors wrapping a
# `previewImage` img whose `alt` attribute is the model title.
_RESULT_RE = re.compile(
    r'<a[^>]+href="(?P<href>/library/[^"?#]+)"[^>]*class="[^"]*\bmodelLink\b[^"]*"[^>]*>'
    r'\s*<img[^>]*class="[^"]*\bpreviewImage\b[^"]*"[^>]*\balt="(?P<title>[^"]*)"',
    re.IGNORECASE | re.DOTALL)
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
        hits: list[dict] = []
        seen: set[str] = set()
        for m in _RESULT_RE.finditer(content):
            href = m.group("href")
            if href == "/library/new" or href in seen:
                continue
            seen.add(href)
            hits.append({
                "title": _html.unescape(m.group("title")).strip(),
                "url": BASE_URL + href,
                "site": SITE,
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
                    "hint": "could not find a CAD download link on the model page"}
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
        )


ADAPTER = register(GrabCADAdapter())
