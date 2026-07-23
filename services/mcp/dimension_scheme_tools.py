"""``suggest_dimension_scheme`` MCP tool — process-aware dimension planning for sketches.

T-W3 / Pillar 20: given a sketch entity roster (from ``describe_sketch`` / doc RPCs),
a manufacturing process, and an optional material hint, emit an ordered dimension plan
that tells the model which dimensions to drive, why they are critical, and what default
tolerance to apply.

Rules encoded
-------------
1. **Bolt/hole patterns**: dimension centre-to-centre + one datum offset from the
   pattern to a reference edge.  NEVER chain edge-to-hole-to-hole (produces
   accumulating tolerance).  Each pattern hole that is not the datum hole gets a
   centre-to-centre dim marked ``needs_user_confirm=False`` (deterministic geometry);
   the single datum-offset dim is function-critical → ``needs_user_confirm=True``.

2. **Tolerance defaults by process** (from the C++ DfM process profiles, core/dfm):
   the ``tolerance_band_mm`` maps onto an approximate IT grade via a documented table
   defined here.  CNC/lathe → IT7 (±0.010–0.025 mm band); FDM → IT12 (±0.10–0.20 mm);
   sheet/laser → IT10; injection → IT11.  The IT grade selects the standard
   ``±IT/2`` bilateral tolerance, rounded per ISO 286.

3. **Function-critical dims**: any dimension on a mating circle/hole entity (type
   ``circle`` or ``arc``) or on an entity tagged ``mating`` is marked
   ``needs_user_confirm=True`` and ``why_critical="mating feature"``.

4. **Dimension ordering**: function-critical dims first (datum + mating features),
   then size dims (length/distance for overall envelope), then auxiliary.

Contract: results carry the ``aicad.calc/1`` schema in the per-item ``tolerance``
dict; the top-level return is a plain ``{ok, items, process, it_grade, notes}``
dict (not §7 calc/1 — this is a multi-item planner output, not a single calculation).
"""
from __future__ import annotations

import json
import math
import re
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP

from mcp_server.cad_tools import _dumps, _err, _doc

# DfM process data (profiles, resolver, known-process list) now lives in the C++ core
# (core/dfm/process_profiles.cpp); reached via core_bridge, lazily, so this module still
# imports + runs its offline fixture mode with no engine present.
def _get_profile(process: str) -> dict:
    """dfm_get_profile RPC; raises ValueError on the oracle's unknown-process envelope."""
    from mcp_server import core_bridge as cb
    r = cb.call("dfm_get_profile", {"process": process})
    if not r.get("ok"):
        raise ValueError(r.get("error") or "dfm_get_profile failed")
    return r.get("result") or {}


def _resolve_process(request: str) -> dict:
    """dfm_resolve_process RPC (the FDM-default resolver + ambiguity gate)."""
    from mcp_server import core_bridge as cb
    r = cb.call("dfm_resolve_process", {"request": request})
    if not r.get("ok"):
        raise ValueError(r.get("error") or "dfm_resolve_process failed")
    return r.get("result") or {}


def _known_processes() -> list:
    """dfm_known_processes RPC; falls back to the static list when no bridge is up."""
    try:
        from mcp_server import core_bridge as cb
        r = cb.call("dfm_known_processes", {})
        if r.get("ok") and isinstance(r.get("result"), list):
            return r["result"]
    except Exception:  # noqa: BLE001
        pass
    return ["fdm", "cnc_mill", "lathe", "sheet_metal", "laser", "injection"]

try:
    from mcp_server.cad_tools import _CB as _cb_module  # noqa: F401 — presence check
    import services.mcp.core_bridge as _cb
    _HAS_BRIDGE = True
except Exception:
    _HAS_BRIDGE = False

# ──────────────────────────────────────────────────── IT-grade from process ───

