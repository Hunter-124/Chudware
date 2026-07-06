"""services/browser/adapters/mcmaster.py — McMaster-Carr source adapter (T-X7).

McMaster-Carr is the canonical MRO/hardware catalog: unlike the commerce-only
Amazon adapter, McMaster has real per-SKU CAD downloads (STEP) behind the
user's own authenticated session, plus a per-download terms click-through on
some catalog pages (same shape as TraceParts — see traceparts.py). This
adapter never accepts those terms on the user's behalf; it detects the
click-through and surfaces :func:`terms_confirmation_required` instead.

NOTE: the regexes/markers below are a v1 best-effort shape, not verified
against a live logged-in McMaster-Carr session (T-X6 gates the real
per-source integration tests behind an env flag).
"""
from __future__ import annotations

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
_SKU_RE = re.compile(r'<span[^>]*class="[^"]*part-number[^"]*"[^>]*>(?P<sku>[^<]+)</span>', re.IGNORECASE)
_PRICE_RE = re.compile(r'<span[^>]*class="[^"]*price[^"]*"[^>]*>\$(?P<price>[0-9,.]+)</span>', re.IGNORECASE)
_DOWNLOAD_RE = re.compile(
    r'href="(?P<href>[^"]+\.(?:step|stp|iges|igs)(?:\?[^"]*)?)"', re.IGNORECASE)


def _strip_tags(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text).strip()


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
        for m in _RESULT_RE.finditer(content):
            sku_match = _SKU_RE.search(m.group("title"))
            hits.append({
                "title": _strip_tags(m.group("title")),
                "url": BASE_URL + m.group("href"),
                "site": SITE,
                "sku": sku_match.group("sku").strip() if sku_match else None,
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
