"""services/browser/adapters/traceparts.py — TraceParts source adapter.

TraceParts requires an account to download most CAD, and a subset of supplier
catalogs additionally gate downloads behind a per-download terms
click-through (reseller/distribution agreements vary by supplier — this is
NOT uniform across TraceParts, unlike GrabCAD's simpler login-only gate).
This adapter never accepts such terms on the user's behalf: it detects the
click-through and surfaces :func:`terms_confirmation_required` instead.

This is the user's authenticated browser-session route, not a paid API integration.

NOTE: as with grabcad.py, the regexes/markers below are a best-effort shape,
not verified against a live logged-in TraceParts session. The real per-source
integration tests are gated by ``CHUDWARE_BROWSER_INTEGRATION_TESTS``.
"""
from __future__ import annotations

import html as html_mod
import re

from .. import session as _session
from . import (download_path, is_login_wall, login_required, provenance_envelope,
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
# Best-effort: TraceParts product links are /en/product/<slug>?CatalogPath=…&Product=…
# (live 2026-07-07: no product-card class, and the query string is REQUIRED — the
# Product/PartNumber params identify the part, so the href keeps it).
_RESULT_RE = re.compile(
    r'<a[^>]+href="(?P<href>/en/product/[^"]+)"[^>]*>(?P<title>.*?)</a>',
    re.IGNORECASE | re.DOTALL)
_DOWNLOAD_RE = re.compile(
    r'href="(?P<href>[^"]+\.(?:step|stp|iges|igs)(?:\?[^"]*)?)"', re.IGNORECASE)


def _strip_tags(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text)).strip()


class TracePartsAdapter:
    key = SITE

    def is_logged_in(self, content: str, url: str) -> bool:
        """Auth URLs and login-wall markup are never a logged-in session."""
        u = (url or "").lower()
        if "/sign-in" in u or "/sign-up" in u or "/login" in u:
            return False
        if any(m in content for m in _LOGIN_WALL_MARKERS):
            return False
        return True

    def search(self, query: str) -> list[dict]:
        url = SEARCH_URL_TMPL.format(query=query.replace(" ", "+"))
        result = _session.browser_fetch(url, SITE)
        if not result.get("ok"):
            return [result]
        content = result.get("content", "")
        if is_login_wall(content, _LOGIN_WALL_MARKERS):
            return [login_required(SITE, url)]
        # A product may be wrapped by several anchors (thumbnail, title); dedupe
        # by href, preferring the first anchor whose inner text is non-empty.
        seen: dict[str, str] = {}
        for m in _RESULT_RE.finditer(content):
            href = html_mod.unescape(m.group("href"))
            title = _strip_tags(m.group("title"))
            if href not in seen or (not seen[href] and title):
                seen[href] = title
        hits = []
        for href, title in seen.items():
            if not title:
                slug = href.split("/en/product/", 1)[1].split("?", 1)[0]
                title = slug.replace("-", " ").strip()
            hits.append({"title": title, "url": BASE_URL + href, "site": SITE})
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
                SITE, url, "this supplier requires accepting per-download distribution terms")

        match = _DOWNLOAD_RE.search(content)
        if not match:
            return {"ok": False, "reason": "no_download_link",
                    "hint": "could not find a CAD download link on the product page"}
        download_url = match.group("href")
        if download_url.startswith("/"):
            download_url = BASE_URL + download_url

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
        )


ADAPTER = register(TracePartsAdapter())
