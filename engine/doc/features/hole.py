"""Hole feature compiler — SolidWorks-style Hole Wizard (simple / cbore / csink / tap).

feature_params.md §hole.

A single hole feature drills N bores into the running body in ONE boolean — exactly
SolidWorks' "Hole Wizard" multi-position behaviour: one feature, many positions on a
target face. All position tools are unioned, then subtracted from the body in a single
cut so downstream selectors see one coherent feature.

Position model (C33-A)
----------------------
The target ``refs.face`` defines a local frame (build123d ``Plane`` on the face: origin =
face centroid, +Z = outward normal). Positions are resolved INTO that frame and each
becomes a hole axis pointing along ``-Z`` (into the material). Sources, in priority order:

1. ``refs.points`` — explicit ``[[u, v], ...]`` offsets in the face's local mm frame
   (NEW, append-only). The primary multi-position path.
2. params ``nx``/``ny`` (counts, ≥1) + ``dx``/``dy`` (pitch, mm) — a rectangular GRID of
   positions, centred on the face frame origin. "Sketch-driven holes lite": the honest,
   fully-functional form-driven path SolidWorks users recognise. Combined with ``points``
   the grid is *appended* to the explicit list.
3. ``refs.positions`` — a list of vertex selectors (contract form). Each picked vertex's
   world point is projected into the face's local frame. Vertex picks are the only viewport
   picks that carry an exact world coordinate today, so this is the click-to-place bridge.
4. Nothing of the above → ONE hole at the face-frame origin (face centre). This is the
   back-compatible single-position default.

A future wave wanting true click-anywhere-on-face placement needs the pick pipeline to
carry the surface hit point (u,v or world xyz) on FACE picks — see PickHit in the host;
today only vertex picks carry coordinates, hence path 3.

Cap: ``_MAX_POSITIONS`` (64) positions; exceeding it is a clean FeatureError.
"""

from __future__ import annotations

import math
from typing import Any

from build123d import Axis, Plane, Solid, Vector

from .base import CompileContext, CompileOutput, FeatureError, register
from engine.doc.selectors import describe_faces, resolve

_MAX_POSITIONS = 64

_INCH = 25.4  # mm per inch (exact)

# ISO metric tap-drill / clearance table (mm). Each entry carries:
#   diameter    -> CLEARANCE drill (close fit, ISO 273) — used for simple/cbore/csink bores.
#   tap_drill   -> TAP drill (≈ major − pitch) — the bore actually drilled for a `tap` hole so
#                  the thread has material to cut into (SolidWorks "Tap Drill Diameter").
#   pitch       -> thread pitch in mm (coarse series) for the cosmetic-thread callout.
#   major       -> nominal MAJOR (outside) thread diameter in mm for the cosmetic-thread callout.
#   cbore_d / cbore_depth / csink_d follow ISO 273 / common shop practice.
# "custom" uses params.diameter verbatim and gets no cosmetic-thread metadata.
_STD_TABLE: dict[str, dict[str, float]] = {
    #          clearance  tap_drill  pitch  major  cbore_d  cbore_depth  csink_d
    "M2":  {"diameter": 2.4,  "tap_drill": 1.6,  "pitch": 0.40, "major": 2.0,
            "cbore_d": 4.4,  "cbore_depth": 2.0,  "csink_d": 4.0},
    "M2.5":{"diameter": 2.9,  "tap_drill": 2.05, "pitch": 0.45, "major": 2.5,
            "cbore_d": 5.0,  "cbore_depth": 2.5,  "csink_d": 5.0},
    "M3":  {"diameter": 3.4,  "tap_drill": 2.5,  "pitch": 0.50, "major": 3.0,
            "cbore_d": 6.0,  "cbore_depth": 3.0,  "csink_d": 6.0},
    "M4":  {"diameter": 4.5,  "tap_drill": 3.3,  "pitch": 0.70, "major": 4.0,
            "cbore_d": 8.0,  "cbore_depth": 4.0,  "csink_d": 8.0},
    "M5":  {"diameter": 5.5,  "tap_drill": 4.2,  "pitch": 0.80, "major": 5.0,
            "cbore_d": 10.0, "cbore_depth": 5.0,  "csink_d": 10.0},
    "M6":  {"diameter": 6.6,  "tap_drill": 5.0,  "pitch": 1.00, "major": 6.0,
            "cbore_d": 11.0, "cbore_depth": 6.0,  "csink_d": 12.0},
    "M8":  {"diameter": 9.0,  "tap_drill": 6.8,  "pitch": 1.25, "major": 8.0,
            "cbore_d": 15.0, "cbore_depth": 8.0,  "csink_d": 16.0},
    "M10": {"diameter": 11.0, "tap_drill": 8.5,  "pitch": 1.50, "major": 10.0,
            "cbore_d": 18.0, "cbore_depth": 10.0, "csink_d": 20.0},
    "M12": {"diameter": 13.5, "tap_drill": 10.2, "pitch": 1.75, "major": 12.0,
            "cbore_d": 20.0, "cbore_depth": 12.0, "csink_d": 24.0},
}


