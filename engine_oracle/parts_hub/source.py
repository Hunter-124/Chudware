"""part_source — deterministic sourcing fan-out over the taxonomy + parts hub.

Parts Library V2 workstream 5: given a natural-language *need* ("4130 chromoly
tube OD 25.4 wall 1.25"), this module — with NO LLM inside —

  1. tokenizes the need (+ optional constraints),
  2. matches taxonomy categories by label / query_terms / id segments,
  3. extracts numeric+unit hints ("OD 25.4", "12mm bore", "wall 1.25") and enum
     hints ("4130", "stainless") into a facet query keyed to the category's
     EFFECTIVE facet definitions,
  4. runs ``hub.search`` per matched category (taxonomy-routed domains),
  5. scores candidates on spec-fit (per-facet match), price presence, source
     (local vs live) and artifact availability, and
  6. attaches an ``artifact_plan`` telling the caller HOW to get real geometry:
     ``part_place`` (local record with geometry_ref), ``scrape_import`` (browser
     catalog record — site + query), or ``make_standard_part`` (fastener-ish
     standard hardware without a direct geometry_ref).

Everything degrades gracefully: no taxonomy → unrouted search; a failing hub
search contributes zero records; the function never raises for bad input.
The result envelope is documented in contracts/sourcing.md.
"""
from __future__ import annotations

import re
from typing import Any, Callable

from engine_oracle.parts_hub import taxonomy as _tax

# ---------------------------------------------------------------------------
# Tokenization + numeric-hint extraction
# ---------------------------------------------------------------------------

_UNIT_TO_MM: dict[str, float] = {
    "mm": 1.0, "millimeter": 1.0, "millimeters": 1.0,
    "cm": 10.0, "in": 25.4, "inch": 25.4, "inches": 25.4, '"': 25.4,
}

#: hint word (as typed by a human) → facet-key stems it can satisfy.
_HINT_KEYS: dict[str, tuple[str, ...]] = {
    "od":        ("od_mm", "tube_od_mm"),
    "bore":      ("bore_mm",),
    "id":        ("id_mm",),
    "wall":      ("wall_mm",),
    "dia":       ("diameter_mm", "tool_diameter_mm", "wheel_diameter_mm"),
    "diameter":  ("diameter_mm", "tool_diameter_mm", "wheel_diameter_mm"),
    "length":    ("length_mm", "loc_mm"),
    "width":     ("width_mm", "rail_width_mm"),
    "thickness": ("thickness_mm",),
    "stroke":    ("stroke_mm",),
    "shaft":     ("shaft_mm",),
    "pitch":     ("pitch_mm", "pitch"),
    "lead":      ("lead_mm",),
    "teeth":     ("teeth",),
    "flutes":    ("flutes",),
}

#: colloquial material names → the enum value the taxonomy actually carries.
_ENUM_ALIASES: dict[str, str] = {
    "chromoly": "4130",
    "cromoly": "4130",
    "chrome-moly": "4130",
    "stainless": "304",
    "aluminum": "6061",
    "aluminium": "6061",
    "titanium": "Ti-6Al-4V",
}

_WORD_RE = re.compile(r"[a-z0-9#/.\-]+")
_NUM = r"(\d+(?:\.\d+)?)"
_UNIT = r"(mm|cm|inches|inch|in|\")"
# "od 25.4", "od 25.4mm", "wall 1.25"
_KEY_VAL_RE = re.compile(rf"\b([a-z]+)\s+{_NUM}\s*{_UNIT}?\b", re.IGNORECASE)
# "12mm bore", "1 in od"
_VAL_KEY_RE = re.compile(rf"\b{_NUM}\s*{_UNIT}\s+([a-z]+)\b", re.IGNORECASE)


def _tokens(text: str) -> list[str]:
    return _WORD_RE.findall((text or "").lower())


def _to_mm(val: str, unit: str | None) -> float:
    v = float(val)
    return v * _UNIT_TO_MM.get((unit or "mm").lower(), 1.0)


