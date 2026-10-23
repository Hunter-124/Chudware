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
real SKU table. Results are typed ``kind="family"`` (a per-SKU configurable
table, not a single final SKU), mirroring MISUMI's honest ``kind="series"``.

``acquire`` reaches an actual STEP by RESOLVING the client-generated download URL
(2026-07-11): McMaster mints the CAD ``FilePath`` in the SPA — there is no static
``.step`` href in the settled HTML. So ``acquire`` drives the page's own JS (via
``session.browser_interact``) to fetch McMaster's product-data endpoint
(``/WebParts/Navigate/ProductContent.aspx?partNumber=<PN>`` — same-origin, so the
browser attaches the login cookies + the ``X-Requested-With`` header the endpoint
requires; a bare ``CefURLRequest`` 403s without it) and reads the ``"3-D STEP"``
model's ``FilePath`` out of a sentinel div. That ``FilePath`` IS a cookie-authed
direct download, so ``browser_download`` fetches it to disk. The concrete SKU is
the caller's ``part_number``/``sku``, the ``/<PN>/`` URL's segment, or — for a
``kind="family"`` table — the first configurable part number on the page. Same
in-page-JS + capture pattern generalises to Misumi/cadenas series→SKU configurators.
The ``product-card`` regex is kept as a first pass for the rare page that renders
SKU cards directly.
"""
from __future__ import annotations

import html
import re
import urllib.parse

from .. import session as _session
from . import (download_path, is_login_wall, login_required, provenance_envelope,
              register, terms_confirmation_required)

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

_LOGIN_WALL_MARKERS = ('Sign In', 'id="SignInEmail"', 'class="sign-in-form"')
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
    r'<a[^>]+class="_selectable_[^"]*"[^>]+href="(?P<slug>[a-z0-9-]+)~~/(?P<qs>\?[^"]*)?"'
    r'[^>]*>.*?<img[^>]+alt="(?P<title>[^"]*)"', re.IGNORECASE | re.DOTALL)
# Legacy mega-menu department tiles (kept only as a last-resort fallback so a
# query that somehow lands on a bare navigation grid still returns *something*
# navigable rather than a silent empty list; these are departments, not products).
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
# A concrete McMaster SKU part number, e.g. 90044A203 / 91290A115 (digits, a letter,
# digits). Used to pick the first configurable row off a family table when the caller
# didn't name a specific part number.
_PART_NUMBER_RE = re.compile(r'\b(\d{3,6}[A-Z]\d{2,5})\b')
# ProductContent.aspx (the SPA's product-data endpoint) returns JSON carrying
# cadControlData.AvailableCAD.ThreeDDownloads[] = [{DisplayName, FilePath}, …]. The
# STEP model's FilePath (server-generated, session-scoped) is the real download URL —
# there is no static .step href on the family/part HTML (it's minted client-side).
# We prefer the exact "3-D STEP" (with threads), then any .STEP FilePath.
_PRODUCT_CONTENT_PATH = "/WebParts/Navigate/ProductContent.aspx?partNumber="
_CAD_STEP_LABEL = "3-D STEP"
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
        # Fallbacks: any .STEP FilePath, else the first CAD FilePath present.
        "if(!fp){var s=t.match(/\"FilePath\":\"([^\"]+\\.STEP)\"/i);fp=s&&s[1];}"
        "if(fp){pub('ok',fp);}else{pub('nofile','');}"
        "}).catch(function(e){pub('err','');});"
        "}catch(e){pub('err','');}"
        "})();"
    )


class McMasterAdapter:
    key = SITE

    def search(self, query: str) -> list[dict]:
        url = SEARCH_URL_TMPL.format(query=query.replace(" ", "-"))
        result = _session.browser_fetch(url, SITE)
        if not result.get("ok"):
            return [result]
        content = result.get("content", "")
        if is_login_wall(content, _LOGIN_WALL_MARKERS):
            return [login_required(SITE, url)]
        hits = []
        # First pass: a page that renders SKU cards directly (rare on the SPA).
        for m in _RESULT_RE.finditer(content):
            sku_match = _SKU_RE.search(m.group("title"))
            hits.append({
                "title": _strip_tags(m.group("title")),
                "url": BASE_URL + m.group("href"),
                "site": SITE,
                "sku": sku_match.group("sku").strip() if sku_match else None,
            })
        if hits:
            return hits

        # Real search-results page (live 2026-07-11): the breadcrumb carries the
        # catalog category slug; result tiles carry the family slugs. Rebuild each
        # family into "/products/<CATEGORY>/<family-slug>~~/" — the only URL that
        # renders the per-SKU table headless (bare slug / breadcrumb URL render a
        # nav shell). Emit as kind="family" (a configurable per-SKU table).
        crumb = (_PRODUCT_FAMILY_CRUMB_RE.search(content)
                 or _PRODUCT_LINE_CRUMB_RE.search(content))
        if crumb:
            category = crumb.group("category")
            seen: set[str] = set()
            for m in _RESULT_TILE_RE.finditer(content):
                slug = m.group("slug")
                if slug in seen:
                    continue
                seen.add(slug)
                hits.append({
                    "title": html.unescape(m.group("title").strip()),
                    "url": f"{BASE_URL}/products/{category}/{slug}~~/",
                    "site": SITE,
                    # A family is a configurable per-SKU table, not one final SKU;
                    # the concrete part number/price only exist after selecting a
                    # size/length row on site — so no sku/part_number is invented.
                    "kind": "family",
                })
            if hits:
                return hits

        # Last resort: a bare mega-menu navigation grid (departments, not
        # products). Return the tiles as typed "category" results — honest that
        # these are catalog sections to drill into, not SKUs — rather than [].
        for m in _CATEGORY_TILE_RE.finditer(content):
            name = _CATEGORY_NAME_RE.search(m.group("inner"))
            hits.append({
                "title": html.unescape(name.group("name").strip()) if name
                         else _strip_tags(m.group("inner")),
                "url": BASE_URL + m.group("href"),
                "site": SITE,
                "kind": "category",   # navigable catalog section, not a purchasable SKU
            })
        return hits

    def acquire(self, result: dict) -> dict:
        url = result.get("url", "")
        if not url:
            return {"ok": False, "reason": "bad_result", "hint": "result has no 'url'"}

        # 1) Determine the concrete SKU part number to download. Caller may name one
        #    (part_number/sku), it may be encoded in a /<PN>/ product URL, or — for a
        #    kind="family" table — we pick the first configurable row off the page.
        part_number = (result.get("part_number") or result.get("sku")
                       or _part_number_from_url(url))
        if not part_number:
            page = _session.browser_fetch(url, SITE)
            if not page.get("ok"):
                return page
            content = page.get("content", "")
            if is_login_wall(content, _LOGIN_WALL_MARKERS):
                return login_required(SITE, url)
            if is_login_wall(content, _TERMS_MARKERS):
                return terms_confirmation_required(SITE, url, _TERMS_SUMMARY)
            m = _PART_NUMBER_RE.search(content)
            if not m:
                return {"ok": False, "reason": "no_sku",
                        "hint": "no concrete McMaster part number found on this page — "
                                "pass part_number (e.g. 91290A115) for a specific SKU"}
            part_number = m.group(1)

        # 2) The STEP model URL is minted client-side — no static href exists. Drive
        #    the page's own JS to fetch the SPA's product-data endpoint (same-origin,
        #    the browser attaches the login cookies + XHR header) and read out the
        #    "3-D STEP" FilePath. browser_interact returns the settled DOM with our
        #    sentinel <div id="__aicad_capture" data-fp="…" data-status="…">.
        part_url = f"{BASE_URL}/{part_number}/"
        interact = _session.browser_interact(part_url, SITE, _cad_capture_js(part_number))
        if not interact.get("ok"):
            return interact
        html_dom = interact.get("content", "")
        if is_login_wall(html_dom, _LOGIN_WALL_MARKERS):
            return login_required(SITE, part_url)
        # A CAD terms/licence click-through must NEVER be accepted on the user's
        # behalf (module docstring; same guard as traceparts/jlcpcb/pcbway) — and it
        # must be caught BEFORE the sentinel read, because a terms page publishes no
        # FilePath and would otherwise be misreported as "no_download_link".
        if is_login_wall(html_dom, _TERMS_MARKERS):
            return terms_confirmation_required(SITE, part_url, _TERMS_SUMMARY)

        cap = _SENTINEL_RE.search(html_dom)
        status = (cap.group("status") if cap else "") or ""
        file_path = html.unescape(cap.group("fp")) if cap and cap.group("fp") else ""
        if status == "login":
            return login_required(SITE, part_url)
        if not file_path:
            return {"ok": False, "reason": "no_download_link",
                    "hint": f"McMaster returned no STEP CAD file for {part_number} "
                            f"(status={status or 'unknown'}); the part may have no 3-D "
                            f"model or the session expired — run vendor_login('mcmaster')"}

        download_url = BASE_URL + urllib.parse.quote(file_path)
        dest, refusal = download_path(
            SITE, download_url, default=f"{part_number}.step",
            filename=file_path.rsplit("/", 1)[-1] or None)
        if refusal is not None:
            return refusal
        outcome = _session.browser_download(download_url, SITE, dest)
        if not outcome.get("ok"):
            return outcome

        return provenance_envelope(
            source_url=part_url,
            session_profile_used=outcome.get("session_profile_used", SITE),
            license_note=LICENSE_NOTE,
            artifact_path=outcome["dest"],
            title=result.get("title", "") or dest.name,
            sku=part_number,
            price=(f"${price_match.group('price')}"
                   if (price_match := _PRICE_RE.search(html_dom)) else None),
        )


ADAPTER = register(McMasterAdapter())