def _inch_entry(major_in: float, tap_drill_in: float, tpi: float,
                clear_in: float, cbore_in: float, cbore_depth_in: float,
                csink_in: float) -> dict[str, float]:
    """Build one inch-thread table entry, converting all lengths to mm.

    ``tpi`` is threads-per-inch; pitch (mm) = 25.4 / tpi. ``tap_drill_in`` /
    ``clear_in`` etc. are the standard inch drill diameters (Machinery's Handbook
    75%-thread tap drills and ANSI clearance drills), stored converted to mm.
    """
    return {
        "diameter": clear_in * _INCH,
        "tap_drill": tap_drill_in * _INCH,
        "pitch": _INCH / tpi,
        "major": major_in * _INCH,
        "cbore_d": cbore_in * _INCH,
        "cbore_depth": cbore_depth_in * _INCH,
        "csink_d": csink_in * _INCH,
    }


# ANSI/ASME inch unified-thread table (UNC + UNF). Values in inches at definition, stored in mm.
# Tap drills are the standard 75%-thread drills; clearance is the "close fit" (ANSI B18.2.8) drill;
# cbore (counterbore for a socket-head cap screw, ASME B18.3) and csink (82° flat-head) diameters
# are common shop dimensions. Major diameter for numbered sizes: d = 0.060 + n*0.013 in.
_UNC_TABLE: dict[str, dict[str, float]] = {
    # ---- numbered sizes, UNC (coarse) ----
    "#0-80":  _inch_entry(0.0600, 0.0469, 80, 0.076, 0.125, 0.060, 0.110),  # #0 only UNF, listed coarse-equiv
    "#1-64":  _inch_entry(0.0730, 0.0595, 64, 0.089, 0.138, 0.073, 0.133),
    "#2-56":  _inch_entry(0.0860, 0.0700, 56, 0.102, 0.161, 0.086, 0.158),
    "#3-48":  _inch_entry(0.0990, 0.0785, 48, 0.116, 0.176, 0.099, 0.183),
    "#4-40":  _inch_entry(0.1120, 0.0890, 40, 0.128, 0.198, 0.112, 0.208),
    "#5-40":  _inch_entry(0.1250, 0.1015, 40, 0.144, 0.221, 0.125, 0.233),
    "#6-32":  _inch_entry(0.1380, 0.1065, 32, 0.154, 0.244, 0.138, 0.258),
    "#8-32":  _inch_entry(0.1640, 0.1360, 32, 0.180, 0.276, 0.164, 0.308),
    "#10-24": _inch_entry(0.1900, 0.1495, 24, 0.207, 0.312, 0.190, 0.358),
    "#12-24": _inch_entry(0.2160, 0.1770, 24, 0.234, 0.343, 0.216, 0.408),
    # ---- fractional sizes, UNC (coarse) ----
    '1/4-20':  _inch_entry(0.2500, 0.2010, 20, 0.266, 0.375, 0.250, 0.477),
    '5/16-18': _inch_entry(0.3125, 0.2570, 18, 0.328, 0.438, 0.312, 0.594),
    '3/8-16':  _inch_entry(0.3750, 0.3125, 16, 0.391, 0.562, 0.375, 0.711),
    '7/16-14': _inch_entry(0.4375, 0.3680, 14, 0.453, 0.625, 0.438, 0.828),
    '1/2-13':  _inch_entry(0.5000, 0.4219, 13, 0.516, 0.750, 0.500, 0.945),
    '9/16-12': _inch_entry(0.5625, 0.4843, 12, 0.578, 0.812, 0.562, 1.062),
    '5/8-11':  _inch_entry(0.6250, 0.5312, 11, 0.641, 0.875, 0.625, 1.180),
    '3/4-10':  _inch_entry(0.7500, 0.6562, 10, 0.766, 1.000, 0.750, 1.414),
    '7/8-9':   _inch_entry(0.8750, 0.7656, 9,  0.891, 1.125, 0.875, 1.649),
    '1-8':     _inch_entry(1.0000, 0.8750, 8,  1.016, 1.312, 1.000, 1.883),
}

