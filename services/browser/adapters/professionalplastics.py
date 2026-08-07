"""services/browser/adapters/professionalplastics.py — Professional Plastics source adapter (Parts Library V2).

Plastics vendor (sheet/rod/tube) with public datasheet + SDS PDFs: ``acquire``
uses the default machining/spec-table param extraction plus datasheet
discovery, and copies only a small price/stock-spec record with provenance.
Parse strategy: live CGI search result links. Challenge pages degrade to
``login_required`` — never solved.
"""
from __future__ import annotations
import re
from urllib.parse import urljoin

from . import register
from ._retail import RetailAdapter, _strip_tags

SITE = "professionalplastics"
BASE_URL = "https://www.professionalplastics.com"
SEARCH_URL_TMPL = (BASE_URL + "/cgi-bin/pp.pl?pgm=co_search&func=process"
                   "&strfnbr=3&terms={query}&type=1")
LICENSE_NOTE = (
    "Professional Plastics product data: reference-only, link-out commerce data. "
    "This adapter never stores or redistributes Professional Plastics' page "
    "content — only a small price/stock-spec record copied with provenance "
    "(source URL + fetch timestamp) for the user's own sourcing decision, never "
    "LLM-generated. See contracts/license_debt.md."
)

_LOGIN_WALL_MARKERS = ("Sign In", 'id="login-email"')
_CHALLENGE_MARKERS = ("Access Denied", "Reference #", "Pardon Our Interruption")

_RESULT_RE = re.compile(
    r'<a\b(?=[^>]*class="[^"]*standLink[^"]*")'
    r'(?=[^>]*href="(?P<href>[^"]+)")[^>]*>(?P<title>[^<][^<]*?)</a>',
    re.IGNORECASE | re.DOTALL,
)


class ProfessionalPlasticsAdapter(RetailAdapter):
    key = SITE
    base_url = BASE_URL
    search_url_tmpl = SEARCH_URL_TMPL
    license_note = LICENSE_NOTE
    login_wall_markers = _LOGIN_WALL_MARKERS
    challenge_markers = _CHALLENGE_MARKERS

    def parse_search(self, content: str) -> list[dict]:
        hits: list[dict] = []
        seen: set[str] = set()
        for match in _RESULT_RE.finditer(content or ""):
            url = urljoin(BASE_URL, match.group("href").split("?", 1)[0])
            title = _strip_tags(match.group("title"))
            if not title or url in seen:
                continue
            seen.add(url)
            hits.append({"ok": True, "title": title, "url": url, "site": SITE})
        return hits


ADAPTER = register(ProfessionalPlasticsAdapter())
