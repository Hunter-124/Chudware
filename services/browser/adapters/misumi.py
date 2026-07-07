"""services/browser/adapters/misumi.py — MISUMI source adapter (T-X7).

MISUMI (misumi-ec.com) is a configurable mechanical-components catalog
(shafts, linear guides, plates, fasteners) with per-part-number CAD downloads
(STEP) gated behind the user's own free MISUMI account. Like McMaster, this
is a CAD-download vendor: `acquire` lands a STEP file with provenance.

NOTE: the regexes/markers below are a v1 best-effort shape, not verified
against a live logged-in MISUMI session (T-X6 gates the real per-source
integration tests behind an env flag).
"""
from __future__ import annotations

import re

from .. import session as _session
from . import download_dir, is_login_wall, login_required, provenance_envelope, register

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
# Best-effort: MISUMI search result cards link to /vona2/detail/<series>/.
_RESULT_RE = re.compile(
    r'<a[^>]+class="[^"]*product-name[^"]*"[^>]+href="(?P<href>/vona2/detail/[^"?#]+)"[^>]*>'
    r'(?P<title>.*?)</a>', re.IGNORECASE | re.DOTALL)
_PART_RE = re.compile(r'<span[^>]*class="[^"]*part-number[^"]*"[^>]*>(?P<part>[^<]+)</span>',
                      re.IGNORECASE)
_PRICE_RE = re.compile(r'<span[^>]*class="[^"]*price[^"]*"[^>]*>\$(?P<price>[0-9,.]+)</span>',
                       re.IGNORECASE)
_DOWNLOAD_RE = re.compile(
    r'href="(?P<href>[^"]+\.(?:step|stp|iges|igs)(?:\?[^"]*)?)"', re.IGNORECASE)


def _strip_tags(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text).strip()


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
        hits = []
        for m in _RESULT_RE.finditer(content):
            card = m.group("title")
            part_match = _PART_RE.search(card)
            price_match = _PRICE_RE.search(card)
            hits.append({
                "title": _strip_tags(card),
                "url": BASE_URL + m.group("href"),
                "site": SITE,
                "part_number": part_match.group("part").strip() if part_match else None,
                "price": ("$" + price_match.group("price")) if price_match else None,
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
            part_number=part_number,
            price=("$" + price_match.group("price")) if price_match else result.get("price"),
        )


ADAPTER = register(MisumiAdapter())