# UNF (fine) inch series — same nominal majors, finer pitch → larger tap drill.
_UNF_TABLE: dict[str, dict[str, float]] = {
    "#0-80":  _inch_entry(0.0600, 0.0469, 80, 0.076, 0.125, 0.060, 0.110),
    "#1-72":  _inch_entry(0.0730, 0.0595, 72, 0.089, 0.138, 0.073, 0.133),
    "#2-64":  _inch_entry(0.0860, 0.0700, 64, 0.102, 0.161, 0.086, 0.158),
    "#3-56":  _inch_entry(0.0990, 0.0820, 56, 0.116, 0.176, 0.099, 0.183),
    "#4-48":  _inch_entry(0.1120, 0.0935, 48, 0.128, 0.198, 0.112, 0.208),
    "#5-44":  _inch_entry(0.1250, 0.1040, 44, 0.144, 0.221, 0.125, 0.233),
    "#6-40":  _inch_entry(0.1380, 0.1130, 40, 0.154, 0.244, 0.138, 0.258),
    "#8-36":  _inch_entry(0.1640, 0.1360, 36, 0.180, 0.276, 0.164, 0.308),
    "#10-32": _inch_entry(0.1900, 0.1590, 32, 0.207, 0.312, 0.190, 0.358),
    "#12-28": _inch_entry(0.2160, 0.1820, 28, 0.234, 0.343, 0.216, 0.408),
    '1/4-28':  _inch_entry(0.2500, 0.2130, 28, 0.266, 0.375, 0.250, 0.477),
    '5/16-24': _inch_entry(0.3125, 0.2720, 24, 0.328, 0.438, 0.312, 0.594),
    '3/8-24':  _inch_entry(0.3750, 0.3320, 24, 0.391, 0.562, 0.375, 0.711),
    '7/16-20': _inch_entry(0.4375, 0.3906, 20, 0.453, 0.625, 0.438, 0.828),
    '1/2-20':  _inch_entry(0.5000, 0.4531, 20, 0.516, 0.750, 0.500, 0.945),
    '9/16-18': _inch_entry(0.5625, 0.5156, 18, 0.578, 0.812, 0.562, 1.062),
    '5/8-18':  _inch_entry(0.6250, 0.5781, 18, 0.641, 0.875, 0.625, 1.180),
    '3/4-16':  _inch_entry(0.7500, 0.6875, 16, 0.766, 1.000, 0.750, 1.414),
    '7/8-14':  _inch_entry(0.8750, 0.8125, 14, 0.891, 1.125, 0.875, 1.649),
    '1-12':    _inch_entry(1.0000, 0.9219, 12, 1.016, 1.312, 1.000, 1.883),
}

# BSP (British Standard Pipe, parallel G-thread) — APPROXIMATION. Pipe-thread "sizes" name a
# nominal bore, NOT the thread major diameter, so the major/tap-drill below are the actual
# thread crest diameter and the parallel tapping drill (BS 2779), converted to mm. NPT is a
# TAPERED thread; we model it as a straight bore at its nominal tap drill (a cosmetic
# approximation — the taper is annotated but not cut). Documented as approximate in the callout.
_BSP_TABLE: dict[str, dict[str, float]] = {
    # name        major(in)  tap_drill(in)  tpi   clear   cbore  cbd    csink
    'G1/8':  _inch_entry(0.383, 0.339, 28,   0.410, 0.55, 0.20, 0.55),
    'G1/4':  _inch_entry(0.518, 0.451, 19,   0.545, 0.70, 0.26, 0.70),
    'G3/8':  _inch_entry(0.656, 0.589, 19,   0.683, 0.85, 0.26, 0.85),
    'G1/2':  _inch_entry(0.825, 0.734, 14,   0.852, 1.05, 0.36, 1.05),
    'G3/4':  _inch_entry(1.041, 0.949, 14,   1.068, 1.30, 0.36, 1.30),
    'G1':    _inch_entry(1.309, 1.193, 11,   1.336, 1.60, 0.45, 1.60),
}
_NPT_TABLE: dict[str, dict[str, float]] = {
    # NPT tapping drill (straight-bore approximation of the tapered thread).
    'NPT1/8': _inch_entry(0.405, 0.339, 27,   0.430, 0.55, 0.20, 0.55),
    'NPT1/4': _inch_entry(0.540, 0.438, 18,   0.565, 0.70, 0.26, 0.70),
    'NPT3/8': _inch_entry(0.675, 0.578, 18,   0.700, 0.85, 0.26, 0.85),
    'NPT1/2': _inch_entry(0.840, 0.703, 14,   0.865, 1.05, 0.36, 1.05),
    'NPT3/4': _inch_entry(1.050, 0.922, 14,   1.075, 1.30, 0.36, 1.30),
    'NPT1':   _inch_entry(1.315, 1.156, 11.5, 1.340, 1.60, 0.45, 1.60),
}

