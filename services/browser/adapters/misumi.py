"""services/browser/adapters/misumi.py — MISUMI source adapter (T-X7).

MISUMI (misumi-ec.com) is a configurable mechanical-components catalog
(shafts, linear guides, plates, fasteners) with per-part-number CAD downloads
(STEP) gated behind the user's own free MISUMI account. Like McMaster, this
is a CAD-download vendor: `acquire` lands a STEP file with provenance.

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

import json
import re

from .. import session as _session
from . import download_path, is_login_wall, login_required, provenance_envelope, register

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

_LOGIN_WALL_MARKERS = ('Log in to MISUMI', 'id="loginId"', 'class="login-form"', 'Please log in')
# Live shape (2026-07-07): results are embedded in the Next.js __NEXT_DATA__ blob.
_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(?P<json>.*?)</script>',
    re.DOTALL)
_PART_RE = re.compile(r'<span[^>]*class="[^"]*part-number[^"]*"[^>]*>(?P<part>[^<]+)</span>',
                      re.IGNORECASE)
_PRICE_RE = re.compile(r'<span[^>]*class="[^"]*price[^"]*"[^>]*>\$(?P<price>[0-9,.]+)</span>',
                       re.IGNORECASE)
_DOWNLOAD_RE = re.compile(
    r'href="(?P<href>[^"]+\.(?:step|stp|iges|igs)(?:\?[^"]*)?)"', re.IGNORECASE)


def _strip_tags(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text).strip()


def _first(doc: dict, key: str) -> str | None:
    """MISUMI Document fields are single-element lists; unwrap the first value."""
    val = doc.get(key)
    if isinstance(val, list):
        val = val[0] if val else None
    if isinstance(val, str):
        val = val.strip()
    return val or None


class MisumiAdapter:
    key = SITE

    def search(self, query: str) -> list[dict]:
        url = SEARCH_URL_TMPL.format(query=query.replace(" ", "+"))
        result = _session.browser_fetch(url, SITE)
        if not result.get("ok"):
            return [result]
        content = result.get("content", "")
        if is_login_wall(content, _LOGIN_WALL_MARKERS):
            return [login_required(SITE, url)]

        # Results are embedded in the Next.js __NEXT_DATA__ JSON blob at
        # props.pageProps.hawksearchKeywordSearchResponse.Results (live 2026-07-07).
        # MISUMI routes generic keyword queries ("bearing", "shaft") to this flat
        # keyword-result list, but routes more specific / category-shaped queries
        # ("aluminum extrusion", "hex bolt m5") to a CATEGORY LANDING response
        # (props.pageProps.npnlCategoryResponse / cmsCategoryResponse) that carries
        # no flat product list — the user is meant to drill into a subcategory on
        # site. We detect that case and surface it honestly rather than returning a
        # bare [] that a caller can't distinguish from "site broke". (Live 2026-07-08.)
        m = _NEXT_DATA_RE.search(content)
        if not m:
            return []
        try:
            page_props = json.loads(m.group("json"))["props"]["pageProps"]
        except (ValueError, KeyError, TypeError):
            return []
        kw = page_props.get("hawksearchKeywordSearchResponse")
        if not isinstance(kw, dict) or "Results" not in kw:
            # Category-landing response (or an unknown shape): no flat product list.
            if any(k in page_props for k in ("npnlCategoryResponse", "cmsCategoryResponse",
                                             "npnlCategoryData")):
                return [{
                    "ok": False,
                    "reason": "category_landing",
                    "site": SITE,
                    "hint": (f"MISUMI routed '{query}' to a category page rather than a "
                             f"flat product list. Try a more specific part term (e.g. a "
                             f"series code or a single keyword like 'shaft'), or browse "
                             f"the category on site."),
                    "source_url": url,
                }]
            return []
        results = kw["Results"]
        if not isinstance(results, list):
            return []

        hits = []
        for entry in results:
            doc = entry.get("Document") if isinstance(entry, dict) else None
            if not isinstance(doc, dict):
                continue
            title = _first(doc, "name")
            rel = _first(doc, "url_detail")
            if not title or not rel:
                continue
            hits.append({
                "title": title,
                "url": BASE_URL + rel,
                "site": SITE,
                # MISUMI parts are configurable series, not final SKUs; the
                # results page carries the series/category code (unique_id) and
                # an empty sku. Concrete part number/price only exist after
                # on-site configuration — surface honestly as kind="series".
                "part_number": _first(doc, "unique_id"),
                "kind": "series",
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

        match = _DOWNLOAD_RE.search(content)
        if not match:
            return {"ok": False, "reason": "no_download_link",
                    "hint": "could not find a CAD download link on the product page"}
        download_url = match.group("href")
        if download_url.startswith("/"):
            download_url = BASE_URL + download_url

        part_match = _PART_RE.search(content)
        part_number = part_match.group("part").strip() if part_match else result.get("part_number")
        price_match = _PRICE_RE.search(content)

        dest, refusal = download_path(SITE, download_url, default="download.step")
        if refusal is not None:
            return refusal
        outcome = _session.browser_download(download_url, SITE, dest)
        if not outcome.get("ok"):
            return outcome

        return provenance_envelope(
            source_url=url,
            session_profile_used=outcome.get("session_profile_used", SITE),
            license_note=LICENSE_NOTE,
            artifact_path=outcome["dest"],
            title=result.get("title", ""),
            part_number=part_number,
            price=("$" + price_match.group("price")) if price_match else result.get("price"),
        )


ADAPTER = register(MisumiAdapter())
