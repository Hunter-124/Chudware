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

from services.mcp.cad_tools import _dumps, _err, _doc

# DfM process data (profiles, resolver, known-process list) now lives in the C++ core
# (core/dfm/process_profiles.cpp); reached via core_bridge, lazily, so this module still
# imports + runs its offline fixture mode with no engine present.
def _get_profile(process: str) -> dict:
    """dfm_get_profile RPC.

    Raises ``ValueError`` naming the requested process and listing the known ones on
    the oracle's unknown-process envelope OR on an ``ok: true`` envelope that carries
    no profile. It must never hand back an empty/None profile: callers read
    ``params.hole_comp_mm`` off it, and a silent blank there becomes a wrong modeled
    diameter (an FDM hole 0.20 mm undersize) or an AttributeError several frames away.
    """
    from services.mcp import core_bridge as cb
    r = cb.call("dfm_get_profile", {"process": process}) or {}
    if not r.get("ok"):
        raise ValueError(r.get("error") or
                         f"dfm_get_profile failed for process {process!r}. "
                         f"Known processes: {_known_processes()}.")
    profile = r.get("result")
    if not profile:
        raise ValueError(f"dfm_get_profile returned an empty DfM profile for process "
                         f"{process!r}. Known processes: {_known_processes()}.")
    return profile


def _resolve_process(request: str) -> dict:
    """dfm_resolve_process RPC (the FDM-default resolver + ambiguity gate)."""
    from services.mcp import core_bridge as cb
    r = cb.call("dfm_resolve_process", {"request": request})
    if not r.get("ok"):
        raise ValueError(r.get("error") or "dfm_resolve_process failed")
    return r.get("result") or {}


def _known_processes() -> list:
    """dfm_known_processes RPC; falls back to the static list when no bridge is up."""
    try:
        from services.mcp import core_bridge as cb
        r = cb.call("dfm_known_processes", {})
        if r.get("ok") and isinstance(r.get("result"), list):
            return r["result"]
    except Exception:  # noqa: BLE001
        pass
    return ["fdm", "cnc_mill", "lathe", "sheet_metal", "laser", "injection"]

def _process_verdict(process: Any, tool: str) -> tuple[Optional[str], Optional[dict]]:
    """Require an explicit canonical manufacturing process; never fall back to FDM."""
    known = sorted({str(item) for item in _known_processes()})
    if process is None or not str(process).strip():
        return None, {
            "ok": False,
            "reason": "process_required",
            "error": f"{tool}: process is required; refusing to assume FDM for a drawing or "
                     "machined feature.",
            "valid_processes": known,
        }
    canonical = re.sub(r"[\s\-]+", "_", str(process).strip().lower())
    if canonical not in known:
        return None, {
            "ok": False,
            "reason": "unknown_process",
            "error": f"{tool}: unknown process {process!r}; choose one of "
                     + ", ".join(known) + ".",
            "valid_processes": known,
        }
    return canonical, None


def _profile_params(process: str) -> dict | None:
    """The DfM params dict for ``process``, or ``None`` when there is no engine bridge.

    Two failure modes that used to look identical are now separated:

    * **No bridge at all** — this module's documented offline mode (fixture sketches, no
      engine). Returns ``None``; callers plan with zero compensation.
    * **Bridge up, profile unresolvable** — a real lookup gap. Propagates the teaching
      ``ValueError`` from :func:`_get_profile` naming the process and the known list,
      instead of quietly planning an uncompensated hole.
    """
    try:
        from services.mcp import core_bridge as cb
    except Exception:  # noqa: BLE001 — no engine package: offline mode
        return None
    try:
        up = bool(cb.available())
    except Exception:  # noqa: BLE001
        up = False
    if not up:
        return None
    profile = _get_profile(process)
    return profile.get("params") or profile

try:
    from services.mcp.cad_tools import _CB as _cb_module  # noqa: F401 — presence check
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
    # _profile_params() nests the DFM fields under "params" and falls back to top-level
    # for forward-compatibility. It returns None ONLY in offline mode (no engine bridge);
    # an unresolvable process on a live bridge raises the teaching ValueError rather than
    # silently planning an uncompensated hole.
    params = _profile_params(process)
    hole_comp_mm: float = float((params or {}).get("hole_comp_mm") or 0.0)

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

def _feature_value(params: Any, *names: str) -> Any:
    if not isinstance(params, dict):
        return None
    for name in names:
        value = params.get(name)
        if isinstance(value, dict) and "value" in value:
            value = value["value"]
        if value is not None:
            return value
    return None


