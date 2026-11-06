"""GrabCAD community model search and authenticated file acquisition.

Search cards contain actual preview images. Downloads use only file links exposed
on the selected model page; no uploader licence or file format is assumed. The
site's current logged-in file-list markup has not been verified by this adapter.
"""
from __future__ import annotations

import html
import os
import re
import tempfile
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .. import challenge, session as _session
from ..cad_validate import extract_cad, validate_cad
from . import (abs_url, download_path, fail, is_login_wall, login_required, make_hit,
               model_selection_question, provenance_envelope, quote_query, register, source_failure)
from ._retail import card_image

SITE = "grabcad"
BASE_URL = "https://grabcad.com"
SEARCH_URL_TMPL = BASE_URL + "/library?query={query}"
LICENSE_NOTE = ("GrabCAD Community models carry uploader-specific terms. "
                "Read the selected model's licence before reuse.")
_LOGIN_WALL_MARKERS = ('Log in to GrabCAD', 'id="user_email"', 'class="login-form"')
_CARD_RE = re.compile(
    r'<a\b(?=[^>]*\bclass\s*=\s*["\'][^"\']*\bmodelLink\b[^"\']*["\'])'
    r'(?=[^>]*\bhref\s*=\s*(?P<quote>["\'])(?P<href>/library/.*?)(?P=quote))[^>]*>'
    r'(?P<card>.*?)</a\s*>', re.IGNORECASE | re.DOTALL)
_PREVIEW_RE = re.compile(
    r'<img\b(?=[^>]*\bclass\s*=\s*["\'][^"\']*\bpreviewImage\b[^"\']*["\'])'
    r'(?=[^>]*\balt\s*=\s*(?P<quote>["\'])(?P<title>.*?)(?P=quote))[^>]*>', re.IGNORECASE | re.DOTALL)
_LINK_RE = re.compile(
    r'<a\b[^>]*\bhref\s*=\s*(?P<quote>["\'])(?P<href>.*?)(?P=quote)[^>]*>(?P<label>.*?)</a\s*>',
    re.IGNORECASE | re.DOTALL)
_EMPTY_RE = re.compile(r'\b(?:no results|no models found|0 results)\b', re.IGNORECASE)
_FORMATS = {"step": "step", "stp": "step", "iges": "iges", "igs": "iges",
            "stl": "stl", "obj": "obj", "3mf": "3mf", "fcstd": "fcstd", "zip": "zip"}
_CAD_FORMATS = frozenset(_FORMATS.values()) - {"zip"}


def _page_failure(content: str, final_url: str, op: str, url: str) -> dict | None:
    kind = challenge.classify_challenge(content, final_url)
    if kind:
        return fail(SITE, op, "captcha" if kind == "captcha" else "blocked",
                    "The site presented a browser challenge.", url=final_url)
    if challenge.is_login_page("", final_url) or is_login_wall(content, _LOGIN_WALL_MARKERS):
        return source_failure(SITE, op, login_required(SITE, url), url=url)
    return None


def _file_links(content: str, base_url: str) -> list[tuple[str, str, str]]:
    """Find file links, including download endpoints labeled with a filename."""
    links = []
    seen: set[str] = set()
    for match in _LINK_RE.finditer(content):
        href = abs_url(base_url, html.unescape(match.group("href")))
        if not href or href in seen:
            continue
        path = unquote(urlsplit(href).path)
        label = html.unescape(re.sub(r'<[^>]+>', ' ', match.group("label"))).strip()
        ext = Path(path).suffix.lower().lstrip('.')
        fmt = _FORMATS.get(ext)
        if not fmt and re.search(r'/(?:download|files?)(?:/|$)', path, re.I):
            label_match = (re.search(r'\.(step|stp|iges|igs|stl|obj|3mf|fcstd|zip)\s*$', label, re.I)
                           or re.fullmatch(r'\s*(?:download(?:\s+as)?\s+)?'
                                           r'(step|stp|iges|igs|stl|obj|3mf|fcstd)\s*', label, re.I))
            fmt = _FORMATS.get(label_match.group(1).lower()) if label_match else None
        if fmt:
            links.append((href, fmt, label))
            seen.add(href)
    return links