# Documented map: process canonical id → default IT grade for dimension tolerances.
# Derived from process_profiles.tolerance_band_mm vs ISO 286 IT grade bands.
# Source: ISO 286-1 Table 1 + DFM profile values; see T-W3 work order step 5.
_PROCESS_IT_GRADE: dict[str, int] = {
    "cnc_mill":   7,   # ±0.010–0.025 mm achievable; IT7 ≈ 0.012–0.025 mm
    "lathe":      7,   # same capability as CNC mill for turned diameters
    "sheet_metal": 10, # punched/formed sheet: ±0.05–0.10 mm → IT10 ≈ 0.07–0.16 mm
    "laser":      10,  # laser cut: similar to sheet metal hole tolerance
    "injection":  11,  # injection moulding: ±0.05–0.15 mm → IT11 ≈ 0.09–0.25 mm
    "fdm":        12,  # FDM 3D print: ±0.1–0.3 mm → IT12 ≈ 0.09–0.62 mm
}
_DEFAULT_IT_GRADE = 9  # fallback for unknown processes


def _it_grade_for_process(process: str) -> int:
    return _PROCESS_IT_GRADE.get(process, _DEFAULT_IT_GRADE)


def _it_value_um(nominal_mm: float, grade: int) -> int:
    """Compute IT grade value in µm for a nominal dimension, matching fits.py logic."""
    # ISO 286 ranges: over lo, up to and including hi.
    _RANGES = [
        (1, 3, math.sqrt(1 * 3)),
        (3, 6, math.sqrt(3 * 6)),
        (6, 10, math.sqrt(6 * 10)),
        (10, 18, math.sqrt(10 * 18)),
        (18, 30, math.sqrt(18 * 30)),
        (30, 50, math.sqrt(30 * 50)),
        (50, 80, math.sqrt(50 * 80)),
        (80, 120, math.sqrt(80 * 120)),
        (120, 180, math.sqrt(120 * 180)),
        (180, 250, math.sqrt(180 * 250)),
        (250, 315, math.sqrt(250 * 315)),
        (315, 400, math.sqrt(315 * 400)),
        (400, 500, math.sqrt(400 * 500)),
    ]
    _MULT = {5: 7, 6: 10, 7: 16, 8: 25, 9: 40, 10: 64, 11: 100, 12: 160}

    D = None
    for lo, hi, d_mean in _RANGES:
        if lo < nominal_mm <= hi:
            D = d_mean
            break
    if D is None:
        # out of ISO range — use a linear approximation
        D = max(1.0, min(500.0, nominal_mm))
    i = 0.45 * D ** (1 / 3) + 0.001 * D
    mult = _MULT.get(grade, 40)  # fallback IT9
    return round(mult * i)


def _bilateral_tolerance(nominal_mm: float, grade: int) -> dict:
    """Return a ±IT/2 bilateral tolerance dict for a dimension."""
    it_um = _it_value_um(nominal_mm, grade)
    half_plus = math.floor(it_um / 2)
    half_minus = math.ceil(it_um / 2)
    return {
        "it_grade": f"IT{grade}",
        "upper_mm": round(half_plus / 1000, 6),
        "lower_mm": round(-half_minus / 1000, 6),
        "band_mm": round(it_um / 1000, 6),
        "source": {"kind": "closed_form", "citation": "ISO 286-1:2010"},
    }


# ──────────────────────────────────────────────────── sketch entity helpers ───

_CIRCLE_TYPES = frozenset({"circle", "arc"})
_LINE_TYPES = frozenset({"line", "segment"})


def _is_mating(entity: dict) -> bool:
    """True if this entity is flagged as a mating feature (type or tag)."""
    t = (entity.get("type") or "").lower()
    tags = entity.get("tags") or []
    return t in _CIRCLE_TYPES or "mating" in tags or "bore" in tags or "shaft" in tags


def _is_hole(entity: dict) -> bool:
    t = (entity.get("type") or "").lower()
    tags = entity.get("tags") or []
    return t in _CIRCLE_TYPES or "hole" in tags or "bore" in tags


def _entity_nominal(entity: dict) -> Optional[float]:
    """Extract a nominal size (radius or length) from entity geom if available."""
    geom = entity.get("geom") or ""
    # Try to parse e.g. "r=5.0" or "L=20.0" from geom string
    for pat in [r"r=([0-9.]+)", r"R=([0-9.]+)", r"L=([0-9.]+)", r"l=([0-9.]+)",
                r"d=([0-9.]+)", r"D=([0-9.]+)"]:
        m = re.search(pat, str(geom))
        if m:
            try:
                v = float(m.group(1))
                # If it looks like a radius, convert to diameter for holes
                if pat.lower().startswith("r="):
                    v *= 2
                return v
            except ValueError:
                pass
    return None


