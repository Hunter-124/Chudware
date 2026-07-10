"""services/browser/adapters/datasheet4u.py — Datasheet4U source adapter (Parts Library V2).

Datasheet4U (datasheet4u.com) is a pure datasheet DATABASE — roughly 900k parts
served as static, ad-heavy HTML with direct PDF downloads. There is no commerce
layer: ``search`` returns part rows and ``acquire`` downloads the datasheet PDF.
This source feeds the reverse-engineering spec pipeline (a manufacturer datasheet
is often the only formal geometry/pinout reference for an unknown part).

The key deliverable is ``find_datasheet_url`` grabbing the PDF on the detail page.
Open catalog — no login. Challenge pages degrade to ``login_required``.

Live-verified 2026-07-10: search is ``/share_search.php?sWord=`` (the old
``/search?q=`` guess 404'd). Results are a server-rendered table of rows carrying a
``mfr-name`` span (manufacturer), an ``<a class="line" href="/datasheets/...">`` part
anchor and a Description cell — no JSON-LD — so this adapter overrides
:meth:`parse_search` to read those rows.
"""
from __future__ import annotations

import re

from . import register
from ._retail import RetailAdapter, _strip_tags

SITE = "datasheet4u"
BASE_URL = "https://www.datasheet4u.com"
SEARCH_URL_TMPL = BASE_URL + "/share_search.php?sWord={query}"
LICENSE_NOTE = (
    "Datasheets served by Datasheet4U remain the respective manufacturers' own "
    "documents, fetched here for the user's own engineering reference only. This "
    "adapter never redistributes or LLM-generates page content — only a small "
    "part record copied with provenance (source URL + fetch timestamp) and the "
    "manufacturer's datasheet saved for the user. See contracts/license_debt.md."
)

_LOGIN_WALL_MARKERS: tuple[str, ...] = ()  # public datasheet DB — no login to browse
_CHALLENGE_MARKERS = ("Access Denied", "Pardon Our Interruption")

# A result row: the part anchor `<a class="line" href="/datasheets/<mfr>/<part>/<id>">`
# holds the URL + part number; the row's `mfr-name` span and Description cell give the
# manufacturer and blurb.
_ROW_RE = re.compile(r'<tr\b[^>]*>(?P<row>(?:(?!</tr>).)*?/datasheets/.*?)</tr>',
                     re.IGNORECASE | re.DOTALL)
_PART_RE = re.compile(
    r'<a\b[^>]*\bclass="line"[^>]*\bhref="(?P<href>/datasheets/[^"]+)"[^>]*>(?P<part>.*?)</a>',
    re.IGNORECASE | re.DOTALL)
_MFR_RE = re.compile(r'class="mfr-name"[^>]*>(?P<mfr>[^<]+)</span>', re.IGNORECASE)
_DESC_RE = re.compile(r'data-label="Description"[^>]*>(?P<desc>.*?)</td>', re.IGNORECASE | re.DOTALL)


class Datasheet4uAdapter(RetailAdapter):
    key = SITE
    base_url = BASE_URL
    search_url_tmpl = SEARCH_URL_TMPL
    license_note = LICENSE_NOTE
    login_wall_markers = _LOGIN_WALL_MARKERS
    challenge_markers = _CHALLENGE_MARKERS

    def parse_search(self, content: str) -> list[dict]:
        hits: list[dict] = []
        seen: set[str] = set()
        for rm in _ROW_RE.finditer(content):
            row = rm.group("row")
            pm = _PART_RE.search(row)
            if not pm:
                continue
            href = pm.group("href")
            url = href if href.startswith("http") else BASE_URL + href
            if url in seen:
                continue
            seen.add(url)
            part = _strip_tags(pm.group("part"))
            hit = {"ok": True, "title": part, "url": url, "site": SITE, "part_number": part}
            mm = _MFR_RE.search(row)
            if mm:
                hit["manufacturer"] = mm.group("mfr").strip()
            dm = _DESC_RE.search(row)
            if dm:
                desc = _strip_tags(dm.group("desc"))
                if desc:
                    hit["description"] = desc
            hits.append(hit)
        return hits


ADAPTER = register(Datasheet4uAdapter())
