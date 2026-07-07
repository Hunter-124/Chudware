"""services/browser/adapters/lowes.py — Lowe's source adapter (Parts Library V2).

Commerce-only (see amazon.py). Lowe's runs Akamai + PerimeterX bot protection:
"Access Denied" / "Reference #" / PX captcha markers degrade to
``login_required`` — never solved or bypassed.

Parse strategy: JSON-LD Products first; when the SEO layer is absent the
search page usually embeds a ``window.__PRELOADED_STATE__`` JSON blob with the
product list, so we fall back to walking that for title/url/sku/price nodes.
"""
from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urljoin

from . import register
from ._retail import RetailAdapter, parse_jsonld_products

SITE = "lowes"
BASE_URL = "https://www.lowes.com"
SEARCH_URL_TMPL = BASE_URL + "/search?searchTerm={query}"
LICENSE_NOTE = (
    "Lowe's product data: reference-only, link-out commerce data. This adapter "
    "never stores or redistributes Lowe's page content — only a small "
    "price/spec record copied with provenance (source URL + fetch timestamp) "
    "for the user's own sourcing decision, never LLM-generated. See "
    "contracts/license_debt.md."
)

_LOGIN_WALL_MARKERS = ("Sign In to Your Account", 'id="email-input"')
# Akamai / PerimeterX walls — never bypass, treat exactly like a login wall.
_CHALLENGE_MARKERS = ("Access Denied", "Reference #", "px-captcha",
                      "Please verify you are a human")

_PRELOADED_RE = re.compile(
    r"window\.__PRELOADED_STATE__\s*=\s*(\{.*?\})\s*;?\s*</script>", re.DOTALL)

_TITLE_KEYS = ("title", "productTitle", "pdpTitle")
_URL_KEYS = ("pdURL", "productLink", "pdpURL", "url")


class LowesAdapter(RetailAdapter):
    key = SITE
    base_url = BASE_URL
    search_url_tmpl = SEARCH_URL_TMPL
    license_note = LICENSE_NOTE
    login_wall_markers = _LOGIN_WALL_MARKERS
    challenge_markers = _CHALLENGE_MARKERS

    def parse_search(self, content: str) -> list[dict]:
        hits = [self._hit_from_product(p) for p in parse_jsonld_products(content)
                if p.get("title") and p.get("url")]
        if hits:
            return hits
        return self._hits_from_preloaded_state(content)

    def _hits_from_preloaded_state(self, content: str) -> list[dict]:
        m = _PRELOADED_RE.search(content or "")
        if not m:
            return []
        try:
            state = json.loads(m.group(1))
        except (ValueError, TypeError):
            return []
        hits: list[dict] = []

        def walk(node: Any) -> None:
            if isinstance(node, dict):
                title = next((node[k] for k in _TITLE_KEYS
                              if isinstance(node.get(k), str) and node[k].strip()), None)
                url = next((node[k] for k in _URL_KEYS
                            if isinstance(node.get(k), str) and node[k].strip()), None)
                if title and url:
                    hit: dict[str, Any] = {"ok": True, "title": title.strip(),
                                           "url": urljoin(BASE_URL, url), "site": SITE}
                    sku = node.get("itemNumber") or node.get("sku") or node.get("modelId")
                    if sku is not None:
                        hit["sku"] = str(sku)
                    price = node.get("price") or node.get("sellingPrice")
                    if isinstance(price, (int, float, str)):
                        hit["price"] = str(price)
                    image = node.get("imageUrl") or node.get("image")
                    if isinstance(image, str):
                        hit["thumbnail"] = image
                    hits.append(hit)
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for value in node:
                    walk(value)

        walk(state)
        return hits


ADAPTER = register(LowesAdapter())