# Merge every inch series into the lookup. Metric ISO entries stay first; inch/pipe entries are
# additive and never shadow a metric key (disjoint names), so existing ISO behaviour is unchanged.
for _tbl in (_UNC_TABLE, _UNF_TABLE, _BSP_TABLE, _NPT_TABLE):
    for _k, _v in _tbl.items():
        _STD_TABLE.setdefault(_k, _v)

# Pipe-thread families whose cosmetic-thread callout is an acknowledged approximation.
_APPROX_THREAD_KEYS = set(_BSP_TABLE) | set(_NPT_TABLE)

_VALID_STYLES = ("simple", "cbore", "csink", "tap")
_VALID_ENDS = ("through_all", "blind")


# --------------------------------------------------------------------------- helpers


def _body_bbox_diag(body) -> float:
    try:
        bb = body.bounding_box()
        dx = bb.max.X - bb.min.X
        dy = bb.max.Y - bb.min.Y
        dz = bb.max.Z - bb.min.Z
        return max(math.sqrt(dx * dx + dy * dy + dz * dz), 1e-3)
    except Exception:  # noqa: BLE001
        return 100.0


def _resolve_face(refs: dict[str, Any], ctx: CompileContext, body) -> Any:
    """Resolve ``refs.face`` to a build123d Face (or raise dangling FeatureError).

    Accepts a face selector (preferred) or a datum-plane id; a datum plane resolves to a
    synthetic Face is NOT supported here — a planar face on the body is the working case.
    """
    sel = refs.get("face")
    if sel is None:
        raise FeatureError(
            "hole: refs.face is required (the planar face the holes are drilled into)"
        )
    if not isinstance(sel, dict):
        raise FeatureError("hole: refs.face must be a face selector")

    infos = describe_faces(body.wrapped)
    diag = _body_bbox_diag(body)
    rr = resolve(sel, infos, diag)
    if rr.index is None:
        raise FeatureError(
            f"hole: face selector dangling — {rr.dangling_reason}",
            dangling_refs=[f"face: {rr.dangling_reason}"],
        )
    return body.faces()[rr.index]


def _face_plane(face) -> Plane:
    """Build a local frame on *face*: origin = centroid, +Z = outward normal."""
    try:
        return Plane(face)
    except Exception:  # noqa: BLE001
        # Fall back to centroid + normal_at for non-trivial faces.
        c = face.center()
        try:
            n = face.normal_at()
        except Exception:  # noqa: BLE001
            n = Vector(0, 0, 1)
        return Plane(origin=c, z_dir=n)


