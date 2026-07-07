"""model_matcher — deterministic product → CAD-model matcher (aicad.match_model/1).

Given a discrete manufactured product that lacks a 3D model, fan out across the
CAD-MODEL-YIELDING source adapters (grabcad, traceparts, content3d,
freecadlibrary, ultralibrarian, componentsearchengine, snapeda, cadenas + the
native-CAD vendors) and rank candidate models by IDENTITY confidence. There is
NO LLM inside — same input, same output. The result envelope is documented in
contracts/sourcing.md §2a.

Pipeline:
  STEP 1 — resolve the product's MPN against the standard-hardware clean-room
           tables (:func:`spec_sheet_matcher.match_part_number`). A hit IS the
           answer (an ``exact``, ``attach:true`` match) — no repo search needed.
  STEP 2 — fan out ``adapter.search(query)`` across the CAD-model sites, rank
           each hit by confidence tier (exact / strong / weak). Only ``exact``
           (normalized-MPN equality) auto-attaches. When no ``exact`` match
           exists a ``reverse_engineer`` fallback (§2b) is offered — never
           auto-started — seeded with the product identity (images/documents are
           filled by the media-harvest step, left EMPTY here).

Honesty: an adapter needing a human login contributes ZERO matches (it degrades
to an ``ok:false`` login_required response) — counted for coverage, never a
fabricated match. Wrapped per-adapter try/except so one bad site never sinks the
fan-out. Never raises: a contract-shaped ``{ok:false, reason, hint}`` on bad input.
"""
from __future__ import annotations

from typing import Any

from engine_oracle import spec_sheet_matcher as _ssm

# CAD-model-yielding sites ONLY (contracts/sourcing.md §2a). Commerce/datasheet
# adapters do not yield importable geometry and are excluded from the fan-out.
# Public: source.py reads this so a record already FROM a CAD-model site keeps its
# direct scrape_import plan instead of round-tripping through find_model.
CAD_MODEL_SITES = {
    "grabcad", "traceparts", "content3d", "3dcontentcentral", "freecadlibrary",
    "ultralibrarian", "componentsearchengine", "snapeda", "cadenas", "mcmaster",
    "misumi", "boschrexroth", "hydraforce", "parker", "maxon", "orientalmotor",
    "anaheimautomation", "thk", "hiwin", "thomson", "igus",
}
_CAD_MODEL_SITES = CAD_MODEL_SITES  # back-compat alias for internal references

# Confidence tier → score (contract §2a). Only `exact` auto-attaches.
_TIER_SCORE = {"exact": 4.0, "strong": 2.0, "weak": 1.0}

# Hit dict fields that may carry a part number.
_PN_FIELDS = ("mpn", "sku", "lcsc", "part_number", "part_no", "manufacturer_part_number")
# Hit dict fields that may carry a brand / manufacturer.
_BRAND_FIELDS = ("brand", "manufacturer", "supplier", "mfr")


def _fail(reason: str, hint: str) -> dict:
    return {"ok": False, "reason": reason, "hint": hint}


def _first_str(d: dict, keys: tuple[str, ...]) -> str:
    for k in keys:
        v = d.get(k)
        if v is not None and str(v).strip():
            return str(v).strip()
    return ""


def _extract_product(product: dict) -> dict:
    """Pull {title, mpn, brand, source_url, part_id} from a PartRecord or plain dict."""
    params = product.get("params") or {}
    title = _first_str(product, ("title", "name", "description")) or _first_str(params, ("title", "name"))
    mpn = _first_str(product, ("mpn", "part_number", "sku", "lcsc")) or \
        _first_str(params, ("mpn", "part_number", "sku", "lcsc"))
    brand = _first_str(product, ("brand", "manufacturer")) or \
        _first_str(params, ("brand", "manufacturer"))
    source_url = _first_str(product, ("source_url", "url")) or _first_str(params, ("source_url", "url"))
    if not source_url:
        pid = str(product.get("part_id") or "")
        if pid.startswith("catalog:") and "::" in pid:
            source_url = pid.split("::", 1)[1]
    return {
        "title": title,
        "mpn": mpn,
        "brand": brand,
        "source_url": source_url,
        "part_id": str(product.get("part_id") or ""),
    }


