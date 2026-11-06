"""HIWIN sitemap search and best-effort static CAD acquisition.

The page sitemap contains category pages; the product-series sitemap also
provides series pages and associated product images. Search is local to
those cached indexes, not an exact SKU search.
"""
from __future__ import annotations

import html
import os
import re
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import unquote, urlsplit

from services.browser.cad_validate import classify_artifact, validate_cad
from services.browser.challenge import classify_challenge
from .. import session as _session
from . import (abs_url, download_path, fail, is_login_wall, make_hit,
               provenance_envelope, register, source_failure)

SITE = "hiwin"
BASE_URL = "https://hiwin.com"
DISTRIBUTORS_URL = BASE_URL + "/about/distributors/"  # sells via distributors, no account
SEARCH_URL = BASE_URL + "/page-sitemap.xml"
SERIES_URL = BASE_URL + "/product_series-sitemap.xml"
LICENSE_NOTE = (
    "HIWIN CAD models and datasheets are fetched for the user's own "
    "engineering use and are not redistributed. See contracts/license_debt.md."
)

_LOGIN_WALL_MARKERS = ('id="loginForm"', 'class="login-form"', 'Please sign in to download')
_URL_RE = re.compile(r'\bhref\s*=\s*["\'](?P<url>https?://[^"\']+)["\']', re.I)
_DOWNLOAD_RE = re.compile(
    r'\bhref\s*=\s*["\'](?P<href>[^"\']+\.(?:step|stp|iges|igs|stl|zip)'
    r'(?:[?#][^"\']*)?)["\']', re.IGNORECASE)
_MFR_RE = re.compile(r'<span[^>]*class=["\'][^"\']*manufacturer[^"\']*["\'][^>]*>'
                     r'(?P<mfr>[^<]+)</span>', re.IGNORECASE)


def _wall(content: str, url: str, op: str) -> dict | None:
    challenge = classify_challenge(content, url)
    if challenge == "captcha":
        return fail(SITE, op, "captcha", "HIWIN returned a browser challenge.", url=url)
    if challenge == "2fa" or is_login_wall(content, _LOGIN_WALL_MARKERS):
        notice = fail(SITE, op, "login_required",
                      "This resource requires authorization; HIWIN sells through distributors "
                      "and does not offer individual customer accounts.", url=url)
        notice["distributors_url"] = DISTRIBUTORS_URL
        return notice
    if re.search(r'<(?:title|h1)[^>]*>\s*(?:access denied|request blocked)\b', content, re.I):
        return fail(SITE, op, "blocked", "HIWIN blocked the request.", url=url)
    return None


def _sitemap_entries(content: str) -> list[tuple[str, str | None]] | None:
    """Return URL/image pairs, including browser-rendered HTML sitemap tables."""
    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        root = None
    if root is not None and root.tag.rsplit("}", 1)[-1].lower() == "urlset":
        entries = []
        url_nodes = 0
        for node in root:
            if node.tag.rsplit("}", 1)[-1].lower() != "url":
                continue
            url_nodes += 1
            url = next((child.text for child in node
                        if child.tag.rsplit("}", 1)[-1].lower() == "loc"), None)
            image = next((child.text for parent in node
                          if parent.tag.rsplit("}", 1)[-1].lower() == "image"
                          for child in parent if child.tag.rsplit("}", 1)[-1].lower() == "loc"), None)
            if url and url.strip():
                entries.append((url.strip(), image.strip() if image else None))
        return entries if entries or not url_nodes else None
    if not re.search(r"<table\b", content, re.I):
        return None
    entries = [(html.unescape(match.group("url")), None) for match in _URL_RE.finditer(content)]
    return entries if any((urlsplit(url).hostname or "").lower().removeprefix("www.") == "hiwin.com"
                          for url, _ in entries) else None


def _kind(path: str, series_sitemap: bool) -> str:
    if series_sitemap:
        return "series"
    parts = [part for part in path.lower().split("/products/", 1)[-1].split("/") if part]
    if len(parts) > 1:
        slug = parts[-1]
        if re.fullmatch(r"[a-z]{2,}[-_]?\d+[a-z0-9_-]*", slug):
            return "product"
        if slug.endswith("-series"):
            return "series"
    return "category"