def _collect_positions(
    refs: dict[str, Any], p: dict[str, Any], ctx: CompileContext, plane: Plane,
) -> list[tuple[float, float]]:
    """Gather all (u, v) local-frame positions from points / grid / vertices.

    Empty result → caller drills a single hole at the frame origin (0, 0).
    """
    out: list[tuple[float, float]] = []

    # 1. explicit [u, v] points
    raw_pts = refs.get("points")
    if isinstance(raw_pts, list):
        for i, pt in enumerate(raw_pts):
            if not (isinstance(pt, (list, tuple)) and len(pt) >= 2):
                raise FeatureError(f"hole: refs.points[{i}] must be [u, v]")
            try:
                out.append((float(pt[0]), float(pt[1])))
            except (TypeError, ValueError):
                raise FeatureError(f"hole: refs.points[{i}] is not numeric")

    # 2. rectangular grid (nx × ny at dx, dy pitch), centred on the frame origin
    nx = int(p.get("nx", 1) or 1)
    ny = int(p.get("ny", 1) or 1)
    if nx < 1 or ny < 1:
        raise FeatureError("hole: nx and ny must be >= 1")
    if nx > 1 or ny > 1:
        dx = ctx.dim(p, "dx", 20.0)
        dy = ctx.dim(p, "dy", 20.0)
        x0 = -(nx - 1) * dx / 2.0
        y0 = -(ny - 1) * dy / 2.0
        for iy in range(ny):
            for ix in range(nx):
                out.append((x0 + ix * dx, y0 + iy * dy))

    # 3. vertex selectors → world point projected into the face frame
    positions = refs.get("positions")
    if isinstance(positions, list):
        for i, vsel in enumerate(positions):
            world = _vertex_world(vsel)
            if world is None:
                raise FeatureError(
                    f"hole: refs.positions[{i}] could not yield a point",
                    dangling_refs=[f"positions[{i}]: no centroid on vertex selector"],
                )
            loc = plane.to_local_coords(Vector(*world))
            out.append((float(loc.X), float(loc.Y)))

    # 4. sketch-point positions → each POINT entity of the referenced sketch becomes one hole.
    #    The sketch's stored point coords are SKETCH-LOCAL (u,v on the sketch plane); they are
    #    lifted to world via the sketch plane, then projected into THIS face's frame — so the holes
    #    land where the sketch points are in space, even when the sketch plane and the target face
    #    differ. SolidWorks "Hole Wizard on a sketch's points".
    out.extend(_sketch_point_positions(refs, ctx, plane))

    return out


def _sketch_point_positions(
    refs: dict[str, Any], ctx: CompileContext, plane: Plane,
) -> list[tuple[float, float]]:
    """Resolve ``refs.positions_sketch`` to its POINT entities, projected into the face frame.

    The ref slot is ``positions_sketch`` (the schema-allocated ``selector_or_datum`` slot for
    hole sketch positions); a plain feature-id string (``"f2"``) or a selector dict carrying the
    id under ``feature`` / ``sketch`` / ``id`` is accepted. ``sketch`` is also read as a direct
    alias for callers that drive the compiler without the schema layer. Only ``point`` entities
    are used (one hole each); construction points are skipped (reference geometry, not drill
    sites). Returns [] when no sketch ref is present. Raises a dangling FeatureError if the ref
    names an uncompiled sketch or the sketch has no usable points.
    """
    sref = refs.get("positions_sketch")
    if sref is None:
        sref = refs.get("sketch")  # non-schema direct alias
    if sref is None:
        return []

    sketch_id: str | None = None
    if isinstance(sref, str):
        sketch_id = sref
    elif isinstance(sref, dict):
        sketch_id = sref.get("sketch") or sref.get("feature") or sref.get("id")
    if not sketch_id:
        raise FeatureError(
            "hole: refs.sketch must be a sketch id or a selector with a 'sketch' field"
        )

    store = ctx.datums.get("_sketch_regions")
    sdata = store.obj.get(sketch_id) if store is not None else None
    if sdata is None:
        raise FeatureError(
            f"hole: refs.sketch '{sketch_id}' is not a compiled sketch (wrong feature order?)",
            dangling_refs=[f"sketch: '{sketch_id}' not found / not yet compiled"],
        )

    sketch_plane: Plane = sdata.get("plane")
    entities = sdata.get("entities") or []

    coords: list[tuple[float, float]] = []
    for ent in entities:
        if not isinstance(ent, dict) or ent.get("type") != "point":
            continue
        if ent.get("construction", False):
            continue
        pt = ent.get("p")
        if not (isinstance(pt, (list, tuple)) and len(pt) >= 2):
            continue
        try:
            su, sv = float(pt[0]), float(pt[1])
        except (TypeError, ValueError):
            continue
        # sketch-local (u,v) -> world (via the sketch plane) -> this face's local frame.
        if sketch_plane is not None:
            world = sketch_plane.from_local_coords((su, sv, 0.0))
        else:
            world = Vector(su, sv, 0.0)
        loc = plane.to_local_coords(Vector(world.X, world.Y, world.Z))
        coords.append((float(loc.X), float(loc.Y)))

    if not coords:
        raise FeatureError(
            f"hole: refs.sketch '{sketch_id}' has no point entities to drill",
            hint="add sketch points (one hole is drilled per point)",
        )
    return coords


def _vertex_world(vsel: Any) -> tuple[float, float, float] | None:
    """Extract a world point from a vertex selector (its stamped centroid)."""
    if isinstance(vsel, dict):
        c = vsel.get("centroid") or vsel.get("point")
        if isinstance(c, (list, tuple)) and len(c) == 3:
            try:
                return (float(c[0]), float(c[1]), float(c[2]))
            except (TypeError, ValueError):
                return None
    return None


