"""services/browser/adapters/amazon.py — Amazon source adapter (T-X7).

Amazon is COMMERCE, not a CAD source: there is no downloadable file here, only
a structured price/availability record copied from the product page with
provenance (see ``contracts/advisor.md`` §7 / ``contracts/research_tools.md``
§1 — "numbers copied-with-provenance, never generated"). This adapter never
downloads anything and never attempts redistribution of Amazon's content; it
only surfaces a link-out plus the price/availability fields a human would read
off the page themselves.

Amazon also runs aggressive bot detection. This adapter treats a CAPTCHA/
bot-challenge page exactly like a login wall — it returns ``login_required``
(never attempts to solve or bypass the challenge) so a human can complete it
in their own saved browser profile if they choose to.

NOTE: the regexes below are a v1 best-effort shape based on Amazon's public
page structure, not verified against a live session — that requires T-X6's
env-flag-gated integration tests to prove. Treat them as the seam to refine,
not as verified-correct markup matching.
"""
from __future__ import annotations

import re

from .. import session as _session
from . import is_login_wall, login_required, provenance_envelope, register

SITE = "amazon"
BASE_URL = "https://www.amazon.com"
SEARCH_URL_TMPL = BASE_URL + "/s?k={query}"
LICENSE_NOTE = (
    "Amazon product data: Amazon's Conditions of Use prohibit scraping product "
    "data for redistribution/republication. This adapter is link-out only — it "
    "never stores or redistributes Amazon's page content, only a small "
    "price/availability record copied with provenance (source URL + fetch "
    "timestamp) for the user's own sourcing decision, never LLM-generated. See "
    "contracts/license_debt.md."
)

# NOTE: 'name="signIn"' is deliberately absent — every ordinary Amazon page
# carries a hidden sign-in nav form with that attribute (live-probe 2026-07-07
# false positive); only markers unique to the actual auth interstitial belong here.
_LOGIN_WALL_MARKERS = ('id="ap_email"', 'Sign in to your account')
# Bot-challenge markers: never bypass, treat exactly like a login wall.
_CHALLENGE_MARKERS = (
    "Enter the characters you see",
    "/errors/validateCaptcha",
    "Robot Check",
    "Type the characters you see in this image",
)

# Best-effort: Amazon search result cards.  Handles both legacy markup (title
# in a separate <span class="a-text-normal">) and current markup where
# a-link-normal…a-text-normal share one <a> and the title is a class-less
# <span> inside an <h2> (live-probe 2026-07-07: old regex matched 0/34 cards).
_RESULT_RE = re.compile(
    r'data-asin="(?P<asin>[A-Z0-9]{10})"[^>]*>.*?'
    r'<a[^>]+class="[^"]*a-link-normal[^"]*"[^>]+href="(?P<href>/[^"?#]+)"[^>]*>\s*'
    r'(?:<h2[^>]*>\s*)?<span[^>]*>(?P<title>[^<]{3,300})</span>',
    re.IGNORECASE | re.DOTALL,
)
_PRICE_RE = re.compile(
    r'<span[^>]*class="[^"]*a-offscreen[^"]*"[^>]*>(?P<price>\$[0-9,.]+)</span>',
    re.IGNORECASE,
)
_AVAILABILITY_RE = re.compile(
    r'id="availability"[^>]*>\s*<span[^>]*>(?P<availability>[^<]+)</span>', re.IGNORECASE
)
_ASIN_RE = re.compile(r'"asin"\s*:\s*"(?P<asin>[A-Z0-9]{10})"', re.IGNORECASE)
_PRIME_RE = re.compile(r'aria-label="Amazon Prime"', re.IGNORECASE)


def _strip_tags(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text).strip()


class AmazonAdapter:
    key = SITE

    def search(self, query: str) -> list[dict]:
        url = SEARCH_URL_TMPL.format(query=query.replace(" ", "+"))
        result = _session.browser_fetch(url, SITE)
        if not result.get("ok"):
            return [result]
        content = result.get("content", "")
        if is_login_wall(content, _LOGIN_WALL_MARKERS):
            return [login_required(SITE, url)]
        if is_login_wall(content, _CHALLENGE_MARKERS):
            return [login_required(SITE, url)]
        hits = []
        for m in _RESULT_RE.finditer(content):
            price_match = _PRICE_RE.search(content, m.end())
            hits.append({
                "title": _strip_tags(m.group("title")),
                "url": BASE_URL + m.group("href"),
                "site": SITE,
                "asin": m.group("asin"),
                "price": price_match.group("price") if price_match else None,
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
        # Bot-challenge: never attempt bypass, surface exactly like a login wall.
        if is_login_wall(content, _CHALLENGE_MARKERS):
            return login_required(SITE, url)

        price_match = _PRICE_RE.search(content)
        availability_match = _AVAILABILITY_RE.search(content)
        asin_match = _ASIN_RE.search(content)

        return provenance_envelope(
            source_url=url,
            session_profile_used=page.get("session_profile_used", SITE),
            license_note=LICENSE_NOTE,
            artifact_path=None,
            price=price_match.group("price") if price_match else None,
            availability=_strip_tags(availability_match.group("availability")) if availability_match else None,
            asin=asin_match.group("asin") if asin_match else result.get("asin"),
            prime=bool(_PRIME_RE.search(content)),
            currency="USD",
        )


ADAPTER = register(AmazonAdapter())