def extract_hints(text: str) -> dict[str, float]:
    """Extract ``{hint_word: value_mm}`` numeric hints from free text.

    Both "OD 25.4" / "wall 1.25" (key-first) and "12mm bore" (value-first)
    forms are recognized; inch values are converted to mm.
    """
    hints: dict[str, float] = {}
    for m in _KEY_VAL_RE.finditer(text or ""):
        key = m.group(1).lower()
        if key in _HINT_KEYS:
            hints.setdefault(key, _to_mm(m.group(2), m.group(3)))
    for m in _VAL_KEY_RE.finditer(text or ""):
        key = m.group(3).lower()
        if key in _HINT_KEYS:
            hints.setdefault(key, _to_mm(m.group(1), m.group(2)))
    return hints


# ---------------------------------------------------------------------------
# Category matching
# ---------------------------------------------------------------------------

def _iter_nodes(doc: dict) -> list[tuple[dict, int]]:
    """All taxonomy nodes with their depth (roots = 0)."""
    out: list[tuple[dict, int]] = []

    def _walk(node: dict, depth: int) -> None:
        out.append((node, depth))
        for child in node.get("children") or []:
            _walk(child, depth + 1)

    for cat in doc.get("categories", []):
        _walk(cat, 0)
    return out


def _node_score(node: dict, depth: int, tokens: set[str]) -> float:
    score = 0.0
    for term in node.get("query_terms") or []:
        words = _tokens(term)
        if not words:
            continue
        matched = sum(1 for w in words if w in tokens)
        if matched:
            score += 2.0 * matched / len(words)
    for w in _tokens(node.get("label", "")):
        if w in tokens:
            score += 1.5
    tail = node.get("id", "").rsplit(".", 1)[-1]
    for w in tail.replace("_", " ").split():
        if w in tokens:
            score += 1.0
    if score > 0:
        score += 0.1 * depth  # prefer the more specific node on ties
    return score


def match_categories(need: str, *, max_categories: int = 3) -> list[str]:
    """Rank taxonomy category ids against the *need* text (deterministic)."""
    tokens = set(_tokens(need))
    if not tokens:
        return []
    doc = _tax.load_taxonomy()
    scored = [(node["id"], _node_score(node, depth, tokens))
              for node, depth in _iter_nodes(doc)]
    scored = [(cid, s) for cid, s in scored if s > 0]
    scored.sort(key=lambda t: (-t[1], t[0]))
    picked: list[str] = []
    for cid, _s in scored:
        # Drop a candidate that is an ancestor of an already-picked deeper node.
        if any(p.startswith(cid + ".") for p in picked):
            continue
        if any(cid.startswith(p + ".") for p in picked):
            continue
        picked.append(cid)
        if len(picked) >= max_categories:
            break
    return picked


def effective_facets_deep(category_id: str) -> list[dict]:
    """Facet definitions for a category INCLUDING its subtree (deduped by key).

    A need often matches a mid-level node ("bearings_motion") whose useful
    facets (bore_mm, od_mm) are defined on leaf children — sourcing wants the
    union so hints can still attach.
    """
    merged: dict[str, dict] = {}
    for f in _tax.effective_facets(category_id):
        if f.get("key"):
            merged.setdefault(f["key"], f)
    for did in sorted(_tax.descendant_ids(category_id)):
        node = _tax.get_node(did)
        for f in (node or {}).get("facets") or []:
            if f.get("key"):
                merged.setdefault(f["key"], f)
    return list(merged.values())


#: taxonomy facet key → param-name aliases found on local PartRecords.
_PARAM_ALIASES: dict[str, tuple[str, ...]] = {
    "od_mm":            ("outer_d", "od", "outer_diameter"),
    "bore_mm":          ("bore",),
    "id_mm":            ("id", "inner_d"),
    "width_mm":         ("width", "b"),
    "wall_mm":          ("wall", "wall_thickness"),
    "length_mm":        ("length", "l"),
    "diameter_mm":      ("d", "diameter"),
    "tool_diameter_mm": ("diameter", "d"),
    "thickness_mm":     ("thickness", "t"),
    "thread":           ("thread", "designation", "size"),
    "material":         ("material", "material_name"),
    "alloy":            ("alloy", "material", "grade"),
}