def _resolve_dims(style: str, p: dict, ctx: CompileContext) -> dict[str, float]:
    """Resolve diameter / cbore / csink dims from the standard table + custom overrides.

    Bore-diameter rule by style:
      * simple / cbore / csink → CLEARANCE drill (table ``diameter``).
      * tap                    → TAP drill (table ``tap_drill``, ≈ major − pitch) so the thread
                                 has material to cut; falls back to the clearance value if the
                                 standard predates the tap-drill columns (older metric rows had
                                 only a clearance ``diameter``). SolidWorks drills the tap-drill
                                 size for a tapped hole; this matches it.
    An explicit ``params.diameter`` always wins (custom override) for any style.
    """
    standard = p.get("standard", "M3")
    table = _STD_TABLE.get(standard, {})

    # The table value this style bores by default (before any explicit override).
    if style == "tap":
        table_default = table.get("tap_drill", table.get("diameter", 3.4))
    else:
        table_default = table.get("diameter", 3.4)

    if standard == "custom":
        diameter = ctx.dim(p, "diameter", 3.4)
    else:
        # custom param.diameter overrides the table only when explicitly provided.
        diameter = ctx.dim(p, "diameter", table_default)
        if standard in _STD_TABLE and "diameter" not in p:
            diameter = table_default

    if diameter <= 0.0:
        raise FeatureError("hole: diameter must be > 0")

    dims: dict[str, float] = {"diameter": diameter}

    if style == "cbore":
        dims["cbore_d"] = (
            ctx.dim(p, "cbore_d", table.get("cbore_d", diameter * 1.8))
            if ("cbore_d" in p or "cbore_d" not in table)
            else table["cbore_d"]
        )
        dims["cbore_depth"] = (
            ctx.dim(p, "cbore_depth", table.get("cbore_depth", diameter))
            if ("cbore_depth" in p or "cbore_depth" not in table)
            else table["cbore_depth"]
        )
        if dims["cbore_d"] <= diameter:
            raise FeatureError("hole: cbore_d must be larger than diameter")
    elif style == "csink":
        dims["csink_d"] = (
            ctx.dim(p, "csink_d", table.get("csink_d", diameter * 2.0))
            if ("csink_d" in p or "csink_d" not in table)
            else table["csink_d"]
        )
        dims["csink_angle"] = ctx.dim(p, "csink_angle", 90.0)
        if dims["csink_d"] <= diameter:
            raise FeatureError("hole: csink_d must be larger than diameter")

    # Thread metadata for the cosmetic-thread callout. Present when the table row carries thread
    # data (every ISO/UNC/UNF/BSP/NPT row does) — consumed only when a cosmetic thread is emitted
    # (tap style or explicit params.cosmetic). pitch defaults from the table; an explicit
    # params.pitch overrides (custom-thread support). major defaults to the table's nominal major,
    # else the clearance diameter as a stand-in.
    if "pitch" in table or "major" in table:
        dims["thread_pitch"] = ctx.dim(p, "pitch", table.get("pitch", 0.0))
        dims["thread_major"] = ctx.dim(p, "thread_major", table.get("major", diameter))

    return dims


def _is_inch_standard(standard: str) -> bool:
    """True if *standard* is an inch/pipe thread (UNC/UNF/BSP/NPT), else (ISO metric/custom)."""
    return standard in _UNC_TABLE or standard in _UNF_TABLE \
        or standard in _BSP_TABLE or standard in _NPT_TABLE


