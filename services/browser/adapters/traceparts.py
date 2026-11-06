"""TraceParts search and selected-part CAD acquisition.

Supplier-specific terms are presented to the user, never accepted by the
adapter. Only a URL identifying a configured PartNumber can be downloaded;
interactive format generation is not driven when no direct file link exists.
"""
from __future__ import annotations

import html
import os
import re
import tempfile
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlsplit

from .. import challenge, session as _session
from ..cad_validate import extract_cad, validate_cad
from . import (abs_url, download_path, fail, is_login_wall, login_required, make_hit,
               model_selection_question, provenance_envelope, quote_query, register, source_failure,
               terms_confirmation_required)
from ._retail import card_image

SITE = "traceparts"
BASE_URL = "https://www.traceparts.com"
SEARCH_URL_TMPL = BASE_URL + "/en/search?CatalogPath=TRACEPARTS%3ATRACEPARTS&Keywords={query}"
LICENSE_NOTE = ("TraceParts supplier catalog terms vary by manufacturer; "
                "read the selected supplier's terms before reuse.")
_LOGIN_WALL_MARKERS = ('Sign in to TraceParts', 'class="tp-login"', 'id="login-form"')
_TERMS_MARKERS = ('Accept the terms', 'class="supplier-terms"', 'License agreement required')
_LINK_RE = re.compile(
    r'<a\b[^>]*\bhref\s*=\s*(?P<quote>["\'])(?P<href>.*?)(?P=quote)[^>]*>(?P<label>.*?)</a\s*>',
    re.IGNORECASE | re.DOTALL)
_EMPTY_RE = re.compile(r'\b(?:no results|no products found|0 results)\b', re.IGNORECASE)
_FORMATS = {"step": "step", "stp": "step", "iges": "iges", "igs": "iges",
            "stl": "stl", "obj": "obj", "3mf": "3mf", "fcstd": "fcstd", "zip": "zip"}
_CAD_FORMATS = frozenset(_FORMATS.values()) - {"zip"}


def _text(value: str) -> str:
    return re.sub(r'\s+', ' ', html.unescape(re.sub(r'<[^>]+>', ' ', value))).strip()


def _part_number(url: str) -> str:
    return next((value.strip() for key, value in parse_qsl(urlsplit(url).query, keep_blank_values=True)
                 if key.casefold() == "partnumber" and value.strip()), "")


def _page_failure(content: str, final_url: str, op: str, url: str) -> dict | None:
    kind = challenge.classify_challenge(content, final_url)
    if kind:
        return fail(SITE, op, "captcha" if kind == "captcha" else "blocked",
                    "The site presented a browser challenge.", url=final_url)
    if challenge.is_login_page("", final_url) or is_login_wall(content, _LOGIN_WALL_MARKERS):
        return source_failure(SITE, op, login_required(SITE, url), url=url)
    return None


def _file_links(content: str, base_url: str, part_number: str) -> list[tuple[str, str, str]]:
    links = []
    seen: set[str] = set()
    for match in _LINK_RE.finditer(content):
        href = abs_url(base_url, html.unescape(match.group("href")))
        if not href or href in seen:
            continue
        linked_part = _part_number(href)
        if linked_part and linked_part.casefold() != part_number.casefold():
            continue
        path = unquote(urlsplit(href).path)
        label = _text(match.group("label"))
        fmt = _FORMATS.get(Path(path).suffix.lower().lstrip('.'))
        if not fmt and re.search(r'/(?:download|files?)(?:/|$)', path, re.I):
            label_match = (re.search(r'\.(step|stp|iges|igs|stl|obj|3mf|fcstd|zip)\s*$', label, re.I)
                           or re.fullmatch(r'\s*(?:download(?:\s+as)?\s+)?'
                                           r'(step|stp|iges|igs|stl|obj|3mf|fcstd)\s*', label, re.I))
            fmt = _FORMATS.get(label_match.group(1).lower()) if label_match else None
        if fmt:
            links.append((href, fmt, label))
            seen.add(href)
    return links


