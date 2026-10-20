"""services/browser/adapters/freecadlibrary.py — FreeCAD-library source adapter (T-X7).

The FreeCAD-library GitHub repo (github.com/FreeCAD/FreeCAD-library) is an OPEN
git repo of raw .stp/.step and .fcstd part files — the ONE truly-open, no-login,
no-captcha CAD source in this registry. `acquire` downloads the raw .stp straight
from raw.githubusercontent.com into download_dir(SITE), yielding directly
importable geometry with provenance for import_part / part_place.

Unlike the login-walled ECAD/CAD portals, no session is provisioned and no wall
is negotiated: files come from the public repo, provenance is recorded, done.

Live-verified 2026-07-10: the old GitHub HTML code-search (``/search?q=...``) is
JS-rendered and unreliable headless. This adapter instead reads the deterministic
GitHub REST git-tree API
``/repos/FreeCAD/FreeCAD-library/git/trees/master?recursive=1`` (HTTP 200, ~11k
entries, ``truncated:false``) once and filters the blob paths client-side by the
query tokens + a CAD extension (.stp/.step/.fcstd) — no JS, no auth, no rate-limit
key needed for one call. The CEF headless fetch wraps the JSON body in
``<html>…<pre>{…}</pre>…``, so the raw JSON is sliced out between the first ``{``
and the last ``}`` before parsing. Each hit carries the /blob/ page URL plus a
ready raw.githubusercontent.com download URL.
"""
from __future__ import annotations

import json
import re

from .. import session as _session
from . import download_path, provenance_envelope, register

SITE = "freecadlibrary"
REPO = "FreeCAD/FreeCAD-library"
REF = "master"
BASE_URL = "https://github.com"
RAW_BASE = "https://raw.githubusercontent.com/" + REPO + "/" + REF
# The deterministic git-tree API (query-independent; filtered client-side).
SEARCH_URL_TMPL = (
    "https://api.github.com/repos/" + REPO + "/git/trees/" + REF + "?recursive=1")
_CAD_EXTS = (".stp", ".step", ".fcstd")
_MAX_HITS = 40
LICENSE_NOTE = (
    "FreeCAD-library is community-contributed CAD content published in the "
    "public github.com/FreeCAD/FreeCAD-library repo under the repository's "
    "LGPL-2.1+/permissive terms. Files are fetched directly from the public "
    "repo (no login, no paywall) and provenance (source URL + fetch timestamp) "
    "is recorded. See contracts/license_debt.md."
)


def _raw_url(path: str) -> str:
    return RAW_BASE + "/" + "/".join(_quote_seg(s) for s in path.split("/"))


def _blob_url(path: str) -> str:
    return BASE_URL + "/" + REPO + "/blob/" + REF + "/" + "/".join(
        _quote_seg(s) for s in path.split("/"))


def _quote_seg(seg: str) -> str:
    from urllib.parse import quote
    return quote(seg)


def _extract_json(content: str) -> dict:
    """CEF wraps the API JSON in <html>…<pre>{…}</pre>…; slice the object out."""
    start = content.find("{")
    end = content.rfind("}")
    if start < 0 or end <= start:
        return {}
    try:
        return json.loads(content[start:end + 1])
    except (ValueError, TypeError):
        return {}


class FreeCadLibraryAdapter:
    key = SITE

    def search(self, query: str) -> list[dict]:
        result = _session.browser_fetch(SEARCH_URL_TMPL, SITE)
        if not result.get("ok"):
            return [result]
        data = _extract_json(result.get("content", ""))
        tree = data.get("tree", [])
        if not isinstance(tree, list):
            return []

        tokens = [t for t in re.split(r"[\s_\-]+", query.lower()) if t]
        hits: list[dict] = []
        seen: set[str] = set()
        for entry in tree:
            if not isinstance(entry, dict) or entry.get("type") != "blob":
                continue
            path = entry.get("path", "")
            low = path.lower()
            if not low.endswith(_CAD_EXTS):
                continue
            name = path.rsplit("/", 1)[-1]
            hay = name.lower()
            # All query tokens must appear in the filename (basename) — a filename
            # match is far more precise than a full-path match.
            if tokens and not all(t in hay for t in tokens):
                continue
            if path in seen:
                continue
            seen.add(path)
            hits.append({
                "ok": True,
                "title": name,
                "url": _blob_url(path),
                "raw_url": _raw_url(path),
                "site": SITE,
                "path": path,
            })
            if len(hits) >= _MAX_HITS:
                break
        return hits

    def acquire(self, result: dict) -> dict:
        url = result.get("url", "")
        raw_url = result.get("raw_url", "")
        if not raw_url and url and "/blob/" in url:
            # Reconstruct a raw URL from a /blob/ page URL.
            path = url.split("/blob/", 1)[-1]
            raw_url = "https://raw.githubusercontent.com/" + REPO + "/" + path
        if not raw_url:
            return {"ok": False, "reason": "bad_result",
                    "hint": "result has no 'raw_url' (or a /blob/ 'url' to derive it from)"}

        dest, refusal = download_path(SITE, raw_url, default="part.stp")
        if refusal is not None:
            return refusal
        outcome = _session.browser_download(raw_url, SITE, dest)
        if not outcome.get("ok"):
            return outcome

        return provenance_envelope(
            source_url=url or raw_url,
            session_profile_used=outcome.get("session_profile_used", SITE),
            license_note=LICENSE_NOTE,
            artifact_path=outcome["dest"],
            title=result.get("title", dest.name),
            raw_url=raw_url,
        )


ADAPTER = register(FreeCadLibraryAdapter())
