"""services/browser/adapters/mcmaster.py — McMaster-Carr source adapter (T-X7).

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

``acquire`` reaching an actual STEP is still expected to be gated: the family
table's CAD download link is generated client-side after the user selects a
concrete size/length row (no static ``.step`` href is present in the settled
family HTML), so ``acquire`` surfaces ``no_download_link`` / login when it can't
find one — the ``_DOWNLOAD_RE`` remains a best-effort shape for a page that does
expose a direct link. The ``product-card`` regex is kept as a first pass for the
rare page that renders SKU cards directly.
"""
from __future__ import annotations

import html
import re

from .. import session as _session
from . import (download_dir, is_login_wall, login_required, provenance_envelope,
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
_PRICE_RE = re.compile(r'<span[^>]*class="[^"]*price[^"]*"[^>]*>\$(?P<price>[0-9,.]+)</span>', re.IGNORECASE)
_DOWNLOAD_RE = re.compile(
    r'href="(?P<href>[^"]+\.(?:step|stp|iges|igs)(?:\?[^"]*)?)"', re.IGNORECASE)


def _strip_tags(text: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", text)).strip()


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

        page = _session.browser_fetch(url, SITE)
        if not page.get("ok"):
            return page
        content = page.get("content", "")
        if is_login_wall(content, _LOGIN_WALL_MARKERS):
            return login_required(SITE, url)
        if is_login_wall(content, _TERMS_MARKERS):
            return terms_confirmation_required(
                SITE, url, "McMaster-Carr requires accepting the CAD download terms for this part")

        match = _DOWNLOAD_RE.search(content)
        if not match:
            return {"ok": False, "reason": "no_download_link",
                    "hint": "could not find a CAD download link on the product page"}
        download_url = match.group("href")
        if download_url.startswith("/"):
            download_url = BASE_URL + download_url

        sku_match = _SKU_RE.search(content)
        sku = sku_match.group("sku").strip() if sku_match else result.get("sku")
        price_match = _PRICE_RE.search(content)

        filename = download_url.rsplit("/", 1)[-1].split("?", 1)[0] or "download.step"
        dest = download_dir(SITE) / filename
        outcome = _session.browser_download(download_url, SITE, dest)
        if not outcome.get("ok"):
            return outcome

        return provenance_envelope(
            source_url=url,
            session_profile_used=outcome.get("session_profile_used", SITE),
            license_note=LICENSE_NOTE,
            artifact_path=outcome["dest"],
            title=result.get("title", ""),
            sku=sku,
            price=("$" + price_match.group("price")) if price_match else None,
        )


ADAPTER = register(McMasterAdapter())
