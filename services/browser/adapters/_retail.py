"""services/browser/adapters/_retail.py — shared parsing helpers + base class for the
retail/commerce adapter wave (Parts Library V2, workstream 2).

The nine retail adapters (homedepot, lowes, acehardware, grainger, fastenal,
harborfreight, tractorsupply, onlinemetals, metalsdepot) are DATA-heavy,
CODE-light: nearly everything that differs between them lives in each module's
site config (URLs, wall markers, license note), while the fetch → wall-check →
parse → envelope control flow lives here once, in :class:`RetailAdapter`.

Same posture as amazon.py: these are COMMERCE sources — link-out plus a small
copied-with-provenance price/spec record, never redistribution of page content.
Bot-challenge pages are reported as captcha or blocked, never solved or bypassed.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from html import unescape
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlparse

from .. import session as _session
from ..challenge import classify_challenge, is_login_page
from . import (abs_url, download_path, fail, is_login_wall, login_required, make_hit,
               provenance_envelope, quote_query, source_failure)

# ---------------------------------------------------------------------------
# generic HTML/JSON parsing helpers (unit-tested directly)
# ---------------------------------------------------------------------------

_JSONLD_RE = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.IGNORECASE | re.DOTALL,
)
_TAG_RE = re.compile(r"<[^>]+>")


def _strip_tags(text: str) -> str:
    return _TAG_RE.sub("", text or "").strip()


class _CardBlocks(HTMLParser):
    def __init__(self, content: str, tag: str, class_name: str):
        super().__init__(convert_charrefs=False)
        self.content, self.tag, self.class_name = content, tag.lower(), class_name
        self.offsets = [0]
        for line in content.splitlines(keepends=True):
            self.offsets.append(self.offsets[-1] + len(line))
        self.start = None
        self.depth = 0
        self.blocks: list[str] = []

    def _offset(self) -> int:
        line, column = self.getpos()
        return self.offsets[line - 1] + column

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != self.tag:
            return
        if self.depth:
            self.depth += 1
        elif self.class_name in (dict(attrs).get("class") or "").split():
            self.start, self.depth = self._offset(), 1

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        pass

    def handle_endtag(self, tag: str) -> None:
        if tag != self.tag or not self.depth:
            return
        self.depth -= 1
        if not self.depth and self.start is not None:
            end = self.content.find(">", self._offset())
            if end >= 0:
                self.blocks.append(self.content[self.start:end + 1])
            self.start = None


def card_blocks(content: str, tag: str, class_name: str) -> list[str]:
    """Return complete, nested card elements bearing the exact class token."""
    parser = _CardBlocks(content or "", tag, class_name)
    parser.feed(content or "")
    return parser.blocks


class _VisibleText(HTMLParser):
    _VOID = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input",
                       "link", "meta", "param", "source", "track", "wbr"})

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.hidden = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._VOID:
            return
        fields = dict(attrs)
        if self.hidden or tag in {"script", "style", "head", "template", "noscript"} or (
                "hidden" in fields or fields.get("aria-hidden") == "true" or
                re.search(r"display\s*:\s*none|visibility\s*:\s*hidden", fields.get("style") or "", re.I)):
            self.hidden += 1

    def handle_endtag(self, tag: str) -> None:
        if self.hidden and tag not in self._VOID:
            self.hidden -= 1

    def handle_data(self, data: str) -> None:
        if not self.hidden:
            self.parts.append(data)


def visible_text(content: str) -> str:
    parser = _VisibleText()
    parser.feed(content or "")
    return " ".join(parser.parts)


def stock_material(title: str) -> str | None:
    """Return only a clearly named common metal in a product title."""
    for pattern, label in ((r"\bstainless\s+steel\b", "Stainless Steel"),
                           (r"\bcast\s+iron\b", "Cast Iron"),
                           (r"\balumini?um\b", "Aluminum"),
                           (r"\bsteel\b", "Steel"), (r"\bbrass\b", "Brass"),
                           (r"\bcopper\b", "Copper"), (r"\bbronze\b", "Bronze")):
        if re.search(pattern, title or "", re.I):
            return label
    return None


class _CardImages(HTMLParser):
    def __init__(self, base_url: str):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.images: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "img":
            return
        fields = {key.lower(): value or "" for key, value in attrs}
        identity = " ".join(fields.get(key, "") for key in ("class", "id", "alt")).lower()
        if re.search(r"(?:^|[\s_-])(?:logo|sprite|placeholder|favicon|icon|manufacturer|pdf|download)(?:[\s_-]|$)", identity):
            return
        for key in ("data-src", "data-lazy-src", "src", "data-srcset", "srcset"):
            if fields.get(key, "").strip().lower().startswith("data:"):
                continue
            for candidate in fields.get(key, "").split(","):
                href = candidate.strip().split(" ", 1)[0]
                if not href or href.lower().startswith("data:"):
                    continue
                if re.search(r"(?:^|[/_.-])(?:logo|sprite|placeholder|favicon|icon|icons|lazy|loading|spinner|missing|pdf|pdfhtml|datasheet)(?:[/_.?-]|$)",
                             href.lower()):
                    continue
                image = abs_url(self.base_url, href)
                if image:
                    self.images.append(image)
                    return


def card_image(card_html: str, base_url: str) -> str | None:
    """First actual product image in a search card, including lazy/srcset images."""
    parser = _CardImages(base_url)
    parser.feed(card_html or "")
    return parser.images[0] if parser.images else None


def _price(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value) if math.isfinite(value) and value >= 0 else None
    text = str(value).strip()
    if not re.fullmatch(r"[$€£]?\s*(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?", text):
        return None
    price = float(text.lstrip("$€£").strip().replace(",", ""))
    return price if math.isfinite(price) else None


def _stock(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    text = str(value).strip()
    return int(text.replace(",", "")) if re.fullmatch(r"(?:\d{1,3}(?:,\d{3})+|\d+)", text) else None


def _is_captcha(url: str, content: str) -> bool:
    explicit = re.search(r"\bcaptcha\b", visible_text(content), re.I)
    return bool(explicit or classify_challenge(content, url) == "captcha")


def _challenge(site: str, op: str, url: str, content: str) -> dict:
    reason = "captcha" if _is_captcha(url, content) else "blocked"
    return fail(site, op, reason, "The site returned a browser challenge.", url=url)


def _standalone_login(content: str) -> bool:
    heading = re.search(r"<(?:title|h1)\b[^>]*>\s*(?:sign\s*in|log\s*in)\b", content or "", re.I)
    return bool(heading and is_login_page(content))


def _iter_jsonld_nodes(data: Any):
    """Depth-first walk over a decoded JSON-LD payload, yielding every dict node.

    Handles top-level arrays, ``@graph`` wrappers, ``ItemList``/``itemListElement``
    (whose entries are usually ``ListItem`` dicts with the Product under ``item``),
    and ``mainEntity`` nesting.
    """
    if isinstance(data, list):
        for entry in data:
            yield from _iter_jsonld_nodes(entry)
    elif isinstance(data, dict):
        yield data
        for key in ("@graph", "itemListElement", "item", "mainEntity"):
            if key in data:
                yield from _iter_jsonld_nodes(data[key])


def parse_jsonld_products(html: str) -> list[dict]:
    """Extract every JSON-LD ``Product`` from ``html`` as a normalized record.

    Returns ``{title, url, price, currency, sku, image, availability, brand}``
    per product (values may be None). Malformed JSON blocks are skipped, never
    raised — a bad analytics blob must not kill the whole page parse.
    """
    products: list[dict] = []
    for m in _JSONLD_RE.finditer(html or ""):
        raw = m.group(1).strip()
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            continue  # malformed block — skip, keep scanning
        for node in _iter_jsonld_nodes(data):
            node_type = node.get("@type")
            types = node_type if isinstance(node_type, list) else [node_type]
            if "Product" not in [str(t) for t in types]:
                continue
            offers = node.get("offers") or {}
            if isinstance(offers, list):
                offers = offers[0] if offers and isinstance(offers[0], dict) else {}
            if not isinstance(offers, dict):
                offers = {}
            price = offers.get("price")
            if (price is None and offers.get("lowPrice") is not None and
                    offers.get("lowPrice") == offers.get("highPrice")):
                price = offers["lowPrice"]
            image = node.get("image")
            if isinstance(image, list):
                image = image[0] if image else None
            if isinstance(image, dict):
                image = image.get("url")
            brand = node.get("brand")
            if isinstance(brand, dict):
                brand = brand.get("name")
            sku = node.get("sku")
            products.append({
                "title": node.get("name"),
                "url": node.get("url") or node.get("@id"),
                "price": str(price) if price is not None else None,
                "currency": offers.get("priceCurrency"),
                "sku": str(sku) if sku is not None else None,
                "mpn": node.get("mpn"),
                "image": image if isinstance(image, str) else None,
                "availability": offers.get("availability"),
                "stock": (offers.get("inventoryLevel") or {}).get("value")
                         if isinstance(offers.get("inventoryLevel"), dict) else offers.get("inventoryLevel"),
                "material": node.get("material") if isinstance(node.get("material"), str) else None,
                "brand": brand if isinstance(brand, str) else None,
            })
    return products


_TH_TD_RE = re.compile(r"<th[^>]*>(.*?)</th>\s*<td[^>]*>(.*?)</td>", re.IGNORECASE | re.DOTALL)
_DT_DD_RE = re.compile(r"<dt[^>]*>(.*?)</dt>\s*<dd[^>]*>(.*?)</dd>", re.IGNORECASE | re.DOTALL)


def _norm_key(key: str) -> str:
    key = _strip_tags(key).strip().lower().rstrip(":").strip()
    key = re.sub(r"[^a-z0-9]+", "_", key).strip("_")
    return key


def parse_spec_table(html: str) -> dict:
    """Generic spec extraction from ``<th>/<td>`` and ``<dt>/<dd>`` pairs.

    Keys are normalized (lowercase, non-alphanumeric runs → ``_``); values are
    tag-stripped text. Later duplicates do not clobber earlier ones.
    """
    spec: dict[str, str] = {}
    for pattern in (_TH_TD_RE, _DT_DD_RE):
        for m in pattern.finditer(html or ""):
            key = _norm_key(m.group(1))
            value = _strip_tags(m.group(2))
            if key and value and key not in spec:
                spec[key] = value
    return spec


# --- unit-aware numeric parsing -------------------------------------------------

_MIXED_NUM_RE = re.compile(r"(\d+)[\s-]+(\d+)/(\d+)")   # 1-1/2, 1 1/2
_FRACTION_RE = re.compile(r"(\d+)/(\d+)")
_DECIMAL_RE = re.compile(r"\d+(?:\.\d+)?")


def _parse_number(text: str) -> float | None:
    """Parse '1-1/2', '1 1/2', '1/4', '0.049', '12,000' → float. None when absent."""
    s = str(text).replace(",", "")
    m = _MIXED_NUM_RE.search(s)
    if m:
        return int(m.group(1)) + int(m.group(2)) / int(m.group(3))
    m = _FRACTION_RE.search(s)
    if m and int(m.group(2)) != 0:
        return int(m.group(1)) / int(m.group(2))
    m = _DECIMAL_RE.search(s)
    if m:
        return float(m.group(0))
    return None


_INCH_UNIT_RE = re.compile(r'"|\bin(?:ch(?:es)?)?\b\.?', re.IGNORECASE)
_MM_UNIT_RE = re.compile(r"\bmm\b", re.IGNORECASE)


def _length_to_mm(value: str) -> float | None:
    """Numeric length in mm. Inch→mm conversion when the value carries a '"' or
    'in'/'inch' unit; explicit 'mm' passes through; a bare number passes through
    unconverted (only-emit-what-you-know posture lives in the caller)."""
    num = _parse_number(value)
    if num is None:
        return None
    s = str(value)
    if _MM_UNIT_RE.search(s):
        return round(num, 4)
    if _INCH_UNIT_RE.search(s):
        return round(num * 25.4, 4)
    return round(num, 4)


# Canonical machining-param key ← vendor spec-table keys (already _norm_key'ed).
_MACHINING_KEY_MAP: dict[str, tuple[str, ...]] = {
    "tool_diameter_mm": ("milling_dia", "milling_diameter", "cutting_diameter",
                         "cutting_dia", "cutting_edge_diameter", "tool_diameter",
                         "diameter", "dia"),
    "shank_diameter_mm": ("shank_diameter", "shank_dia", "shank_diameter_dia"),
    "loc_mm": ("length_of_cut", "flute_length", "loc", "cutting_length",
               "length_of_cut_loc"),
    "flutes": ("number_of_flutes", "flutes", "flute_count", "no_of_flutes"),
    "coating": ("coating", "tool_coating", "finish_coating"),
    "material": ("tool_material", "cutting_tool_material", "body_material"),
    "max_rpm": ("max_rpm", "maximum_rpm", "max_speed", "maximum_speed",
                "maximum_rotational_speed"),
    "feed_per_tooth_mm": ("feed_per_tooth", "chip_load", "chip_load_per_tooth"),
}

_LENGTH_PARAMS = {"tool_diameter_mm", "shank_diameter_mm", "loc_mm", "feed_per_tooth_mm"}
_INT_PARAMS = {"flutes", "max_rpm"}


def normalize_machining_params(spec: dict) -> dict:
    """Map vendor spec keys to canonical machining params. Only emits keys whose
    source was actually found (and parsed) — never invents values."""
    params: dict[str, Any] = {}
    for canonical, aliases in _MACHINING_KEY_MAP.items():
        for alias in aliases:
            if alias not in spec:
                continue
            raw = spec[alias]
            if canonical in _LENGTH_PARAMS:
                value: Any = _length_to_mm(raw)
            elif canonical in _INT_PARAMS:
                num = _parse_number(raw)
                value = int(num) if num is not None else None
            else:  # free-text params (coating, material)
                value = _strip_tags(str(raw)).strip() or None
            if value is not None:
                params[canonical] = value
                break
    return params


# --- stock-metal params (onlinemetals / metalsdepot) ------------------------------

_ALLOY_RE = re.compile(
    r"\b("
    r"(?:6061|6063|7075|5052|5083|3003|2024)(?:-[TOH]\d+\w*)?"   # aluminum + temper
    r"|30[346]L?|316L?|410|416|17-4"                              # stainless
    r"|A36|1018|1045|1144|12L14|4130|4140|8620|O1|A2|D2"          # steels
    r"|C[0-9]{3}|260|360|464"                                     # brass/copper
    r"|Ti-6Al-4V|CP2"                                             # titanium
    r")\b",
    re.IGNORECASE,
)

_PROFILES = ("round tube", "square tube", "rectangular tube", "round bar",
             "square bar", "flat bar", "hex bar", "angle", "channel", "sheet",
             "plate", "pipe", "beam", "rod")

_OD_RE = re.compile(r'([\d][\d.\s/,-]*?)\s*(?:"|in(?:ch)?\.?|mm)\s*(?:od|outer\s*diameter)\b',
                    re.IGNORECASE)
_WALL_RE = re.compile(r'([\d][\d.\s/,-]*?)\s*(?:"|in(?:ch)?\.?|mm)\s*wall\b', re.IGNORECASE)
_THICK_RE = re.compile(r'([\d][\d.\s/,-]*?)\s*(?:"|in(?:ch)?\.?|mm)\s*(?:thick(?:ness)?|thk)\b',
                       re.IGNORECASE)
_MM_HINT_RE = re.compile(r"mm\s*(?:od|wall|thick)", re.IGNORECASE)


def parse_stock_params(title: str, spec: dict | None = None) -> dict:
    """Parse alloy/profile/dimensions from a metals-vendor title (+ optional spec
    table), e.g. ``6061-T6 Aluminum Round Tube 1" OD x 0.049" wall`` →
    ``{alloy: "6061-T6", profile: "round_tube", od_mm: 25.4, wall_mm: 1.2446}``.
    Only emits keys actually derivable."""
    params: dict[str, Any] = {}
    text = title or ""
    if spec:
        text = text + " | " + " | ".join(f"{k} {v}" for k, v in spec.items())

    m = _ALLOY_RE.search(text)
    if m:
        params["alloy"] = m.group(1).upper()

    lowered = text.lower()
    for profile in _PROFILES:
        if profile in lowered:
            params["profile"] = profile.replace(" ", "_")
            break

    for key, pattern in (("od_mm", _OD_RE), ("wall_mm", _WALL_RE), ("thickness_mm", _THICK_RE)):
        dm = pattern.search(text)
        if not dm:
            continue
        num = _parse_number(dm.group(1))
        if num is None:
            continue
        # The unit token sits between the number and the keyword; mm passes through.
        segment = dm.group(0)
        if _MM_HINT_RE.search(segment):
            params[key] = round(num, 4)
        else:
            params[key] = round(num * 25.4, 4)
    return params


# --- datasheet discovery ----------------------------------------------------------

_ANCHOR_RE = re.compile(r'<a\b[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
                        re.IGNORECASE | re.DOTALL)
_DOC_WORD_RE = re.compile(r"spec|data\s*sheet|datasheet|manual|sds", re.IGNORECASE)


def find_datasheet_url(html: str, base_url: str) -> str | None:
    """First absolute ``.pdf`` href whose link text (or the href itself) matches
    spec|datasheet|manual|sds (case-insensitive). None when nothing qualifies."""
    for m in _ANCHOR_RE.finditer(html or ""):
        href, text = unescape(m.group(1)), m.group(2)
        if not href.split("?", 1)[0].split("#", 1)[0].lower().endswith(".pdf"):
            continue
        if _DOC_WORD_RE.search(_strip_tags(text)) or _DOC_WORD_RE.search(href):
            result = abs_url(base_url, href)
            if result:
                return result
    return None


# ---------------------------------------------------------------------------
# shared adapter skeleton
# ---------------------------------------------------------------------------

class RetailAdapter:
    """Base class for the retail commerce adapters.

    Subclasses supply site config as class attributes and (optionally) override
    :meth:`parse_search` / :meth:`extract_params`. The control flow — fetch,
    challenge/login-wall degradation (NEVER solved or bypassed), JSON-LD + spec
    parse, datasheet download, provenance envelope — is identical everywhere.
    """

    key: str = ""
    base_url: str = ""
    search_url_tmpl: str = ""
    license_note: str = ""
    login_wall_markers: tuple[str, ...] = ()
    challenge_markers: tuple[str, ...] = ()
    no_results_markers: tuple[str, ...] = ()
    currency: str | None = None

    # --- search -------------------------------------------------------------

    def search_url(self, query: str) -> str:
        return self.search_url_tmpl.format(query=quote_query(query))

    def search(self, query: str) -> list[dict]:
        url = self.search_url(query)
        result = _session.browser_fetch(url, self.key)
        if not result.get("ok"):
            return [source_failure(self.key, "search", result, url=url)]
        content = result.get("content", "")
        page_url = result.get("final_url") or url
        if is_login_wall(content, self.challenge_markers) or _is_captcha(page_url, content):
            return [_challenge(self.key, "search", page_url, content)]
        if (self._redirected_to_login(url, page_url) or _standalone_login(content) or
                is_login_wall(content, self.login_wall_markers)):
            return [source_failure(self.key, "search", login_required(self.key, page_url), url=page_url)]
        hits = self.parse_search_page(content, page_url)
        if not hits:
            if self.is_no_results(content):
                return []
            return [fail(self.key, "search", "parse_failed", "No recognizable results on the page.", url=page_url)]
        return [self._normalize_hit(hit, page_url) if hit.get("ok") is not False else
                {**hit, "site": self.key} for hit in hits]

    @staticmethod
    def _redirected_to_login(url: str, final_url: str) -> bool:
        pattern = r"/(?:login|log-in|signin|sign-in)(?:/|$)"
        return bool(re.search(pattern, urlparse(final_url).path, re.I) and
                    not re.search(pattern, urlparse(url).path, re.I))

    # Search parsers can override these hooks for source-specific page shapes.

    def is_no_results(self, content: str) -> bool:
        text = visible_text(content)
        return bool(re.search(r"\b(?:no results|no products found|0 results)\b", text, re.I) or
                    any(marker.lower() in text.lower() for marker in self.no_results_markers))

    def parse_search_page(self, content: str, final_url: str) -> list[dict]:
        return self.parse_search(content)

    def parse_search(self, content: str) -> list[dict]:
        """Default strategy: JSON-LD Product records (the stable, sanctioned
        machine-readable layer most retail sites ship for SEO)."""
        return [self._hit_from_product(p) for p in parse_jsonld_products(content)
                if p.get("title") and p.get("url")]

    def _hit_from_product(self, product: dict) -> dict:
        return {**product, "ok": True, "thumbnail": product.get("image")}

    def _normalize_hit(self, hit: dict, page_url: str | None = None) -> dict:
        base = page_url or self.base_url
        url = abs_url(base, str(hit.get("url") or "")) or ""
        model = hit.get("model") if isinstance(hit.get("model"), dict) else {}
        kind = hit.get("kind") or "product"
        if kind == "post":
            kind = "page"
        fields = {"site", "title", "url", "kind", "sku", "mpn", "manufacturer", "brand",
                  "price", "currency", "stock", "material", "thumbnail", "image", "model",
                  "model_formats", "model_available", "license"}
        return make_hit(
            self.key, title=_strip_tags(str(hit.get("title") or "")), url=url, kind=kind,
            sku=hit.get("sku"), mpn=hit.get("mpn"), manufacturer=hit.get("manufacturer") or hit.get("brand"),
            price=_price(hit.get("price")), currency=hit.get("currency") or self.currency,
            stock=_stock(hit.get("stock")), material=hit.get("material"),
            thumbnail=abs_url(base, hit.get("thumbnail") or hit.get("image")),
            model_formats=hit.get("model_formats") or model.get("formats"),
            model_available=hit.get("model_available", model.get("available")),
            license=hit.get("license"), extra={key: value for key, value in hit.items() if key not in fields})

    # --- acquire ------------------------------------------------------------

    def extract_params(self, result: dict, spec: dict, content: str) -> dict:
        """Canonical engineering params for this hit; default = machining params
        (only emits what the spec table actually names). Metals vendors override
        with :func:`parse_stock_params`."""
        return normalize_machining_params(spec)

    def acquire(self, result: dict) -> dict:
        url = result.get("url", "")
        if not url:
            return fail(self.key, "acquire", "not_found", "result has no 'url'")

        page = _session.browser_fetch(url, self.key)
        if not page.get("ok"):
            return source_failure(self.key, "acquire", page, url=url)
        content = page.get("content", "")
        if is_login_wall(content, self.challenge_markers) or _is_captcha(page.get("final_url") or url, content):
            return _challenge(self.key, "acquire", url, content)
        if (self._redirected_to_login(url, page.get("final_url") or url) or
                _standalone_login(content) or is_login_wall(content, self.login_wall_markers)):
            return source_failure(self.key, "acquire", login_required(self.key, url), url=url)

        spec = parse_spec_table(content)
        products = parse_jsonld_products(content)
        sku = str(result.get("sku") or "")
        mpn = str(result.get("mpn") or "")
        identifier, field = (sku, "sku") if sku else (mpn, "mpn")
        listed = sorted({str(p[field]) for p in products if p.get(field)})
        if identifier and listed and identifier not in listed:
            identity = hashlib.sha256(f"{self.key}:{url}:{field}:{identifier}".encode("utf-8")).hexdigest()[:16]
            question = {"id": f"browser_product_mismatch.{identity}", "kind": "text",
                        "prompt": f"Which {self.key} {field.upper()} should be acquired?",
                        "suggested": identifier, "required": True,
                        "why": f"The requested {field.upper()} is {identifier}, but the page lists {', '.join(listed)}."}
            return {**fail(self.key, "acquire", "unsupported", question["why"],
                           url=url, ask_user=question), "candidates": listed}
        product = next((p for p in products if (sku and str(p.get("sku") or "") == sku) or
                        (mpn and str(p.get("mpn") or "") == mpn)), None)
        if product is None:
            final_url = page.get("final_url") or url
            product = next((p for p in products
                            if (not sku or not p.get("sku")) and (not mpn or not p.get("mpn")) and
                            p.get("url") and abs_url(final_url, p["url"]) in {url, final_url}), None)
        if product is None and len(products) == 1 and not sku and not mpn and not products[0].get("url"):
            product = products[0]
        product = product or {}
        params = self.extract_params(result, spec, content) or {}

        datasheet_url = find_datasheet_url(content, page.get("final_url") or url)
        if not datasheet_url:
            return fail(self.key, "acquire", "not_found", "No datasheet link found.", url=url)
        dest, refusal = download_path(self.key, datasheet_url, default="document.pdf")
        if refusal is not None:
            return source_failure(self.key, "acquire", refusal, url=datasheet_url)
        outcome = _session.browser_download(datasheet_url, self.key, dest)
        if not outcome.get("ok"):
            return source_failure(self.key, "acquire", outcome, url=datasheet_url)
        datasheet_path = outcome.get("dest")
        if not datasheet_path:
            return fail(self.key, "acquire", "parse_failed", "Download returned no file.", url=datasheet_url)
        from services.browser.cad_validate import classify_artifact
        checked = classify_artifact(datasheet_path, "document")
        if not checked["ok"]:
            return fail(self.key, "acquire", "parse_failed", checked["reason"], url=datasheet_url)

        return provenance_envelope(
            source_url=url,
            session_profile_used=page.get("session_profile_used", self.key),
            license_note=self.license_note,
            artifact_path=datasheet_path,
            artifact_kind="document",
            format="pdf",
            title=result.get("title") or product.get("title"),
            sku=product.get("sku") or result.get("sku"),
            price=product.get("price") or result.get("price"),
            currency=product.get("currency") or result.get("currency"),
            availability=product.get("availability"),
            brand=product.get("brand"),
            thumbnail=abs_url(page.get("final_url") or url, product.get("image") or result.get("thumbnail")),
            spec=spec,
            params=params,
            datasheet_url=datasheet_url,
            datasheet_path=datasheet_path,
        )