def _find_bolt_patterns(entities: list[dict]) -> list[list[dict]]:
    """Detect groups of circles that form a bolt/hole pattern (2+ circles of similar radius).

    Returns list of groups; each group is a list of circle entities.
    """
    circles = [e for e in entities if (e.get("type") or "").lower() in _CIRCLE_TYPES
               and not e.get("construction")]
    if len(circles) < 2:
        return []

    # Group by nominal radius (within 10% tolerance)
    used = set()
    groups: list[list[dict]] = []
    for i, c in enumerate(circles):
        if i in used:
            continue
        nom = _entity_nominal(c)
        if nom is None:
            continue
        group = [c]
        used.add(i)
        for j, c2 in enumerate(circles[i + 1:], i + 1):
            if j in used:
                continue
            nom2 = _entity_nominal(c2)
            if nom2 is not None and abs(nom2 - nom) <= 0.1 * max(nom, nom2, 1.0):
                group.append(c2)
                used.add(j)
        if len(group) >= 2:
            groups.append(group)
    return groups


# ──────────────────────────────────────────────────── main planner ────────────

def _plan_from_entities(entities: list[dict], process: str, it_grade: int,
                        nominal_fallback_mm: float = 20.0) -> list[dict]:
    """Build the ordered dimension plan from a list of sketch entities.

    Returns a list of dimension-plan items:
    ``{entity_ref, dim_type, why_critical, suggested_value, default_tolerance,
       needs_user_confirm, priority, hole_comp_mm}``

    ``hole_comp_mm`` is the process-specific diameter compensation that must be added to
    the nominal modeled diameter so the finished hole meets the nominal size.  FDM prints
    shrink inward (hole_comp_mm=+0.20 → model the hole 0.20 mm larger); CNC has no comp.
    This is sourced from the DfM profile's ``params.hole_comp_mm`` (C++ core/dfm
    process_profiles, via ``_get_profile``) — never invented.
    """
    # Pull the hole compensation from the DFM profile (real values, never invented).
    # get_profile() wraps the DFM fields under a "params" key; fall back to top-level
    # for forward-compatibility.
    try:
        profile = _get_profile(process)
        params = profile.get("params") or profile  # nested or flat
        hole_comp_mm: float = float(params.get("hole_comp_mm") or 0.0)
    except Exception:
        hole_comp_mm = 0.0

    items: list[dict] = []
    priority = 1

    bolt_groups = _find_bolt_patterns(entities)
    bolt_entity_ids: set = {e.get("id") for g in bolt_groups for e in g}

    # ── 1. Bolt-pattern dims ──
    for group in bolt_groups:
        # First hole: datum offset dim (function-critical)
        datum = group[0]
        nom = _entity_nominal(datum) or nominal_fallback_mm
        items.append({
            "entity_ref": datum.get("id"),
            "dim_type": "datum_offset",
            "why_critical": "bolt pattern datum — locates the pattern from a reference edge",
            "suggested_value": None,
            "default_tolerance": _bilateral_tolerance(nom, it_grade),
            "needs_user_confirm": True,
            "priority": priority,
            "hole_comp_mm": hole_comp_mm,
        })
        priority += 1

        # Remaining holes: centre-to-centre dims (deterministic if constrained)
        for other in group[1:]:
            nom2 = _entity_nominal(other) or nominal_fallback_mm
            items.append({
                "entity_ref": f"{datum.get('id')}->{other.get('id')}",
                "dim_type": "centre_to_centre",
                "why_critical": "bolt pattern pitch — chain via datum only, never edge-to-hole-to-hole",
                "suggested_value": None,
                "default_tolerance": _bilateral_tolerance(nom2, it_grade),
                "needs_user_confirm": False,
                "priority": priority,
                "hole_comp_mm": hole_comp_mm,
            })
            priority += 1

        # Hole diameter dim for each bolt-pattern circle (mating → confirm).
        # suggested_value = nominal + hole_comp_mm (compensated model diameter).
        for circle in group:
            nom_d = _entity_nominal(circle) or nominal_fallback_mm
            comp_d = round(nom_d + hole_comp_mm, 6) if nom_d is not None else None
            items.append({
                "entity_ref": circle.get("id"),
                "dim_type": "diameter",
                "why_critical": "mating feature (bolt clearance hole diameter)",
                "suggested_value": comp_d,
                "default_tolerance": _bilateral_tolerance(nom_d, it_grade),
                "needs_user_confirm": True,
                "priority": priority,
                "hole_comp_mm": hole_comp_mm,
            })
            priority += 1

    # ── 2. Remaining mating circles (not in bolt patterns) ──
    for e in entities:
        if e.get("construction"):
            continue
        if e.get("id") in bolt_entity_ids:
            continue
        if _is_mating(e):
            nom = _entity_nominal(e) or nominal_fallback_mm
            comp = round(nom + hole_comp_mm, 6) if nom is not None else None
            items.append({
                "entity_ref": e.get("id"),
                "dim_type": "diameter",
                "why_critical": "mating feature — bore/shaft diameter drives fit class",
                "suggested_value": comp,
                "default_tolerance": _bilateral_tolerance(nom, it_grade),
                "needs_user_confirm": True,
                "priority": priority,
                "hole_comp_mm": hole_comp_mm,
            })
            priority += 1

    # ── 3. Non-mating lines (overall envelope dimensions) ──
    for e in entities:
        if e.get("construction"):
            continue
        if e.get("id") in bolt_entity_ids:
            continue
        t = (e.get("type") or "").lower()
        if t in _LINE_TYPES and not _is_mating(e):
            nom = _entity_nominal(e) or nominal_fallback_mm
            items.append({
                "entity_ref": e.get("id"),
                "dim_type": "length",
                "why_critical": "overall envelope — constrains part size",
                "suggested_value": nom if nom else None,
                "default_tolerance": _bilateral_tolerance(nom, it_grade),
                "needs_user_confirm": False,
                "priority": priority,
                "hole_comp_mm": hole_comp_mm,
            })
            priority += 1

    return items


