"""services/browser/adapters/mcmaster.py — McMaster-Carr source adapter.

McMaster-Carr is the canonical MRO/hardware catalog: unlike the commerce-only
Amazon adapter, McMaster has real per-SKU CAD downloads (STEP) behind the
user's own authenticated session, plus a per-download terms click-through on
some catalog pages (same shape as TraceParts — see traceparts.py). This
adapter never accepts those terms on the user's behalf; it detects the
click-through and surfaces :func:`terms_confirmation_required` instead.

NOTE (live-verified 2026-07-11): McMaster is a hierarchical catalog SPA. A flat
``/{query}/`` fetch DOES render a real search-results page server-side — it just
isn't a SKU list. The page carries two things this adapter needs:

1. A breadcrumb with the canonical product-family link
   ``<a href="/products/product-family~~<CATEGORY>~~<hash>/?s=...">`` — the
   ``<CATEGORY>`` token (e.g. ``socket-head-screws``) is the catalog category slug.
2. A grid of result tiles ``<a class="_selectable_<hash>" href="<family-slug>~~/?s=...">``
   with the family title in the tile's ``<img alt="...">``. Each ``<family-slug>~~/``
   is a PRODUCT-FAMILY table page (the real per-SKU table + CAD downloads).

The old adapter parsed the wrong ``<a>`` set — the top-nav mega-menu department
links (``/products/fastening-and-joining/`` …), which are the "26 category
landing pages" the bug reported. Those departments are NOT product pages.

The linchpin (live-verified cold, no warm-cache): a family table only RENDERS
headless at the full path ``/products/<CATEGORY>/<family-slug>~~/`` (735 KB, 266
part numbers). The bare ``/<family-slug>~~/`` or the ``product-family~~<hash>``
breadcrumb URL both render only a category-nav SHELL (~360 KB, 0 SKUs). So
``search`` derives ``<CATEGORY>`` from the breadcrumb and rebuilds each result
into ``/products/<CATEGORY>/<family-slug>~~/`` — a URL ``acquire`` can fetch to a
real SKU table. Results are typed ``kind="series"`` (a per-SKU configurable
table, not a single final SKU).

``acquire`` reaches an actual STEP by RESOLVING the client-generated download URL
(2026-07-11): McMaster mints the CAD ``FilePath`` in the SPA — there is no static
``.step`` href in the settled HTML. So ``acquire`` drives the page's own JS (via
``session.browser_interact``) to fetch McMaster's product-data endpoint
(``/WebParts/Navigate/ProductContent.aspx?partNumber=<PN>`` — same-origin, so the
browser attaches the login cookies + the ``X-Requested-With`` header the endpoint
requires; a bare ``CefURLRequest`` 403s without it) and reads the ``"3-D STEP"``
model's ``FilePath`` out of a sentinel div. That ``FilePath`` IS a cookie-authed
direct download, so ``browser_download`` fetches it to disk. The concrete SKU is
the caller's ``part_number``/``sku`` or the ``/<PN>/`` URL's segment; a family
requires the caller to choose a specific SKU before any model is downloaded.
The ``product-card`` regex is kept as a first pass for the rare page that renders
SKU cards directly.
"""
from __future__ import annotations

import html
import re
import urllib.parse

from .. import challenge, session as _session
from ..cad_validate import validate_cad
from . import (abs_url, download_path, fail, is_login_wall, login_required, make_hit,
               provenance_envelope, quote_query, register, source_failure,
               terms_confirmation_required)
from ._retail import _price, card_image

SITE = "mcmaster"
BASE_URL = "https://www.mcmaster.com"
SEARCH_URL_TMPL = BASE_URL + "/{query}/"
LICENSE_NOTE = (
    "McMaster-Carr CAD models are provided for the customer's own use in "
    "designing/manufacturing products they purchase McMaster parts for, under "
    "a session-scoped license tied to the user's own McMaster account — not a "
    "general redistribution license. This adapter downloads a SKU's CAD only "
    "under the user's own authenticated session. See contracts/license_debt.md."
)

