"""services/browser/adapters/wch.py — WCH (Nanjing Qinheng Microelectronics) source adapter (Parts Library V2).

WCH is a Chinese MCU / USB-bridge vendor (CH340 USB-UART bridges, CH32V RISC-V
MCUs). It is primarily a DATASHEET / driver source — each chip ships a direct
datasheet PDF and driver archives under ``/downloads/``.

Live-verified 2026-07-10: the old ``/en/search?q=`` guess 302-redirects to a
dedicated Vue (Element-Plus) search app on ``search.wch-ic.com`` whose result
cards are rendered from an XHR and are NOT present in a plain fetch of the main
site. The rendered results are DOCUMENT hits: each is an
``<a href="https://www.wch-ic.com/downloads/<NAME>_PDF.html">Title</a>`` followed
by a ``<div class="file-content">description</div>`` (datasheets, drivers,
app-notes). No per-product SKU/price exists on this document index, so this
adapter overrides :meth:`parse_search` to emit ``kind="document"`` hits with the
datasheet/driver URL + title (the ``data-v-*`` scoped-style hash is a per-build
value, so the regex keys on the stable ``/downloads/`` href + ``file-content``
class instead). Open catalog — no login.
"""
from __future__ import annotations

import re

from . import register
from ._retail import RetailAdapter, _strip_tags

SITE = "wch"
BASE_URL = "https://www.wch-ic.com"
# The header search box posts to ``search.wch-ic.com`` with ?q=<query>&t=all
# (``t=all`` is the "everything" tab; the rendered results are the downloads
# index). Fetched under CEF so the Vue app's XHR result cards render.
SEARCH_URL_TMPL = "https://search.wch-ic.com/?q={query}&t=all"
LICENSE_NOTE = (
    "WCH datasheets remain WCH / Nanjing Qinheng's own manufacturer documents, "
    "fetched here for the user's own engineering reference only. This adapter "
    "never redistributes or LLM-generates page content — only a small "
    "spec/part record copied with provenance (source URL + fetch timestamp) and "
    "the manufacturer's datasheet saved for the user. See contracts/license_debt.md."
)

_LOGIN_WALL_MARKERS = ("Sign In", "Log In", 'id="login"')
_CHALLENGE_MARKERS = ("Access Denied", "Reference #", "Pardon Our Interruption")

# One WCH search result: a downloads-index anchor immediately followed by a
# ``file-content`` description div. Keyed on the stable ``/downloads/`` href and
# the ``file-content`` class — NOT the per-build ``data-v-<hash>`` scope marker.
# The title carries ``<em>`` search-highlight spans, stripped by ``_strip_tags``.
_RESULT_RE = re.compile(
    r'<a\b[^>]*\bhref="(?P<href>https://www\.wch-ic\.com/downloads/[^"]+)"[^>]*>'
    r'(?P<title>.*?)</a>\s*</div>\s*'
    r'<div\b[^>]*\bclass="file-content"[^>]*>(?P<desc>.*?)</div>',
    re.IGNORECASE | re.DOTALL)


class WchAdapter(RetailAdapter):
    key = SITE
    base_url = BASE_URL
    search_url_tmpl = SEARCH_URL_TMPL
    license_note = LICENSE_NOTE
    login_wall_markers = _LOGIN_WALL_MARKERS
    challenge_markers = _CHALLENGE_MARKERS

    def parse_search(self, content: str) -> list[dict]:
        """WCH search returns a datasheet/driver DOCUMENT index (no product SKU
        or price). Parse each downloads anchor + its ``file-content`` blurb."""
        hits: list[dict] = []
        seen: set[str] = set()
        for m in _RESULT_RE.finditer(content):
            url = m.group("href")
            if url in seen:
                continue
            seen.add(url)
            title = _strip_tags(m.group("title"))
            if not title:
                continue
            hit = {
                "ok": True,
                "title": title,
                "url": url,
                "site": SITE,
                "kind": "document",
            }
            desc = _strip_tags(m.group("desc"))
            if desc:
                hit["description"] = desc
            hits.append(hit)
        return hits


ADAPTER = register(WchAdapter())