class HiwinAdapter:
    key = SITE
    result_kinds = ("product", "series", "category")

    def search(self, query: str) -> list[dict]:
        # The query is matched locally; it is never interpolated into a URL.
        terms = re.findall(r"[a-z0-9]+", query.lower())
        hits: list[dict] = []
        errors = []
        seen: set[str] = set()
        for sitemap_url, is_series in ((SEARCH_URL, False), (SERIES_URL, True)):
            result = _session.browser_fetch(sitemap_url, SITE)
            if not result.get("ok"):
                errors.append(source_failure(SITE, "search", result, url=sitemap_url))
                continue
            content = result.get("content") or ""
            wall = _wall(content, result.get("final_url") or sitemap_url, "search")
            if wall:
                errors.append(wall)
                continue
            entries = _sitemap_entries(content)
            if entries is None:
                errors.append(fail(SITE, "search", "parse_failed",
                                   "Could not parse HIWIN's product sitemap.", url=sitemap_url))
                continue
            for raw_url, image in entries:
                url = abs_url(sitemap_url, raw_url)
                if not url or (urlsplit(url).hostname or "").lower().removeprefix("www.") != "hiwin.com":
                    continue
                path = unquote(urlsplit(url).path)
                if "/products/" not in path.lower() or url in seen:
                    continue
                words = re.findall(r"[a-z0-9]+", path.lower())
                if terms and not all(any(word.startswith(term) or term.startswith(word)
                                         for word in words) for term in terms):
                    continue
                slug = path.rstrip("/").rsplit("/", 1)[-1]
                title = slug.replace("-", " ").replace("_", " ").strip().title()
                if not title:
                    continue
                seen.add(url)
                hits.append(make_hit(SITE, title=title, url=url, kind=_kind(path, is_series),
                                     manufacturer="HIWIN", thumbnail=image))
        if errors:
            return hits + errors if hits else errors[:1]
        return hits

    def acquire(self, result: dict) -> dict:
        url = abs_url(BASE_URL, result.get("url"))
        if not url:
            return fail(SITE, "acquire", "not_found", "Result has no valid product URL.")
        if result.get("kind") in {"series", "category", "page", "document"}:
            return fail(SITE, "acquire", "unsupported",
                        "Select a specific HIWIN product configuration before downloading CAD.", url=url)
        page = _session.browser_fetch(url, SITE)
        if not page.get("ok"):
            return source_failure(SITE, "acquire", page, url=url)
        content = page.get("content") or ""
        page_url = page.get("final_url") or url
        wall = _wall(content, page_url, "acquire")
        if wall:
            return wall
        links = list(dict.fromkeys(
            link for m in _DOWNLOAD_RE.finditer(content)
            if (link := abs_url(page_url, html.unescape(m.group("href"))))
        ))
        raw_selected = result.get("download_url")
        selected = abs_url(page_url, raw_selected)
        if raw_selected and (not selected or selected not in links):
            return fail(SITE, "acquire", "not_found", "The selected CAD link is not on this page.", url=url)
        requested = (result.get("requested_format") or "").lower()
        requested = {"stp": "step", "igs": "iges"}.get(requested, requested)
        if not selected:
            if len(links) != 1:
                return fail(SITE, "acquire", "unsupported",
                            "Choose a specific CAD link; the page does not provide "
                            "one unambiguous link.", url=url)
            selected = links[0]
        suffix = Path(urlsplit(selected).path).suffix.lower().lstrip(".")
        file_format = {"stp": "step", "igs": "iges"}.get(suffix, suffix)
        if requested and file_format not in {requested, "zip"}:
            return fail(SITE, "acquire", "unsupported", "The selected link is not the requested format.", url=url)
        dest, refusal = download_path(SITE, selected, default="download.step")
        if refusal is not None:
            return source_failure(SITE, "acquire", refusal, url=selected)
        try:
            with tempfile.TemporaryDirectory(dir=dest.parent, prefix=".cad-") as directory:
                staged = Path(directory) / dest.name
                outcome = _session.browser_download(selected, SITE, staged)
                if not outcome.get("ok"):
                    return source_failure(SITE, "acquire", outcome, url=selected)
                checked = validate_cad(staged, requested_format=requested or None)
                archive = staged.suffix.lower() == ".zip"
                classified = classify_artifact(staged, kind="archive") if archive else checked
                if not classified["ok"] or (requested and not checked["ok"]):
                    return fail(SITE, "acquire", "parse_failed",
                                "Downloaded file is not valid requested CAD: " +
                                (classified.get("reason") or checked.get("reason", "")), url=selected)
                os.replace(staged, dest)
        except OSError as exc:
            return fail(SITE, "acquire", "transport_failed", str(exc), url=selected)
        mfr_match = _MFR_RE.search(content)
        return provenance_envelope(
            source_url=url, session_profile_used=outcome.get("session_profile_used", SITE),
            license_note=LICENSE_NOTE, artifact_path=str(dest),
            artifact_kind="archive" if archive else "cad",
            format="zip" if archive else checked["format"],
            title=result.get("title", ""),
            manufacturer=(html.unescape(mfr_match.group("mfr")).strip() if mfr_match
                          else result.get("manufacturer") or "HIWIN"),
        )


ADAPTER = register(HiwinAdapter())
