"""services/parts/product_media.py (re-homed from engine_oracle/parts_hub/) — product-media harvest (sourcing §2b).

When the user accepts the ``reverse_engineer`` fallback from ``match_model``, the
RE suite should open already loaded with everything fetchable about the product.
:func:`harvest_product_media` is that fetch step: it fetches the product page
under the user's own persistent browser session (never importing Playwright
directly — always via :mod:`services.browser.session`) and collects EVERY
candidate image (gallery ``<img src>``/``srcset``, ``og:image``, JSON-LD Product
``image``) and EVERY document (``.pdf`` / datasheet / manual / spec / drawing
anchors).

Reference-only: it returns URLs, downloading and redistributing NOTHING here —
the hand-off (``reveng_start_from_product`` in :mod:`engine_oracle.reveng_rpc`)
is what acquires the bytes into the user's own local session. Best-effort and
honest: a login/challenge wall or a fetch failure degrades to
``{ok:false, reason, hint}``; a page that fetched fine but named no media returns
``{ok:true, images:[], documents:[]}``. Never raises, never fabricates a URL.
CI-safe: no live network (tests monkeypatch ``browser_fetch``).
"""
from __future__ import annotations

import re
from urllib.parse import urljoin, urlparse

from services.browser import session as _session
from services.browser.adapters._retail import (
    _ANCHOR_RE,
    _iter_jsonld_nodes,
    _JSONLD_RE,
    _strip_tags,
)

import json as _json

#: Cap per list — a gallery page can name dozens of thumbnails; keep it bounded.
_MAX_ITEMS = 30

_IMG_TAG_RE = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
_SRC_ATTR_RE = re.compile(r'\bsrc=["\']([^"\']+)["\']', re.IGNORECASE)
_SRCSET_ATTR_RE = re.compile(r'\bsrcset=["\']([^"\']+)["\']', re.IGNORECASE)
_OG_IMAGE_RE = re.compile(
    r'<meta\b[^>]*property=["\']og:image["\'][^>]*content=["\']([^"\']+)["\']',
    re.IGNORECASE,
)
#: og:image with content-before-property attribute ordering.
_OG_IMAGE_ALT_RE = re.compile(
    r'<meta\b[^>]*content=["\']([^"\']+)["\'][^>]*property=["\']og:image["\']',
    re.IGNORECASE,
)

#: A document link qualifies on link-text OR href matching these words …
_DOC_WORD_RE = re.compile(r"spec|data\s*sheet|datasheet|manual|drawing|sds", re.IGNORECASE)


def _largest_srcset(srcset: str) -> str | None:
    """Pick the highest-resolution candidate from a ``srcset`` string.

    ``srcset`` is ``"url1 320w, url2 640w, url3 2x"`` — take the URL with the
    largest width/density descriptor (bare-URL entries count as descriptor 0).
    """
    best_url: str | None = None
    best_w = -1.0
    for part in srcset.split(","):
        tokens = part.strip().split()
        if not tokens:
            continue
        url = tokens[0].strip()
        if not url:
            continue
        w = 0.0
        if len(tokens) > 1:
            desc = tokens[1].strip().lower().rstrip("wx")
            try:
                w = float(desc)
            except ValueError:
                w = 0.0
        if w > best_w:
            best_w, best_url = w, url
    return best_url


def _add(seq: list[str], seen: set[str], base_url: str, raw: str | None) -> None:
    """Absolutize + de-dupe (order-preserving), capping at ``_MAX_ITEMS``."""
    if not raw or len(seq) >= _MAX_ITEMS:
        return
    raw = raw.strip()
    if not raw or raw.startswith("data:"):
        return
    absolute = urljoin(base_url, raw)
    if absolute in seen:
        return
    seen.add(absolute)
    seq.append(absolute)