def facet_params_view(params: dict, keys: list[str] | set[str]) -> dict:
    """Project record *params* onto taxonomy facet *keys*, resolving aliases.

    For each facet key, picks the first of: the key itself, the key without a
    unit suffix ("od_mm" → "od"), then the alias table. Returns a params dict
    keyed by the FACET keys, suitable for ``taxonomy.match_facets``.
    """
    params = params or {}
    out: dict = {}
    for key in keys:
        candidates = [key]
        for suffix in ("_mm", "_in", "_kg", "_v", "_bar"):
            if key.endswith(suffix):
                candidates.append(key[: -len(suffix)])
        candidates.extend(_PARAM_ALIASES.get(key, ()))
        for cand in candidates:
            if cand in params:
                out[key] = params[cand]
                break
    return out


def build_facets(category_id: str, text: str) -> dict[str, Any]:
    """Build a facet query for *category_id* from the hint text.

    Numeric hints attach to range facets whose key matches the hint's stems;
    tokens matching an enum facet's values (directly or via alias) attach to it.
    Facet definitions are gathered from the category AND its subtree.
    """
    defs = effective_facets_deep(category_id)
    if not defs:
        return {}
    hints = extract_hints(text)
    tokens = set(_tokens(text))
    aliased = {(_ENUM_ALIASES.get(t) or "").lower() for t in tokens} - {""}
    facets: dict[str, Any] = {}
    for f in defs:
        key = str(f.get("key", ""))
        ftype = f.get("type")
        if ftype == "range":
            for hint, stems in _HINT_KEYS.items():
                if hint in hints and (key in stems or key == hint):
                    facets[key] = hints[hint]
                    break
        elif ftype == "enum":
            values = [str(v) for v in f.get("values") or []]
            hit = [v for v in values
                   if v.lower() in tokens or v.lower() in aliased]
            if hit:
                facets[key] = hit[0] if len(hit) == 1 else hit
    return facets


# ---------------------------------------------------------------------------
# Candidate scoring + artifact planning
# ---------------------------------------------------------------------------

_STANDARD_HW_DOMAINS = frozenset({"fastener", "bearing", "mech_family"})


def _first_price(rec: dict) -> float:
    for row in rec.get("sourcing") or []:
        if isinstance(row, dict):
            for k in ("price", "unit_price", "price_usd"):
                try:
                    v = row.get(k)
                    if v is not None and float(v) > 0:
                        return float(v)
                except (TypeError, ValueError):
                    continue
    params = rec.get("params") or {}
    for k in ("price_usd", "price", "unit_price"):
        try:
            v = params.get(k)
            if v is not None and float(v) > 0:
                return float(v)
        except (TypeError, ValueError):
            continue
    return 0.0


def _vendor_of(rec: dict) -> str:
    pid = str(rec.get("part_id", ""))
    if pid.startswith("catalog:"):
        return pid[len("catalog:"):].split("::", 1)[0]
    params = rec.get("params") or {}
    for k in ("vendor", "manufacturer", "supplier"):
        if params.get(k):
            return str(params[k])
    return "library" if rec.get("source") != "live" else "vendor_api"


#: raw-stock param signals — a record carrying any of these is modeled
#: parametrically (metals/plastics tube/bar), never "found" or reverse-engineered.
_RAW_STOCK_PARAMS = frozenset({"od_mm", "wall_mm", "alloy"})