def _feature_number(params: Any, *names: str) -> Optional[float]:
    value = _feature_value(params, *names)
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _drawing_plan_from_features(features: list[dict], process: str,
                                it_grade: int) -> list[dict]:
    """Translate authored feature semantics into drawing-dimension/callout proposals."""
    items: list[dict] = []

    def add_dimension(feature: dict, source_param: str, nominal: Optional[float],
                      dim_type: str, why: str) -> None:
        if nominal is None or nominal <= 0:
            return
        items.append({
            "feature_ref": feature.get("id") or feature.get("name"),
            "feature_name": feature.get("name"),
            "feature_type": str(feature.get("type") or "").lower(),
            "source_param": source_param,
            "annotation_kind": "dimension_tolerance",
            "suggested_tool": "add_dimension",
            "dim_type": dim_type,
            "suggested_value": nominal,
            "default_tolerance": _bilateral_tolerance(nominal, it_grade),
            "why_critical": why,
            "needs_user_confirm": True,
        })

    def add_angle(feature: dict, source_param: str, nominal: Optional[float],
                  why: str) -> None:
        if nominal is None or nominal <= 0:
            return
        items.append({
            "feature_ref": feature.get("id") or feature.get("name"),
            "feature_name": feature.get("name"),
            "feature_type": str(feature.get("type") or "").lower(),
            "source_param": source_param,
            "annotation_kind": "dimension_tolerance",
            "suggested_tool": "add_dimension",
            "dim_type": "angular_dim",
            "suggested_value": nominal,
            "default_tolerance": None,
            "why_critical": why,
            "needs_user_confirm": True,
            "tolerance_note": "ISO 286 IT grades are linear; choose an angular tolerance from "
                              "function/process capability rather than converting IT blindly.",
        })

    for feature in features:
        if not isinstance(feature, dict):
            continue
        kind = re.sub(r"[\s\-]+", "_", str(feature.get("type") or "").lower())
        params = feature.get("params") if isinstance(feature.get("params"), dict) else {}
        if kind in {"box", "block", "primitive_box"}:
            for key in ("length", "width", "height"):
                add_dimension(feature, key, _feature_number(params, key),
                              "linear_dim", f"{key} controls the feature envelope")
        elif kind in {"cylinder", "primitive_cylinder"}:
            diameter = _feature_number(params, "diameter", "dia")
            if diameter is None:
                radius = _feature_number(params, "radius", "r")
                diameter = 2.0 * radius if radius is not None else None
            add_dimension(feature, "diameter", diameter, "diameter_dim",
                          "cylindrical size controls mating/stock geometry")
            add_dimension(feature, "height", _feature_number(params, "height", "length"),
                          "linear_dim", "cylinder axial extent controls the feature")
        elif kind == "hole":
            style = re.sub(r"[\s\-]+", "_",
                           str(_feature_value(params, "style", "hole_type") or "simple").lower())
            thread = _feature_value(params, "thread", "tap", "standard", "thread_size")
            end = re.sub(r"[\s\-]+", "_",
                         str(_feature_value(params, "end", "end_condition") or "").lower())
            if style in {"tap", "tapped"} or thread:
                items.append({
                    "feature_ref": feature.get("id") or feature.get("name"),
                    "feature_name": feature.get("name"),
                    "feature_type": "hole",
                    "annotation_kind": "thread",
                    "suggested_tool": "add_hole_callout",
                    "thread": str(thread) if thread else None,
                    "depth": _feature_number(params, "depth"),
                    "thru": end in {"through", "through_all", "thru"},
                    "why_critical": "finished thread and engagement replace the tap-drill diameter",
                    "needs_user_confirm": not bool(thread),
                })
            else:
                add_dimension(feature, "diameter",
                              _feature_number(params, "diameter", "dia", "d"),
                              "diameter_dim", "finished hole size controls fit/fastener clearance")
                depth = _feature_number(params, "depth")
                if depth is not None:
                    add_dimension(feature, "depth", depth, "linear_dim",
                                  "blind-hole depth controls remaining wall and engagement")
            for key, names, dim_type, why in (
                ("cbore_dia", ("cbore_dia", "cbore_d"), "diameter_dim",
                 "counterbore diameter controls fastener-head fit"),
                ("cbore_depth", ("cbore_depth",), "linear_dim",
                 "counterbore depth controls head seating"),
                ("csk_dia", ("csk_dia", "csink_d"), "diameter_dim",
                 "countersink major diameter controls head seating"),
            ):
                add_dimension(feature, key, _feature_number(params, *names), dim_type, why)
            add_angle(feature, "csk_angle",
                      _feature_number(params, "csk_angle", "csink_angle"),
                      "countersink angle must match the fastener")
        elif kind in {"extrude", "pad", "pocket", "cut"}:
            add_dimension(feature, "depth", _feature_number(params, "depth", "length"),
                          "linear_dim", "feature depth controls the resulting step")
        elif kind in {"fillet", "round"}:
            add_dimension(feature, "radius", _feature_number(params, "radius", "r"),
                          "radial_dim", "edge radius controls stress concentration and tool path")
        elif kind in {"chamfer", "bevel"}:
            add_dimension(feature, "distance",
                          _feature_number(params, "distance", "size", "length"),
                          "chamfer", "chamfer size controls edge condition/lead-in")
            add_angle(feature, "angle", _feature_number(params, "angle"),
                      "chamfer angle controls mating lead-in")
        elif kind == "thread":
            items.append({
                "feature_ref": feature.get("id") or feature.get("name"),
                "feature_name": feature.get("name"),
                "feature_type": kind,
                "annotation_kind": "thread",
                "suggested_tool": "add_hole_callout",
                "thread": _feature_value(params, "size", "thread", "designation"),
                "depth": _feature_number(params, "length", "depth"),
                "why_critical": "thread designation and engagement are manufacturing controls",
                "needs_user_confirm": not bool(_feature_value(
                    params, "size", "thread", "designation")),
            })
        elif "pattern" in kind:
            add_dimension(feature, "spacing",
                          _feature_number(params, "spacing", "pitch", "distance"),
                          "linear_dim", "pattern spacing controls feature locations from a datum")
            add_dimension(feature, "radius",
                          _feature_number(params, "radius", "pattern_radius"),
                          "radial_dim", "pattern radius controls the bolt circle")
    return items


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
        Apply the selected critical dimensions with sketch_add_constraint (driven:false),
        then sketch_fully_define for residual freedom; verify check_sketch dof 0 and
        not pinned_only. This tool proposes a plan; it does not mutate the sketch.

        `process` is REQUIRED. Omitted or unknown values are refused with the valid process
        list; the planner never silently assumes FDM for a machined feature. ``material`` is
        informational only.
        """
        try:
            resolved_process, bad = _process_verdict(process, "suggest_dimension_scheme")
            if bad:
                return _dumps(bad)
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

    @mcp.tool()
    def suggest_drawing_dimension_scheme(doc_id: str, process: str) -> str:
        """Propose per-feature drawing dimensions, tolerances and semantic callouts.

        ``process`` is REQUIRED and must be one of the returned valid_processes; there is no
        FDM default.  The plan reads authored feature parameters from the live document and
        proposes add_dimension/add_hole_callout work without placing annotations.
        """
        try:
            resolved_process, bad = _process_verdict(
                process, "suggest_drawing_dimension_scheme")
            if bad:
                return _dumps(bad)
            import services.mcp.core_bridge as cb
            d = _doc(doc_id)
            result = cb.call("doc_list_features", {"doc_id": d})
            features = result.get("features") or []
            if not features:
                return _dumps({
                    "ok": False,
                    "reason": "no_features",
                    "doc_id": d,
                    "error": "suggest_drawing_dimension_scheme: the document has no authored "
                             "features to plan from.",
                })
            it_grade = _it_grade_for_process(resolved_process)
            items = _drawing_plan_from_features(features, resolved_process, it_grade)
            if not items:
                return _dumps({
                    "ok": False,
                    "reason": "no_semantic_dimensions",
                    "doc_id": d,
                    "process": resolved_process,
                    "error": "suggest_drawing_dimension_scheme: no supported dimensional "
                             "feature semantics were found; author the controlling dimensions "
                             "explicitly rather than using envelope defaults.",
                })
            return _dumps({
                "ok": True,
                "doc_id": d,
                "process": resolved_process,
                "it_grade": f"IT{it_grade}",
                "items": items,
                "notes": [
                    f"Size tolerance proposals use ±IT{it_grade}/2 bilateral, ISO 286-1.",
                    "Every needs_user_confirm row must be reconciled with fits, function and "
                    "the governing spec before placing it on sheet.",
                    "Thread rows intentionally call for add_hole_callout, not tap-drill diameter.",
                ],
            })
        except Exception as exc:  # noqa: BLE001
            return _err(exc)