def _hit_pn(hit: dict) -> str:
    return _first_str(hit, _PN_FIELDS)


def _hit_brand(hit: dict) -> str:
    return _first_str(hit, _BRAND_FIELDS)


def _hit_site(hit: dict) -> str:
    return _first_str(hit, ("site",))


def _hit_part_id(hit: dict) -> str:
    """Deterministic part_id for a hit: catalog:<site>::<url> when a url is present."""
    site = _hit_site(hit)
    url = _first_str(hit, ("url",))
    if site and url:
        return f"catalog:{site}::{url}"
    return f"catalog:{site}::{_hit_pn(hit) or _first_str(hit, ('title',))}"


def _token_overlap(a: str, b: str) -> float:
    ta = {t for t in a.lower().split() if len(t) > 1}
    tb = {t for t in b.lower().split() if len(t) > 1}
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta)


def _classify(product: dict, hit: dict) -> str | None:
    """Confidence tier for a hit given the product identity, or None to drop it.

    exact  — normalized-MPN equality between product and hit.
    strong — brand match + one part-number is a substring of the other.
    weak   — title-token overlap only (no part-number agreement).
    """
    p_mpn = product["mpn"]
    p_mpn_n = _ssm.normalize_mpn(p_mpn) if p_mpn else ""
    h_pn = _hit_pn(hit)
    h_pn_n = _ssm.normalize_mpn(h_pn) if h_pn else ""

    if p_mpn_n and h_pn_n and p_mpn_n.upper() == h_pn_n.upper():
        return "exact"

    p_brand = product["brand"].lower()
    h_brand = _hit_brand(hit).lower()
    brand_match = bool(p_brand and h_brand and (p_brand in h_brand or h_brand in p_brand))
    if brand_match and p_mpn_n and h_pn_n:
        a, b = p_mpn_n.upper(), h_pn_n.upper()
        if a in b or b in a:
            return "strong"

    title = _first_str(hit, ("title",))
    if title and product["title"] and _token_overlap(product["title"], title) > 0.0:
        return "weak"
    return None


def _cad_adapters() -> list[Any]:
    try:
        from services.browser import adapters as _adapters
        return [a for a in _adapters.all_adapters()
                if getattr(a, "key", None) in _CAD_MODEL_SITES]
    except Exception:  # noqa: BLE001 — no browser stack → no repo search, RE fallback stands
        return []


def _standard_match(product: dict) -> dict | None:
    """STEP 1: resolve the product MPN via the standard-hardware clean-room tables.

    A hit IS the answer — an ``exact``/``attach:true`` match whose artifact_plan
    builds the part parametrically (make_standard_part for a fastener/bearing
    family, else part_place). ``generate=False``: we only need identity here, not
    a (no-op-offline) core build. ``None`` when the MPN does not resolve.
    """
    mpn = product["mpn"]
    if not mpn:
        return None
    try:
        res = _ssm.match_part_number(mpn, generate=False)
    except Exception:  # noqa: BLE001
        return None
    if not res or not res.get("matched"):
        return None
    family = res.get("family")
    if family:
        plan = {"how": "make_standard_part", "family": str(family),
                "size": str(res.get("size") or ""), "query": mpn}
    else:
        plan = {"how": "part_place", "query": mpn}
    return {
        "part_id": res.get("catalog_id") or f"standard:{_ssm.normalize_mpn(mpn)}",
        "title": res.get("standard_name") or res.get("description") or product["title"] or mpn,
        "site": "standard_hardware",
        "mpn": _ssm.normalize_mpn(mpn),
        "confidence": "exact",
        "score": _TIER_SCORE["exact"],
        "attach": True,
        "artifact_plan": plan,
    }


