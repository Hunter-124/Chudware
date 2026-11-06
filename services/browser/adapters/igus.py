"""igus public search and best-effort, unambiguous static CAD acquisition.

Search results come from the Next.js ``initialData.products`` payload.
Multi-article entries represent a series, not a selected configuration.
The site's interactive CAD configurator is not driven by this adapter.
"""
from __future__ import annotations

import html
import json
import os
import re
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from services.browser.cad_validate import classify_artifact, validate_cad
from services.browser.challenge import classify_challenge
from .. import session as _session
from . import (abs_url, download_path, fail, is_login_wall, login_required,
               make_hit, provenance_envelope, quote_query, register, source_failure)
from ._retail import _price, _stock

SITE = "igus"
BASE_URL = "https://www.igus.com"
SEARCH_URL_TMPL = BASE_URL + "/search?q={query}"
LICENSE_NOTE = (
    "igus CAD models are fetched through the user's own browser session "
    "for engineering use and are not redistributed. Some downloads may "
    "require authorization. See contracts/license_debt.md."
)

_LOGIN_WALL_MARKERS = ('id="loginForm"', 'class="login-form"', 'Please sign in to download')
_NEXT_DATA_RE = re.compile(
    r'<script\b(?=[^>]*\bid=["\']__NEXT_DATA__["\'])[^>]*>(?P<json>.*?)</script>',
    re.IGNORECASE | re.DOTALL)
_DOWNLOAD_RE = re.compile(
    r'\bhref\s*=\s*["\'](?P<href>[^"\']+\.(?:step|stp|iges|igs|stl|zip)'
    r'(?:[?#][^"\']*)?)["\']', re.IGNORECASE)
_MFR_RE = re.compile(r'<span[^>]*class=["\'][^"\']*manufacturer[^"\']*["\'][^>]*>'
                     r'(?P<mfr>[^<]+)</span>', re.IGNORECASE)


def _text(value) -> str | None:
    return html.unescape(value).strip() or None if isinstance(value, str) else None


def _wall(content: str, url: str, op: str) -> dict | None:
    challenge = classify_challenge(content, url)
    if challenge == "captcha":
        return fail(SITE, op, "captcha", "igus returned a browser challenge.", url=url)
    if challenge == "2fa" or is_login_wall(content, _LOGIN_WALL_MARKERS):
        return source_failure(SITE, op, login_required(SITE, url), url=url)
    if re.search(r'<(?:title|h1)[^>]*>\s*(?:access denied|request blocked)\b', content, re.I):
        return fail(SITE, op, "blocked", "igus blocked the request.", url=url)
    return None


class IgusAdapter:
    key = SITE
    result_kinds = ("product", "series")

    def search(self, query: str) -> list[dict]:
        url = SEARCH_URL_TMPL.format(query=quote_query(query))
        result = _session.browser_fetch(url, SITE)
        if not result.get("ok"):
            return [source_failure(SITE, "search", result, url=url)]
        content = result.get("content") or ""
        wall = _wall(content, result.get("final_url") or url, "search")
        if wall:
            return [wall]
        match = _NEXT_DATA_RE.search(content)
        try:
            products = json.loads(match.group("json"))["props"]["pageProps"]["initialData"]["products"]
            entries = products["data"]
            if not isinstance(entries, list):
                raise TypeError("products.data is not a list")
        except (AttributeError, KeyError, TypeError, ValueError):
            return [fail(SITE, "search", "parse_failed", "Could not parse igus search results.", url=url)]

        hits = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            title = _text(entry.get("title"))
            product_url = abs_url(result.get("final_url") or url, _text(entry.get("productUrl")))
            if not title or not product_url:
                continue
            title = html.unescape(re.sub(r"<[^>]+>", "", title)).strip()
            if not title:
                continue
            price = entry.get("price")
            if isinstance(price, dict):
                currency = _text(price.get("currency")) or _text(price.get("currencyCode"))
                price = price.get("value", price.get("amount"))
            else:
                currency = None
            count = entry.get("articleCount")
            formats = entry.get("modelFormats")
            formats = [fmt for fmt in formats if isinstance(fmt, str) and fmt] if isinstance(formats, list) else None
            available = entry.get("modelAvailable")
            hits.append(make_hit(
                SITE, title=title, url=product_url,
                kind="series" if isinstance(count, int) and count > 1 else "product",
                sku=_text(entry.get("sku")), mpn=_text(entry.get("mpn")), manufacturer="igus",
                price=_price(price), currency=currency or _text(entry.get("priceCurrency"))
                or _text(entry.get("currency")),
                stock=_stock(entry.get("stock")), material=_text(entry.get("material")),
                thumbnail=_text(entry.get("imageUrl")), model_formats=formats,
                model_available=available if isinstance(available, bool) else None,
            ))
        if entries and not hits:
            return [fail(SITE, "search", "parse_failed", "igus returned unrecognized product entries.", url=url)]
        return hits

    def acquire(self, result: dict) -> dict:
        url = abs_url(BASE_URL, result.get("url"))
        if not url:
            return fail(SITE, "acquire", "not_found", "Result has no valid product URL.")
        if result.get("kind") in {"series", "category", "page", "document"}:
            return fail(SITE, "acquire", "unsupported",
                        "Select a specific igus product configuration before downloading CAD.", url=url)
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
                            "Choose a specific CAD link or configure the part on igus; "
                            "the page does not provide one unambiguous link.", url=url)
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
                          else result.get("manufacturer") or "igus"),
        )


ADAPTER = register(IgusAdapter())