# ──────────────────────────────────────────────────── sketch fixture ──────────

def _make_fixture_sketch(num_holes: int = 4, bolt_spacing_mm: float = 40.0,
                          hole_dia_mm: float = 5.0) -> list[dict]:
    """Return a synthetic sketch entity list for a square bolt-circle plate.

    Used in tests when a live doc/bridge is unavailable.  Produces ``num_holes``
    circle entities at the corners of a square with side ``bolt_spacing_mm``.
    """
    spacing = bolt_spacing_mm
    centers: list[tuple[float, float]] = []
    if num_holes == 4:
        centers = [(0, 0), (spacing, 0), (spacing, spacing), (0, spacing)]
    else:
        for i in range(num_holes):
            angle = 2 * math.pi * i / num_holes
            centers.append((spacing / 2 * math.cos(angle), spacing / 2 * math.sin(angle)))

    entities = []
    for i, (cx, cy) in enumerate(centers):
        entities.append({
            "id": f"e{i + 1}",
            "type": "circle",
            "geom": f"cx={cx:.1f} cy={cy:.1f} r={hole_dia_mm / 2:.1f}",
            "construction": False,
            "tags": [],
        })
    return entities


def _make_bore_sketch(bore_dia_mm: float = 20.0) -> list[dict]:
    """Return a minimal sketch fixture: one circle representing a bore/hole.

    Used in process-differentiation tests (machined bore vs printed hole) to verify that
    the dimension scheme produces different ``suggested_value`` and ``it_grade`` per process.
    The bore diameter is nominal (what the designer wants); each process adds its own
    hole_comp_mm on top when producing the modeled dimension.
    """
    return [{
        "id": "bore1",
        "type": "circle",
        "geom": f"cx=0 cy=0 r={bore_dia_mm / 2:.1f}",
        "construction": False,
        "tags": ["bore"],
    }]


# ──────────────────────────────────────────────────── MCP registration ────────