def match_model(product: dict, *, limit: int = 8) -> dict:
    """Match a product to a CAD-repo model. Contract-shaped (contracts/sourcing.md §2a).

    ``product`` is a PartRecord (from ``part_get``) or a plain
    ``{title, mpn?, brand?, source_url?, part_id?}``. Returns
    ``{ok, product, matches, best, fallback, coverage}``. Deterministic — no LLM,
    no randomness. Never raises: an ``{ok:false, reason, hint}`` on bad input.
    """
    if not isinstance(product, dict):
        return _fail("product must be a record or {title, mpn?, brand?}",
                     "Pass a PartRecord or a dict with at least a title or mpn.")
    prod = _extract_product(product)
    if not prod["title"] and not prod["mpn"]:
        return _fail("product needs a title or mpn to match",
                     "Provide at least a product title or a manufacturer part number.")

    limit = max(1, min(int(limit or 8), 50))
    prod_out = {"title": prod["title"], "mpn": prod["mpn"], "brand": prod["brand"]}

    # STEP 1 — standard-hardware resolution short-circuits the repo search.
    std = _standard_match(prod)
    if std is not None:
        return {
            "ok": True,
            "product": prod_out,
            "matches": [std],
            "best": std,
            "fallback": None,  # an exact match exists → no RE fallback
            "coverage": {"sites_searched": 0, "raw_hits": 0},
        }

    # STEP 2 — fan out across CAD-model adapters.
    query = prod["mpn"] or (f"{prod['brand']} {prod['title']}".strip())
    matches: list[dict] = []
    sites_searched = 0
    raw_hits = 0
    for adapter in _cad_adapters():
        site = getattr(adapter, "key", "")
        sites_searched += 1
        try:
            hits = adapter.search(query)
        except Exception:  # noqa: BLE001 — one bad adapter never sinks the fan-out
            continue
        if not isinstance(hits, list):
            continue
        for hit in hits:
            if not isinstance(hit, dict):
                continue
            # A login-required / degrade response is not a hit: skip (counted for
            # coverage via sites_searched already), never a fabricated match.
            if hit.get("ok") is False:
                continue
            raw_hits += 1
            tier = _classify(prod, hit)
            if tier is None:
                continue
            pid = _hit_part_id(hit)
            hsite = _hit_site(hit) or site
            q = prod["mpn"] or _first_str(hit, ("title",)) or prod["title"]
            matches.append({
                "part_id": pid,
                "title": _first_str(hit, ("title",)),
                "site": hsite,
                "mpn": _ssm.normalize_mpn(_hit_pn(hit)) if _hit_pn(hit) else "",
                "confidence": tier,
                "score": _TIER_SCORE[tier],
                "attach": tier == "exact",
                "artifact_plan": {"how": "scrape_import", "site": hsite, "query": q},
            })

    # Deterministic stable sort: score desc, then part_id.
    matches.sort(key=lambda m: (-m["score"], m["part_id"]))

    has_exact = any(m["confidence"] == "exact" for m in matches)
    best = matches[0] if matches else None

    fallback = None
    if not has_exact:
        fallback = {
            "how": "reverse_engineer",
            "reason": "no exact match" if matches else "no repo match",
            "seed": {
                "title": prod["title"],
                "mpn": prod["mpn"],
                "brand": prod["brand"],
                "source_url": prod["source_url"],
                "images": [],       # filled by product_media.harvest_product_media
                "documents": [],    # filled by product_media.harvest_product_media
            },
        }

    return {
        "ok": True,
        "product": prod_out,
        "matches": matches[:limit],
        "best": best,
        "fallback": fallback,
        "coverage": {"sites_searched": sites_searched, "raw_hits": raw_hits},
    }
