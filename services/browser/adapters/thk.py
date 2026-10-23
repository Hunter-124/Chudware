"""services/browser/adapters/thk.py — THK source adapter.

THK (thk.com) makes linear motion guides, ball screws and actuators. Search
is public; per-part CAD (STEP) downloads sit behind the user's own THK
account. `acquire` lands the STEP model with provenance for part_search /
part_place.

NOTE: search() is verified against a saved real THK site-search page (the
"popfind" widget renders result cards as <div class="_item"> with a
<div class="_title"><a href=...> and a sibling <div class="_thumbnail">).
The acquire() download markers remain a best-effort shape, not verified
against a live logged-in THK session (the gated integration suite covers the real per-source
integration tests behind an env flag).
"""
from __future__ import annotations

import re

from .. import session as _session
from . import download_path, is_login_wall, login_required, provenance_envelope, register

SITE = "thk"
BASE_URL = "https://www.thk.com"
SEARCH_URL_TMPL = BASE_URL + "/us/en/search/?q={query}"
LICENSE_NOTE = (
    "THK CAD models and datasheets are fetched under the user's own "
    "authenticated THK session for their own engineering use and are not "
    "redistributed. This adapter downloads only under the user's own "
    "authenticated THK session. See contracts/license_debt.md."
)

_LOGIN_WALL_MARKERS = ('Log in to THK', 'id="loginForm"', 'class="login-form"',
                       'Please sign in to download')
# THK site search is served by the "popfind" widget: results live in
# <div class="_item"> cards, each with a <div class="_title"><a href=...>Title</a>.
# Hrefs are already absolute https URLs; the thumbnail sits in a sibling
# <div class="_thumbnail"><a><img src=...>.
_ITEM_SPLIT_RE = re.compile(r'<div class="_item"', re.IGNORECASE)
_TITLE_RE = re.compile(
    r'<div class="_title">\s*<a[^>]+href="(?P<href>https?://[^"]+)"[^>]*>(?P<title>.*?)</a>',
    re.IGNORECASE | re.DOTALL)
_THUMB_RE = re.compile(r'<div class="_thumbnail">.*?<img[^>]+src="(?P<src>[^"]+)"',
                       re.IGNORECASE | re.DOTALL)
_MFR_RE = re.compile(r'<span[^>]*class="[^"]*manufacturer[^"]*"[^>]*>(?P<mfr>[^<]+)</span>',
                     re.IGNORECASE)
# Best-effort: a product page's native CAD download link (STEP model or zip).
_DOWNLOAD_RE = re.compile(
    r'href="(?P<href>[^"]+\.(?:step|stp|zip)(?:\?[^"]*)?)"', re.IGNORECASE)


def _strip_tags(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text).strip()


class ThkAdapter:
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
        # THK search results are popfind "_item" cards; split on them and pull
        # the title anchor (absolute href) + optional thumbnail from each card.
        for card in _ITEM_SPLIT_RE.split(content)[1:]:
            tm = _TITLE_RE.search(card)
            if not tm:
                continue
            title = _strip_tags(tm.group("title"))
            if not title:
                continue
            hit = {
                "title": title,
                "url": tm.group("href"),
                "site": SITE,
                "manufacturer": "THK",
            }
            thumb = _THUMB_RE.search(card)
            if thumb:
                hit["thumbnail"] = thumb.group("src")
            hits.append(hit)
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
                    "hint": "could not find a STEP/native CAD download link on the product page"}
        download_url = match.group("href")
        if download_url.startswith("/"):
            download_url = BASE_URL + download_url

        dest, refusal = download_path(SITE, download_url, default="download.step")
        if refusal is not None:
            return refusal
        outcome = _session.browser_download(download_url, SITE, dest)
        if not outcome.get("ok"):
            return outcome

        mfr_match = _MFR_RE.search(content)
        return provenance_envelope(
            source_url=url,
            session_profile_used=outcome.get("session_profile_used", SITE),
            license_note=LICENSE_NOTE,
            artifact_path=outcome["dest"],
            title=result.get("title", ""),
            manufacturer=mfr_match.group("mfr").strip() if mfr_match else result.get("manufacturer"),
        )


ADAPTER = register(ThkAdapter())
