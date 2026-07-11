"""services/browser/adapters/marcorubber.py — Marco Rubber source adapter (Parts Library V2).

Marco Rubber (marcorubber.com): O-rings and seals with a material selector.
Product pages carry spec PDFs, so ``acquire`` uses the inherited default
``extract_params`` — :func:`~._retail.find_datasheet_url` grabs the datasheet/
spec PDF. Challenge/login pages degrade to ``login_required`` — never solved.

Live-verified 2026-07-10: the site is an Angular SPA (the old ``/search?q=``
guess and ``?s=`` both served the app shell — byte-identical to the homepage).
The public catalog needs NO login: the header "Sign In" link rode every page and
falsely tripped the login wall (there is no ``name="password"`` widget, no
captcha, no 403 — a confirmed false positive, so those markers are dropped). The
real query-driven search is the lazy-loaded Angular route
``/search-results?search=<q>`` (found in main.js): after JS settle it renders a
list of ``<a class="search-header" href="…">Title</a>`` result anchors (query
highlighted with ``<b>`` — stripped), including the ``/store?searchQuery=<q>``
commerce listing plus material/reference pages. This adapter overrides
:meth:`parse_search` to read them (relative hrefs → joined onto ``base_url``).
"""
from __future__ import annotations

import html
import re
from urllib.parse import urljoin

from . import register
from ._retail import RetailAdapter, _strip_tags

SITE = "marcorubber"
BASE_URL = "https://www.marcorubber.com"
SEARCH_URL_TMPL = BASE_URL + "/search-results?search={query}"
LICENSE_NOTE = (
    "Marco Rubber product data: reference-only, link-out commerce data. This "
    "adapter never stores or redistributes Marco Rubber's page content — only a "
    "small price/spec record copied with provenance (source URL + fetch "
    "timestamp) for the user's own sourcing decision, never LLM-generated. "
    "Datasheets remain the manufacturer's documents fetched for the user's own "
    "engineering reference. See contracts/license_debt.md."
)

# The public catalog needs no login; the header "Sign In" link rides every page
# (no password field), so it is NOT a wall marker here — confirmed false positive.
_LOGIN_WALL_MARKERS: tuple[str, ...] = ()
_CHALLENGE_MARKERS = ("Access Denied", "Pardon Our Interruption")

# SPA search results: each hit is an `<a class="search-header" href="…">Title</a>`.
_CARD_RE = re.compile(
    r'<a[^>]*class="[^"]*search-header[^"]*"[^>]*href="(?P<href>[^"]+)"[^>]*>'
    r'(?P<title>.*?)</a>',
    re.IGNORECASE | re.DOTALL)


class MarcoRubberAdapter(RetailAdapter):
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
            url = urljoin(self.base_url, m.group("href"))
            if url in seen:
                continue
            title = html.unescape(_strip_tags(m.group("title"))).strip()
            if not title:
                continue
            seen.add(url)
            hits.append({"ok": True, "title": title, "url": url, "site": SITE})
        return hits


ADAPTER = register(MarcoRubberAdapter())