class TracePartsAdapter:
    key = SITE
    hosts = ("traceparts.com",)
    result_kinds = ("product", "series")
    cad_acquisition = True
    login_required = True
    api = False

    def is_logged_in(self, content: str, url: str) -> bool:
        return not challenge.is_login_page("", url) and not is_login_wall(content, _LOGIN_WALL_MARKERS)

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
        cards: dict[str, dict] = {}
        for match in _LINK_RE.finditer(content):
            href = abs_url(final_url, html.unescape(match.group("href")))
            if not href:
                continue
            parsed = urlsplit(href)
            if (not parsed.hostname or parsed.hostname.lower().removeprefix("www.") != "traceparts.com"
                    or not parsed.path.startswith("/en/product/")):
                continue
            card = cards.setdefault(href, {"title": "", "thumbnail": None})
            title = _text(match.group("label"))
            if title and not card["title"]:
                card["title"] = title
            image = card_image(match.group("label"), final_url)
            if image and not card["thumbnail"]:
                card["thumbnail"] = image
        hits = []
        for href, card in cards.items():
            number = _part_number(href)
            slug = unquote(urlsplit(href).path.rsplit('/', 1)[-1]).replace('-', ' ').strip()
            hits.append(make_hit(SITE, title=card["title"] or slug, url=href,
                                 kind="product" if number else "series", sku=number or None,
                                 thumbnail=card["thumbnail"]))
        if not hits and not _EMPTY_RE.search(content):
            return [fail(SITE, "search", "parse_failed", "No recognizable product cards on the page.", url=url)]
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
        if is_login_wall(content, _TERMS_MARKERS):
            notice = terms_confirmation_required(SITE, url, "this supplier requires accepting per-download terms")
            return fail(SITE, "acquire", "terms_required", notice["hint"], url=url,
                        ask_user=notice["ask_user"])
        number = _part_number(url)
        final_number = _part_number(final_url)
        supplied = str(result.get("sku") or result.get("part_number") or result.get("mpn") or "").strip()
        if not number and not supplied:
            variants: list[str] = []
            for match in _LINK_RE.finditer(content):
                candidate = abs_url(final_url, html.unescape(match.group("href")))
                if not candidate or not _part_number(candidate):
                    continue
                parsed = urlsplit(candidate)
                if (parsed.hostname and parsed.hostname.lower().removeprefix("www.") == "traceparts.com"
                        and parsed.path.startswith("/en/product/") and candidate not in variants):
                    variants.append(candidate)
            if len(variants) > 1:
                return {**fail(SITE, "acquire", "unsupported",
                               "Select the configured part URL before downloading CAD.", url=url,
                               ask_user=model_selection_question(SITE, url, variants)),
                        "candidates": variants, "selection_field": "url"}
        if not number or not final_number or number.casefold() != final_number.casefold() or (
                supplied and supplied.casefold() != number.casefold()):
            return fail(SITE, "acquire", "unsupported",
                        "Select the exact configured PartNumber in TraceParts before downloading CAD.", url=url)
        links = _file_links(content, final_url, number)
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
                        "No direct link for the requested CAD format was found for this configured part.", url=url)
        if len(candidates) != 1:
            names = [unquote(urlsplit(link[0]).path).rsplit('/', 1)[-1] or link[2] or link[0]
                     for link in candidates]
            if len({name.casefold() for name in names}) != len(names):
                names = [unquote(urlsplit(link[0]).path) for link in candidates]
            if len({name.casefold() for name in names}) != len(names):
                names = [link[0] for link in candidates]
            return {**fail(SITE, "acquire", "unsupported",
                           "The part has multiple matching CAD files; specify an exact filename.", url=url,
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
                                       artifact_kind="cad", format=extracted["format"], sku=number,
                                       download_url=download_url, title=result.get("title", ""), **extras)
        finally:
            if not keep:
                Path(staged).unlink(missing_ok=True)


ADAPTER = register(TracePartsAdapter())