class GrabCADAdapter:
    key = SITE
    hosts = ("grabcad.com",)
    result_kinds = ("model",)
    cad_acquisition = True
    login_required = True
    api = False

    def search(self, query: str) -> list[dict]:
        url = SEARCH_URL_TMPL.format(query=quote_query(query))
        page = _session.browser_fetch(url, SITE)
        if not page.get("ok"):
            return [source_failure(SITE, "search", page, url=url)]
        content = page.get("content", "")
        final_url = page.get("final_url") or url
        failure = _page_failure(content, final_url, "search", url)
        if failure:
            return [failure]
        hits = []
        seen: set[str] = set()
        for match in _CARD_RE.finditer(content):
            href = abs_url(final_url, html.unescape(match.group("href")))
            if not href or urlsplit(href).path.rstrip('/') == "/library/new" or href in seen:
                continue
            preview = _PREVIEW_RE.search(match.group("card"))
            if not preview:
                continue
            title = html.unescape(preview.group("title")).strip()
            if not title:
                continue
            seen.add(href)
            hits.append(make_hit(SITE, title=title, url=href, kind="model",
                                 thumbnail=card_image(preview.group(0), final_url)))
        if not hits and not _EMPTY_RE.search(content):
            return [fail(SITE, "search", "parse_failed", "No recognizable model cards on the page.", url=url)]
        return hits

    def acquire(self, result: dict) -> dict:
        url = result.get("url", "")
        if not url:
            return fail(SITE, "acquire", "not_found", "Result has no URL.")
        requested = str(result.get("requested_format") or result.get("format") or "").strip().lower().lstrip('.')
        requested = _FORMATS.get(requested, requested)
        if requested and requested not in _CAD_FORMATS:
            return fail(SITE, "acquire", "unsupported", "Requested CAD format is not supported.", url=url)
        page = _session.browser_fetch(url, SITE)
        if not page.get("ok"):
            return source_failure(SITE, "acquire", page, url=url)
        content = page.get("content", "")
        final_url = page.get("final_url") or url
        failure = _page_failure(content, final_url, "acquire", url)
        if failure:
            return failure
        links = _file_links(content, final_url)
        filename = str(result.get("filename") or "").strip()
        if filename:
            links = [link for link in links if filename == link[0] or filename.casefold() in {
                link[2].strip().casefold(), unquote(urlsplit(link[0]).path).casefold(),
                unquote(urlsplit(link[0]).path).rsplit('/', 1)[-1].casefold()}]
        member = result.get("archive_member")
        candidates = [link for link in links if
                      link[1] == "zip" or (not member and (not requested or link[1] == requested))]
        if not candidates:
            return fail(SITE, "acquire", "unsupported",
                        "No link for the requested CAD file or format was found on this model page.", url=url)
        if len(candidates) != 1:
            names = [unquote(urlsplit(link[0]).path).rsplit('/', 1)[-1] or link[2] or link[0]
                     for link in candidates]
            if len({name.casefold() for name in names}) != len(names):
                names = [unquote(urlsplit(link[0]).path) for link in candidates]
            if len({name.casefold() for name in names}) != len(names):
                names = [link[0] for link in candidates]
            return {**fail(SITE, "acquire", "unsupported",
                           "The model has multiple matching files; specify an exact filename.", url=url,
                           ask_user=model_selection_question(SITE, url, names)),
                    "candidates": names, "selection_field": "filename"}
        download_url, link_format, _ = candidates[0]
        path_format = _FORMATS.get(Path(unquote(urlsplit(download_url).path)).suffix.lower().lstrip('.'))
        dest, refusal = download_path(SITE, download_url, default=f"download.{link_format}",
                                      filename=None if path_format else f"download.{link_format}")
        if refusal is not None:
            return source_failure(SITE, "acquire", refusal, url=download_url)
        try:
            fd, staged = tempfile.mkstemp(prefix=dest.stem + "-", suffix=dest.suffix, dir=dest.parent)
            os.close(fd)
        except OSError as exc:
            return fail(SITE, "acquire", "transport_failed", str(exc), url=download_url)
        keep = False
        try:
            outcome = _session.browser_download(download_url, SITE, Path(staged))
            if not outcome.get("ok"):
                return source_failure(SITE, "acquire", outcome, url=download_url)
            member = result.get("archive_member")
            extracted = extract_cad(staged, requested_format=requested or None,
                                    requested_member=member)
            if not extracted["ok"]:
                if extracted.get("candidates"):
                    names = extracted["candidates"]
                    return {**fail(SITE, "acquire", "unsupported",
                                   "The archive contains multiple CAD files; select an archive member.",
                                   url=download_url, ask_user=model_selection_question(SITE, url, names)),
                            "candidates": names, "selection_field": "archive_member"}
                if link_format == "zip" and (requested or member):
                    available = validate_cad(staged)
                    if available["ok"] or available.get("candidates"):
                        return fail(SITE, "acquire", "unsupported",
                                    "The archive does not contain the requested CAD file.", url=download_url)
                return fail(SITE, "acquire", "parse_failed", extracted["reason"], url=download_url)
            keep = True
            extras = {key: extracted[key] for key in ("archive_path", "member") if key in extracted}
            return provenance_envelope(source_url=url, session_profile_used=outcome.get("session_profile_used", SITE),
                                       license_note=LICENSE_NOTE, artifact_path=extracted["artifact_path"],
                                       artifact_kind="cad", format=extracted["format"],
                                       download_url=download_url, title=result.get("title", ""), **extras)
        finally:
            if not keep:
                Path(staged).unlink(missing_ok=True)


ADAPTER = register(GrabCADAdapter())