_LOGIN_WALL_MARKERS = ('id="SignInEmail"', 'class="sign-in-form"')
_TERMS_MARKERS = ('Accept the terms', 'class="cad-terms"', 'License agreement required')
_TERMS_SUMMARY = (
    "McMaster gates this SKU's CAD download behind a licence/terms click-through; "
    "this adapter never accepts those terms on your behalf"
)
# Best-effort: McMaster catalog/product pages are keyed by part number in the URL.
_RESULT_RE = re.compile(
    r'<a[^>]+class="[^"]*product-card[^"]*"[^>]+href="(?P<href>/[0-9A-Za-z]+/?)"[^>]*>'
    r'(?P<title>.*?)</a>', re.IGNORECASE | re.DOTALL)
# Live shape (2026-07-11): the flat "/{query}/" search page embeds breadcrumb
# links of the form "/products/<crumb-kind>~~<SLUG>~~<hash>/". The <SLUG> is the
# catalog category token needed to build a renderable family URL
# "/products/<SLUG>/<family-slug>~~/". McMaster's router accepts EITHER the
# product-family slug OR the product-line slug as that segment (both live-verified
# to render the SKU table), so we prefer product-family (most specific) and fall
# back to product-line. product-line is present on BOTH the family-disambiguation
# search pages (e.g. "m8 socket head cap screw") AND the direct-category search
# pages (e.g. "ball bearing", "hex nut") that carry no product-family crumb —
# without this fallback those queries drop to the department mega-menu (the bug).
_PRODUCT_FAMILY_CRUMB_RE = re.compile(
    r'href="/products/product-family~~(?P<category>[a-z0-9-]+)~~[^"/]+/', re.IGNORECASE)
_PRODUCT_LINE_CRUMB_RE = re.compile(
    r'href="/products/product-line~~(?P<category>[a-z0-9-]+)~~[^"/]+/', re.IGNORECASE)
# Result tiles on the search page: <a class="_selectable_<hash>" href="<slug>~~/?s=...">
# whose title is in the tile's <img alt="...">. The class hash (_selectable_srkjh_1)
# rotates on deploys, so match only the stable "_selectable_" prefix. Only "~~/"
# hrefs are product-family (SKU-table) pages; "~/" hrefs are subcategories.
_RESULT_TILE_RE = re.compile(
    r'<a\b(?=[^>]*\bclass=["\'][^"\']*_selectable_[^"\']*["\'])'
    r'(?=[^>]*\bhref=["\'](?P<slug>[a-z0-9-]+)~~/(?:\?[^"\']*)?["\'])'
    r'[^>]*>(?P<inner>.*?)</a\s*>', re.IGNORECASE | re.DOTALL)
_IMAGE_ALT_RE = re.compile(r'<img\b[^>]*\balt=["\'](?P<title>[^"\']+)["\']', re.IGNORECASE)
# The persistent browse-catalog menu is navigation, not query results.
_BROWSE_MENU_RE = re.compile(
    r'<section\b[^>]*\bclass=["\'][^"\']*\bbrowse-catalog-menu\b[^"\']*["\'][^>]*>'
    r'.*?</section\s*>', re.IGNORECASE | re.DOTALL)
# Category tiles outside the persistent menu can be catalog landing results.
_CATEGORY_TILE_RE = re.compile(
    r'<a[^>]+class="[^"]*category-tile[^"]*"[^>]+href="(?P<href>/[^"]+)"[^>]*>'
    r'(?P<inner>.*?)</a>', re.IGNORECASE | re.DOTALL)
_CATEGORY_NAME_RE = re.compile(
    r'<span[^>]*class="[^"]*category-name[^"]*"[^>]*>(?P<name>[^<]+)</span>', re.IGNORECASE)
