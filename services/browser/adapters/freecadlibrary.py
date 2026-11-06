"""Search and acquire models from the public FreeCAD-library GitHub repository.

The git-tree API provides file paths and adjacent previews. If GitHub truncates
the recursive tree, walk the affected subtrees instead of reporting partial
results as a complete search.
"""
from __future__ import annotations

import hashlib
import html
import json
import os
import re
import tempfile
from pathlib import Path, PurePosixPath
from urllib.parse import quote, unquote, urlparse

from .. import session as _session
from ..cad_validate import validate_cad
from . import download_path, fail, make_hit, provenance_envelope, register

SITE = "freecadlibrary"
REPO = "FreeCAD/FreeCAD-library"
REF = "master"
BASE_URL = "https://github.com"
RAW_BASE = "https://raw.githubusercontent.com/" + REPO + "/" + REF
# The deterministic git-tree API (query-independent; filtered client-side).
_TREE_URL = "https://api.github.com/repos/" + REPO + "/git/trees/"
SEARCH_URL_TMPL = _TREE_URL + REF + "?recursive=1"
_CAD_FORMATS = {".stp": "step", ".step": "step", ".fcstd": "fcstd"}
_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp")
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
    return quote(seg, safe="")


def _extract_json(content: str) -> dict | None:
    """Read a raw API response or the HTML-escaped pre element rendered by CEF."""
    wrapped = re.search(r"<pre\b[^>]*>(.*?)</pre\s*>", content, re.I | re.S)
    if wrapped:
        content = html.unescape(wrapped.group(1))
    try:
        data = json.loads(content)
    except (ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def _tree(url: str) -> tuple[dict | None, dict | None]:
    response = _session.browser_fetch(url, SITE)
    if not response.get("ok"):
        return None, response
    data = _extract_json(response.get("content", ""))
    if (not data or not isinstance(data.get("tree"), list) or
            not isinstance(data.get("truncated"), bool)):
        return None, fail(SITE, "search", "parse_failed", "Invalid GitHub tree response.", url=url)
    return data, None


def _paths() -> tuple[list[str] | None, dict | None]:
    data, error = _tree(SEARCH_URL_TMPL)
    if error:
        return None, error
    if not data["truncated"]:
        return [e["path"] for e in data["tree"] if isinstance(e, dict)
                and e.get("type") == "blob" and isinstance(e.get("path"), str)], None

    paths: list[str] = []
    # The truncated root cannot be trusted. List its immediate children, then
    # ask for each subtree recursively; repeat only where GitHub reports truncation.
    pending = [(REF, "", False)]
    while pending:
        ref, prefix, recursive = pending.pop()
        url = _TREE_URL + ref + ("?recursive=1" if recursive else "")
        node, error = _tree(url)
        if error:
            return None, error
        if node["truncated"]:
            if recursive:
                pending.append((ref, prefix, False))
                continue
            return None, fail(SITE, "search", "parse_failed",
                              "A non-recursive GitHub tree was truncated.", url=url)
        for entry in node["tree"]:
            if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
                continue
            path = prefix + entry["path"]
            if entry.get("type") == "blob":
                paths.append(path)
            elif entry.get("type") == "tree" and not recursive:
                sha = entry.get("sha", "")
                if not isinstance(sha, str) or not re.fullmatch(r"[a-fA-F0-9]{40}", sha):
                    return None, fail(SITE, "search", "parse_failed",
                                      "GitHub subtree has no valid object ID.", url=url)
                pending.append((sha, path + "/", True))
    return paths, None


def _repository_raw_url(result: dict) -> tuple[str | None, bool]:
    raw = result.get("raw_url") or ""
    if not raw:
        page = urlparse(result.get("url") or "")
        prefix = "/" + REPO + "/blob/" + REF + "/"
        if page.scheme != "https" or page.hostname not in {"github.com", "www.github.com"} \
                or not page.path.startswith(prefix):
            return None, False
        raw = RAW_BASE + "/" + page.path[len(prefix):]
    parsed = urlparse(raw)
    prefix = "/" + REPO + "/" + REF + "/"
    if parsed.scheme != "https" or parsed.hostname != "raw.githubusercontent.com" \
            or not parsed.path.startswith(prefix):
        return None, False
    parts = PurePosixPath(unquote(parsed.path[len(prefix):])).parts
    if not parts or any(part in {".", ".."} for part in parts) or "\\" in unquote(parsed.path):
        return None, True
    return raw, False


class FreeCadLibraryAdapter:
    key = SITE
    hosts = ("github.com",)
    result_kinds = ("model",)
    cad_acquisition = True
    login_required = False
    api = True

    def search(self, query: str) -> list[dict]:
        paths, error = _paths()
        if error:
            return [error]
        image_paths = {path.lower(): path for path in paths
                       if Path(path).suffix.lower() in _IMAGE_EXTS}
        tokens = [t for t in re.split(r"[\s_\-]+", query.lower()) if t]
        hits: list[dict] = []
        seen: set[str] = set()
        for path in paths:
            fmt = _CAD_FORMATS.get(Path(path).suffix.lower())
            if not fmt or path in seen:
                continue
            name = path.rsplit("/", 1)[-1]
            if tokens and not all(token in name.lower() for token in tokens):
                continue
            seen.add(path)
            stem = path.rsplit(".", 1)[0].lower()
            preview = next((image_paths[stem + ext] for ext in _IMAGE_EXTS
                            if stem + ext in image_paths), None)
            hits.append(make_hit(
                SITE, title=name, url=_blob_url(path), kind="model",
                thumbnail=_raw_url(preview) if preview else None,
                model_formats=[fmt], model_available=True,
                extra={"raw_url": _raw_url(path), "path": path},
            ))
            if len(hits) >= _MAX_HITS:
                break
        return hits

    def acquire(self, result: dict) -> dict:
        url = result.get("url", "")
        raw_url, unsafe_path = _repository_raw_url(result)
        if not raw_url:
            if unsafe_path:
                return {**fail(SITE, "acquire", "policy_refused", "Unsafe repository file path.",
                               url=result.get("raw_url") or url or None),
                        "original_reason": "unsafe_filename"}
            return fail(SITE, "acquire", "unsupported", "Expected a FreeCAD-library model URL.",
                        url=url or None)
        leaf = unquote(urlparse(raw_url).path.rsplit("/", 1)[-1])
        fmt = _CAD_FORMATS.get(Path(leaf).suffix.lower())
        requested = str(result.get("format") or result.get("requested_format") or "").lower().lstrip(".")
        requested = {"stp": "step"}.get(requested, requested)
        if not fmt or (requested and requested != fmt):
            return fail(SITE, "acquire", "unsupported", "Requested model format is not available.",
                        url=url or raw_url)
        identity_url = urlparse(raw_url)._replace(query="", fragment="").geturl()
        identity = hashlib.sha256(identity_url.encode("utf-8")).hexdigest()[:16]
        dest, refusal = download_path(SITE, raw_url, filename=f"{identity}-{leaf}")
        if refusal is not None:
            return {**fail(SITE, "acquire", "policy_refused", refusal.get("hint", ""), url=raw_url),
                    "original_reason": refusal.get("reason")}

        # Do not damage an earlier valid download if this fetch or validation fails.
        with tempfile.NamedTemporaryFile(dir=dest.parent, prefix=".cad-", suffix=dest.suffix,
                                         delete=False) as temp:
            staging = Path(temp.name)
        try:
            outcome = _session.browser_download(raw_url, SITE, staging)
            if not outcome.get("ok"):
                return outcome
            checked = validate_cad(staging)
            if not checked["ok"]:
                return fail(SITE, "acquire", "parse_failed", checked["reason"], url=raw_url)
            os.replace(staging, dest)
        finally:
            staging.unlink(missing_ok=True)

        return provenance_envelope(
            source_url=url or raw_url,
            session_profile_used=outcome.get("session_profile_used", SITE),
            license_note=LICENSE_NOTE,
            artifact_path=str(dest), artifact_kind="cad", format=checked["format"],
            title=result.get("title", leaf),
            raw_url=raw_url,
        )


ADAPTER = register(FreeCadLibraryAdapter())
