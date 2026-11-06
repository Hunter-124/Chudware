"""services/browser/adapters/misumi.py — MISUMI source adapter.

MISUMI (misumi-ec.com) is a configurable mechanical-components catalog
(shafts, linear guides, plates, fasteners) with per-part-number CAD downloads
gated behind the user's own free MISUMI account. A static CAD link is used only
when the page identifies exactly the SKU requested by the caller.

NOTE (live-probe 2026-07-07): the MISUMI result page (``/vona2/result/?Keyword=``)
is a Next.js app whose search results are embedded server-side in a
``<script id="__NEXT_DATA__" type="application/json">`` blob. Product results
live at JSON path ``props.pageProps.hawksearchKeywordSearchResponse.Results``
(~30 items). Each entry's ``.Document`` is a dict of **single-element lists**;
the useful fields are ``name`` (title), ``url_detail`` (leading-slash-relative
product URL), and ``unique_id`` (the series/category code). ``sku`` is present
but EMPTY on the results page (``['']``) — MISUMI parts are configurable series,
so a concrete part number/price only exist after on-site configuration; hence
results are typed ``kind="series"`` and carry no price. ``search`` now parses
that JSON blob (verified against the real saved page) instead of speculative
CSS-class regex, which never matched the live DOM. The ``_PART_RE`` / ``_PRICE_RE``
/ ``_DOWNLOAD_RE`` regexes below remain a best-effort shape for ``acquire`` on a
configured product page and are not yet live-verified.
"""
from __future__ import annotations

import html
import json
import re
from urllib.parse import urlsplit

from .. import challenge, session as _session
from ..cad_validate import validate_cad
from . import (abs_url, download_path, fail, is_login_wall, login_required, make_hit,
               provenance_envelope, quote_query, register, source_failure)
from ._retail import _price

SITE = "misumi"
BASE_URL = "https://us.misumi-ec.com"
SEARCH_URL_TMPL = BASE_URL + "/vona2/result/?Keyword={query}"
LICENSE_NOTE = (
    "MISUMI CAD models (STEP) are provided to registered account holders for "
    "their own design/manufacturing use per MISUMI's site terms — not a "
    "general redistribution license. This adapter downloads a part number's "
    "CAD only under the user's own authenticated MISUMI session. See "
    "contracts/license_debt.md."
)

_LOGIN_WALL_MARKERS = ('id="loginId"', 'class="login-form"')
# MISUMI embeds the search results in the Next.js __NEXT_DATA__ blob.
_NEXT_DATA_RE = re.compile(
    r'<script\b(?=[^>]*\bid=["\']__NEXT_DATA__["\'])[^>]*>(?P<json>.*?)</script\s*>',
    re.IGNORECASE | re.DOTALL)
_PART_RE = re.compile(r'<span[^>]*class="[^"]*part-number[^"]*"[^>]*>(?P<part>[^<]+)</span>',
                      re.IGNORECASE)
_PRICE_RE = re.compile(r'<span[^>]*class="[^"]*price[^"]*"[^>]*>\$(?P<price>[0-9,.]+)</span>',
                       re.IGNORECASE)
_DOWNLOAD_RE = re.compile(
    r'href=["\'](?P<href>[^"\']+\.(?:step|stp|iges|igs)(?:\?[^"\']*)?)["\']',
    re.IGNORECASE)


def _first(doc: dict, key: str) -> str | None:
    """MISUMI Document fields are single-element lists; unwrap the first value."""
    val = doc.get(key)
    if isinstance(val, list):
        val = val[0] if val else None
    if isinstance(val, str):
        val = val.strip()
    return val or None


def _page_failure(content: str, url: str, op: str) -> dict | None:
    kind = challenge.classify_challenge(content, url)
    if kind == "captcha":
        return fail(SITE, op, "captcha", "MISUMI returned a captcha challenge.", url=url)
    if kind == "2fa" or challenge.is_login_page(content, url) or is_login_wall(content, _LOGIN_WALL_MARKERS):
        return source_failure(SITE, op, login_required(SITE, url), url=url)
    if re.search(r'<(?:title|h1)\b[^>]*>\s*(?:access denied|request blocked)', content, re.I):
        return fail(SITE, op, "blocked", "MISUMI blocked the request.", url=url)
    return None


