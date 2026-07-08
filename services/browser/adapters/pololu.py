"""services/browser/adapters/pololu.py — Pololu source adapter (Parts Library V2).

Pololu (pololu.com) is a small-robotics electronics vendor — micro metal
gearmotors, motor controllers/drivers, wheels, encoders, and other student- and
hobby-robotics building blocks. Each product carries a per-product page with a
price/spec record and (often) a manufacturer datasheet PDF; this adapter treats
Pololu as a commerce + datasheet reference source, never a CAD-model source.

Unlike most retail sites, Pololu's search page ships NO JSON-LD Product blocks,
so the default :class:`~._retail.RetailAdapter` JSON-LD flow finds nothing here.
This adapter therefore OVERRIDES :meth:`parse_search` with a small HTML parser:
the search page repeats ``<a href="/product/NNNN">`` links whose text is the
product name (often prefixed ``Pololu item NNNN: <Name>``, from which the item
number is lifted when present — never invented). Open catalog — no login.
Challenge pages degrade to ``login_required`` — never solved.

Provenance-only: this adapter never redistributes or LLM-generates page content —
only a small price/spec record copied with provenance (source URL + fetch
timestamp) and the manufacturer's datasheet saved for the user's own engineering
reference. See contracts/license_debt.md.

v1 best-effort, offline-verified against a saved search page; live tests gated.
"""
from __future__ import annotations

import re
from urllib.parse import urljoin

from . import register
from ._retail import RetailAdapter, _strip_tags

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
_CHALLENGE_MARKERS = ("Access Denied", "Reference #", "Pardon Our Interruption")

# Product-result anchors: <a href="/product/NNNN">name…</a>. The search page
# emits two anchors per result (an empty image link + the named text link); we
# keep only the ones with visible text and de-duplicate by product URL.
_PRODUCT_ANCHOR_RE = re.compile(
    r'<a\b[^>]*href="(/product/\d+)"[^>]*>(.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)
# "Pololu item 2590: Optical Encoder Pair Kit…" → item number + clean title.
_ITEM_PREFIX_RE = re.compile(r"^Pololu item (\d+):\s*(.*)$", re.IGNORECASE)


class PololuAdapter(RetailAdapter):
    key = SITE
    base_url = BASE_URL
    search_url_tmpl = SEARCH_URL_TMPL
    license_note = LICENSE_NOTE
    login_wall_markers = _LOGIN_WALL_MARKERS
    challenge_markers = _CHALLENGE_MARKERS

    def parse_search(self, content: str) -> list[dict]:
        """Pololu ships no JSON-LD, so parse ``/product/NNNN`` result anchors.

        De-duplicates repeated links to the same product, skips image/nav links
        that carry no visible text, and lifts the Pololu item number out of the
        ``Pololu item NNNN:`` title prefix when present (never invented).
        """
        hits: list[dict] = []
        seen: set[str] = set()
        for m in _PRODUCT_ANCHOR_RE.finditer(content or ""):
            href = m.group(1)
            title = _strip_tags(m.group(2))
            if not title:
                continue  # image-only / empty link — not a result row
            if href in seen:
                continue
            seen.add(href)

            part_number = None
            pm = _ITEM_PREFIX_RE.match(title)
            if pm:
                part_number = pm.group(1)
                title = pm.group(2).strip() or title

            hit = {
                "ok": True,
                "title": title,
                "url": urljoin(self.base_url, href),
                "site": self.key,
            }
            if part_number:
                hit["part_number"] = part_number
            hits.append(hit)
        return hits


ADAPTER = register(PololuAdapter())