def _is_discrete_product(rec: dict, gref: dict) -> bool:
    """True when *rec* is a discrete manufactured product with an identity but no
    geometry (contracts/sourcing.md §2 ``find_model`` boundary).

    Requires a product-identity signal (a normalized MPN/part-number OR a
    brand/manufacturer) AND: no usable geometry_ref, NOT standard hardware, and
    NO raw-stock params (od_mm/wall_mm/alloy) — those keep scrape_import /
    make_standard_part.
    """
    if gref.get("kind"):
        return False
    if rec.get("domain") in _STANDARD_HW_DOMAINS:
        return False
    # A record already FROM a CAD-model-yielding site (grabcad/traceparts/…) keeps
    # its direct scrape_import plan — that download IS the geometry. Only records
    # from commerce/no-CAD sites (amazon, retail) need find_model's repo search.
    pid = str(rec.get("part_id", ""))
    if pid.startswith("catalog:"):
        site = pid[len("catalog:"):].split("::", 1)[0]
        try:
            from engine_oracle.parts_hub.model_matcher import CAD_MODEL_SITES
            if site in CAD_MODEL_SITES:
                return False
        except Exception:  # noqa: BLE001 — matcher optional; fall through to identity check
            pass
    params = rec.get("params") or {}
    if any(k in params for k in _RAW_STOCK_PARAMS):
        return False
    mpn = ""
    for k in ("mpn", "part_number", "sku", "lcsc"):
        v = params.get(k)
        if v is not None and str(v).strip():
            mpn = str(v).strip()
            break
    has_brand = any(str(params.get(k) or "").strip()
                    for k in ("brand", "manufacturer"))
    return bool(mpn) or has_brand


def artifact_plan(rec: dict, *, need: str = "",
                  route_sites: list[str] | None = None) -> dict:
    """How to obtain real geometry for a candidate record.

    Vocabulary (contracts/sourcing.md): part_place | scrape_import |
    make_standard_part.
    """
    pid = str(rec.get("part_id", ""))
    gref = rec.get("geometry_ref") or {}
    # Discrete manufactured product (an MPN/brand identity, no geometry, NOT
    # standard hardware, NOT raw stock) → the caller matches it to a CAD repo
    # model via match_model. Checked FIRST so a discrete-product catalog record
    # (e.g. an Amazon listing catalog:amazon::…) routes to find_model rather
    # than a bare scrape_import; raw stock / standard hardware keep their plans.
    if _is_discrete_product(rec, gref):
        return {"how": "find_model", "part_id": pid}
    if pid.startswith("catalog:"):
        site = pid[len("catalog:"):].split("::", 1)[0]
        return {"how": "scrape_import", "site": site,
                "query": str(rec.get("title") or need)}
    if gref.get("kind"):
        return {"how": "part_place"}
    if rec.get("domain") in _STANDARD_HW_DOMAINS:
        return {"how": "make_standard_part"}
    return {"how": "scrape_import",
            "site": (route_sites or [""])[0],
            "query": str(rec.get("title") or need)}


def _facet_fit(rec: dict, facets: dict, defs: list[dict]) -> tuple[float, list[str]]:
    """Fraction of queried facets the record's params satisfy + a match trace."""
    if not facets:
        return 0.0, []
    view = facet_params_view(rec.get("params") or {}, facets.keys())
    matched: list[str] = []
    for key, q in facets.items():
        if key in view and _tax.match_facets(view, {key: q}, defs):
            matched.append(f"{key}={q}")
    return len(matched) / len(facets), matched