def _build_cosmetic_thread(standard: str, dims: dict[str, float], depth: float,
                           through: bool) -> dict[str, Any]:
    """Structured cosmetic-thread record stamped on a tapped/threaded cylindrical bore.

    SolidWorks renders a cosmetic thread as a dashed circle on the cylindrical face and calls it
    out by designation in the BOM / hole table; we don't cut the helix (clean parametric geometry),
    we record the call-out so a drawing/BOM can reproduce it. Fields:
      designation  -> e.g. "1/4-20 UNC", "M6x1.0", "G1/2" (the human-readable thread call-out).
      major_dia    -> nominal MAJOR (crest) diameter in mm.
      minor_dia    -> tap-drill / bore diameter in mm (the dashed circle SW draws is at ~major).
      pitch        -> thread pitch in mm.
      depth        -> threaded length in mm along the bore axis.
      through      -> True if the thread runs the full thickness (through-all).
      class        -> "metric" | "unified" | "pipe".
      approx       -> True for pipe threads (BSP/NPT) whose geometry is approximated.
    """
    major = float(dims.get("thread_major", dims["diameter"]))
    pitch = float(dims.get("thread_pitch", 0.0))
    if standard in _BSP_TABLE or standard in _NPT_TABLE:
        cls = "pipe"
        designation = standard
    elif _is_inch_standard(standard):
        cls = "unified"
        series = "UNF" if standard in _UNF_TABLE else "UNC"
        designation = f"{standard} {series}"
    elif standard.startswith("M"):
        cls = "metric"
        designation = f"{standard}x{pitch:g}" if pitch else standard
    else:
        cls = "custom"
        designation = standard
    return {
        "designation": designation,
        "major_dia": major,
        "minor_dia": float(dims["diameter"]),
        "pitch": pitch,
        "depth": float(depth),
        "through": bool(through),
        "class": cls,
        "approx": standard in _APPROX_THREAD_KEYS,
    }


def _make_hole_tool(plane: Plane, uv: tuple[float, float], depth: float,
                    style: str, dims: dict[str, float]) -> Solid:
    """Build one hole tool (cylinder + optional cbore/csink) at local (u, v), drilling -Z.

    The shaft cylinder is over-driven slightly above the face (lift) so a flush bore cuts
    cleanly through the surface; depth is measured into the material from the face.
    """
    lift = 0.05  # mm above the face so the boolean clears the surface coplanarity
    origin = plane.from_local_coords((uv[0], uv[1], lift))
    down = -plane.z_dir
    tool_plane = Plane(origin=origin, z_dir=down)

    r = dims["diameter"] / 2.0
    shaft = Solid.make_cylinder(r, depth + lift, tool_plane)
    tool = shaft

    if style == "cbore":
        cb_r = dims["cbore_d"] / 2.0
        cb_h = dims["cbore_depth"] + lift
        cbore = Solid.make_cylinder(cb_r, cb_h, tool_plane)
        tool = tool + cbore
    elif style == "csink":
        cs_r = dims["csink_d"] / 2.0
        ang = dims.get("csink_angle", 90.0)
        # Cone depth so the top radius is cs_r at a half-angle of ang/2.
        half = math.radians(ang / 2.0)
        cone_h = cs_r / max(math.tan(half), 1e-6)
        # Cone tip points into material: top (at face) radius cs_r, tip radius 0.
        cone = Solid.make_cone(cs_r, 0.0, cone_h + lift, tool_plane)
        tool = tool + cone

    return tool


# --------------------------------------------------------------------------- compiler


