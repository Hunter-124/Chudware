"""services/browser/adapters/pololu.py — Pololu source adapter (Parts Library V2).

Pololu (pololu.com) is a small-robotics electronics vendor — micro metal
gearmotors, motor controllers/drivers, wheels, encoders, and other student- and
hobby-robotics building blocks. Each product carries a per-product page with a
price/spec record and (often) a manufacturer datasheet PDF; this adapter treats
Pololu as a commerce + datasheet reference source, never a CAD-model source.

Unlike most retail sites, Pololu's search page ships NO JSON-LD Product blocks,
so the default :class:`~._retail.RetailAdapter` JSON-LD flow finds nothing here.
This adapter parses search-result rows, pairing the product image and named
anchor by product URL. The item number is lifted only when explicitly printed.
The catalog is public; challenges are reported and never solved.

Provenance-only: this adapter never redistributes or LLM-generates page content —
only a small price/spec record copied with provenance (source URL + fetch
timestamp) and the manufacturer's datasheet saved for the user's own engineering
reference. See contracts/license_debt.md.
"""
from __future__ import annotations

import re
from html import unescape

from . import abs_url, make_hit, register
from ._retail import RetailAdapter, _strip_tags, card_image

SITE = "pololu"
BASE_URL = "https://www.pololu.com"
SEARCH_URL_TMPL = BASE_URL + "/search?query={query}"
LICENSE_NOTE = (
    "Pololu product data is reference-only link-out commerce data, and any "
    "datasheet remains the manufacturer's own document, fetched here for the "
    "user's own engineering reference only. This adapter never redistributes or "
    "LLM-generates page content — only a small price/spec record copied with "
    "provenance (source URL + fetch timestamp) and the manufacturer's datasheet "
    "saved for the user. See contracts/license_debt.md."
)

# Pololu's global header carries a plain "Log In" nav link on EVERY page, so we
# can't use it as a wall marker (substring match ⇒ false positive on results).
# These phrases only appear when the page genuinely gates access.
_LOGIN_WALL_MARKERS = ("Please log in to continue", "You must be logged in")
_CHALLENGE_MARKERS = (
    "Access Denied", "Reference #", "Pardon Our Interruption",
    "Verify you are human", "verify you are human", "<h1>CAPTCHA</h1>", "Please complete the CAPTCHA",
)

# Search rows have an image link and a named link; the right-hand sidebar also
# contains product links but must not be mistaken for search matches.
_SEARCH_LIST_RE = re.compile(
    r'''<dl\b[^>]*\bid=["']search["'][^>]*>(.*?)</dl>''', re.IGNORECASE | re.DOTALL,
)
_SEARCH_ROW_RE = re.compile(r"<dt\b[^>]*>(.*?)</dt>", re.IGNORECASE | re.DOTALL)
_PRODUCT_ANCHOR_RE = re.compile(
    r'''<a\b[^>]*\bhref=["'](?P<href>/product/\d+)["'][^>]*>(?P<body>.*?)</a>''',
    re.IGNORECASE | re.DOTALL,
)
# "Pololu item 2590: Optical Encoder Pair Kit…" → item number + clean title.
_ITEM_PREFIX_RE = re.compile(r"^Pololu item (\d+):\s*(.*)$", re.IGNORECASE)


class PololuAdapter(RetailAdapter):
    key = SITE
    hosts = ("pololu.com",)
    result_kinds = ("product",)
    cad_acquisition = False
    login_required = False
    api = False
    base_url = BASE_URL
    search_url_tmpl = SEARCH_URL_TMPL
    license_note = LICENSE_NOTE
    login_wall_markers = _LOGIN_WALL_MARKERS
    challenge_markers = _CHALLENGE_MARKERS

    def parse_search(self, content: str) -> list[dict]:
        """Parse only search rows and associate images by their linked product."""
        hits: list[dict] = []
        seen: set[str] = set()
        search_list = _SEARCH_LIST_RE.search(content or "")
        if not search_list:
            return []
        for row in _SEARCH_ROW_RE.finditer(search_list.group(1)):
            anchors = list(_PRODUCT_ANCHOR_RE.finditer(row.group(1)))
            images = {
                anchor.group("href"): image
                for anchor in anchors
                if (image := card_image(anchor.group(), self.base_url))
            }
            for anchor in anchors:
                href = anchor.group("href")
                title = unescape(_strip_tags(anchor.group("body"))).strip()
                if not title or href in seen:
                    continue
                url = abs_url(self.base_url, href)
                if not url:
                    continue
                seen.add(href)
                part_number = None
                prefix = _ITEM_PREFIX_RE.match(title)
                if prefix:
                    part_number = prefix.group(1)
                    title = prefix.group(2).strip() or title
                hits.append(make_hit(
                    self.key, title=title, url=url, sku=part_number,
                    thumbnail=images.get(href), extra={"ok": True},
                ))
        return hits


ADAPTER = register(PololuAdapter())
