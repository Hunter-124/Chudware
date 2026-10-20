"""services/browser/adapters/_retail.py — shared parsing helpers + base class for the
retail/commerce adapter wave (Parts Library V2, workstream 2).

The nine retail adapters (homedepot, lowes, acehardware, grainger, fastenal,
harborfreight, tractorsupply, onlinemetals, metalsdepot) are DATA-heavy,
CODE-light: nearly everything that differs between them lives in each module's
site config (URLs, wall markers, license note), while the fetch → wall-check →
parse → envelope control flow lives here once, in :class:`RetailAdapter`.

Same posture as amazon.py: these are COMMERCE sources — link-out plus a small
copied-with-provenance price/spec record, never redistribution of page content.
Bot-challenge pages (Akamai "Access Denied", PerimeterX, captchas) are treated
exactly like login walls — degrade to ``login_required``, never solve/bypass.
"""
from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import quote, urljoin

from .. import session as _session
from . import download_path, is_login_wall, login_required, provenance_envelope

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
            if price is None:
                price = offers.get("lowPrice")
            image = node.get("image")
            if isinstance(image, list):
                image = image[0] if image else None
            if isinstance(image, dict):
                image = image.get("url")
            brand = node.get("brand")
            if isinstance(brand, dict):
                brand = brand.get("name")
            sku = node.get("sku") or node.get("mpn")
            model = node.get("model")
            if not sku and isinstance(model, str):
                sku = model
            products.append({
                "title": node.get("name"),
                "url": node.get("url") or node.get("@id"),
                "price": str(price) if price is not None else None,
                "currency": offers.get("priceCurrency"),
                "sku": str(sku) if sku is not None else None,
                "image": image if isinstance(image, str) else None,
                "availability": offers.get("availability"),
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
        href, text = m.group(1), m.group(2)
        if not href.split("?", 1)[0].lower().endswith(".pdf"):
            continue
        if _DOC_WORD_RE.search(_strip_tags(text)) or _DOC_WORD_RE.search(href):
            return urljoin(base_url, href)
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
    currency: str = "USD"

    # --- search -------------------------------------------------------------

    def search_url(self, query: str) -> str:
        return self.search_url_tmpl.format(query=quote(query))

    def search(self, query: str) -> list[dict]:
        url = self.search_url(query)
        result = _session.browser_fetch(url, self.key)
        if not result.get("ok"):
            return [result]
        content = result.get("content", "")
        # Bot-challenge pages degrade EXACTLY like login walls — never bypassed.
        if is_login_wall(content, self.challenge_markers):
            return [login_required(self.key, url)]
        if is_login_wall(content, self.login_wall_markers):
            return [login_required(self.key, url)]
        return self.parse_search(content)

    def parse_search(self, content: str) -> list[dict]:
        """Default strategy: JSON-LD Product records (the stable, sanctioned
        machine-readable layer most retail sites ship for SEO)."""
        return [self._hit_from_product(p) for p in parse_jsonld_products(content)
                if p.get("title") and p.get("url")]

    def _hit_from_product(self, product: dict) -> dict:
        hit: dict[str, Any] = {
            "ok": True,
            "title": _strip_tags(str(product["title"])),
            "url": urljoin(self.base_url, product["url"]),
            "site": self.key,
        }
        if product.get("sku"):
            hit["sku"] = product["sku"]
        if product.get("price"):
            hit["price"] = product["price"]
        if product.get("image"):
            hit["thumbnail"] = product["image"]
        return hit

    # --- acquire ------------------------------------------------------------

    def extract_params(self, result: dict, spec: dict, content: str) -> dict:
        """Canonical engineering params for this hit; default = machining params
        (only emits what the spec table actually names). Metals vendors override
        with :func:`parse_stock_params`."""
        return normalize_machining_params(spec)

    def acquire(self, result: dict) -> dict:
        url = result.get("url", "")
        if not url:
            return {"ok": False, "reason": "bad_result", "hint": "result has no 'url'"}

        page = _session.browser_fetch(url, self.key)
        if not page.get("ok"):
            return page
        content = page.get("content", "")
        if is_login_wall(content, self.challenge_markers):
            return login_required(self.key, url)
        if is_login_wall(content, self.login_wall_markers):
            return login_required(self.key, url)

        spec = parse_spec_table(content)
        products = parse_jsonld_products(content)
        product = products[0] if products else {}
        params = self.extract_params(result, spec, content) or {}

        datasheet_url = find_datasheet_url(content, self.base_url)
        datasheet_path: str | None = None
        if datasheet_url:
            dest, refusal = download_path(self.key, datasheet_url, default="document.pdf")
            if refusal is not None:
                return refusal
            outcome = _session.browser_download(datasheet_url, self.key, dest)
            # A failed datasheet download must not fail the structured record.
            if outcome.get("ok"):
                datasheet_path = outcome["dest"]

        return provenance_envelope(
            source_url=url,
            session_profile_used=page.get("session_profile_used", self.key),
            license_note=self.license_note,
            artifact_path=datasheet_path,
            title=result.get("title") or product.get("title"),
            sku=product.get("sku") or result.get("sku"),
            price=product.get("price") or result.get("price"),
            currency=product.get("currency") or self.currency,
            availability=product.get("availability"),
            brand=product.get("brand"),
            thumbnail=product.get("image") or result.get("thumbnail"),
            spec=spec,
            params=params,
            datasheet_url=datasheet_url,
            datasheet_path=datasheet_path,
        )
