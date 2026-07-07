"""services/browser/adapters/cadenas.py — CADENAS PARTcommunity / 3Dfindit adapter (T-X7).

3Dfindit.com / PARTcommunity is the CADENAS white-label CAD portal engine behind
thousands of manufacturer catalogs — the STEP/native geometry only emerges after
driving a JavaScript part configurator (and usually a login). This v1 CANNOT drive
that interactive configurator headlessly, so it is HONEST about the limitation:

- `search` best-effort parses result cards if the (often JS-rendered) markup is
  present, so callers still get candidate part links.
- `acquire` does NOT attempt to fake a download: it degrades to login_required
  (or terms_confirmation_required) because the actual STEP artifact is gated
  behind interactive configuration this adapter cannot perform. When and if a
  configurator-driving capability lands, this is the seam to upgrade.

NOTE: the regexes/markers below are a v1 best-effort shape, not verified against
a live CADENAS session (T-X6 gates the real per-source integration tests behind
an env flag).
"""
from __future__ import annotations

import re

from .. import session as _session
from . import (is_login_wall, login_required, register, terms_confirmation_required)

SITE = "cadenas"
BASE_URL = "https://www.3dfindit.com"
SEARCH_URL_TMPL = BASE_URL + "/en/search?q={query}"
LICENSE_NOTE = (
    "CADENAS PARTcommunity / 3Dfindit CAD models are supplied by manufacturer "
    "catalogs to registered users under each manufacturer's terms, produced on "
    "demand by an interactive configurator. This adapter records provenance but "
    "does not download: the STEP only emerges after interactive configuration "
    "and login that this v1 cannot perform. See contracts/license_debt.md."
)
_TERMS_SUMMARY = (
    "the STEP model is generated only after you drive the manufacturer's part "
    "configurator and accept its terms; this adapter cannot do that on your behalf"
)

_LOGIN_WALL_MARKERS = ('Please log in', 'id="id_username"', 'class="login-form"',
                       'Sign in to download', 'Login required')
# Best-effort: 3Dfindit result cards link to /en/component/<id> or /en/details/....
_RESULT_RE = re.compile(
    r'<a[^>]+class="[^"]*result-card[^"]*"[^>]+href="(?P<href>/en/(?:component|details)/[^"?#]+)"[^>]*>'
    r'(?P<title>.*?)</a>', re.IGNORECASE | re.DOTALL)
_MFR_RE = re.compile(r'<span[^>]*class="[^"]*manufacturer[^"]*"[^>]*>(?P<mfr>[^<]+)</span>',
                     re.IGNORECASE)


def _strip_tags(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text).strip()


class CadenasAdapter:
    key = SITE

    def search(self, query: str) -> list[dict]:
        url = SEARCH_URL_TMPL.format(query=query.replace(" ", "+"))
        result = _session.browser_fetch(url, SITE)
        if not result.get("ok"):
            return [result]
        content = result.get("content", "")
        if is_login_wall(content, _LOGIN_WALL_MARKERS):
            return [login_required(SITE, url)]
        hits = []
        for m in _RESULT_RE.finditer(content):
            card = m.group("title")
            mfr_match = _MFR_RE.search(card)
            hits.append({
                "title": _strip_tags(card),
                "url": BASE_URL + m.group("href"),
                "site": SITE,
                "manufacturer": mfr_match.group("mfr").strip() if mfr_match else None,
            })
        return hits

    def acquire(self, result: dict) -> dict:
        url = result.get("url", "")
        if not url:
            return {"ok": False, "reason": "bad_result", "hint": "result has no 'url'"}

        page = _session.browser_fetch(url, SITE)
        if page.get("ok"):
            content = page.get("content", "")
            if is_login_wall(content, _LOGIN_WALL_MARKERS):
                return login_required(SITE, url)

        # Honest degradation: the STEP artifact is gated behind an interactive
        # JS configurator (and terms click-through) this v1 cannot drive.
        return terms_confirmation_required(SITE, url, _TERMS_SUMMARY)


ADAPTER = register(CadenasAdapter())
