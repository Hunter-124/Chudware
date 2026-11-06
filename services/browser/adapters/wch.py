"""services/browser/adapters/wch.py — WCH (Nanjing Qinheng Microelectronics) source adapter (Parts Library V2).

WCH search is a rendered Vue index of datasheets, drivers and application
notes. A hit is a document/reference page, not a product or CAD model. A bare
unrendered SPA shell is not evidence of an empty search.
"""
from __future__ import annotations

import html
import re

from . import abs_url, make_hit, register
from ._retail import RetailAdapter, _strip_tags, card_image

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

_LOGIN_WALL_MARKERS: tuple[str, ...] = ()
_CHALLENGE_MARKERS = ("Access Denied", "Reference #", "Pardon Our Interruption")

_RESULT_RE = re.compile(
    r"""<a\b[^>]*\bhref=["'](?P<href>(?:(?:https?:)?//(?:www\.)?wch-ic\.com)?/downloads/[^"']+)["'][^>]*>"""
    r"(?P<title>.*?)</a>",
    re.IGNORECASE | re.DOTALL)
_DESC_RE = re.compile(r"""<div\b[^>]*\bclass=["'][^"']*\bfile-content\b[^"']*["'][^>]*>"""
                      r"(?P<desc>.*?)</div>", re.IGNORECASE | re.DOTALL)
_EMPTY_RE = re.compile(r"""class=["'][^"']*\bel-empty__description\b[^"']*["'][^>]*>"""
                       r"\s*<p[^>]*>\s*No Data\s*</p>", re.IGNORECASE)


class WchAdapter(RetailAdapter):
    key = SITE
    hosts = ("wch-ic.com", "search.wch-ic.com")
    result_kinds = ("document",)
    cad_acquisition = False
    login_required = False
    api = False
    base_url = BASE_URL
    search_url_tmpl = SEARCH_URL_TMPL
    license_note = LICENSE_NOTE
    login_wall_markers = _LOGIN_WALL_MARKERS
    challenge_markers = _CHALLENGE_MARKERS

    def is_no_results(self, content: str) -> bool:
        return bool(_EMPTY_RE.search(content)) or super().is_no_results(content)

    def parse_search(self, content: str) -> list[dict]:
        hits: list[dict] = []
        seen: set[str] = set()
        matches = list(_RESULT_RE.finditer(content))
        for index, match in enumerate(matches):
            url = abs_url(BASE_URL, html.unescape(match.group("href")))
            title = html.unescape(_strip_tags(match.group("title")))
            if not title or not url or url in seen:
                continue
            seen.add(url)
            end = matches[index + 1].start() if index + 1 < len(matches) else len(content)
            description = _DESC_RE.search(content, match.end(), end)
            extra = {"ok": True}
            if description:
                desc = html.unescape(_strip_tags(description.group("desc")))
                if desc:
                    extra["description"] = desc
            hits.append(make_hit(SITE, title=title, url=url, kind="document",
                                 thumbnail=card_image(match.group(), BASE_URL), extra=extra))
        return hits


ADAPTER = register(WchAdapter())
