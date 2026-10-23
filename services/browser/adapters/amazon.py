"""services/browser/adapters/amazon.py — Amazon source adapter.

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

LIVE SHAPE (verified 2026-07-07 against a real fetched
``https://www.amazon.com/s?k=m3+screw`` page, HTTP 200, no captcha): the search
results page is SERVER-RENDERED. Each result is a ``<div ... data-asin="B0…"
data-component-type="s-search-result">`` card carrying a non-empty 10-char
ASIN. The title lives in ``<a class="a-link-normal s-line-clamp-2 … s-link-style
a-text-normal" href="…"><h2 …><span>TITLE</span></h2></a>``; the price is a
``<span class="a-offscreen">$X.YZ</span>`` inside an ``<span class="a-price">``
block. ``search`` splits the page on card boundaries and parses title/price
WITHIN each card slice (so a priceless card yields ``price=None`` instead of
borrowing the next card's price). The ASIN is the natural SKU and the canonical
product URL is ``https://www.amazon.com/dp/<ASIN>`` (sponsored hrefs are
``/sspa/click?…`` tracking redirects, so we do NOT trust the raw href).

The ``acquire()`` product-page regexes below remain a best-effort shape not yet
live-verified against a real product page.
"""
from __future__ import annotations

import html
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

# Live shape (verified 2026-07-07): each result card is a <div … data-asin="B0…"
# data-component-type="s-search-result">. We locate the card boundaries with this
# marker (it matched all 22 organic+sponsored cards on the real page) and parse
# title/price WITHIN each card slice — never across card boundaries.
_CARD_RE = re.compile(
    r'data-asin="(?P<asin>[A-Z0-9]{10})"[^>]*data-component-type="s-search-result"',
    re.IGNORECASE,
)
# Title: the s-line-clamp title anchor, then <h2 …><span>TITLE</span></h2>.
_TITLE_RE = re.compile(
    r'<a[^>]*class="[^"]*s-line-clamp[^"]*"[^>]*>\s*'
    r'<h2[^>]*>\s*<span[^>]*>(?P<title>[^<]{2,400})</span>',
    re.IGNORECASE | re.DOTALL,
)
# Price: <span class="a-offscreen">$X.YZ</span> — only a bare "$"-leading value,
# so "List: $9.99" strike-through prices are skipped.
_PRICE_RE = re.compile(
    r'<span[^>]*class="[^"]*a-offscreen[^"]*"[^>]*>(?P<price>\$[0-9][0-9,.]*)</span>',
    re.IGNORECASE,
)
_AVAILABILITY_RE = re.compile(
    r'id="availability"[^>]*>\s*<span[^>]*>(?P<availability>[^<]+)</span>', re.IGNORECASE
)
_ASIN_RE = re.compile(r'"asin"\s*:\s*"(?P<asin>[A-Z0-9]{10})"', re.IGNORECASE)
_PRIME_RE = re.compile(r'aria-label="Amazon Prime"', re.IGNORECASE)


def _strip_tags(text: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", text)).strip()


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
        # Split the page on card boundaries and parse each card slice in
        # isolation so title/price never leak across cards.
        cards = list(_CARD_RE.finditer(content))
        hits = []
        for idx, m in enumerate(cards):
            asin = m.group("asin")
            if not asin:
                continue  # skip cards with an empty ASIN
            start = m.start()
            end = cards[idx + 1].start() if idx + 1 < len(cards) else len(content)
            block = content[start:end]
            title_match = _TITLE_RE.search(block)
            if not title_match:
                continue  # no readable title → not a real product card
            price_match = _PRICE_RE.search(block)
            hits.append({
                "title": _strip_tags(title_match.group("title")),
                # Canonical product URL from the ASIN — sponsored hrefs are
                # /sspa/click tracking redirects, so we never trust the raw href.
                "url": BASE_URL + "/dp/" + asin,
                "site": SITE,
                "sku": asin,   # the ASIN is the natural Amazon SKU
                "asin": asin,
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
