"""Future Electronics search through its same-origin autocomplete endpoint.

Autocomplete terms are links to a search page, not purchasable product hits.
When the endpoint actually provides product records with product URLs, those are
reported separately. The adapter never retries or bypasses a vendor challenge.
"""
from __future__ import annotations
import html
import json
import re
from urllib.parse import urlsplit

from .. import challenge, session as _session
from . import (abs_url, fail, is_login_wall, login_required, make_hit, quote_query,
               register, source_failure)
from ._retail import RetailAdapter, _challenge, _price, _stock, card_image

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

_LOGIN_WALL_MARKERS = ('id="login"', 'id="login-email"')
_CHALLENGE_MARKERS = ("Access Denied", "Reference #", "Pardon Our Interruption", "Just a moment")


class FutureElectronicsAdapter(RetailAdapter):
    key = SITE
    hosts = ("futureelectronics.com",)
    result_kinds = ("product", "page")
    cad_acquisition = False
    login_required = False
    api = True
    base_url = BASE_URL
    search_url_tmpl = SEARCH_URL_TMPL
    license_note = LICENSE_NOTE
    login_wall_markers = _LOGIN_WALL_MARKERS
    challenge_markers = _CHALLENGE_MARKERS

    def search(self, query: str) -> list[dict]:
        url = AUTOCOMPLETE_URL_TMPL.format(query=quote_query(query))
        response = _session.browser_fetch(url, SITE)
        if not response.get("ok"):
            return [source_failure(SITE, "search", response, url=url)]
        content = response.get("content", "")
        page_url = response.get("final_url") or url
        if challenge.classify_challenge(content, page_url) == "captcha" or is_login_wall(content, _CHALLENGE_MARKERS):
            return [_challenge(SITE, "search", page_url, content)]
        if self._redirected_to_login(url, page_url) or is_login_wall(content, _LOGIN_WALL_MARKERS):
            return [source_failure(SITE, "search", login_required(SITE, page_url), url=page_url)]
        pre = re.search(r"<pre[^>]*>(?P<body>.*?)</pre>", content, re.IGNORECASE | re.DOTALL)
        raw = html.unescape(pre.group("body")) if pre else content
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            return [fail(SITE, "search", "parse_failed", "Autocomplete did not return valid JSON.", url=url)]
        if not isinstance(payload, dict) or not ({"suggestions", "products"} & payload.keys()):
            return [fail(SITE, "search", "parse_failed", "Autocomplete response has no result lists.", url=url)]
        suggestions, products = payload.get("suggestions"), payload.get("products")
        if any(value is not None and not isinstance(value, list) for value in (suggestions, products)):
            return [fail(SITE, "search", "parse_failed", "Autocomplete result lists were malformed.", url=url)]

        hits: list[dict] = []
        seen: set[str] = set()
        for product in products or []:
            if not isinstance(product, dict):
                continue
            title = str(product.get("name") or product.get("title") or "").strip()
            product_url = abs_url(BASE_URL, product.get("url"))
            if not title or not product_url or urlsplit(product_url).path.rstrip("/") == "/search":
                continue
            if product_url in seen:
                continue
            seen.add(product_url)
            price_info = product.get("price")
            price_value = price_info.get("value") if isinstance(price_info, dict) else price_info
            currency = price_info.get("currencyIso") if isinstance(price_info, dict) else product.get("currency")
            stock_info = product.get("stock")
            stock_value = stock_info.get("stockLevel") if isinstance(stock_info, dict) else stock_info
            images = product.get("images")
            images = [product.get("image")] + (images if isinstance(images, list) else [])
            thumbnail = None
            for image in images:
                if isinstance(image, dict):
                    image = image.get("url")
                if isinstance(image, str):
                    thumbnail = card_image(f'<img src="{html.escape(image, quote=True)}">', product_url)
                    if thumbnail:
                        break
            hits.append(make_hit(
                SITE, title=title, url=product_url, kind="product", sku=product.get("code"),
                mpn=product.get("manufacturerPartNumber") or product.get("mpn"),
                manufacturer=product.get("manufacturer"), price=_price(price_value), currency=currency,
                stock=_stock(stock_value), thumbnail=thumbnail,
            ))
        for suggestion in suggestions or []:
            if not isinstance(suggestion, dict):
                continue
            term = str(suggestion.get("term") or "").strip()
            if not term:
                continue
            search_url = SEARCH_URL_TMPL.format(query=quote_query(term))
            if search_url in seen:
                continue
            seen.add(search_url)
            hits.append(make_hit(SITE, title=term, url=search_url, kind="page",
                                 mpn=term if suggestion.get("searchSpace") == "upshiftedMPN" else None))
        if not hits and (suggestions or products):
            return [fail(SITE, "search", "parse_failed", "Autocomplete returned unrecognized entries.", url=url)]
        return hits

    def acquire(self, result: dict) -> dict:
        url = result.get("url", "")
        if result.get("kind") == "page" or (url and urlsplit(url).path.rstrip("/") == "/search"):
            return fail(SITE, "acquire", "unsupported",
                        "An autocomplete suggestion is not a specific product.", url=url,
                        ask_user="Select a specific product before requesting a datasheet.")
        return super().acquire(result)


ADAPTER = register(FutureElectronicsAdapter())
