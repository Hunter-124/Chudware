"""Thomson linear-motion product and series search."""
from __future__ import annotations

import re

from . import register
from ._retail import card_blocks
from .parker import _IndustrialAdapter, _card_hit, _kind_from_title

SITE = "thomson"
BASE_URL = "https://www.thomsonlinear.com"
SEARCH_URL_TMPL = BASE_URL + "/en/search?q={query}"
LICENSE_NOTE = ("Thomson CAD models and datasheets are fetched under the user's own session "
                "for engineering use and are not redistributed. See contracts/license_debt.md.")
_RESULT_RE = re.compile(
    r'<a\b(?=[^>]*\bclass\s*=\s*["\'][^"\']*\bproduct-box__title\b)'
    r'(?=[^>]*\bhref\s*=\s*["\'](?P<href>(?:https?://[^/"\']+)?/en/product/[^"\']+)["\'])[^>]*>'
    r'(?P<title>.*?)</a>', re.I | re.S)


class ThomsonAdapter(_IndustrialAdapter):
    key = SITE
    base_url = BASE_URL
    search_url_tmpl = SEARCH_URL_TMPL
    manufacturer = "Thomson"
    license_note = LICENSE_NOTE
    login_markers = ("Log in to Thomson", "Please sign in to download")
    result_kinds = ("product", "series")

    def parse_search(self, content: str, base: str) -> list[dict]:
        blocks = (card_blocks(content, "div", "product-box") or
                  card_blocks(content, "article", "product-box") or
                  card_blocks(content, "a", "product-box__title"))
        hits = []
        for block in blocks:
            match = _RESULT_RE.search(block)
            if match:
                hit = _card_hit(self.key, block, base, match.group("href"), match.group("title"),
                                kind=_kind_from_title(match.group("title")), manufacturer=self.manufacturer)
                if hit:
                    hits.append(hit)
        return hits


ADAPTER = register(ThomsonAdapter())
