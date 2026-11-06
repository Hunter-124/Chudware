"""Bosch Rexroth product, category and media search."""
from __future__ import annotations

import re
from urllib.parse import urlsplit

from . import register
from ._retail import card_blocks
from .parker import _IndustrialAdapter, _card_hit, _kind_from_title, _text

SITE = "boschrexroth"
BASE_URL = "https://www.boschrexroth.com"
SEARCH_URL_TMPL = BASE_URL + "/en/us/search?q={query}"
LICENSE_NOTE = ("Bosch Rexroth CAD and datasheets are fetched under the user's own session "
                "for engineering use and are not redistributed. See contracts/license_debt.md.")
_RESULT_RE = re.compile(
    r'<a\b(?=[^>]*\bclass\s*=\s*["\'][^"\']*\bitem-headline\b)'
    r'(?=[^>]*\bhref\s*=\s*["\'](?P<href>[^"\']+)["\'])[^>]*>'
    r'(?P<title>.*?)</a>', re.I | re.S)


class BoschRexrothAdapter(_IndustrialAdapter):
    key = SITE
    base_url = BASE_URL
    search_url_tmpl = SEARCH_URL_TMPL
    manufacturer = "Bosch Rexroth"
    license_note = LICENSE_NOTE
    distributor_only = True
    login_markers = ("Please log in", "Log in to download", "Sign in to your account")
    result_kinds = ("product", "series", "category", "document", "page")

    def parse_search(self, content: str, base: str) -> list[dict]:
        blocks = (card_blocks(content, "div", "search-result-item") or
                  card_blocks(content, "div", "search-result") or
                  card_blocks(content, "article", "search-result") or
                  card_blocks(content, "a", "item-headline"))
        hits = []
        for block in blocks:
            match = _RESULT_RE.search(block)
            if not match:
                continue
            href, title = match.group("href"), match.group("title")
            try:
                path = urlsplit(href).path.lower()
            except ValueError:
                continue
            text = _text(title).lower()
            if path.endswith(".pdf") or re.search(r"\b(?:datasheet|data sheet|brochure|catalogue|catalog)\b", text):
                kind = "document"
            elif any(segment in path for segment in ("/category/", "/categories/", "/products/")):
                kind = "category"
            elif "/product/" in path:
                kind = _kind_from_title(title)
            else:
                kind = "page"
            hit = _card_hit(self.key, block, base, href, title, kind=kind, manufacturer=self.manufacturer)
            if hit:
                hits.append(hit)
        return hits


ADAPTER = register(BoschRexrothAdapter())
