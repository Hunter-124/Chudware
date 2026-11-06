"""CADENAS 3Dfindit search and honest configurator capability reporting.

Digital-twin cards are configurable catalog families, not configured SKUs. The
portal's interactive part generator is not driven by this adapter, so acquiring
one cannot silently select a variant or claim a terms wall when there is none.
"""
from __future__ import annotations

import html
import re
from urllib.parse import urlsplit

from .. import challenge, session as _session
from . import (abs_url, fail, is_login_wall, login_required, make_hit, quote_query,
               register, source_failure, terms_confirmation_required)
from ._retail import card_image

SITE = "cadenas"
BASE_URL = "https://www.3dfindit.com"
SEARCH_URL_TMPL = BASE_URL + "/en/search?q={query}"

_LOGIN_WALL_MARKERS = ('Please log in', 'id="id_username"', 'class="login-form"',
                       'Sign in to download', 'Login required')
_TERMS_MARKERS = ('Accept the terms', 'class="supplier-terms"', 'License agreement required')
_CARD_RE = re.compile(
    r'<a\b(?=[^>]*\bclass\s*=\s*["\'][^"\']*\b(?:gridCardBody|grid-card-wrapper)\b[^"\']*["\'])'
    r'(?=[^>]*\bhref\s*=\s*(?P<quote>["\'])(?P<href>.*?)(?P=quote))[^>]*>'
    r'(?P<card>.*?)</a\s*>', re.IGNORECASE | re.DOTALL)
_TEXT_RE = re.compile(r'<div\b[^>]*class\s*=\s*["\'][^"\']*\btext\b[^"\']*["\'][^>]*>(?P<t>.*?)</div>',
                      re.IGNORECASE | re.DOTALL)
_NNNAME_RE = re.compile(r'<div\b[^>]*class\s*=\s*["\'][^"\']*\bpart-nn-name\b[^"\']*["\'][^>]*>(?P<t>.*?)</div>',
                        re.IGNORECASE | re.DOTALL)
_CATALOG_NAME_RE = re.compile(r'<div\b[^>]*class\s*=\s*["\'][^"\']*\bcatalog-name\b[^"\']*["\'][^>]*>(?P<t>.*?)</div>',
                              re.IGNORECASE | re.DOTALL)
_NODE_IMAGE_RE = re.compile(
    r'<img\b(?=[^>]*\bclass\s*=\s*["\'][^"\']*\b(?:nodeImage|node-image)\b[^"\']*["\'])[^>]*>',
    re.IGNORECASE | re.DOTALL)
_SUPPLIER_RE = re.compile(
    r'<img\b(?=[^>]*\bclass\s*=\s*["\'][^"\']*\bsupplierImage\b[^"\']*["\'])'
    r'(?=[^>]*\btitle\s*=\s*(?P<quote>["\'])(?P<mfr>.*?)(?P=quote))[^>]*>', re.IGNORECASE | re.DOTALL)
_SUPPLIER_INFO_RE = re.compile(
    r'<div\b(?=[^>]*\bclass\s*=\s*["\'][^"\']*\bsupplier-info\b[^"\']*["\'])'
    r'(?=[^>]*\btitle\s*=\s*(?P<quote>["\'])(?P<mfr>.*?)(?P=quote))[^>]*>', re.IGNORECASE | re.DOTALL)
_EMPTY_RE = re.compile(r'\b(?:no results|no products found|0 results)\b', re.IGNORECASE)


def _text(value: str) -> str:
    return re.sub(r'\s+', ' ', html.unescape(re.sub(r'<[^>]+>', ' ', value))).strip()


def _page_failure(content: str, final_url: str, op: str, url: str) -> dict | None:
    kind = challenge.classify_challenge(content, final_url)
    if kind:
        return fail(SITE, op, "captcha" if kind == "captcha" else "blocked",
                    "The site presented a browser challenge.", url=final_url)
    if challenge.is_login_page("", final_url) or is_login_wall(content, _LOGIN_WALL_MARKERS):
        return source_failure(SITE, op, login_required(SITE, url), url=url)
    return None


class CadenasAdapter:
    key = SITE
    hosts = ("3dfindit.com",)
    result_kinds = ("series", "category")
    cad_acquisition = False
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
            if not href:
                continue
            parsed = urlsplit(href)
            if not parsed.hostname or parsed.hostname.lower().removeprefix("www.") != "3dfindit.com":
                continue
            if "/digitaltwin/" in parsed.path:
                kind = "series"
            elif "/cad-bim-library/manufacturer/" in parsed.path:
                kind = "category"
            else:
                continue
            if href in seen:
                continue
            seen.add(href)
            card = match.group("card")
            name = _TEXT_RE.search(card) or _NNNAME_RE.search(card) or _CATALOG_NAME_RE.search(card)
            title = _text(name.group("t")) if name else _text(card)
            title = title or parsed.path.rstrip("/").rsplit("/", 1)[-1].replace("-", " ")
            node = _NODE_IMAGE_RE.search(card)
            supplier = _SUPPLIER_RE.search(card) or _SUPPLIER_INFO_RE.search(card)
            number = _NNNAME_RE.search(card) if _TEXT_RE.search(card) else None
            catalog_id = _text(number.group("t")) if number else ""
            hits.append(make_hit(
                SITE, title=title, url=href, kind=kind,
                manufacturer=html.unescape(supplier.group("mfr")).strip() if supplier else None,
                thumbnail=card_image(node.group(0), final_url) if node and kind == "series" else None,
                extra={"catalog_id": catalog_id} if catalog_id else None))
        if not hits and not _EMPTY_RE.search(content):
            return [fail(SITE, "search", "parse_failed", "No recognizable catalog cards on the page.", url=url)]
        return hits

    def acquire(self, result: dict) -> dict:
        url = result.get("url", "")
        if not url:
            return fail(SITE, "acquire", "not_found", "Result has no URL.")
        page = _session.browser_fetch(url, SITE)
        if not page.get("ok"):
            return source_failure(SITE, "acquire", page, url=url)
        content = page.get("content", "")
        failure = _page_failure(content, page.get("final_url") or url, "acquire", url)
        if failure:
            return failure
        if is_login_wall(content, _TERMS_MARKERS):
            notice = terms_confirmation_required(SITE, url, "this catalog requires accepting download terms")
            return fail(SITE, "acquire", "terms_required", notice["hint"], url=url,
                        ask_user=notice["ask_user"])
        return fail(SITE, "acquire", "unsupported",
                    "This catalog's interactive configuration and CAD generation are not supported; "
                    "configure and download the exact part on the supplier site.", url=url)


ADAPTER = register(CadenasAdapter())