class MisumiAdapter:
    key = SITE
    hosts = ("misumi-ec.com",)
    result_kinds = ("product", "series")
    cad_acquisition = True
    login_required = True
    api = False

    def search(self, query: str) -> list[dict]:
        url = SEARCH_URL_TMPL.format(query=quote_query(query))
        result = _session.browser_fetch(url, SITE)
        if not result.get("ok"):
            return [source_failure(SITE, "search", result, url=url)]
        content = result.get("content", "")
        if issue := _page_failure(content, url, "search"):
            return [issue]

        m = _NEXT_DATA_RE.search(content)
        if not m:
            return [fail(SITE, "search", "parse_failed", "MISUMI search data is missing.", url=url)]
        try:
            page_props = json.loads(m.group("json"))["props"]["pageProps"]
        except (ValueError, KeyError, TypeError):
            return [fail(SITE, "search", "parse_failed", "MISUMI search data is malformed.", url=url)]
        if not isinstance(page_props, dict):
            return [fail(SITE, "search", "parse_failed", "MISUMI search data is malformed.", url=url)]
        kw = page_props.get("hawksearchKeywordSearchResponse")
        if not isinstance(kw, dict) or "Results" not in kw:
            if any(k in page_props for k in ("npnlCategoryResponse", "cmsCategoryResponse",
                                             "npnlCategoryData")):
                return [fail(SITE, "search", "unsupported",
                             "MISUMI returned a category landing page; browse it or search by series.",
                             url=url)]
            return [fail(SITE, "search", "parse_failed", "MISUMI result list is missing.", url=url)]
        results = kw["Results"]
        if not isinstance(results, list):
            return [fail(SITE, "search", "parse_failed", "MISUMI result list is malformed.", url=url)]

        hits = []
        for entry in results:
            doc = entry.get("Document") if isinstance(entry, dict) else None
            if not isinstance(doc, dict):
                continue
            title = _first(doc, "name")
            product_url = abs_url(BASE_URL + "/", _first(doc, "url_detail"))
            if not isinstance(title, str) or not title or not product_url:
                continue
            raw_skus = doc.get("sku")
            sku = _first(doc, "sku") if not isinstance(raw_skus, list) or len(raw_skus) <= 1 else None
            sku = sku if isinstance(sku, str) else None
            brand = _first(doc, "brandname")
            manufacturer = brand if isinstance(brand, str) and brand.lower() != "approved vendor" else None
            materials = doc.get("material")
            material = _first(doc, "material") if isinstance(materials, list) and len(materials) == 1 else None
            cad_types = doc.get("cad_type")
            model_available = True if isinstance(cad_types, list) and "3D" in cad_types else None
            price = _price(_first(doc, "price")) if sku else None
            currency = _first(doc, "currency") if price is not None else None
            hits.append(make_hit(
                SITE, title=title, url=product_url, kind="product" if sku else "series",
                sku=sku, manufacturer=manufacturer, material=material,
                price=price, currency=currency, thumbnail=_first(doc, "image"),
                model_available=model_available,
                extra={"series_id": _first(doc, "unique_id")}))
        if results and not hits:
            return [fail(SITE, "search", "parse_failed", "MISUMI results lacked usable titles or URLs.", url=url)]
        return hits

    def acquire(self, result: dict) -> dict:
        url = result.get("url", "")
        if not url:
            return fail(SITE, "acquire", "unsupported", "Result has no product URL.")
        part_number = result.get("sku") or result.get("part_number")
        if not isinstance(part_number, str) or not part_number.strip():
            prompt = "Which configured MISUMI part number should be downloaded?"
            question = {"id": "misumi.sku", "kind": "text", "prompt": prompt,
                        "suggested": "", "required": True,
                        "why": "A series may contain many different CAD models."}
            return fail(SITE, "acquire", "unsupported", prompt, url=url, ask_user=question)
        part_number = part_number.strip()
        page = _session.browser_fetch(url, SITE)
        if not page.get("ok"):
            return source_failure(SITE, "acquire", page, url=url)
        content = page.get("content", "")
        if issue := _page_failure(content, url, "acquire"):
            return issue

        visible_skus = {html.unescape(m.group("part")).strip().casefold()
                        for m in _PART_RE.finditer(content)}
        if visible_skus != {part_number.casefold()}:
            return fail(SITE, "acquire", "unsupported",
                        "The page does not identify only the requested configured part number; "
                        "configure it on MISUMI before downloading.", url=url)
        match = _DOWNLOAD_RE.search(content)
        if not match:
            return fail(SITE, "acquire", "unsupported",
                        "No direct CAD link is available for the selected configuration.", url=url)
        download_url = abs_url(page.get("final_url") or url, html.unescape(match.group("href")))
        if not download_url:
            return fail(SITE, "acquire", "parse_failed", "The CAD link is invalid.", url=url)
        suffix = urlsplit(download_url).path.rsplit(".", 1)[-1].lower()
        dest, refusal = download_path(SITE, download_url, default=f"download.{suffix}")
        if refusal is not None:
            return source_failure(SITE, "acquire", refusal, url=download_url)
        outcome = _session.browser_download(download_url, SITE, dest)
        if not outcome.get("ok"):
            return source_failure(SITE, "acquire", outcome, url=download_url)
        artifact_path = outcome.get("dest")
        if not artifact_path:
            return fail(SITE, "acquire", "transport_failed", "The download returned no artifact path.",
                        url=download_url)
        validation = validate_cad(artifact_path)
        if not validation.get("ok"):
            return fail(SITE, "acquire", "parse_failed",
                        f"Downloaded CAD is invalid: {validation.get('reason', '')}", url=download_url)

        price_match = _PRICE_RE.search(content)
        price = _price(price_match.group("price")) if price_match else None
        return provenance_envelope(
            source_url=url, session_profile_used=outcome.get("session_profile_used", SITE),
            license_note=LICENSE_NOTE, artifact_path=str(artifact_path),
            artifact_kind="cad", format=validation["format"], title=result.get("title", ""),
            sku=part_number, price=price, currency="USD" if price is not None else None)


ADAPTER = register(MisumiAdapter())