_SKU_RE = re.compile(r'<span[^>]*class="[^"]*part-number[^"]*"[^>]*>(?P<sku>[^<]+)</span>', re.IGNORECASE)
# Same shape as the other catalog adapters (jlcpcb/pcbway/lcsc): a $-price in a
# span whose class carries "price". Best-effort — the settled SPA DOM is already
# in hand when acquire() reads the CAD sentinel, so parsing it costs nothing and
# keeps the provenance envelope's commercial identity populated.
_PRICE_RE = re.compile(r'<span[^>]*class="[^"]*price[^"]*"[^>]*>\$(?P<price>[0-9,.]+)</span>',
                       re.IGNORECASE)
# A concrete McMaster SKU part number, e.g. 90044A203 / 91290A115.
_PART_NUMBER_RE = re.compile(r'\b(\d{3,6}[A-Z]\d{2,5})\b')
_NO_RESULTS_RE = re.compile(r'\b(?:no results found|no results for|no matching (?:products|results)|'
                            r'did not match any)\b', re.IGNORECASE)
# ProductContent.aspx (the SPA's product-data endpoint) returns JSON carrying
# cadControlData.AvailableCAD.ThreeDDownloads[] = [{DisplayName, FilePath}, …]. The
# STEP model's FilePath (server-generated, session-scoped) is the real download URL —
# there is no static .step href on the family/part HTML (it's minted client-side).
# We prefer the exact "3-D STEP" (with threads), then any .STEP FilePath.
_PRODUCT_CONTENT_PATH = "/WebParts/Navigate/ProductContent.aspx?partNumber="
# The sentinel element browser_interact's JS publishes with the captured FilePath.
_SENTINEL_RE = re.compile(
    r'id="__aicad_capture"[^>]*\bdata-status="(?P<status>[^"]*)"[^>]*\bdata-fp="(?P<fp>[^"]*)"',
    re.IGNORECASE)


