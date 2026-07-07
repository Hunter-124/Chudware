"""services/browser/adapters/freecadlibrary.py — FreeCAD-library source adapter (T-X7).

The FreeCAD-library GitHub repo (github.com/FreeCAD/FreeCAD-library) is an OPEN
git repo of raw .stp/.step and .fcstd part files — the ONE truly-open, no-login,
no-captcha CAD source in this registry. Search queries GitHub's public code
search; `acquire` downloads the raw .stp straight from raw.githubusercontent.com
into download_dir(SITE), yielding directly importable geometry with provenance
for import_part / part_place.

Unlike the login-walled ECAD/CAD portals, no session is provisioned and no wall
is negotiated: files come from the public repo, provenance is recorded, done.

NOTE: the GitHub search markup / regexes below are a v1 best-effort shape, not
verified against a live GitHub response (T-X6 gates the real per-source
integration tests behind an env flag).
"""
from __future__ import annotations

import re

from .. import session as _session
from . import download_dir, provenance_envelope, register

SITE = "freecadlibrary"
REPO = "FreeCAD/FreeCAD-library"
BASE_URL = "https://github.com"
RAW_BASE = "https://raw.githubusercontent.com/" + REPO + "/master"
SEARCH_URL_TMPL = (
    BASE_URL + "/search?q=repo%3A" + REPO.replace("/", "%2F") + "+{query}&type=code")
LICENSE_NOTE = (
    "FreeCAD-library is community-contributed CAD content published in the "
    "public github.com/FreeCAD/FreeCAD-library repo under the repository's "
    "LGPL-2.1+/permissive terms. Files are fetched directly from the public "
    "repo (no login, no paywall) and provenance (source URL + fetch timestamp) "
    "is recorded. See contracts/license_debt.md."
)

# Best-effort: GitHub code-search result rows link to /FreeCAD/FreeCAD-library/blob/<ref>/<path>.
_RESULT_RE = re.compile(
    r'href="(?P<href>/' + re.escape(REPO) +
    r'/blob/[^"?#]+\.(?:stp|step|fcstd))"', re.IGNORECASE)


def _strip_tags(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text).strip()


def _raw_url(blob_href: str) -> str:
    # /FreeCAD/FreeCAD-library/blob/<ref>/<path...> -> raw.githubusercontent.com/<repo>/<ref>/<path...>
    path = blob_href.split("/blob/", 1)[-1]
    return "https://raw.githubusercontent.com/" + REPO + "/" + path


class FreeCadLibraryAdapter:
    key = SITE

    def search(self, query: str) -> list[dict]:
        url = SEARCH_URL_TMPL.format(query=query.replace(" ", "+"))
        result = _session.browser_fetch(url, SITE)
        if not result.get("ok"):
            return [result]
        content = result.get("content", "")
        hits = []
        seen = set()
        for m in _RESULT_RE.finditer(content):
            href = m.group("href")
            if href in seen:
                continue
            seen.add(href)
            filename = href.rsplit("/", 1)[-1]
            hits.append({
                "title": filename,
                "url": BASE_URL + href,
                "raw_url": _raw_url(href),
                "site": SITE,
            })
        return hits

    def acquire(self, result: dict) -> dict:
        url = result.get("url", "")
        raw_url = result.get("raw_url", "")
        if not raw_url and url:
            # Reconstruct raw URL from a /blob/ page URL if only 'url' was passed.
            if "/blob/" in url:
                raw_url = _raw_url(url[len(BASE_URL):] if url.startswith(BASE_URL) else url)
        if not raw_url:
            return {"ok": False, "reason": "bad_result",
                    "hint": "result has no 'raw_url' (or a /blob/ 'url' to derive it from)"}

        filename = raw_url.rsplit("/", 1)[-1].split("?", 1)[0] or "part.stp"
        dest = download_dir(SITE) / filename
        outcome = _session.browser_download(raw_url, SITE, dest)
        if not outcome.get("ok"):
            return outcome

        return provenance_envelope(
            source_url=url or raw_url,
            session_profile_used=outcome.get("session_profile_used", SITE),
            license_note=LICENSE_NOTE,
            artifact_path=outcome["dest"],
            title=result.get("title", filename),
            raw_url=raw_url,
        )


ADAPTER = register(FreeCadLibraryAdapter())