def score_candidate(rec: dict, *, facets: dict, defs: list[dict],
                    need_tokens: set[str]) -> tuple[float, str]:
    """Deterministic candidate score + human rationale."""
    fit, matched = _facet_fit(rec, facets, defs)
    score = 3.0 * fit
    reasons: list[str] = []
    if matched:
        reasons.append("spec fit: " + ", ".join(matched))
    title_toks = set(_tokens(str(rec.get("title", ""))))
    overlap = (len(need_tokens & title_toks) / len(need_tokens)) if need_tokens else 0.0
    if need_tokens:
        score += 0.5 * overlap
    # Relevance gate: a record that matches NONE of the queried facets and shares
    # negligible title vocabulary with the need is almost certainly the wrong part
    # family (e.g. a fastener FTS-matched on a stray shared token like "OD" when
    # the correct category has zero local records). Without this, the flat
    # "local library"/"placeable geometry" bonuses below let such a false-positive
    # float above a legitimately-empty result set. Skip the gate when the need
    # carried no concrete facet constraints — title overlap is the only signal.
    relevant = (not facets) or fit > 0 or overlap >= 0.34
    price = _first_price(rec)
    if price > 0:
        score += 0.3
        reasons.append(f"priced ({price:g})")
    if rec.get("source") == "live":
        if relevant:
            score += 0.1
            reasons.append("live vendor result")
    elif relevant:
        score += 0.4
        reasons.append("local library record")
    gref = rec.get("geometry_ref") or {}
    if relevant and gref.get("kind"):
        score += 0.5
        reasons.append("placeable geometry available")
    elif relevant and str(rec.get("part_id", "")).startswith("catalog:"):
        score += 0.2
        reasons.append("downloadable via fetch queue")
    if not relevant:
        reasons.append("low relevance to requested specs")
    return round(score, 4), "; ".join(reasons)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def source(need: str, constraints: str | None = None, limit: int = 5, *,
           search_fn: Callable[..., list[dict]] | None = None) -> dict:
    """Deterministic sourcing fan-out. NO LLM inside — pure routing + ranking.

    Returns ``{ok, interpreted, candidates, coverage}`` (contracts/sourcing.md).
    *search_fn* is injectable for tests; defaults to the parts hub search.
    """
    need = str(need or "").strip()
    if not need:
        return {"ok": False, "reason": "need is required",
                "hint": "Describe the part you need, e.g. '4130 tube OD 25.4 wall 1.25'."}
    limit = max(1, min(int(limit or 5), 50))
    text = need if not constraints else f"{need} {constraints}"
    need_tokens = set(_tokens(text))

    if search_fn is None:
        from engine_oracle.parts_hub import search as search_fn  # noqa: PLC0415

    categories = match_categories(text)
    primary = categories[0] if categories else ""
    facets = build_facets(primary, text) if primary else {}
    defs = effective_facets_deep(primary) if primary else []

    seen: dict[str, dict] = {}
    per_cat_hits = 0
    for cid in categories or [""]:
        routing = _tax.route(cid) if cid else {"domains": [], "sites": [], "query_terms": []}
        domains = routing["domains"] or None
        try:
            recs = search_fn(need, domain=domains, limit=limit * 4)
        except Exception:  # noqa: BLE001 — one bad route contributes nothing
            recs = []
        per_cat_hits += len(recs)
        for r in recs:
            pid = str(r.get("part_id", ""))
            if pid and pid not in seen:
                r = dict(r)
                r["_route_sites"] = routing["sites"]
                seen[pid] = r

    candidates: list[dict] = []
    for rec in seen.values():
        sc, rationale = score_candidate(rec, facets=facets, defs=defs,
                                        need_tokens=need_tokens)
        candidates.append({
            "part_id": rec.get("part_id", ""),
            "title": rec.get("title", ""),
            "vendor": _vendor_of(rec),
            "price": _first_price(rec) or None,
            "specs": rec.get("params") or {},
            "source": rec.get("source", "local") or "local",
            "score": sc,
            "rationale": rationale,
            "artifact_plan": artifact_plan(rec, need=need,
                                           route_sites=rec.get("_route_sites")),
        })
    candidates.sort(key=lambda c: (-c["score"], c["part_id"]))
    candidates = candidates[:limit]

    local = sum(1 for c in candidates if c["source"] != "live")
    return {
        "ok": True,
        "interpreted": {
            "category_id": primary,
            "categories": categories,
            "facets": facets,
            "terms": sorted(need_tokens),
        },
        "candidates": candidates,
        "coverage": {
            "categories_matched": len(categories),
            "records_considered": len(seen),
            "raw_hits": per_cat_hits,
            "local_candidates": local,
            "live_candidates": len(candidates) - local,
            "facets_queried": len(facets),
        },
    }