@register("hole")
def compile_hole(feat: dict, ctx: CompileContext) -> CompileOutput:
    p = feat.get("params", {})
    refs = feat.get("refs", {})

    style = p.get("style", "simple")
    end = p.get("end", "through_all")
    if style not in _VALID_STYLES:
        raise FeatureError(f"hole: unknown style '{style}'")
    if end not in _VALID_ENDS:
        raise FeatureError(f"hole: unknown end '{end}'")

    bodies = list(ctx.bodies)
    if not bodies:
        raise FeatureError("hole: no body to drill into")
    body = bodies[-1]

    face = _resolve_face(refs, ctx, body)
    plane = _face_plane(face)

    positions = _collect_positions(refs, p, ctx, plane)
    if not positions:
        positions = [(0.0, 0.0)]  # back-compat single hole at face centre

    if len(positions) > _MAX_POSITIONS:
        raise FeatureError(
            f"hole: {len(positions)} positions exceeds the cap of {_MAX_POSITIONS}",
            hint="split into multiple hole features or reduce the grid count",
        )

    dims = _resolve_dims(style, p, ctx)

    # Depth: through_all = 1.05× body extent along the drill direction; blind = depth.
    # ``material_extent`` is the true wall thickness along the drill axis (the over-drive 1.05×
    # is only for the boolean tool) — used as the cosmetic thread's threaded length for a
    # through-all tap.
    drill_dir = -plane.z_dir
    try:
        bb = body.bounding_box()
        corners = [Vector(bb.min.X, bb.min.Y, bb.min.Z),
                   Vector(bb.max.X, bb.max.Y, bb.max.Z)]
        material_extent = abs(drill_dir.dot(corners[1]) - drill_dir.dot(corners[0]))
    except Exception:  # noqa: BLE001
        material_extent = _body_bbox_diag(body)
    if end == "through_all":
        depth = max(material_extent, _body_bbox_diag(body)) * 1.05
        thread_depth = material_extent
    else:
        depth = ctx.dim(p, "depth", 10.0)
        if depth <= 0.0:
            raise FeatureError("hole: blind depth must be > 0")
        thread_depth = depth

    # Build every position's tool, union into one tool body (one feature, N holes).
    tools: list[Solid] = []
    for uv in positions:
        try:
            tools.append(_make_hole_tool(plane, uv, depth, style, dims))
        except Exception as exc:  # noqa: BLE001
            raise FeatureError(f"hole: failed to build tool at {uv}: {exc}")

    tool_solid = tools[0]
    for t in tools[1:]:
        tool_solid = tool_solid + t

    target = body
    try:
        result = target - tool_solid
    except Exception as exc:  # noqa: BLE001
        raise FeatureError(
            f"hole: boolean cut failed: {exc}",
            hint="check that the holes intersect the target face",
        )

    # Normalise to a single solid where possible.
    try:
        solids = result.solids()
        if len(solids) == 1:
            result = solids[0]
    except Exception:  # noqa: BLE001
        pass

    old_vol = getattr(target, "volume", 0.0)
    new_vol = getattr(result, "volume", 0.0)
    if abs(new_vol - old_vol) < 1e-9:
        raise FeatureError(
            "hole removed no material",
            hint="the positions may fall outside the face / body",
        )

    resolved: dict[str, float] = {
        "diameter": dims["diameter"],
        "depth": depth,
        "_hole_count": float(len(positions)),
    }
    for k in ("cbore_d", "cbore_depth", "csink_d", "csink_angle"):
        if k in dims:
            resolved[k] = dims[k]

    # ----- cosmetic thread annotation -----------------------------------------
    # A cosmetic thread is emitted when the hole is tapped (style == "tap") or when the caller
    # explicitly requests one (params.cosmetic truthy — e.g. a clearance hole that will be
    # threaded). It is NOT cut as a helix (clean parametric geometry); it is a structured call-out
    # the BOM / drawing / hole-table reproduces (SolidWorks draws a dashed circle on the bore).
    standard = p.get("standard", "M3")
    want_thread = (style == "tap") or bool(p.get("cosmetic", False))
    has_thread_data = ("thread_pitch" in dims) or ("thread_major" in dims) or standard == "custom"
    if want_thread and has_thread_data:
        cosmetic = _build_cosmetic_thread(standard, dims, thread_depth, end == "through_all")
        # Numeric fields echoed into feature_status.resolved (floats only, per the contract).
        resolved["cosmetic_thread"] = 1.0
        resolved["thread_major_dia"] = cosmetic["major_dia"]
        resolved["thread_minor_dia"] = cosmetic["minor_dia"]
        resolved["thread_pitch"] = cosmetic["pitch"]
        resolved["thread_depth"] = cosmetic["depth"]

        # Rich record stashed on a sentinel Datum side-channel (the same pattern weldments /
        # sheet-metal use): obj is {feature_id: [thread_record, ...]}. One record per drilled
        # position so a BOM can call out every threaded bore. The host/drawing reads this; the
        # compiler core leaves the sentinel untouched (key is created on demand, no schema change).
        from .base import Datum
        _THREAD_KEY = "_cosmetic_threads"
        if _THREAD_KEY not in ctx.datums:
            ctx.datums[_THREAD_KEY] = Datum(kind="_cosmetic_threads", obj={}, feature_id="__sys__")
        # Stamp the face-frame (u, v) of each threaded bore onto its own record copy.
        records: list[dict[str, Any]] = []
        for uv in positions:
            rec = dict(cosmetic)
            rec["at_uv"] = [float(uv[0]), float(uv[1])]
            records.append(rec)
        ctx.datums[_THREAD_KEY].obj[feat.get("id", "?")] = records
        # Also attach to the resulting body object so an in-process consumer can read it directly
        # without the datum lookup (best-effort; ignored if the shape rejects attribute set).
        try:
            existing = list(getattr(result, "cosmetic_threads", []) or [])
            existing.extend(records)
            result.cosmetic_threads = existing
        except Exception:  # noqa: BLE001
            pass

    bodies[-1] = result
    return CompileOutput(bodies=bodies, tool_body=tool_solid, op="cut", resolved=resolved)
