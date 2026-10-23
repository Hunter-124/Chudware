"""services/browser/adapters/anaheimautomation.py — anaheimautomation source adapter.

Anaheim Automation (anaheimautomation.com) makes stepper motors, drivers,
controllers and linear actuators on a Magento storefront with clean product
HTML. Search and CAD-model downloads are OPEN — no login is required to browse
the catalog or pull models; `acquire` lands the native 3D model with provenance
for part_search / part_place.

The search + result shapes below are verified against a live headless session
(2026-07-10): the catalog is Magento, so search is `/catalogsearch/result/?q=`
(the old `/search/search.php` guess 404'd) and result cards are Magento
`product-item-link` anchors carrying ABSOLUTE product URLs. CAD models are
series-level archives under `/media/anaheim/files/3D/...` (.rar/.zip/.step).
"""
from __future__ import annotations

import re

from .. import session as _session
from . import download_path, is_login_wall, login_required, provenance_envelope, register

SITE = "anaheimautomation"
# Canonical host is NON-www (www 308s → bare). Login cookies set on one host
# do not stick on the other, which is the main "I signed in then bounced back
# to login/referer" failure mode on this Magento storefront.
BASE_URL = "https://anaheimautomation.com"
POST_LOGIN_URL = BASE_URL + "/customer/account/"
# Clean Magento customer login (NO /login/referer/… path). The storefront's
# header "Sign In" link is self-referential
# (/login/referer/<base64 of the login page itself>/) and is what leaves users
# on a "login/referral" URL after a failed captcha/form_key — we deliberately
# open the plain login form; native Chrome login lands on POST_LOGIN_URL after
# Magento accepts the session.
LOGIN_URL = BASE_URL + "/customer/account/login/"
# MSP invisible reCAPTCHA rejects ANY Playwright/CDP-controlled session (live
# proven 2026-07-10: valid credentials → "reCAPTCHA verification failed").
# browser_login must open system Chrome with zero automation during sign-in.
NATIVE_CHROME_LOGIN = True
SEARCH_URL_TMPL = BASE_URL + "/catalogsearch/result/?q={query}"
LICENSE_NOTE = (
    "Anaheim Automation CAD models and datasheets are fetched under the "
    "user's own authenticated Anaheim Automation session for their own "
    "engineering use and are not redistributed. This adapter downloads only "
    "under the user's own authenticated Anaheim Automation session. See "
    "contracts/license_debt.md."
)

# Real Magento login markup (live 2026-07-10): id="login-form", class
# "form form-login", title "Customer Login". Older guesses (loginForm /
# login-form class) never matched and made wall detection unreliable.
_LOGIN_WALL_MARKERS = (
    "Customer Login",
    'id="login-form"',
    'class="form form-login"',
    "Please sign in to download",
    "reCAPTCHA verification failed",
    "Invalid Form Key",
)


def is_logged_in(content: str | None, url: str = "") -> bool:
    """True when Magento shows a post-login surface (account dashboard or
    header customer menu), never when still on login/create/referer auth."""
    u = (url or "").lower()
    if any(frag in u for frag in (
        "/customer/account/login",
        "/customer/account/create",
        "/customer/account/forgotpassword",
        "/customer/ajax/login",
    )):
        return False
    if "/customer/account" in u and "/login" not in u:
        return True
    hay = content or ""
    # Magento signed-in header / account chrome (not present on the login form).
    if re.search(r'class="[^"]*customer-welcome', hay, re.I):
        return True
    if re.search(r'href="[^"]*customer/account/logout', hay, re.I):
        return True
    if "Log Out" in hay or "Sign Out" in hay:
        return True
    return False


_RESULT_RE = re.compile(
    r'<a\b(?=[^>]*\bclass="[^"]*product-item-link)[^>]*\bhref="(?P<href>[^"]+)"[^>]*>'
    r'(?P<title>.*?)</a>', re.IGNORECASE | re.DOTALL)
_MFR_RE = re.compile(r'<span[^>]*class="[^"]*manufacturer[^"]*"[^>]*>(?P<mfr>[^<]+)</span>',
                     re.IGNORECASE)
# A product page's native CAD download link — Anaheim ships series-level 3D
# archives under /media/anaheim/files/3D/ (.rar today; keep STEP/zip/iges too).
_DOWNLOAD_RE = re.compile(
    r'href="(?P<href>[^"]+\.(?:step|stp|stl|igs|iges|zip|rar)(?:\?[^"]*)?)"', re.IGNORECASE)


def _strip_tags(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text).strip()


class AnaheimAutomationAdapter:
    key = SITE
    # expose hooks for embed.classify_login_state / start_url_for_site
    LOGIN_URL = LOGIN_URL
    BASE_URL = BASE_URL
    NATIVE_CHROME_LOGIN = NATIVE_CHROME_LOGIN
    _LOGIN_WALL_MARKERS = _LOGIN_WALL_MARKERS

    def is_logged_in(self, content: str | None, url: str = "") -> bool:
        return is_logged_in(content, url)

    def search(self, query: str) -> list[dict]:
        url = SEARCH_URL_TMPL.format(query=query.replace(" ", "+"))
        result = _session.browser_fetch(url, SITE)
        if not result.get("ok"):
            return [result]
        content = result.get("content", "")
        if is_login_wall(content, _LOGIN_WALL_MARKERS):
            return [login_required(SITE, url)]
        hits = []
        seen: set[str] = set()
        for m in _RESULT_RE.finditer(content):
            href = m.group("href")
            product_url = href if href.startswith("http") else BASE_URL + href
            if product_url in seen:  # Magento repeats each card (grid + list view)
                continue
            seen.add(product_url)
            card = m.group("title")
            mfr_match = _MFR_RE.search(card)
            hits.append({
                "title": _strip_tags(card),
                "url": product_url,
                "site": SITE,
                "manufacturer": mfr_match.group("mfr").strip() if mfr_match else "Anaheim Automation",
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


ADAPTER = register(AnaheimAutomationAdapter())
