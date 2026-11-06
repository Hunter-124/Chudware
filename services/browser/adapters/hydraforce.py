"""HydraForce product-family, document and page search."""
from __future__ import annotations

import html
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from . import register
from ._retail import card_blocks
from .parker import _IndustrialAdapter, _card_hit, _kind_from_title, _text

SITE = "hydraforce"
BASE_URL = "https://www.hydraforce.com"
DISTRIBUTORS_URL = BASE_URL + "/distributors/"
SEARCH_URL_TMPL = BASE_URL + "/search/?q={query}"
LICENSE_NOTE = ("HydraForce CAD and datasheets are fetched under the user's own session "
                "for engineering use and are not redistributed. See contracts/license_debt.md.")

_RESULT_RE = re.compile(
    r'<h4\b(?=[^>]*\bclass\s*=\s*["\'][^"\']*\bresult-title\b)[^>]*>'
    r'(?P<title>.*?)</h4>.*?<div\b(?=[^>]*\bclass\s*=\s*["\'][^"\']*\bresult-links\b)[^>]*>'
    r'.*?<a\b[^>]*\bhref\s*=\s*["\'](?P<href>[^"\']+)["\']', re.I | re.S)
_BADGE_RE = re.compile(
    r'<[^>]+\bclass\s*=\s*["\'][^"\']*\bresult-badge\b[^"\']*["\'][^>]*>(.*?)</', re.I | re.S)


def _without_tracking(href: str) -> str:
    parts = urlsplit(html.unescape(href))
    if not parts.query:
        return html.unescape(href)
    query = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True)
             if not (key.lower().startswith(("_t_", "utm_")) or
                     (key.lower() == "src" and value.lower() == "search"))]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


class HydraForceAdapter(_IndustrialAdapter):
    key = SITE
    base_url = BASE_URL
    search_url_tmpl = SEARCH_URL_TMPL
    manufacturer = "HydraForce"
    license_note = LICENSE_NOTE
    distributor_url = DISTRIBUTORS_URL
    login_markers = ("Please log in", "Log in to download", "Sign in to your account")
    result_kinds = ("series", "category", "document", "page")

    def parse_search(self, content: str, base: str) -> list[dict]:
        blocks = card_blocks(content, "div", "search-page-result")
        if not blocks:
            # Older rendered fragments contain the title and links without the outer card.
            blocks = [match.group(0) for match in _RESULT_RE.finditer(content)]
        hits = []
        for block in blocks:
            match = _RESULT_RE.search(block)
            if not match:
                continue
            try:
                href = _without_tracking(match.group("href"))
                path = urlsplit(href).path.lower()
            except ValueError:
                continue
            badge = _BADGE_RE.search(block)
            label = _text(badge.group(1)).lower() if badge else ""
            if "document" in label or "datasheet" in label or path.endswith(".pdf"):
                kind = "document"
            elif re.search(r"\b(?:valves|pumps|motors|manifolds|cartridges)\s*$",
                           _text(match.group("title")), re.I):
                kind = "category"
            elif "product" in label or "/products/" in path:
                # These are base-model pages with an ordering-code configurator,
                # not a selected orderable valve configuration.
                kind = "series"
            else:
                kind = _kind_from_title(match.group("title"), "page")
            hit = _card_hit(self.key, block, base, href, match.group("title"),
                            kind=kind, manufacturer=self.manufacturer)
            # The live search often supplies circuit symbols, not product images.
            # Do not represent a hydraulic schematic as a product photo.
            if hit and hit["thumbnail"] and re.search(
                    r"(?:^|[/_-])(?:sym|symbol)(?:[_.-]|\d)",
                    urlsplit(hit["thumbnail"]).path, re.I):
                hit["thumbnail"] = None
            if hit:
                hits.append(hit)
        return hits


ADAPTER = register(HydraForceAdapter())
