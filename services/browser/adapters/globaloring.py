"""services/browser/adapters/globaloring.py — Global O-Ring source adapter (Parts Library V2).

Global O-Ring and Seal (globaloring.com): O-rings, seals, and gaskets sold
e-commerce with price plus specs. ``acquire`` uses the inherited default
``extract_params`` — :func:`~._retail.find_datasheet_url` grabs the datasheet/
spec PDF. Challenge pages degrade to ``login_required`` — never solved.

Live-verified 2026-07-10: the storefront is WooCommerce, so search is
``/?s=<query>&post_type=product`` (the old ``/search?q=`` guess 404'd). The catalog
is browsable with NO login — the header login widget's ``name="password"`` /
"Log In" markers rode on every page and falsely tripped the login wall, so those
are dropped. Results are a table listing of ``/product/<slug>/`` anchors whose text
is the O-ring part number; this adapter overrides :meth:`parse_search` to read them.
"""
from __future__ import annotations

import re

from . import register
from ._retail import RetailAdapter, _strip_tags

SITE = "globaloring"
BASE_URL = "https://www.globaloring.com"
SEARCH_URL_TMPL = BASE_URL + "/?s={query}&post_type=product"
LICENSE_NOTE = (
    "Global O-Ring product data: reference-only, link-out commerce data. This "
    "adapter never stores or redistributes Global O-Ring's page content — only a "
    "small price/spec record copied with provenance (source URL + fetch "
    "timestamp) for the user's own sourcing decision, never LLM-generated. "
    "Datasheets remain the manufacturer's documents fetched for the user's own "
    "engineering reference. See contracts/license_debt.md."
)

# The public catalog needs no login; the header login widget ("Log In" /
# name="password") rides every page, so it is NOT a wall marker here.
_LOGIN_WALL_MARKERS: tuple[str, ...] = ()
_CHALLENGE_MARKERS = ("Access Denied", "Pardon Our Interruption")

# WooCommerce table listing: each result is a `/product/<slug>/` anchor whose text
# is the O-ring part number. The thumbnail anchor to the same URL is empty, so
# requiring non-empty anchor text keeps one hit per product.
_CARD_RE = re.compile(
    r'<a[^>]*\bhref="(?P<href>https?://[^"]*/product/[^"]+)"[^>]*>(?P<title>[^<]*?)</a>',
    re.IGNORECASE)


class GlobalORingAdapter(RetailAdapter):
    key = SITE
    base_url = BASE_URL
    search_url_tmpl = SEARCH_URL_TMPL
    license_note = LICENSE_NOTE
    login_wall_markers = _LOGIN_WALL_MARKERS
    challenge_markers = _CHALLENGE_MARKERS

    def parse_search(self, content: str) -> list[dict]:
        hits: list[dict] = []
        seen: set[str] = set()
        for m in _CARD_RE.finditer(content):
            title = _strip_tags(m.group("title")).strip()
            if not title:
                continue
            url = m.group("href")
            if url in seen:
                continue
            seen.add(url)
            hits.append({"ok": True, "title": title, "url": url, "site": SITE,
                         "part_number": title})
        return hits


ADAPTER = register(GlobalORingAdapter())