def _strip_tags(text: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", text)).strip()


def _part_number_from_url(url: str) -> str:
    """A McMaster product URL is keyed by the SKU: ``/90044A203/`` → ``90044A203``.
    Returns "" for family/category/search URLs (which carry no single SKU)."""
    for seg in reversed(urllib.parse.urlparse(url).path.strip("/").split("/")):
        m = _PART_NUMBER_RE.fullmatch(seg)
        if m:
            return m.group(1)
    return ""


def _cad_capture_js(part_number: str) -> str:
    """Page JS that fetches McMaster's product-data endpoint (same-origin, so the
    browser attaches the login cookies + the XHR header the endpoint requires) and
    publishes the "3-D STEP" model's server-generated FilePath into a sentinel div.

    The FilePath is minted per-session and has no static href on the HTML, so this
    in-page fetch is the only way to reach it — a direct CefURLRequest for the
    endpoint 403s (it lacks the same-origin XHR header). ``data-status`` reports the
    outcome (ok | login | nofile | err) so the adapter can degrade honestly."""
    pn = urllib.parse.quote(part_number)
    return (
        "(function(){"
        "function pub(st,fp){var d=document.createElement('div');"
        "d.id='__aicad_capture';d.setAttribute('data-status',st);"
        "d.setAttribute('data-fp',fp||'');d.textContent='done';"
        "document.documentElement.appendChild(d);}"
        "try{"
        "fetch('" + _PRODUCT_CONTENT_PATH + pn + "',"
        "{credentials:'include',headers:{'X-Requested-With':'XMLHttpRequest'}})"
        ".then(function(r){return r.text();}).then(function(t){"
        "if(/PromptLoginInd\"\\s*:\\s*true/.test(t)){pub('login','');return;}"
        # Scope to the single ThreeDDownloads object whose DisplayName is "3-D STEP",
        # then pull its FilePath (avoids grabbing the DWG/IGES sibling's path).
        "var m=t.match(/\\{[^{}]*\"DisplayName\":\"3-D STEP\"[^{}]*\\}/);"
        "var fp=m&&(m[0].match(/\"FilePath\":\"([^\"]+)\"/)||[])[1];"
        # Fall back to another STEP FilePath, never a different CAD format.
        "if(!fp){var s=t.match(/\"FilePath\":\"([^\"]+\\.STEP)\"/i);fp=s&&s[1];}"
        "if(fp){pub('ok',fp);}else{pub('nofile','');}"
        "}).catch(function(e){pub('err','');});"
        "}catch(e){pub('err','');}"
        "})();"
    )


def _page_failure(content: str, url: str, op: str) -> dict | None:
    kind = challenge.classify_challenge(content, url)
    if kind == "captcha":
        return fail(SITE, op, "captcha", "McMaster returned a captcha challenge.", url=url)
    if kind == "2fa" or challenge.is_login_page(content, url) or is_login_wall(content, _LOGIN_WALL_MARKERS):
        return source_failure(SITE, op, login_required(SITE, url), url=url)
    if re.search(r'<(?:title|h1)\b[^>]*>\s*(?:access denied|request blocked)', content, re.I):
        return fail(SITE, op, "blocked", "McMaster blocked the request.", url=url)
    if is_login_wall(content, _TERMS_MARKERS):
        question = terms_confirmation_required(SITE, url, _TERMS_SUMMARY)
        return fail(SITE, op, "terms_required", _TERMS_SUMMARY, url=url,
                    ask_user=question.get("ask_user"))
    return None


class McMasterAdapter:
    key = SITE
    hosts = ("mcmaster.com",)
    result_kinds = ("product", "series", "category")
    cad_acquisition = True
    login_required = True
    api = False

    def search(self, query: str) -> list[dict]:
        # McMaster's search is a path slug. quote_query encodes reserved characters;
        # replacing its space separator with '-' preserves the vendor's slug route.
        url = SEARCH_URL_TMPL.format(query=quote_query(query).replace("+", "-"))
        result = _session.browser_fetch(url, SITE)
        if not result.get("ok"):
            return [source_failure(SITE, "search", result, url=url)]
        content = result.get("content", "")
        if issue := _page_failure(content, url, "search"):
            return [issue]
        hits = []
        for m in _RESULT_RE.finditer(content):
            card = m.group("title")
            sku_match = _SKU_RE.search(card)
            price_match = _PRICE_RE.search(card)
            product_url = abs_url(BASE_URL, m.group("href"))
            if not product_url:
                continue
            price = _price(price_match.group("price")) if price_match else None
            hits.append(make_hit(
                SITE, title=_strip_tags(card), url=product_url,
                sku=sku_match.group("sku").strip() if sku_match else _part_number_from_url(product_url) or None,
                price=price, currency="USD" if price is not None else None,
                thumbnail=card_image(card, product_url)))
        if hits:
            return hits

        crumb = (_PRODUCT_FAMILY_CRUMB_RE.search(content)
                 or _PRODUCT_LINE_CRUMB_RE.search(content))
        tiles = list(_RESULT_TILE_RE.finditer(content))
        if crumb:
            category = crumb.group("category")
            seen: set[str] = set()
            for m in tiles:
                slug = m.group("slug")
                title = _IMAGE_ALT_RE.search(m.group("inner"))
                if slug in seen or not title:
                    continue
                seen.add(slug)
                product_url = f"{BASE_URL}/products/{category}/{slug}~~/"
                hits.append(make_hit(SITE, title=html.unescape(title.group("title").strip()),
                                     url=product_url, kind="series",
                                     thumbnail=card_image(m.group("inner"), product_url)))
            if hits:
                return hits
        if tiles:
            return [fail(SITE, "search", "parse_failed",
                         "McMaster family tiles lack a usable category or title.", url=url)]

        for m in _CATEGORY_TILE_RE.finditer(_BROWSE_MENU_RE.sub("", content)):
            name = _CATEGORY_NAME_RE.search(m.group("inner"))
            title = html.unescape(name.group("name").strip()) if name else _strip_tags(m.group("inner"))
            category_url = abs_url(BASE_URL, m.group("href"))
            if title and category_url:
                hits.append(make_hit(SITE, title=title, url=category_url, kind="category"))
        if hits:
            return hits
        if _NO_RESULTS_RE.search(content):
            return []
        return [fail(SITE, "search", "parse_failed", "Could not parse McMaster search results.", url=url)]

    def acquire(self, result: dict) -> dict:
        url = result.get("url", "")
        if not url:
            return fail(SITE, "acquire", "unsupported", "Result has no product URL.")

        requested = result.get("sku") or result.get("part_number")
        url_sku = _part_number_from_url(url)
        if requested is not None and (not isinstance(requested, str) or
                                      not re.fullmatch(r"[A-Za-z0-9-]+", requested.strip())):
            return fail(SITE, "acquire", "unsupported", "A valid McMaster SKU is required.", url=url)
        requested = requested.strip().upper() if requested else ""
        if requested and url_sku and requested != url_sku.upper():
            return fail(SITE, "acquire", "unsupported",
                        "The requested SKU does not match the product URL.", url=url)
        part_number = requested or url_sku
        if not part_number:
            page = _session.browser_fetch(url, SITE)
            if not page.get("ok"):
                return source_failure(SITE, "acquire", page, url=url)
            content = page.get("content", "")
            if issue := _page_failure(content, url, "acquire"):
                return issue
            examples = list(dict.fromkeys(_PART_NUMBER_RE.findall(content)))[:5]
            prompt = "Which exact McMaster SKU should be downloaded?"
            if examples:
                prompt += " Possible SKUs on this page: " + ", ".join(examples) + "."
            question = {"id": "mcmaster.sku", "kind": "text", "prompt": prompt,
                        "suggested": "", "required": True,
                        "why": "Each family contains different configurable parts."}
            return fail(SITE, "acquire", "unsupported", prompt, url=url, ask_user=question)

        # The per-SKU STEP URL is minted by the vendor's in-page product-data endpoint.
        part_url = f"{BASE_URL}/{part_number}/"
        interact = _session.browser_interact(part_url, SITE, _cad_capture_js(part_number))
        if not interact.get("ok"):
            return source_failure(SITE, "acquire", interact, url=part_url)
        html_dom = interact.get("content", "")
        if issue := _page_failure(html_dom, part_url, "acquire"):
            return issue

        cap = _SENTINEL_RE.search(html_dom)
        status = (cap.group("status") if cap else "") or ""
        file_path = html.unescape(cap.group("fp")) if cap and cap.group("fp") else ""
        if status == "login":
            return source_failure(SITE, "acquire", login_required(SITE, part_url), url=part_url)
        if not file_path or status != "ok":
            reason = "unsupported" if status == "nofile" else "parse_failed"
            return fail(SITE, "acquire", reason,
                        f"McMaster did not provide a STEP file for {part_number} (status={status or 'unknown'}).",
                        url=part_url)
        if not file_path.startswith("/") or file_path.startswith("//"):
            return fail(SITE, "acquire", "parse_failed", "McMaster returned an invalid STEP path.", url=part_url)

        download_url = BASE_URL + urllib.parse.quote(file_path, safe="/?=&%")
        filename = urllib.parse.urlsplit(file_path).path.rsplit("/", 1)[-1]
        dest, refusal = download_path(SITE, download_url, default=f"{part_number}.step", filename=filename)
        if refusal is not None:
            return source_failure(SITE, "acquire", refusal, url=download_url)
        if not urllib.parse.urlsplit(file_path).path.lower().endswith((".step", ".stp")):
            return fail(SITE, "acquire", "parse_failed", "McMaster returned an invalid STEP path.", url=part_url)
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

        price_match = _PRICE_RE.search(html_dom)
        price = _price(price_match.group("price")) if price_match else None
        return provenance_envelope(
            source_url=part_url,
            session_profile_used=outcome.get("session_profile_used", SITE),
            license_note=LICENSE_NOTE, artifact_path=str(artifact_path),
            artifact_kind="cad", format=validation["format"],
            title=result.get("title", "") or dest.name, sku=part_number,
            price=price, currency="USD" if price is not None else None)


ADAPTER = register(McMasterAdapter())
