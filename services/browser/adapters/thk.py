"""THK popfind site search and best-effort static CAD acquisition.

The search widget returns products as well as catalog and non-product pages.
Its static CAD links are not verified against an authenticated live session.
"""
from __future__ import annotations

import html
import os
import re
import tempfile
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

from services.browser.cad_validate import classify_artifact, validate_cad
from services.browser.challenge import classify_challenge
from .. import session as _session
from . import (abs_url, download_path, fail, is_login_wall, login_required,
               make_hit, provenance_envelope, quote_query, register, source_failure)
from ._retail import _price, _stock, card_image

SITE = "thk"
BASE_URL = "https://www.thk.com"
SEARCH_URL_TMPL = BASE_URL + "/us/en/search/?q={query}"
LICENSE_NOTE = (
    "THK CAD models are fetched through the user's own browser session "
    "for engineering use and are not redistributed. Some downloads may "
    "require authorization. See contracts/license_debt.md."
)

_LOGIN_WALL_MARKERS = ('id="loginForm"', 'class="login-form"', 'Please sign in to download')
_TITLE_RE = re.compile(
    r'<div\b(?=[^>]*\bclass\s*=\s*["\'][^"\']*\b_title\b[^"\']*["\'])[^>]*>'
    r'\s*<a\b[^>]*\bhref\s*=\s*["\'](?P<href>[^"\']+)["\'][^>]*>(?P<title>.*?)</a>',
    re.IGNORECASE | re.DOTALL)
_THUMB_RE = re.compile(
    r'<div\b(?=[^>]*\bclass\s*=\s*["\'][^"\']*\b_thumbnail\b[^"\']*["\'])'
    r'[^>]*>(?P<body>.*?)</div>', re.IGNORECASE | re.DOTALL)
_DOWNLOAD_RE = re.compile(
    r'\bhref\s*=\s*["\'](?P<href>[^"\']+\.(?:step|stp|iges|igs|stl|zip)'
    r'(?:[?#][^"\']*)?)["\']', re.IGNORECASE)
_MFR_RE = re.compile(r'<span[^>]*class=["\'][^"\']*manufacturer[^"\']*["\'][^>]*>'
                     r'(?P<mfr>[^<]+)</span>', re.IGNORECASE)


class _Cards(HTMLParser):
    """Keep each popfind item bounded so footer data cannot leak into the last hit."""
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.cards: list[str] = []
        self.current: list[str] = []
        self.depth = 0

    def handle_starttag(self, tag, attrs):
        if not self.depth:
            classes = dict(attrs).get("class") or ""
            if tag != "div" or "_item" not in classes.split():
                return
        if tag == "div":
            self.depth += 1
        self.current.append(self.get_starttag_text())

    def handle_startendtag(self, tag, attrs):
        if self.depth:
            self.current.append(self.get_starttag_text())

    def handle_endtag(self, tag):
        if not self.depth:
            return
        self.current.append(f"</{tag}>")
        if tag == "div":
            self.depth -= 1
            if not self.depth:
                self.cards.append("".join(self.current))
                self.current = []

    def handle_data(self, data):
        if self.depth:
            self.current.append(data)

    def handle_entityref(self, name):
        if self.depth:
            self.current.append(f"&{name};")

    def handle_charref(self, name):
        if self.depth:
            self.current.append(f"&#{name};")


def _attribute(card: str, name: str) -> str | None:
    match = re.search(r'\bdata-' + re.escape(name) + r'\s*=\s*["\']([^"\']+)["\']', card, re.I)
    return html.unescape(match.group(1)).strip() or None if match else None


def _part_number(card: str) -> str | None:
    value = _attribute(card, "sku") or _attribute(card, "part-number")
    if value:
        return value
    text = html.unescape(re.sub(r"<[^>]+>", " ", card))
    match = re.search(r'\b(?:sku|part\s*(?:number|no\.?))\s*[:#]\s*'
                      r'([a-z0-9][a-z0-9._/-]*)', text, re.I)
    return match.group(1) if match else None


def _kind(url: str, sku: str | None, mpn: str | None) -> str:
    path = urlsplit(url).path.lower()
    if path.endswith(".pdf"):
        return "document"
    if sku or mpn or "/product/" in path:
        return "product"
    if "/products/" not in path:
        return "page"
    parts = [part for part in path.split("/products/", 1)[1].split("/") if part]
    if parts and parts[0] in {"search", "faq", "information", "tools"}:
        return "page"
    return "category" if len(parts) <= 1 else "series"


def _wall(content: str, url: str, op: str) -> dict | None:
    challenge = classify_challenge(content, url)
    if challenge == "captcha":
        return fail(SITE, op, "captcha", "THK returned a browser challenge.", url=url)
    if challenge == "2fa" or is_login_wall(content, _LOGIN_WALL_MARKERS):
        return source_failure(SITE, op, login_required(SITE, url), url=url)
    if re.search(r'<(?:title|h1)[^>]*>\s*(?:access denied|request blocked)\b', content, re.I):
        return fail(SITE, op, "blocked", "THK blocked the request.", url=url)
    return None


class ThkAdapter:
    key = SITE
    result_kinds = ("product", "series", "category", "page", "document")

    def search(self, query: str) -> list[dict]:
        url = SEARCH_URL_TMPL.format(query=quote_query(query))
        result = _session.browser_fetch(url, SITE)
        if not result.get("ok"):
            return [source_failure(SITE, "search", result, url=url)]
        content = result.get("content") or ""
        page_url = result.get("final_url") or url
        wall = _wall(content, page_url, "search")
        if wall:
            return [wall]
        parser = _Cards()
        parser.feed(content)
        cards = parser.cards
        hits = []
        for card in cards:
            match = _TITLE_RE.search(card)
            if not match:
                continue
            title = html.unescape(re.sub(r"<[^>]+>", "", match.group("title"))).strip()
            product_url = abs_url(page_url, html.unescape(match.group("href")))
            if not title or not product_url:
                continue
            sku = _part_number(card)
            mpn = _attribute(card, "mpn")
            thumb = _THUMB_RE.search(card)
            hits.append(make_hit(
                SITE, title=title, url=product_url, kind=_kind(product_url, sku, mpn),
                sku=sku, mpn=mpn, manufacturer="THK",
                price=_price(_attribute(card, "price")), currency=_attribute(card, "currency"),
                stock=_stock(_attribute(card, "stock")), material=_attribute(card, "material"),
                thumbnail=card_image(thumb.group("body"), page_url) if thumb else None,
            ))
        if hits:
            return hits
        if not cards and re.search(r'class\s*=\s*["\'][^"\']*\b_nohits\b', content, re.I):
            return []
        return [fail(SITE, "search", "parse_failed",
                     "THK search results were not present in the rendered widget.", url=url)]

    def acquire(self, result: dict) -> dict:
        url = abs_url(BASE_URL, result.get("url"))
        if not url:
            return fail(SITE, "acquire", "not_found", "Result has no valid product URL.")
        if result.get("kind") in {"series", "category", "page", "document"}:
            return fail(SITE, "acquire", "unsupported",
                        "Select a specific THK product configuration before downloading CAD.", url=url)
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
                          else result.get("manufacturer") or "THK"),
        )


ADAPTER = register(ThkAdapter())