def _extract_images(html: str, base_url: str) -> list[str]:
    images: list[str] = []
    seen: set[str] = set()

    # 1) og:image (usually the single best hero shot) — try both attr orders.
    for pat in (_OG_IMAGE_RE, _OG_IMAGE_ALT_RE):
        for m in pat.finditer(html):
            _add(images, seen, base_url, m.group(1))

    # 2) JSON-LD Product image field(s) — string or list, every node.
    for m in _JSONLD_RE.finditer(html):
        try:
            data = _json.loads(m.group(1).strip())
        except (ValueError, TypeError):
            continue
        for node in _iter_jsonld_nodes(data):
            img = node.get("image") if isinstance(node, dict) else None
            for candidate in img if isinstance(img, list) else [img]:
                if isinstance(candidate, dict):
                    candidate = candidate.get("url")
                if isinstance(candidate, str):
                    _add(images, seen, base_url, candidate)

    # 3) Every <img>: prefer the largest srcset candidate, else the src.
    for m in _IMG_TAG_RE.finditer(html):
        tag = m.group(0)
        ss = _SRCSET_ATTR_RE.search(tag)
        if ss:
            _add(images, seen, base_url, _largest_srcset(ss.group(1)))
        src = _SRC_ATTR_RE.search(tag)
        if src:
            _add(images, seen, base_url, src.group(1))

    return images


def _extract_documents(html: str, base_url: str) -> list[str]:
    """Every ``.pdf`` href, plus any anchor whose text/href reads like a datasheet/
    manual/spec/drawing/sds. Broader than ``_retail.find_datasheet_url`` (which
    returns only the FIRST and requires a doc-word)."""
    docs: list[str] = []
    seen: set[str] = set()
    for m in _ANCHOR_RE.finditer(html):
        href, text = m.group(1), m.group(2)
        stem = href.split("?", 1)[0].lower()
        is_pdf = stem.endswith(".pdf")
        is_doc_word = bool(_DOC_WORD_RE.search(_strip_tags(text)) or _DOC_WORD_RE.search(href))
        if is_pdf or is_doc_word:
            _add(docs, seen, base_url, href)
    return docs


def _resolve(url_or_part_id: str) -> tuple[str, str] | None:
    """Return ``(url, site)`` for a ``catalog:<site>::<url>`` part_id or a bare URL.

    A bare URL infers ``site`` from its domain (best-effort — the registered
    session profile keys are domain-ish, and an unknown site just gets a fresh
    anonymous profile via ``browser_fetch``). Returns ``None`` when no URL can be
    resolved.
    """
    s = (url_or_part_id or "").strip()
    if not s:
        return None
    if s.startswith("catalog:"):
        rest = s[len("catalog:"):]
        site, sep, url = rest.partition("::")
        if not sep or not url:
            return None
        return url, site
    if s.startswith("http://") or s.startswith("https://"):
        netloc = urlparse(s).netloc.lower()
        if netloc.startswith("www."):
            netloc = netloc[4:]
        # site slug = registrable-ish label (host minus TLD), best-effort.
        parts = netloc.split(".")
        site = parts[-2] if len(parts) >= 2 else (parts[0] if parts else netloc)
        return s, site or netloc
    return None


def harvest_product_media(url_or_part_id: str) -> dict:
    """Fetch a product page and collect reference-only image + document URLs.

    Returns ``{ok, source_url, images:[...], documents:[...]}`` on success (either
    list may be empty), or ``{ok:false, reason, hint}`` when the URL can't be
    resolved, the fetch fails, or a login/challenge wall is hit. Never raises.
    """
    try:
        resolved = _resolve(url_or_part_id)
        if resolved is None:
            return {"ok": False, "reason": "bad_reference",
                    "hint": "expected a http(s) URL or a 'catalog:<site>::<url>' part_id"}
        url, site = resolved

        fetched = _session.browser_fetch(url, site)
        if not fetched.get("ok"):
            # Propagate the honest browser-layer failure (login wall, bot wall,
            # playwright-missing, http error, …) with a harvest-shaped hint.
            return {"ok": False,
                    "reason": fetched.get("reason", "fetch_failed"),
                    "hint": fetched.get("hint") or fetched.get("detail")
                            or f"could not fetch {url} under session '{site}'"}

        html = fetched.get("content", "") or ""
        return {
            "ok": True,
            "source_url": url,
            "images": _extract_images(html, url),
            "documents": _extract_documents(html, url),
        }
    except Exception as exc:  # noqa: BLE001 — harvest is best-effort, never raises
        return {"ok": False, "reason": f"harvest_failed: {exc}",
                "hint": "unexpected error while harvesting product media"}
