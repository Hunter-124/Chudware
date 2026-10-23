"""services/browser/adapters/content3d.py — 3D ContentCentral source adapter.

3D ContentCentral (3dcontentcentral.com, Dassault/SOLIDWORKS) hosts free
supplier-certified and community CAD models. Search is public; downloads
require the user's own free account. `acquire` lands the model (STEP/zip)
with provenance, GrabCAD-style.

Site key is "3dcontentcentral" (the registry and profiles.slug() both accept
digits); the friendly display label is "3D ContentCentral".

NOTE: the regexes/markers below are a v1 best-effort shape, not verified
against a live logged-in 3D ContentCentral session (the gated integration suite covers the real
per-source integration tests behind an env flag).
"""
from __future__ import annotations

import re

from .. import session as _session
from . import download_path, is_login_wall, login_required, provenance_envelope, register

SITE = "3dcontentcentral"
BASE_URL = "https://www.3dcontentcentral.com"
SEARCH_URL_TMPL = BASE_URL + "/Search.aspx?arg={query}"
LICENSE_NOTE = (
    "3D ContentCentral models: supplier-certified models are published by the "
    "supplier for use in the user's own designs; user-library models carry "
    "the uploader's terms. Downloads happen only under the user's own free "
    "account session and per-model terms are not verified by this adapter — "
    "read the model's page before reuse. See contracts/license_debt.md."
)

_LOGIN_WALL_MARKERS = ('Log In', 'id="ctl00_LoginName"', 'class="login-form"',
                       'You must be logged in to download')
# Best-effort: search result cards link to /parts/supplier|user-library detail pages.
_RESULT_RE = re.compile(
    r'<a[^>]+class="[^"]*model-title[^"]*"[^>]+href="(?P<href>/[^"?#]*[Dd]ownload-[Mm]odel[^"#]*|/parts/[^"?#]+)"[^>]*>'
    r'(?P<title>.*?)</a>', re.IGNORECASE | re.DOTALL)
_SUPPLIER_RE = re.compile(r'<span[^>]*class="[^"]*supplier[^"]*"[^>]*>(?P<supplier>[^<]+)</span>',
                          re.IGNORECASE)
# Best-effort: a model detail page's direct CAD download link.
_DOWNLOAD_RE = re.compile(
    r'href="(?P<href>[^"]+\.(?:step|stp|iges|igs|zip)(?:\?[^"]*)?)"', re.IGNORECASE)


def _strip_tags(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text).strip()


class Content3DAdapter:
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
            supplier_match = _SUPPLIER_RE.search(card)
            href = m.group("href")
            hits.append({
                "title": _strip_tags(card),
                "url": href if href.startswith("http") else BASE_URL + href,
                "site": SITE,
                "supplier": supplier_match.group("supplier").strip() if supplier_match else None,
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

        supplier_match = _SUPPLIER_RE.search(content)
        return provenance_envelope(
            source_url=url,
            session_profile_used=outcome.get("session_profile_used", SITE),
            license_note=LICENSE_NOTE,
            artifact_path=outcome["dest"],
            title=result.get("title", ""),
            supplier=supplier_match.group("supplier").strip() if supplier_match else result.get("supplier"),
        )


ADAPTER = register(Content3DAdapter())
