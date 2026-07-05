"""services/browser/adapters/traceparts.py — TraceParts source adapter (T-X2).

TraceParts requires an account to download most CAD, and a subset of supplier
catalogs additionally gate downloads behind a per-download terms
click-through (reseller/distribution agreements vary by supplier — this is
NOT uniform across TraceParts, unlike GrabCAD's simpler login-only gate).
This adapter never accepts such terms on the user's behalf: it detects the
click-through and surfaces :func:`terms_confirmation_required` instead.

Relationship to ``work/backlog/T-BL12.md`` ("TraceParts + Werk24 paid tiers",
parked, priority 5): that plan is the *paid API* route and is untouched by
this adapter, which uses the user's own authenticated browser session
instead — a different mechanism serving the same catalog.

NOTE: as with grabcad.py, the regexes/markers below are a v1 best-effort
shape, not verified against a live logged-in TraceParts session (T-X6 gates
the real per-source integration tests behind an env flag).
"""
from __future__ import annotations

import re

from .. import session as _session
from . import (download_dir, is_login_wall, login_required, provenance_envelope,
              register, terms_confirmation_required)

SITE = "traceparts"
BASE_URL = "https://www.traceparts.com"
SEARCH_URL_TMPL = BASE_URL + "/en/search?CatalogPath=TRACEPARTS%3ATRACEPARTS&Keywords={query}"
LICENSE_NOTE = (
    "TraceParts supplier catalogs: terms vary per supplier — some are open "
    "manufacturer catalogs, others gate CAD downloads behind reseller "
    "agreements accepted per-download. This adapter never auto-accepts those "
    "terms; see contracts/license_debt.md for the per-supplier posture as "
    "observed."
)

_LOGIN_WALL_MARKERS = ('Sign in to TraceParts', 'class="tp-login"', 'id="login-form"')
_TERMS_MARKERS = ('Accept the terms', 'class="supplier-terms"', 'License agreement required')
# Best-effort: TraceParts product cards link to /en/product/<slug>.
_RESULT_RE = re.compile(
    r'<a[^>]+class="[^"]*product-card[^"]*"[^>]+href="(?P<href>/en/product/[^"?#]+)"[^>]*>'
    r'(?P<title>.*?)</a>', re.IGNORECASE | re.DOTALL)
_DOWNLOAD_RE = re.compile(
    r'href="(?P<href>[^"]+\.(?:step|stp|iges|igs)(?:\?[^"]*)?)"', re.IGNORECASE)


def _strip_tags(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text).strip()


class TracePartsAdapter:
    key = SITE

    def search(self, query: str) -> list[dict]:
        url = SEARCH_URL_TMPL.format(query=query.replace(" ", "+"))
        result = _session.browser_fetch(url, SITE)
        if not result.get("ok"):
            return [result]
        content = result.get("content", "")
        if is_login_wall(content, _LOGIN_WALL_MARKERS):
            return [login_required(SITE, url)]
        return [
            {"title": _strip_tags(m.group("title")), "url": BASE_URL + m.group("href"), "site": SITE}
            for m in _RESULT_RE.finditer(content)
        ]

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
                SITE, url, "this supplier requires accepting per-download distribution terms")

        match = _DOWNLOAD_RE.search(content)
        if not match:
            return {"ok": False, "reason": "no_download_link",
                    "hint": "could not find a CAD download link on the product page"}
        download_url = match.group("href")
        if download_url.startswith("/"):
            download_url = BASE_URL + download_url

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
        )


ADAPTER = register(TracePartsAdapter())