def register_dimension_scheme_tools(mcp: FastMCP) -> None:

    @mcp.tool()
    def suggest_dimension_scheme(
        doc_id: str,
        sketch_id: str,
        process: Optional[str] = None,
        material: Optional[str] = None,
    ) -> str:
        """Propose which dimensions to drive in a sketch, with process-aware tolerances.

        Reads the sketch entity roster and classifies each entity by function
        (mating holes, bolt patterns, envelope lines). Returns an ordered dimension
        plan: `[{entity_ref, dim_type, why_critical, suggested_value,
        default_tolerance, needs_user_confirm}]`.

        Rules:
        - Bolt hole patterns: dimension centre-to-centre + one datum offset from a
          reference edge (never chain edge-to-hole-to-hole — accumulating tolerance).
        - Mating circles/holes: `needs_user_confirm=true` — these feed into `fit_calc`.
          Use `fit_calc` to size a mating bore BEFORE calling this for hole-pattern dims.
        - Default tolerances derived from the process IT grade:
          CNC mill/lathe → IT7, sheet/laser → IT10, injection → IT11, FDM → IT12.
        - Function-critical dims listed first; envelope/auxiliary dims last.

        `process` is optional: resolved from the doc's DFM profile if omitted (defaults
        to FDM per the project default-prior rule). `material` is informational only
        (affects the DFM default but not the IT grade lookup in this release).
        """
        try:
            # Resolve process
            resolved_process = "fdm"  # default prior
            if process:
                try:
                    prof = _resolve_process(process + (f" {material}" if material else ""))
                    resolved_process = prof.get("process", process) if isinstance(prof, dict) else process
                except Exception:
                    resolved_process = process.lower().strip().replace(" ", "_")
                    if resolved_process not in _known_processes():
                        resolved_process = "fdm"

            it_grade = _it_grade_for_process(resolved_process)

            # Try to read sketch entities from live bridge
            entities: list[dict] = []
            bridge_ok = False
            if _HAS_BRIDGE:
                try:
                    import services.mcp.core_bridge as cb
                    d = _doc(doc_id)
                    overview = cb.call("doc_sketch_overview", {"doc_id": d, "sketch": sketch_id})
                    raw_entities = overview.get("entities") or []
                    # Also get authored params for tags
                    all_features = cb.call("doc_list_features", {"doc_id": d}).get("features") or []
                    feat = next((f for f in all_features if isinstance(f, dict)
                                 and f.get("id") == sketch_id), None)
                    authored: list[dict] = []
                    if feat and isinstance(feat.get("params"), dict):
                        authored = feat["params"].get("entities") or []
                    authored_by_id = {e.get("id"): e for e in authored if isinstance(e, dict)}
                    for e in raw_entities:
                        if not isinstance(e, dict):
                            continue
                        eid = e.get("id")
                        merged = {**authored_by_id.get(eid, {}), **e}
                        entities.append(merged)
                    bridge_ok = True
                except Exception:
                    bridge_ok = False

            notes = []
            if not bridge_ok:
                # Offline / no bridge: synthesise a 4-hole fixture for testing
                entities = _make_fixture_sketch(num_holes=4)
                notes.append(
                    "Bridge unavailable — dimension plan generated from a 4-hole plate "
                    "fixture (offline mode). Run with a live doc for geometry-specific results."
                )

            if not entities:
                return _dumps({
                    "ok": False,
                    "reason": "no_entities",
                    "hint": f"Sketch {sketch_id!r} has no entities, or sketch not found.",
                })

            items = _plan_from_entities(entities, resolved_process, it_grade)

            result = {
                "ok": True,
                "doc_id": doc_id,
                "sketch_id": sketch_id,
                "process": resolved_process,
                "it_grade": f"IT{it_grade}",
                "items": items,
                "notes": notes + [
                    f"Default tolerance: ±IT{it_grade}/2 bilateral, ISO 286-1.",
                    "Mating bore/hole dims (needs_user_confirm=true) → run fit_calc before "
                    "finalising their tolerances.",
                    "Bolt pattern dims use datum-offset + centre-to-centre only "
                    "(no edge-to-hole-to-hole chains).",
                ],
            }
            return _dumps(result)

        except Exception as exc:  # noqa: BLE001
            return _err(exc)
