"""services/browser/adapters/futureelectronics.py — Future Electronics source adapter (Parts Library V2).

Future Electronics is a broadline electronics distributor. Full product-list
pages are Akamai-walled in headless browsers, so search uses Future's
same-origin autocomplete endpoint. That endpoint is fast and public, returns
concrete manufacturer part-number suggestions, and provides normal product
link-outs without retrying or bypassing a challenge.

Detail acquisition keeps the default retail datasheet flow and degrades
honestly if the linked page is blocked.
"""
from __future__ import annotations
import html
import json
import re
from urllib.parse import quote

from .. import session as _session
from . import register
from ._retail import RetailAdapter

SITE = "futureelectronics"
BASE_URL = "https://www.futureelectronics.com"
SEARCH_URL_TMPL = BASE_URL + "/search?text={query}"
AUTOCOMPLETE_URL_TMPL = BASE_URL + "/search/autocomplete/SearchBox_GC?term={query}"
LICENSE_NOTE = (
    "Future Electronics product data is reference-only link-out commerce data, "
    "and any datasheet remains the manufacturer's own document, fetched here for "
    "the user's own engineering reference only. This adapter never redistributes "
    "or LLM-generates page content — only a small price/stock/spec record copied "
    "with provenance (source URL + fetch timestamp) and the manufacturer's "
    "datasheet saved for the user. See contracts/license_debt.md."
)

_LOGIN_WALL_MARKERS = ("Sign In", "Log In", 'id="login"')
_CHALLENGE_MARKERS = ("Access Denied", "Reference #", "Pardon Our Interruption")


class FutureElectronicsAdapter(RetailAdapter):
    key = SITE
    base_url = BASE_URL
    search_url_tmpl = SEARCH_URL_TMPL
    license_note = LICENSE_NOTE
    login_wall_markers = _LOGIN_WALL_MARKERS
    challenge_markers = _CHALLENGE_MARKERS

    def search(self, query: str) -> list[dict]:
        url = AUTOCOMPLETE_URL_TMPL.format(query=quote(query))
        result = _session.browser_fetch(url, SITE)
        if not result.get("ok"):
            return [result]
        content = result.get("content", "")
        pre = re.search(r"<pre[^>]*>(?P<body>.*?)</pre>", content,
                        re.IGNORECASE | re.DOTALL)
        raw = html.unescape(pre.group("body") if pre else content).strip()
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            return []
        hits: list[dict] = []
        seen: set[str] = set()
        for suggestion in payload.get("suggestions") or []:
            term = str(suggestion.get("term") or "").strip()
            if not term or term in seen:
                continue
            seen.add(term)
            hits.append({
                "ok": True,
                "title": term,
                "sku": term,
                "url": SEARCH_URL_TMPL.format(query=quote(term)),
                "site": SITE,
            })
        return hits


ADAPTER = register(FutureElectronicsAdapter())
