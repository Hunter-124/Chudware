"""CAD document-lifecycle MCP tools (split out of cad_tools.py): new_part, add_features, get_part, undo/redo, save/close/recover, configurations, material, engine_profile, cad_guide."""
from __future__ import annotations

from mcp.server.fastmcp import FastMCP  # noqa: F401 — register signature
from typing import Annotated
from typing import Any
from typing import Optional
from typing import Union

from pydantic import Field  # noqa: TC002 — runtime-read by FastMCP's schema builder
import difflib
import json
import math
import os
import re
import time
import zipfile
from pathlib import Path
from uuid import uuid4

from .cad_common import (
    _CUR,
    _DOC_ALIASES,
    _PROCESS_DOCS,
    _JOBS,
    _JOB_LOCK,
    _DURABLE_JOB_PREFIX,
    _LAST_DELIVER_REV,
    _LAST_VOLUME,
    _TOUCHED_DOCS,
    _SID_RE,
    _autosave,
    _default_deliverable_path,
    _doc,
    _doc_new_scope,
    _doc_revision,
    _docs_in_version_series,
    _docs_named,
    _dumps,
    _err,
    _face_hist,
    _pick_best_doc,
    _publish_current,
    _project_save,
    _resolve_alias,
    _resumable_docs,
    _resolve_appearance,
    _material_candidates,
    _material_norm,
    _material_not_modelled,
    _sanitize_output_path,
    _sid_match,
    durable_job_snapshot,
    _solve_sync_or_handoff,
    _start_job,
    _strip_version_suffix,
    _unpublish,
    _version_series_base,
    _write_diff,
    is_geom_query,
    normalize_geom_query,
    note_discarded_doc,
    note_harness_saved_path,
    note_touched_doc,
    placement_warnings,
    pose_to_transform16,
    cb,
    recover_autosaved,
    retire_harness_copy,
    recover_skipped,
    user_doc_open_at,
)

from .cad_guide import (
    cad_guide_text,
)

# ---------------------------------------------------------------------------
# Sketch-op shape normalizer (model-robustness).
#
# The engine's sketch schema is strict: entities under params with e<N> ids and
# p1/p2 point fields, constraints with c<N> ids + `refs` handles (eN.pM), the
# sketch plane under refs.plane. LLMs (esp. smaller ones) very naturally emit a
# flatter form instead — L1/L2 ids, start/end keys, a `constraints` list keyed on
# `entities`, a separate `dimensions` array, and a top-level `plane:"XY"`. That
# form is REJECTED wholesale by validate.cpp (invalid_op), which surfaces as a red
# "add_features failed" card mid-build. Normalize the common deviations into the
# canonical shape BEFORE doc_apply_ops so a natural sketch just works. Conservative:
# only rewrites a feature that shows the deviant form; canonical ops pass through
# untouched (byte-identical), and any failure falls back to the original op.
# The engine's RESERVED DATUMS (core/doc/validate.cpp::RESERVED_DATUMS) are the only bare
# strings a datum/plane/axis ref may carry besides an f<N> feature id — everything else is
# rejected as "'XZ' is neither a reserved datum nor a feature id". Models write the plane
# the way a person names it ("XZ", "front", "Top Plane"), so map every natural spelling onto
# the reserved token here, before the call.
_DATUM_TOKENS = frozenset({
    "origin", "point.origin",
    "plane.front", "plane.top", "plane.right",
    "axis.x", "axis.y", "axis.z",
})
_PLANE_ALIASES = {
    "xy": "plane.top", "yx": "plane.top", "top": "plane.top", "topplane": "plane.top",
    "plane.xy": "plane.top", "plane_xy": "plane.top", "top_plane": "plane.top",
    "xz": "plane.front", "zx": "plane.front", "front": "plane.front",
    "frontplane": "plane.front", "plane.xz": "plane.front", "plane_xz": "plane.front",
    "front_plane": "plane.front",
    "yz": "plane.right", "zy": "plane.right", "right": "plane.right",
    "rightplane": "plane.right", "plane.yz": "plane.right", "plane_yz": "plane.right",
    "right_plane": "plane.right", "side": "plane.right",
}
_AXIS_DATUM_ALIASES = {
    "x": "axis.x", "+x": "axis.x", "-x": "axis.x", "xaxis": "axis.x", "x_axis": "axis.x",
    "axis_x": "axis.x", "axis.x": "axis.x",
    "y": "axis.y", "+y": "axis.y", "-y": "axis.y", "yaxis": "axis.y", "y_axis": "axis.y",
    "axis_y": "axis.y", "axis.y": "axis.y",
    "z": "axis.z", "+z": "axis.z", "-z": "axis.z", "zaxis": "axis.z", "z_axis": "axis.z",
    "axis_z": "axis.z", "axis.z": "axis.z",
}
_DATUM_WORDS = {**_PLANE_ALIASES, **_AXIS_DATUM_ALIASES}
_CID_RE = re.compile(r"^c\d+$")
_EID_RE = re.compile(r"^e\d+$")
_FID_RE = re.compile(r"^f\d+$")
_BATCH_REF_RE = re.compile(r"^\$\d+$")
_DIM_CTYPES = frozenset({"distance", "length", "angle", "radius", "diameter", "arc_length"})


def _xy(v: Any) -> Any:
    """Coerce a coordinate to the engine's 2D [x, y]; pass anything else through.

    Models write a point three ways: the canonical ``[x, y]``, and the object forms
    ``{"x":20,"y":8}`` / ``{"u":20,"v":8}``. The object forms USED to pass straight
    through to the engine, which reads a center only from an array — so
    ``{"type":"polygon","center":{"x":20,"y":8"},…}`` silently became a polygon at the
    ORIGIN. Seven hexagons stacked on top of each other instead of a pattern (live
    2026-07-28 kimi-keychain run). Silent wrong geometry is worse than a rejection, so
    the object forms are coerced here, at the one place every entity normalizes points.
    """
    if isinstance(v, list) and len(v) >= 2 and all(isinstance(x, (int, float)) for x in v[:2]):
        return [v[0], v[1]]
    if isinstance(v, dict):
        for xk, yk in (("x", "y"), ("u", "v")):
            p = _flat_point(v, xk, yk)
            if p is not None:
                return p
    return v


#: Flat radius-shaped keys a model plausibly writes instead of the canonical `radius`.
_RADIUS_KEYS = ("r", "radius")
#: Flat diameter-shaped keys — halved into the canonical radius.
_DIAMETER_KEYS = ("d", "dia", "diameter")


def _flat_point(e: dict, xk: str, yk: str) -> Optional[list]:
    """[x, y] from two flat numeric keys, or None.

    Only BOTH present resolves it: one flat coordinate with its twin missing is
    half-specified, not unambiguous, so it is left for the engine's own error.
    """
    x, y = e.get(xk), e.get(yk)
    if isinstance(x, (int, float)) and not isinstance(x, bool) \
            and isinstance(y, (int, float)) and not isinstance(y, bool):
        return [x, y]
    return None


def _flat_radius(e: dict) -> tuple[Optional[float], bool]:
    """(radius, was_diameter) for a circle/arc entity that names its size flat.

    r/radius is the radius itself; d/dia/diameter is a DIAMETER and is halved. Both a
    radius-shaped and a diameter-shaped key present is only resolved when they agree
    (r == d/2) — disagreeing values are genuinely ambiguous and left for the engine,
    never guessed away.
    """
    def _num(k: str) -> Optional[float]:
        v = e.get(k)
        return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None

    r_val = next((v for k in _RADIUS_KEYS if (v := _num(k)) is not None), None)
    d_val = next((v for k in _DIAMETER_KEYS if (v := _num(k)) is not None), None)
    if r_val is not None and d_val is not None:
        return (r_val, False) if abs(r_val * 2 - d_val) < 1e-9 else (None, False)
    if r_val is not None:
        return r_val, False
    if d_val is not None:
        return d_val / 2.0, True
    return None, False


#: Spellings of the three-point ("through a point on the arc") arc the engine has no
#: separate type for. It is the SolidWorks/Fusion 3-point arc, so models reach for it
#: constantly; expanding it here is arithmetic on a fully specified shape, not a guess.
_ARC3_TYPES = frozenset({"arc3", "arc_3p", "arc3p", "3parc", "3p_arc",
                         "three_point_arc", "threepointarc", "point_arc"})
#: Mid-point ("through") spellings for that form.
_ARC_MID_KEYS = ("mid", "midpoint", "mid_point", "pm", "via", "through",
                 "through_point", "point_on_arc", "on_arc")
#: Endpoint spellings. p1/p2 come from the 3D sketch arc, from/to from the line dialect.
_ARC_START_KEYS = ("start", "start_point", "startpoint", "p1", "from", "a")
_ARC_END_KEYS = ("end", "end_point", "endpoint", "p2", "to", "b")
#: Angle spellings for the CANONICAL form (degrees about the centre).
_ARC_START_ANGLE_KEYS = ("start_angle", "startangle", "a0", "theta0", "theta1", "ang0")
_ARC_END_ANGLE_KEYS = ("end_angle", "endangle", "a1", "theta2", "ang1")
#: Sweep-direction words. The engine carries the direction in a bool `ccw`.
_ARC_CCW_WORDS = {"ccw": True, "counterclockwise": True, "counter_clockwise": True,
                  "anticlockwise": True, "anti_clockwise": True, "left": True,
                  "cw": False, "clockwise": False, "right": False}
_ARC_SWEEP_KEYS = ("sweep", "direction", "dir", "orientation", "winding")


def _pt2(v: Any) -> Optional[tuple[float, float]]:
    """(x, y) when *v* is a 2D numeric point in any accepted spelling, else None."""
    p = _xy(v)
    if isinstance(p, list) and len(p) >= 2:
        x, y = p[0], p[1]
        if isinstance(x, (int, float)) and not isinstance(x, bool) \
                and isinstance(y, (int, float)) and not isinstance(y, bool):
            return float(x), float(y)
    return None


def _first_key(e: dict, keys: tuple) -> Optional[str]:
    return next((k for k in keys if k in e), None)


def _arc_angle(center: tuple, p: tuple) -> float:
    return math.degrees(math.atan2(p[1] - center[1], p[0] - center[0])) % 360.0


def _normalize_arc_entity(ne: dict, label: str, sink: set) -> None:
    """Rewrite the arc dialects models actually write into the engine's canonical form.

    Canonical (validate.cpp): center + radius + start/end ANGLES IN DEGREES + ccw.
    Accepted here, because each is a complete and unambiguous specification:
      * endpoints as POINTS plus the centre  — the endpoint/centre arc every CAD UI draws;
      * endpoints plus a non-zero bulge (tan(signed sweep/4));
      * three points (start, a point on the arc, end) — the 3-point arc, incl. `arc3`;
      * start_angle / end_angle spellings of the canonical angles.
    Anything under-specified or self-contradictory raises a teaching ValueError naming
    the values received rather than silently building a different arc.
    """
    # Direction word -> the engine's bool, whichever dialect named it.
    if "ccw" not in ne or isinstance(ne.get("ccw"), str):
        word = ne.get("ccw") if isinstance(ne.get("ccw"), str) else None
        if word is None:
            sk = _first_key(ne, _ARC_SWEEP_KEYS)
            word = ne.get(sk) if isinstance(ne.get(sk), str) else None
            if word is not None:
                ne.pop(sk, None)
        if isinstance(word, str):
            flag = _ARC_CCW_WORDS.get(word.strip().lower().replace(" ", "_"))
            if flag is None:
                raise ValueError(
                    f"{label}: arc sweep {word!r} is neither clockwise nor "
                    f"counter-clockwise. Accepted: "
                    f"{', '.join(sorted(_ARC_CCW_WORDS))}.")
            ne["ccw"] = flag
            sink.add("arc_sweep_word")

    # Angle spellings of the canonical form.
    for keys, canon in ((_ARC_START_ANGLE_KEYS, "start"), (_ARC_END_ANGLE_KEYS, "end")):
        k = _first_key(ne, keys)
        if k is None:
            continue
        v = ne[k]
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            if isinstance(ne.get(canon), (int, float)) and not isinstance(
                    ne.get(canon), bool) and float(ne[canon]) != float(v):
                raise ValueError(
                    f"{label}: arc {canon}={ne[canon]!r} conflicts with {k}={v!r}.")
            ne[canon] = float(v)
            ne.pop(k, None)
            sink.add("arc_angle_key")

    s_key = _first_key(ne, _ARC_START_KEYS)
    e_key = _first_key(ne, _ARC_END_KEYS)
    s_pt = _pt2(ne.get(s_key)) if s_key else None
    e_pt = _pt2(ne.get(e_key)) if e_key else None
    m_key = _first_key(ne, _ARC_MID_KEYS)
    m_pt = _pt2(ne.get(m_key)) if m_key else None
    has_bulge = "bulge" in ne
    if s_pt is None and e_pt is None:
        if has_bulge:
            raise ValueError(
                f"{label}: arc bulge={ne.get('bulge')!r} needs point endpoints "
                f'from=[x,y] and to=[x,y] (or start/end). Canonical arcs use '
                f"center/radius/start/end angles.")
        return  # already angular (or nothing to work with) — engine reports the rest
    if s_pt is None or e_pt is None:
        raise ValueError(
            f"{label}: an arc given point endpoints needs BOTH of them; got "
            f"{s_key}={ne.get(s_key)!r}, {e_key}={ne.get(e_key)!r}.")

    center = _pt2(ne.get("center"))
    if has_bulge and m_pt is not None:
        raise ValueError(
            f"{label}: arc gives both bulge={ne.get('bulge')!r} and {m_key}="
            f"{ne.get(m_key)!r}. Use from/to plus bulge, OR start/mid/end.")
    if has_bulge:
        # A bulge is tan(signed sweep/4), the standard polyline-arc spelling. Endpoints
        # plus any non-zero bulge determine one circle and direction exactly. The live
        # 2026-08-04 IWB-holster run used this complete form; dropping it left an open
        # profile that could only fail later with "profile sketch has no closed face".
        raw_bulge = ne["bulge"]
        if not isinstance(raw_bulge, (int, float)) or isinstance(raw_bulge, bool):
            raise ValueError(
                f"{label}: arc bulge={raw_bulge!r} must be a non-zero number "
                f"(tan(signed sweep/4)); or use center=[x,y] with point endpoints.")
        bulge = float(raw_bulge)
        if bulge == 0.0:
            raise ValueError(
                f"{label}: arc bulge=0 describes a straight line. Use "
                f'{{"type":"line","p1":[x,y],"p2":[x,y]}} or a non-zero bulge.')
        dx, dy = e_pt[0] - s_pt[0], e_pt[1] - s_pt[1]
        chord = math.hypot(dx, dy)
        if chord == 0.0:
            raise ValueError(
                f"{label}: arc bulge endpoints are identical at {list(s_pt)}; "
                f"from/to must be distinct points.")
        offset = chord * (1.0 - bulge * bulge) / (4.0 * bulge)
        implied = ((s_pt[0] + e_pt[0]) / 2.0 - dy / chord * offset,
                   (s_pt[1] + e_pt[1]) / 2.0 + dx / chord * offset)
        if center is not None and math.dist(center, implied) > 1e-6 * max(
                1.0, math.dist(s_pt, implied)):
            raise ValueError(
                f"{label}: arc center={list(center)} disagrees with bulge={bulge}, "
                f"which implies {[round(v, 6) for v in implied]}. Give from/to plus "
                f"bulge, OR the centre, not both.")
        implied_ccw = bulge > 0.0
        if isinstance(ne.get("ccw"), bool) and ne["ccw"] != implied_ccw:
            raise ValueError(
                f"{label}: arc ccw={ne['ccw']} conflicts with bulge={bulge}; "
                f"{'positive' if implied_ccw else 'negative'} bulge means "
                f"{'counter-clockwise' if implied_ccw else 'clockwise'}.")
        center = implied
        ne["ccw"] = implied_ccw
        ne.pop("bulge")
        sink.add("arc_bulge")
    elif m_pt is not None:
        # Circumcentre of the three points.
        ax, ay = s_pt
        bx, by = m_pt
        cx_, cy_ = e_pt
        d = 2.0 * (ax * (by - cy_) + bx * (cy_ - ay) + cx_ * (ay - by))
        if abs(d) < 1e-9:
            raise ValueError(
                f"{label}: the three arc points {s_pt}, {m_pt}, {e_pt} are collinear, "
                f"so they define a straight line rather than an arc. Use a line, or "
                f"move the middle point off the chord.")
        ux = ((ax * ax + ay * ay) * (by - cy_) + (bx * bx + by * by) * (cy_ - ay)
              + (cx_ * cx_ + cy_ * cy_) * (ay - by)) / d
        uy = ((ax * ax + ay * ay) * (cx_ - bx) + (bx * bx + by * by) * (ax - cx_)
              + (cx_ * cx_ + cy_ * cy_) * (bx - ax)) / d
        computed = (ux, uy)
        if center is not None and math.dist(center, computed) > 1e-6 * max(
                1.0, math.dist(s_pt, computed)):
            raise ValueError(
                f"{label}: arc center={list(center)} disagrees with the centre implied "
                f"by start/mid/end ({[round(v, 6) for v in computed]}). Give the three "
                f"points OR the centre, not both.")
        center = computed
        # Traversal order start -> mid -> end fixes the sweep direction.
        ne["ccw"] = ((bx - ax) * (cy_ - ay) - (by - ay) * (cx_ - ax)) > 0
        ne.pop(m_key, None)
        sink.add("arc_three_point")
    elif center is None:
        raise ValueError(
            f"{label}: an arc through {list(s_pt)} and {list(e_pt)} needs a "
            f"center=[x,y], a third point on the arc (mid=[x,y]), or a non-zero "
            f"bulge — two endpoints alone describe infinitely many arcs.")

    r_start = math.dist(center, s_pt)
    r_end = math.dist(center, e_pt)
    if abs(r_start - r_end) > max(1e-6, 1e-4 * max(r_start, r_end)):
        raise ValueError(
            f"{label}: arc endpoints are not the same distance from the centre "
            f"({r_start:.6g} mm vs {r_end:.6g} mm), so they do not lie on one arc.")
    radius = (r_start + r_end) / 2.0
    declared = _flat_radius(ne)[0] if "radius" not in ne else ne.get("radius")
    if isinstance(declared, (int, float)) and not isinstance(declared, bool) \
            and abs(float(declared) - radius) > max(1e-6, 1e-4 * radius):
        raise ValueError(
            f"{label}: arc radius={declared!r} disagrees with the endpoints, which are "
            f"{radius:.6g} mm from the centre.")
    ne["center"] = [center[0], center[1]]
    ne["radius"] = radius
    ne["start"] = _arc_angle(center, s_pt)
    ne["end"] = _arc_angle(center, e_pt)
    for k in (s_key, e_key):
        if k not in ("start", "end"):
            ne.pop(k, None)
    for k in _RADIUS_KEYS + _DIAMETER_KEYS:
        if k != "radius":
            ne.pop(k, None)
    ne.setdefault("ccw", True)
    sink.add("arc_endpoints")


def _norm_datum(v: Any) -> Optional[str]:
    """A plane/axis word -> the engine's reserved datum token. Non-strings -> None.

    An already-canonical token, an f<N> feature id and a same-batch "$N" placeholder are
    returned untouched; anything the map does not know is handed back verbatim so the
    engine (not this function) reports it.
    """
    if not isinstance(v, str) or not v.strip():
        return None
    s = v.strip()
    if s in _DATUM_TOKENS or _FID_RE.match(s) or _BATCH_REF_RE.match(s):
        return s
    return _DATUM_WORDS.get(s.lower().replace(" ", "").replace("-", "_"), s)


def _is_canon_datum(v: Any) -> bool:
    """True when a bare-string ref would satisfy validate_sel_or_datum as-is."""
    return isinstance(v, str) and (v in _DATUM_TOKENS or bool(_FID_RE.match(v))
                                   or bool(_BATCH_REF_RE.match(v)))


def _norm_dim_value(c: dict) -> dict:
    """Wrap a bare numeric dimension value in the engine's {value, expr} literal."""
    if c.get("type") in _DIM_CTYPES:
        val = c.get("value")
        if isinstance(val, (int, float)) and not isinstance(val, bool):
            c["value"] = {"value": val, "expr": None}
        elif isinstance(val, dict) and "value" in val and "expr" not in val:
            c["value"] = {"value": val["value"], "expr": val.get("expr")}
        c.setdefault("driven", False)
    return c


# A POLYLINE is how a model draws a revolve/extrude profile in one breath
# ({"type":"polyline","closed":true,"points":[[x,y],…]}) and the engine has no such
# entity type, so the whole batch died on «params.entities[0].type: not a sketch entity
# type» (live run 2026-07-27, "mudbucket"). Unlike `rectangle`/`polygon` — which
# session.cpp already expands from a size — a polyline carries its own coordinates, so
# turning it into the N `line` entities it names needs no geometry guessing at all.
_POLYLINE_TYPES = frozenset({"polyline", "poly_line", "polyline_closed"})


def _polyline_points(ent: dict) -> list:
    """The polyline's [x,y] points, consecutive duplicates dropped. [] = not expandable."""
    raw = ent.get("points")
    if not isinstance(raw, list):
        return []
    pts: list = []
    for p in raw:
        q = _xy(p)
        if not (isinstance(q, list) and len(q) == 2 and _is_num(q[0]) and _is_num(q[1])):
            return []
        if not pts or pts[-1] != q:
            pts.append(q)
    return pts


def _expand_polyline(ent: dict, start_id: int) -> list:
    """A polyline entity -> the `line` entities it means, ids e<start_id+1…>.

    `closed` adds the segment back to the first point (a repeated closing point is the
    same segment, so it is dropped first). [] when the shape is not unambiguously a
    polyline — a non-[x,y] point, or fewer than two distinct points — so the engine's
    own error still teaches instead of this inventing a profile.
    """
    pts = _polyline_points(ent)
    closed = bool(ent.get("closed", ent.get("close", False)))
    if closed and len(pts) > 1 and pts[0] == pts[-1]:
        pts = pts[:-1]
    if len(pts) < 2:
        return []
    constr = bool(ent.get("construction", False))
    segs = [(pts[i], pts[i + 1]) for i in range(len(pts) - 1)]
    if closed and len(pts) > 2:
        segs.append((pts[-1], pts[0]))
    return [{"id": f"e{start_id + i + 1}", "type": "line", "construction": constr,
             "p1": list(a), "p2": list(b)} for i, (a, b) in enumerate(segs)]


#: What a model calls the field naming a sketch entity's shape. The engine reads only
#: `type`; a live run (2026-07-28) sent a perfectly valid six-line hexagon as
#: {"kind":"line","p1":…,"p2":…} and every entity was rejected as "not a sketch entity
#: type" — a whole sketch lost to one synonym.
_ENTITY_TYPE_ALIASES = ("kind", "shape", "entity", "entity_type", "etype", "geom_type")

#: Sketch entity shapes the ENGINE can build from two opposite corners
#: (core/doc/session.cpp ~1403: `et == "rectangle" || et == "rect"`). It reads those
#: corners ONLY as arrays — corner1/corner2, p1/p2 or min/max — so the flat
#: coordinate dialect the `line` branch already understands (x1/y1/x2/y2) lands one
#: slot over and dies on "rectangle: expected center-form … or two-corner form".
#: Live run 2026-07-29 (lexus_f_keychain): all three regions of an embossed "F" were
#: written {"kind":"rect","x1":10,"y1":-6,"x2":16,"y2":12} and the whole sketch was
#: rejected — the model had to guess the canonical spelling on a second call.
_TWO_CORNER_TYPES = frozenset({"rectangle", "rect"})

#: Width/height spellings the engine's center-form accepts (session.cpp ~1440).
_RECT_W_KEYS = ("width", "w", "dx", "length")
_RECT_H_KEYS = ("height", "h", "dy", "depth")


def _flat_corners(e: dict) -> Optional[tuple[list, list]]:
    """(corner1, corner2) for a rectangle that names its corners flat, else None.

    Declines when either two-corner slot is already filled, and when the entity
    carries a width AND a height — that payload already satisfies the engine's
    center-form, so rewriting it would change a working call.
    """
    if any(k in e for k in ("corner1", "p1", "min")) and any(
            k in e for k in ("corner2", "p2", "max")):
        return None
    if any(k in e for k in _RECT_W_KEYS) and any(k in e for k in _RECT_H_KEYS):
        return None
    c1 = _flat_point(e, "x1", "y1")
    c2 = _flat_point(e, "x2", "y2")
    if c1 is None or c2 is None:
        return None
    return c1, c2


def _entity_type_alias(e: dict) -> Optional[str]:
    """The alias key carrying this entity's shape word, when `type` is missing."""
    if isinstance(e.get("type"), str) and e["type"].strip():
        return None
    for k in _ENTITY_TYPE_ALIASES:
        v = e.get(k)
        if isinstance(v, str) and v.strip():
            return k
    return None


# ---------------------------------------------------------------------------
# RELATION / DIMENSION dialect (model-robustness).
#
# validate.cpp::CONSTRAINT_SPECS is a CLOSED 20-word vocabulary and anything outside it
# kills the WHOLE batch («"perp" is not a constraint type»), taking the sketch and every
# feature that referenced it with it. Measured against the live engine 2026-07-30 over 40
# realistic relation payloads and 29 dimension payloads: 22 relations and 17 dimensions
# were rejected, and every single one was a pure SPELLING miss on a relation the solver
# already implements. Map the spellings a modeler actually writes onto the engine's.
#
# A value is either the canonical token, or (token, extra-fields) when the dialect word
# carries MEANING the canonical token needs a field to express — "horizontal_distance" is
# the engine's `distance` with axis:"h" (validate.cpp:1155), not a distinct relation.
_CONSTRAINT_TYPE_ALIASES: dict[str, Any] = {
    "coincidence": "coincident", "coinc": "coincident", "coincide": "coincident",
    "point_coincident": "coincident", "merge_points": "coincident", "connect": "coincident",
    "horiz": "horizontal", "hor": "horizontal", "horizontal_relation": "horizontal",
    "vert": "vertical", "vertical_relation": "vertical",
    "par": "parallel", "parallel_to": "parallel", "parallelism": "parallel",
    "perp": "perpendicular", "perpendicular_to": "perpendicular", "normal": "perpendicular",
    "orthogonal": "perpendicular", "perpendicularity": "perpendicular",
    "tangent_to": "tangent", "tangency": "tangent",
    "equal_length": "equal", "equal_radius": "equal", "same_length": "equal",
    "same_radius": "equal", "equals": "equal", "equality": "equal",
    "coaxial": "concentric", "concentric_with": "concentric", "same_center": "concentric",
    "colinear": "collinear", "co_linear": "collinear", "collinearity": "collinear",
    "mid": "midpoint", "midpoint_of": "midpoint", "mid_point": "midpoint",
    "symmetric_about": "symmetric", "symmetry": "symmetric", "mirror": "symmetric",
    "fixed": "fix", "lock": "fix", "locked": "fix", "anchor": "fix", "pin": "fix",
    "ground": "fix", "grounded": "fix",
    "point_on_line": "point_on", "point_on_entity": "point_on", "point_on_curve": "point_on",
    "on_line": "point_on", "on_curve": "point_on", "on_entity": "point_on",
    "rad": "radius", "radial": "radius", "radius_dimension": "radius",
    "radial_dimension": "radius", "radial_dim": "radius",
    "dia": "diameter", "diametral": "diameter", "diameter_dimension": "diameter",
    "dia_dimension": "diameter",
    "angular": "angle", "angle_dimension": "angle", "angular_dimension": "angle",
    "length_dimension": "length", "line_length": "length",
    "distance_between": "distance", "linear_dimension": "distance",
    "linear_dim": "distance", "distance_dimension": "distance",
    "horizontal_distance": ("distance", {"axis": "h"}),
    "horizontal_dimension": ("distance", {"axis": "h"}),
    "distance_x": ("distance", {"axis": "h"}), "dist_x": ("distance", {"axis": "h"}),
    "dx_distance": ("distance", {"axis": "h"}), "dim_horizontal": ("distance", {"axis": "h"}),
    "vertical_distance": ("distance", {"axis": "v"}),
    "vertical_dimension": ("distance", {"axis": "v"}),
    "distance_y": ("distance", {"axis": "v"}), "dist_y": ("distance", {"axis": "v"}),
    "dy_distance": ("distance", {"axis": "v"}), "dim_vertical": ("distance", {"axis": "v"}),
}

#: Type words that name "put a dimension here" without saying WHICH — SolidWorks' one
#: Smart Dimension button. The engine has no such type, so the concrete one is resolved
#: from the refs (a lone circle/arc -> radius, a lone line -> length, two refs ->
#: distance) and the model is told which it got.
_GENERIC_DIM_TYPES = frozenset({
    "dimension", "dim", "smart_dimension", "smartdimension", "measure", "size"})

#: Constraint ref-slot spellings, in the order the engine's `refs` list wants them.
#: `entities`/`handles` were already understood; the PAIRED forms below are what a model
#: reaches for when it thinks of a relation as "between A and B" rather than as a list,
#: and every one of them landed on «refs: coincident takes 2..2 refs» (live probe).
_CONSTRAINT_REF_PAIRS = (
    ("a", "b"), ("first", "second"), ("entity1", "entity2"), ("entity_1", "entity_2"),
    ("e1", "e2"), ("ref1", "ref2"), ("r1", "r2"), ("from", "to"),
)
#: Single-slot ref spellings for the 1-ref relations (fix/horizontal/radius/…).
_CONSTRAINT_REF_SINGLES = ("entity", "ref", "target", "handle", "on", "of")
#: List-shaped ref spellings beyond the two the normalizer already read.
_CONSTRAINT_REF_LISTS = ("refs", "entities", "handles", "elements", "items", "targets")
#: How many refs each relation needs (validate.cpp::CONSTRAINT_SPECS lower bound). Used
#: only to decide whether a relation is still SHORT after the first ref slot resolved, so
#: a payload that spread its two refs over two dialects is completed rather than lost.
_CONSTRAINT_ARITY = {
    "coincident": 2, "parallel": 2, "perpendicular": 2, "tangent": 2, "collinear": 2,
    "equal": 2, "concentric": 2, "coradial": 2, "midpoint": 2, "point_on": 2, "angle": 2,
    "symmetric": 3, "horizontal": 1, "vertical": 1, "fix": 1, "length": 1,
    "radius": 1, "diameter": 1, "distance": 2, "arc_length": 1,
    "intersection": 3, "smooth": 2, "equal_curve_length": 2,
}

#: What a model calls the axis a `distance` dimension projects onto. The engine takes only
#: 'h'/'v' (validate.cpp:1155) and rejects everything else, so the world-axis and prose
#: spellings are mapped on. Purely a rename: 'x' IS the horizontal projection here.
_DIM_AXIS_WORDS = {
    "x": "h", "+x": "h", "h": "h", "horizontal": "h", "horiz": "h", "u": "h", "dx": "h",
    "y": "v", "+y": "v", "v": "v", "vertical": "v", "vert": "v", "dy": "v",
}

#: Where a `symmetric` relation's MIRROR LINE hides when it is not the third ref. The
#: engine wants refs=[a, b, axis]; a model that writes the axis in its own slot loses the
#: relation to «refs: symmetric takes 3..3 refs». Read only for `symmetric` — `axis` on a
#: `distance` is the h/v projection selector and means something else entirely.
_SYMMETRY_AXIS_KEYS = ("axis", "about", "line", "mirror_line", "symmetry_line", "centerline")

#: Where a dimension's NUMBER hides when it is not under `value`. A model naming the
#: number after the quantity ({"type":"radius","radius":7}) got «value: required for
#: radius» and lost the batch.
_DIM_VALUE_KEYS = ("value", "distance", "length", "radius", "diameter", "angle",
                   "dim", "amount", "measure", "val", "size", "d")

#: Truthy under these and the dimension is a REFERENCE (driven) dimension: it reports the
#: geometry instead of forcing it. The engine spells that `driven`, and used to IGNORE
#: every other spelling in silence — the single worst outcome measured this session. Live
#: probe 2026-07-30: {"type":"length","refs":["e1"],"value":80,"reference":true} returned
#: ok and STRETCHED e1 from 40.1 mm to 80 mm. The model asked for a measurement and got
#: geometry it never requested, with nothing in the response saying so.
_DRIVEN_TRUE_KEYS = ("driven", "reference", "is_reference", "reference_dimension",
                     "readonly", "read_only")
#: …and the inverse spelling: driving:true means driven:false.
_DRIVING_KEYS = ("driving", "is_driving")

#: Point-handle suffix spellings. The engine's HANDLE_RE takes e<N> optionally followed by
#: one of its OWN sub-names (validate.cpp ENTITY_SUBS); everything else is «not a valid
#: handle», which names no alternative.
_HANDLE_SUB_ALIASES = {
    "centre": "center", "centerpoint": "center", "center_point": "center", "c": "center",
    "1": "p1", "2": "p2", "a": "p1", "b": "p2",
    "p": "p1", "point1": "p1", "point2": "p2", "pt1": "p1", "pt2": "p2",
    "startpoint": "start", "start_point": "start", "endpoint": "end", "end_point": "end",
}
#: A `line`/`slot` names its ends p1/p2, so .start/.end on one must become them (the
#: engine already accepts .start/.end on an arc, where they ARE the sub-names).
_LINE_END_SUBS = {"start": "p1", "end": "p2"}
#: The modeler's own entity alias ("l0"/"l5", session.cpp::sketch_entity_aliases) and the
#: case-shifted canonical id. `c<N>` is deliberately absent: that is a CONSTRAINT id.
_MODELER_EID_RE = re.compile(r"^[lLeE](\d+)$")
_DEG_NUM_RE = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*(?:deg|degs|degree|degrees|°)\s*$",
                         re.IGNORECASE)


def _dim_number(v: Any, angular: bool) -> Optional[float]:
    """The plain number a dimension carries, or None when it is not unambiguously one.

    Accepts the bare number, the canonical {"value":N,"expr":null} wrapper, a mm-unit
    literal ("50mm" -> 50) and — for an ANGLE only — a degree literal ("90deg" -> 90).
    A real expression string ("width/2") is left alone: the engine evaluates those.
    """
    if _is_num(v):
        return float(v)
    if isinstance(v, dict) and not v.get("expr") and _is_num(v.get("value")):
        return float(v["value"])
    if isinstance(v, str):
        if angular:
            m = _DEG_NUM_RE.match(v)
            if m:
                return float(m.group(1))
        mm = _dim_mm(v)
        if mm is not None:
            return float(mm)
    return None


def _norm_handle(h: Any, etypes: dict[str, str]) -> Any:
    """One constraint ref -> the handle spelling validate.cpp's HANDLE_RE accepts.

    `etypes` maps entity id -> type for THIS sketch, which is what makes `.start` on a
    line resolvable to `.p1`. Anything not recognisably a handle is returned untouched so
    the caller (not this function) decides whether to teach or defer.
    """
    if not isinstance(h, str) or not h.strip():
        return h
    s = h.strip().replace(":", ".")
    base, _, sub = s.partition(".")
    m = _MODELER_EID_RE.match(base)
    if m:
        # "L1"/"E1"/"l1" — the modeler's own entity alias, and the case-shifted canonical
        # id. Both fail HANDLE_RE, which is case-sensitive on a lowercase `e`.
        base = "e" + m.group(1)
    if sub:
        low = sub.lower()
        sub = _HANDLE_SUB_ALIASES.get(low, low)
        if etypes.get(base) in ("line", "slot"):
            sub = _LINE_END_SUBS.get(sub, sub)
    return base + ("." + sub if sub else "")


def _constraint_dialect_keys(c: dict) -> bool:
    """True when a constraint carries any spelling the normalizer must rewrite.

    Read by _sketch_needs_norm so a sketch whose ENTITIES are already canonical still
    reaches the constraint pass — without this, {"type":"perp"} beside canonical geometry
    slipped past normalization entirely and died in validate.
    """
    if not isinstance(c, dict):
        return False
    ctype = _slug(c.get("type"))
    if ctype in _CONSTRAINT_TYPE_ALIASES or ctype in _GENERIC_DIM_TYPES:
        return True
    if any(k in c for k in _DRIVEN_TRUE_KEYS[1:]) or any(k in c for k in _DRIVING_KEYS):
        return True
    if not isinstance(c.get("refs"), list):
        return True
    if ctype in _DIM_CTYPES and not _is_dim_object(c.get("value")):
        return True
    return False


def _natural_key(s: Any) -> tuple:
    """e2 < e10: an id roster listed lexicographically (e10, e11, e2…) reads as scrambled."""
    m = re.fullmatch(r"(.*?)(\d+)", s) if isinstance(s, str) else None
    return (m.group(1), int(m.group(2))) if m else (str(s), -1)


def _id_assignment_note(renamed: Optional[dict], expanded: Optional[dict]) -> str:
    """How this sketch's entity ids were actually assigned, for a dangling-ref refusal."""
    parts = ["an e<N> id you gave is kept; any other id takes the next free e<N> "
             "in list order"]
    moved = sorted(((k, v) for k, v in (renamed or {}).items() if k != v),
                   key=lambda kv: _natural_key(kv[1]))
    if moved:
        parts.append(", ".join(f'your "{k}" is {v}' for k, v in moved))
    for pid, segs in (expanded or {}).items():
        span = segs[0] if len(segs) == 1 else f"{segs[0]}..{segs[-1]}"
        parts.append(f'polyline "{pid}" became lines {span}')
    return "; ".join(parts)


def _normalize_one_constraint(c: dict, remap, etypes: Optional[dict], label: str,
                              rewrites: set, expanded: Optional[dict] = None,
                              renamed: Optional[dict] = None) -> dict:
    """One relation/dimension -> the engine's canonical constraint object.

    `remap` rewrites an AUTHORED entity id onto the e<N> the entity pass minted; `etypes`
    is the resulting id -> type map, which is what lets `.start` on a line resolve to
    `.p1` and what makes a dangling ref detectable HERE, where the valid ids can be named.
    Rewrites are accumulated in `rewrites` so a 30-constraint sketch emits one note per
    KIND of rewrite, not one per constraint.
    Raises ValueError for a ref that names nothing — a dangling relation is never dropped.
    `expanded` maps an expanded polyline's authored id -> its segment ids: a ref to it is
    not dangling (the caller fans a `fix` out or teaches), and it and `renamed` (authored
    id -> assigned e<N>) let the refusal state the id assignment that actually happened.
    """
    # etypes=None means "the entity roster is not in hand" — the standalone
    # sketch_add_constraint op names a sketch that is already in the document, so the
    # membership check belongs to the engine there. Type/ref/value normalization is
    # roster-independent and still runs.
    roster_known = etypes is not None
    etypes = etypes or {}
    nc = dict(c)

    # ---- type ---------------------------------------------------------------
    raw_type = nc.get("type")
    ctype = _slug(raw_type)
    alias = _CONSTRAINT_TYPE_ALIASES.get(ctype)
    if isinstance(alias, tuple):
        ctype, extras = alias
        nc.update(extras)
        rewrites.add(f'"{raw_type}" -> {ctype} + axis:"{extras["axis"]}"')
    elif isinstance(alias, str):
        rewrites.add(f'"{raw_type}" -> {alias}')
        ctype = alias
    generic = ctype in _GENERIC_DIM_TYPES
    if generic:
        # "Which dimension" named in a PARAM rather than in the type — the shape a model
        # writes when it thinks of the type as the category ({"type":"dimension",
        # "kind":"length"}). An explicit concrete kind settles it with no guessing.
        for key in ("kind", "dim_type", "dimension_type", "measure", "subtype"):
            named = _slug(nc.get(key))
            concrete = _CONSTRAINT_TYPE_ALIASES.get(named, named)
            if isinstance(concrete, tuple):
                concrete, extras = concrete
                nc.update(extras)
            if concrete in _DIM_CTYPES:
                rewrites.add(f'"{raw_type}" + {key}:"{nc[key]}" -> {concrete}')
                ctype = concrete
                generic = False
                nc.pop(key, None)
                break
    if ctype:
        nc["type"] = ctype

    # ---- refs ---------------------------------------------------------------
    refs = None
    for key in _CONSTRAINT_REF_LISTS:
        v = nc.get(key)
        if isinstance(v, list):
            refs = list(v)
            if key != "refs":
                rewrites.add(f"{key}[] -> refs[]")
            break
        if isinstance(v, str) and v.strip():
            # A 1-ref relation written without the list ({"type":"fix","refs":"e5"}).
            refs = [v]
            rewrites.add(f'{key}:"…" -> refs:["…"]')
            break
    # Slots not yet consumed may still hold the refs the relation is short of — a model
    # that wrote {"refs":"e1.p2","b":"e2.p1"} named both, one per dialect. Top up rather
    # than lose the relation to «coincident takes 2..2 refs».
    need = _CONSTRAINT_ARITY.get(ctype, 0)
    if refs is None or len(refs) < need:
        for a, b in _CONSTRAINT_REF_PAIRS:
            if isinstance(nc.get(a), str) and isinstance(nc.get(b), str):
                refs = (refs or []) + [nc[a], nc[b]]
                rewrites.add(f"{a}/{b} -> refs[]")
                break
            for key in (a, b):
                if refs and len(refs) < need and isinstance(nc.get(key), str) \
                        and nc[key].strip() and nc[key] not in refs:
                    refs = refs + [nc[key]]
                    rewrites.add(f"{key} -> refs[]")
    if refs is None or len(refs) < need:
        for key in _CONSTRAINT_REF_SINGLES:
            if isinstance(nc.get(key), str) and nc[key].strip():
                refs = (refs or []) + [nc[key]]
                rewrites.add(f"{key} -> refs[]")
                if len(refs) >= need:
                    break
    if refs is not None:
        if ctype == "symmetric" and len(refs) == 2:
            # The mirror line in its own slot. Only `symmetric` reads these keys — `axis`
            # on a distance is the h/v projection selector, a different thing entirely.
            for key in _SYMMETRY_AXIS_KEYS:
                v = nc.get(key)
                if isinstance(v, str) and v.strip():
                    refs.append(v)
                    nc.pop(key, None)
                    rewrites.add(f"symmetric {key} -> the third ref (refs=[a, b, axis])")
                    break
        out_refs = []
        for h in refs:
            h2 = _norm_handle(remap(h), etypes)
            if isinstance(h2, str) and h2 != h:
                rewrites.add(f'ref "{h}" -> "{h2}"')
            out_refs.append(h2)
        nc["refs"] = out_refs

    for key in _CONSTRAINT_REF_LISTS[1:] + _CONSTRAINT_REF_SINGLES:
        nc.pop(key, None)
    for a, b in _CONSTRAINT_REF_PAIRS:
        if a in nc and b in nc and a not in _DIM_VALUE_KEYS:
            nc.pop(a, None)
            nc.pop(b, None)

    # ---- dangling refs ------------------------------------------------------
    # A ref naming nothing is the one thing that must NEVER be quietly dropped: the sketch
    # then solves without the relation the model believes it placed. The engine does reject
    # it, but names no alternative («'e99' is not a valid handle»), so teach here where the
    # id roster is in hand.
    known = sorted(etypes, key=_natural_key)
    for h in (nc.get("refs") or []):
        if not isinstance(h, str):
            raise ValueError(
                f'{label}: constraint "{ctype or raw_type}" ref {h!r} is not an entity '
                f"handle. A relation references sketch entities by id — "
                f'{", ".join(known) if known else "(this sketch has no entities)"} — '
                f'optionally with a point suffix ("e1.p2", "e3.center").')
        if not roster_known:
            continue
        base = h.partition(".")[0]
        if h in ("axis.h", "axis.v") or base == "origin" or base in etypes or base in (expanded or {}):
            continue
        hint = ""
        if _BATCH_REF_RE.match(base):
            hint = ("  A \"$N\" placeholder names a FEATURE created earlier in this batch; "
                    "a sketch relation references entities INSIDE this sketch instead.")
        raise ValueError(
            f'{label}: constraint "{ctype or raw_type}" references entity "{base}", which '
            f"this sketch does not contain. Its entity ids are "
            f'{", ".join(known) if known else "(none — params.entities is empty)"} '
            f"({_id_assignment_note(renamed, expanded)}).{hint}")

    # ---- generic "put a dimension here" -------------------------------------
    if generic:
        refs_now = nc.get("refs") or []
        n = len(refs_now)
        first = refs_now[0] if n else ""
        # A 1-ref dimension measures a WHOLE entity (a line's length, a circle's radius);
        # a lone POINT handle ("e1.p1") measures nothing on its own, so it is the one shape
        # here that must not be guessed at.
        whole = isinstance(first, str) and "." not in first
        base_type = etypes.get(first, "") if whole else ""
        if n >= 2:
            ctype = "distance"
        elif base_type in ("circle", "arc"):
            ctype = "radius"
        elif base_type in ("line", "slot"):
            ctype = "length"
        elif not roster_known:
            raise ValueError(
                f'{label}: "{raw_type}" does not say WHICH dimension. On an existing '
                f"sketch this op cannot infer it, so name the concrete relation in "
                f'"type": length (one whole line), radius or diameter (one circle/arc), '
                f"distance (two refs), angle (two lines).")
        else:
            raise ValueError(
                f'{label}: "{raw_type}" does not say WHICH dimension, and refs='
                f"{refs_now} does not settle it. The engine names them explicitly: "
                f"distance (two refs), length (one whole line), radius/diameter (one "
                f"circle or arc), angle (two lines). A lone point handle like "
                f'"e1.p1" measures nothing — give the second point too.')
        nc["type"] = ctype
        rewrites.add(f'"{raw_type}" on {base_type or "2 refs"} -> {ctype}')

    # ---- value --------------------------------------------------------------
    if ctype in _DIM_CTYPES:
        angular = ctype == "angle"
        if ctype == "distance" and isinstance(nc.get("axis"), str):
            ax = _DIM_AXIS_WORDS.get(nc["axis"].strip().lower())
            if ax and ax != nc["axis"]:
                rewrites.add(f'axis:"{nc["axis"]}" -> "{ax}"')
                nc["axis"] = ax
        num = _dim_number(nc.get("value"), angular)
        if num is None:
            for key in _DIM_VALUE_KEYS[1:]:
                if key not in nc:
                    continue
                num = _dim_number(nc[key], angular)
                if num is not None:
                    nc.pop(key, None)
                    rewrites.add(f"{ctype} value read from `{key}`")
                    break
        if num is not None:
            nc["value"] = {"value": _clean_num(num), "expr": None}
        elif isinstance(nc.get("value"), dict) and "value" in nc["value"]:
            nc["value"] = {"value": nc["value"]["value"], "expr": nc["value"].get("expr")}

        # driven / reference. Silently ignoring these made a REFERENCE dimension drive the
        # geometry (probe 2026-07-30: reference:true stretched a 40 mm line to 80 mm and
        # reported ok), so every spelling is resolved and echoed.
        driven = None
        for key in _DRIVEN_TRUE_KEYS:
            if key in nc and isinstance(nc[key], bool):
                driven = nc[key]
                if key != "driven":
                    nc.pop(key, None)
                    rewrites.add(f"{key}:{str(driven).lower()} -> driven:"
                                 f"{str(driven).lower()} (a reference dimension MEASURES "
                                 f"the geometry; it does not move it)")
                break
        if driven is None:
            for key in _DRIVING_KEYS:
                if key in nc and isinstance(nc[key], bool):
                    driven = not nc.pop(key)
                    rewrites.add(f"{key} -> driven:{str(driven).lower()} (the engine names "
                                 f"the INVERSE flag)")
                    break
        nc["driven"] = bool(driven)
    else:
        # A non-dimensional relation carrying a value is rejected outright
        # («value: parallel must not carry a value»); a null one is the same as absent.
        if nc.get("value") is None:
            nc.pop("value", None)

    return nc


#: What a model calls "this entity is construction geometry". The engine reads only
#: `construction`, and `bool(ne.get("construction", False))` silently swallowed every
#: other spelling — so a centreline written {"is_construction":true} became REAL profile
#: geometry, which is what the extrude then tries to sweep (live probe 2026-07-30: all
#: four alternative spellings produced construction=false with no note).
_CONSTRUCTION_ALIASES = ("is_construction", "for_construction", "construction_geometry",
                         "construction_line", "reference", "is_reference")
#: An entity type word that MEANS construction: SolidWorks' centreline, which every
#: mirror/revolve workflow starts with, is a `line` with construction:true. The engine has
#: no such entity type, so it was rejected outright as "not a sketch entity type".
_CONSTRUCTION_TYPES = {"centerline", "centreline", "construction_line", "axis_line"}
#: Words a model writes for a boolean it serialised as text. `bool("false")` is True, so
#: {"construction":"false"} silently INVERTED into construction geometry (probe verified).
_FALSE_WORDS = frozenset({"false", "no", "off", "0", "none", "null", ""})


def _as_bool(v: Any) -> Optional[bool]:
    """A flag however it is spelled — real bool, 0/1, or a serialised word. None = unknown."""
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    if isinstance(v, str):
        return v.strip().lower() not in _FALSE_WORDS
    return None


def _construction_alias_key(e: dict) -> Optional[str]:
    """The alias key carrying this entity's construction flag, when `construction` is not
    already a real bool. Also fires on the string form, which `bool()` inverts."""
    if isinstance(e.get("construction"), bool):
        return None
    if "construction" in e:
        return "construction"
    for k in _CONSTRUCTION_ALIASES:
        if k in e:
            return k
    return None


def _sketch_needs_norm(feat: dict, params: dict) -> bool:
    ents = params.get("entities") if isinstance(params.get("entities"), list) else feat.get("entities")
    cons = params.get("constraints") if isinstance(params.get("constraints"), list) else feat.get("constraints")
    if "entities" in feat or "constraints" in feat or "dimensions" in feat or "plane" in feat:
        return True
    if "dimensions" in params or "plane" in params:
        return True
    plane_ref = (feat.get("refs") or {}).get("plane")
    if not plane_ref:
        return True
    # A plane the model NAMED ("XZ", "Top Plane") is not a reserved datum, so the batch dies
    # on the dangling-ref guardrail even though every entity is already canonical. That path
    # never reached the normalizer before: fire on it too.
    if isinstance(plane_ref, str) and not _is_canon_datum(plane_ref):
        return True
    # `params.entities` / `params.constraints` are BOTH required for a sketch; a feature that
    # omits one is rejected outright ("params.constraints: required for sketch").
    if not isinstance(params.get("entities"), list) or not isinstance(
            params.get("constraints"), list):
        return True
    for e in (ents or []):
        if not isinstance(e, dict):
            continue
        if _entity_type_alias(e):
            return True
        # An object-form point ({"x":…,"y":…}) on an otherwise canonical entity: the
        # engine reads points only as arrays and would silently place the shape at the
        # origin, so this needs normalizing even though nothing else looks wrong.
        if any(isinstance(e.get(k), dict)
               for k in ("center", "p", "p1", "p2", "corner1", "corner2")):
            return True
        if _slug(e.get("type")) in _POLYLINE_TYPES:
            return True
        if not (isinstance(e.get("id"), str) and _EID_RE.match(e["id"])):
            return True
        if not isinstance(e.get("construction"), bool) or _construction_alias_key(e):
            return True
        if e.get("type") == "line" and ("p1" not in e or "p2" not in e):
            return True
        if e.get("type") in ("circle", "arc", "ellipse", "ellipse_arc") and "center" not in e:
            return True
        if e.get("type") in ("circle", "arc") and "radius" not in e:
            return True
        if e.get("type") == "arc" and "bulge" in e:
            return True
        if e.get("type") == "point" and "p" not in e:
            return True
        # A rectangle whose corners are flat numbers: canonical in every other respect,
        # but the engine reads corners only as arrays, so without this it never reaches
        # the normalizer and dies on the engine's center-form/two-corner rejection.
        if _slug(e.get("type")) in _TWO_CORNER_TYPES and _flat_corners(e) is not None:
            return True
    for c in (cons or []):
        if not isinstance(c, dict):
            continue
        if _constraint_dialect_keys(c):
            return True
        if not (isinstance(c.get("id"), str) and _CID_RE.match(c["id"])):
            return True
    return False

#: Side words a model uses when it names a face in prose, mapped to the engine's side token.
_FACE_SIDE_WORDS = {
    "top": "top", "upper": "top", "above": "top",
    "bottom": "bottom", "lower": "bottom", "under": "bottom", "underside": "bottom",
    "front": "front", "back": "back", "rear": "back",
    "left": "left", "right": "right",
}
_FACE_NOUNS = ("face", "surface", "facet")


def _face_phrase_query(v: Any) -> Optional[dict]:
    """'the top face' -> {"of": "face", "side": "top"}; anything else -> None.

    A prose FACE reference is not a datum. _norm_datum returns an unrecognized string
    UNCHANGED, so refs.plane="the top face" sailed through to the kernel and died on
    "'the top face' is neither a reserved datum nor a feature id" — which also stranded the
    extrude that referenced the sketch by name (live copilot run, 2026-07-28).

    Mapping it to the like-named ORIGIN DATUM would be wrong geometry, not a near miss:
    plane.top is z=0, while the top face of a 20mm boss is z=20. The geometry query resolves
    the actual face, so that is what a face phrase becomes. A phrase must name a face noun
    AND exactly one side, or it stays untouched for the datum path.
    """
    if not isinstance(v, str):
        return None
    words = re.findall(r"[a-z]+", v.lower())
    if not any(w in _FACE_NOUNS for w in words):
        return None
    sides = {_FACE_SIDE_WORDS[w] for w in words if w in _FACE_SIDE_WORDS}
    if len(sides) != 1:
        return None
    return {"of": "face", "side": sides.pop()}


#: Nouns that make a prose phrase name an EDGE (mirrors _FACE_NOUNS for the face path).
#: "corner" is deliberately NOT here: a corner is as often a vertex as an edge.
_EDGE_NOUNS = ("edge", "edges")
#: Words that mean "parallel to the Z axis". parse_axis_vec (core/doc/picks.cpp:585) reads
#: the literal "vertical" as Z too, so this is the engine's own vocabulary, not a guess.
_EDGE_VERTICAL_WORDS = ("vertical", "vertically", "upright")
#: "all edges" / "every edge" — an unfiltered query IS every edge, so it is unambiguous.
_EDGE_ALL_WORDS = ("all", "every", "each")


def _compact(v: Any) -> str:
    """A dict/value spelled the way the model should have written it (no stray spaces)."""
    return json.dumps(v, separators=(",", ":"), sort_keys=False)


def _edge_phrase_query(v: Any) -> Optional[dict]:
    """'the vertical edges' -> {"of":"edge","parallel":"z"}; anything ambiguous -> None.

    The EDGE twin of _face_phrase_query, and the same defect one slot over: a live copilot
    run (2026-07-28) asked for a fillet on "the vertical edges" and climbed four rejections
    — non-empty list, not-a-datum, kind not in [...], centroid missing — because prose is
    neither a selector nor a datum. The engine already resolves this exact geometry:
    picks.cpp::kQueryGrammar takes of/side/parallel, and parse_axis_vec reads "vertical"
    as the Z axis, so the phrase has one meaning and becomes that query.

    A phrase must name an EDGE noun and exactly one filter, or it stays untouched:
      * "horizontal edges" is TWO axes (x and y) — genuinely ambiguous, never guessed;
      * "the weird edges" names no filter the engine has;
    both keep the engine's teaching error instead of a silent wrong pick.
    """
    if not isinstance(v, str):
        return None
    words = re.findall(r"[a-z]+", v.lower())
    if not any(w in _EDGE_NOUNS for w in words):
        return None
    sides = {_FACE_SIDE_WORDS[w] for w in words if w in _FACE_SIDE_WORDS}
    vertical = any(w in _EDGE_VERTICAL_WORDS for w in words)
    if vertical and not sides:
        return {"of": "edge", "parallel": "z"}
    if sides and not vertical:
        if len(sides) != 1:
            return None
        return {"of": "edge", "side": sides.pop()}
    if not sides and not vertical and any(w in _EDGE_ALL_WORDS for w in words):
        return {"of": "edge"}
    return None


def _prose_geom_query(v: Any, kind: str) -> Optional[dict]:
    """The geometry query a prose FACE/EDGE phrase names, or None."""
    if kind == "face":
        return _face_phrase_query(v)
    if kind == "edge":
        return _edge_phrase_query(v)
    return None


#: A side token the ENGINE itself accepts under `side` (picks.cpp::kQueryGrammar), plus the
#: prose synonyms _FACE_SIDE_WORDS already knows. Used only where the ref SLOT has already
#: pinned the topology kind, so the face/edge noun a phrase would have to carry is implicit.
_BARE_SIDE_TOKENS = {
    **_FACE_SIDE_WORDS,
    "+x": "+x", "-x": "-x", "+y": "+y", "-y": "-y", "+z": "+z", "-z": "-z",
}


def _bare_side_query(v: Any, kind: str) -> Optional[dict]:
    """`refs.face="top"` -> `{"of":"face","side":"top"}`; anything else -> None.

    The phrase mappers need a face/edge NOUN ("the top face"), so the ONE-WORD form every
    model writes for the slot that already names the kind fell through to the datum path
    and died on «'top' is neither a reserved datum nor a feature id». Observed twice in one
    live sweep (claude-sonnet-5, 2026-08-04), on a plate and on a flange.
    """
    if kind not in ("face", "edge") or not isinstance(v, str):
        return None
    side = _BARE_SIDE_TOKENS.get(v.strip().lower())
    return {"of": kind, "side": side} if side else None


def _geom_ref_member(v: Any, kind: str) -> Optional[dict]:
    """One ref member -> the canonical geometry query it means, or None to leave it alone.

    Handles the prose phrase itself, the bare side word, and the {"query": …} wrapper the
    model reaches for ({"edges":{"query":"the vertical edges"}} — the live 2026-07-28
    rejection). A wrapper around a query DICT is just the query nested one level too deep,
    so it is unwrapped; a wrapper around prose that does not map is left in place so the
    engine still reports it.
    """
    q = _prose_geom_query(v, kind) or _bare_side_query(v, kind)
    if q is not None:
        return q
    if isinstance(v, dict) and set(v) == {"query"}:
        inner = v["query"]
        if isinstance(inner, dict) and inner:
            return dict(inner)
        return _prose_geom_query(inner, kind) or _bare_side_query(inner, kind)
    return None


#: Sketch params that legitimately hold a LIST and are not entity geometry.
_SKETCH_NON_GEOM_LISTS = frozenset({"entities", "constraints", "dimensions", "refs"})


def _stray_geometry_key(feat: dict, params: dict) -> Optional[str]:
    """A key holding what is unmistakably sketch geometry, that is not the entity list.

    "Unmistakably" = a non-empty list of dicts that each carry a geometry-shaped field.
    A list of strings, ids or numbers is NOT claimed — only a shape the caller clearly
    meant as entities, so this can reject without ever eating a legitimate param.
    """
    # Include every endpoint spelling the entity normalizer reads. In particular, the
    # live 2026-08-04 holster entities had `kind` plus from/to (no canonical `type`/p1/p2),
    # so omitting those fields let an unread list leak through to the engine.
    geom_fields = (
        "type", "points", "center", "radius",
        "p1", "p2", "start", "end", "from", "to", "a", "b",
        "start_point", "startpoint", "end_point", "endpoint",
        "x1", "y1", "x2", "y2",
    )
    for src in (params, feat):
        for key, val in src.items():
            if key in _SKETCH_NON_GEOM_LISTS or not isinstance(val, list) or not val:
                continue
            if all(isinstance(x, dict) and any(g in x for g in geom_fields) for x in val):
                return key
    return None


def _normalize_sketch_feature(feat: dict, notes: Optional[list] = None) -> dict:
    """Rewrite a natural/flat sketch add_feature into the engine's canonical shape."""
    params = feat.get("params") if isinstance(feat.get("params"), dict) else {}
    sink = notes if isinstance(notes, list) else []

    def _pick(key: str) -> Any:
        if isinstance(params.get(key), list):
            return params[key]
        if isinstance(feat.get(key), list):
            return feat[key]
        return None

    # A sketch with no `entities` at all is still a sketch: the engine REQUIRES
    # params.entities and params.constraints on every one, so an empty profile must be
    # normalized into the canonical empty lists rather than skipped.
    ents_in = _pick("entities")
    ents_key = "entities"
    if ents_in is None:
        # Aliases for the entity list. `_pick` only accepts LIST values, so the singular
        # shape/profile/curve/element/entity/segment spellings are unambiguously entity
        # collections too. `outline` and `sketch` are deliberately excluded: an outline
        # may be a raw point chain, while sketch commonly names a profile reference.
        # `profile` is the verbatim live 2026-08-04 IWB-holster spelling; before this it
        # leaked into refs and the validator taught the model the wrong layer.
        for alias in (
                "shape", "shapes", "profile", "profiles", "geometry",
                "curve", "curves", "element", "elements", "entity",
                "segment", "segments"):
            alt = _pick(alias)
            if alt is not None:
                ents_in, ents_key = alt, alias
                sink.append(
                    f'{_feat_label(feat, "sketch")}: read params.{alias} as the sketch '
                    f"entity list — a sketch carries params.entities (each one a "
                    f"line/arc/circle/polygon/rectangle/polyline)")
                break
    ents_in = ents_in or []
    if not ents_in:
        # Geometry the model DID send, under a key nothing here reads. Creating the sketch
        # anyway is the worst outcome: the call reports ok, the sketch is empty, and the
        # extrude that references it dies later on "profile sketch has no closed face" —
        # an error that names neither the real cause nor a fix. Reject here, naming the key.
        stray = _stray_geometry_key(feat, params)
        if stray:
            raise ValueError(
                f'sketch "{_feat_label(feat, "sketch")}": params.{stray} holds geometry but '
                f"a sketch's entities live in params.entities — move them there. Each entity "
                f'is one shape: {{"type":"line","p1":[x,y],"p2":[x,y]}}, '
                f'{{"type":"circle","center":[x,y],"radius":r}}, '
                f'{{"type":"polygon","sides":6,"center":[x,y],"radius":r}}, '
                f'{{"type":"rectangle","center":[x,y],"width":w,"height":h}} or '
                f'{{"type":"polyline","closed":true,"points":[[x,y],…]}}.')
    if not _sketch_needs_norm(feat, params):
        return feat  # already canonical — leave byte-identical

    cons_in = _pick("constraints") or []
    dims_in = _pick("dimensions") or []

    id_map: dict[str, str] = {}
    norm_ents: list = []
    eid = 0
    reserved_eids = {e["id"] for e in ents_in if isinstance(e, dict)
                     and isinstance(e.get("id"), str) and _EID_RE.fullmatch(e["id"])}
    expanded_ids: dict[str, list[str]] = {}
    expanded_segs: dict[str, list[dict]] = {}
    # Flat shorthand a model writes instead of the engine's canonical entity fields
    # (cx/cy for center, r/d/dia/diameter for radius, x1/y1/x2/y2 for a line's
    # endpoints, x/y for a lone point) — tracked here so a 29-circle lattice sketch
    # emits ONE batch note, not one per entity.
    used_shorthand: set[str] = set()
    used_alias_keys: set[str] = set()
    for e in ents_in:
        if not isinstance(e, dict):
            norm_ents.append(e)
            continue
        alias = _entity_type_alias(e)
        if alias:
            # {"kind":"line",…} — the shape word is right, the FIELD name is a synonym.
            e = {"type": e[alias], **{k: v for k, v in e.items() if k != alias}}
            used_alias_keys.add(alias)
        if _slug(e.get("type")) in _POLYLINE_TYPES:
            # Expansion must not consume a canonical id belonging to another entity.
            start = max([eid] + [int(x[1:]) for x in reserved_eids])
            seg = _expand_polyline(e, start)
            if seg:
                pid = e.get("id")
                if isinstance(pid, str) and pid:
                    if pid in id_map or pid in expanded_ids:
                        raise ValueError(f'sketch "{_feat_label(feat, "sketch")}": '
                                         f"duplicate entity id {pid!r}")
                    expanded_ids[pid] = [x["id"] for x in seg]
                    expanded_segs[pid] = seg
                eid = start + len(seg)
                norm_ents.extend(seg)
                span = seg[0]["id"] if len(seg) == 1 else f'{seg[0]["id"]}..{seg[-1]["id"]}'
                sink.append(
                    f'{_feat_label(feat, "sketch")}: expanded the polyline into '
                    f"{len(seg)} line entities ({span}) — a sketch entity is one segment "
                    f"(line/arc/circle/spline), so a profile is a list of them")
                continue
            # < 2 distinct points, or a point that is not [x,y]: nothing unambiguous to
            # expand. Fall through and let the engine name what is wrong with it.
        eid += 1
        raw_id = e.get("id")
        if isinstance(raw_id, str) and _EID_RE.fullmatch(raw_id):
            new_id = raw_id
        else:
            while f"e{eid}" in reserved_eids:
                eid += 1
            new_id = f"e{eid}"
            reserved_eids.add(new_id)
        if isinstance(raw_id, str) and raw_id:
            if raw_id in id_map or raw_id in expanded_ids:
                raise ValueError(f'sketch "{_feat_label(feat, "sketch")}": duplicate entity id {raw_id!r}')
            id_map[raw_id] = new_id
        ne = dict(e)
        ne["id"] = new_id
        if _slug(ne.get("type")) in _CONSTRUCTION_TYPES:
            # A centreline IS a construction line — the engine has no separate type.
            ne["type"] = "line"
            ne["construction"] = True
            used_shorthand.add("centerline")
        else:
            ck = _construction_alias_key(ne)
            flag = _as_bool(ne.pop(ck)) if ck else None
            for k in _CONSTRUCTION_ALIASES:
                ne.pop(k, None)
            ne["construction"] = bool(flag) if flag is not None else bool(
                ne.get("construction", False))
            if ck and ck != "construction":
                used_shorthand.add("construction_alias")
            elif ck:
                used_shorthand.add("construction_word")
        if _slug(ne.get("type")) in _ARC3_TYPES:
            # The engine has one "arc" type; the three-point form is a spelling of it.
            ne["type"] = "arc"
            used_shorthand.add("arc_three_point_type")
        etype = ne.get("type")
        if etype == "line":
            # Endpoint aliases models write: p1/p2 canonical; start/end, from/to, and
            # a/b all seen in live rejected batches (a/b from a 2026-07-27 copilot run);
            # x1/y1/x2/y2 is the flat-coordinate dialect (the same shape a model reaches
            # for on circle/arc, one slot over).
            p1 = ne.get("p1", ne.get("start", ne.get("from", ne.get("a"))))
            p2 = ne.get("p2", ne.get("end", ne.get("to", ne.get("b"))))
            if p1 is None:
                p1 = _flat_point(ne, "x1", "y1")
                if p1 is not None:
                    used_shorthand.add("line_xy")
            if p2 is None:
                p2 = _flat_point(ne, "x2", "y2")
                if p2 is not None:
                    used_shorthand.add("line_xy")
            if p1 is not None:
                ne["p1"] = _xy(p1)
            if p2 is not None:
                ne["p2"] = _xy(p2)
            for k in ("start", "end", "from", "to", "a", "b", "x1", "y1", "x2", "y2"):
                ne.pop(k, None)
        elif etype == "point":
            if isinstance(ne.get("p"), list):
                ne["p"] = _xy(ne["p"])
            elif "p" not in ne:
                flat_p = _flat_point(ne, "x", "y")
                if flat_p is not None:
                    ne["p"] = flat_p
                    ne.pop("x", None)
                    ne.pop("y", None)
                    used_shorthand.add("point_xy")
        elif etype in ("circle", "arc", "ellipse", "ellipse_arc"):
            # cx/cy for the center — the live 2026-07-28 SIGSEGV payload
            # ({"type":"circle","cx":-23,"cy":0,"r":2.5}, a 29-circle keychain-hole
            # lattice) reached allocate_entity with no "center"/"radius" at all; the
            # crash is fixed, but the shape still needs normalizing or it just becomes
            # a clean rejection ("center: required for circle") instead of a build.
            if "center" in ne:
                ne["center"] = _xy(ne["center"])
            else:
                flat_c = _flat_point(ne, "cx", "cy")
                if flat_c is not None:
                    ne["center"] = flat_c
                    ne.pop("cx", None)
                    ne.pop("cy", None)
                    used_shorthand.add("center")
            for k in ("start", "end"):
                if isinstance(ne.get(k), list):
                    ne[k] = _xy(ne[k])
            if etype == "arc":
                _normalize_arc_entity(ne, _feat_label(feat, "sketch"), used_shorthand)
                if "ccw" not in ne:
                    ne["ccw"] = True
            if etype in ("circle", "arc") and "radius" not in ne:
                flat_r, was_diam = _flat_radius(ne)
                if flat_r is not None:
                    ne["radius"] = flat_r
                    for k in ("r",) + _DIAMETER_KEYS:
                        ne.pop(k, None)
                    used_shorthand.add("diameter" if was_diam else "radius")
        else:
            # Shape-primitive entities the ENGINE expands (polygon/ngon/hexagon/…,
            # rectangle/rect, slot): it reads their center and corners ONLY as arrays, so
            # an object point silently degrades to the origin instead of erroring. Coerce
            # every point-shaped slot here; the size keys stay for the engine to read.
            for k in ("center", "corner1", "corner2", "p1", "p2", "start", "end"):
                if k in ne:
                    coerced = _xy(ne[k])
                    # By VALUE: _xy hands back a fresh list even for an already-canonical
                    # [x,y], and an identity test made every polygon claim a rewrite that
                    # never happened — a note that teaches the model a lie.
                    if coerced != ne[k]:
                        ne[k] = coerced
                        used_shorthand.add("point_obj")
            if _slug(etype) in _TWO_CORNER_TYPES:
                corners = _flat_corners(ne)
                if corners is not None:
                    ne["corner1"], ne["corner2"] = corners
                    for k in ("x1", "y1", "x2", "y2"):
                        ne.pop(k, None)
                    used_shorthand.add("corner_xy")
            if "center" not in ne:
                flat_c = _flat_point(ne, "cx", "cy")
                if flat_c is not None:
                    ne["center"] = flat_c
                    ne.pop("cx", None)
                    ne.pop("cy", None)
                    used_shorthand.add("center")
        norm_ents.append(ne)

    if used_alias_keys:
        sink.append(
            f'{_feat_label(feat, "sketch")}: read the entity shape word from '
            f'{"/".join(sorted(used_alias_keys))} as `type` — a sketch entity names its '
            f'shape in "type" ({{"type":"line","p1":[x,y],"p2":[x,y]}})')
    if used_shorthand:
        rewrites = []
        if "center" in used_shorthand:
            rewrites.append('cx/cy -> center:[x,y]')
        if "radius" in used_shorthand:
            rewrites.append('r -> radius')
        if "diameter" in used_shorthand:
            rewrites.append('d/dia/diameter -> radius (halved)')
        if "line_xy" in used_shorthand:
            rewrites.append('x1/y1/x2/y2 -> p1/p2')
        if "corner_xy" in used_shorthand:
            rewrites.append('x1/y1/x2/y2 -> corner1/corner2')
        if "point_xy" in used_shorthand:
            rewrites.append('x/y -> p')
        if "point_obj" in used_shorthand:
            rewrites.append('{"x":…,"y":…} -> [x,y]')
        if "centerline" in used_shorthand:
            rewrites.append('type:"centerline" -> line + construction:true')
        if "construction_alias" in used_shorthand:
            rewrites.append('is_construction/for_construction/reference -> construction')
        if "construction_word" in used_shorthand:
            rewrites.append('construction:"true"/"false" -> a real boolean')
        if "arc_bulge" in used_shorthand:
            rewrites.append("from/to + bulge -> arc center/radius/start/end/ccw")
        elif "arc_endpoints" in used_shorthand:
            rewrites.append("arc point endpoints -> start/end angles")
        if "arc_three_point" in used_shorthand:
            rewrites.append("start/mid/end -> arc center/radius/start/end/ccw")
        if "arc_angle_key" in used_shorthand:
            rewrites.append("start_angle/end_angle -> start/end")
        if "arc_sweep_word" in used_shorthand:
            rewrites.append("arc sweep word -> ccw")
        sink.append(
            f'{_feat_label(feat, "sketch")}: expanded flat entity shorthand '
            f'({"; ".join(rewrites)}) into the engine\'s canonical entity fields '
            f"— one note for the whole sketch, not per entity")

    def _remap(h: Any) -> Any:
        if not isinstance(h, str) or h in ("origin", "origin.p"):
            return h
        base, _, sub = h.partition(".")
        base = id_map.get(base, base)
        return base + ("." + sub if sub else "")

    etypes = {e["id"]: e.get("type") for e in norm_ents
              if isinstance(e, dict) and isinstance(e.get("id"), str)}
    norm_cons = []
    all_cons = list(cons_in) + list(dims_in)
    reserved_cids = {c["id"] for c in all_cons if isinstance(c, dict)
                     and isinstance(c.get("id"), str) and _CID_RE.fullmatch(c["id"])}
    cn = 0
    con_rewrites: set = set()
    for c in all_cons:
        if not isinstance(c, dict):
            norm_cons.append(c)
            continue
        nc = _normalize_one_constraint(c, _remap, etypes,
                                       f'sketch "{_feat_label(feat, "sketch")}"',
                                       con_rewrites, expanded_ids, id_map)
        copies = [nc]
        for ref in nc.get("refs", []):
            pid = ref.partition(".")[0] if isinstance(ref, str) else None
            if pid not in expanded_ids:
                continue
            ctype = nc.get("type")
            if ctype != "fix" or "." in ref or len(nc["refs"]) != 1:
                which = ", ".join(f'{s["id"]} {s["p1"]}->{s["p2"]}'
                                  for s in expanded_segs[pid])
                raise ValueError(
                    f'sketch "{_feat_label(feat, "sketch")}": constraint "{ctype}" '
                    f'references {ref!r}, but polyline "{pid}" was expanded into '
                    f"{len(expanded_ids[pid])} line entities, segment by segment in point "
                    f"order: {which}. A \"fix\" whose only ref is the whole polyline pins "
                    f"every segment; any other relation must name the segment (or its "
                    f'endpoint, e.g. "{expanded_ids[pid][0]}.p1") it means.')
            copies = [{**nc, "refs": [sid]} for sid in expanded_ids[pid]]
            ids = expanded_ids[pid]
            sink.append(
                f'{_feat_label(feat, "sketch")}: "fix" on polyline "{pid}" -> one "fix" '
                f"per expanded segment ({ids[0]}..{ids[-1]}), which pins the whole "
                f"polyline")
            break
        for index, nc in enumerate(copies):
            cid = nc.get("id")
            if index or not (isinstance(cid, str) and _CID_RE.fullmatch(cid)):
                cn += 1
                while f"c{cn}" in reserved_cids:
                    cn += 1
                nc["id"] = f"c{cn}"
                reserved_cids.add(nc["id"])
            norm_cons.append(_norm_dim_value(nc))
    if con_rewrites:
        sink.append(
            f'{_feat_label(feat, "sketch")}: rewrote relations/dimensions onto the engine\'s '
            f"closed vocabulary ({'; '.join(sorted(con_rewrites))}) — one note for the whole "
            f'sketch. Canonical: {{"id":"c1","type":"<relation>","refs":["e1","e2.p1"]}}, and '
            f'a dimension adds {{"value":{{"value":N,"expr":null}},"driven":false}} '
            f"(driven:true MEASURES the geometry instead of forcing it)")

    refs_out = dict(feat.get("refs") or {})
    plane = None
    for src in (refs_out.get("plane"), params.get("plane"), feat.get("plane")):
        if src is None:
            continue
        # An already-resolved selector ({"of":"face","side":"top"}, a sid selector, …).
        # _norm_datum only speaks strings, so this used to fall through the whole loop and
        # land on the "plane.top" default — a SILENT swap of the real top face for the
        # origin datum, which is different geometry, not a near miss.
        if isinstance(src, dict):
            plane = src
            break
        query = _face_phrase_query(src)
        if query is not None:
            plane = query
            if notes is not None:
                notes.append(
                    f'{_feat_label(feat, "sketch")}: read plane="{src}" as '
                    f'{{"of":"face","side":"{query["side"]}"}} — a prose FACE reference '
                    f"resolves the real face of the body; the like-named origin datum "
                    f'("plane.{query["side"]}") is a different plane entirely')
            break
        p = _norm_datum(src)
        if p:
            plane = p
            break
    refs_out["plane"] = plane or "plane.top"

    new_feat = dict(feat)
    for k in ("entities", "constraints", "dimensions", "plane"):
        new_feat.pop(k, None)
    new_feat.pop(ents_key, None)
    # Models label a sketch with "id":"profile" intending to reference it later as the
    # extrude/revolve profile — but refs resolve by feature NAME (or "$N"/f-id), so the
    # label was dead weight and the batch died on "profile 'profile' not found" (live
    # copilot run, 2026-07-27). Promote it to the display name when none was given.
    if (not isinstance(new_feat.get("name"), str) or not new_feat["name"].strip()):
        sid = new_feat.get("id")
        if isinstance(sid, str) and sid.strip():
            new_feat["name"] = sid.strip()
    new_params = dict(params)
    new_params.pop("plane", None)
    new_params.pop("dimensions", None)
    new_params.pop(ents_key, None)   # consumed into `entities` above
    new_params["entities"] = norm_ents
    new_params["constraints"] = norm_cons
    new_feat["params"] = new_params
    new_feat["refs"] = refs_out
    return new_feat


def _normalize_sketch_ops(ops: Any, notes: Optional[list] = None,
                          doc_id: Optional[str] = None) -> Any:
    """Normalize creation and replacement geometry with one sketch convention.

    An edit is a patch, not a new sketch: merge the current feature first so omitted
    entities, constraints, plane and offset cannot be reset by creation defaults.
    """
    if not isinstance(ops, list):
        return ops
    edits_geometry = any(isinstance(op, dict) and op.get("op") == "edit_feature"
                         and isinstance(op.get("params"), dict)
                         and any(k in op["params"] for k in ("entities", "constraints", "dimensions"))
                         for op in ops)
    features = {}
    if edits_geometry and doc_id:
        tree = cb.call("doc_tree", {"doc_id": doc_id}).get("tree") or {}
        features = {f["id"]: f for f in tree.get("features", []) if isinstance(f, dict)}
    out = []
    for index, op in enumerate(ops):
        if not isinstance(op, dict):
            out.append(op)
            continue
        feat = op.get("feature")
        if op.get("op") == "add_feature" and isinstance(feat, dict):
            if feat.get("type") == "sketch":
                feat = _normalize_sketch_feature(feat, notes)
                op = {**op, "feature": feat}
            features[f"${index}"] = feat
        elif op.get("op") == "edit_feature":
            current = features.get(op.get("id"))
            params = op.get("params")
            if (isinstance(current, dict) and current.get("type") == "sketch"
                    and isinstance(params, dict)
                    and any(k in params for k in ("entities", "constraints", "dimensions"))):
                for key in ("entities", "constraints", "dimensions"):
                    if key in params and not isinstance(params[key], list):
                        raise ValueError(
                            f"edit_feature {op.get('id')}: params.{key} must be a JSON "
                            "array; omit the field to keep its current value")
                merged = {**current,
                          "params": {**current.get("params", {}), **params},
                          "refs": {**current.get("refs", {}), **op.get("refs", {})}}
                normalized = _normalize_sketch_feature(merged, notes)
                normalized_params = normalized["params"]
                # Read the current sketch for context, but send only the caller's patch
                # and handles the normalizer actually rewrote. Unrelated live parameters
                # must not be copied back from this earlier readback.
                patch = {key: normalized_params[key] for key in params
                         if key in normalized_params}
                for key in ("entities", "constraints"):
                    if normalized_params[key] != merged["params"].get(key):
                        patch[key] = normalized_params[key]
                op = {**op, "params": patch}
                if "plane" in params or "plane" in op.get("refs", {}):
                    op["refs"] = {**op.get("refs", {}), "plane": normalized["refs"]["plane"]}
                features[op["id"]] = normalized
            elif isinstance(current, dict):
                features[op["id"]] = {**current,
                    "params": {**current.get("params", {}), **(params or {})}}
        out.append(op)
    return out


# ---------------------------------------------------------------------------
# 2D EDITING-OP dialect (trim / extend / offset / fillet / chamfer / mirror /
# pattern / move / rotate / toggle-construction / set-dim).
#
# These ops are the advanced half of the sketcher and, unlike the sketch FEATURE, they had
# no Python normalizer at all — every payload went straight to session.cpp, whose key names
# are not even consistent with each other (trim/extend read `entity`, offset/mirror/pattern
# read `entities`, fillet/chamfer read `e1`/`e2`). Measured on the live engine 2026-07-30
# over 33 realistic payloads: 19 were rejected on a key name alone, and five FLAGS were
# accepted and then silently ignored, which is worse:
#   * sketch_move/sketch_rotate {"duplicate":true} DESTROYED the original instead of
#     copying it — the op reported ok and the source geometry was gone.
#   * a circular sketch_pattern_entities with angle:360 and no `equal_spacing` stacked all
#     four copies on top of the original (angle is PER STEP, so 360/720/1080° are each the
#     identity rotation) — the same "seven hexagons at the origin" failure the 2026-07-28
#     keychain run hit one layer up.
#   * {"keep_as_construction":true} on a trim deleted the span it was asked to keep.
#   * {"both_sides":true} on an offset produced one side.
_SKETCH_EDIT_KEY_ALIASES: dict[str, dict[str, str]] = {
    "sketch_trim": {"id": "entity", "target": "entity", "entity_id": "entity",
                    "curve": "entity", "at": "point", "pick": "point",
                    "pick_point": "point", "position": "point", "location": "point",
                    "uv": "point", "keep_as_construction": "keep_construction",
                    "as_construction": "keep_construction"},
    "sketch_extend": {"id": "entity", "target": "entity", "entity_id": "entity",
                      "curve": "entity", "which": "end", "which_end": "end", "side": "end"},
    "sketch_offset": {"offset": "distance", "dist": "distance", "amount": "distance",
                      "d": "distance", "direction": "side", "toward": "side",
                      "side_point": "side", "through": "side"},
    "sketch_fillet": {"r": "radius", "fillet_radius": "radius", "rad": "radius"},
    "sketch_chamfer": {"d": "distance", "size": "distance", "dist": "distance",
                       "length": "distance", "d2": "distance2", "distance_2": "distance2"},
    "sketch_mirror_entities": {"axis": "line", "mirror_line": "line", "about": "line",
                               "line_id": "line", "mirror_about": "line",
                               "centerline": "line"},
    "sketch_pattern_entities": {"type": "mode", "pattern_type": "mode", "kind": "mode",
                                "direction": "dir", "vector": "dir", "dir_vec": "dir",
                                "step": "spacing", "pitch": "spacing",
                                "distance": "spacing", "instances": "count",
                                "copies": "count", "num": "count", "total_angle": "angle",
                                "angle_deg": "angle", "equal": "equal_spacing",
                                "even_spacing": "equal_spacing",
                                "equally_spaced": "equal_spacing"},
    "sketch_move": {"duplicate": "copy", "keep_original": "copy", "as_copy": "copy"},
    "sketch_rotate": {"duplicate": "copy", "keep_original": "copy", "as_copy": "copy",
                      "angle_deg": "angle", "degrees": "angle", "rotation": "angle"},
    "sketch_toggle_construction": {"entity": "id", "entity_id": "id", "target": "id"},
    "sketch_set_dim": {"constraint": "id", "cid": "id", "dim": "id",
                       "constraint_id": "id", "dimension": "id", "dim_id": "id",
                       "dimension_id": "id", "reference": "driven",
                       "is_reference": "driven"},
}
#: Ops the engine reads as a LIST of entity ids, and the spellings a model puts them under.
_EDIT_ENTITY_LIST_OPS = frozenset({
    "sketch_offset", "sketch_mirror_entities", "sketch_pattern_entities", "sketch_move",
    "sketch_rotate", "sketch_delete_entity"})
_EDIT_ENTITY_LIST_ALIASES = ("entity", "ids", "entity_ids", "elements", "items",
                             "targets", "selection")
#: …and the two that read a SINGLE id, where a one-element list is the same intent.
_EDIT_ENTITY_SINGLE_OPS = frozenset({"sketch_trim", "sketch_extend"})
#: Ops whose value keys are coordinates the engine reads only as [x, y] arrays.
_EDIT_POINT_KEYS = ("point", "side", "dir", "center")
#: Pattern mode words -> the engine's closed pair.
_PATTERN_MODE_WORDS = {
    "linear": "linear", "line": "linear", "rectangular": "linear", "rect": "linear",
    "grid": "linear", "translate": "linear", "linear_pattern": "linear",
    "circular": "circular", "polar": "circular", "radial": "circular",
    "circle": "circular", "round": "circular", "rotational": "circular",
    "circular_pattern": "circular",
}
#: Which end of a curve `sketch_extend` grows.
_EXTEND_END_WORDS = {
    "start": "start", "p1": "start", "begin": "start", "beginning": "start",
    "first": "start", "head": "start",
    "end": "end", "p2": "end", "last": "end", "finish": "end", "tail": "end",
}


def _edit_op_label(op: dict) -> str:
    sk = op.get("sketch")
    return f'{op.get("op")} on sketch "{sk}"' if isinstance(sk, str) else str(op.get("op"))


#: Op-name spellings session.cpp does not have. The engine's own "closest:" hint already
#: names the right one, but a rejected batch costs a whole round trip for a synonym.
_SKETCH_OP_NAME_ALIASES = {
    "sketch_remove_constraint": "sketch_delete_constraint",
    "sketch_remove_entity": "sketch_delete_entity",
    "sketch_add_relation": "sketch_add_constraint",
    "sketch_add_dimension": "sketch_add_constraint",
    "sketch_set_dimension": "sketch_set_dim",
}
#: Op-level spellings of the refs a standalone constraint op needs INSIDE its constraint.
_CONSTRAINT_OP_REF_KEYS = ("refs", "entities", "handles", "elements", "targets")


def _normalize_constraint_op(op: dict, notes: list) -> dict:
    """`sketch_add_constraint` -> the canonical constraint object.

    The INLINE sketch path has run every relation through `_normalize_one_constraint`
    for a long time; this standalone op never did, so the same payload that builds
    inline was rejected by document validation once the sketch already existed
    (live probe-holster-5: `type:"dimension"` + `kind:"length"`, and refs written at
    op level where the engine does not read them). The entity roster is not in hand
    here — the sketch is already in the document — so membership stays the engine's
    call and only the roster-independent normalization runs.
    """
    con = op.get("constraint")
    if not isinstance(con, dict):
        return op
    nc = dict(op)
    con = dict(con)
    rewrites: set = set()
    # Refs written beside the constraint rather than inside it.
    if not any(k in con for k in _CONSTRAINT_OP_REF_KEYS):
        for key in _CONSTRAINT_OP_REF_KEYS:
            v = nc.get(key)
            if isinstance(v, list) and v:
                con["refs"] = list(v)
                nc.pop(key, None)
                rewrites.add(f"op-level {key}[] -> constraint.refs[]")
                break
            if isinstance(v, str) and v.strip():
                con["refs"] = [v]
                nc.pop(key, None)
                rewrites.add(f'op-level {key}:"…" -> constraint.refs[…]')
                break
    nc["constraint"] = _normalize_one_constraint(
        con, lambda h: h, None, _edit_op_label(op), rewrites)
    if rewrites:
        notes.append(f"{_edit_op_label(op)}: " + "; ".join(sorted(rewrites)))
    return nc


def _normalize_sketch_edit_op(op: dict, notes: list) -> dict:
    """One 2D editing op -> the exact key names session.cpp reads.

    Raises ValueError for a payload whose INTENT cannot be expressed in one op — those
    would otherwise apply a different edit and report success.
    """
    raw_name = op.get("op")
    name = _SKETCH_OP_NAME_ALIASES.get(raw_name, raw_name)
    if name != raw_name:
        op = {**op, "op": name}
        notes.append(f'op "{raw_name}" -> "{name}" (the engine\'s own spelling)')
    if name == "sketch_add_constraint":
        return _normalize_constraint_op(op, notes)
    aliases = _SKETCH_EDIT_KEY_ALIASES.get(name)
    if aliases is None:
        return op
    nc = dict(op)
    label = _edit_op_label(op)
    moved: list[str] = []
    for alias, canon in aliases.items():
        if alias in nc and canon not in nc:
            nc[canon] = nc.pop(alias)
            moved.append(f"{alias} -> {canon}")
        elif alias in nc:
            nc.pop(alias)

    # An OBJECT where the engine reads a constraint id reached nlohmann as
    # `type must be string, but is object` — a kernel exception, not a teaching
    # rejection (live probe-holster-5). Say what the slot takes instead.
    if name == "sketch_set_dim" and "id" in nc and not isinstance(nc["id"], str):
        raise ValueError(
            f"{label}: sketch_set_dim RETUNES an existing dimension, so `id` is that "
            f'dimension\'s constraint id ("c3") — got {nc["id"]!r}. To CREATE a '
            f"dimension use sketch_add_constraint with the concrete type "
            f'(length/radius/diameter/distance/angle) and its `value`.')

    # ---- entity selection ---------------------------------------------------
    if name in _EDIT_ENTITY_LIST_OPS and not isinstance(nc.get("entities"), list):
        for alias in _EDIT_ENTITY_LIST_ALIASES:
            v = nc.get(alias)
            if isinstance(v, str) and v.strip():
                nc["entities"] = [v]
                nc.pop(alias, None)
                moved.append(f'{alias}:"…" -> entities:["…"]')
                break
            if isinstance(v, list) and v:
                nc["entities"] = list(v)
                nc.pop(alias, None)
                moved.append(f"{alias}[] -> entities[]")
                break
    if name in _EDIT_ENTITY_SINGLE_OPS and not isinstance(nc.get("entity"), str):
        v = nc.get("entities")
        if isinstance(v, list) and len(v) == 1 and isinstance(v[0], str):
            nc["entity"] = v[0]
            nc.pop("entities", None)
            moved.append("entities:[id] -> entity:id")
        elif isinstance(v, list) and len(v) > 1:
            raise ValueError(
                f"{label}: {name} edits ONE entity per op (it takes `entity`, not a list) — "
                f"emit one {name} op per entity.")
    if name in ("sketch_fillet", "sketch_chamfer"):
        v = nc.get("entities")
        if isinstance(v, list) and len(v) == 2 and all(isinstance(x, str) for x in v) \
                and not (isinstance(nc.get("e1"), str) and isinstance(nc.get("e2"), str)):
            nc["e1"], nc["e2"] = v
            nc.pop("entities", None)
            moved.append("entities:[a,b] -> e1/e2")

    # ---- coordinates --------------------------------------------------------
    for key in _EDIT_POINT_KEYS:
        if key in nc:
            coerced = _xy(nc[key])
            if coerced != nc[key]:
                nc[key] = coerced
                moved.append(f'{key}:{{"x":…,"y":…}} -> [x, y]')
    for key, (kx, ky) in (("delta", ("dx", "dy")), ("translation", ("dx", "dy")),
                          ("center", ("cx", "cy")), ("pivot", ("cx", "cy")),
                          ("about", ("cx", "cy"))):
        if name not in ("sketch_move", "sketch_rotate"):
            break
        if name == "sketch_move" and key not in ("delta", "translation"):
            continue
        if name == "sketch_rotate" and key in ("delta", "translation"):
            continue
        v = _xy(nc.get(key))
        if isinstance(v, list) and len(v) == 2 and all(_is_num(x) for x in v) \
                and kx not in nc and ky not in nc:
            nc[kx], nc[ky] = v
            nc.pop(key, None)
            moved.append(f"{key}:[a, b] -> {kx}/{ky}")

    # ---- enums and flags ----------------------------------------------------
    if name == "sketch_extend" and isinstance(nc.get("end"), str):
        e = _EXTEND_END_WORDS.get(nc["end"].strip().lower())
        if e and e != nc["end"]:
            moved.append(f'end:"{nc["end"]}" -> "{e}"')
            nc["end"] = e
    if name == "sketch_pattern_entities" and isinstance(nc.get("mode"), str):
        m = _PATTERN_MODE_WORDS.get(_slug(nc["mode"]))
        if m and m != nc["mode"]:
            moved.append(f'mode:"{nc["mode"]}" -> "{m}"')
            nc["mode"] = m
    for key in ("copy", "keep_construction", "equal_spacing", "construction", "fix",
                "driven"):
        if key in nc and not isinstance(nc[key], bool):
            b = _as_bool(nc[key])
            if b is not None:
                moved.append(f"{key}:{nc[key]!r} -> {str(b).lower()}")
                nc[key] = b

    # ---- semantics the engine cannot express in one op ----------------------
    if name == "sketch_offset":
        if _as_bool(nc.pop("both_sides", None)):
            raise ValueError(
                f"{label}: sketch_offset offsets ONE side per op — `side`:[u, v] is a point "
                f"on the side you want. For a two-sided offset emit two ops with opposite "
                f'`side` points (e.g. [0, 1] and [0, -1]).')
        if "side" not in nc:
            raise ValueError(
                f"{label}: sketch_offset needs `side`:[u, v] — a sketch-plane point on the "
                f"side the copy goes, which is what picks between the two offsets of a "
                f"curve. `distance` is the gap; `side` is the direction.")
    if name == "sketch_pattern_entities" and nc.get("mode") == "circular" \
            and not nc.get("equal_spacing"):
        ang = nc.get("angle")
        if _is_num(ang) and abs(ang) >= 360.0 - 1e-9:
            # `angle` is the PER-STEP rotation, so a 360° step is the identity and every
            # copy lands exactly on the source. That is never what "arrange N of these
            # around a full circle" means, and it fails silently — the op reports ok and
            # produces N invisible duplicates stacked on the original.
            nc["equal_spacing"] = True
            moved.append(f'angle:{ang} read as the TOTAL sweep -> equal_spacing:true '
                         f"(angle is the PER-STEP rotation; a 360° step is the identity, "
                         f"so every copy would land on the original)")

    if moved:
        notes.append(f"{label}: {'; '.join(moved)} — the 2D editing ops name their "
                     f"arguments exactly: trim/extend take `entity`+`point`/`end`, "
                     f"offset/mirror/pattern/move/rotate take `entities`:[…], and "
                     f"fillet/chamfer take `e1`/`e2`")
    return nc


def _normalize_sketch_edit_ops(ops: Any, notes: Optional[list] = None) -> Any:
    """Normalize the 2D editing ops in a batch; return a new list."""
    if not isinstance(ops, list):
        return ops
    sink = notes if isinstance(notes, list) else []
    out = []
    for op in ops:
        try:
            if isinstance(op, dict) and op.get("op") in _SKETCH_EDIT_KEY_ALIASES:
                op = _normalize_sketch_edit_op(op, sink)
        except ValueError:
            raise   # a teachable rejection — see _normalize_sketch_ops
        except Exception:  # noqa: BLE001 — never let normalization break a valid call
            pass
        out.append(op)
    return out


# ---------------------------------------------------------------------------
# Primitive placement/orientation dialect normalizer (model-robustness).
#
# The canonical primitive add_feature carries placement + orientation as plain
# feature-level keys: at / at_offset / axis / centered / mode / merge. Frontier
# models instead reach for the shape they know from other CAD APIs — a nested
# `placement:{origin:[…],direction:[…]}` plus `boolean:"union"` — and those keys
# used to be DROPPED WHOLE: a spout meant to sit at x=200 pointing along +X was
# built centred on the origin pointing up, buried in the body, and surfaced only
# as the opaque "cylinder boss added no material". The engine now hard-errors on
# any unrecognised feature key, so the dialect is rewritten into the canonical
# keys HERE, before the call — and every dialect key this understands must be
# FULLY CONSUMED (popped, never left beside the rewritten form), because a
# leftover key is now a rejected batch. Conservative: primitives only, and only a
# feature that actually shows a dialect key; canonical ops pass through untouched.
_PRIMITIVE_TYPES = frozenset({"box", "cylinder", "sphere", "cone", "torus"})
#: Aliases for the canonical placement point `at`. `center`/`centre` are listed
#: separately because they are the ONE spelling whose meaning depends on `centered`:
#: a primitive is built centred on the origin and `at` moves its CENTRE
#: (core/doc/document.cpp:3049-3058), so `center` IS `at` — but only while `centered`
#: is true. With `centered:false` the same `at` names the BASE, so the two together are
#: a genuine contradiction and get taught, never guessed (see below).
#: Live 2026-07-28: a stepped shaft sent two coaxial cylinders as center:[0,0,0] then
#: center:[0,0,60]; `center` was not an alias, so the engine ignored it, built BOTH on
#: the origin, and reported only "cylinder boss added no material (it is buried inside
#: the existing solid)" — a placement word dropped on the floor, read back as a
#: modelling mistake.
_CENTER_AT_ALIASES = ("center", "centre")
_AT_ALIASES = ("position", "location", "origin") + _CENTER_AT_ALIASES
_AXIS_ALIASES = ("direction", "dir", "normal", "orientation")
_BOOLEAN_ALIASES = ("boolean", "operation")
# The same intent written as a FLAG rather than a value: {"subtract": true}. Live
# 2026-07-30 (run-13566637896867, sports_bike): the model asked for wheel pockets
# with {"type":"cylinder",…,"subtract":true} and the engine FUSED them instead —
# the key was consumed by nothing, so it never became mode:"cut". It then reported
# "cylinder boss added no material … this primitive lies entirely inside it",
# which is true of the boss it was silently turned into and says nothing about the
# cut that was asked for. Wrong geometry with a confident error is the worst
# outcome this normalizer can produce, so the flag spellings are honoured here.
# "merge" and "add" are NOT flags: `merge` is already the canonical new-body key
# and a bare `add` is too weak to override an explicit mode.
_BOOLEAN_FLAG_MODES = {
    "subtract": "cut", "cut": "cut", "difference": "cut", "remove": "cut",
    "union": "boss", "fuse": "boss", "boss": "boss",
}
_FLAG_WORDS = {"true": True, "yes": True, "1": True,
               "false": False, "no": False, "0": False}
_PLACEMENT_AT_KEYS = ("at", "origin", "position", "location", "point")
_PLACEMENT_AXIS_KEYS = ("axis", "direction", "normal", "dir")
_PRIM_DIALECT_KEYS = frozenset(
    ("placement",) + _AT_ALIASES + _AXIS_ALIASES + _BOOLEAN_ALIASES
    + tuple(_BOOLEAN_FLAG_MODES))
_AXIS_WORDS = frozenset({"x", "+x", "-x", "y", "+y", "-y", "z", "+z", "-z"})
_BOOLEAN_MODES = {
    "union": "boss", "add": "boss", "join": "boss", "fuse": "boss",
    "merge": "boss", "weld": "boss", "boss": "boss",
    "cut": "cut", "subtract": "cut", "difference": "cut", "remove": "cut",
}
_BOOLEAN_NEW_BODY = frozenset({"new", "none", "new_body", "newbody"})
_BOOLEAN_INTERSECT = frozenset({"intersect", "intersection"})
_BOOLEAN_VALUES = ('union|add|join|fuse|merge|weld -> mode:"boss"; '
                   'cut|subtract|difference|remove -> mode:"cut"; '
                   'new|none|new_body -> merge:false (a separate body)')
_MISSING = object()


def _norm_axis_value(v: Any) -> Any:
    """Canonicalize an axis word ("+X " -> "+x"); a [x,y,z] vector passes through."""
    if isinstance(v, str):
        s = v.strip().lower()
        if s in _AXIS_WORDS:
            return s
    return v


def _primitive_needs_norm(feat: dict, params: dict) -> bool:
    """True when a primitive carries a dialect key (or an un-canonical axis word)."""
    for k in _PRIM_DIALECT_KEYS:
        if k in feat or k in params:
            return True
    for src in (feat.get("axis"), params.get("axis")):
        if isinstance(src, str) and src.strip().lower() != src:
            return True
    return False


def _normalize_primitive_feature(feat: dict) -> dict:
    """Rewrite a dialect primitive add_feature into the canonical placement shape.

    placement/position/direction/boolean -> at / axis / mode / merge. Pure: the
    caller's feature (and its params) are never mutated. When a dialect key and its
    canonical target disagree the CANONICAL key wins and the dialect key is dropped
    (the model asked for the same thing twice; honour the documented spelling).
    """
    out = dict(feat)
    params = feat.get("params")
    new_params = dict(params) if isinstance(params, dict) else {}

    def peek(key: str) -> Any:
        # `params` is merged into the feature by the engine, so either level counts.
        if key in out:
            return out[key]
        return new_params.get(key, _MISSING)

    def consume(key: str) -> Any:
        v = peek(key)
        out.pop(key, None)
        new_params.pop(key, None)
        return v

    at_val: Any = _MISSING
    axis_val: Any = _MISSING

    # placement:{origin|position|location|point:[x,y,z], direction|axis|normal|dir:…}
    # A non-dict `placement` is NOT consumed — we don't understand it, so let the
    # engine's unknown-key error name it rather than dropping the model's intent.
    place = peek("placement")
    if isinstance(place, dict):
        consume("placement")
        for k in _PLACEMENT_AT_KEYS:
            if place.get(k) is not None:
                at_val = place[k]
                break
        for k in _PLACEMENT_AXIS_KEYS:
            if place.get(k) is not None:
                axis_val = place[k]
                break

    at_from: Optional[str] = None
    for k in _AT_ALIASES:
        v = consume(k)
        if at_val is _MISSING and v is not _MISSING and v is not None:
            at_val = v
            at_from = k
    # `center` means the CENTRE, and `at` only means the centre while `centered` is true —
    # so center + centered:false says two contradictory things about the same point. The
    # engine would happily build the base at that point, which is silently NOT what was
    # asked for; the whole reason `center` is honoured at all is that dropping a placement
    # word is worse than rejecting it, and quietly relocating one is worse still.
    if at_from in _CENTER_AT_ALIASES and peek("centered") is False:
        raise ValueError(
            f"add_feature {feat.get('type')}: '{at_from}':{at_val!r} together with "
            f"'centered':false is contradictory — a primitive is built centred and "
            f"'at' moves its CENTRE, but with 'centered':false 'at' names the BASE "
            f"instead. Pick one: 'at':{at_val!r} (that point is the centre), or "
            f"'at':[…],'centered':false (that point is the base/corner).")
    for k in _AXIS_ALIASES:
        v = consume(k)
        if axis_val is _MISSING and v is not _MISSING and v is not None:
            axis_val = v

    if at_val is not _MISSING and peek("at") is _MISSING:
        out["at"] = at_val
    if axis_val is not _MISSING and peek("axis") is _MISSING:
        out["axis"] = axis_val

    # boolean|operation -> mode / merge. An unusable value is an ERROR, not a drop:
    # the model named an operation it believes in, and "intersect" in particular has
    # no primitive mode at all (it would silently fuse instead of intersecting).
    for k in _BOOLEAN_ALIASES:
        v = consume(k)
        if v is _MISSING or v is None:
            continue
        word = str(v).strip().lower()
        if word in _BOOLEAN_INTERSECT:
            raise ValueError(
                f"add_feature {feat.get('type')}: '{k}':'{v}' is not supported — the "
                f"engine has no intersect mode for primitives. Supported: "
                f"{_BOOLEAN_VALUES}")
        if word in _BOOLEAN_MODES:
            if peek("mode") is _MISSING:
                out["mode"] = _BOOLEAN_MODES[word]
        elif word in _BOOLEAN_NEW_BODY:
            if peek("merge") is _MISSING:
                out["merge"] = False
        else:
            raise ValueError(
                f"add_feature {feat.get('type')}: unknown '{k}':'{v}'. Supported: "
                f"{_BOOLEAN_VALUES}")

    # The flag spelling: {"subtract": true} rather than {"operation": "subtract"}.
    # A truthy flag sets the mode; an explicit `mode` already present wins. A value
    # that is neither a bool nor a yes/no word is left in place ON PURPOSE, so the
    # engine names it -- consuming a key we could not interpret would recreate the
    # silent drop this block exists to fix.
    for k in sorted(_BOOLEAN_FLAG_MODES):
        v = peek(k)
        if v is _MISSING:
            continue
        flag = v if isinstance(v, bool) else _FLAG_WORDS.get(str(v).strip().lower(),
                                                             _MISSING)
        if flag is _MISSING:
            continue
        consume(k)
        if flag and peek("mode") is _MISSING:
            out["mode"] = _BOOLEAN_FLAG_MODES[k]

    # Canonicalize the axis WORD wherever it ended up ("+X" -> "+x"); a [x,y,z]
    # vector passes through. Feature level is the canonical home (session.cpp's
    # op_add_feature hoists it into params alongside at/centered/mode/merge).
    axis_out = peek("axis")
    if axis_out is not _MISSING and axis_out is not None:
        if "axis" in out:
            out["axis"] = _norm_axis_value(axis_out)
        else:
            new_params["axis"] = _norm_axis_value(axis_out)

    if isinstance(params, dict) or new_params:
        out["params"] = new_params
    return out


# ---------------------------------------------------------------------------
# Dimension dialect: unit suffixes, diameter->radius, box `size` (model-robustness).
#
# The engine's MODEL SPACE IS MILLIMETRES — `units.system` ("MMGS"/"IPS") is a display
# setting read by the drawing formatter, never a geometry scale — and a dimension may
# reach it as a bare number, a numeric string, {"value":N,"expr":null}, or an EXPRESSION
# string evaluated by core/util/expr.cpp ("width/2"). What it cannot parse is the way a
# person actually states a size: "25mm", "1in", '2.5"' all die in the expression lexer as
# «syntax error in '25mm': trailing tokens». The same run also loses a cylinder spelled by
# `diameter` (the single most common way to describe a shaft or a bore) and a box spelled
# by `size:[l,w,h]`, both hard-rejected as a missing required param.
#
# All three are rewritten HERE, before the call, so the engine's grammar stays closed and
# still receives a clean number. Conservative on purpose:
#   * a string is converted ONLY when it is ENTIRELY a number plus a unit suffix — a real
#     expression ("width/2"), a bare number ("25") and a size code ("M8") pass through
#     untouched for the engine to evaluate;
#   * `diameter` is rewritten PER FEATURE TYPE, only where the engine wants a radius.
#     hole and bolt_circle take a REAL `diameter` param (core/doc/validate.cpp) — halving
#     it there would silently drill a bore at half size, so those types are never touched;
#   * every rewrite is ECHOED back in the response (`interpreted`) so the model learns the
#     canonical spelling instead of leaning on the alias forever.
_UNIT_TO_MM = {
    "mm": 1.0, "millimeter": 1.0, "millimeters": 1.0, "millimetre": 1.0, "millimetres": 1.0,
    "cm": 10.0, "centimeter": 10.0, "centimeters": 10.0, "centimetre": 10.0,
    "centimetres": 10.0,
    "m": 1000.0, "meter": 1000.0, "meters": 1000.0, "metre": 1000.0, "metres": 1000.0,
    "in": 25.4, "inch": 25.4, "inches": 25.4, '"': 25.4, "\u201d": 25.4, "\u2033": 25.4,
    "ft": 304.8, "foot": 304.8, "feet": 304.8, "'": 304.8, "\u2019": 304.8, "\u2032": 304.8,
    "thou": 0.0254, "mil": 0.0254, "mils": 0.0254,
    "um": 0.001, "\u00b5m": 0.001, "\u03bcm": 0.001, "micron": 0.001, "microns": 0.001,
}
# <number><unit>, the WHOLE string. `[^\W\d_]+` is the unicode letter run (so "µm" works);
# the alternation adds the prime marks, which are not letters.
_UNIT_NUM_RE = re.compile(
    r"^\s*(?P<n>[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)"
    r"\s*(?P<u>[^\W\d_]+|[\"'\u2019\u201d\u2032\u2033])\s*$")
# The compound imperial form a person speaks: 5'6", 5 ft 6 in.
_FT_IN_RE = re.compile(
    r"^\s*(?P<ft>\d+(?:\.\d*)?)\s*(?:'|\u2019|\u2032|ft|foot|feet)"
    r"\s*(?P<in>\d+(?:\.\d*)?)\s*(?:\"|\u201d|\u2033|in|inch|inches)?\s*$")
# Params whose value is TEXT, never a dimension — a text feature that embosses the literal
# string "25mm" must not have it silently turned into the number 25.
_NON_DIM_KEYS = frozenset({
    "type", "name", "id", "text", "font", "label", "title", "note", "comment",
    "description", "code", "source", "script", "style", "end", "standard", "tap",
    "mode", "align", "material", "appearance", "system", "unit", "units", "format",
    "plane", "size", "of", "side", "surf", "pick", "op",
})
# diameter -> radius, PER PRIMITIVE TYPE (the engine's radius params, validate.cpp).
# Deliberately absent: hole and bolt_circle, whose `diameter` is the real param.
_DIAMETER_ALIASES = {
    "cylinder": {"diameter": "radius", "dia": "radius", "d": "radius"},
    "sphere": {"diameter": "radius", "dia": "radius", "d": "radius"},
    "cone": {"d1": "r1", "d2": "r2", "diameter": "r1", "dia": "r1",
             "base_diameter": "r1", "bottom_diameter": "r1", "top_diameter": "r2"},
    "torus": {"major_diameter": "major_r", "outer_diameter": "major_r", "od": "major_r",
              "minor_diameter": "minor_r", "tube_diameter": "minor_r",
              "diameter": "major_r"},
}
# Straight RENAMES (no halving): the same dimension under the name a model reaches
# for. Found 2026-07-30 by probing the siblings of the cylinder `depth` bug from
# run-21784882079258 — a cone given radius1/radius2 and a torus given tube_radius
# were both rejected for "required" params they had in fact supplied, and a box
# given length/width/depth was told params.height was missing.
#
# A rename only ever fills a MISSING canonical key (enforced below), so an ambiguous
# spelling can never overwrite what the model said explicitly: a box with
# length+depth+height keeps its height, `depth` stays unconsumed, and the engine
# names the real gap (width) instead of this pass inventing one.
#
# `cylinder` is deliberately absent: _check_cylinder_height already handles its
# height aliases and carries the cylinder-vs-hole teaching that belongs with them.
_PARAM_RENAMES = {
    "cone": {"radius1": "r1", "radius2": "r2", "base_radius": "r1",
             "bottom_radius": "r1", "top_radius": "r2", "radius": "r1",
             "depth": "height", "length": "height"},
    "torus": {"tube_radius": "minor_r", "minor_radius": "minor_r",
              "major_radius": "major_r", "ring_radius": "major_r",
              "radius": "major_r"},
    "box": {"depth": "height", "thickness": "height"},
    # The bolt-circle diameter is the ONE param nothing but the literal key `pcd`
    # reached. core/doc/document.cpp accepts `bolt_circle_d` at compile time, but
    # validate.cpp's required set is {diameter, count, pcd}, so that alias was dead on
    # arrival — the batch was rejected before the compiler ever saw it. Every spelling
    # here is the DIAMETER under another name, so nothing is scaled: a radius spelling
    # is deliberately absent, because filling `pcd` from a radius would silently place
    # the ring at half its size.
    "bolt_circle": {"bolt_circle_d": "pcd", "bolt_circle_diameter": "pcd",
                    "bcd": "pcd", "circle_diameter": "pcd",
                    "pitch_circle_diameter": "pcd", "pattern_diameter": "pcd",
                    "bolt_hole_circle": "pcd"},
}
#: Feature types the dimension-alias pass runs for. It started life as the primitive
#: set, which is why `_PARAM_RENAMES["bolt_circle"]` would otherwise never have fired:
#: the alias tables are keyed by feature type already, so the gate follows them rather
#: than a hand-kept list that has to be remembered twice.
_DIM_ALIAS_TYPES = _PRIMITIVE_TYPES | frozenset(_PARAM_RENAMES) | frozenset(_DIAMETER_ALIASES)
_SIZE_KEYS = ("size", "dimensions", "dimension", "dims")
_BOX_DIMS = ("length", "width", "height")
_MAX_INTERPRETED_NOTES = 8


def _clean_num(x: float) -> Union[int, float]:
    """Kill binary-float noise from a unit conversion; hand back an int when it is one."""
    v = round(x, 9)
    return int(v) if v == int(v) and abs(v) < 1e15 else v


def _dim_mm(s: Any) -> Optional[Union[int, float]]:
    """A plain "<number><unit>" string -> millimetres. None for anything else.

    None means "not a unit literal" — a bare number, an expression ("width/2"), a size
    code ("M8"), a word. Those are the engine's business, not ours.
    """
    if not isinstance(s, str) or not s.strip():
        return None
    m = _UNIT_NUM_RE.match(s)
    if m:
        factor = _UNIT_TO_MM.get(m.group("u").lower())
        if factor is None:
            return None
        return _clean_num(float(m.group("n")) * factor)
    m = _FT_IN_RE.match(s)
    if m:
        return _clean_num(float(m.group("ft")) * 304.8 + float(m.group("in")) * 25.4)
    return None


def _is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _plain_num(v: Any) -> Optional[float]:
    """The number a param carries, however it is spelled: bare, numeric string, or the
    canonical {"value":N,"expr":null} dim object. None for a real expression."""
    if _is_num(v):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v.strip())
        except ValueError:
            return None
    if isinstance(v, dict) and not v.get("expr") and _is_num(v.get("value")):
        return float(v["value"])
    return None


def _is_dim_object(v: Any) -> bool:
    """The canonical dim shape {"value":…,"expr":…} (± an explicit unit field)."""
    return (isinstance(v, dict) and ("value" in v or "expr" in v)
            and not (set(v) - {"value", "expr", "unit", "units"}))


def _convert_units(val: Any, notes: list, label: str, key: str = "", depth: int = 0):
    """Recursively convert unit-suffixed dimension strings to mm. -> (value, changed).

    Pure: an unchanged subtree is returned BY IDENTITY, so a canonical op round-trips
    byte-for-byte and no caller dict is ever copied for nothing.
    """
    if depth > 6:
        return val, False
    where = f"{label}{key}" if key else label.rstrip(".")

    if isinstance(val, str):
        if val.lstrip().startswith("{"):
            try:
                dimension = json.loads(val)
            except (json.JSONDecodeError, ValueError):
                dimension = None
            if _is_dim_object(dimension):
                notes.append(f"{where}: decoded the JSON dimension object")
                converted, _ = _convert_units(dimension, notes, label, key, depth + 1)
                return converted, True
        mm = _dim_mm(val)
        if mm is None:
            return val, False
        notes.append(f"{where}: interpreted {val!r} as {mm} (document units are mm)")
        return mm, True

    if _is_dim_object(val):
        expr = val.get("expr")
        if isinstance(expr, str):
            mm = _dim_mm(expr)
            if mm is not None:
                notes.append(
                    f"{where}: interpreted expr {expr!r} as the value {mm} (mm)")
                return {"value": mm, "expr": None}, True
            return val, False  # a REAL expression — the engine evaluates it
        unit = val.get("unit") or val.get("units")
        if isinstance(unit, str) and _is_num(val.get("value")):
            factor = _UNIT_TO_MM.get(unit.strip().lower())
            if factor is not None:
                mm = _clean_num(float(val["value"]) * factor)
                notes.append(f"{where}: interpreted {val['value']} {unit} as {mm} (mm)")
                return {"value": mm, "expr": None}, True
        if isinstance(val.get("value"), str):
            mm = _dim_mm(val["value"])
            if mm is not None:
                notes.append(
                    f"{where}: interpreted {val['value']!r} as {mm} (document units are mm)")
                out = {k: v for k, v in val.items() if k not in ("unit", "units")}
                out["value"] = mm
                return out, True
        return val, False

    if isinstance(val, list):
        new: list = []
        changed = False
        for item in val:
            nv, ch = _convert_units(item, notes, label, key, depth + 1)
            changed = changed or ch
            new.append(nv)
        return (new, True) if changed else (val, False)

    if isinstance(val, dict):
        new = {}
        changed = False
        for k, v in val.items():
            if str(k).strip().lower() in _NON_DIM_KEYS:
                new[k] = v
                continue
            nv, ch = _convert_units(v, notes, label, str(k), depth + 1)
            changed = changed or ch
            new[k] = nv
        return (new, True) if changed else (val, False)

    return val, False


def _feat_label(feat: dict, ftype: str) -> str:
    """"Boss" / "cylinder" — names the feature a note is about."""
    name = feat.get("name")
    return name if isinstance(name, str) and name else (ftype or "feature")


def _split_two_levels(feat: dict):
    """(feature copy, params copy) plus peek/consume over BOTH levels.

    `params` is merged into the feature by session.cpp::op_add_feature, so a model may put
    a dimension at either level and mean the same thing.
    """
    out = dict(feat)
    params = feat.get("params")
    new_params = dict(params) if isinstance(params, dict) else {}

    def peek(key: str) -> Any:
        if key in out:
            return out[key]
        return new_params.get(key, _MISSING)

    def consume(key: str) -> Any:
        v = peek(key)
        out.pop(key, None)
        new_params.pop(key, None)
        return v

    return out, new_params, peek, consume


def _halve(v: Any):
    """diameter value -> radius value, preserving the shape it arrived in. None = can't."""
    if _is_num(v):
        return _clean_num(float(v) / 2.0)
    if isinstance(v, str):
        mm = _dim_mm(v)
        if mm is not None:
            return _clean_num(float(mm) / 2.0)
        try:
            return _clean_num(float(v.strip()) / 2.0)
        except (TypeError, ValueError):
            # A real expression ("shaft_d"): halve it AS an expression, still the engine's job.
            return {"value": 0.0, "expr": f"({v.strip()})/2"}
    if _is_dim_object(v):
        expr = v.get("expr")
        if isinstance(expr, str) and expr.strip():
            mm = _dim_mm(expr)
            if mm is None:
                return {"value": 0.0, "expr": f"({expr.strip()})/2"}
            return {"value": _clean_num(float(mm) / 2.0), "expr": None}
        inner = _halve(v.get("value"))
        if inner is None:
            return None
        return {"value": inner, "expr": None} if not isinstance(inner, dict) else inner
    return None


def _normalize_primitive_dims(feat: dict, ftype: str, notes: list) -> dict:
    """box `size` shorthand, diameter->radius aliases, and per-type param renames.
    Pure; identity when nothing applies."""
    aliases = _DIAMETER_ALIASES.get(ftype, {})
    renames = _PARAM_RENAMES.get(ftype, {})
    size_key = next((k for k in _SIZE_KEYS
                     if k in feat or (isinstance(feat.get("params"), dict)
                                      and k in feat["params"])), None) if ftype == "box" else None

    def _present(k: str) -> bool:
        return k in feat or (isinstance(feat.get("params"), dict) and k in feat["params"])

    hits = [k for k in aliases if _present(k)]
    rhits = [k for k in renames if _present(k)]
    # A cone with r1 and no r2 needs the apex default below even when no alias fired.
    apex_cone = (ftype == "cone" and _present("r1") and not _present("r2"))
    if not hits and not rhits and not size_key and not apex_cone:
        return feat

    label = _feat_label(feat, ftype)
    out, new_params, peek, consume = _split_two_levels(feat)

    if size_key:
        v = consume(size_key)
        dims: Optional[list] = None
        if isinstance(v, list) and len(v) == 3:
            dims = list(v)
        elif isinstance(v, list) and len(v) == 1:
            dims = [v[0]] * 3
        elif _is_num(v) or isinstance(v, str) or _is_dim_object(v):
            dims = [v] * 3
        if dims is None:
            # An unusable shape — hand it back so the engine names it rather than guessing.
            new_params[size_key] = v
        else:
            written = []
            for name, dv in zip(_BOX_DIMS, dims):
                if peek(name) is not _MISSING:
                    continue  # an explicit length/width/height wins over the shorthand
                # Convert a unit literal HERE so the split reads as one rewrite
                # ("size '1in' as a 25.4 cube") instead of one note per side.
                mm = _dim_mm(dv)
                new_params[name] = dv if mm is None else mm
                written.append(f"{name} {new_params[name]}")
            if written:
                shape = "a cube" if len(set(map(str, dims))) == 1 else "l/w/h"
                notes.append(f"{label}: interpreted {size_key} {v!r} as {shape}: "
                             + ", ".join(written) + " — a box takes length/width/height")

    for key in sorted(hits):
        target = aliases[key]
        v = consume(key)
        if v is _MISSING or v is None:
            continue
        if peek(target) is not _MISSING:
            notes.append(f"{label}: ignored {key} (the canonical {target} was also given)")
            continue
        r = _halve(v)
        if r is None:
            new_params[key] = v  # unusable — let the engine report it
            continue
        new_params[target] = r
        shown = r.get("expr") if isinstance(r, dict) and r.get("expr") else (
            r["value"] if isinstance(r, dict) else r)
        given = v["value"] if _is_dim_object(v) and "value" in v else v
        notes.append(f"{label}: interpreted {key} {given!r} as {target} {shown!r} "
                     f"— a {ftype} is dimensioned by radius, not diameter")

    for key in sorted(rhits):
        target = renames[key]
        if peek(target) is not _MISSING:
            continue  # the canonical name was given too — leave the spare alone
        v = consume(key)
        if v is _MISSING or v is None:
            continue
        new_params[target] = v
        notes.append(f"{label}: read {key} as {target} — a {ftype} is dimensioned by "
                     f"{target}")

    # A cone given ONE radius is the textbook cone: base r1, apex on top. The engine
    # requires both radii (it builds frustums too), so {"type":"cone","radius":20,
    # "height":40} was refused with "params.r1: required; params.r2: required" and no
    # hint that r2 is the TOP radius or that a point means 0. A model that wanted a
    # frustum states two radii, so one radius is unambiguous — but it is disclosed,
    # never silent.
    if ftype == "cone" and peek("r1") is not _MISSING and peek("r2") is _MISSING:
        new_params["r2"] = 0
        notes.append(f"{label}: read a single radius as a cone with a POINT on top "
                     f"(r1 = base, r2 = 0). Give r2 for a truncated cone/frustum")

    if isinstance(feat.get("params"), dict) or new_params:
        out["params"] = new_params
    return out


def _normalize_feature_dims(feat: dict, ftype: str, notes: list) -> dict:
    """Every dimension dialect on one feature: box size, then units, then diameter.

    ORDER MATTERS. `size:["40mm",…]` must be split before the unit pass can see its
    elements, and `diameter:"50mm"` must already be the number 50 before it is halved.
    """
    out = feat
    if ftype in _DIM_ALIAS_TYPES:
        out = _normalize_primitive_dims(out, ftype, notes)
    converted, changed = _convert_units(out, notes, _feat_label(feat, ftype) + ".")
    if changed:
        out = converted
    if ftype in _DIM_ALIAS_TYPES:
        out = _normalize_primitive_dims(out, ftype, notes)
    return out


def _normalize_feature_ops(ops: Any, notes: Optional[list] = None) -> Any:
    """Normalize dialect primitive placement/orientation + dimension + schema ops.

    `notes` (optional) collects a plain-English line per rewrite, echoed to the model as
    `interpreted` so it learns the canonical spelling. Returns a new list.
    """
    if not isinstance(ops, list):
        return ops
    sink = notes if isinstance(notes, list) else []
    out = []
    for op in ops:
        try:
            if isinstance(op, dict) and op.get("op") == "add_feature" and isinstance(
                    op.get("feature"), dict):
                feat = op["feature"]
                ftype = str(feat.get("type") or "").strip().lower()
                params = feat.get("params") if isinstance(feat.get("params"), dict) else {}
                new = feat
                if ftype in _PRIMITIVE_TYPES and _primitive_needs_norm(feat, params):
                    new = _normalize_primitive_feature(feat)
                new = _normalize_feature_dims(new, ftype, sink)
                # Last: the requirements the engine enforces and the model forgets
                # (hole style/end/face, boolean op, extrude/revolve mode + profile ref,
                # datum words, kind-less selectors, {"value":[…]} around a coordinate).
                new = _normalize_feature_schema(new, ftype, sink)
                if new is not feat:
                    op = {**op, "feature": new}
            elif (isinstance(op, dict) and op.get("op") == "edit_feature"
                    and isinstance(op.get("params"), dict)):
                # An edit carries bare params (no feature wrapper) and no type, so only the
                # type-independent unit pass applies — never the per-type diameter alias.
                label = str(op.get("id") or "edit_feature") + "."
                conv, changed = _convert_units(op["params"], sink, label)
                if changed:
                    op = {**op, "params": conv}
        except ValueError:
            raise  # an unsupported request (boolean:"intersect") — tell the model
        except Exception:  # noqa: BLE001 — never let normalization break a valid call
            pass
        out.append(op)
    return out


def _interpreted_notes(notes: list) -> list:
    """De-duplicate and cap the echo list (a 20-hole batch must not bury the response)."""
    seen: list = []
    for n in notes:
        if n not in seen:
            seen.append(n)
    if len(seen) > _MAX_INTERPRETED_NOTES:
        extra = len(seen) - _MAX_INTERPRETED_NOTES
        seen = seen[:_MAX_INTERPRETED_NOTES] + [f"(+{extra} more of the same kind)"]
    return seen


# ---------------------------------------------------------------------------
# OP-ELEMENT + FEATURE-TYPE normalizer (model-robustness).
#
# The canonical batch element is {"op":"add_feature","feature":{"type":…}}. The single
# most common live rejection in the recorded corpus is one level flatter than that —
# ops=[{"type":"box","name":"Plate","params":{…}}], which dies as «op[0]: missing 'op'»
# — closely followed by the model using a feature TYPE as the op name
# ({"op":"box",…}) and by a type spelled the way a person says it ("cube", "cylindrical",
# "cut_cylinder"). All three are unambiguous, so they are rewritten here instead of
# rejected; each rewrite is echoed ONCE per batch in `interpreted` (the notes are
# de-duplicated, so a 20-element batch adds one line, not twenty).
#: A pattern names its own KIND in a param, and `_FEATURE_TYPE_ALIASES` blindly mapped the
#: generic word to linear_pattern. Live run 2026-07-28:
#: {"type":"pattern","params":{"mode":"circular","axis":"z","count":12,"angle":360}}
#: became a LINEAR pattern and was rejected for the four linear fields it never had
#: (count1/spacing1/dir1/features) — none of which the model had any reason to send.
_PATTERN_KIND_WORDS = {
    "circular": "circular_pattern", "polar": "circular_pattern",
    "radial": "circular_pattern", "angular": "circular_pattern",
    "revolved": "circular_pattern", "rotational": "circular_pattern",
    "axial": "circular_pattern",
    "linear": "linear_pattern", "rectangular": "linear_pattern",
    "rect": "linear_pattern", "grid": "linear_pattern", "row": "linear_pattern",
    "directional": "linear_pattern",
}
_GENERIC_PATTERN_WORDS = frozenset({"pattern", "array", "repeat", "duplicate"})
_PATTERN_KIND_KEYS = ("mode", "kind", "pattern_type", "pattern", "arrangement", "style",
                      "direction_type")
#: Engine feature types whose OWN canonical name contains a generic pattern word, and which
#: are therefore NOT dialect spellings of linear_pattern/circular_pattern. Live probe
#: 2026-07-30: {"type":"sm_flat_pattern"} — the exact type the sheetmetal cheatsheet tells
#: the model to send to unfold the blank — hit the generic branch below on the word
#: "pattern", was rewritten to a linear_pattern, and was then rejected for
#: count1/spacing1/dir1/features. The sheet-metal flat pattern could not be built AT ALL
#: through add_features. The two asm_*_pattern component patterns fall the same way (their
#: seeds live in refs.components, not refs.features), so they are guarded here too.
_PATTERN_NAMED_NON_PATTERN_TYPES = frozenset({
    "sm_flat_pattern", "asm_linear_pattern", "asm_circular_pattern",
})
#: What a model calls the thing being patterned. The engine wants refs.features, a LIST.
_PATTERN_SOURCE_KEYS = ("source", "seed", "feature", "features", "of", "target",
                        "targets", "entities", "items")
#: linear_pattern's params are count1/spacing1; circular_pattern's are count/angle.
_LINEAR_COUNT_KEYS = ("count1", "count", "instances", "n", "copies", "quantity", "num")
_LINEAR_SPACING_KEYS = ("spacing1", "spacing", "distance", "pitch", "step", "gap")
#: Direction 1 and direction 2 are DIFFERENT slots (refs.dir1 / refs.dir2), so their
#: spellings must stay apart — a dir1 lookup that swallowed "direction2" would build a
#: single row along the wrong axis. The numbered forms are what a model writes for a
#: two-way grid ("Direction 1"/"Direction 2" is the CAD-UI wording); without them the
#: only way to express the second direction was the literal key `dir2`, so a grid
#: pattern was unreachable in every dialect but the canonical one.
_LINEAR_DIR_KEYS = ("dir1", "dir", "direction", "axis", "along", "vector",
                    "direction1", "direction_1", "dir_1", "axis1")
_LINEAR_DIR2_KEYS = ("dir2", "direction2", "direction_2", "dir_2", "axis2")
_CIRCULAR_COUNT_KEYS = ("count", "count1", "instances", "n", "copies", "quantity", "num")
_CIRCULAR_ANGLE_KEYS = ("angle", "total_angle", "span", "sweep", "arc", "extent")
_CIRCULAR_AXIS_KEYS = ("axis", "about", "around", "axis1", "dir1", "direction")
_PATTERN_CONSUMED_KEYS = frozenset(
    _PATTERN_KIND_KEYS + _PATTERN_SOURCE_KEYS + _LINEAR_COUNT_KEYS + _LINEAR_SPACING_KEYS
    + _LINEAR_DIR_KEYS + _LINEAR_DIR2_KEYS + _CIRCULAR_COUNT_KEYS + _CIRCULAR_ANGLE_KEYS
    + _CIRCULAR_AXIS_KEYS)


def _param_or_ref(feat: dict, params: dict, key: str) -> Any:
    """A value from params or refs, whichever bag the model used."""
    v = params.get(key)
    if v is not None:
        return v
    refs = feat.get("refs")
    return refs.get(key) if isinstance(refs, dict) else None


def _pattern_kind(feat: dict, params: dict) -> Optional[str]:
    """The pattern feature type this feature MEANS, or None when it is not a pattern."""
    tword = _slug(feat.get("type"))
    if tword in ("linear_pattern", "circular_pattern"):
        return tword
    # A canonical engine type that merely CONTAINS a pattern word is itself, not a dialect
    # spelling of a part-level pattern. Checked against the live vocabulary too, so a future
    # `*_pattern` feature type is safe the day it lands.
    if tword in _PATTERN_NAMED_NON_PATTERN_TYPES:
        return None
    vocab, live = _feature_type_vocab()
    if live and tword in vocab:
        return None
    if tword in _PATTERN_KIND_WORDS:
        return _PATTERN_KIND_WORDS[tword]
    # A QUALIFIED spelling — "polar_pattern", "pattern_circular", "rectangular_array".
    # Strip the generic noun and read the qualifier that is left.
    words = [w for w in tword.split("_") if w]
    if any(w in _GENERIC_PATTERN_WORDS for w in words):
        for w in words:
            if w in _PATTERN_KIND_WORDS:
                return _PATTERN_KIND_WORDS[w]
    elif tword not in _GENERIC_PATTERN_WORDS:
        return None
    for k in _PATTERN_KIND_KEYS:
        kind = _PATTERN_KIND_WORDS.get(_slug(params.get(k)))
        if kind:
            return kind
    # A generic pattern naming an ANGLE is polar; otherwise linear, which is also what the
    # bare word aliased to before this existed — so nothing that worked before changes.
    if any(params.get(k) is not None for k in ("angle", "total_angle", "sweep")):
        return "circular_pattern"
    return "linear_pattern"


def _pattern_source_refs(feat: dict, params: dict) -> Optional[list]:
    """The feature list being patterned, from wherever the model put it."""
    refs = feat.get("refs") if isinstance(feat.get("refs"), dict) else {}
    for holder in (refs, params):
        for k in _PATTERN_SOURCE_KEYS:
            v = holder.get(k)
            if isinstance(v, str) and v.strip():
                return [v.strip()]
            if isinstance(v, list) and v:
                return list(v)
    return None


def _normalize_pattern_feature(feat: dict, notes: list) -> dict:
    """A pattern in any dialect -> linear_pattern/circular_pattern with its real schema."""
    params = dict(feat.get("params")) if isinstance(feat.get("params"), dict) else {}
    kind = _pattern_kind(feat, params)
    if kind is None:
        return feat
    label = _feat_label(feat, kind)
    refs_in = dict(feat.get("refs")) if isinstance(feat.get("refs"), dict) else {}
    src = _pattern_source_refs(feat, params)
    out_params: dict = {}
    out_refs: dict = {}
    if kind == "circular_pattern":
        for dest, keys in (("count", _CIRCULAR_COUNT_KEYS), ("angle", _CIRCULAR_ANGLE_KEYS)):
            val = next((v for k in keys if (v := _param_or_ref(feat, params, k)) is not None),
                       None)
            if val is not None:
                out_params[dest] = val
        # "pattern this 12 times around Z" means a full revolution, and `angle` is REQUIRED
        # — defaulting it beats rejecting a request that is otherwise complete.
        out_params.setdefault("angle", 360)
        axis = next((v for k in _CIRCULAR_AXIS_KEYS
                     if (v := _param_or_ref(feat, params, k)) is not None), None)
        if axis is not None:
            out_refs["axis"] = axis
    else:
        for dest, keys in (("count1", _LINEAR_COUNT_KEYS),
                           ("spacing1", _LINEAR_SPACING_KEYS)):
            val = next((v for k in keys if (v := _param_or_ref(feat, params, k)) is not None),
                       None)
            if val is not None:
                out_params[dest] = val
        d1 = next((v for k in _LINEAR_DIR_KEYS
                   if (v := _param_or_ref(feat, params, k)) is not None), None)
        if d1 is not None:
            out_refs["dir1"] = d1
        for k in ("count2", "spacing2"):
            if params.get(k) is not None:
                out_params[k] = params[k]
        d2 = next((v for k in _LINEAR_DIR2_KEYS
                   if (v := _param_or_ref(feat, params, k)) is not None), None)
        if d2 is not None:
            out_refs["dir2"] = d2
    # Strip every alias FIRST, then lay the canonical values on top — the destination key
    # is itself an alias (refs.axis, params.count), so cleaning last erased the answer.
    for k in _PATTERN_CONSUMED_KEYS:
        params.pop(k, None)
        refs_in.pop(k, None)
    refs = {**refs_in, **out_refs}
    if src is not None:
        refs["features"] = src
    out = {**feat, "type": kind, "params": {**params, **out_params}}
    if refs:
        out["refs"] = refs
    else:
        out.pop("refs", None)
    if out == feat:
        return feat
    shape = ("count+angle with refs.axis" if kind == "circular_pattern"
             else "count1+spacing1 with refs.dir1")
    notes.append(
        f"{label}: read this as a {kind} — moved its size onto that schema ({shape}) and "
        f"the patterned feature(s) into refs.features. A pattern names its kind in the "
        f'TYPE; the generic word "pattern" used to default to LINEAR regardless of mode')
    return out


#: Feature types whose body is a standalone primitive and therefore carries `merge`.
#: Only these can be separated after the fact; anything else keeps the engine's error.
_MERGEABLE_PRIMITIVES = frozenset({"box", "cylinder", "cone", "sphere", "torus"})
_FEAT_ID_RE = re.compile(r"^f\d+$")


def _repair_merged_boolean_operands(ops: Any, doc_id: str, notes: list) -> Any:
    """Separate the bodies an EXISTING-feature boolean needs, before the engine refuses.

    A default primitive fuses into the running body, so a boolean naming it dies on
    «operand 'f3' was already merged into another body — build that body with
    "merge":false». A model cannot act on that: f3 is already built, and rebuilding it is
    not something it knows how to express. The engine's own instruction IS the fix, and
    edit_feature can apply it, so do that — keeping the EARLIEST operand as the base body
    and separating the later ones, which are the tools. Live run 2026-07-28: four
    consecutive batches died on this one sentence.
    """
    if not isinstance(ops, list):
        return ops
    wanted: set = set()
    for op in ops:
        feat = op.get("feature") if isinstance(op, dict) else None
        if not isinstance(feat, dict) or _slug(feat.get("type")) != "boolean":
            continue
        params = feat.get("params") if isinstance(feat.get("params"), dict) else {}
        ids = [v.strip() for v in (params.get("a"), params.get("b"))
               if isinstance(v, str) and _FEAT_ID_RE.match(v.strip())]
        if len(ids) == 2:
            wanted.update(ids)
    if not wanted:
        return ops

    try:
        # doc_tree hands back BOTH the feature list and the live owner map in one call —
        # there is no doc_get/doc_owner_map method.
        snap = cb.call("doc_tree", {"doc_id": doc_id}) or {}
        tree = snap.get("tree") or {}
        owner = snap.get("owner_map") or {}
    except Exception as exc:  # noqa: BLE001
        if getattr(exc, "code", None) == "engine_connection_lost":
            raise
        return ops
    bodies_of = {fid: set(v.get("bodies") or ())
                 for fid, v in owner.items() if isinstance(v, dict)}
    order = {f.get("id"): i for i, f in enumerate(tree.get("features") or [])
             if isinstance(f, dict)}
    types = {f.get("id"): _slug(f.get("type")) for f in (tree.get("features") or [])
             if isinstance(f, dict)}
    # Operands sharing a body are ONE solid; the earliest is the base, the rest are tools.
    shared = [fid for fid in wanted
              if any(fid != other and bodies_of.get(fid)
                     and bodies_of.get(fid) == bodies_of.get(other) for other in wanted)]
    if len(shared) < 2:
        return ops
    tools = sorted(shared, key=lambda f: order.get(f, 0))[1:]
    fixes = [{"op": "edit_feature", "id": fid, "params": {"merge": False}}
             for fid in tools if types.get(fid) in _MERGEABLE_PRIMITIVES]
    if not fixes:
        return ops
    notes.append(
        "separated " + ", ".join(f["id"] for f in fixes) + " into its own body with "
        'merge:false before the boolean — a primitive built with the default mode:"boss" '
        "fuses into the running solid, so a boolean naming it has no second operand left")
    return fixes + list(ops)


# ------------------------------------------------------------------ assembly re-pose
# `asm_edit_component` writes ONLY transform / name / suppressed / fixed / hidden
# (core/doc/session.cpp::op_asm_edit_component) and silently ignores every other key, so
# the at/rotate pose dialect — which is asm_add_component's OWN public signature — applied
# "ok" and moved nothing. Translate it into the matrix the op actually reads.
_ASM_POSE_AT_KEYS = ("at", "position", "origin", "translate", "location", "xyz")
_ASM_POSE_AXIS_KEYS = ("rotate", "axis", "rotation_axis", "about")
_ASM_POSE_ANGLE_KEYS = ("angle", "degrees", "deg", "rotation_angle")


def _asm_pose_vec(op: dict, keys: tuple) -> Optional[list]:
    for k in keys:
        v = op.get(k)
        if isinstance(v, dict):
            v = v.get("value")
        if isinstance(v, (list, tuple)) and 2 <= len(v) <= 3 and all(_is_num(x) for x in v):
            return [float(x) for x in v] + [0.0] * (3 - len(v))
    return None


def _normalize_asm_component_pose(ops: Any, notes: list) -> Any:
    """`{"op":"asm_edit_component","id":"k2","at":[0,0,20]}` -> the `transform` matrix."""
    if not isinstance(ops, list):
        return ops
    out: list = []
    changed = False
    for op in ops:
        if not isinstance(op, dict) or _slug(op.get("op")) != "asm_edit_component":
            out.append(op)
            continue
        at = _asm_pose_vec(op, _ASM_POSE_AT_KEYS)
        axis = _asm_pose_vec(op, _ASM_POSE_AXIS_KEYS)
        angle = next((float(v) for k in _ASM_POSE_ANGLE_KEYS
                      if _is_num(v := op.get(k))), 0.0)
        if at is None and axis is None:
            out.append(op)
            continue
        fixed = dict(op)
        for k in _ASM_POSE_AT_KEYS + _ASM_POSE_AXIS_KEYS + _ASM_POSE_ANGLE_KEYS:
            fixed.pop(k, None)
        if isinstance(op.get("transform"), (list, tuple)) and len(op["transform"]) == 16:
            notes.append(
                f"asm_edit_component '{op.get('id')}': kept the explicit 16-value transform "
                f"and dropped the at/rotate keys beside it (the op reads only `transform`)")
        else:
            fixed["transform"] = pose_to_transform16(at, axis, angle)
            notes.append(
                f"asm_edit_component '{op.get('id')}': built its `transform` from "
                f"at={at} rotate={axis} angle={angle} — the op reads a row-major 4x4 and "
                f"IGNORES at/rotate, so that spelling used to apply cleanly and move nothing")
        out.append(fixed)
        changed = True
    return out if changed else ops


# The engine reads asm_add_mate ONLY as {"op":"asm_add_mate","mate":{type, align, refs, value}}
# with refs [{component, selector}], and a selector must be an engine match
# (core/doc/session.cpp::op_asm_add_mate). Live 2026-10-10 (six-axis arm): a flat op
# {op, type, align, refs} failed "requires a 'mate' object", then refs [{component, query}]
# failed "selector must be an object". asm_mate takes that exact operand, so the same
# resolver (cad_asm_tools.resolve_mate_ref) turns it into the engine selector here.
_MATE_REF_QUERY_KEYS = ("query", "sid")


def _normalize_asm_mate_ops(ops: Any, doc_id: Optional[str], notes: list) -> Any:
    """Wrap a flat asm_add_mate into `mate`; resolve {component, query|sid} refs."""
    if not isinstance(ops, list):
        return ops
    out: list = []
    changed = False
    comps: Optional[dict] = None
    for index, op in enumerate(ops):
        if not isinstance(op, dict) or _slug(op.get("op")) != "asm_add_mate":
            out.append(op)
            continue
        fixed = dict(op)
        if "mate" not in fixed:
            body = {k: v for k, v in fixed.items() if k != "op"}
            if body:
                fixed = {"op": fixed["op"], "mate": body}
                notes.append(
                    f"asm_add_mate ${index}: put {sorted(body)} into the `mate` object. "
                    "The op reads only op.mate.")
        mate = fixed.get("mate")
        if isinstance(mate, dict) and _is_num(mate.get("value")):
            # asm_mate's `value` is a number; the stored mate value is a dimension object.
            mate = {**mate, "value": {"value": float(mate["value"]), "expr": None}}
            fixed["mate"] = mate
            notes.append(f"asm_add_mate ${index}: stored the number `value` as "
                         "{value, expr}. A mate value is a dimension object.")
        refs = mate.get("refs") if isinstance(mate, dict) else None
        pending = [j for j, ref in enumerate(refs or ())
                   if isinstance(ref, dict) and "selector" not in ref
                   and isinstance(ref.get("component"), str)
                   and any(k in ref for k in _MATE_REF_QUERY_KEYS)]
        if pending and doc_id:
            from .cad_asm_tools import resolve_mate_ref  # noqa: PLC0415
            if comps is None:
                tree = (cb.call("doc_tree", {"doc_id": doc_id}) or {}).get("tree") or {}
                comps = {c.get("id"): c for c in tree.get("components") or []
                         if isinstance(c, dict)}
            new_refs = list(refs)
            for j in pending:
                ref = refs[j]
                if ref["component"] not in comps:
                    continue  # a same-batch "$N" component: the engine names it
                resolved = resolve_mate_ref(doc_id, comps, f"asm_add_mate ${index} refs[{j}]",
                                            ref)
                if "error" in resolved:
                    raise ValueError(resolved["error"] + (
                        f" Candidates: {json.dumps(resolved['candidates'])}"
                        if resolved.get("candidates") else ""))
                new_refs[j] = resolved["ref"]
            if new_refs != refs:
                fixed["mate"] = {**mate, "refs": new_refs}
                notes.append(
                    f"asm_add_mate ${index}: resolved the ref query against each component's "
                    "source part, as asm_mate does.")
        if fixed != op:
            changed = True
        out.append(fixed)
    return out if changed else ops


def _asm_transform_edits(ops: Any) -> dict:
    """{component_id: requested row-major 4x4} for every asm_edit_component pose in a batch."""
    edits: dict = {}
    if not isinstance(ops, list):
        return edits
    for op in ops:
        if not isinstance(op, dict) or _slug(op.get("op")) != "asm_edit_component":
            continue
        cid, tf = op.get("id"), op.get("transform")
        if isinstance(cid, str) and cid and isinstance(tf, (list, tuple)) and len(tf) == 16 \
                and all(_is_num(v) for v in tf):
            edits[cid] = [float(v) for v in tf]
    return edits


def _asm_repose_refusal(ops: Any, doc_id: str) -> Optional[dict]:
    """Refuse a transform edit the MATE SOLVER will immediately overwrite.

    A component that is not `fixed` and is referenced by a live mate owes its pose to
    ``mate::solve_mates``: the doc `transform` is only a solver SEED, and the rebuild that
    every ``doc_apply_ops`` runs writes the solved pose straight back over it
    (core/doc/session.cpp::rebuild_assembly). The op reports ok, the component never
    moves — the exact "asm_edit_component no-ops on this document" a live session hit.
    Say so, machine-readably, instead of succeeding at nothing.
    """
    edits = _asm_transform_edits(ops)
    if not edits:
        return None
    try:
        tree = (cb.call("doc_tree", {"doc_id": doc_id}) or {}).get("tree") or {}
    except Exception as exc:  # noqa: BLE001
        if getattr(exc, "code", None) == "engine_connection_lost":
            raise
        return None
    comps = {c.get("id"): c for c in (tree.get("components") or []) if isinstance(c, dict)}
    mated: dict = {}
    for m in tree.get("mates") or []:
        if not isinstance(m, dict) or m.get("suppressed"):
            continue
        for ref in m.get("refs") or []:
            if isinstance(ref, dict) and isinstance(ref.get("component"), str):
                mated.setdefault(ref["component"], []).append(m.get("id"))
    # An op that grounds the component in the SAME breath is not blocked: a fixed frame's
    # pose is authored, never solved (mate.cpp keeps t0/w0 for a fixed frame).
    grounding = {op.get("id") for op in ops
                 if isinstance(op, dict) and _slug(op.get("op")) == "asm_edit_component"
                 and op.get("fixed") is True}
    blocked = {cid: [m for m in mated[cid] if m]
               for cid in edits
               if cid in mated and cid not in grounding
               and not (comps.get(cid) or {}).get("fixed", False)}
    if not blocked:
        return None
    named = ", ".join(f"{cid} (mated by {', '.join(ms) or 'a mate'})"
                      for cid, ms in sorted(blocked.items()))
    first_mate = next((ms[0] for ms in blocked.values() if ms), "m1")
    return {
        "ok": False,
        "reason": "component_is_mated",
        "doc_id": doc_id,
        "components": blocked,
        "applied": False,
        "error": (
            f"asm_edit_component cannot re-pose {named}: a component that is not `fixed` "
            f"and carries a mate owes its pose to the mate solver. Its `transform` is only "
            f"a solver seed — the rebuild that follows the op writes the solved pose back "
            f"over it, so the edit applies 'ok' and the component never moves. Nothing was "
            f"changed."),
        "hint": (
            "to re-pose a MATED component, move the MATE, not the matrix: "
            f'(1) drive it — add_features(ops=[{{"op":"asm_edit_mate","id":"{first_mate}",'
            '"value":{"value":<mm|deg>}}]) then solve_mates(); '
            f'(2) explore a range without mutating the doc — simulate_motion(mate="{first_mate}", '
            "start=…, stop=…) returns a pose per step; "
            "(3) really want a free pose? take the mate out of the network first "
            f'(add_features(ops=[{{"op":"asm_edit_mate","id":"{first_mate}","suppressed":true}}]) '
            f'or {{"op":"asm_delete_mate","id":"{first_mate}"}}) and send the transform after; '
            'or ground the component in the same batch with "fixed": true, which makes its '
            "transform authoritative."),
    }


def _asm_transform_rejects(ops: Any, tree: Any) -> list:
    """Components whose requested pose is NOT what the doc carries after the apply.

    The engine seeds each mate solve from ``Session::asm_warm`` (the previous solve's
    params) and that map wins over ``components[].transform`` for EVERY component, fixed
    ones included — so once any solve has run on the doc (i.e. after the first
    ``doc_apply_ops``), an authored transform is discarded on the very rebuild the op
    triggers. Verified against a private hostd. Never report success for that.
    """
    edits = _asm_transform_edits(ops)
    if not edits or not isinstance(tree, dict):
        return []
    live = {c.get("id"): c.get("transform") for c in (tree.get("components") or [])
            if isinstance(c, dict)}
    rejects = []
    for cid, want in edits.items():
        got = live.get(cid)
        if not isinstance(got, (list, tuple)) or len(got) != 16:
            continue
        if any(abs(float(a) - float(b)) > 1e-6 * max(1.0, abs(float(b)))
               for a, b in zip(got, want)):
            rejects.append({"component": cid, "requested": want,
                            "actual": [float(v) for v in got]})
    return rejects


def _normalize_pattern_ops(ops: Any, notes: list) -> Any:
    """Run the pattern normalizer over the batch, before the type alias table sees it."""
    if not isinstance(ops, list):
        return ops
    out, changed = [], False
    for op in ops:
        feat = op.get("feature") if isinstance(op, dict) else None
        if not isinstance(feat, dict):
            out.append(op)
            continue
        new = _normalize_pattern_feature(feat, notes)
        changed = changed or new is not feat
        out.append(op if new is feat else {**op, "feature": new})
    return out if changed else ops


_FEATURE_TYPE_ALIASES = {
    "cube": "box", "cuboid": "box", "rect_prism": "box", "rectangular_prism": "box",
    "block": "box", "rectangle": "box", "prism": "box", "plate": "box", "slab": "box",
    "circle": "cylinder", "disc": "cylinder", "disk": "cylinder", "cylindrical": "cylinder",
    "rod": "cylinder", "shaft": "cylinder", "tube": "cylinder", "pipe": "cylinder",
    "round_bar": "cylinder", "boss": "cylinder",
    "ball": "sphere", "spherical": "sphere",
    "conical": "cone", "frustum": "cone", "taper": "cone",
    "toroid": "torus", "ring": "torus", "donut": "torus",
    "bore": "hole", "drill": "hole", "counterbore": "hole", "countersink": "hole",
    "cbore": "hole", "csink": "hole", "tapped_hole": "hole", "clearance_hole": "hole",
    "pocket": "extrude", "profile": "sketch", "sketch2d": "sketch", "sketch_2d": "sketch",
    "round": "fillet", "blend": "fillet", "bevel": "chamfer", "hollow": "shell",
    "pattern": "linear_pattern", "array": "linear_pattern",
    "polar_pattern": "circular_pattern", "revolved_pattern": "circular_pattern",
    "engrave": "text", "emboss": "text", "extrusion": "extrude", "revolution": "revolve",
}
# An alias that also implies a boolean MODE ("a pocket" is an extrude that removes material).
_TYPE_ALIAS_MODE = {"pocket": "cut"}
# An alias that also pins the hole STYLE the word names.
_TYPE_ALIAS_HOLE_STYLE = {
    "counterbore": "cbore", "cbore": "cbore",
    "countersink": "csink", "csink": "csink",
    "tapped_hole": "tap",
}
# "<something>_cut" / "cut_<something>" = that shape, subtracted.
_CUT_TYPE_PREFIXES = ("cut_", "remove_", "subtract_", "negative_")
_CUT_TYPE_SUFFIXES = ("_cut", "_pocket", "_subtract", "_removal")
# A boolean OPERATION written where a feature type belongs — never a type, always a mode.
_BOOLEAN_AS_TYPE = frozenset({
    "cut", "subtract", "difference", "remove", "union", "add", "join", "fuse", "merge",
    "intersect", "intersection", "common", "combine",
})
# Suffix noise a model adds to a real type ("box3D", "cylinder_solid").
_TYPE_NOISE_SUFFIXES = ("_3d", "3d", "_solid", "_feature", "_prim", "_primitive", "_shape")


def _feature_type_vocab() -> tuple[frozenset, bool]:
    """(the engine's feature types, is-it-live?).

    Sourced from `doc_list_ops` via op_vocab — the same single source the docstring's OP
    VOCABULARY block quotes — with op_registry's COMMON_FEATURE_TYPES as the offline
    fallback. The flag matters: the fallback is a SUBSET, so an unknown type may only be
    REJECTED here when the list came from the live engine.
    """
    types: list = []
    try:
        from services.mcp import op_vocab as _ov  # noqa: PLC0415
        types = [str(t) for t in _ov.feature_types() if t]
    except Exception:  # noqa: BLE001
        types = []
    fallback: list = []
    try:
        from services.mcp.op_registry import COMMON_FEATURE_TYPES  # noqa: PLC0415
        fallback = list(COMMON_FEATURE_TYPES)
    except Exception:  # noqa: BLE001
        fallback = []
    live = bool(types) and set(types) != set(fallback)
    return frozenset(types or fallback), live


def _doc_op_vocab() -> frozenset:
    """The engine's op names (doc_list_ops, op_registry fallback)."""
    try:
        from services.mcp import op_vocab as _ov  # noqa: PLC0415
        ops = [str(o) for o in _ov.doc_ops() if o]
        if ops:
            return frozenset(ops)
    except Exception:  # noqa: BLE001
        pass
    try:
        from services.mcp.op_registry import DOC_OP_KINDS  # noqa: PLC0415
        return frozenset(DOC_OP_KINDS)
    except Exception:  # noqa: BLE001
        return frozenset()


def _slug(word: Any) -> str:
    return str(word or "").strip().lower().replace("-", "_").replace(" ", "_")


def _canon_feature_type(word: Any, vocab: frozenset) -> Optional[str]:
    """A word -> an engine feature type, or None. Pure (alias table + suffix noise)."""
    w = _slug(word)
    if not w:
        return None
    if w in vocab:
        return w
    if w in _FEATURE_TYPE_ALIASES:
        return _FEATURE_TYPE_ALIASES[w]
    for suf in _TYPE_NOISE_SUFFIXES:
        if w.endswith(suf) and len(w) > len(suf):
            base = w[: -len(suf)]
            if base in vocab:
                return base
            if base in _FEATURE_TYPE_ALIASES:
                return _FEATURE_TYPE_ALIASES[base]
    if w.endswith("s") and len(w) > 1:  # "boxes" is not a thing, but "holes" is written
        base = w[:-1]
        if base in vocab:
            return base
        if base in _FEATURE_TYPE_ALIASES:
            return _FEATURE_TYPE_ALIASES[base]
    return None


def _feature_type_error(raw: Any, vocab: frozenset) -> str:
    """The unknown-feature-type message: closest matches FIRST, then the real vocabulary."""
    import difflib  # noqa: PLC0415 — failure path only
    known = sorted(vocab)
    near = difflib.get_close_matches(_slug(raw), known, n=4, cutoff=0.4)
    head = f'add_feature: "{raw}" is not a feature type.'
    if near:
        head += " Closest: " + ", ".join(near) + "."
    return (f"{head} A feature type goes in feature.type, never in op — "
            f'{{"op":"add_feature","feature":{{"type":"box","name":"Block",'
            f'"params":{{…}}}}}}. Feature types: ' + ", ".join(known) + ".")


def _retype_feature(feat: dict, vocab: frozenset, live: bool, notes: list) -> dict:
    """Map a natural type word onto an engine feature type. Identity when already canonical."""
    raw = feat.get("type")
    if isinstance(raw, str) and raw.strip() in vocab:
        return feat  # canonical — byte-identical
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError(
            'add_feature: the feature has no "type". Every feature needs one, e.g. '
            '{"op":"add_feature","feature":{"type":"cylinder","name":"Shaft",'
            '"params":{"radius":10,"height":40}}}. Feature types: '
            + ", ".join(sorted(vocab)) + ".")

    word = _slug(raw)
    if word in _BOOLEAN_AS_TYPE:
        raise ValueError(
            f'add_feature: "{raw}" is a boolean OPERATION, not a feature type. Subtract by '
            f'building the SAME shape with mode:"cut" (e.g. {{"type":"cylinder","params":'
            f'{{"radius":10,"height":40}},"at":[0,0,0],"mode":"cut"}}), fuse with '
            f'mode:"boss" (the default), or combine two existing bodies with '
            f'{{"type":"boolean","params":{{"op":"cut","a":"f1","b":"f2"}}}}.')

    base, cut = word, False
    for pre in _CUT_TYPE_PREFIXES:
        if base.startswith(pre) and len(base) > len(pre):
            base, cut = base[len(pre):], True
            break
    else:
        for suf in _CUT_TYPE_SUFFIXES:
            if base.endswith(suf) and len(base) > len(suf):
                base, cut = base[: -len(suf)], True
                break

    ftype = _canon_feature_type(base, vocab)
    if ftype is None:
        if live:
            raise ValueError(_feature_type_error(raw, vocab))
        return feat  # offline vocabulary is a subset — let the engine be the authority

    out = dict(feat)
    out["type"] = ftype
    params = dict(out["params"]) if isinstance(out.get("params"), dict) else {}
    extra = ""
    if cut or _TYPE_ALIAS_MODE.get(word) == "cut":
        if out.get("mode") is None and params.get("mode") is None:
            out["mode"] = "cut"
            extra = ' with mode:"cut"'
    style = _TYPE_ALIAS_HOLE_STYLE.get(word)
    if style and ftype == "hole" and params.get("style") is None:
        params["style"] = style
        extra += f' (style "{style}")'
    if params or isinstance(out.get("params"), dict):
        out["params"] = params
    if word in ("tube", "pipe"):
        extra += " — a hollow tube also needs a concentric mode:\"cut\" cylinder for its bore"
    notes.append(f'{_feat_label(feat, ftype)}: interpreted type "{raw}" as "{ftype}"{extra}')
    return out


def _normalize_feature_types(ops: Any, notes: list) -> Any:
    """Map every add_feature's `type` onto the engine vocabulary. Runs FIRST, because the
    sketch/primitive/dimension normalizers downstream all branch on the canonical type —
    a sketch spelled "profile" used to slip past the sketch normalizer entirely."""
    if not isinstance(ops, list):
        return ops
    vocab, live = _feature_type_vocab()
    out = []
    for op in ops:
        if (isinstance(op, dict) and op.get("op") == "add_feature"
                and isinstance(op.get("feature"), dict)):
            new = _retype_feature(op["feature"], vocab, live, notes)
            op = op if new is op["feature"] else {**op, "feature": new}
        out.append(op)
    return out


# The op keys that name the feature an op acts on. The canonical spelling is `id`;
# `feature_id` in particular produced the silent «delete_feature: no feature ''».
_OP_TARGET_ALIASES = ("feature_id", "featureId", "feature", "target", "target_id", "fid")
_OP_TARGET_OPS = frozenset({
    "delete_feature", "set_suppressed", "reorder_feature", "rename", "set_folder",
    "copy_feature", "edit_feature",
})


# Natural verbs for the one canonical suppression mutation.  These are deliberately
# narrow: each action fixes the value, so accepting it cannot invert a model's intent.
_SUPPRESSION_OP_ALIASES = {
    "suppress_feature": True,
    "unsuppress_feature": False,
}


def _normalize_suppression_op(op: dict, kind: str, notes: list) -> dict:
    """Normalize explicit suppression verbs and the ``suppressed`` field alias.

    The core operation is ``set_suppressed(id, value)``.  Its former ``value`` default
    is false, so passing the natural ``suppressed:true`` spelling used to report success
    while doing nothing.  Never pick between contradictory action/value spellings.
    """
    implied = _SUPPRESSION_OP_ALIASES.get(kind)
    supplied = {key: op[key] for key in ("value", "suppressed") if key in op}
    if kind == "set_suppressed":
        if "suppressed" not in supplied:
            return op
        if any(type(value) is not bool for value in supplied.values()):
            raise ValueError(
                f'set_suppressed: "value" and "suppressed" must be booleans; got '
                f'{supplied!r}. Use {{"op":"set_suppressed","id":"f1","value":true}}.')
        if "value" in supplied and supplied["value"] != supplied["suppressed"]:
            raise ValueError(
                'set_suppressed has conflicting "value" and "suppressed" values. '
                'Send one boolean intent: {"op":"set_suppressed","id":"f1","value":true}.')
        out = dict(op)
        out.pop("suppressed")
        out["value"] = supplied["suppressed"]
        notes.append(
            'set_suppressed: interpreted "suppressed" as canonical "value" — '
            'suppression state is written with value:true or value:false')
        return out

    if implied is None:
        return op
    conflicts = {key: value for key, value in supplied.items()
                 if type(value) is not bool or value != implied}
    if conflicts:
        wanted = "true" if implied else "false"
        raise ValueError(
            f'{kind} means set_suppressed value:{wanted}, but got conflicting '
            f'{conflicts!r}. Send either {{"op":"{kind}","id":"f1"}} or '
            f'{{"op":"set_suppressed","id":"f1","value":{wanted}}}.')
    out = {key: value for key, value in op.items() if key not in ("value", "suppressed")}
    out["op"] = "set_suppressed"
    out["value"] = implied
    wanted = "true" if implied else "false"
    notes.append(
        f'interpreted op:"{kind}" as {{"op":"set_suppressed","id":…,"value":{wanted}}} '
        "— set_suppressed is the canonical suppression mutation")
    return out


#: Keys whose value is a STRUCTURE. A string here is a nested encoding, never data —
#: unlike name/mode/plane, where a string is exactly what belongs.
_STRUCTURE_KEYS = frozenset({
    "feature", "features", "params", "refs", "ops", "entities", "constraints",
    "dimensions", "equations", "profile", "profiles", "sections", "targets", "bodies"})


def _decode_one_structure(v: Any) -> Any:
    """A string encoding a dict/list -> that dict/list. Anything else, unchanged.

    JSON first, then `ast.literal_eval` — the live payload was a PYTHON repr (single
    quotes, bare `None`), which json.loads cannot read. Never raises: an unparseable
    string is data as far as this is concerned, and the caller's own validation owns it.
    """
    if not isinstance(v, str):
        return v
    s = v.strip()
    if not (s[:1] in "{[" and s[-1:] in "}]"):
        return v
    try:
        return json.loads(s)
    except ValueError:
        pass
    try:
        import ast  # noqa: PLC0415 — only reached on the repr dialect
        parsed = ast.literal_eval(s)
    except (ValueError, SyntaxError, MemoryError, RecursionError):
        return v
    return parsed if isinstance(parsed, (dict, list)) else v


def _decode_embedded_structures(v: Any, notes: list, _depth: int = 0) -> Any:
    """Decode every stringified structure in the batch, at any depth."""
    if _depth > 8:
        return v
    if isinstance(v, list):
        return [_decode_embedded_structures(x, notes, _depth + 1) for x in v]
    if not isinstance(v, dict):
        return v
    out = {}
    for k, val in v.items():
        if k in _STRUCTURE_KEYS:
            decoded = _decode_one_structure(val)
            if decoded is not val:
                notes.append(
                    f'decoded "{k}" from a string into the object it encodes — a '
                    f"structure slot takes real JSON, not a quoted dict")
                val = decoded
            if k == "refs" and isinstance(val, list):
                decoded_items = [_decode_one_structure(x) for x in val]
                if decoded_items != val:
                    notes.append('decoded stringified refs[] selectors into JSON objects')
                    val = decoded_items
            elif k == "refs" and isinstance(val, dict):
                decoded_slots = {slot: _decode_one_structure(item) for slot, item in val.items()}
                if decoded_slots != val:
                    notes.append('decoded stringified refs slots into JSON selectors')
                    val = decoded_slots
        out[k] = _decode_embedded_structures(val, notes, _depth + 1)
    return out


# Ops a model invents for "change this feature's parameter". Every one of them is
# edit_feature. Deliberately EXCLUDES set_dim/set_dimension: those genuinely could
# mean the sketch op `sketch_set_dim`, and guessing between two real ops is how a
# normalizer starts building the wrong thing confidently.
_PARAM_EDIT_OPS = frozenset({
    "set_param", "set_parameter", "set_params", "set_parameters",
    "update_param", "update_parameter", "change_param", "change_parameter",
    "modify_param", "modify_parameter", "set_feature_param",
})
_PARAM_EDIT_ID_KEYS = ("id", "feature", "feature_id", "target", "name")
_PARAM_EDIT_NAME_KEYS = ("param", "parameter", "key", "field")
_PARAM_EDIT_VALUE_KEYS = ("value", "to", "new_value", "val")


def _rewrite_param_edit(el: dict, index: int, notes: list) -> dict:
    """``{"op":"set_param","feature":F,"param":P,"value":V}`` -> canonical edit_feature.

    Also accepts the batched ``"params":{...}`` form. Refuses rather than guesses when
    the target or the value is absent — a parameter edit aimed at nothing is not
    something this pass can repair.
    """
    fid = next((el[k] for k in _PARAM_EDIT_ID_KEYS
                if el.get(k) is not None), None)
    if fid is None:
        raise ValueError(
            f'ops[{index}]: op:"{el.get("op")}" names no feature to edit. A parameter '
            f'edit is {{"op":"edit_feature","id":"f1","params":{{"radius":12}}}}.')
    params = el.get("params")
    if not isinstance(params, dict):
        pname = next((el[k] for k in _PARAM_EDIT_NAME_KEYS
                      if el.get(k) is not None), None)
        val = next((el[k] for k in _PARAM_EDIT_VALUE_KEYS if k in el), _MISSING)
        if pname is None or val is _MISSING:
            raise ValueError(
                f'ops[{index}]: op:"{el.get("op")}" needs either "params":{{…}} or a '
                f'"param" name with its "value". Got keys: {sorted(el)}.')
        params = {str(pname): val}
    notes.append(
        f'interpreted op:"{el.get("op")}" as {{"op":"edit_feature","id":"{fid}",'
        f'"params":{{…}}}} — changing a feature parameter is edit_feature')
    order = {k: el[k] for k in _OP_ORDER_KEYS if k in el}
    return {"op": "edit_feature", "id": fid, "params": params, **order}


#: What op_edit_feature (core/doc/session.cpp) reads beside the target id; the engine drops
#: any other op-level key without a word. before/after ride along from op rewrites, and a
#: `type` copied out of a {"feature":{…}} wrapper only restates what the feature is.
_EDIT_FEATURE_OP_KEYS = frozenset({"op", "id", "params", "refs", "name", "type",
                                   "before", "after"})
#: add_feature's feature-envelope placement keys. The engine stores them IN params, so an
#: edit that sets one beside "params" means that param.
_EDIT_FEATURE_PARAM_KEYS = ("at", "at_offset", "axis", "axis_dir", "centered", "mode", "merge")


def _hoist_edit_feature_keys(op: dict, index: int, notes: list) -> dict:
    """Move edit_feature's op-level placement keys into ``params``; refuse any other stray key.

    Live c219: ``{"op":"edit_feature","id":"f8","at":[…],"params":{"height":…}}`` applied
    the height and silently dropped the move (preflight_ops passed it too); the sub-agent
    only noticed from a face count and spent three calls finding out why.
    """
    stray = [k for k in op if k not in _EDIT_FEATURE_OP_KEYS]
    if not stray:
        return op
    unknown = [k for k in stray if k not in _EDIT_FEATURE_PARAM_KEYS]
    if unknown:
        hint = (' To suppress a feature use {"op":"set_suppressed","id":…,"value":true}.'
                if "suppressed" in unknown else "")
        raise ValueError(
            f"ops[{index}] edit_feature: unknown key(s) {unknown} beside \"params\" — "
            f"edit_feature applies only params, refs and name, so they would be ignored. "
            f'Put parameter changes inside "params".{hint}')
    params = op.get("params")
    params = dict(params) if isinstance(params, dict) else {}
    for key in stray:
        if key in params and params[key] != op[key]:
            raise ValueError(
                f'ops[{index}] edit_feature: "{key}" is given both beside and inside "params" '
                f"with different values ({op[key]!r} vs {params[key]!r}) — send it once, "
                f'inside "params".')
        params[key] = op[key]
    out = {k: v for k, v in op.items() if k not in stray}
    out["params"] = params
    notes.append(f"edit_feature {op.get('id')}: moved {stray} into params — a feature's "
                 f"placement lives in its params, and edit_feature reads only params/refs/name")
    return out



def _normalize_op_elements(ops: list, notes: list) -> list:
    """Give every batch element a valid `op`. Raises ValueError with a teaching message
    for an element that names neither an op nor a feature type."""
    ftypes, _live = _feature_type_vocab()
    docops = _doc_op_vocab()
    out: list = []
    for i, el in enumerate(ops):
        if not isinstance(el, dict):
            pair = (' A ["<name>", "<value>"] pair is not an op: the op name and its data '
                    "live in ONE object, keyed."
                    if isinstance(el, (list, tuple)) else "")
            raise ValueError(
                f"ops[{i}] is {type(el).__name__}, not an object.{pair} Every element is "
                f'either {{"op":"<op name>", …}} from the op vocabulary or a bare feature '
                f'{{"type":"box","name":"Block","params":{{"length":40,"width":30,'
                f'"height":20}}}} (read as add_feature). Got: {el!r}')
        name = el.get("op")
        if isinstance(name, str) and name.strip():
            kind = name.strip()
            # {"op":"add_feature","type":"sketch","refs":{…}} — the feature keys sit BESIDE
            # the op instead of inside a "feature" object. Unambiguous, so wrap them.
            if (kind == "add_feature" and not isinstance(el.get("feature"), dict)
                    and el.get("type") is not None):
                order = {k: el[k] for k in _OP_ORDER_KEYS if k in el}
                inner = {k: v for k, v in el.items()
                         if k != "op" and k not in _OP_ORDER_KEYS and k != "feature"}
                out.append({"op": "add_feature", "feature": inner, **order})
                notes.append('read the feature keys sitting beside op:"add_feature" as its '
                             '"feature" object — an add_feature op is '
                             '{"op":"add_feature","feature":{"type":…,"params":{…}}}')
                continue
            # A string feature that survived _decode_embedded_structures unparsed (not
            # JSON, not a Python literal, or a literal that isn't a dict — e.g. a bare
            # number or a list) must never reach a downstream `.get()` and crash with a
            # bare AttributeError the way the live 2026-07-28 run did. Every other pass
            # below only ever normalizes a DICT feature (isinstance-guarded), so a feature
            # that is still not a dict here would otherwise ride silently to the engine.
            if kind == "add_feature" and el.get("feature") is not None and not isinstance(
                    el.get("feature"), dict):
                bad = el["feature"]
                text = bad if isinstance(bad, str) else repr(bad)
                if len(text) > 80:
                    text = text[:80] + "…"
                raise ValueError(
                    f'ops[{i}]["feature"] is a {type(bad).__name__} ({text!r}), not a JSON '
                    f"object — it did not decode into one either. feature must be a JSON "
                    f'object, e.g. {{"type":"cylinder","name":"PoleBore","params":{{...}}}}')
            # {"op":"set_param","feature":"f1","param":"center","value":[700,0,300]}
            # — a parameter edit written as its own op. That IS edit_feature, and the
            # payload says so unambiguously: a feature reference plus one param and
            # its value. Live 2026-07-30 (run-13566637896867, sports_bike): the model
            # tried to move two tires it had just mis-placed and got "unknown op
            # 'set_param'. closest: set_material, set_units, set_appearance,
            # set_folder, set_rollback" — five ops that share nothing but the `set_`
            # prefix, while edit_feature (the actual answer) was never suggested,
            # because _score_match only ever sees the NAME, not the payload shape.
            if kind in _PARAM_EDIT_OPS:
                out.append(_normalize_op_target(
                    _rewrite_param_edit(el, i, notes), "edit_feature", notes))
                continue
            if kind in _SUPPRESSION_OP_ALIASES:
                normalized = _normalize_suppression_op(el, kind, notes)
                out.append(_normalize_op_target(normalized, "set_suppressed", notes))
                continue
            if kind == "set_suppressed":
                normalized = _normalize_suppression_op(el, kind, notes)
                out.append(_normalize_op_target(normalized, kind, notes))
                continue
            if kind in docops or kind == "new_part":
                normalized = _normalize_op_target(el, kind, notes)
                if kind == "edit_feature":
                    normalized = _hoist_edit_feature_keys(normalized, i, notes)
                out.append(normalized)
                continue
            ftype = _canon_feature_type(kind, ftypes)
            if ftype:
                rest = {k: v for k, v in el.items() if k != "op"}
                inner = rest.pop("feature", None)
                feature = dict(inner) if isinstance(inner, dict) else rest
                feature.setdefault("type", ftype)
                out.append({"op": "add_feature", "feature": feature})
                notes.append(
                    f'interpreted op:"{kind}" as {{"op":"add_feature","feature":'
                    f'{{"type":"{ftype}",…}}}} — a feature type is not an op name')
                continue
            out.append(el)  # a genuinely unknown op: the engine names it (enrich_unknown_op)
            continue
        if isinstance(el.get("feature"), dict):
            out.append({"op": "add_feature", **el})
            notes.append('read an element carrying a "feature" but no "op" as op:"add_feature"')
            continue
        if el.get("type") is not None:
            out.append({"op": "add_feature", "feature": el})
            notes.append(
                'read bare feature dict(s) in ops as {"op":"add_feature","feature":{…}} — '
                'every ops element needs an "op"')
            continue
        raise ValueError(
            f'ops[{i}] has no "op". An ops element is either {{"op":"<name>", …}} from the op '
            f'vocabulary or a bare feature {{"type":"<feature type>","name":…,"params":{{…}}}} '
            f"(read as add_feature). Got keys: {sorted(el)}.")
    return out


def _normalize_op_target(op: dict, kind: str, notes: list) -> dict:
    """`{"op":"delete_feature","feature_id":"f2"}` -> the canonical `id` key."""
    if kind == "set_rollback":
        # The rollback bar is addressed by `after` (the feature it sits after), not `id`.
        # Every other spelling used to reach the engine as an ignored key and come back
        # ok:true with no change (c259 tried after/id/feature/feature_id in turn).
        if "after" in op:
            return op
        for alias in ("id",) + _OP_TARGET_ALIASES:
            v = op.get(alias)
            if isinstance(v, str) and v.strip():
                out = {k: x for k, x in op.items() if k != alias}
                out["after"] = v.strip()
                notes.append(f'set_rollback: interpreted {alias}="{v}" as after="{v}" '
                             f"— the bar is placed with `after` (null = at the end)")
                return out
        return op
    if kind not in _OP_TARGET_OPS or op.get("id") is not None:
        return op
    # The target (and its params) nested inside a `feature` wrapper — the shape a model
    # reaches for because add_feature has one. session.cpp reads the id at OP level, so
    # this arrived as «edit_feature: no feature ''» with the id sitting right there
    # (live probe-holster-7, after preflight_ops had accepted the flat spelling).
    inner = op.get("feature")
    if isinstance(inner, dict):
        for alias in ("id",) + _OP_TARGET_ALIASES:
            v = inner.get(alias)
            if isinstance(v, str) and v.strip():
                out = dict(op)
                out["id"] = v.strip()
                rest = {k: x for k, x in inner.items() if k != alias}
                if isinstance(rest.get("params"), dict) and "params" not in out:
                    out["params"] = rest.pop("params")
                for k, x in rest.items():
                    out.setdefault(k, x)
                out.pop("feature", None)
                notes.append(
                    f'{kind}: read the target out of feature.{alias}="{v}" — this op '
                    f'addresses its feature with a top-level `id` (and `params` beside it)')
                return out

    for alias in _OP_TARGET_ALIASES:
        v = op.get(alias)
        if isinstance(v, str) and v.strip():
            out = dict(op)
            out.pop(alias, None)
            out["id"] = v.strip()
            notes.append(f'{kind}: interpreted {alias}="{v}" as id="{v}" '
                         f"— the op addresses its feature with `id`")
            return out
    if kind == "rename":
        # {"op":"rename","name":"aa_battery_cassette"} with no target is a model naming the
        # PART (live c245 and c247 batch heads); session.cpp only answers «rename: no feature».
        raise ValueError(
            f'rename renames a FEATURE by id: {{"op":"rename","id":"f1","name":'
            f'"{op.get("name") or "NewName"}"}} (feature ids come from get_part(summary=true) '
            f"or an earlier add_features reply). "
            f"This one names no feature, so nothing was applied. add_features has no op "
            f"that renames the open part/document: its name is set when it is created — "
            f'new_part(name="…"), or a leading {{"op":"new_part","name":"…"}} in a batch '
            f'that has no doc yet — and save_part(path="<name>.step+") writes the file '
            f"under the name you want. An already-saved .step+ can be copied under a new "
            f"display name with relocate_native_files.")
    return op


# ---------------------------------------------------------------------------
# FLAT FACE-POCKET expander (model-robustness).
#
# The shape a model writes when it wants a milled pocket in a face is flat:
#   {"op":"pocket","name":"DevicePocket","face":"top","shape":"rect",
#    "width":51.6,"height":22.5,"depth":72,"center":[0,0],"corner_radius":4}
# (live copilot run, 2026-07-28). `pocket` used to alias straight onto the `extrude`
# feature type, which then died on «refs.profile: required for extrude» — the flat spec
# names a profile in words but never builds one. It is nevertheless fully determined: a
# rectangle (or circle) of that size, centred at [u,v] on the named face, extruded `depth`
# as a cut. So it expands here into the canonical pair the engine wants, and the rewrite is
# echoed so the model learns the form.
_FACE_POCKET_OPS = frozenset({"pocket", "cut"})
_FACE_POCKET_SIDES = frozenset({"top", "bottom", "front", "back", "left", "right"})
_POCKET_RECT_SHAPES = frozenset({"rect", "rectangle", "rectangular", "slot", "square", "box"})
_POCKET_ROUND_SHAPES = frozenset({"circle", "circular", "round", "bore", "hole", "disc", "disk"})
_POCKET_DEPTH_KEYS = ("depth", "distance", "height_of_cut", "cut_depth")
_POCKET_CENTER_KEYS = ("center", "centre", "at", "position", "location", "origin")


def _pocket_num(el: dict, *keys) -> Optional[float]:
    """The first numeric among `keys`, reading a {"value":N} dimension wrapper too."""
    for k in keys:
        v = el.get(k)
        if isinstance(v, dict):
            v = v.get("value")
        if _is_num(v):
            return float(v)
    return None


def _pocket_face_side(v: Any) -> Optional[str]:
    """The face word a flat pocket names ("top", "the top face", {"side":"top"})."""
    if isinstance(v, dict):
        v = v.get("side") or v.get("face")
    q = _face_phrase_query(v)
    if q is not None:
        return q["side"]
    if isinstance(v, str):
        w = _slug(v)
        if w in _FACE_POCKET_SIDES:
            return w
    return None


def _pocket_center(el: dict) -> list:
    for k in _POCKET_CENTER_KEYS:
        v = el.get(k)
        if isinstance(v, (list, tuple)) and len(v) >= 2 and all(_is_num(x) for x in v[:2]):
            return [float(v[0]), float(v[1])]
    u, w = _pocket_num(el, "u", "cx", "x"), _pocket_num(el, "v", "cy", "y")
    if u is not None or w is not None:
        return [u or 0.0, w or 0.0]
    return [0.0, 0.0]  # the face CENTRE, matching the hole `positions` convention


def _face_pocket_entity(el: dict, kind: str, label: str, notes: list) -> dict:
    """The one sketch entity a flat pocket spec names. Raises a teaching ValueError for a
    shape word the sketch vocabulary has no profile for."""
    center = _pocket_center(el)
    raw = el.get("shape") or el.get("profile_shape") or el.get("cross_section")
    word = _slug(raw) if isinstance(raw, str) else ""
    w = _pocket_num(el, "width", "w", "size_x", "length_x")
    h = _pocket_num(el, "height", "h", "size_y", "length_y")
    dia = _pocket_num(el, "diameter", "dia", "d")
    rad = _pocket_num(el, "radius", "r")
    if not word:
        word = "rect" if (w is not None and h is not None) else "circle"
    if word in _POCKET_RECT_SHAPES:
        if w is None or h is None:
            raise ValueError(
                f'{kind} "{label}": shape "{raw or word}" needs a numeric width and height '
                f"(mm) on the face. Got width={w!r}, height={h!r}.")
        ent = {"type": "rectangle", "center": center, "width": w, "height": h}
        cr = _pocket_num(el, "corner_radius", "fillet_radius", "corner_r")
        if cr:
            # There is no rounded-rectangle sketch entity (line/arc/circle/spline only —
            # core/doc/sketch_geom.cpp), and rounding the corners after the fact means
            # picking the pocket's four vertical edges, which is geometry selection, not a
            # rewrite of what was written. So the radius is dropped, loudly.
            notes.append(
                f'{label}: dropped corner_radius={cr} — the sketch vocabulary has no '
                f"rounded-rectangle entity, so the pocket is sharp-cornered. Round it with a "
                f'separate {{"op":"add_feature","feature":{{"type":"fillet","refs":'
                f'{{"edges":[…the pocket\'s 4 vertical edges…]}},"params":{{"radius":{cr}}}}}}} '
                f"after this cut")
        return ent
    if word in _POCKET_ROUND_SHAPES:
        r = rad if rad is not None else (dia / 2.0 if dia is not None else None)
        if r is None and w is not None:
            r = w / 2.0
        if r is None:
            raise ValueError(
                f'{kind} "{label}": shape "{raw or word}" needs a numeric diameter or radius '
                f"(mm).")
        if rad is None and dia is not None:
            notes.append(f'{label}: read diameter={dia} as radius={r} — a sketch circle '
                         f"carries its RADIUS")
        return {"type": "circle", "center": center, "radius": r}
    raise ValueError(
        f'{kind} "{label}": shape "{raw}" is not a pocket profile. Use shape:"rect" '
        f'(rect|rectangle|slot, with width+height) or shape:"circle" (circle|round|bore, '
        f"with diameter or radius) — or write the profile yourself as a sketch feature and "
        f'extrude it with mode:"cut".')


def _pocket_footprint(ent: dict) -> tuple:
    """The entity's axis-aligned [u0,u1,v0,v1] extent on the face."""
    cu, cv = ent["center"]
    if ent["type"] == "rectangle":
        hu, hv = ent["width"] / 2.0, ent["height"] / 2.0
    else:
        hu = hv = ent["radius"]
    return (cu - hu, cu + hu, cv - hv, cv + hv)


def _face_pocket_pair(el: dict, kind: str, notes: list, prior: list) -> Optional[list]:
    """The flat face-pocket spec -> [sketch, extrude-cut], or None when `el` is not one.

    Deliberately conservative for `cut`: a bare {"op":"cut","a":…,"b":…} is a BOOLEAN and
    must keep going down its own path, so the flat spec has to be unmistakably present —
    a named planar face AND a numeric depth AND a profile size.
    """
    side = _pocket_face_side(el.get("face") if el.get("face") is not None else el.get("on"))
    depth = _pocket_num(el, *_POCKET_DEPTH_KEYS)
    if side is None or depth is None:
        return None
    has_size = any(_pocket_num(el, k) is not None for k in (
        "width", "w", "height", "h", "diameter", "dia", "d", "radius", "r"))
    if not has_size and not isinstance(el.get("shape"), str):
        return None
    name = el.get("name") if isinstance(el.get("name"), str) and el["name"].strip() else None
    name = (name or "Pocket").strip()
    label = name
    if kind == "cut":
        notes.append(f'{label}: read op:"cut" carrying a face/shape/depth spec as a POCKET '
                     f'— "cut" is not an op; removing material is an extrude with '
                     f'params.mode:"cut"')
    ent = _face_pocket_entity(el, kind, label, notes)
    sk_name = f"{name}Profile"
    # A second pocket whose profile sits INSIDE one this batch already cut into the same
    # face is a deeper STEP, and its depth is measured from that pocket's floor — cutting
    # only `depth` from the outer face plane would sweep pure void and the engine would
    # reject the whole batch with "extrude cut removed no material". The live batch is
    # exactly this: a 72-deep pocket in an 80 block, then a 14x10 slot 8 deep in the same
    # place, i.e. straight through the 8 mm floor left behind.
    u0, u1, v0, v1 = _pocket_footprint(ent)
    total, over = depth, None
    for p in prior:
        if (p["side"] == side and p["u0"] - 1e-9 <= u0 and u1 <= p["u1"] + 1e-9
                and p["v0"] - 1e-9 <= v0 and v1 <= p["v1"] + 1e-9):
            total, over = p["depth"] + depth, p
    if over is not None:
        notes.append(
            f'{label}: its profile sits inside "{over["name"]}", already cut {over["depth"]} '
            f'deep into the same {side} face, so depth={depth} is measured from THAT floor — '
            f"the extrude cuts {total} from the {side} face plane. A cut sketched on the "
            f"outer face but swept only through the void above an existing pocket removes "
            f"nothing and the engine rejects it")
    prior.append({"side": side, "u0": u0, "u1": u1, "v0": v0, "v1": v1,
                  "depth": total, "name": name})
    sketch = {"op": "add_feature", "feature": {
        "type": "sketch", "name": sk_name,
        "refs": {"plane": {"of": "face", "side": side}},
        "params": {"entities": [ent], "constraints": []}}}
    extrude = {"op": "add_feature", "feature": {
        "type": "extrude", "name": name,
        "refs": {"profile": [{"kind": "region", "sketch": sk_name, "hint_index": 0}]},
        "params": {"mode": "cut", "depth": total}}}
    shape_word = "rectangle" if ent["type"] == "rectangle" else "circle"
    notes.append(
        f'{label}: expanded the flat {kind} spec into the canonical pair — a sketch '
        f'"{sk_name}" on refs.plane={{"of":"face","side":"{side}"}} carrying the {shape_word}, '
        f'then {{"type":"extrude","params":{{"mode":"cut","depth":{total}}},'
        f'"refs":{{"profile":[{{"kind":"region","sketch":"{sk_name}"}}]}}}}. A pocket is '
        f"always those two features; center=[u,v] is FACE-LOCAL and [0,0] is the face centre")
    return [sketch, extrude]


def _shift_batch_placeholders(v: Any, table: dict) -> Any:
    """Renumber "$N" same-batch placeholders after ops were inserted ahead of them."""
    if isinstance(v, str):
        match = re.fullmatch(r"(\$\d+)((?:#\d+)?(?:\.[A-Za-z0-9_]+)?)", v.strip())
        return table.get(match[1], match[1]) + match[2] if match else v
    if isinstance(v, list):
        return [_shift_batch_placeholders(x, table) for x in v]
    if isinstance(v, dict):
        return {k: _shift_batch_placeholders(x, table) for k, x in v.items()}
    return v


def _append_sketch_auto_define(ops: list, *, auto_define: bool = True,
                               doc_id: Optional[str] = None,
                               notes: Optional[list] = None,
                               index_map: Optional[dict] = None) -> list:
    """Finish sketch-authoring runs in the same transaction, after explicit intent ops.

    Flush before the next feature consumes the sketch, not before caller-authored
    relations/dimensions. Renumber every downstream placeholder when inserting ops.
    Feature params retain auto_define:false so later sketch_add_entity batches respect it.
    """
    policies: dict[str, bool] = {}
    if auto_define and doc_id and any(
            isinstance(op, dict) and op.get("op") == "sketch_add_entity"
            and not str(op.get("sketch", "")).startswith("$") for op in ops):
        tree = cb.call("doc_tree", {"doc_id": doc_id}).get("tree") or {}
        policies.update({feature["id"]: (feature.get("params") or {}).get("auto_define") is not False
                         for feature in tree.get("features") or []
                         if isinstance(feature, dict) and feature.get("type") == "sketch"})
    pending: dict[str, bool] = {}
    out: list = []
    shifts: dict[str, str] = {}
    additions = 0

    def flush() -> None:
        nonlocal additions
        for sketch, enabled in pending.items():
            if enabled:
                out.append({"op": "sketch_fully_define", "sketch": sketch})
                additions += 1
        pending.clear()

    for index, original in enumerate(ops):
        if not isinstance(original, dict):
            flush()
            shifts[f"${index}"] = f"${len(out)}"
            out.append(original)
            continue
        op = {key: value for key, value in original.items() if key != "auto_define"}
        kind = op.get("op", "")
        if not kind.startswith("sketch_"):
            flush()
        shifts[f"${index}"] = f"${len(out)}"
        out.append(op)
        feature = op.get("feature") or {}
        if kind == "add_feature" and feature.get("type") == "sketch":
            sketch = f"${index}"
            enabled = (auto_define and original.get("auto_define") is not False
                       and feature.get("auto_define") is not False
                       and (feature.get("params") or {}).get("auto_define") is not False)
            policies[sketch] = enabled
            pending[sketch] = enabled
        elif kind == "sketch_add_entity":
            sketch = op.get("sketch")
            if isinstance(sketch, str):
                enabled = (auto_define and policies.get(sketch, True)
                           and original.get("auto_define") is not False)
                pending[sketch] = pending.get(sketch, True) and enabled
        elif kind == "sketch_fully_define":
            pending.pop(op.get("sketch"), None)
    flush()
    if additions and notes is not None:
        notes.append(f"appended sketch_fully_define for {additions} sketch-authoring run(s) "
                     "in this same revision; auto_define:false opts out")
    if index_map is not None:
        index_map.update(shifts)
    return _shift_batch_placeholders(out, shifts)


def _authored_batch_id_map(id_map: dict, index_map: dict) -> dict:
    """Keep caller op handles stable; automatic definition ops are implementation details."""
    original = {final: authored for authored, final in index_map.items()}
    out = {}
    for key, value in id_map.items():
        base, marker, suffix = key.partition("#")
        if base in original:
            out[original[base] + marker + suffix] = value
    return out


def _expand_face_pockets(ops: Any, notes: list) -> Any:
    """Expand every flat face-pocket element. Runs FIRST — ahead of _normalize_op_elements,
    which would otherwise alias op:"pocket" onto a bare, profile-less extrude."""
    if not isinstance(ops, list) or not any(
            isinstance(el, dict) and _slug(el.get("op")) in _FACE_POCKET_OPS for el in ops):
        return ops
    out: list = []
    shift: dict = {}
    changed = False
    prior: list = []
    for i, el in enumerate(ops):
        pair = None
        if isinstance(el, dict):
            kind = _slug(el.get("op"))
            if kind in _FACE_POCKET_OPS:
                pair = _face_pocket_pair(el, kind, notes, prior)
        if pair is None:
            shift[f"${i}"] = f"${len(out)}"
            out.append(el)
            continue
        changed = True
        shift[f"${i}"] = f"${len(out) + 1}"  # the pocket IS the extrude, not its sketch
        out.extend(pair)
    if not changed:
        return ops
    if any(k != v for k, v in shift.items()):
        out = [_shift_batch_placeholders(o, shift) for o in out]
    return out


# ---------------------------------------------------------------------------
# EXTRUDED-POLYGON sugar. The primitive vocabulary has box/cylinder/cone/sphere but no
# prism, so a model that wants a hexagonal slab writes the compound it wishes existed:
# {"type":"polygon_extrude","params":{"sides":6,"radius":30,"height":20}} (live run
# 2026-07-28, five stacked levels, rejected on the first op). The intent is complete and
# unambiguous — sides, size and height are all there — and a prism IS a polygon sketch
# plus an extrude, so build that pair instead of teaching the model to type it out.
_POLY_SIDE_WORDS = {"triangle": 3, "triangular": 3, "square": 4, "pentagon": 5,
                    "pentagonal": 5, "hexagon": 6, "hexagonal": 6, "hex": 6,
                    "heptagon": 7, "octagon": 8, "octagonal": 8, "nonagon": 9,
                    "decagon": 10}
_PRISM_TOKENS = ("extrude", "extrusion", "extruded", "prism", "prismatic")
_PRISM_SIDE_KEYS = ("sides", "n_sides", "num_sides", "side_count", "edges", "n")
_PRISM_HEIGHT_KEYS = ("height", "depth", "thickness", "length", "extent", "h")


def _prism_spec(ftype: str) -> Optional[int]:
    """Sides implied by a compound feature-type word, or 0 when it says "polygon" without
    naming a shape. None when the word is not an extruded-polygon request at all."""
    parts = set(_slug(ftype).split("_"))
    if not (parts & set(_PRISM_TOKENS)):
        return None
    for word in parts:
        if word in _POLY_SIDE_WORDS:
            return _POLY_SIDE_WORDS[word]
    if parts & {"polygon", "ngon", "poly"}:
        return 0
    return None


def _prism_pair(feat: dict, notes: list) -> Optional[list]:
    """[sketch, extrude] for an extruded-polygon feature, or None when `feat` is not one."""
    implied = _prism_spec(feat.get("type"))
    if implied is None:
        return None
    params = feat.get("params") if isinstance(feat.get("params"), dict) else {}
    name = feat.get("name") if isinstance(feat.get("name"), str) and feat["name"] else "Prism"
    sides = next((int(n) for k in _PRISM_SIDE_KEYS
                  if (n := _plain_num(params.get(k))) is not None), implied or 0)
    radius = _plain_num(params.get("radius") if params.get("radius") is not None
                        else params.get("circumradius") or params.get("r"))
    if radius is None:
        dia = _plain_num(params.get("diameter") or params.get("d")
                         or params.get("across_corners"))
        if dia is not None:
            radius = dia / 2.0
    if radius is None and sides >= 3:
        # Across-flats/apothem is the inscribed radius: circumradius = a / cos(pi/n).
        flats = _plain_num(params.get("across_flats") or params.get("apothem")
                           or params.get("width"))
        if flats is not None:
            apothem = flats / 2.0 if params.get("apothem") is None else flats
            radius = apothem / math.cos(math.pi / sides)
    height = next((_plain_num(params[k]) for k in _PRISM_HEIGHT_KEYS
                   if _plain_num(params.get(k)) is not None), None)
    if sides < 3 or radius is None or height is None:
        raise ValueError(
            f'"{feat.get("type")}" is an extruded polygon, which this kernel builds as a '
            f"polygon SKETCH plus an extrude — but the spec is incomplete: got sides="
            f"{sides or None}, radius={radius}, height={height}. Give all three (radius "
            f"is to a CORNER; across_flats works too), or write the two features yourself.")
    center = _xy(params.get("center") or params.get("at") or [0, 0])
    plane = params.get("plane") or (feat.get("refs") or {}).get("plane") or "top"
    sk_name = f"{name}Profile"
    sketch = {"op": "add_feature", "feature": {
        "type": "sketch", "name": sk_name,
        "params": {"plane": plane, "entities": [
            {"type": "polygon", "sides": sides, "center": center,
             "radius": _clean_num(radius)}]}}}
    extrude = {"op": "add_feature", "feature": {
        "type": "extrude", "name": name,
        "refs": {"profile": [{"kind": "region", "sketch": sk_name, "hint_index": 0}]},
        "params": {"mode": params.get("mode") or "boss", "depth": _clean_num(height)}}}
    notes.append(
        f'{name}: expanded "{feat.get("type")}" into the canonical pair — a '
        f"{sides}-sided polygon sketch \"{sk_name}\" of radius {_clean_num(radius)} on "
        f"{plane}, then an extrude of depth {_clean_num(height)} through "
        f'refs.profile=[{{"kind":"region","sketch":"{sk_name}"}}]. There is no prism '
        f"primitive: every extruded profile is those two features")
    return [sketch, extrude]


def _expand_prisms(ops: Any, notes: list) -> Any:
    """Expand every extruded-polygon feature into its sketch+extrude pair."""
    if not isinstance(ops, list) or not any(
            isinstance(el, dict) and isinstance(el.get("feature"), dict)
            and _prism_spec((el["feature"] or {}).get("type")) is not None for el in ops):
        return ops
    out: list = []
    shift: dict = {}
    for i, el in enumerate(ops):
        pair = None
        if isinstance(el, dict) and isinstance(el.get("feature"), dict):
            pair = _prism_pair(el["feature"], notes)
        if pair is None:
            shift[f"${i}"] = f"${len(out)}"
            out.append(el)
            continue
        shift[f"${i}"] = f"${len(out) + 1}"  # the prism IS the extrude, not its sketch
        out.extend(pair)
    if any(k != v for k, v in shift.items()):
        out = [_shift_batch_placeholders(o, shift) for o in out]
    return out


# ---------------------------------------------------------------------------
# VALIDATION-MISS normalizer (model-robustness).
#
# These are the schema requirements a model reliably forgets, every one of them
# recovered from a real rejected batch in the run ledgers:
#   hole      params.style + params.end are REQUIRED and their vocabulary is
#             simple|tap|cbore|csink / through_all|blind (core/doc/document.cpp) — the
#             model writes "counterbore"/"through", or omits them entirely; it also omits
#             refs.face, which the compiler needs to know WHICH face to drill.
#   sketch    refs.plane must be a RESERVED DATUM, not "XZ" (handled in the sketch pass).
#   boolean   params.op must be fuse|cut|common with operands a/b, not
#             params.operation:"union" + params.targets:[…].
#   extrude/  params.mode is required, and the profile is a refs.region selector, not a
#   revolve   params.sketch name / an inline polyline.
#   any       at/axis/origin take a plain [x,y,z]; only SIZES take {"value":N,"expr":…}.
# Each rewrite is echoed in `interpreted`; anything genuinely ambiguous raises a teaching
# ValueError instead of guessing.
_HOLE_STYLES = frozenset({"simple", "tap", "cbore", "csink"})
_HOLE_STYLE_WORDS = {
    "simple": "simple", "plain": "simple", "straight": "simple", "drilled": "simple",
    "clearance": "simple", "standard": "simple", "normal": "simple", "none": "simple",
    "cbore": "cbore", "counterbore": "cbore", "c_bore": "cbore", "counter_bore": "cbore",
    "counterbored": "cbore", "socket": "cbore", "spotface": "cbore",
    "csink": "csink", "countersink": "csink", "c_sink": "csink", "counter_sink": "csink",
    "countersunk": "csink", "csk": "csink", "flathead": "csink", "chamfered": "csink",
    "tap": "tap", "tapped": "tap", "threaded": "tap", "thread": "tap",
}
_HOLE_ENDS = frozenset({"through_all", "blind"})
_HOLE_END_WORDS = {
    "through_all": "through_all", "through": "through_all", "thru": "through_all",
    "throughall": "through_all", "through_hole": "through_all", "all": "through_all",
    "full": "through_all", "fully": "through_all", "open": "through_all",
    "blind": "blind", "to_depth": "blind", "depth": "blind", "partial": "blind",
    "closed": "blind", "stopped": "blind",
}
_HOLE_STYLE_KEYS = ("style", "hole_style", "hole_type")
_HOLE_END_KEYS = ("end", "end_condition", "termination", "depth_type", "end_type")
_HOLE_DIR_KEYS = ("direction", "dir", "normal", "drill_direction")
_HOLE_AT_KEYS = ("origin", "position", "location", "at", "center", "centre")
# The face word a drill axis names. The SIGN is deliberately dropped: a bore along Z is
# entered from the top face in every real part, and the note says so.
_AXIS_FACE_WORD = ("right", "back", "top")


def _vec3(v: Any) -> Optional[list]:
    if isinstance(v, list) and len(v) == 3 and all(_is_num(x) for x in v):
        return [float(x) for x in v]
    return None


def _vec3_list(v: Any) -> Optional[list]:
    """One [x,y,z] or a non-empty list of them -> a list of float triples; else None."""
    one = _vec3(v)
    if one is not None:
        return [one]
    if isinstance(v, list) and v:
        pts = [_vec3(p) for p in v]
        if all(p is not None for p in pts):
            return pts
    return None


def _face_word_for_axis(v: Any) -> Optional[str]:
    vec = _vec3(v)
    if vec is None:
        if isinstance(v, str):
            w = _slug(v).lstrip("+-")
            return {"x": "right", "y": "back", "z": "top"}.get(w)
        return None
    dom = max(range(3), key=lambda i: abs(vec[i]))
    return _AXIS_FACE_WORD[dom] if abs(vec[dom]) > 0 else None


def _normalize_hole_feature(feat: dict, notes: list) -> dict:
    """style/end defaults + synonyms, and the face the bore is entered through."""
    out, params, peek, consume = _split_two_levels(feat)
    label = _feat_label(feat, "hole")
    changed = False

    style_raw = _MISSING
    for k in _HOLE_STYLE_KEYS:
        v = consume(k)
        if style_raw is _MISSING and v is not _MISSING and v is not None:
            style_raw = v
    end_raw = _MISSING
    for k in _HOLE_END_KEYS:
        v = consume(k)
        if end_raw is _MISSING and v is not _MISSING and v is not None:
            end_raw = v

    # "depth":"through" — an END CONDITION written into the depth slot. The engine reads
    # `depth` as a number, so leaving it there is a hard parse failure later. It arrives
    # BARE and dim-WRAPPED ({"depth":{"value":"through_all"}}, live run 2026-07-28): the
    # model wraps every size the same way, and validate.cpp then rejects the whole batch
    # with "params.depth.value: missing or non-numeric" — a units complaint about
    # something that was never a number. `through_all` IS the engine's through-hole
    # representation (document.cpp:5459 sizes it as 1.1x the body diagonal), so it needs
    # no depth at all once it is read as the end condition.
    depth_v = peek("depth")
    depth_word = depth_v if isinstance(depth_v, str) else (
        depth_v.get("value") if _is_dim_object(depth_v) else None)
    if isinstance(depth_word, str) and _slug(depth_word) in _HOLE_END_WORDS:
        if end_raw is _MISSING:
            end_raw = depth_word
        consume("depth")
        changed = True
        wrapped = "" if depth_v is depth_word else " (wrapped as a dimension object)"
        notes.append(f'{label}: read depth="{depth_word}"{wrapped} as the end condition, not '
                     f'a distance — a hole takes end:"through_all"|"blind" plus a numeric '
                     f"depth when blind")
    elif isinstance(depth_word, str):
        # Neither a distance nor an end word, and _normalize_feature_dims already had its
        # go at "15mm". Bounce it HERE with the vocabulary — left alone it reaches
        # validate.cpp as "params.depth.value: missing or non-numeric", which reads as a
        # units problem for something that was never a number.
        raise ValueError(
            f'hole "{label}": params.depth "{depth_word}" is neither a distance in mm nor an '
            f"end condition. Give a number for a blind hole (depth:15), or "
            f'end:"through_all" to drill straight through.')

    style = None
    if style_raw is not _MISSING:
        w = _slug(style_raw)
        if w in _HOLE_STYLE_WORDS:
            style = _HOLE_STYLE_WORDS[w]
            if style != str(style_raw).strip():
                changed = True
                notes.append(f'{label}: interpreted style "{style_raw}" as "{style}" '
                             f"— hole styles are simple|tap|cbore|csink")
        elif w in _HOLE_END_WORDS and end_raw is _MISSING:
            end_raw = style_raw
            changed = True
            notes.append(f'{label}: "{style_raw}" is an END condition, not a style — moved to '
                         f"end; style defaulted to simple")
        else:
            raise ValueError(
                f'hole "{label}": style "{style_raw}" is not a hole style. Use simple (a plain '
                f'bore), tap (tapped), cbore (counterbored) or csink (countersunk).')
    if style is None:
        if peek("cbore_d") is not _MISSING or peek("cbore_depth") is not _MISSING:
            style = "cbore"
        elif peek("csink_d") is not _MISSING or peek("csink_angle") is not _MISSING:
            style = "csink"
        elif peek("tap") is not _MISSING or peek("thread") is not _MISSING:
            style = "tap"
        else:
            style = "simple"
        changed = True
        notes.append(f'{label}: params.style is required for a hole — defaulted to "{style}" '
                     f"(simple|tap|cbore|csink)")

    end = None
    if end_raw is not _MISSING:
        w = _slug(end_raw)
        if w in _HOLE_END_WORDS:
            end = _HOLE_END_WORDS[w]
            if end != str(end_raw).strip():
                changed = True
                notes.append(f'{label}: interpreted end "{end_raw}" as "{end}" — a hole ends '
                             f'"through_all" or "blind"')
        else:
            raise ValueError(
                f'hole "{label}": end "{end_raw}" is not an end condition. Use '
                f'end:"through_all" (drill straight through) or end:"blind" with a numeric '
                f"params.depth.")
    if end is None:
        depth_now = peek("depth")
        end = "blind" if (_is_num(depth_now) or _is_dim_object(depth_now)) else "through_all"
        changed = True
        notes.append(f'{label}: params.end is required for a hole — defaulted to "{end}"'
                     + (" (a depth was given)" if end == "blind" else " (no depth was given)"))
    if end == "blind" and not (_is_num(peek("depth")) or _is_dim_object(peek("depth"))):
        raise ValueError(
            f'hole "{label}": end:"blind" needs a numeric params.depth (mm). Give the depth, '
            f'or use end:"through_all" to drill through.')

    params["style"] = style
    params["end"] = end

    # A drill DIRECTION names the entry face: that is a rewrite of what the model wrote, so
    # it belongs here. Supplying a face nobody named is a different decision — see
    # _supply_missing_refs, which add_features runs on its own.
    refs = dict(out.get("refs") or {})
    side = None
    for k in _HOLE_DIR_KEYS:
        v = consume(k)
        if v is not _MISSING and v is not None and side is None:
            side = _face_word_for_axis(v)
            changed = True
    if side and not refs.get("face") and not refs.get("positions_sketch"):
        refs["face"] = {"of": "face", "side": side}
        changed = True
        notes.append(f'{label}: interpreted the drill direction as the {side} face '
                     f'(refs.face={{"of":"face","side":"{side}"}}) — a bore is entered '
                     f"through a named planar face, not along a world vector")

    # A WORLD [x,y,z] (or a list of them) where the placement belongs. It is NOT a face-local
    # [u,v]: positions are measured from the face's outer-boundary centroid along u = world
    # +X projected, v = n×u (document.cpp hole_face_frame), so copying [x,y] across drilled
    # the wrong spot on any off-centre face and dropped z on a side face. world_points
    # carries the points as given: each names a point on the bore AXIS, which the engine
    # projects along the face normal onto refs.face (refusing one whose axis misses it).
    wp_now = peek("world_points")
    if (not refs.get("positions") and not refs.get("points")
            and (wp_now is _MISSING or wp_now is None)):
        placed = peek("positions")
        if placed is not _MISSING and placed is not None:
            pts = _vec3_list(placed)
            if pts is not None:
                consume("positions")
                params["world_points"] = pts
                changed = True
                notes.append(f"{label}: positions={placed} are [x,y,z] WORLD points, not "
                             f"face-local [u,v] pairs — sent as params.world_points={pts}; the "
                             f"engine projects each along refs.face's normal onto that face "
                             f"(a point on the bore axis)")
        else:
            for k in _HOLE_AT_KEYS:
                pts = _vec3_list(peek(k))
                if pts is None:
                    continue
                consume(k)
                changed = True
                params["world_points"] = pts
                notes.append(f"{label}: read {k}={pts if len(pts) > 1 else pts[0]} as a WORLD "
                             f"point on the bore axis — params.world_points={pts}; the engine "
                             f"projects it along refs.face's normal onto that face. Face-local "
                             f"positions [[u,v]] are measured from the face centre, not the "
                             f"world origin")
                break

    if not changed:
        return feat
    out["params"] = params
    out["refs"] = refs
    return out


def _normalize_bolt_circle_feature(feat: dict, notes: list) -> dict:
    """A WORLD [x,y,z] ring centre -> params.world_center.

    The engine centres the ring on the face centre and reads no other centre key, so a
    `center:[x,y,z]` used to be ignored and the ring landed on the face centre. Same
    projection rule as hole world_points (document.cpp): a point on the ring's axis,
    projected along refs.face's normal.
    """
    out, params, peek, consume = _split_two_levels(feat)
    wc = peek("world_center")
    if wc is not _MISSING and wc is not None:
        return feat
    for k in _HOLE_AT_KEYS:
        vec = _vec3(peek(k))
        if vec is None:
            continue
        consume(k)
        params["world_center"] = vec
        notes.append(f'{_feat_label(feat, "bolt_circle")}: read {k}={vec} as the ring centre '
                     f"WORLD point — params.world_center={vec}; the engine projects it along "
                     f"refs.face's normal onto that face")
        out["params"] = params
        return out
    return feat


_BOOLEAN_OP_WORDS = {
    "fuse": "fuse", "union": "fuse", "add": "fuse", "join": "fuse", "merge": "fuse",
    "combine": "fuse", "weld": "fuse", "boss": "fuse", "plus": "fuse",
    "cut": "cut", "subtract": "cut", "difference": "cut", "remove": "cut", "minus": "cut",
    "common": "common", "intersect": "common", "intersection": "common",
}
_BOOLEAN_OP_KEYS = ("op", "operation", "boolean", "mode", "action", "kind")
_BOOLEAN_OPERAND_KEYS = ("targets", "bodies", "operands", "inputs", "features", "tools")
# The SINGULAR operand pair. `{"op":"boolean","target":"body","tool":"boss","kind":"union"}`
# is the flat op a model writes for a boolean (live run 2026-07-27) and it died on «needs
# its two operands» because only the plural LIST spellings above were read. target = the
# body that is kept (params.a), tool = the body applied to it (params.b) — the roles are
# named, so there is nothing to guess.
_BOOLEAN_TARGET_KEYS = ("target", "targets", "base", "target_body", "base_body")
_BOOLEAN_TOOL_KEYS = ("tool", "tools", "tool_body", "cutter")


def _operand_name(v: Any) -> Optional[str]:
    if isinstance(v, str) and v.strip():
        return v.strip()
    if isinstance(v, dict):
        for k in ("owner_feature", "feature", "id", "body", "name"):
            s = v.get(k)
            if isinstance(s, str) and s.strip():
                return s.strip()
    return None


def _one_operand(v: Any) -> Optional[str]:
    """One operand name from a body reference, or a one-element list holding one."""
    if isinstance(v, list):
        return _one_operand(v[0]) if len(v) == 1 else None
    return _operand_name(v)


#: A selector slot the model explicitly EMPTIED. Live run 2026-07-28: a shell with
#: refs.faces:"none" and, on the retry, refs.remove:[] — both hard-rejected («'none' is
#: neither a reserved datum…», «must be a non-empty list»). The model was saying "remove
#: no faces", which is a legal closed shell; the slot should simply not be there.
_EMPTY_SLOT_WORDS = frozenset({"none", "null", "nil", "nothing", "no", "n/a", "n_a", "na",
                               "", "empty", "undefined", "no_faces", "not_applicable"})


def _is_empty_slot(v: Any) -> bool:
    if isinstance(v, str):
        return _slug(v) in _EMPTY_SLOT_WORDS
    if isinstance(v, list):
        return len(v) == 0 or all(_is_empty_slot(x) for x in v)
    return v is None


def _drop_empty_ref_slots(ops: Any, notes: list) -> Any:
    """Delete every ref slot the model emptied. An absent slot is a legal "no selection";
    an empty one is a rejection, and the two mean the same thing."""
    if not isinstance(ops, list):
        return ops
    out, hits = [], []
    for op in ops:
        feat = op.get("feature") if isinstance(op, dict) else None
        refs = feat.get("refs") if isinstance(feat, dict) else None
        if not isinstance(refs, dict):
            out.append(op)
            continue
        kept = {k: v for k, v in refs.items() if not _is_empty_slot(v)}
        if len(kept) == len(refs):
            out.append(op)
            continue
        dropped = sorted(set(refs) - set(kept))
        hits.extend(dropped)
        nf = {k: v for k, v in feat.items() if k != "refs"}
        if kept:
            nf["refs"] = kept
        out.append({**op, "feature": nf})
    if not hits:
        return ops
    notes.append(
        "dropped the empty ref slot(s) " + ", ".join(sorted(set(hits)))
        + " — an emptied selector slot means no selection, which is an ABSENT slot; "
          "a shell with no removed faces is a closed hollow, not an error")
    return out


#: A primitive carrying its own boolean intent. Live run 2026-07-28:
#: {"type":"cylinder",…,"refs":{"boolean":"subtract","target":"f2"}} — the model wants the
#: cylinder subtracted, and the engine already does exactly that with params.mode:"cut".
#: No extra boolean feature is needed, and inventing one would add a body the part
#: does not want.
_INLINE_BOOLEAN_KEYS = ("boolean", "boolean_op", "operation", "bool_op", "combine")
_INLINE_BOOLEAN_MODE = {"subtract": "cut", "cut": "cut", "difference": "cut",
                        "remove": "cut", "minus": "cut", "union": "boss", "add": "boss",
                        "fuse": "boss", "join": "boss", "boss": "boss", "merge": "boss"}
_MODE_TYPES = frozenset({"box", "cylinder", "cone", "sphere", "torus", "extrude",
                         "revolve", "sweep", "loft", "rib"})


def _normalize_inline_boolean(ops: Any, notes: list) -> Any:
    """A boolean word attached to a PRIMITIVE -> params.mode, which is the same thing."""
    if not isinstance(ops, list):
        return ops
    out, hits = [], []
    for op in ops:
        feat = op.get("feature") if isinstance(op, dict) else None
        if not isinstance(feat, dict) or _slug(feat.get("type")) not in _MODE_TYPES:
            out.append(op)
            continue
        nf = dict(feat)
        params = dict(nf.get("params")) if isinstance(nf.get("params"), dict) else {}
        refs = dict(nf.get("refs")) if isinstance(nf.get("refs"), dict) else {}
        mode = None
        for holder in (refs, params):
            for k in _INLINE_BOOLEAN_KEYS:
                word = _INLINE_BOOLEAN_MODE.get(_slug(holder.get(k)))
                if word:
                    mode = mode or word
                    holder.pop(k, None)
        if mode is None:
            out.append(op)
            continue
        # The companion `target` names the body being cut. A mode:"cut" primitive already
        # cuts every body it intersects and these types have no target slot, so keeping it
        # would only re-raise "unknown ref slot".
        target = next((refs.pop(k) for k in ("target", "into", "target_body")
                       if k in refs), None)
        params.setdefault("mode", mode)
        nf["params"] = params
        if refs:
            nf["refs"] = refs
        else:
            nf.pop("refs", None)
        hits.append((_feat_label(feat, "feature"), mode, target))
        out.append({**op, "feature": nf})
    for label, mode, target in hits:
        notes.append(
            f'{label}: read the boolean word as params.mode="{mode}" — a primitive '
            f"removes material itself, so this is one feature, not a primitive plus a "
            f"boolean" + (f'; dropped target "{target}" (a cut applies to every body it '
                          f"intersects)" if target else ""))
    return ops if not hits else out


def _unmerge_boolean_operands(ops: Any, notes: list) -> Any:
    """A body this batch feeds to a boolean must stay a SEPARATE solid.

    Live run 2026-07-28: a sphere built inside a box to be subtracted was merged into it
    on creation, so it "added no material" and the boolean's operand did not exist —
    «ref 'b' -> 'f2' not found». The model cannot know this; the batch says it.
    """
    if not isinstance(ops, list):
        return ops
    wanted: set = set()
    by_name: dict = {}
    for i, op in enumerate(ops):
        feat = op.get("feature") if isinstance(op, dict) else None
        if isinstance(feat, dict) and isinstance(feat.get("name"), str):
            by_name.setdefault(feat["name"].strip(), i)
    for op in ops:
        feat = op.get("feature") if isinstance(op, dict) else None
        if not isinstance(feat, dict) or _slug(feat.get("type")) != "boolean":
            continue
        params = feat.get("params") if isinstance(feat.get("params"), dict) else {}
        for slot in ("a", "b"):
            v = params.get(slot)
            if not isinstance(v, str):
                continue
            v = v.strip()
            if _BATCH_REF_RE.match(v):
                wanted.add(int(v[1:]))
            elif v in by_name:
                wanted.add(by_name[v])
    hits = []
    out = list(ops)
    for i in sorted(wanted):
        feat = out[i].get("feature") if isinstance(out[i], dict) else None
        if not isinstance(feat, dict) or _slug(feat.get("type")) == "boolean":
            continue
        params = dict(feat.get("params")) if isinstance(feat.get("params"), dict) else {}
        if params.get("merge") is False:
            continue
        params["merge"] = False
        out[i] = {**out[i], "feature": {**feat, "params": params}}
        hits.append(_feat_label(feat, f"op {i}"))
    if not hits:
        return ops
    notes.append(
        "set merge:false on " + ", ".join(hits) + " — a body this batch hands to a "
        "boolean has to stay a separate solid, or it is absorbed on creation and the "
        "boolean's operand no longer exists")
    return out


#: `{"op":"boolean_subtract","target":"f1","tool":"f2"}` (live run 2026-07-28). The op
#: name is invented, but it carries its operation AND both operands — a complete boolean.
_COMPOUND_BOOLEAN_RE = re.compile(
    r"^(?:boolean[_-]?)?(subtract|difference|minus|cut|union|add|fuse|join|"
    r"intersect|intersection|common)(?:[_-]?boolean)?$")


def _expand_compound_boolean_ops(ops: Any, notes: list) -> Any:
    """An invented `boolean_<word>` op -> the canonical boolean add_feature."""
    if not isinstance(ops, list):
        return ops
    out, hits = [], []
    for el in ops:
        m = _COMPOUND_BOOLEAN_RE.match(_slug(el.get("op"))) if isinstance(el, dict) else None
        if not m or isinstance(el.get("feature"), dict):
            out.append(el)
            continue
        word = m.group(1)
        rest = {k: v for k, v in el.items() if k != "op"}
        name = rest.pop("name", None)
        feat = {"type": "boolean", "params": {"op": word, **rest}}
        if name is not None:
            feat["name"] = name
        out.append({"op": "add_feature", "feature": feat})
        hits.append(_slug(el.get("op")))
    if not hits:
        return ops
    notes.append(
        'read op "' + '", "'.join(sorted(set(hits))) + '" as '
        '{"op":"add_feature","feature":{"type":"boolean","params":{"op":"cut|fuse|common",'
        '"a":…,"b":…}}} — a boolean is a FEATURE; the only ops are add_feature, '
        "edit_feature and delete_feature")
    return out


def _normalize_boolean_feature(feat: dict, notes: list) -> dict:
    """params.operation:"union" + targets:[a,b] -> params.op:"fuse" + params.a/b."""
    out, params, peek, consume = _split_two_levels(feat)
    label = _feat_label(feat, "boolean")
    changed = False

    raw_op = _MISSING
    for k in _BOOLEAN_OP_KEYS:
        v = peek(k)
        if v is _MISSING or v is None:
            continue
        if k != "op":
            consume(k)
            changed = True
        raw_op = v
        break
    if raw_op is not _MISSING:
        word = _slug(raw_op)
        canon = _BOOLEAN_OP_WORDS.get(word)
        if canon is None:
            raise ValueError(
                f'boolean "{label}": op "{raw_op}" is not a boolean operation. Use '
                f'"fuse" (union), "cut" (subtract) or "common" (intersect).')
        if canon != str(raw_op).strip() or params.get("op") != canon:
            changed = changed or params.get("op") != canon
            if params.get("op") != canon:
                notes.append(f'{label}: interpreted boolean op "{raw_op}" as "{canon}" '
                             f"— the engine's operations are fuse|cut|common")
        params["op"] = canon

    if params.get("a") is None or params.get("b") is None:
        operands: list = []
        for k in _BOOLEAN_OPERAND_KEYS:
            v = peek(k)
            if isinstance(v, list) and len(v) >= 2:
                operands = [_operand_name(x) for x in v[:2]]
                consume(k)
                break
        if not operands:
            tv = (out.get("refs") or {}).get("targets")
            if isinstance(tv, list) and len(tv) >= 2:
                operands = [_operand_name(x) for x in tv[:2]]
        pair_note = None
        if not operands:
            # The pair may sit in `refs` instead of `params`: a live 2026-07-28 batch sent
            # refs.base=[{"kind":"feature","id":"f1"}] + refs.tool=[…], which reads as
            # exactly the two operands and died anyway because only params was consulted.
            refs_in = out.get("refs") if isinstance(out.get("refs"), dict) else {}

            def _peek_either(k: str) -> Any:
                v = peek(k)
                return refs_in.get(k, _MISSING) if v is _MISSING else v

            tgt = next((k for k in _BOOLEAN_TARGET_KEYS
                        if _one_operand(_peek_either(k)) is not None), None)
            tol = next((k for k in _BOOLEAN_TOOL_KEYS
                        if _one_operand(_peek_either(k)) is not None), None)
            if tgt and tol:
                def _take(k: str) -> Any:
                    if peek(k) is not _MISSING:
                        return consume(k)
                    return refs_in.pop(k)

                operands = [_one_operand(_take(tgt)), _one_operand(_take(tol))]
                out["refs"] = refs_in or None
                if out["refs"] is None:
                    out.pop("refs")
                pair_note = (f'{label}: read {tgt}="{operands[0]}" as params.a (the body '
                             f'kept) and {tol}="{operands[1]}" as params.b (the body '
                             f"applied to it) — a boolean's operands are params.a/params.b")
        if len(operands) == 2 and all(operands):
            params["a"], params["b"] = operands
            changed = True
            notes.append(pair_note or
                         f'{label}: interpreted the operand list as a="{operands[0]}", '
                         f'b="{operands[1]}" — a boolean fuses/cuts exactly two bodies')
        elif params.get("op") is not None:
            raise ValueError(
                f'boolean "{label}": needs its two operands — params.a and params.b, each a '
                f'feature id ("f1") or a same-batch placeholder ("$0"). Build both bodies with '
                f'"merge":false first so they stay separate solids.')
    if not changed:
        return feat
    out["params"] = params
    return out


_PROFILE_MODE_TYPES = frozenset({"extrude", "revolve", "loft", "sweep"})
_PROFILE_SOURCE_KEYS = ("profile", "sketch", "section", "profile_sketch", "sketch_id",
                        "source_sketch", "sections")
# A loft's profiles live ONLY in refs.sections (validate.cpp: SelList, required). The
# plural spellings a model reaches for — profiles / sketches — name that same slot, so
# they are unambiguous aliases; a genuinely unknown slot keeps the engine's error.
_LOFT_SECTION_ALIASES = ("profiles", "sketches")
# Dict keys that wrap the real list one level too deep ({"sections":[…]}).
_LOFT_SECTION_WRAP_KEYS = ("sections",) + _LOFT_SECTION_ALIASES


def _loft_section_list(v: Any) -> Optional[list]:
    """A loft sections value in any unambiguous dialect -> the canonical selector LIST.

    A bare string is one sketch name; a selector dict is ONE section (the value should
    have been a list); a dict whose only key is sections/profiles/sketches is the list
    nested one level too deep. None when the shape says nothing certain — an empty list,
    a dict that is neither wrapper nor selector, a non-string/selector member — so the
    engine's error names it instead of this inventing sections.
    """
    if isinstance(v, str):
        name = v.strip()
        return [{"kind": "region", "sketch": name, "hint_index": 0}] if name else None
    if isinstance(v, dict):
        wrap = [k for k in _LOFT_SECTION_WRAP_KEYS if isinstance(v.get(k), list)]
        if len(wrap) == 1 and set(v) == {wrap[0]}:
            return _loft_section_list(v[wrap[0]])
        if v.get("kind") or v.get("of") or isinstance(v.get("sketch"), str):
            return [v]
        return None
    if isinstance(v, list):
        if not v:
            return None
        out: list = []
        for el in v:
            if isinstance(el, str) and el.strip():
                out.append({"kind": "region", "sketch": el.strip(), "hint_index": 0})
            elif isinstance(el, dict):
                out.append(el)
            else:
                return None
        return out
    return None
_AXIS_SOURCE_TYPES = frozenset({"revolve", "circular_pattern", "surf_revolve", "helix"})


def _is_inline_geometry(v: Any) -> bool:
    """True when a profile source carries COORDINATES instead of naming a sketch.

    A sketch is named ("Profile", "f1", "$0"); anything holding numbers is geometry
    the model inlined. Both shapes it writes count: {"points": …}/{"entities": …} and
    the bare point list [[335,0],[345,0],…] — the second used to slip through and
    resurface as the unhelpful "refs.profile: required for revolve"."""
    if isinstance(v, dict):
        return bool(v.get("points") or v.get("entities"))
    if isinstance(v, list):
        return any(isinstance(p, (list, tuple)) and len(p) >= 2
                   and all(isinstance(c, (int, float)) and not isinstance(c, bool)
                           for c in p[:2])
                   for p in v)
    return False


# The datum plane to sketch a profile on for a revolve about each axis: the plane must
# CONTAIN the axis (revolving about a plane's own NORMAL sweeps nothing and the engine
# rejects it as a zero-volume solid). Verified live against all nine combinations.
_PLANE_CONTAINING = {"axis.x": "plane.top", "axis.y": "plane.right", "axis.z": "plane.front"}


def _normalize_profile_feature(feat: dict, ftype: str, notes: list) -> dict:
    """`params.sketch:"Profile"` + `params.axis:"Z"` -> the refs the engine requires."""
    out, params, peek, consume = _split_two_levels(feat)
    refs = dict(out.get("refs") or {})
    label = _feat_label(feat, ftype)
    changed = False

    if not refs.get("profile") and not refs.get("sections"):
        keys = _PROFILE_SOURCE_KEYS + (
            ("profiles", "sketches") if ftype == "loft" else ())
        for k in keys:
            v = peek(k)
            if v is _MISSING or v is None:
                continue
            if _is_inline_geometry(v):
                av = peek("axis")
                plane = _PLANE_CONTAINING.get(
                    _norm_datum(av) if isinstance(av, str) else "", "plane.top")
                raise ValueError(
                    f'{ftype} "{label}": params.{k} carries INLINE geometry. A profile is '
                    f"always a sketch FEATURE, so this needs two ops in the same batch — "
                    f'{{"op":"add_feature","feature":{{"type":"sketch","name":"{label}Profile",'
                    f'"params":{{"plane":"{plane}","entities":[{{"type":"line","p1":[x1,y1],'
                    f'"p2":[x2,y2]}}, …closing the loop]}}}}}} first, then this {ftype} with '
                    f'refs.profile=[{{"kind":"region","sketch":"$<index of that sketch op>",'
                    f'"hint_index":0}}] and no params.{k}. Sketch coordinates are 2D on that '
                    f'plane — "{plane}" is one that CONTAINS the axis, which a revolve needs.')
            if ftype == "loft" and isinstance(v, (list, dict)):
                canon = _loft_section_list(v)
                if canon is None:
                    continue  # ambiguous shape — the engine names it better than a guess
                consume(k)
                refs["sections"] = canon
                changed = True
                notes.append(
                    f'{label}: interpreted params.{k} as refs.sections ({len(canon)} '
                    f"region selector(s)) — a loft's profiles are a LIST of "
                    f'{{"kind":"region","sketch":…}} selectors under refs.sections, '
                    f"not a params.{k} value")
                break
            if isinstance(v, str) and v.strip():
                consume(k)
                slot = "sections" if ftype == "loft" else "profile"
                refs[slot] = [{"kind": "region", "sketch": v.strip(), "hint_index": 0}]
                changed = True
                notes.append(
                    f'{label}: interpreted params.{k}="{v}" as refs.{slot}='
                    f'[{{"kind":"region","sketch":"{v}","hint_index":0}}] — a profile is a '
                    f"region selector on a sketch feature, not a param")
                break

    if ftype in _AXIS_SOURCE_TYPES and not refs.get("axis"):
        v = peek("axis")
        datum = _norm_datum(v) if isinstance(v, str) else None
        if datum in _DATUM_TOKENS:
            consume("axis")
            refs["axis"] = datum
            changed = True
            notes.append(f'{label}: interpreted params.axis="{v}" as refs.axis="{datum}" '
                         f"— the axis of revolution is a datum REF, not a param")

    if ftype in _PROFILE_MODE_TYPES and params.get("mode") is None:
        params["mode"] = "cut" if out.get("mode") == "cut" else "boss"
        out.pop("mode", None)
        changed = True
        notes.append(f'{label}: params.mode is required for a {ftype} — defaulted to '
                     f'"{params["mode"]}" (boss adds material, cut removes it)')

    # A revolve with no angle means a FULL revolution — the single most common revolve
    # intent (a bucket, a knob, a shaft); omitting it used to bounce the whole batch on
    # "params.angle: required for revolve" (live copilot run, 2026-07-27).
    if ftype == "revolve" and params.get("angle") is None:
        params["angle"] = 360
        changed = True
        notes.append(f'{label}: params.angle is required for a revolve — defaulted to 360 '
                     f'(a full revolution; pass angle=180 etc. for a partial one)')

    if not changed:
        return feat
    out["params"] = params
    out["refs"] = refs
    return out


# ---------------------------------------------------------------------------
# INLINE-SKETCH AUTO-SPLIT (model-robustness).
#
# The single most frequent modeling dialect failure in the live ledger: the model writes
# the whole profile INSIDE the feature that consumes it —
# {"type":"extrude","params":{"depth":…,"sketch":{"plane":"XY","entities":[…]}}} — because
# to a person that is one thought, not two. `_normalize_profile_feature` raises a good
# teaching error for it, but when the inline value names BOTH its plane and its geometry
# the transformation is purely MECHANICAL: lift it into its own sketch op ahead of the
# consumer and point the consumer's ref at it. Anything less than certain (no resolvable
# plane, a shapeless value) falls through UNTOUCHED so that teaching error still fires —
# an auto-split that builds the wrong geometry is far worse than a clear rejection.
#
# Covers every engine feature whose refs carry a profile/sections SelList
# (core/doc/validate.cpp::FEATURE_TYPES), not just extrude.
_INLINE_PROFILE_TYPES = frozenset({
    "extrude", "revolve", "sweep", "loft", "rib", "sm_base_flange",
    "surf_extrude", "surf_revolve", "surf_sweep", "surf_planar",
})
#: Where an inline sketch names the plane it is drawn on.
_INLINE_PLANE_KEYS = ("plane", "sketch_plane", "on", "datum", "reference_plane")


def _inline_plane(src: Any) -> Optional[str]:
    """The reserved datum token `src` names a sketch plane as, or None."""
    if not isinstance(src, dict):
        return None
    for k in _INLINE_PLANE_KEYS:
        v = src.get(k)
        tok = _norm_datum(v) if isinstance(v, str) else None
        if tok in _DATUM_TOKENS:
            return tok
    return None


def _inline_sketch_members(v: Any, ftype: str) -> Optional[list]:
    """The inline sketch dict(s) a profile slot carries, or None when it carries none.

    A canonical region selector ({"kind":"region","sketch":"P"}) holds no entities/points,
    so it is never mistaken for one. Only a loft may carry a LIST of them — its sections.
    """
    def _one(x: Any) -> bool:
        return isinstance(x, dict) and bool(x.get("entities") or x.get("points"))
    if _one(v):
        return [v]
    if ftype == "loft" and isinstance(v, list) and v and all(_one(x) for x in v):
        return list(v)
    return None


def _inline_sketch_feature(src: dict, name: str, plane: str) -> Optional[dict]:
    """A canonical sketch FEATURE for one inline profile, or None when it is ambiguous.

    None for anything the value does not say outright — an entity list that is not typed
    entity dicts and no usable point loop behind it — so the caller leaves the feature
    alone and the teaching error survives.
    """
    ents = src.get("entities")
    if isinstance(ents, list) and ents and all(
            isinstance(e, dict) and isinstance(e.get("type"), str) for e in ents):
        ents = [dict(e) for e in ents]
    else:
        pts = src.get("points")
        if not (isinstance(pts, list) and len(pts) >= 3 and all(
                isinstance(p, (list, tuple)) and len(p) >= 2
                and all(_is_num(c) for c in p[:2]) for p in pts)):
            return None
        # A closed polyline is what a bare point loop IS; _normalize_sketch_ops expands it
        # into the N `line` entities the engine's vocabulary actually has.
        ents = [{"type": "polyline", "closed": True,
                 "points": [[float(p[0]), float(p[1])] for p in pts]}]
    return {"type": "sketch", "name": name, "refs": {"plane": plane},
            "params": {"entities": ents, "constraints": []}}


def _split_inline_profile_sketches(ops: Any, notes: list) -> Any:
    """Lift an inline profile into its own sketch op AHEAD of the feature consuming it."""
    if not isinstance(ops, list):
        return ops
    used = set()
    for el in ops:
        f = el.get("feature") if isinstance(el, dict) else None
        if isinstance(f, dict) and isinstance(f.get("name"), str):
            used.add(f["name"].strip())
    out: list = []
    shift: dict = {}
    changed = False
    for i, op in enumerate(ops):
        feat = op.get("feature") if isinstance(op, dict) else None
        ftype = _slug(feat.get("type")) if isinstance(feat, dict) else ""
        made: list = []
        if (isinstance(op, dict) and op.get("op") == "add_feature"
                and isinstance(feat, dict) and ftype in _INLINE_PROFILE_TYPES):
            params = dict(feat.get("params") or {})
            refs = dict(feat.get("refs") or {})
            slot = "sections" if ftype == "loft" else "profile"
            keys = _PROFILE_SOURCE_KEYS + (
                _LOFT_SECTION_ALIASES if ftype == "loft" else ())
            label = _feat_label(feat, ftype)
            for k in keys:
                bag = params if k in params else (refs if k in refs else None)
                if bag is None:
                    continue
                members = _inline_sketch_members(bag[k], ftype)
                if members is None:
                    continue
                built: Optional[list] = []
                for n, src in enumerate(members):
                    # The plane may sit on the inline value or on the consuming feature;
                    # both say it outright. Nowhere else — a guessed plane is wrong
                    # geometry that builds successfully, the worst possible outcome.
                    plane = (_inline_plane(src) or _inline_plane(params)
                             or _inline_plane(refs))
                    base = f"{label}Profile" + (str(n + 1) if len(members) > 1 else "")
                    name, bump = base, 2
                    while name in used:
                        name, bump = f"{base}{bump}", bump + 1
                    sk = _inline_sketch_feature(src, name, plane) if plane else None
                    if sk is None:
                        built = None
                        break
                    used.add(name)
                    built.append(sk)
                if not built:
                    continue  # ambiguous — the teaching error names it better than a guess
                bag.pop(k)
                refs[slot] = [{"kind": "region", "sketch": s["name"], "hint_index": 0}
                              for s in built]
                made = [{"op": "add_feature", "feature": s} for s in built]
                op = {**op, "feature": {**feat, "params": params, "refs": refs}}
                changed = True
                names = ", ".join(f'"{s["name"]}"' for s in built)
                notes.append(
                    f'{label}: {"params" if bag is params else "refs"}.{k} carried the '
                    f"profile INLINE — lifted it into its own sketch feature {names} on "
                    f'{built[0]["refs"]["plane"]}, placed BEFORE this {ftype} in the same '
                    f"batch, and pointed refs.{slot} at it. A profile is always a sketch "
                    f"FEATURE the {ftype} references by its \"$<index>\" batch position")
                break
        shift[f"${i}"] = f"${len(out) + len(made)}"
        out.extend(made)
        out.append(op)
    if not changed:
        return ops
    if any(k != v for k, v in shift.items()):
        out = [_shift_batch_placeholders(o, shift) for o in out]
    return out


# A ref slot -> the selector kind its members must carry.
_REF_SLOT_KIND = {
    "face": "face", "faces": "face", "target_face": "face", "middle_face": "face",
    "face_set_a": "face", "face_set_b": "face", "remove": "face", "parting": "face",
    "neutral": "face",
    "edges": "edge", "edge": "edge", "edge_a": "edge", "edge_b": "edge",
    "vertices": "vertex",
    "targets": "body", "tools": "body", "bodies": "body",
}
# Slots whose bare-string value must be a reserved datum / feature id.
_DATUM_REF_SLOTS = frozenset({"plane", "axis", "dir1", "dir2", "pull", "base", "path"})
# Ref slots the engine types Sel / SelOrDatum (validate.cpp::FEATURE_TYPES) — ONE selector
# object or datum string, never a list. A model that wraps the single selector in a list
# ("axis":[{"kind":"datum",…}]) died on «selector must be an object»; a ONE-element list
# says exactly one thing, so unwrap it. A multi-element list keeps the error — that is a
# real disagreement about the slot, not a spelling.
_SINGLE_REF_SLOTS = frozenset({
    "plane", "axis", "base", "second", "a", "b", "target", "origin", "x_dir", "y_dir",
    "offset", "at", "up_to", "up_to2", "start_ref", "end_ref", "end2_ref", "path",
    "centerline", "target_face", "middle_face", "reference_face", "neutral", "pull",
    "face", "positions_sketch", "dir1", "dir2", "edge", "sketch",
})
# Ref slots the engine types SelList (validate.cpp::FEATURE_TYPES) — a LIST of selectors or
# queries. A model that hands one bare selector/query to a list slot said exactly one thing,
# so wrap it; that is the first rung of the live 2026-07-28 fillet ladder
# («refs.edges: must be a non-empty list»). dir1/dir2 and `positions` are deliberately out:
# dir1/dir2 are SelList only on surf_boundary and SelOrDatum on asm_linear_pattern, and
# `positions` is a list of face-local [u,v] pairs, not selectors.
_LIST_REF_SLOTS = frozenset({
    "profile", "sections", "guides", "edges", "faces", "vertices", "remove", "parting",
    "face_set_a", "face_set_b", "targets", "tools", "bodies", "surfaces", "points",
})
# The keys that make a dict a geometry QUERY rather than a topology selector.
_QUERY_KEYS = frozenset({"side", "surf", "nearest", "largest", "smallest", "index", "at",
                         "all", "where", "contains", "min", "max"})


def _repair_selector(v: Any, kind: str) -> Any:
    """Give a kind-less ref member the shape validate_selector / validate_geom_query wants."""
    if isinstance(v, str) and v.strip() and kind == "body":
        return {"kind": "body", "owner_feature": v.strip()}
    if not isinstance(v, dict) or v.get("kind") or v.get("of"):
        return v
    if kind in ("face", "edge") and (set(v) & _QUERY_KEYS):
        return {"of": kind, **v}
    owner = _operand_name(v)
    if owner and kind == "body":
        return {"kind": "body", "owner_feature": owner}
    return v


def _repair_datum_selector(v: Any) -> Optional[dict]:
    """`{"kind":"datum","id":"Z"}` -> `{"kind":"datum","feature":"axis.z"}`.

    A datum selector names its datum under `feature` and validate_selector accepts nothing
    else; models reach for id/name/ref. None when there is nothing unambiguous to fix —
    already canonical, not a datum selector, or a word that is no datum the engine knows.
    """
    if not isinstance(v, dict) or _slug(v.get("kind")) != "datum":
        return None
    if _is_canon_datum(v.get("feature")):
        return None
    for k in ("feature", "datum", "id", "name", "ref"):
        token = _norm_datum(v.get(k))
        if token and _is_canon_datum(token):
            out = dict(v)
            if k != "feature":
                out.pop(k, None)
            out["feature"] = token
            return out
    return None

#: Direction-ish datum ref slots — the only members of _DATUM_REF_SLOTS where an arbitrary
#: [x,y,z] vector unambiguously names a reserved axis (nearest cardinal). "plane"/"base"/
#: "path" need a PLANE or a point/curve, not a direction, so a vector there stays untouched.
_AXIS_DIRECTION_SLOTS = frozenset({"axis", "dir1", "dir2", "pull"})


def _axis_vector_to_datum(v: Any) -> Optional[str]:
    """A bare/mis-wrapped direction vector for an axis-ish ref slot -> the nearest cardinal
    reserved axis ("axis.x"/"axis.y"/"axis.z").

    An axis/dir ref slot only ever accepts a reserved datum token or a real selector object
    — the engine has no "arbitrary 3D direction" selector kind, so [0,0,1] dies with
    "selector must be an object" (it is a JSON array) and a pseudo-selector like
    {"centroid":[0,0,1]} dies one rung later with "refs.axis.kind: not in […]" /
    "refs.axis.centroid: must be [x,y,z]" (a real region/face selector, wrong shape). Every
    observed case is a model naming X/Y/Z the way it would to a human — snap to the
    dominant component. None when the value carries no recognizable vector at all.
    """
    vec: Optional[list] = None
    if isinstance(v, (list, tuple)) and len(v) == 3 and all(_is_num(x) for x in v):
        vec = [float(x) for x in v]
    elif isinstance(v, dict):
        if all(_is_num(v.get(k)) for k in ("x", "y", "z")):
            vec = [float(v["x"]), float(v["y"]), float(v["z"])]
        else:
            # The wrappers a model actually writes around a direction. `centroid` is the
            # pseudo-selector shape; the rest are the plain-English ones — live
            # 2026-08-04 a revolve arrived with refs.axis={"origin":[…],"direction":[0,0,1]}
            # and died on «refs.axis.kind: absent not in [...]», which says nothing about
            # the direction it did supply. `origin` is IGNORED on purpose: a reserved axis
            # passes through the world origin, so honouring an offset would be a lie.
            for key in ("centroid", "direction", "dir", "vector", "axis"):
                c = v.get(key)
                if isinstance(c, (list, tuple)) and len(c) == 3 and all(_is_num(x) for x in c):
                    vec = [float(x) for x in c]
                    break
    if vec is None or not any(vec):
        return None
    dominant = max(range(3), key=lambda i: abs(vec[i]))
    return ("axis.x", "axis.y", "axis.z")[dominant]


#: A ref-slot synonym valid ONLY because the feature type has exactly one ref slot that
#: could plausibly mean it — mirrors validate.cpp's REF_SLOT_ALIASES (single-slot types),
#: but applied client-side so the "interpreted" note reaches the model the SAME turn
#: instead of round-tripping to the engine and back. shell's only ref slot is `remove`
#: (live 2026-07-28: refs.faces rejected with "unknown ref slot for shell"); extrude has
#: SEVERAL ref slots (profile/up_to/up_to2/…), so the engine deliberately does not guess
#: there — but "face" can only ever mean the sketch profile a model calls a face, so alias
#: it too (live: refs.face rejected the same way).
_TYPE_REF_ALIASES = {
    "shell": {"faces": "remove", "open_faces": "remove", "open": "remove",
              "remove_faces": "remove"},
    "extrude": {"face": "profile"},
    "surf_extrude": {"face": "profile"},
}


def _normalize_feature_refs(feat: dict, ftype: str, notes: list) -> dict:
    """Datum words + kind-less selectors in `refs`. Identity when everything is canonical."""
    refs = feat.get("refs")
    if not isinstance(refs, dict) or not refs:
        return feat
    label = _feat_label(feat, ftype)
    out_refs = dict(refs)
    changed = False
    alias_map = _TYPE_REF_ALIASES.get(ftype)
    if alias_map:
        for alias, target in alias_map.items():
            if alias in out_refs and target not in out_refs:
                out_refs[target] = out_refs.pop(alias)
                changed = True
                notes.append(f"{label}: interpreted refs.{alias} as refs.{target} — "
                             f"{ftype} accepts refs.{target}")
        refs = dict(out_refs)  # re-snapshot so the loop below reads the renamed keys
    for slot, v in refs.items():
        if (slot in _SINGLE_REF_SLOTS and isinstance(v, list) and len(v) == 1
                and isinstance(v[0], (dict, str))):
            v = v[0]
            out_refs[slot] = v
            changed = True
            notes.append(f"{label}: unwrapped the one-element list in refs.{slot} — that "
                         f"slot takes ONE selector; only a multi-selector slot "
                         f"(refs.profile, refs.edges, refs.faces) is a list")
        if slot in _DATUM_REF_SLOTS:
            if isinstance(v, str):
                datum = _norm_datum(v)
                if datum != v:
                    out_refs[slot] = datum
                    changed = True
                    notes.append(f'{label}: interpreted refs.{slot}="{v}" as the reserved '
                                 f'datum "{datum}" — the engine knows plane.top/plane.front/'
                                 f"plane.right and axis.x/axis.y/axis.z")
                continue
            fixed = _repair_datum_selector(v)
            if fixed is not None:
                out_refs[slot] = fixed
                changed = True
                notes.append(f"{label}: completed the datum selector in refs.{slot} into "
                             f'{{"kind":"datum","feature":"{fixed["feature"]}"}} — a datum '
                             f"selector names its datum under `feature`")
                continue
            if slot in _AXIS_DIRECTION_SLOTS:
                axis_datum = _axis_vector_to_datum(v)
                if axis_datum is not None:
                    out_refs[slot] = axis_datum
                    changed = True
                    notes.append(
                        f"{label}: read refs.{slot} {_compact(v)} as the reserved datum "
                        f'"{axis_datum}" — a direction slot takes a reserved axis token '
                        f"(axis.x/axis.y/axis.z) or a real selector, never a bare vector")
            continue
        # Prose the model uses to NAME geometry ("the vertical edges", "the top face"),
        # and the {"query": …} wrapper it puts around it. Both are dialect for a geometry
        # query the engine already resolves — mapped here, never guessed: a phrase that
        # names no unambiguous filter is left for the engine's teaching error.
        kind = _REF_SLOT_KIND.get(slot)
        if kind in ("face", "edge"):
            members = v if isinstance(v, list) else [v]
            new_members = []
            rewrote = False
            for m in members:
                q = _geom_ref_member(m, kind)
                if q is None:
                    new_members.append(m)
                    continue
                new_members.append(q)
                rewrote = True
                notes.append(
                    f"{label}: read refs.{slot} {_compact(m)} as the geometry query "
                    f"{_compact(q)} — a ref slot takes a selector, a query, a reserved "
                    f'datum, an f<N> id or a "$N" placeholder, never a prose phrase')
            if rewrote:
                v = new_members if isinstance(v, list) else new_members[0]
                out_refs[slot] = v
                changed = True
        # One bare selector/query handed to a LIST slot says exactly one thing. Only a
        # dict that IS a selector/query is wrapped — a dict that merely nests the list one
        # level deeper ({"sections":[…]}) means something else entirely.
        #
        # The wrap is safe because the engine resolves a query in a face list exactly like the
        # bare query: normalize_feature_shape unwraps a one-element query list, and
        # resolve_face_picks routes every query ELEMENT of a longer list (shell remove top +
        # bottom) through the query resolver. Pinned end-to-end by
        # test_cad_tools.py::test_one_element_query_list_still_builds_a_shell and
        # test_modeling_repertoire.py::test_a_shell_opened_top_and_bottom_is_a_tube.
        if (slot in _LIST_REF_SLOTS and isinstance(v, dict)
                and (v.get("kind") or v.get("of") or v.get("sid") or (set(v) & _QUERY_KEYS))):
            notes.append(f"{label}: wrapped refs.{slot} {_compact(v)} in a list — "
                         f"refs.{slot} takes a LIST of selectors/queries, even for one")
            v = [v]
            out_refs[slot] = v
            changed = True
        if kind is None:
            continue
        if isinstance(v, list):
            new = [_repair_selector(x, kind) for x in v]
            if new != v:
                out_refs[slot] = new
                changed = True
                notes.append(f'{label}: completed refs.{slot} into {kind} selectors — every '
                             f'member needs a "kind" (or an "of" geometry query)')
        else:
            new = _repair_selector(v, kind)
            if new is not v and new != v:
                out_refs[slot] = new
                changed = True
                notes.append(f'{label}: read refs.{slot} as a {kind} query — a bare '
                             f'{{"side":…}} is not a topology selector')
    if ftype == "loft":
        # refs.sections is the ONLY profile slot a loft has — so refs.profiles /
        # refs.sketches can only mean it, a {"sections":[…]} wrapper is the list nested
        # one level too deep, and ONE bare selector/string is the list that should have
        # been. Remap/unwrap; anything ambiguous keeps the engine's error.
        src = "sections" if "sections" in out_refs else next(
            (a for a in _LOFT_SECTION_ALIASES if a in out_refs), None)
        if src is not None:
            canon = _loft_section_list(out_refs[src])
            if canon is not None and (src != "sections" or canon != out_refs[src]):
                if src != "sections":
                    out_refs.pop(src)
                out_refs["sections"] = canon
                changed = True
                notes.append(
                    f"{label}: read refs.{src} as refs.sections ({len(canon)} region "
                    f"selector(s)) — a loft's profiles are a LIST of "
                    f'{{"kind":"region","sketch":…}} selectors under refs.sections')
    # Every ref member the engine will resolve as a QUERY, checked before it gets there:
    # picks.cpp ignores a key it does not know, so {"of":"edge","sid":"…"} in refs.edges
    # filleted the entire body instead of one edge, silently. Runs last so the repairs
    # above have already put `of` where it belongs.
    for slot, v in list(out_refs.items()):
        members = v if isinstance(v, list) else [v]
        fixed: list = []
        touched = False
        for m in members:
            if not is_geom_query(m):
                fixed.append(m)
                continue
            q, qnotes = normalize_geom_query(m, where=f"{label}: refs.{slot}",
                                             defer_sid=True)
            notes.extend(qnotes)
            fixed.append(q)
            touched = touched or q != m
        if touched:
            out_refs[slot] = fixed if isinstance(v, list) else fixed[0]
            changed = True
    if not changed:
        return feat
    out = dict(feat)
    out["refs"] = out_refs
    return out


# Keys whose value is a COORDINATE, never a dimension: a {"value":[x,y,z]} wrapper around
# one of these is rejected outright ("params.at.value: missing or non-numeric").
_VECTOR_KEYS = frozenset({
    "at", "at_offset", "axis", "axis_dir", "dir", "direction", "normal", "translate",
    "rotate", "origin", "position", "location", "center", "centre", "point", "up",
})


def _unwrap_vector_dims(feat: dict, ftype: str, notes: list) -> dict:
    """`at:{"value":[0,0,-7.5],"expr":null}` -> `at:[0,0,-7.5]`."""
    out = dict(feat)
    params = dict(out["params"]) if isinstance(out.get("params"), dict) else None
    changed = False
    for holder in (out, params):
        if holder is None:
            continue
        for k in list(holder):
            if k not in _VECTOR_KEYS:
                continue
            v = holder[k]
            if (isinstance(v, dict) and isinstance(v.get("value"), list)
                    and set(v) <= {"value", "expr", "unit"}):
                holder[k] = v["value"]
                changed = True
    if not changed:
        return feat
    if params is not None:
        out["params"] = params
    notes.append(f'{_feat_label(feat, ftype)}: unwrapped a {{"value":[…]}} around a coordinate '
                 f"— at/axis/origin take a plain [x,y,z]; only SIZES take "
                 f'{{"value":N,"expr":…}}')
    return out


# A placement written as a WORD. `at` accepts a [x,y,z] or a geometry query, never a bare
# adjective — "location":"center" reached the engine as at:"center" and died on
# "'center' is neither a reserved datum nor a feature id".
_AT_CENTER_WORDS = frozenset({"center", "centre", "origin", "middle", "centered", "centred",
                              "0,0,0", "world_origin"})
_AT_SIDE_WORDS = frozenset({"top", "bottom", "left", "right", "front", "back",
                            "+x", "-x", "+y", "-y", "+z", "-z"})


def _normalize_placement_word(feat: dict, ftype: str, notes: list) -> dict:
    """`at:"center"` -> [0,0,0]; `at:"top"` -> the top-face geometry query."""
    out, params, peek, _consume = _split_two_levels(feat)
    v = peek("at")
    if not isinstance(v, str) or not v.strip():
        return feat
    word = _slug(v)
    label = _feat_label(feat, ftype)
    if word in _AT_CENTER_WORDS:
        new: Any = [0, 0, 0]
        why = "the world origin (which is also the default)"
    elif word.replace("_", "") in _AT_SIDE_WORDS or word in _AT_SIDE_WORDS:
        side = word if word in _AT_SIDE_WORDS else word.replace("_", "")
        new = {"of": "face", "side": side}
        why = f'the centroid of the {side} face'
    else:
        return feat  # not a word we can honour — let the engine name it
    if "at" in out:
        out["at"] = new
    else:
        params["at"] = new
        out["params"] = params
    notes.append(f'{label}: interpreted at="{v}" as {why} — placement is [x,y,z] or a '
                 f'geometry query like {{"of":"face","side":"top"}}')
    return out


# The extrude family's param NAMES, verified against the keys document.cpp actually reads
# on an extrude feature (depth, depth2, draft, draft_outward, end, end2, end_offset, flip,
# mid_plane, mode, offset, start, start_offset, start_ref, thin, thin_side, thin_thickness,
# up_to). Anything else the model writes for "how far" or "both ways" is inert: the batch
# died on a raw `key 'depth' not found` kernel exception with nothing to learn from.
_EXTRUDE_TYPES = frozenset({"extrude", "surf_extrude"})
_EXTRUDE_DEPTH_ALIASES = ("distance", "length", "extent", "amount", "height", "thickness")
_MID_PLANE_ALIASES = ("symmetric", "symmetrical", "both_directions", "both_sides",
                      "midplane", "centered")
_EXTRUDE_END_WORDS = {"through": "through_all", "thru": "through_all", "all": "through_all",
                      "midplane": "mid_plane", "symmetric": "mid_plane"}


def _normalize_extrude_params(feat: dict, ftype: str, notes: list) -> dict:
    """distance/length -> depth, symmetric/both_directions -> mid_plane, through -> the end."""
    out, params, peek, consume = _split_two_levels(feat)
    label = _feat_label(feat, ftype)
    changed = False

    if peek("depth") is _MISSING:
        for k in _EXTRUDE_DEPTH_ALIASES:
            v = peek(k)
            if v is _MISSING or v is None:
                continue
            if k == "thickness" and peek("thin") not in (_MISSING, None, False):
                continue  # a THIN feature's wall thickness (thin_thickness), not the run
            consume(k)
            params["depth"] = v
            changed = True
            notes.append(f'{label}: interpreted {k}={v!r} as params.depth — an {ftype} is '
                         f"dimensioned by `depth` along its direction")
            break

    for k in _MID_PLANE_ALIASES:
        v = peek(k)
        if v is _MISSING or v is None:
            continue
        consume(k)
        changed = True
        if peek("mid_plane") is _MISSING:
            params["mid_plane"] = bool(v)
            notes.append(f'{label}: interpreted {k}={v!r} as params.mid_plane — the run is '
                         f"split evenly either side of the sketch plane")

    end_v = peek("end")
    if isinstance(end_v, str) and _slug(end_v) in _EXTRUDE_END_WORDS:
        canon = _EXTRUDE_END_WORDS[_slug(end_v)]
        params["end"] = canon
        out.pop("end", None)
        changed = True
        notes.append(f'{label}: interpreted end "{end_v}" as "{canon}" — an {ftype} ends '
                     f'"blind" (depth), "through_all" or "mid_plane"')

    # A negative depth is how a model says "the other way" — overwhelmingly a pocket cut
    # DOWN from the sketch plane (params {"distance": -8, "mode": "cut"}, live copilot run
    # 2026-07-28). The kernel refuses it outright with "extrude depth must be > 0" and never
    # says how to reverse, so the whole batch died. The sign is direction, not magnitude:
    # take the magnitude and flip the run.
    depth_v = peek("depth")
    given = depth_v.get("value") if _is_dim_object(depth_v) else depth_v
    if _is_num(given) and float(given) < 0:
        magnitude = _clean_num(abs(float(given)))
        if _is_dim_object(depth_v):
            params["depth"] = {**depth_v, "value": magnitude}
        else:
            params["depth"] = magnitude
        flipped = not bool(peek("flip") not in (_MISSING, None, False))
        params["flip"] = flipped
        out.pop("depth", None)
        out.pop("flip", None)
        changed = True
        notes.append(f"{label}: read depth={given} as depth={magnitude} with flip="
                     f"{str(flipped).lower()} — the sign of an {ftype} is its DIRECTION; "
                     f"depth is a magnitude and must be > 0")

    if not changed:
        return feat
    out["params"] = params
    return out


# CAD extensions save_part REPLACES rather than stacks onto (see save_part). Derived from
# fs_tools.CAD_EXTS — the one canonical list — minus "step+" itself, which is the target.
def _cad_exts_for_save() -> frozenset:
    from services.mcp.fs_tools import CAD_EXTS  # noqa: PLC0415 — keep the import off module load
    return frozenset(e.lower().lstrip(".") for e in CAD_EXTS) - {"step+"}


_CAD_EXTS_FOR_SAVE = _cad_exts_for_save()


# Sketching on a FACE is core CAD and the engine supports it, but a ref slot only takes a
# reserved datum, an f<N> id, a "$N" placeholder or a selector OBJECT — so the bare stable
# sid describe_part/query_geometry hands the model back ("Ffa9a6e") is rejected outright.
# The sid grammar and the kind its leading letter implies are cad_common's (_SID_RE /
# _sid_match, core/doc/selectors.cpp::stable_sid); wrap it into the selector the slot wants,
# and VERIFY it against the live document first — a stale sid must name what does exist.
_SID_KINDS = {"f": "face", "e": "edge", "v": "vertex"}
_SID_REF_SLOTS = frozenset({
    "plane", "face", "faces", "target_face", "middle_face", "face_set_a", "face_set_b",
    "remove", "parting", "neutral", "reference_face", "edges", "edge", "edge_a", "edge_b",
    "vertices", "surface", "tool", "up_to", "start_ref", "end_ref", "base", "path", "axis",
})


#: Slots the engine reads from ``feature.refs``. A model that writes them under
#: ``params`` is not wrong about the GEOMETRY, only about which bag they live in.
#: Live 2026-07-27 (claude-haiku-4-5): it resolved the four real corner edges and
#: sent
#:     {"type":"fillet","params":{"radius":{...},"edges":[{"sid":"Ed7ec5d"},…]}}
#: three times in a row, each rejected with ".refs.edges: required for fillet" —
#: correct selectors, wrong bag, and nothing in the error said which bag.
_REFS_ONLY_SLOTS = frozenset({
    "edges", "edge", "edge_a", "edge_b", "faces", "face", "target_face",
    "middle_face", "face_set_a", "face_set_b", "vertices", "profile",
    "sections", "remove", "parting", "neutral", "reference_face", "surface",
    "tool", "guides", "up_to", "up_to2", "start_ref", "end_ref", "end2_ref",
    # The second half of the same bag problem, measured live 2026-07-30 by driving each
    # 3D feature with its selectors in `params`. Every one of these reached NO slot at
    # all, and three of them did it SILENTLY — the feature built and reported ok while
    # ignoring what the model asked for:
    #   pull      draft: params.pull:"axis.x" built the DEFAULT axis.z taper (22942.038
    #             mm^3 for all three axes); refs.pull:"axis.x" is correctly rejected.
    #   features  mirror: params.features:["f2"] left the mirror with no seed, so it fell
    #             through to the WHOLE-BODY branch and fused a reflection that filled the
    #             hole the model wanted mirrored — volume went UP, 23748.673 -> 24000.0.
    #   centerline loft: params.centerline was dropped, so the guide had no effect.
    # …and `path`/`plane`/`bodies` were rejected for a slot the model DID supply
    # («refs.path: required for sweep», «refs.plane: required for mirror»).
    "path", "centerline", "pull", "features", "bodies", "plane",
})


#: The word a model reaches for when it names a 3D feature's ONE defining size, mapped to
#: the key the engine actually reads (verified against the literals each compiler branch in
#: core/doc/document.cpp consumes — rib 4424, chamfer 4562, shell 5623, draft 5764, thread
#: 7038, mirror 5049, sweep 4380 — so no alias here shadows a key that already means
#: something else on that type).
#:
#: Measured live 2026-07-30 on a fresh engine, one alias per call. Two of these were the
#: worst possible failure, a build that reports ok and ignores the number it was given:
#:   rib     params.width=8 built the 3 mm DEFAULT rib (7650.0 mm^3); thickness=8 builds
#:           8400.0. The model asked for an 8 mm rib and silently got a 3 mm one.
#:   thread  params.depth=10 threaded the WHOLE 30 mm face (1251.619 mm^3); length=10
#:           gives 1418.832. The model asked for a 10 mm thread run and got a full one.
#: The rest were clean rejections for a value the model DID supply — «params.thickness:
#: required for shell», «params.length: required for chamfer», «params.angle: required for
#: draft», «refs.plane: required for mirror», «refs.path: required for sweep».
_TYPE_PARAM_ALIASES: dict = {
    "shell": {"thickness": ("wall_thickness", "wall", "wall_t", "t")},
    "rib": {"thickness": ("width", "rib_thickness", "wall_thickness", "wall", "t")},
    # chamfer's second leg is `length2`; d1/d2 are how a model spells an ASYMMETRIC one.
    "chamfer": {"length": ("size", "d", "d1", "width", "amount", "leg", "setback"),
                "length2": ("d2",)},
    "draft": {"angle": ("draft_angle", "taper", "taper_angle", "degrees", "deg")},
    "thread": {"length": ("depth", "thread_length", "run", "engagement"),
               "size": ("thread_size", "designation"),
               "major_dia": ("diameter", "major_diameter", "nominal_diameter",
                             "nominal_dia")},
    # These two are SELECTOR slots, not sizes — renamed here so _migrate_ref_slots (next
    # pass) can carry them into `refs`, which is where the engine reads them.
    "mirror": {"plane": ("about", "across", "mirror_plane", "symmetry_plane")},
    "sweep": {"path": ("spine", "trajectory", "rail", "along", "sweep_path")},
    # SHEET METAL. core/doc/document.cpp reads sm_base_flange 5885-5889 (thickness /
    # k_factor / default_bend_radius) and the shared bend branch 5966-6088 (length /
    # angle / radius / k_factor / offset / hem_type); validate.cpp 223-227 requires NO
    # params on any sm_* type, so every one of these is read with contains()/value() and
    # an unrecognised spelling is silently swallowed. Measured live 2026-07-30 on a fresh
    # engine — an 80x40x2 base + two 90 deg 25 mm flanges + sm_flat_pattern, reading the
    # unfolded blank's X extent (analytic target 134.106 mm for r=6, k=0.20, L=25):
    #   length   height / flange_length / wall_length -> 124.106; the flange fell back to
    #            the 20 mm DEFAULT. The model asked for a 25 mm wall and got a 20 mm one,
    #            10 mm short on the blank, reported ok.
    #   radius   bend_radius / inner_radius -> 129.540; the bend used the default radius
    #            (= thickness) instead of the 6 mm the model specified.
    #   k_factor kfactor / k -> 135.677; the bend allowance used k=0.45, not 0.20.
    # Every one of those is a silent wrong blank — the part looks built and the flat
    # pattern is simply the wrong size, which is exactly what gets cut.
    "sm_base_flange": {
        "thickness": ("sheet_thickness", "material_thickness", "gauge", "wall_thickness", "t"),
        "default_bend_radius": ("bend_radius", "radius", "inner_radius", "default_radius",
                                "bend_r"),
        "k_factor": ("kfactor", "k", "k_fac", "bend_k"),
    },
    "sm_edge_flange": {
        "length": ("height", "flange_length", "flange_height", "wall_length", "wall",
                   "leg", "leg_length"),
        "radius": ("bend_radius", "inner_radius", "bend_r"),
        "k_factor": ("kfactor", "k", "k_fac", "bend_k"),
        "angle": ("bend_angle", "fold_angle", "flange_angle"),
    },
    "sm_hem": {
        "length": ("height", "hem_length", "wall_length", "wall", "leg", "leg_length"),
        "radius": ("bend_radius", "inner_radius", "bend_r"),
        "k_factor": ("kfactor", "k", "k_fac", "bend_k"),
        "hem_type": ("hem_style", "style"),
    },
    "sm_jog": {
        "length": ("height", "flange_length", "wall_length", "wall", "leg", "leg_length"),
        "offset": ("jog_offset", "step", "step_offset", "z_offset"),
        "radius": ("bend_radius", "inner_radius", "bend_r"),
        "k_factor": ("kfactor", "k", "k_fac", "bend_k"),
        "angle": ("bend_angle", "fold_angle", "jog_angle"),
    },
}
#: A surface sweep is the same feature without the boolean, so it takes the same dialect.
_TYPE_PARAM_ALIASES["surf_sweep"] = _TYPE_PARAM_ALIASES["sweep"]


def _normalize_type_param_aliases(ops: Any, notes: list) -> Any:
    """Rename a feature's defining param/slot from the word the model used to the one the
    engine reads. Identity for a canonical batch; an explicit canonical key always wins."""
    if not isinstance(ops, list):
        return ops
    out, changed = [], False
    for op in ops:
        feat = op.get("feature") if isinstance(op, dict) else None
        if not isinstance(feat, dict):
            out.append(op)
            continue
        ftype = _slug(feat.get("type"))
        table = _TYPE_PARAM_ALIASES.get(ftype)
        if not table:
            out.append(op)
            continue
        new, params, peek, consume = _split_two_levels(feat)
        hit = False
        for canon, aliases in table.items():
            if peek(canon) is not _MISSING:
                continue
            for alias in aliases:
                v = peek(alias)
                if v is _MISSING or v is None:
                    continue
                consume(alias)
                params[canon] = v
                hit = True
                notes.append(
                    f"{_feat_label(feat, ftype)}: read params.{alias} as params.{canon} — "
                    f"a {ftype} names that {'selection' if canon in _REFS_ONLY_SLOTS else 'value'}"
                    f" `{canon}`, and an unread key is silently ignored, not rejected")
                break
        if not hit:
            out.append(op)
            continue
        new["params"] = params
        out.append({**op, "feature": new})
        changed = True
    return out if changed else ops


#: Structural feature keys — the ONLY ones a feature dict is allowed at its top level per
#: the engine's own schema (type/name/id/suppressed/params/refs/folder). Mirrors
#: core/doc/session.cpp's own hoist set (960-991) exactly.
_FEATURE_STRUCTURAL_KEYS = frozenset(
    {"type", "name", "id", "suppressed", "params", "refs", "folder"})


def _hoist_feature_sibling_keys(ops: Any, notes: Optional[list] = None) -> Any:
    """Move every non-structural TOP-LEVEL feature key into ``params``, mirroring the
    engine's own hoist (core/doc/session.cpp:960-991).

    A model routinely writes a feature's real data BESIDE params/refs instead of inside
    either — live 2026-07-28: ``{"type":"fillet","edges":[...],"params":{"radius":2}}``,
    ``{"type":"linear_pattern","dir1":"x","count1":4,"spacing1":10,"features":["f2"]}``.
    The C++ side already hoists any such sibling into ``params`` so nothing silently drops
    (a stray top-level option used to vanish — a cut cylinder fused, a hole built once
    instead of N times), but the Python normalizers that decide the FINAL shape run
    entirely client-side, before that hoist ever sees the batch. Two of them —
    ``_migrate_ref_slots`` (ref-only slot names -> refs) and ``_param_or_ref``/
    ``_normalize_pattern_feature`` (pattern params/refs) — each independently look in
    ``params``/``refs`` only, so a sibling key reached neither and the feature was rejected
    for a slot the model DID supply, just in the wrong (nonexistent, from the engine's
    normalizer's point of view) bag.

    Doing the hoist ONCE, here, before either of those passes runs, is the root fix: by
    the time they look, the value is already in ``params`` where they already know to look
    — neither needs its own copy of this lookup, so a THIRD slot added later cannot regress
    only one of them.

    Silent, deliberately: session.cpp's own docstring example for this tool already shows
    ``"at":[...],"axis":[...],"mode":"boss"`` living at a feature's top level as CANONICAL,
    documented dialect (session.cpp: "params is merged into the feature ... so a model may
    put a dimension at either level and mean the same thing") — the same reason
    ``_split_two_levels`` never notes a top-level peek either. A downstream pass that finds
    something to actually TEACH about a moved value (``_migrate_ref_slots``'s bag,
    ``_normalize_pattern_feature``'s kind) still adds its own note; this pass only makes
    the value visible to them.
    """
    if not isinstance(ops, list):
        return ops
    for op in ops:
        if not isinstance(op, dict):
            continue
        feat = op.get("feature")
        if not isinstance(feat, dict):
            continue
        siblings = [k for k in feat if k not in _FEATURE_STRUCTURAL_KEYS]
        if not siblings:
            continue
        params = feat.get("params")
        if not isinstance(params, dict):
            params = {}
        moved = []
        for key in siblings:
            if key in params:
                continue  # an explicit params entry always wins, same as the C++ hoist
            params[key] = feat[key]
            moved.append(key)
        if not moved:
            continue
        feat["params"] = params
        for key in moved:
            del feat[key]
    return ops


#: A ref-slot name that is ALSO a legitimate top-level dialect word for a DIFFERENT
#: feature type, where the two meanings disagree about which bag the value belongs in.
#: "tool" is a real ref slot for split/surf_trim (validate.cpp), but boolean's own flat
#: dialect ("tool":"drain_boss") names an OPERAND -> params.b, never a selector —
#: _normalize_boolean_feature (which runs later, off the top level itself via
#: _split_two_levels) already owns that reading, so this migration must not preempt it.
#:
#: `plane` is the sketch's own plane slot, but _normalize_sketch_ops reads it off
#: params/top-level and owns the whole "which datum is this drawn on" reading (including
#: the named-plane dialect "XZ"/"Top Plane"), so migrating it early would strand the
#: sketch on the default plane.top — the exact silent-wrong-datum failure that pass exists
#: to prevent. `features`/`bodies` are boolean's flat OPERAND dialect
#: (_BOOLEAN_OPERAND_KEYS -> params.a/params.b), same reasoning as `tool`.
_REFS_ONLY_SLOTS_TYPE_EXCLUDE = {
    "tool": frozenset({"boolean"}),
    "plane": frozenset({"sketch", "sketch3d"}),
    "features": frozenset({"boolean"}),
    "bodies": frozenset({"boolean"}),
}


def _migrate_ref_slots(ops: Any, notes: Optional[list] = None) -> Any:
    """Move ref-only slots out of ``params`` into ``refs`` — including a whole nested
    ``params.refs`` sub-dict.

    Two distinct mistakes, one bag problem:

    * A ref-only slot written as a PARAM (``params.edges``). Only moved when ``refs``
      does not already define it, so an explicit ref always wins; ``params`` keeps
      everything else untouched.
    * The whole ``refs`` bag written one level too DEEP, inside ``params``. Live
      2026-07-28 a stepped-shaft build sent
      ``{"type":"extrude","params":{"refs":{"profile":[{"kind":"region","sketch":"$0",
      "hint_index":0}]},"depth":{"value":60},"mode":"boss"}}`` and was rejected with
      ``refs.profile: required for extrude`` — the profile WAS supplied, and the
      same-batch ``"$0"`` spelling was exactly right; only the nesting was wrong. The same
      batch also lost a sketch's ``params.refs.plane`` = ``"plane.front"``, which is
      WORSE than a rejection: ``_normalize_sketch_ops`` finds no plane anywhere and
      defaults to ``plane.top``, so the sketch silently lands on the wrong datum.
      Hoisting here makes the reserved datum visible again — every documented spelling
      (``plane.top``/``plane.front``/``plane.right``, ``axis.x``/``axis.y``/``axis.z``,
      core/doc/validate.cpp:31-35 RESERVED_DATUMS) is already accepted as a BARE STRING by
      validate_sel_or_datum (validate.cpp:900-911), so nothing further is needed once the
      value reaches ``feature.refs``.

    The nested bag MERGES: a slot already present at the top level wins (same rule as the
    per-slot migration above). A slot present in BOTH with a DIFFERENT value is a genuine
    contradiction — the model named two different pieces of geometry for one slot — so it
    raises a teaching error instead of picking one, and the whole batch is scanned for that
    before anything is mutated so a rejection leaves ``ops`` exactly as it arrived.
    """
    if not isinstance(ops, list):
        return ops

    def _nested_refs(feat: dict) -> dict:
        params = feat.get("params")
        nested = params.get("refs") if isinstance(params, dict) else None
        return nested if isinstance(nested, dict) and nested else {}

    # Pre-scan: a conflict anywhere rejects the batch with NOTHING mutated.
    for op in ops:
        feat = op.get("feature") if isinstance(op, dict) else None
        if not isinstance(feat, dict):
            continue
        nested = _nested_refs(feat)
        top = feat.get("refs") if isinstance(feat.get("refs"), dict) else {}
        clash = sorted(k for k, v in nested.items() if k in top and top[k] != v)
        if clash:
            label = feat.get("name") or feat.get("type") or "feature"
            details = "; ".join(
                f"refs.{k}={top[k]!r} vs params.refs.{k}={nested[k]!r}" for k in clash)
            raise ValueError(
                f"{label}: params.refs is the refs bag written one level too deep, but "
                f"{'this slot names' if len(clash) == 1 else 'these slots name'} DIFFERENT "
                f"geometry at each level — {details}. A ref slot holds exactly one "
                f"selection, so pick the one you meant and put it in the feature's "
                f"top-level refs (refs is a SIBLING of params, never a param).")

    for op in ops:
        if not isinstance(op, dict):
            continue
        feat = op.get("feature")
        if not isinstance(feat, dict):
            continue
        params = feat.get("params")
        if not isinstance(params, dict):
            continue
        refs = dict(feat.get("refs") or {})
        new_params = dict(params)
        label = feat.get("name") or feat.get("type") or "feature"

        nested = _nested_refs(feat)
        if nested:
            # Never leave a second refs bag in params. An EMPTY or non-dict params.refs is
            # not a bag we understand, so it stays put for the engine to name.
            new_params.pop("refs", None)
        hoisted = []
        for key, value in nested.items():
            if key in refs:
                continue  # identical (a conflict already raised above) — drop the copy
            refs[key] = value
            hoisted.append(key)

        ftype = feat.get("type")
        present = [k for k in list(new_params) if k in _REFS_ONLY_SLOTS
                  and ftype not in _REFS_ONLY_SLOTS_TYPE_EXCLUDE.get(k, ())]
        moved = []
        for key in present:
            value = new_params.pop(key)  # never leave a selector in params
            if isinstance(refs.get(key), (list, dict, str)):
                continue  # an explicit ref wins; the params copy is just dropped
            refs[key] = value
            moved.append(key)

        if not nested and not present:
            continue
        feat["refs"] = refs
        feat["params"] = new_params
        if notes is not None:
            if hoisted:
                notes.append(f"{label}: hoisted params.refs ({', '.join(sorted(hoisted))}) "
                             f"up to the feature's own refs — refs is a SIBLING of params, "
                             f"not a param, so a nested copy reached no slot at all")
            for key in moved:
                notes.append(f"{label}: "
                             f"moved {key!r} from params to refs (the engine reads "
                             f"geometry selectors from refs)")
    return ops


def _unwrap_enum_dims(ops: Any, notes: Optional[list] = None) -> Any:
    """Unwrap text/enum params a model dressed up as a dimension.

    validate.cpp treats ANY params entry shaped ``{value|expr: …}`` as a
    dimension and demands a numeric ``value``. Models habitually wrap an enum
    the same way they wrap a length — live 2026-07-27 an extrude arrived as
    ``params.mode = {"value": "blind"}`` and was rejected with
    "params.mode.value: missing or non-numeric", which reads as a units problem
    for something that was never a number. ``_NON_DIM_KEYS`` already names every
    param whose value is text, so unwrap those back to the bare value.
    """
    if not isinstance(ops, list):
        return ops
    for op in ops:
        if not isinstance(op, dict):
            continue
        feat = op.get("feature")
        if not isinstance(feat, dict):
            continue
        params = feat.get("params")
        if not isinstance(params, dict):
            continue
        for key in list(params):
            if key not in _NON_DIM_KEYS:
                continue
            val = params[key]
            if not isinstance(val, dict) or "value" not in val:
                continue
            inner = val.get("value")
            if isinstance(inner, bool) or not isinstance(inner, (int, float)):
                params[key] = inner
                if notes is not None:
                    notes.append(
                        f"{feat.get('name') or feat.get('type') or 'feature'}: "
                        f"unwrapped params.{key} from a dimension object to "
                        f"{inner!r} ({key} is an enum/text param, not a number)")
    return ops


def _bare_sid(v: Any) -> Optional[str]:
    """The stable sid this ref value IS, or None (a datum / f<N> id / selector is not one)."""
    if not isinstance(v, str):
        return None
    s = v.strip()
    if s in _DATUM_TOKENS or _BATCH_REF_RE.match(s):
        return None
    return s if _SID_RE.fullmatch(s) else None


def _ref_sid(v: Any) -> Optional[str]:
    """The sid a ref member names — bare, or carried inside a selector/query dict.

    A sid IS the identity of one face/edge, so wherever it appears the engine's own live
    match for it is the authoritative selector. Only the bare-string form used to be
    resolved, which is how the fabricated-centroid path opened: the validator rejected
    ``{"kind":"edge","sid":"E6b9abd"}`` for a missing centroid, and a live model
    (claude-sonnet-5, 2026-08-04) answered by inventing ``centroid:[0,0,0]``. That was
    ACCEPTED, and chamfered the rim at z=0 instead of the one at z=100 the sid named.
    """
    if isinstance(v, str):
        return _bare_sid(v)
    if isinstance(v, dict):
        sid = v.get("sid")
        if isinstance(sid, str) and _SID_RE.fullmatch(sid.strip()):
            return sid.strip()
    return None


def _walk_sid_refs(ops: Any):
    """Yield (refs dict, slot, index-or-None, sid) for every sid sitting in a ref slot."""
    for op in ops if isinstance(ops, list) else ():
        feat = op.get("feature") if isinstance(op, dict) else None
        refs = feat.get("refs") if isinstance(feat, dict) else None
        if not isinstance(refs, dict):
            continue
        for slot, v in refs.items():
            if slot not in _SID_REF_SLOTS:
                continue
            if isinstance(v, list):
                for i, x in enumerate(v):
                    sid = _ref_sid(x)
                    if sid:
                        yield refs, slot, i, sid
            else:
                sid = _ref_sid(v)
                if sid:
                    yield refs, slot, None, sid


def _sid_roster(doc_id: str, kind: str, limit: int = 12) -> list:
    """The document's live sids of one kind, for a teaching error."""
    try:
        res = cb.call("doc_query", {"doc_id": doc_id, "query": {"of": kind}}) or {}
    except Exception as exc:  # noqa: BLE001
        if getattr(exc, "code", None) == "engine_connection_lost":
            raise
        return []
    out = []
    for m in res.get("matches") or []:
        sid = m.get("sid")
        if not sid:
            continue
        bits = [b for b in (m.get("side"), m.get("surf")) if b]
        out.append(f"{sid} ({'/'.join(bits)})" if bits else str(sid))
        if len(out) >= limit:
            break
    return out


#: Substrings that mark a kernel error as a genuine RESOLUTION failure — a selector or
#: query that reached the engine but matched nothing/ambiguously — as opposed to a
#: SYNTAX failure (bad shape, unknown slot). The C++ validator already teaches syntax in
#: full (kQueryGrammar, unknown-ref-slot, "must be a non-empty list") and duplicating that
#: here would bloat every error; only these markers mean "the model was flying blind on
#: geometry it could not see" (core/doc/document.cpp / picks.cpp throw sites).
_REF_RESOLUTION_MARKERS = (
    "dangling", "matched no edge", "matched no face", "matched no geometry",
    "no edges matched", "no candidates of matching kind", "selector dangling",
    "query matched no",
)


def _ref_failure_kind(text: str) -> Optional[str]:
    """'edge'/'face' — the entity kind a kernel error's RESOLUTION failure was about.

    None when the text carries no resolution marker at all (a syntax rejection, which the
    C++ validator already teaches) — see _REF_RESOLUTION_MARKERS.
    """
    low = text.lower()
    if not any(marker in low for marker in _REF_RESOLUTION_MARKERS):
        return None
    has_edge, has_face = "edge" in low, "face" in low
    if has_edge and not has_face:
        return "edge"
    if has_face and not has_edge:
        return "face"
    if has_edge:  # both mentioned — fillet/chamfer (edge ops) are the common case
        return "edge"
    return None


def _declined_geom_phrases(ops: Any) -> list:
    """[(kind, slot, phrase), …] for every ref member that is STILL raw face/edge prose
    at apply time.

    By the time `ops` reaches here every normalizer pass (_geom_ref_member and friends)
    has already had its shot at rewriting prose into a query; a string that still names a
    face/edge noun ("horizontal edges") is one the normalizer looked at and deliberately
    declined to guess at (genuinely ambiguous, or names no filter the engine has) — never
    one it silently skipped, since anything unambiguous is rewritten in place.
    """
    out = []
    for op in ops if isinstance(ops, list) else ():
        feat = op.get("feature") if isinstance(op, dict) else None
        refs = feat.get("refs") if isinstance(feat, dict) else None
        if not isinstance(refs, dict):
            continue
        for slot, v in refs.items():
            kind = _REF_SLOT_KIND.get(slot)
            if kind not in ("face", "edge"):
                continue
            nouns = _FACE_NOUNS if kind == "face" else _EDGE_NOUNS
            for m in (v if isinstance(v, list) else [v]):
                if not isinstance(m, str):
                    continue
                if any(w in nouns for w in re.findall(r"[a-z]+", m.lower())):
                    out.append((kind, slot, m))
    return out


#: Exact labels _classify_edge_match emits for an axis-aligned bucket -> the `parallel`
#: letter query_geometry accepts. Kept as an explicit map (not string-parsed back out of
#: the label) so the label wording can change freely without silently breaking the
#: suggested-query text.
_AXIS_LABEL_LETTER = {
    "parallel to z (vertical)": "z",
    "parallel to x": "x",
    "parallel to y": "y",
}


def _classify_edge_match(m: dict) -> str:
    """One doc_query edge match -> a short orientation/surface bucket label."""
    surf = m.get("surf")
    axis = m.get("axis")
    if surf == "line" and isinstance(axis, list) and len(axis) == 3:
        ax, ay, az = (abs(c) for c in axis)
        if az > 0.999:
            return "parallel to z (vertical)"
        if ax > 0.999:
            return "parallel to x"
        if ay > 0.999:
            return "parallel to y"
        return "diagonal (non-axis-aligned)"
    return f"{surf} edges" if surf else "other edges"


def _classify_face_match(m: dict) -> str:
    """One doc_query face match -> a short side/surface bucket label."""
    side, surf = m.get("side"), m.get("surf")
    if side:
        return f"{side} ({surf})" if surf and surf != "plane" else side
    return f"{surf} faces" if surf else "other faces"


#: How many distinct orientation/side buckets a geometry-inventory hint enumerates before
#: rolling the rest into a single "N other" bucket — a lattice part can carry hundreds of
#: edges, and a full dump would be worse than no hint at all.
_INVENTORY_BUCKET_CAP = 6


def _geometry_inventory_line(doc_id: str, kind: str) -> Optional[str]:
    """A short, REAL, capped inventory of the doc's edges/faces for a ref-resolution error.

    Built from a live doc_query against the CURRENT document — never a guess or a
    hardcoded list. Bucketed by orientation (edges) or side (faces) so a model can act on
    it directly, e.g. "this body has 12 edges — 4 parallel to z (vertical), 4 parallel to
    x, 4 parallel to y".
    """
    try:
        res = cb.call("doc_query", {"doc_id": doc_id, "query": {"of": kind}}) or {}
    except Exception:  # noqa: BLE001
        return None
    matches = [m for m in (res.get("matches") or []) if isinstance(m, dict)]
    total = len(matches)
    if total == 0:
        return f"this body has no {kind}s at all — check the body/feature you targeted"
    classify = _classify_edge_match if kind == "edge" else _classify_face_match
    counts: dict[str, int] = {}
    for m in matches:
        label = classify(m)
        counts[label] = counts.get(label, 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    shown = ranked[:_INVENTORY_BUCKET_CAP]
    rest = sum(c for _, c in ranked[_INVENTORY_BUCKET_CAP:])
    parts = [f"{c} {label}" for label, c in shown]
    if rest:
        parts.append(f"{rest} other {kind}(s)")
    example = next((label for label, _ in shown if label in _AXIS_LABEL_LETTER), None)
    suggestion = ""
    if example:
        axis = _AXIS_LABEL_LETTER[example]
        suggestion = (f' — call query_geometry({{"of":"{kind}","parallel":"{axis}"}}) '
                      f"to select those")
    return f"this body has {total} {kind}s — {', '.join(parts)}{suggestion}"


def _enrich_ref_resolution_error(result: Any, doc_id: Optional[str], ops: Any) -> Any:
    """Append a real geometry inventory to a ref-RESOLUTION failure (dict form).

    Mirrors op_vocab.enrich_unknown_op_error's shape (mega-8 F7): detect a thin kernel
    error, and only when it names a genuine resolution failure (never a syntax one — the
    C++ validator already teaches that), attach what the document ACTUALLY has so the
    model can act instead of guessing again. Live root cause: a fillet on "the vertical
    edges" was rejected four times and the model never once could see what edges the part
    had (2026-07-28).
    """
    if not isinstance(result, dict) or result.get("ok") is not False or not doc_id:
        return result
    if "geometry_hint" in result:
        return result
    text = str(result.get("error") or result.get("message") or "")
    if not text:
        return result
    kind = _ref_failure_kind(text)
    if kind is None:
        # Kernel text alone doesn't say "resolution" (e.g. the syntax path this batch
        # actually hit) — but a phrase our own normalizer declined to guess at, sitting in
        # exactly the ref slot this error names, is just as much "flying blind on
        # geometry" and deserves the same real inventory.
        for phrase_kind, slot, _phrase in _declined_geom_phrases(ops):
            if f".refs.{slot}" in text:
                kind = phrase_kind
                break
    if kind is None:
        return result
    line = _geometry_inventory_line(doc_id, kind)
    if not line:
        return result
    out = dict(result)
    out["geometry_hint"] = line
    out["error"] = f"{text.rstrip()} — {line}"
    return out


def _resolve_sid_refs(ops: Any, doc_id: str, notes: list) -> Any:
    """Wrap a bare stable sid in a ref slot into the selector the engine accepts.

    Costs one doc_query per sid KIND, and only when a bare sid is actually present — a
    canonical batch never touches the engine here. A sid the document does not carry is a
    teaching error naming the live roster, never a blind wrap (sids change on every edit).
    """
    found = list(_walk_sid_refs(ops))
    if not found:
        return ops
    resolved: dict = {}
    for _refs, slot, _i, sid in found:
        if sid in resolved:
            continue
        m = _sid_match(doc_id, sid)
        if m is None:
            kind = _SID_KINDS.get(sid[:1].lower(), "face")
            roster = _sid_roster(doc_id, "face" if kind == "vertex" else kind)
            extra = (" Vertex sids are not selectable — name the face or edge that carries it."
                     if kind == "vertex" else "")
            raise ValueError(
                f'refs.{slot}: no {kind} with sid "{sid}" in this document (sids change '
                f"whenever the model changes).{extra} Live {kind} sids: "
                + (", ".join(roster) if roster else "none — the part has no bodies yet")
                + ". Call describe_part(doc_id) for the current roster, or use a geometry "
                  'query like {"of":"face","side":"top"}.')
        # The LIVE doc_query match IS the canonical selector: validate_selector wants a
        # centroid on a face/edge/vertex, and the resolver already carries it (plus surf /
        # axis / owner_feature, which is what makes the pick survive a rebuild).
        sel = {"kind": m.get("kind") or _SID_KINDS.get(sid[:1].lower(), "face"),
               "sid": m.get("sid") or sid}
        for k in ("centroid", "surf", "axis", "owner_feature", "radius"):
            if m.get(k) is not None:
                sel[k] = m[k]
        resolved[sid] = sel
    out = []
    for op in ops:
        feat = op.get("feature") if isinstance(op, dict) else None
        refs = feat.get("refs") if isinstance(feat, dict) else None
        if not isinstance(refs, dict):
            out.append(op)
            continue
        new_refs = dict(refs)
        touched = False
        for slot, v in refs.items():
            if slot not in _SID_REF_SLOTS:
                continue
            if isinstance(v, list):
                lst = [resolved.get(_ref_sid(x) or "", x) for x in v]
                if lst != v:
                    new_refs[slot], touched = lst, True
            else:
                sid = _ref_sid(v)
                if sid and resolved[sid] != v:
                    new_refs[slot], touched = resolved[sid], True
        if not touched:
            out.append(op)
            continue
        out.append({**op, "feature": {**feat, "refs": new_refs}})
    if resolved:
        shown = ", ".join(sorted(resolved))
        notes.append(
            f"resolved face/edge sid(s) {shown} against this document — the sid IS the "
            f"element's identity, so the engine's own measured selector (kind + centroid "
            f"+ surf) replaces whatever accompanied it. A centroid guessed to satisfy "
            f'"incomplete engine topology selector" would have picked a DIFFERENT element')
    return out


# A cylinder with no height cannot build. When the call also reads as a BORE, the model
# reached for the wrong feature type: a bore in existing material is a `hole`, which is
# dimensioned by diameter and drilled through a named face. Say that instead of letting the
# schema report a missing param — but never convert it silently: cylinder-vs-hole is a real
# modelling choice (a standalone post IS a cylinder).
_BORE_INTENT_WORDS = ("bore", "hole", "drill", "counterbore", "countersink", "pocket",
                      "clearance", "thru", "through")


# A cylinder's axial length, spelled the other ways a model states it. Every one of
# these is unambiguous for a cylinder specifically: there is exactly one linear
# dimension besides the radius. Live 2026-07-30 (run-21784882079258, a car): four
# wheels arrived as {"radius":325,"depth":220,...} and the whole batch was rejected
# with "params.height is required" — the spec was COMPLETE, just not in the spelling
# the schema wanted. `depth` in particular is the canonical key for the extrude this
# same model had just written, so the inconsistency is ours, not the model's.
_CYL_HEIGHT_ALIASES = ("depth", "length", "thickness", "extent", "h")


def _check_cylinder_height(feat: dict, ftype: str, notes: list) -> dict:
    if ftype != "cylinder":
        return feat
    out, params, peek, consume = _split_two_levels(feat)
    if peek("height") is not _MISSING:
        return feat
    for alias in _CYL_HEIGHT_ALIASES:
        v = peek(alias)
        if v is _MISSING or v is None:
            continue
        consume(alias)
        params["height"] = v
        out["params"] = params
        notes.append(
            f'{_feat_label(feat, ftype)}: read params.{alias} as the cylinder\'s '
            f"height — a cylinder is radius + height, and {alias} is its only other "
            f"linear dimension")
        return out
    label = _feat_label(feat, ftype)
    boreish = (str(peek("mode") or "").strip().lower() == "cut"
               or any(w in label.lower() for w in _BORE_INTENT_WORDS))
    msg = (f'cylinder "{label}": params.height is required (a cylinder is radius + height, '
           f"both in mm).")
    if boreish:
        msg += (' This reads like a BORE, and a bore in existing material is a `hole`, not a '
                'cylinder: {"op":"add_feature","feature":{"type":"hole","name":"' + label +
                '","params":{"diameter":20.042,"style":"simple","end":"through_all"},'
                '"refs":{"face":{"of":"face","side":"top"}}}} — a hole takes the DIAMETER '
                'directly and is drilled through a named face. Use a cylinder with '
                'mode:"cut" only when you want a free-standing tool solid.')
    raise ValueError(msg)


def _supply_missing_refs(ops: Any, notes: list) -> Any:
    """Supply a REF the engine cannot infer and the model did not name.

    Deliberately separate from the dialect passes above: those rewrite what the model
    wrote, this one adds something it never said. Today that is a hole's entry face — a
    solid has six of them, the compiler refuses to pick ("hole: 'face' selector is
    required (the planar face to drill into)"), and the top face is what a person means by
    an undecorated bore. Echoed, so the model can name another face next turn.
    """
    if not isinstance(ops, list):
        return ops
    out = []
    for op in ops:
        feat = op.get("feature") if isinstance(op, dict) else None
        if not (isinstance(feat, dict) and op.get("op") == "add_feature"
                and str(feat.get("type") or "").strip().lower() == "hole"):
            out.append(op)
            continue
        refs = feat.get("refs") if isinstance(feat.get("refs"), dict) else {}
        if refs.get("face") or refs.get("positions_sketch"):
            out.append(op)
            continue
        out.append({**op, "feature": {**feat,
                                      "refs": {**refs,
                                               "face": {"of": "face", "side": "top"}}}})
        notes.append(
            f'{_feat_label(feat, "hole")}: no refs.face given — drilling the TOP face '
            f'(refs.face={{"of":"face","side":"top"}}); a bore has to be entered through a '
            f"named planar face, so name another one to drill elsewhere")
    return out


def _normalize_feature_schema(feat: dict, ftype: str, notes: list) -> dict:
    """Every "the engine requires this and the model forgot" rewrite for one feature."""
    out = _unwrap_vector_dims(feat, ftype, notes)
    out = _normalize_placement_word(out, ftype, notes)
    out = _normalize_feature_refs(out, ftype, notes)
    if ftype in _PROFILE_MODE_TYPES or ftype in _AXIS_SOURCE_TYPES:
        out = _normalize_profile_feature(out, ftype, notes)
    if ftype in _EXTRUDE_TYPES:
        out = _normalize_extrude_params(out, ftype, notes)
    elif ftype == "hole":
        out = _normalize_hole_feature(out, notes)
    elif ftype == "bolt_circle":
        out = _normalize_bolt_circle_feature(out, notes)
    elif ftype == "boolean":
        out = _normalize_boolean_feature(out, notes)
    elif ftype == "cylinder":
        out = _check_cylinder_height(out, ftype, notes)
    return out


#: "the sketch I just made". A live run (2026-07-28) built five stacked hexagon levels as
#: sketch/extrude pairs, every extrude carrying params.sketch:"last", and the whole batch
#: was rejected — «profile 'last' -> 'last' not found» ×4 — because a profile ref must name
#: an f<N> id or a "$N" batch position. The word is unambiguous when this batch created a
#: sketch before that feature, so resolve it there and nowhere else.
_RELATIVE_PROFILE_WORDS = frozenset({
    "last", "latest", "previous", "prev", "prior", "above", "preceding", "current",
    "this", "it", "last_sketch", "previous_sketch", "prev_sketch", "current_sketch",
    "the_last_sketch", "last_one", "recent", "most_recent"})

#: Where a profile lives. Deliberately NOT every slot: "last" in refs.face/refs.edges is
#: not this sketch, and guessing there would silently pick the wrong geometry.
_PROFILE_SLOTS = ("profile", "profiles", "sections", "section", "sketch")


def _sub_relative_profile(v: Any, target: str) -> Any:
    """Swap a relative profile word for `target`, through a list or a region selector."""
    if isinstance(v, str):
        return target if _slug(v) in _RELATIVE_PROFILE_WORDS else v
    if isinstance(v, list):
        return [_sub_relative_profile(x, target) for x in v]
    if isinstance(v, dict) and isinstance(v.get("sketch"), str):
        sk = _sub_relative_profile(v["sketch"], target)
        return v if sk == v["sketch"] else {**v, "sketch": sk}
    return v


def _resolve_relative_profile_refs(ops: Any, notes: list) -> Any:
    """`params.sketch:"last"` -> the "$N" of the nearest sketch EARLIER in this batch."""
    if not isinstance(ops, list):
        return ops
    out, last_sketch, hits = [], None, []
    for i, op in enumerate(ops):
        feat = op.get("feature") if isinstance(op, dict) else None
        if not (isinstance(feat, dict) and op.get("op") == "add_feature"):
            out.append(op)
            continue
        if last_sketch is not None:
            nf = dict(feat)
            for holder in ("refs", "params"):
                src = nf.get(holder)
                if not isinstance(src, dict):
                    continue
                new = {k: (_sub_relative_profile(v, last_sketch) if k in _PROFILE_SLOTS else v)
                       for k, v in src.items()}
                if new != src:
                    nf[holder] = new
            if nf != feat:
                hits.append(feat.get("name") or f"op {i}")
                op = {**op, "feature": nf}
        if _slug(feat.get("type")) == "sketch":
            last_sketch = f"${i}"
        out.append(op)
    if not hits:
        return ops  # identity — nothing to renumber, nothing to say
    notes.append(
        "resolved the relative profile word on " + ", ".join(str(h) for h in hits)
        + ' to the sketch created earlier in this batch — a profile names an f<N> id '
          'or a "$N" position, never "last"/"previous"')
    return out


#: Where a feature reference can hide in ``params``. ``params`` holds plenty of strings that
#: are NOT references (mode:"cut", side:"top"), so there the sweep needs an allowlist. Inside
#: ``refs`` the opposite is true: EVERY string slot is a reference by definition, so a key
#: allowlist there can only ever miss one. It did: refs.profile held a bare "HexPattern"
#: (live 2026-07-28 kimi-keychain run) — `profile` was absent from the allowlist and the
#: value was a plain string, not a list, so the batch reached the engine with a display name
#: in a selector slot and died on the dangling-ref guardrail.
_NAME_REF_PARAM_KEYS = ("a", "b", "sketch", "profile", "targets", "bodies", "features")


def _sub_feature_names(v: Any, resolve, all_strings: bool = False) -> Any:
    """Walk one refs/params value, handing every string that could NAME a feature to
    ``resolve`` and swapping in whatever it hands back (None = leave the string alone).

    Shared by the same-batch and the already-in-the-document resolvers so both see exactly
    the same slots — a name reachable by one and not the other is the bug this shape avoids.
    """
    if isinstance(v, str):
        s = v.strip()
        if s and not _is_canon_datum(s):
            tgt = resolve(s)
            if tgt:
                return tgt
        return v
    if isinstance(v, list):
        return [_sub_feature_names(x, resolve, all_strings) for x in v]
    if isinstance(v, dict):
        # A geometry QUERY ({"of":"face","side":"top"}) is data, not a ref: its values
        # are enum words that could collide with a feature named "top" or "face".
        if "of" in v:
            return v
        ref_keys = ("feature", "owner_feature", "sketch", "a", "b", "id")
        return {k: (_sub_feature_names(x, resolve, all_strings)
                    if all_strings or k in ref_keys or isinstance(x, (list, dict))
                    else x)
                for k, x in v.items()}
    return v


def _rewrite_feature_name_refs(feat: dict, resolve) -> tuple:
    """(new refs, new params) for one feature with `resolve` applied to every name slot."""
    refs, params = feat.get("refs"), feat.get("params")
    new_refs = (_sub_feature_names(refs, resolve, all_strings=True)
                if isinstance(refs, dict) else refs)
    new_params = params
    if isinstance(params, dict):
        new_params = {k: (_sub_feature_names(v, resolve) if k in _NAME_REF_PARAM_KEYS else v)
                      for k, v in params.items()}
    return new_refs, new_params


def _apply_name_table(ops: list, table_for, hits: list) -> list:
    """Run ``table_for(index) -> {name: target}`` over the batch, rewriting name refs."""
    out = []
    for i, op in enumerate(ops):
        if not (isinstance(op, dict) and op.get("op") == "add_feature"
                and isinstance(op.get("feature"), dict)):
            out.append(op)
            continue
        feat = op["feature"]
        table = table_for(i)

        def resolve(name: str, _t=table) -> Optional[str]:
            tgt = _t.get(name)
            if tgt:
                hits.append(name)
            return tgt

        refs, params = feat.get("refs"), feat.get("params")
        new_refs, new_params = ((refs, params) if not table
                                else _rewrite_feature_name_refs(feat, resolve))
        if new_refs != refs or new_params != params:
            nf = dict(feat)
            if new_refs != refs:
                nf["refs"] = new_refs
            if new_params != params:
                nf["params"] = new_params
            op = {**op, "feature": nf}
        out.append(op)
    return out


def _batch_feature_names(ops: list) -> dict:
    """{display name: "$<index>"} for every feature this batch creates."""
    index: dict[str, str] = {}
    for i, op in enumerate(ops):
        if isinstance(op, dict) and op.get("op") == "add_feature":
            nm = (op.get("feature") or {}).get("name")
            if isinstance(nm, str) and nm.strip():
                index.setdefault(nm.strip(), f"${i}")
    return index


def _resolve_batch_name_refs(ops: Any, notes: list) -> Any:
    """Rewrite a reference to a feature this SAME batch creates, by NAME, into its "$N".

    Models name their features and then reference the name ("targets":["Barrel"]); the engine
    only knows f<N> ids and same-batch "$<index>" placeholders, so the batch died on the
    dangling-ref guardrail. Pure: only names created earlier in this very list are rewritten.
    """
    if not isinstance(ops, list):
        return ops
    index = _batch_feature_names(ops)
    if not index:
        return ops
    hits: list = []
    # A feature never references ITSELF, and its own `name` is not a ref.
    out = _apply_name_table(
        ops, lambda i: {k: v for k, v in index.items() if v != f"${i}"}, hits)
    if hits:
        notes.append("resolved same-batch feature name(s) " + ", ".join(sorted(set(hits)))
                     + ' to their "$<index>" placeholders — a ref names an f<N> id or a '
                       '"$N" position in THIS batch, never a display name')
    return out

def _validate_batch_placeholders(ops: Any) -> None:
    """Reject an out-of-range, self- or forward-referential "$N" with the real roster.

    "$N" is a ZERO-BASED index into THIS batch's op list, but 1-based is the natural
    reading and nothing corrected it. Live run 2026-07-30 (a Lexus-F keychain): the model
    wrote profile:"$2" and profile:"$4" meaning the 2nd and 4th ops, and the whole batch
    died on «f5 (Keyring hole): profile '$2' -> '$2' not found; f7 (F emboss): profile
    '$4' -> '$4' not found» — a message that names no range, no base, and does not
    mention that $2 WAS the very feature doing the referencing. Two features and a turn
    lost to an off-by-one nobody told the model about.

    Runs AFTER every insert/expand/renumber pass, so the indices it checks are the ones
    the engine will see.
    """
    if not isinstance(ops, list) or not ops:
        return

    def _name(i: int) -> str:
        el = ops[i] if isinstance(ops[i], dict) else {}
        feat = el.get("feature") if isinstance(el.get("feature"), dict) else {}
        return str(feat.get("name") or el.get("name") or "")

    def _kind(i: int) -> str:
        el = ops[i] if isinstance(ops[i], dict) else {}
        feat = el.get("feature") if isinstance(el.get("feature"), dict) else {}
        return str(feat.get("type") or el.get("op") or "op")

    def _slot(i: int) -> str:
        return f"${i}={_name(i)} ({_kind(i)})" if _name(i) else f"${i}=({_kind(i)})"

    last = len(ops) - 1
    roster = ", ".join(_slot(i) for i in range(len(ops)))

    def _walk(v: Any, i: int) -> None:
        if isinstance(v, str):
            s = v.strip()
            if not _BATCH_REF_RE.match(s):
                return
            k = int(s[1:])
            # Order matters: an out-of-range index is ALSO > i, so it must be tested
            # first or its (more useful) message is unreachable.
            if k > last:
                why = (f"is out of range — this batch has {len(ops)} op(s), so its "
                       f'placeholders are "$0".."${last}"')
            elif k == i:
                why = ("refers to ITSELF — a feature cannot be its own input"
                       + (f'; counting from 0 the op before it is "${i - 1}"' if i else ""))
            elif k > i:
                why = (f"names op {k}, which this batch creates LATER — a ref can only "
                       f"name a feature built EARLIER in the same batch")
            else:
                return
            who = _name(i) or f"op {i}"
            raise ValueError(
                f'{who}: "{s}" {why}. "$N" is a ZERO-BASED index into THIS batch: '
                f"{roster}. To name a feature an EARLIER CALL built, use its f<N> id.")
        if isinstance(v, list):
            for x in v:
                _walk(x, i)
        elif isinstance(v, dict):
            for x in v.values():
                _walk(x, i)

    for i, el in enumerate(ops):
        if not isinstance(el, dict):
            continue
        # Only REF slots carry placeholders; a `name` never does.
        feat = el.get("feature") if isinstance(el.get("feature"), dict) else {}
        for holder in (el.get("refs"), feat.get("refs"), feat.get("params"), el.get("params")):
            _walk(holder, i)


def _resolve_doc_name_refs(ops: Any, doc_id: str, notes: list) -> Any:
    """Rewrite a reference to a feature an EARLIER CALL built, by NAME, into its f<N> id.

    The same-batch pass above only knows the names in the list it was handed, so the moment
    a model spreads its work across calls — the normal shape of an agent turn: sketch now,
    extrude next; boss now, mirror next — every reference by name died. Measured live
    2026-07-30 against a fresh engine, one call after the other:

      * a sketch named "Prof" extruded next call -> «profile 'Prof' -> 'Prof' not found»
      * refs.features:["pin"] on a mirror/pattern -> «refs.features[0]: not a feature id»
      * params.a:"blk" on a boolean                -> «ref 'a' -> 'blk' not found»

    The document knows every one of those names. Costs one doc_tree call, and only when an
    unresolved name is actually present — a canonical batch never touches the engine here.

    A name the document carries TWICE is ambiguous, so it is left for the engine's error
    rather than guessed at; a name THIS batch also creates belongs to the same-batch pass,
    which already ran, so it is never re-pointed at an older namesake.
    """
    if not isinstance(ops, list) or not ops:
        return ops
    candidates: set = set()

    def _collect(name: str) -> None:
        candidates.add(name)
        return None

    for op in ops:
        feat = op.get("feature") if isinstance(op, dict) else None
        if isinstance(op, dict) and op.get("op") == "add_feature" and isinstance(feat, dict):
            _rewrite_feature_name_refs(feat, _collect)
    # "$N" is this batch's own placeholder and a bare sid names topology, not a feature —
    # neither is a display name, and neither can appear in the tree's name column.
    candidates = {s for s in candidates
                  if not _BATCH_REF_RE.match(s) and not _bare_sid(s)}
    candidates -= set(_batch_feature_names(ops))
    if not candidates:
        return ops
    try:
        tree = (cb.call("doc_tree", {"doc_id": doc_id}) or {}).get("tree") or {}
    except Exception as exc:  # noqa: BLE001
        if getattr(exc, "code", None) == "engine_connection_lost":
            raise
        return ops
    seen: dict = {}
    for f in tree.get("features") or []:
        if not isinstance(f, dict):
            continue
        nm, fid = f.get("name"), f.get("id")
        if isinstance(nm, str) and nm.strip() and isinstance(fid, str):
            seen[nm.strip()] = None if nm.strip() in seen else fid  # None = ambiguous
    table = {n: fid for n, fid in seen.items() if fid and n in candidates}
    if not table:
        return ops
    hits: list = []
    out = _apply_name_table(ops, lambda _i: table, hits)
    if hits:
        notes.append(
            "resolved feature name(s) " + ", ".join(
                f"{n} -> {table[n]}" for n in sorted(set(hits)))
            + " against the document — a ref names the f<N> id an EARLIER call created "
              '(or a "$N" position in THIS batch), never a display name; describe_part '
              "lists the ids")
    return out


# ---------------------------------------------------------------------------
# The sketch-space coordinate that measures a profile's distance from each revolve
# axis, per sketch plane (plane bases: top u=+X v=+Y, front u=+X v=+Z, right u=+Y
# v=+Z — core/geom/plane.h). 0 = the u ("x") coordinate, 1 = the v ("y") one. A
# plane that does not CONTAIN the axis has no single radial coordinate -> absent.
_RADIAL_COORD = {("plane.top", "axis.x"): 1, ("plane.top", "axis.y"): 0,
                 ("plane.front", "axis.x"): 1, ("plane.front", "axis.z"): 0,
                 ("plane.right", "axis.y"): 1, ("plane.right", "axis.z"): 0}


def _datum_token(v: Any) -> Optional[str]:
    """A ref-slot datum value (bare word or {"kind":"datum","feature":…}) -> token."""
    if isinstance(v, dict):
        v = v.get("feature")
    tok = _norm_datum(v)
    return tok if tok in _DATUM_TOKENS else None


def _sketch_radial_extent(sketch_feat: dict, plane: str, axis: str):
    """(min, max) of the profile's radial coordinate, or None when not cheaply known.

    Only reads points already written in THIS batch's sketch entities (line/arc
    p1/p2, circle/arc centre +/- radius) — never invents or repairs geometry.
    """
    idx = _RADIAL_COORD.get((plane, axis))
    if idx is None:
        return None
    ents = ((sketch_feat.get("params") or {}).get("entities")
            if isinstance(sketch_feat.get("params"), dict) else None)
    if not isinstance(ents, list):
        return None
    vals: list = []
    for e in ents:
        if not isinstance(e, dict):
            continue
        for pk in ("p1", "p2", "center"):
            p = e.get(pk)
            if (isinstance(p, list) and len(p) >= 2
                    and all(isinstance(c, (int, float)) and not isinstance(c, bool)
                            for c in p[:2])):
                vals.append(float(p[idx]))
                r = e.get("radius") if pk == "center" else None
                if isinstance(r, (int, float)) and not isinstance(r, bool) and r > 0:
                    vals.extend((float(p[idx]) - r, float(p[idx]) + r))
    return (min(vals), max(vals)) if vals else None


def _enrich_zero_volume_errors(fs: list, ops: list, idmap: dict) -> None:
    """Make a revolve zero-volume failure ACTIONABLE, in place on feature_status.

    The engine guard ("revolve produced a zero-volume solid") says WHAT is wrong but
    not WHERE the profile sits, and the model resent the identical batch (live k3
    run, 2026-07-27). This is a modelling mistake, never a dialect — no geometry is
    repaired; when the profile sketch is in the SAME batch its radial extent is
    cheap, so the message names the exact offset that clears the axis.
    """
    idx_of = {fid: int(k[1:]) for k, fid in idmap.items()
              if isinstance(k, str) and k.startswith("$")
              and k[1:].isdigit() and isinstance(fid, str)}
    for s in fs:
        err = str(s.get("error") or "")
        if s.get("ok") or "zero-volume" not in err or "revolve" not in err:
            continue
        i = idx_of.get(s.get("id"))
        feat = ops[i].get("feature") if i is not None and i < len(ops) else None
        if not isinstance(feat, dict) or feat.get("type") != "revolve":
            continue
        refs = feat.get("refs") if isinstance(feat.get("refs"), dict) else {}
        axis = _datum_token(refs.get("axis")) or "axis.y"
        prof = refs.get("profile")
        sel = prof[0] if isinstance(prof, list) and prof else prof
        sk_name = (sel.get("sketch") if isinstance(sel, dict)
                   else sel if isinstance(sel, str) else None)
        sketch = None
        if isinstance(sk_name, str) and sk_name.startswith("$") and sk_name[1:].isdigit():
            j = int(sk_name[1:])
            f = ops[j].get("feature") if j < len(ops) else None
            sketch = f if isinstance(f, dict) and f.get("type") == "sketch" else None
        elif sk_name is not None:
            sketch = next((o["feature"] for o in ops
                           if isinstance(o, dict)
                           and isinstance(o.get("feature"), dict)
                           and o["feature"].get("type") == "sketch"
                           and o["feature"].get("name") == sk_name), None)
        extent = None
        plane = "plane.top"
        if sketch is not None:
            srefs = sketch.get("refs") if isinstance(sketch.get("refs"), dict) else {}
            plane = (_datum_token(srefs.get("plane"))
                     or _datum_token((sketch.get("params") or {}).get("plane"))
                     or "plane.top")
            extent = _sketch_radial_extent(sketch, plane, axis)
        if extent is None:
            s["error"] = err + (" | fix: move the profile so it sits strictly to ONE "
                                "side of the axis — no point on or across it")
            continue
        lo, hi = extent
        coord = "xy"[_RADIAL_COORD[(plane, axis)]]
        if lo > 0:
            s["error"] = err + (f" | the profile already clears the axis "
                                f"({coord} in [{lo:g}, {hi:g}] mm on {plane}) — the "
                                f"cause is the angle; use an angle well above 0")
        else:
            shift = 1.0 - lo
            s["error"] = err + (
                f" | fix: on {plane} the profile spans {coord} in [{lo:g}, {hi:g}] mm "
                f"and the axis ({axis}) lies along the sketch {coord}=0 line — offset "
                f"the profile so its minimum {coord} is > 0 (e.g. add {shift:g} mm to "
                f"every point's {coord}, giving [{lo + shift:g}, {hi + shift:g}])")


# add_features ARGUMENT-SHAPE normalizer (model-robustness).
#
# `ops=[{"op":"add_feature","feature":{…}}]` is the documented call and is passed through
# byte-identical. Everything ELSE a model actually writes is rewritten into it here rather
# than rejected, because a teaching error still burns a whole turn:
#   ops={…} / ops="[…]"          one op, or the array as a JSON string
#   feature= features= op=       the batch under the name the model reached for
#   operations= actions= ops_json=
#   type=… name=… diameter=…     ONE feature spread over flat top-level kwargs
#   name= identifier= part=      the DOCUMENT, when no feature type is in sight
# add_features takes a var-keyword parameter for exactly this reason: agent_turn's
# _reconcile_kwargs hands a var-kw tool its arguments UNTOUCHED instead of rejecting the
# unknown names, so the tool owns its own input dialect. Every rewrite is echoed in
# `interpreted`; genuinely ambiguous input raises a teaching error that names the accepted
# forms, and nothing is ever silently dropped.
_BATCH_ALIASES = ("features", "feature", "operations", "actions", "ops_json", "op")
_DOC_HANDLE_ALIASES = ("identifier", "part_name", "part", "document_id", "document", "doc",
                       "part_id", "docid", "target_doc", "name")
_PARAMS_ALIASES = ("params", "parameters", "param", "properties", "props", "attributes")
_FEATURE_NAME_ALIASES = ("feature", "identifier", "feature_name", "label", "name")
_BACKGROUND_ALIASES = ("async", "in_background", "run_in_background", "defer")
# The feature envelope the engine accepts beside "type" (session.cpp::kPrimitiveKeys plus
# the structural keys). Anything else on a flat call is a param.
_FEATURE_ENVELOPE_KEYS = frozenset({
    "type", "name", "params", "refs", "at", "at_offset", "axis", "axis_dir", "centered",
    "mode", "merge", "folder", "id", "suppressed", "body", "dir",
})
_OP_ORDER_KEYS = ("before", "after")
# DESIGN INTENT, not geometry. A model routinely annotates its build call with why it chose
# a number; none of these is a parameter of anything. Failing the whole build over an
# annotation is the exact turn-burning behaviour this normalizer exists to kill — so they
# are accepted, ignored, and reported.
_COMMENTARY_KEYS = frozenset({
    "purpose", "basis", "rationale", "reason", "why", "intent", "design_intent",
    "justification", "note", "notes", "comment", "comments", "description", "summary",
    "explanation", "requirement", "requirements", "acceptance", "acceptance_criteria",
    "criteria", "context", "assumption", "assumptions", "citation", "confidence",
    "clearance", "goal", "objective", "source", "reference",
})
_ADD_FEATURES_ACCEPTS = (
    'add_features accepts ops=[{"op":"add_feature","feature":{"type":"box","name":"Block",'
    '"params":{"length":40,"width":30,"height":20}}}] (the canonical batch), '
    'doc_id="doc-…" (an open part\'s NAME also resolves) and background=true. The batch may '
    "also arrive as feature=/features=/op=/operations=/actions=/ops_json=, as one op dict, "
    "as a JSON string, or as ONE feature spread over top-level kwargs "
    "(type=…, name=…, params={…}). Call cad_guide() for the op vocabulary."
)


def _jsonify(v: Any) -> Any:
    """Tuples/sets from a Python literal -> JSON-shaped lists."""
    if isinstance(v, (list, tuple, set)):
        return [_jsonify(x) for x in v]
    if isinstance(v, dict):
        return {str(k): _jsonify(x) for k, x in v.items()}
    return v


def _as_batch(value: Any, where: str) -> Optional[list]:
    """Any container a model reaches for -> a list of ops. None: not a batch at all."""
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        return [value]
    if isinstance(value, str):
        s = value.strip()
        if s[:1] not in ("[", "{"):
            return None  # a bare word — a NAME, not a batch
        try:
            parsed = json.loads(s)
        except ValueError as e:
            # A Python REPR of the same structure ("[{'op': 'add_feature', …}]") is the
            # other thing models emit when told "send JSON"; it is unambiguous, so read it
            # rather than burn the turn. Only genuine pseudo-code falls through to the error.
            import ast  # noqa: PLC0415 — failure path only
            try:
                lit = ast.literal_eval(s)
            except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
                lit = None
            if not isinstance(lit, (list, tuple, dict)):
                raise ValueError(
                    f"{where} is a string that parses as neither JSON nor a Python literal "
                    f"({e}). Send a real JSON array: "
                    f'ops=[{{"op":"add_feature","feature":{{"type":"box","name":"Block",'
                    f'"params":{{"length":40,"width":30,"height":20}}}}}}].') from None
            parsed = _jsonify(lit)
        return parsed if isinstance(parsed, list) else [parsed]
    return None


def _batch_from_flat_kwargs(kw: dict, notes: list) -> Optional[list]:
    """ONE feature (or one op) spread over top-level kwargs -> a one-op batch.

    Consumes the keys it uses out of *kw*; returns None when the call names neither an op
    nor a feature type (the caller then reports the leftovers).
    """
    docops = _doc_op_vocab()
    ftypes, _live = _feature_type_vocab()

    op_key = op_word = None
    for k in ("op", "operation", "action", "kind"):
        v = kw.get(k)
        if isinstance(v, str) and v.strip():
            op_key, op_word = k, v.strip()
            break
    type_word = kw.get("type") if isinstance(kw.get("type"), str) else None
    type_word = type_word.strip() if type_word and type_word.strip() else None
    if not op_word and not type_word:
        return None

    # A doc op spelled flat: {"op":"delete_feature","id":"f2"}.
    if op_word and op_word in docops and not type_word:
        kw.pop(op_key, None)
        rest = {k: kw.pop(k) for k in list(kw)}
        notes.append(f'interpreted the flat call as ops=[{{"op":"{op_word}", …}}] — '
                     f"add_features always takes a LIST of ops")
        return [_normalize_op_target({"op": op_word, **rest}, op_word, notes)]

    ftype = type_word or op_word
    if op_word and not type_word:
        kw.pop(op_key, None)
        # Canonicalize HERE so the echoed note names the type the engine will see; an
        # unresolvable word is left verbatim for _retype_feature's teaching error.
        ftype = _canon_feature_type(op_word, ftypes) or op_word
        notes.append(f'interpreted {op_key}="{op_word}" as feature type "{ftype}" — '
                     f"a feature type is not an op name")
    else:
        kw.pop("type", None)

    feature: dict = {"type": ftype}
    params: dict = {}
    for pk in _PARAMS_ALIASES:
        v = kw.pop(pk, None)
        if isinstance(v, dict):
            params.update(v)
            if pk != "params":
                notes.append(f"interpreted {pk}= as the feature's params")
        elif v is not None:
            kw[pk] = v  # not a mapping — fall through to the leftover report

    order = {k: kw.pop(k) for k in _OP_ORDER_KEYS if k in kw}
    for k in list(kw):
        if k in _FEATURE_ENVELOPE_KEYS and k != "type":
            feature[k] = kw.pop(k)
    if not feature.get("name"):
        for k in _FEATURE_NAME_ALIASES:
            v = kw.get(k)
            if isinstance(v, str) and v.strip():
                feature["name"] = v.strip()
                kw.pop(k)
                notes.append(f'interpreted {k}="{v}" as the feature name')
                break
    folded = sorted(kw)
    for k in folded:
        params[k] = kw.pop(k)
    if params or "params" in feature:
        feature["params"] = {**params, **(feature.get("params") or {})}
    if folded:
        notes.append("read top-level " + ", ".join(folded) + " as the feature's params")
    notes.append(
        'interpreted a bare feature spec as one add_feature op; the canonical form is '
        'ops=[{"op":"add_feature","feature":{…}}]')
    return [{"op": "add_feature", "feature": feature, **order}]


def _normalize_add_features_args(ops: Any, doc_id: Optional[str], extra: dict,
                                 notes: list) -> tuple:
    """-> (ops list, doc_id, background_override). Raises ValueError to TEACH, never guesses."""
    supplied = {k: v for k, v in (extra or {}).items() if v is not None}
    background = None
    for k in _BACKGROUND_ALIASES:
        if k in supplied:
            background = bool(supplied.pop(k))
            notes.append(f"interpreted {k}= as background=")

    batches: list = []
    if ops is not None:
        primary = _as_batch(ops, "ops")
        if primary is None:
            raise ValueError(
                f"ops must be a list of ops — got {type(ops).__name__} {ops!r}. "
                + _ADD_FEATURES_ACCEPTS)
        if not isinstance(ops, list):
            notes.append(f"read ops={type(ops).__name__} as a one-element op list"
                         if isinstance(ops, dict) else "parsed the ops JSON string")
        batches.append(("ops", primary))
    for key in _BATCH_ALIASES:
        if key not in supplied:
            continue
        batch = _as_batch(supplied[key], key)
        if batch is None:
            continue  # a bare word under an alias name — a NAME; resolved further down
        supplied.pop(key)
        batches.append((key, batch))
        notes.append(f"interpreted {key}= as ops= — the batch parameter is called `ops`")
    if len(batches) > 1:
        head_label, head = batches[0]
        clashing = [lbl for lbl, b in batches[1:] if b != head]
        if clashing:
            raise ValueError(
                f"add_features() got DIFFERENT op batches under "
                f"{', '.join([head_label] + clashing)} and cannot tell which one to build. "
                f"Send exactly one, under `ops`. " + _ADD_FEATURES_ACCEPTS)
        notes.append(f"ignored duplicate batch(es) under "
                     f"{', '.join(lbl for lbl, _ in batches[1:])} (identical to {head_label})")

    commentary = [k for k in sorted(supplied) if _slug(k) in _COMMENTARY_KEYS]
    for k in commentary:
        supplied.pop(k)
    if commentary:
        notes.append("ignored " + ", ".join(commentary) + " — design intent/commentary, not "
                     "modeling parameters; record reasoning with ledger_note()")

    final = batches[0][1] if batches else _batch_from_flat_kwargs(supplied, notes)

    for key in _DOC_HANDLE_ALIASES:
        v = supplied.get(key)
        if not (isinstance(v, str) and v.strip()):
            continue
        supplied.pop(key)
        if doc_id and str(doc_id).strip() != v.strip():
            raise ValueError(
                f'add_features() got doc_id="{doc_id}" AND {key}="{v}", which name different '
                f"documents. Pass the target part once, as doc_id (its id or its exact name).")
        if not doc_id:
            doc_id = v.strip()
            notes.append(
                f'interpreted {key}="{v}" as doc_id — add_features targets a DOCUMENT and an '
                f"open part's name resolves to its doc_id; a FEATURE's name goes in "
                f"feature.name inside an op")
        break

    if supplied:
        raise ValueError(
            f"add_features() got argument(s) it cannot place: {', '.join(sorted(supplied))}. "
            + _ADD_FEATURES_ACCEPTS)
    if final is None:
        raise ValueError("add_features() was called with no ops. " + _ADD_FEATURES_ACCEPTS)
    return final, doc_id, background



_NATIVE_FILE_REPLY_LIMIT = 32_768
_NATIVE_FILE_PLAN_LIMIT = 16 * 1024 * 1024


def _native_file_path(value: Any, field: str, *, native: bool = False) -> str:
    """Resolve and authorize a file through the existing filesystem jail."""
    from .fs_tools import _canonical_target

    if not isinstance(value, str) or not value.strip() or "\0" in value:
        raise ValueError(f"{field} must be a non-empty path string")
    if not Path(value).is_absolute():
        raise ValueError(f"{field} must be an absolute path")
    target, blocked = _canonical_target(value, allow_missing=True)
    if blocked:
        raise cb.CoreError(blocked["hint"], code=blocked["reason"],
                           detail={"field": field})
    assert target is not None
    if native and target.suffix.lower() != ".step+":
        raise ValueError(f"{field} must name a native .step+ file")
    return str(target)


def _native_report_path(value: str) -> str:
    if isinstance(value, str) and os.path.lexists(value):
        raise FileExistsError("output_path already exists; choose a new .json file")
    path = _native_file_path(value, "output_path")
    if Path(path).suffix.lower() != ".json":
        raise ValueError("output_path must end in .json (never a native CAD file)")
    if os.path.lexists(path):
        raise FileExistsError("output_path already exists; choose a new .json file")
    if not Path(path).parent.is_dir():
        raise ValueError("output_path parent directory must already exist")
    return path


def _native_file_reply(result: dict, output_path: Optional[str] = None) -> str:
    """Keep native outcomes intact in an artifact; bound only the chat projection."""
    text = json.dumps(result, ensure_ascii=True, indent=2)
    report_error = None
    if output_path:
        try:
            # Recheck symlinks/root immediately before the exclusive report write.
            output_path = _native_report_path(output_path)
            with open(output_path, "x", encoding="utf-8") as stream:
                stream.write(text)
                stream.write("\n")
        except Exception as exc:  # noqa: BLE001 — native writes may already have succeeded
            report_error = str(exc)[:512]
    if not output_path and len(text) <= _NATIVE_FILE_REPLY_LIMIT:
        return _dumps(result)
    files = result.get("files", [])
    failures = result.get("failures", [])
    summary = {
        "ok": result.get("ok", False),
        "action": result.get("action"),
        "file_count": len(files),
        "succeeded_count": sum(isinstance(f, dict) and f.get("ok") is True for f in files),
        "failed_count": sum(isinstance(f, dict) and f.get("ok") is False for f in files),
        "failure_count": len(failures),
        "full_result_in_response": False,
    }
    preview = []
    for failure in failures[:5]:
        failure_text = json.dumps(failure)
        if len(failure_text) > 1024:
            preview.append({"preview": failure_text[:1024], "truncated": True})
        else:
            preview.append(failure)
    summary["failures_preview"] = preview
    summary["failures_omitted"] = max(0, len(failures) - len(preview))
    if output_path and report_error is None:
        summary.update(output_path=output_path, report_written=True)
    else:
        summary["report_written"] = False
        summary["hint"] = ("Full metadata omitted. Inspect smaller path batches or supply "
                           "a new guarded output_path to inspect_native_files.")
        if report_error:
            # Do not turn completed native relocations into an apparent no-write refusal.
            summary.update(native_ok=result.get("ok", False), ok=False,
                           report_error=report_error, requested_output_path=output_path)
            summary["hint"] = ("Native outcomes above remain valid; report persistence failed. "
                               "Inspect the destinations before retrying any relocation.")
    for key in ("error", "reason", "message", "code"):
        if key in result:
            summary[key] = str(result[key])[:512]
    return _dumps(summary)


def _native_file_error(exc: Exception, action: str) -> str:
    text = _err(exc)
    if len(text) <= _NATIVE_FILE_REPLY_LIMIT:
        return text
    return _dumps({"ok": False, "action": action, "error": str(exc)[:2048],
                   "code": str(getattr(exc, "code", ""))[:256], "error_detail_omitted": True})


def _native_plan_object(pairs: list[tuple[str, Any]]) -> dict:
    """Reject duplicate JSON keys, especially silently overwritten path-map entries."""
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError(f"relocation plan contains a duplicate JSON key: {key[:200]}")
        obj[key] = value
    return obj


_NATIVE_READ_SECTIONS = (
    "features", "components", "mates", "explode", "material", "body_materials",
    "appearances", "face_appearances", "meta", "equations", "configurations",
    "rollback_after", "studies",
)


def _native_read_bounded(value: Any) -> Any:
    """Keep large embedded payloads out of the model's tool response."""
    if isinstance(value, str):
        return f"<{len(value)} chars omitted>" if len(value) > 2000 else value
    if isinstance(value, dict):
        return {key: _native_read_bounded(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_native_read_bounded(item) for item in value]
    return value


def _native_read_rows(rows: Any, keys: tuple[str, ...],
                      ids: Optional[list[str]] = None) -> list[dict]:
    return [
        {key: row[key] for key in keys if key in row}
        for row in rows if isinstance(row, dict) and
        (ids is None or row.get("id") in ids)
    ]


def _native_read_file(path: str, sections: Optional[list[str]],
                      feature_ids: Optional[list[str]], component_ids: Optional[list[str]],
                      include_params: bool) -> str:
    if sections is not None and not isinstance(sections, list):
        return _dumps({"ok": False, "code": "unknown_section",
                       "error": f"sections must be a list of section names, got {sections!r}. "
                                f"Valid sections: {', '.join(_NATIVE_READ_SECTIONS)}",
                       "valid_sections": list(_NATIVE_READ_SECTIONS)})
    unknown = [section for section in sections or ()
               if not isinstance(section, str) or section not in _NATIVE_READ_SECTIONS]
    if unknown:
        suggestions = {
            str(section): close[0]
            for section in unknown
            if (close := difflib.get_close_matches(str(section), _NATIVE_READ_SECTIONS,
                                                   n=1, cutoff=0.5))
        }
        return _dumps({"ok": False, "code": "unknown_section",
                       "error": f"Unknown section(s): {unknown!r}"
                                + (f" (did you mean {suggestions!r}?)" if suggestions else "")
                                + f". Valid sections: {', '.join(_NATIVE_READ_SECTIONS)}",
                       "unknown_sections": unknown,
                       "did_you_mean": suggestions,
                       "valid_sections": list(_NATIVE_READ_SECTIONS)})
    try:
        if isinstance(path, str) and path.strip() and not Path(path).is_absolute():
            from .fs_tools import _canonical_target
            target, blocked = _canonical_target(path, allow_missing=True)
            if blocked:
                raise cb.CoreError(blocked["hint"], code=blocked["reason"],
                                   detail={"field": "path"})
            assert target is not None
            path = str(target)
        path = _native_file_path(path, "path", native=True)
        with zipfile.ZipFile(path) as archive:
            try:
                document = json.loads(archive.read("document.json"))
            except KeyError:
                return _dumps({"ok": False, "code": "missing_document",
                               "error": "Native file is missing document.json"})
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                return _dumps({"ok": False, "code": "bad_document_json",
                               "error": f"Invalid document.json: {exc}"})
    except (zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        return _dumps({"ok": False, "code": "invalid_zip",
                       "error": f"Not a valid .step+ ZIP container: {exc}"})
    except Exception as exc:  # noqa: BLE001
        return _err(exc)
    if not isinstance(document, dict):
        return _dumps({"ok": False, "code": "bad_document_json",
                       "error": "document.json must contain a JSON object"})
    schema = document.get("schema")
    if schema not in ("aicad.part/1", "aicad.asm/1"):
        return _dumps({"ok": False, "code": "unsupported_schema",
                       "error": f"Unsupported part/assembly document schema: {schema!r}"})
    selected = set(_NATIVE_READ_SECTIONS if sections is None else sections)
    features = document.get("features") or []
    components = document.get("components") or []
    mates = document.get("mates") or []
    result = {
        "ok": True, "path": path, "schema": schema, "id": document.get("id"),
        "name": document.get("name"), "units": document.get("units"),
        "kind": "assembly" if schema == "aicad.asm/1" else "part",
        "counts": {
            "features": len(features), "components": len(components),
            "mates": len(mates),
            "bodies_with_material": len(document.get("body_materials") or {}),
            "appearances": len(document.get("appearances") or {}),
        },
    }
    if "features" in selected:
        keys = ("id", "type", "name", "suppressed", "folder", "refs")
        if include_params:
            keys += ("params",)
        result["features"] = _native_read_rows(features, keys, feature_ids)
    if "components" in selected:
        from .cad_asm_tools import resolve_component_source  # noqa: PLC0415
        rows = _native_read_rows(
            components, ("id", "name", "source", "transform", "fixed",
                         "suppressed", "hidden"), component_ids)
        # A stored relative source is relative to THIS file's folder. Report the absolute
        # file next to it, so a caller can insert the same file into another assembly
        # (2026-10-10: `parts/redesign/...` copied into a new assembly missed the file).
        base = os.path.dirname(path)
        for row in rows:
            source = row.get("source")
            stored = source.get("path") if isinstance(source, dict) else None
            if isinstance(stored, str) and stored:
                row["source_resolved"] = resolve_component_source(stored, base)
        result["components"] = rows
    if "mates" in selected:
        result["mates"] = _native_read_rows(
            mates, ("id", "type", "align", "suppressed", "refs", "value"))
    for section in _NATIVE_READ_SECTIONS:
        if section in selected and section not in {
                "features", "components", "mates", "configurations", "studies"}:
            result[section] = document.get(section)
    if "configurations" in selected:
        result["configurations"] = document.get("configurations", [])
        result["active_configuration"] = document.get("active_configuration")
    if "studies" in selected:
        result["studies"] = [
            {"id": study.get("id"), "name": study.get("name"),
             "kind": study.get("kind", study.get("type"))}
            for study in (document.get("studies") or []) if isinstance(study, dict)
        ]
    return _dumps(_native_read_bounded(result))


#: get_part(summary=True): a list longer than _SUMMARY_LIST_MAX keeps its first
#: _SUMMARY_LIST_KEEP items, and the feature records the cut in `truncated_lists`.
_SUMMARY_LIST_MAX = 64
_SUMMARY_LIST_KEEP = 16


def register_cad_doc_tools(mcp: FastMCP) -> None:
    """CAD document-lifecycle MCP tools (split out of cad_tools.py): new_part, add_features, get_part, undo/redo, save/close/recover, configurations, material, engine_profile, cad_guide."""


    # ---------------------------------------------------------------- knowledge

    @mcp.tool()
    def cad_guide(topic: str = "overview") -> str:
        """Return the engine's modeling/simulation knowledge so you can drive it well. CALL FIRST.

        This is the same closed op vocabulary + worked examples the in-app agent's modeler is
        taught. topic="overview" (default) gives the workflow + full op cheatsheet (primitives,
        sketch+extrude/revolve/loft/sweep, fillet/chamfer/shell/draft, holes, bolt_circle,
        patterns/mirror, edits) and the plain-English geometry-query predicates. Other topics:
        "assembly" (new_assembly/asm_add_component/asm_mate/solve_mates — put saved parts
        together with mates so they actually fit; seating patterns + validation loop),
        "simulation" (FEA/CFD how-to), "motion" (simulate_motion — drive a mate to animate an
        assembly + swept interference, kinematics), "review" (how to read the annotated render), "gsl" (the
        progressive TEXT read of a part's geometry — describe_part/describe_sketch/
        describe_scene/hover/query_adjacency — including the non-vision self-review path),
        "narration" (how to use describe_toolpaths and describe_results to get compact
        text summaries of CAM jobs and FEA/CFD results instead of raw JSON dumps),
        "parts" (standard-parts library: search→get→place workflow, standard-before-custom
        bias, parts_taxonomy category browse + faceted part_search, domain routing,
        editability guarantee, sourcing), "reverse"
        (reverse-engineering an imported STEP/mesh into an editable feature tree),
        "requirements" (req_create/update/status/report — design-brief CRUD),
        "dag" (dag_plan/run/status/approve/abort/replan — multi-step mission orchestration),
        "verify" (verify_battery — done-check before declaring a design finished),
        "interaction" (ask_user elicitation seam — batch unknowns into one call),
        "sourcing" (part_source need→ranked candidates, artifact plans (place / scrape+import /
        make_standard_part), vendors_health login etiquette, cam_tool_import cutting tools,
        bom_rollup/bom_price + request_quote),
        "cost" (cam_cost_estimate/request_quote — made-part cost model),
        "spec" (match_part_number/spec_sheet_match — MPN resolution + datasheet extraction),
        "piping" (piping_solve/report/... — 1-D hydraulic solve + CFD handoff),
        "hydraulic" (hydraulic_new/add_component/... — ISO 1219 circuit design),
        "firmware" (fw_new_project/build/sim_run/flash/debug — embedded compile loop),
        "structural" (struct_analyze/check/estimate — PyNite member forces + code check),
        "mechanical" (gear_check/bearing_life — AGMA gear tooth stress + ISO 281 bearing life),
        "regen" (regen_diagnose — diagnose + propose fix-ops on rebuild failure),
        "code_compliance" (code_check — building-code compliance with RAG citations),
        "design_review" (design_review — one-call geometry battery narrated verdict),
        "doc_diff" (doc_diff_tool — semantic feature-level diff between two revisions),
        "plm" (project-vault versioning),
        "tolerance_stackup" (stackup_analyze — 1D worst-case + RSS tolerance stack-up),
        "sketch3d" (sketch3d_new/add_curves/get/as_sweep_guide + render_sketch/
        sketch_preview_text/doc_sketch_contact_sheet — 3D free-space sketch curves
        usable as sweep spines/guide curves, and 2D sketch preview before extruding),
        or a single feature's cheatsheet by name ("hole", "fillet", "revolve", "loft",
        "sweep", "shell", "chamfer", "pattern", "mirror", "sketch", "airfoil",
        "drawing", "hardware", ...).
        """
        return cad_guide_text(topic)

    @mcp.tool()
    def cad_skill(name: str, path: str | None = None) -> str:
        """Load one CAD skill pack — the full procedure behind a name in the [SKILLS] catalog.

        The catalog in your system prompt lists every pack by name and one-line
        description; this returns the body: the numbered procedure, its failure modes
        and its exact tool-call sequences (e.g. name="assembly-merge" for merging
        independently-sourced CAD, name="simulate" for the FEA/CFD release-evidence
        path). Pass path="<file>" for a resource file inside a pack's directory.
        This is the ONLY way to fetch a Chudware skill: a skill:// URL is not a
        Chudware path and resolves against an unrelated registry. An unknown name
        returns the list of real names instead of an error.
        """
        # Lazy import: the skills catalog is agent-side and services/mcp must stay
        # importable without it (packaged MCP-only builds).
        from services.agent import skills as _agent_skills

        return _agent_skills.skill_reference_text(name, path)



    # ---------------------------------------------------------------- diagnostics

    @mcp.tool()
    def engine_profile(action: str = "status", hz: int = 0, mode: str = "modeling",
                       top: int = 40, min_samples: int = 0,
                       write_folded: str = "", write_speedscope: str = "") -> str:
        """Profile THIS engine process (the one your modeling RPCs run in) at runtime.

        The desktop app's profiler window only sees the GUI's own engine — a DIFFERENT process
        from this MCP server, where subagent modeling actually happens. This tool drives the
        engine's built-in sampling profiler + lock-contention meters in-process, so a heavy
        multi-subagent run can be measured without a restart. All modeling in one engine
        serialises under a single global lock; the `contention` action is what reveals time
        spent BLOCKED on that lock (which the CPU sampler cannot see).

        action:
          "start"       begin sampling. hz sets the rate (default ~1kHz); mode "modeling"
                        (default) samples only modeling ops, "all" samples every RPC.
          "stop"        stop sampling and return the symbolized report.
          "report"      return the report WITHOUT stopping (modules + hot frames + folded stacks).
          "status"      (default) is the sampler running? how many samples so far?
          "contention"  return g_mtx lock-wait/hold/queue-depth stats since last reset — the
                        primary multi-subagent bottleneck signal (%wall blocked, top callers).
          "reset_contention"  zero the contention counters (call before a measured run).
          "phases"      per-study simulation phase timings (always-on, no start needed):
                        every mesher / FEA static / modal / thermal / fatigue / LBM /
                        flow / overlay run as {domain, total_ms, phases:[{name,ms,pct}]}
                        — mesh vs assemble vs factorize vs post, per study. The same
                        breakdown also rides along on "stop"/"report" under "phases".
          "reset_phases"  clear the phase registry (call before a measured run).

        top/min_samples/write_folded/write_speedscope tune the sampling report.
        """
        try:
            a = (action or "status").strip().lower()
            if a == "start":
                p = {"mode": mode}
                if hz and hz > 0:
                    p["hz"] = int(hz)
                return _dumps(cb.call("prof_start", p))
            if a == "stop":
                return _dumps(cb.call("prof_stop", {"top": top, "min_samples": min_samples,
                                                    "write_folded": write_folded or None,
                                                    "write_speedscope": write_speedscope or None}))
            if a == "report":
                return _dumps(cb.call("prof_report", {"top": top, "min_samples": min_samples,
                                                      "write_folded": write_folded or None,
                                                      "write_speedscope": write_speedscope or None}))
            if a == "status":
                return _dumps(cb.call("prof_status", {}))
            if a == "contention":
                return _dumps(cb.call("prof_contention", {}))
            if a == "reset_contention":
                return _dumps(cb.call("prof_contention", {"reset": True}))
            if a == "phases":
                return _dumps(cb.call("prof_phases", {}))
            if a == "reset_phases":
                return _dumps(cb.call("prof_phases", {"reset": True}))
            return _err(ValueError(
                f"unknown action {action!r}; use start|stop|report|status|contention|"
                f"reset_contention|phases|reset_phases"))
        except Exception as e:  # noqa: BLE001
            return _err(e)



    # ---------------------------------------------------------------- modeling

    def _doc_path_stem(entry: dict) -> str:
        """Case-folded file stem of a doc's backing path, or "".

        The app's native suffixes are two-part tokens (".step+", ".stl+"), so the
        stem is everything before the LAST '.' of the basename — "mudbucket.step+"
        stems to "mudbucket", and a dotted name like "v1.2_bracket" keeps its dot
        only if it also carries a real extension."""
        raw = entry.get("path") or entry.get("file_path") or ""
        if not isinstance(raw, str) or not raw.strip():
            return ""
        base = raw.strip().replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
        if "." in base:
            base = base.rsplit(".", 1)[0]
        return base.strip().casefold()

    def _resumed_reply(entry: dict, name: str, *, by_stem: str = "",
                       version_of: str = "") -> dict:
        """The new_part reply for a document RESUMED instead of minted.

        `entry` is either the engine's doc_new(resume_if_open=True) reply or a doc_list
        row — both carry doc_id / name / revision / feature_count. A resumed part that
        ALREADY CARRIES features says so in the note: inheriting a half-built body
        silently is what produced twelve "boss added no material (it is buried inside the
        body)" failures, so the count always travels."""
        did = entry["doc_id"]
        _CUR["doc_id"] = did
        note_touched_doc(did)
        # Never seed a FAKE baseline. `setdefault` alone only protected a baseline this
        # process had already built, and neither doc_new(resume_if_open=True) nor doc_list
        # reports a `volume` key — so every resume from a process that had not itself built
        # the part seeded 0.0. Measured live 2026-07-30: a resumed 48000 mm^3 box, then cut
        # by 282.3 mm^3, reported "volume 0.0->47717.7 mm^3 (+47717.7)" — sign inverted,
        # magnitude 169x off, on the exact op the model was meant to verify. Read the real
        # volume from the engine; if that fails leave the baseline ABSENT so _write_diff
        # degrades to the honest "volume now X" branch instead of inventing a delta.
        if did not in _LAST_VOLUME:
            vol = entry.get("volume")
            if not isinstance(vol, (int, float)):
                try:
                    vol = (cb.call("eval_mass", {"doc_id": did}) or {}).get("volume_mm3")
                except Exception:  # noqa: BLE001
                    vol = None
            if not isinstance(vol, (int, float)) and entry.get("feature_count") == 0:
                vol = 0.0  # an empty part's baseline really is zero
            if isinstance(vol, (int, float)):
                _LAST_VOLUME[did] = float(vol)
        built = entry.get("feature_count")
        carries = (f"; it ALREADY CARRIES {built} feature(s) — build on them "
                   f"(get_part first), or call new_part(fresh=True) for a "
                   f"separate empty document"
                   if isinstance(built, int) and built > 0 else "")
        out = {
            "ok": True, "doc_id": did,
            "name": entry.get("name") or name,
            "revision": entry.get("revision", 0),
            "reused": True, "resumed": True,
            "feature_count": built,
            "note": f'resumed the open part "{name}" (doc_id={did}) instead of '
                    f"minting a duplicate document" + carries}
        if by_stem:
            out["resumed_by"] = by_stem
            src = entry.get("path") or entry.get("file_path") or ""
            out["note"] = (
                f'resumed the open part imported from "{src}" (doc_id={did}), '
                f'whose file stem is "{name}", instead of minting a duplicate '
                f"document" + carries)
        elif version_of:
            # The user's own report: nine `_v3…_v11` tabs for one holster, offered back
            # as "parts potentially lost" on the next launch. A bumped version suffix is
            # a REVISION of the design, and this product keeps revisions in history, not
            # in extra documents — so say what happened and where the old state went.
            actual = out["name"]
            out["resumed_by"] = "version_series"
            out["version_series"] = version_of
            out["requested_name"] = name
            out["note"] = (
                f'"{name}" is the next VERSION of the open part "{actual}" '
                f'(doc_id={did}), not a separate part — resumed it so the design stays '
                f"ONE document, ONE file and ONE version history. Edit it IN PLACE; the "
                f"state before this call is already checkpointed (plm_timeline lists the "
                f"history, plm_rollback returns to an earlier point). Keep calling this "
                f'part "{actual}" — do NOT bump the name again. Only call '
                f"new_part(fresh=True) if you genuinely need a second variant to exist "
                f"ALONGSIDE this one." + carries)
        else:
            others = [str(o) for o in (entry.get("duplicate_doc_ids") or [])]
            if others:
                out["duplicates"] = len(others) + 1
                out["note"] += (
                    f'; {len(others) + 1} open documents answer to "{name}" '
                    f"({', '.join([did] + others)}) — pass doc_id= explicitly to work "
                    f"in a specific one, or new_part(fresh=True) for a deliberately "
                    f"separate document")
        return out

    def _in_name_series(doc_name: str, requested: str) -> bool:
        """True for `requested` itself and for the sibling names doc_new's
        unique_doc_name mints when it uniquifies a collision. Case-insensitive.

        MIRRORS core/doc/session.cpp unique_doc_name (~4983-5002): it strips a TRAILING
        INTEGER off the requested name to get the SERIES BASE, then appends N. So the
        siblings of "Part1" are "Part2"/"Part3" — they do NOT start with "Part1".
        Matching `requested` + digits therefore missed every name ENDING in a digit,
        which is the common case (Part1, bracket_v2, Rev3, and any name carrying a hex
        id whose last character happens to be a digit) and made the orphan sweep
        silently find nothing. Caught by test_new_part_resume_discards_unbuilt_orphans,
        which was passing or failing on the coin flip of its random suffix's last
        character."""
        n = (doc_name or "").strip().casefold()
        req = (requested or "").strip().casefold()
        if not n or not req:
            return False
        if n == req:
            return True
        base = req.rstrip("0123456789") or req  # all-digit name: the whole thing is base
        if not n.startswith(base):
            return False
        tail = n[len(base):]
        # `tail.size() > 9` is the engine's own guard against absurd suffixes.
        return bool(tail) and tail.isdigit() and len(tail) <= 9

    def _is_own_project_stub(path) -> bool:
        """True when `path` is absent, or is a file new_part's own _project_save wrote
        into CHUDWARE_PROJECT_SAVE_DIR. Anything else is a file the USER chose, and a
        document backed by one is never discarded by the orphan sweep."""
        if not path or not isinstance(path, str) or not path.strip():
            return True
        from services.agent.request_context import root_value
        root = root_value("CHUDWARE_PROJECT_SAVE_DIR")
        if not root:
            return False
        try:
            return os.path.dirname(os.path.realpath(path.strip())) == \
                os.path.realpath(root)
        except OSError:
            return False

    def _discard_unbuilt_orphans(name: str, keep: str) -> list:
        """Close leftover EMPTY documents in the "name", "name2", "name3", ... series.

        A worker SIGKILLed on the 5-minute run timeout (core/agent/run.cpp:782) never gets
        to close the document it minted, and the machine-wide daemon keeps it open: the
        GUI's read-only mirror attach clears hostd's orphan deadline, so its GC never
        reaps it either. The user's retry then meets a stale empty tab — or three (live
        2026-07-29, run-42508173312214: one requested part, three tabs).

        SAFETY — only a document with NOTHING TO LOSE is closed, judged from the engine's
        own doc_list summary: `empty` (its "no authored content worth recovering" flag)
        AND feature_count 0 (no geometry), revision 0 (never edited), not `dirty` (no
        unsaved work), kind "part", in this name series, and never `keep` or the current
        doc. Its `path` must be absent or be the .step+ stub new_part itself wrote into
        CHUDWARE_PROJECT_SAVE_DIR — a document backed by a file the USER chose is never
        touched, however empty it looks. (That stub file is left on disk: deleting a
        user-visible file is not this function's business, and an empty .step+ is
        harmless.) Attachment is deliberately NOT consulted — the engine exposes none —
        but a document passing every test above holds no content any attached client could
        lose, and closing it fans out doc.closed so a mirror tab follows. A document
        mid-operation is protected by the engine itself: doc_close try_locks and raises
        doc_busy rather than tearing down a live session, and that error is swallowed here
        so the doc simply stays open."""
        closed = []
        try:
            rows = (cb.call("doc_list", {}) or {}).get("docs", [])
        except Exception:  # noqa: BLE001 — cleanup must never fail the caller's mint
            return closed
        for e in rows:
            if not isinstance(e, dict):
                continue
            did = e.get("doc_id")
            if not did or did == keep or did == _CUR["doc_id"]:
                continue
            if e.get("kind", "part") != "part" or not _in_name_series(e.get("name"), name):
                continue
            if not e.get("empty") or e.get("feature_count") or e.get("component_count"):
                continue
            if e.get("dirty") or (e.get("revision") or 0) != 0:
                continue
            if not _is_own_project_stub(e.get("path")):
                continue
            try:
                cb.call("doc_close", {"doc_id": did})
            except Exception:  # noqa: BLE001 — doc_busy et al: leave it open
                continue
            _unpublish(did)
            _LAST_VOLUME.pop(did, None)
            closed.append(did)
        return closed

    @mcp.tool()
    def new_part(name: str = "part", *, fresh: bool = False) -> str:
        """Create an editable part document named `name` and make it current.

        A part of that name that is ALREADY OPEN is RESUMED, never duplicated — and that
        holds however many open documents answer to the name, and however many callers ask
        at the same instant. A general agent that retries a brief, or a `model3d` worker
        that respawns per turn, calls new_part(name="Widget") again with no memory of the
        doc_id it got back; left to itself the engine's doc_new UNIQUIFIES the collision
        into "Widget2" and mints a second document, and every mint fans out doc.opened to
        the GUI, so copies of "the same part" pile up in the user's tab strip (observed
        live: one requested part produced CD-1400_TwistedLatticeSphere and five more
        numbered ...2 through ...6; and an import followed by two new_part("mudbucket")
        calls left mudbucket, mudbucket and mudbucket2 open at once).
        The name IS the identity an agent addresses a part by, so asking for one that exists
        means "resume it". The rules, exactly:

          * ONE OR MORE open parts named `name` -> resume the best of them (most features,
            then highest revision, then most recently opened). The reply reports
            `duplicates` and every colliding doc_id when there was more than one; pass
            `doc_id=` to a modeling tool to work in a specific one. The decision is made
            INSIDE doc_new (resume_if_open), under the engine's mint lock — two callers
            racing on the same name get one document, not two.
          * NO name match, but the name is the NEXT VERSION of an open part — "holster_v4"
            when "holster" or "holster_v3" is open and built -> resume THAT part; the reply
            carries `resumed_by="version_series"`. A version bump is a REVISION of one
            design, and revisions live in this product's version history (plm_timeline /
            plm_rollback), never in extra documents: keep using the name the reply hands
            back and edit the part in place. (Live 2026-08-05: one holster request left
            NINE tabs — ruger_lcp_iwb_holster_v3 through _v11 — nine .step+ files in the
            user's project folder, nine separate PLM items, and nine rows in the next
            launch's "Recover unsaved parts?" prompt.) An explicit token is required:
            "_v4"/"-rev2"/" gen3" are versions; a trailing bare number is not, because
            "PN-1235" is a different part from "PN-1234".
          * NO name match, but exactly ONE open part whose FILE STEM is `name`
            (import_part("Projects/Testing/mudbucket.step+") often leaves the doc named
            after the file) -> resume that one; the reply carries `resumed_by="path_stem"`.
            Several parts stemming to the same name are ambiguous and are NOT guessed — a
            new document is minted instead, because editing the wrong part is worse.
          * A resume also closes any EMPTY same-name-series leftovers (`discarded_orphans`)
            — the zero-geometry documents a worker killed on the run timeout left behind.
          * `fresh=True` is the ONLY way to force a second document with a taken name, and
            it neither resumes nor discards anything.

        A resumed part that already carries geometry says so in `feature_count` and `note`:
        build ON what is there. Ignoring that and placing the next body at the origin is what
        produces "boss added no material (it is buried inside the body)".

        Follow with add_features(...) to build geometry. Each part is a real parametric feature
        tree (undo/redo, editable parameters), not a one-shot mesh.
        """
        try:
            # A document under the EXACT requested name is already open. That case is the
            # engine's own resume_if_open path (below), and the engine keys it on the name
            # VERBATIM — so the mint payload must stay verbatim too. Stripping the version
            # token here would hand doc_new a name nothing is open under, and the "resume"
            # would mint a tenth document: precisely the duplication this whole path exists
            # to stop. A document already CALLED "..._v10" is existing state, not a new name
            # being coined; renaming it is plm's business, not new_part's.
            exact_open = False
            if not fresh:
                # The PATH-STEM resume still needs the doc list: the engine keys documents
                # by name, not by the file they were imported from. It never mints, so two
                # racing callers both land on the same imported document.
                docs = _resumable_docs()
                exact_open = bool(_docs_named(docs, name))
                if not exact_open:
                    folded = (name or "").strip().casefold()
                    stemmed = [e for e in docs if folded and _doc_path_stem(e) == folded]
                    # Exactly one is an identification; several is a guess, and the wrong
                    # part edited is worse than one extra document.
                    if len(stemmed) == 1:
                        return _dumps(_resumed_reply(stemmed[0], name, by_stem="path_stem"))
                    # "holster_v4" while "holster_v3" is open and built: the model is on
                    # its next iteration, not its next part. Resume the most-built member
                    # of the series so the design stays one document with one history.
                    # Checked AFTER the stem match — a file the user actually opened is a
                    # stronger identification than a name pattern.
                    series = _docs_in_version_series(docs, name)
                    best = _pick_best_doc(series)
                    if best is not None:
                        out = _resumed_reply(
                            best, name, version_of=_version_series_base(name))
                        dropped = _discard_unbuilt_orphans(name, best["doc_id"])
                        if dropped:
                            out["discarded_orphans"] = dropped
                            out["note"] += (
                                f"; closed {len(dropped)} empty leftover document(s) of "
                                f"the same series ({', '.join(dropped)})")
                        return _dumps(out)
            # The NAME check is NOT made here. It used to be — an unlocked doc_list
            # "is it open?" followed by a separate doc_new mint, with nothing holding
            # across the two. When the user retried a request whose first worker was still
            # inside its 5-minute timeout, both attempts reached the ONE machine-wide
            # daemon, both saw the name free, and both minted: one requested part became
            # three tabs (live 2026-07-29, run-42508173312214 — lexus_f_keychain, ...2,
            # ...3). resume_if_open makes the engine do check-and-mint under its mint lock,
            # so a concurrent loser resumes the winner's document instead of minting.
            mint_name = name if exact_open else _strip_version_suffix(name)
            r = cb.call("doc_new", {"kind": "part", "name": mint_name,
                                    "resume_if_open": not fresh, **_doc_new_scope()})
            did = r.get("doc_id")
            if r.get("reused"):
                out = _resumed_reply(r, name)
                # Sweep the empty documents a killed worker left in this name series
                # (never on fresh=True: that call explicitly asks for a document ALONGSIDE
                # whatever is open, so removing one would contradict it).
                dropped = _discard_unbuilt_orphans(name, did)
                if dropped:
                    out["discarded_orphans"] = dropped
                    out["note"] += (f"; closed {len(dropped)} empty leftover document(s) "
                                    f"of the same name ({', '.join(dropped)})")
                return _dumps(out)
            _CUR["doc_id"] = did
            note_touched_doc(did)
            # Seed the write-diff baseline: an empty part has zero volume, so the FIRST
            # add_features reports a real volume delta ("0.0 -> X (+X)") instead of degrading
            # to the volume-only "volume now X" branch of _write_diff.
            _LAST_VOLUME[did] = 0.0
            # doc_new UNIQUIFIES a colliding name. Returning the requested name would be a
            # lie the caller then repeats in every later name-based lookup, so report what
            # the document is actually called.
            actual = r.get("name") or mint_name
            out = {"ok": True, "doc_id": did, "name": actual,
                   "revision": r.get("revision", 0)}
            if mint_name != name:
                out["requested_name"] = name
                out["note"] = (
                    f'dropped the version token from "{name}" because revisions are '
                    f'tracked in PLM (plm_checkpoint / plm_timeline); this document was '
                    f'minted as "{actual}", so keep calling it "{actual}" from now on')
            if fresh and actual != name and actual != mint_name:
                collision_note = (
                    f'a document named "{mint_name}" was already open, so the engine '
                    f'numbered this fresh document "{actual}"; to iterate on the existing '
                    f'design instead of making a numbered sibling, use plm_checkpoint / '
                    f'plm_timeline')
                out["note"] = (f'{out["note"]}; {collision_note}'
                               if out.get("note") else collision_note)
            # Land the (still empty) part in the user's project folder immediately so it
            # exists as a real, openable file; add_features refreshes it as geometry grows.
            saved = _project_save(did, actual)
            if saved:
                out["saved_path"] = saved
            return _dumps(out)
        except Exception as e:  # noqa: BLE001
            return _err(e)



    @mcp.tool()
    def add_features(ops: Optional[list] = None, doc_id: Optional[str] = None,
                     background: bool = False, auto_define: bool = True,
                     **extra: Annotated[Any, Field(default=None)]) -> str:
        """Apply a list of document ops to build/edit the part (the core modeling tool).

        `ops` is a JSON array of ops from the CLOSED vocabulary (see OP VOCABULARY below
        and cad_guide()) — e.g.
          [{"op":"add_feature","feature":{"type":"box","name":"Block",
              "params":{"length":{"value":40,"expr":null},"width":{"value":30,"expr":null},
                        "height":{"value":20,"expr":null}}}},
           {"op":"add_feature","feature":{"type":"fillet","name":"Round",
              "params":{"radius":{"value":3,"expr":null}},"refs":{"edges":{"of":"edge"}}}}]
        Geometry is addressed in plain words via `refs` (e.g. {"face":{"of":"face",
        "side":"top"}}). Ops in one call may reference an earlier op in the SAME array as
        "$<index>" (0-based); existing tree features by id ("f1").

        ARGUMENT SHAPES — the canonical call is `ops=[…]`; these are accepted too, and every
        rewrite comes back in `interpreted` (prefer the canonical spelling once you know it):
          ops={"op":…}           a single op dict — read as a one-element batch
          ops="[{…}]"            the array as a JSON string (real JSON only: Python repr
                                 with single quotes/tuples is a teaching error, not a guess)
          feature= features= op= operations= actions= ops_json=
                                 the batch under another name. Two DIFFERENT batches in one
                                 call is an error — send exactly one.
          ops=[{"type":"box",…}] bare feature dicts with no "op" — read as add_feature
          {"op":"box",…}         a feature TYPE used as the op name — read as add_feature
          type=… name=… params={…} diameter=… (no ops at all)
                                 ONE feature spread over top-level kwargs; unrecognised
                                 kwargs become that feature's params
          name= identifier= part= part_name=   with NO type= in the call: the DOCUMENT to
                                 build into (an open part's name resolves to its doc_id).
                                 Supplying both a differing doc_id and one of these is an
                                 error. A feature's name always goes in feature.name.
          purpose= basis= rationale= note= …   design intent is ACCEPTED and ignored (it is
                                 not a modeling parameter) — record reasoning with
                                 ledger_note(), not here.

        DIMENSIONS — write a size in whichever of these is most natural; all are accepted:
          40                     a bare number, in DOCUMENT UNITS (millimetres)
          "40"                   the same number as a string
          {"value":40,"expr":null}   the canonical dim object (what get_part hands back)
          "40mm" "2.5cm" "1in" '2.5"' "1ft" "5 ft 6 in" "0.5m" "20thou"
                                 a number plus a unit — converted to mm for you, and the
                                 conversion is echoed back in the response's `interpreted`
          {"value":2,"unit":"in"}    the same thing with the unit in its own field
          "width/2"              an EXPRESSION, evaluated by the engine against the part's
                                 equations/globals (also {"value":0,"expr":"width/2"}).
                                 Anything that isn't purely <number><unit> is left alone and
                                 handed to the expression evaluator, so a real formula is
                                 never mangled — but a unit inside a formula ("2*10mm") is
                                 NOT understood: do the arithmetic or use one unit literal.
        DIMENSION SHORTHAND (rewritten for you, and reported in `interpreted` — prefer the
        canonical spelling once you know it):
          "diameter" (or dia/d) on cylinder/sphere -> radius, HALVED. On a cone d1/d2 -> r1/r2;
            on a torus major_diameter/minor_diameter -> major_r/minor_r.
            NOT on hole / bolt_circle: those take a REAL "diameter" param and are never halved.
          "size":[l,w,h] on a box -> length/width/height; "size":40 -> a 40 cube.
        These forms work on any add_feature params and on edit_feature params (the unit
        conversion; the per-type diameter alias needs the feature type, so it is add-only).
        A JSON-encoded {"value":N,"expr":null} dimension from a tree readback is decoded
        before evaluation too; literal text fields and ordinary expressions stay text.
        Every rewrite this tool performs comes back as `interpreted`:[…] beside the result.

        SCHEMA DEFAULTS the engine requires and you may omit (all echoed in `interpreted`):
          hole      params.style defaults to "simple" — the vocabulary is simple|tap|cbore|
                    csink, and counterbore/c-bore/countersink/csk/tapped are mapped onto it.
                    params.end defaults to "blind" when a numeric depth is present and
                    "through_all" otherwise; through/thru are mapped onto "through_all".
                    refs.face defaults to {"of":"face","side":"top"} (a bore has to be
                    entered SOMEWHERE) — a direction vector picks the face instead. World
                    [x,y,z] point(s) under origin/position/at (or [x,y,z] positions) become
                    params.world_points — points on each bore AXIS, which the engine projects
                    along refs.face's normal onto that face (refused when the axis misses
                    it); positions=[[u,v]] stay FACE-LOCAL.
          bolt_circle  a world [x,y,z] ring centre under center/at/origin becomes
                    params.world_center (same projection rule); omitted = face centre.
          sketch    missing/noncanonical entity ids become e<N>; canonical ids are retained,
                    with constraint handles remapped. Missing construction defaults to false.
                    Creation AND edit_feature replacements use the same entity/constraint
                    dialect normalizer; omitted edit fields keep their current values.
                    params.constraints defaults to [] on creation only. refs.plane accepts
                    XY/XZ/YZ/top/front/right -> plane.top/plane.front/plane.right.
                    NEW sketches and sketch_add_entity batches finish with sketch_fully_define
                    in this SAME atomic ops batch (one revision). Author relations/dimensions
                    first; it defines remaining DOF without blanket fix pins. Pass call-level
                    auto_define=False, op.auto_define:false, or feature.params.auto_define:false
                    to opt out (the feature option persists for later entity batches).
                    A fix on a rectangle/polygon/polyline pins every expanded segment;
                    other relations on a whole expanded shape are ambiguous and refuse.
                    reorder_feature accepts same-batch "$N" in both id and before.
          boolean   params.op accepts union/add/join -> "fuse", subtract/difference -> "cut",
                    intersect -> "common"; targets/bodies/operands become params.a and .b.
          extrude / revolve / loft / sweep
                    params.mode defaults to "boss"; params.sketch:"<name>" becomes
                    refs.profile=[{"kind":"region","sketch":…}]; revolve's params.axis:"Z"
                    becomes refs.axis:"axis.z", and a revolve with no angle defaults to
                    a full 360° revolution.
          transform moves its body by default; only copy:true duplicates. body names a
                    live body-owner feature id on a part, or a component id on an assembly.
                    Missing/consumed refs refuse, never target another body. rotate accepts
                    [x,y,z] plus angle or {"axis":[x,y,z],"angle":degrees}. Assembly motion
                    composes in world space with placement; mate-owned poses refuse.
          set_suppressed targets feature ids on parts and component ids on assemblies;
                    value:true suppresses, value:false restores. Wrong-kind ids refuse.
          any       feature TYPE synonyms are mapped (cube/cuboid/block -> box, cylindrical/
                    rod/tube -> cylinder, ball -> sphere, cut_<shape> -> <shape> with
                    mode:"cut", pocket -> extrude with mode:"cut"); an unknown type is
                    rejected with the closest real types named. A ref that names a feature
                    CREATED IN THIS SAME BATCH by its display name resolves to its "$N".
                    at/axis/origin take a plain [x,y,z] — a {"value":[…]} wrapper around a
                    coordinate is unwrapped, and at:"center" becomes [0,0,0].

        OP VOCABULARY (closed — do NOT invent names):
          ops (field op=…): add_feature, edit_feature, delete_feature, reorder_feature,
            set_suppressed, set_folder, rename, set_units, set_material, set_appearance,
            set_rollback, eq_set, eq_delete, sketch_add_entity, sketch_add_constraint,
            sketch_edit_entity, sketch_delete_entity, sketch_delete_constraint, sketch_set_dim,
            sketch_toggle_construction, sketch_convert, sketch_break_external_ref, sketch_trim,
            sketch_extend, sketch_offset, sketch_fillet, sketch_chamfer, sketch_mirror_entities,
            sketch_fully_define,
            sketch_pattern_entities, sketch_move, sketch_rotate, asm_insert_component,
            asm_edit_component, asm_delete_component, asm_add_mate, asm_edit_mate,
            asm_delete_mate, asm_set_explode, copy_feature, paste_feature,
            paste_feature_as_derived, sketch_paste_entities
          feature types (ONLY under op=add_feature): box, cylinder, sphere, cone, torus,
            sketch, extrude, revolve, loft, sweep, helix, fillet, chamfer, shell, draft,
            hole, bolt_circle, linear_pattern, circular_pattern, mirror, transform, boolean,
            rib, split, thread, text, import, script, plane, axis, point, csys, sketch3d, …
          WRONG: {"op":"box",…} / {"op":"sketch",…} / {"op":"extrude",…} / {"op":"cylinder",…}
          RIGHT: {"op":"add_feature","feature":{"type":"box"|"cylinder"|"sketch"|…, …}}
          rename relabels ONE FEATURE and needs its id: {"op":"rename","id":"f1","name":"Base"}.
            No op renames the part/document: name it at creation (new_part(name="…")) and
            name its file with save_part(path="<name>.step+").
          sketch_edit_entity changes the fields of ONE entity. Put the id and the changed fields
            in "entity": {"op":"sketch_edit_entity","sketch":"f1","entity":{"id":"e1","p2":[60,0]}}.
            The type cannot change. An unknown id, an empty patch or a wrong field is refused.
          A leading {"op":"new_part","name":…} (or bare {"type":"new_part",…}) is also
            accepted as a convenience: it mints the document and the rest of the batch
            builds into it, so you can create-and-build in one call. Everywhere else
            new_part is the standalone tool, not an op.
          Live list: engine doc_list_ops (sourced from core/doc/op_registry — single source).
          Call cad_guide() for worked examples and full param shapes.

        RE-POSING AN ASSEMBLY COMPONENT — asm_edit_component moves a component ONLY while
        nothing else owns its pose. It carries `transform` (row-major 4x4; at/rotate/angle
        are accepted here and converted for you), `name`, `fixed`, `suppressed`, `hidden`.
          * MATED component (not `fixed`, referenced by a live mate): its pose belongs to
            the mate solver, and the rebuild overwrites any transform you write. This call
            REFUSES with {"ok":false,"reason":"component_is_mated"} instead of reporting a
            move that did not happen. Move it by driving the mate —
            {"op":"asm_edit_mate","id":"m1","value":{"value":<mm|deg>}} then solve_mates()
            — or explore poses with simulate_motion (never mutates the doc). To pose it
            freely, suppress/delete the mate first, or set "fixed":true in the same batch.
          * ALREADY-PLACED component: the engine seeds each solve from the previous one, so
            a fresh transform can be discarded even with no mates; this call reads the pose
            back and returns {"ok":false,"reason":"transform_not_applied"} when that
            happens. Place components at insert time (asm_add_component at/rotate) or move
            them through mates.

        PRIMITIVE PLACEMENT + ORIENTATION (box/cylinder/sphere/cone/torus) — feature-level
        keys beside "type"/"name"/"params", all optional. An unrecognised key is REJECTED,
        never silently ignored, so use exactly these spellings:
          "at":[x,y,z]           where its CENTRE goes (default: centred on the origin) — so
                                 two coaxial cylinders of height h stack at "at" z = 0 and
                                 z = h, not 0 and h/2. Also spelled "center"/"centre", which
                                 mean the same thing and are rejected outright next to
                                 "centered":false (then "at" is the BASE, not the centre).
                                 May instead be a geometry query — "at":{"of":"face",
                                 "side":"top"} places it at that face's centroid.
          "at_offset":[dx,dy,dz] world-axis nudge added on top of "at".
          "axis":[x,y,z] | "+x"|"-x"|"+y"|"-y"|"+z"|"-z"   which way it POINTS: the primitive's
                                 natural local +Z is rotated onto "axis" (default "+z" = up).
                                 This is how you get a sideways cylinder/cone/box without a
                                 sketch — "centered":false then measures along the ROTATED axis.
          "centered":false       put the base/corner at "at" instead of the centre (box corner,
                                 cylinder/cone base); sphere/torus are always centred.
          "mode":"boss"|"cut"    fuse into (default) or subtract from the current body.
          "merge":false          start a SEPARATE new body instead of combining.
          An off-origin sideways boss, end to end:
          {"op":"add_feature","feature":{"type":"cylinder","name":"DrainSpout",
              "params":{"radius":{"value":25,"expr":null},"height":{"value":60,"expr":null}},
              "at":[200,0,40],"axis":[1,0,0],"centered":false,"mode":"boss"}}
          WRONG: "placement":{"origin":[…],"direction":[…]} / "boolean":"union"
          RIGHT: "at":[…], "axis":[…], "mode":"boss"
          Selectors live in the feature's OWN "refs" — a SIBLING of "params", never inside it:
          WRONG: "params":{"refs":{"profile":[…]},"depth":{…}}   (reaches no slot at all)
          RIGHT: "refs":{"profile":[…]}, "params":{"depth":{…}}
          A datum slot takes a bare reserved name: "plane.top"|"plane.front"|"plane.right",
          "axis.x"|"axis.y"|"axis.z", "origin" — no wrapper object, no centroid.

        Returns revision, body_count, volume_mm3, and per-feature status (ok / error / warning)
        so you can immediately tell a dropped cut, a failed reference, or a stray extra body.
        VERIFY LOOP (after every mutation): call render_part() or render_views() to SEE the
        result, then measure_part()/query_geometry()/inspect_feature(target) on the changed
        region, and before declaring done run verify_battery(doc_id). On a validation error
        the whole batch is rejected — read `error`, fix the ops, call again. A rebuild-failure
        response may also carry `regen_diagnosis`: an IP-18 fix-preview card (diagnosis +
        editable fix_ops + verifier verdict) — record your verdict with regen_resolve(card, action).

        SKETCH MUTATIONS: any batch that includes sketch_add_entity / sketch_add_constraint /
        sketch_set_dim / sketch_fillet / … auto-appends one readback per touched sketch under
        `sketch_readbacks[]`: `ascii_ruled` (the ruled ASCII picture, the only copy), `summary`
        (closure / dof / ordered schedule), `grid` + `index` (the cell<->world contract
        apply_ascii_edits resolves cell addresses against) and `findings`. READ that readback
        before the next sketch edit. Before declaring a sketch done, call
        render_sketch_ascii(sketch) + sketch_health(sketch) (or check_sketch) and require dof 0.

        HEAVY OPS: a costly op (a global fillet/chamfer/shell/boolean on a many-edged body) is
        run on a worker thread and AUTO-HANDED-OFF to the background if it overruns the call
        budget, so it can never block past the MCP transport timeout (which would otherwise drop
        the whole session). If that happens you get {job_id, status:"running"} — poll get_job().
        All open docs are autosaved BEFORE the op, so nothing is lost even on a hard timeout.
        Pass background=True to hand off immediately for an op you already know is heavy.
        """
        interpreted: list = []
        try:
            # FastMCP's schema builder turns a var-keyword parameter into a real (optional,
            # Annotated) field named after it, so a FastMCP call arrives carrying
            # {"extra": None}. Drop that artifact — and ONLY that artifact — before reading
            # anything the model actually wrote. The built-in agent path (_bind_tool ->
            # tool.fn) never sets it.
            if extra.get("extra", _MISSING) is None:
                extra.pop("extra", None)
            if extra.get("kwargs", _MISSING) is None:
                extra.pop("kwargs", None)
            # Accept the argument shapes models actually write (a bare feature spec, an
            # aliased batch, a JSON string, a lone document name) and rewrite them into the
            # canonical ops list. Canonical input is returned untouched with no notes.
            ops, doc_id, bg = _normalize_add_features_args(ops, doc_id, extra, interpreted)
            if bg is not None:
                background = bg
            ops = list(ops)
            # A model that nests a structure inside a STRING. Live run 2026-07-28 sent
            # {"op":"add_feature","feature":"{'type': 'cylinder', …, 'expr': None}"} —
            # a Python repr, not JSON — and every downstream pass called .get() on a str,
            # so the turn died on «'str' object has no attribute 'get'»: our crash, and a
            # message that taught the model nothing. Decode once, here, before any pass
            # touches the batch; anything that does not parse is left exactly as it came.
            ops = _decode_embedded_structures(ops, interpreted)
            # Model-robustness: `new_part` is a top-level TOOL, not a batch op — but models
            # reliably open a build batch with {"op":"new_part","name":…} (or the bare
            # {"type":"new_part",…} — a real rejected batch from the field) because that
            # reads as the natural first step. Rejecting the whole batch over it (and over
            # the bare "No current part" that follows) burns a turn for nothing, so honour
            # the intent: mint the document here and apply the remaining ops to it.
            created: Optional[str] = None
            mint_requested_name: Optional[str] = None
            mint_actual_name: Optional[str] = None
            def _is_new_part_head(el: Any) -> bool:
                if not isinstance(el, dict):
                    return False
                word = el.get("op") if el.get("op") is not None else el.get("type")
                return _slug(word) == "new_part"
            while ops and _is_new_part_head(ops[0]):
                head = ops.pop(0)
                if head.get("op") is None:
                    interpreted.append(
                        'read bare {"type":"new_part",…} as the leading {"op":"new_part"} '
                        "create — new_part is a tool/doc op, never a feature type")
                if doc_id or created:
                    continue  # a target doc already exists; the extra create is a no-op
                # Same resume-or-mint contract as the `new_part` TOOL (see its docstring
                # and core/doc/session.cpp doc_new): a batch head naming a part that is
                # ALREADY OPEN must resume it, not breed a numbered twin. This path used
                # to call doc_new with no `resume_if_open`, so it always uniquified —
                # measured live 2026-07-30: an agent created "fkey_live" with the tool,
                # then opened a later batch with {"op":"new_part","name":"fkey_live"} and
                # got a SECOND document, "fkey_live2", carrying 2 of the part's features
                # while the other 7 stayed behind. One requested part, two tabs. The
                # atomic-mint fix landed on the tool; this is its batch-op twin.
                head_name = head.get("name") or head.get("part") or "part"
                # …and its version-series twin: a batch head that bumps the version
                # ("holster_v4" while "holster_v3" is open and built) is the next
                # REVISION of one design, not a second part. Same rule as the tool, so a
                # model cannot dodge it by opening a batch with the create instead.
                head_docs = _resumable_docs()
                series = _pick_best_doc(_docs_in_version_series(head_docs, head_name))
                if series is not None:
                    created = series["doc_id"]
                    _CUR["doc_id"] = created
                    note_touched_doc(created)
                    interpreted.append(
                        f'"{head_name}" is the next VERSION of the open part '
                        f'"{series.get("name")}" (doc_id={created}, '
                        f'{series.get("feature_count")} feature(s)) — built this batch '
                        f"into it so the design stays ONE document with ONE version "
                        f"history; keep using that name")
                    continue
                # Verbatim when a part of that exact name is already open — that is the
                # engine's resume_if_open match, and it keys on the name as given (see the
                # same guard in the new_part tool).
                head_mint_name = (head_name if _docs_named(head_docs, head_name)
                                  else _strip_version_suffix(head_name))
                r = cb.call("doc_new", {"kind": "part", "resume_if_open": True,
                                        "name": head_mint_name, **_doc_new_scope()})
                created = r.get("doc_id")
                _CUR["doc_id"] = created
                note_touched_doc(created)
                if r.get("reused"):
                    # NEVER seed a fake 0.0 baseline on a part that already carries
                    # geometry — the next write-diff would report "0.0 -> X" instead of
                    # the true delta (same hazard _resumed_reply documents).
                    interpreted.append(
                        f'resumed the open part "{r.get("name") or head_name}" '
                        f'(doc_id={created}, {r.get("feature_count")} feature(s)) for this '
                        f"batch instead of minting a duplicate document")
                else:
                    _LAST_VOLUME[created] = 0.0
                    if head_mint_name != head_name:
                        mint_requested_name = head_name
                        mint_actual_name = r.get("name") or head_mint_name
            if created and not doc_id:
                doc_id = created
            d = _doc(doc_id)
            if created and not ops:
                out = {"ok": True, "doc_id": d, "created_doc": created,
                       "note": "created the part; send its build ops in the next call"}
                if mint_requested_name is not None:
                    out["requested_name"] = mint_requested_name
                    out["note"] = (
                        f'dropped the version token from "{mint_requested_name}" because '
                        f'revisions are tracked in PLM (plm_checkpoint / plm_timeline); '
                        f'this document was minted as "{mint_actual_name}", so keep '
                        f'calling it "{mint_actual_name}" from now on; send its build ops '
                        f"in the next call")
                return _dumps(out)
            # A flat face-pocket spec ({"op":"pocket","face":"top","shape":"rect",…}) is
            # two features, so it expands BEFORE the op-element pass aliases it onto a
            # bare, profile-less extrude.
            ops = _expand_face_pockets(ops, interpreted)
            # …and an extruded-polygon compound, before the vocabulary check rejects it.
            ops = _expand_prisms(ops, interpreted)
            # …and an invented `boolean_<word>` op, which is a whole boolean feature.
            ops = _expand_compound_boolean_ops(ops, interpreted)
            # Give every element an `op` (a bare feature dict is the single most common
            # rejection in the field) and map every feature TYPE onto the engine vocabulary
            # FIRST — the sketch/primitive passes below branch on the canonical type, so a
            # sketch spelled "profile" used to slip past the sketch normalizer entirely.
            ops = _normalize_op_elements(ops, interpreted)
            # A feature's real data may sit BESIDE params/refs at its own top level —
            # mirror the engine's own hoist (session.cpp:960-991) so every pass after this
            # one, including the pattern/ref-slot passes right below, sees it in params.
            ops = _hoist_feature_sibling_keys(ops, interpreted)
            # asm_edit_component reads ONLY a 16-value `transform`, so the at/rotate pose
            # dialect (asm_add_component's own signature) has to become that matrix here or
            # the op applies "ok" and moves nothing.
            ops = _normalize_asm_component_pose(ops, interpreted)
            # asm_add_mate reads only op.mate with refs [{component, selector}]; resolve
            # asm_mate's {component, query|sid} operand the same way asm_mate does.
            ops = _normalize_asm_mate_ops(ops, d, interpreted)
            # A pattern decides its own type from params.mode, so it must run BEFORE the
            # alias table maps the generic word "pattern" onto linear_pattern regardless.
            ops = _normalize_pattern_ops(ops, interpreted)
            ops = _normalize_feature_types(ops, interpreted)
            # The type is canonical only now, and the alias table is keyed by it. Runs
            # BEFORE the ref migration so a renamed SELECTOR slot (mirror's `about` ->
            # `plane`, sweep's `spine` -> `path`) still gets carried into refs below.
            ops = _normalize_type_param_aliases(ops, interpreted)
            # Geometry selectors written into `params` belong in `refs` — the
            # model had the right edges, just the wrong bag. Runs before the
            # sketch/primitive passes so they see the canonical layout.
            ops = _migrate_ref_slots(ops, interpreted)
            # An emptied selector slot ("none", []) means NO selection, which is an absent
            # slot — and a boolean word on a primitive is params.mode, not a second feature.
            ops = _drop_empty_ref_slots(ops, interpreted)
            ops = _normalize_inline_boolean(ops, interpreted)
            # An enum wrapped as a dimension ({"mode":{"value":"blind"}}) is
            # rejected as non-numeric — unwrap before the dim passes run.
            ops = _unwrap_enum_dims(ops, interpreted)
            # A profile written INLINE on the feature that consumes it is two features —
            # split it here, ahead of the sketch pass that normalizes the one we mint.
            ops = _split_inline_profile_sketches(ops, interpreted)
            # Model-robustness: accept the natural/flat sketch shape LLMs emit
            # (L1 ids, start/end, dimensions[], top-level plane) by normalizing it
            # to the engine's canonical schema before validation.
            ops = _normalize_sketch_ops(ops, interpreted, d)
            # …and the 2D EDITING ops beside it (trim/extend/offset/fillet/chamfer/
            # mirror/pattern/move/rotate), whose key names session.cpp reads verbatim and
            # whose ignored flags silently destroyed or stacked geometry.
            ops = _normalize_sketch_edit_ops(ops, interpreted)
            # Same, for the primitive placement/orientation dialect models emit
            # (placement:{origin,direction}, boolean:"union") — rewritten to the
            # canonical at/axis/mode/merge keys the engine actually accepts — for the
            # dimension dialect ("25mm", diameter, box size), and for the schema
            # requirements models forget (hole style/end/face, boolean op, extrude mode).
            ops = _normalize_feature_ops(ops, interpreted)
            # Supply the one ref the engine cannot infer (a hole's entry face).
            ops = _supply_missing_refs(ops, interpreted)
            # A bare face/edge sid in a ref slot -> the selector object the engine wants,
            # verified against THIS document (one doc_query, only when a sid is present).
            ops = _resolve_sid_refs(ops, d, interpreted)
            # Finally, a ref that names a feature THIS batch creates -> its "$N" placeholder.
            ops = _resolve_batch_name_refs(ops, interpreted)
            # …and "the sketch I just made", which no name table can carry.
            ops = _resolve_relative_profile_refs(ops, interpreted)
            # …and a name an EARLIER call created, which no batch-local table can carry.
            ops = _resolve_doc_name_refs(ops, d, interpreted)
            # Indices are final here (every insert/expand/renumber pass has run), so a
            # surviving "$N" can be range-checked against the batch the engine will see.
            _validate_batch_placeholders(ops)
            # LAST: operands are canonical only now, and this reads them.
            ops = _unmerge_boolean_operands(ops, interpreted)
            # …and the same problem for operands built by an EARLIER batch, which
            # merge:false on a new feature cannot reach — that needs an edit_feature.
            ops = _repair_merged_boolean_operands(ops, d, interpreted)
            auto_define_indices: dict = {}
            ops = _append_sketch_auto_define(
                ops, auto_define=auto_define, doc_id=d, notes=interpreted,
                index_map=auto_define_indices)
            interpreted = _interpreted_notes(interpreted)
            # A transform edit on a component the mate solver owns is a guaranteed no-op
            # (the rebuild re-solves and writes the solved pose back). Refuse before
            # mutating anything, and name the ways to actually move it.
            _repose_refusal = _asm_repose_refusal(ops, d)
            if _repose_refusal is not None:
                if interpreted:
                    _repose_refusal["interpreted"] = interpreted
                return _dumps(_repose_refusal)

            def _apply() -> dict:
                # Assemblies may retain failed features; rollback must preserve other writers.
                ckpt = None
                try:
                    ckpt = cb.call("doc_checkpoint", {"doc_id": d})
                except Exception as exc:  # noqa: BLE001
                    if getattr(exc, "code", None) == "engine_connection_lost":
                        raise
                    ckpt = None

                # GSL Stage 1a — snapshot the PRE-op topology for the write-diff (W2).
                before_hist = _face_hist(d)
                before_vol = _LAST_VOLUME.get(d)

                # Auto-preflight: dry-run on shadow clone before mutating.
                # Freedom rule: fail verdict never blocks — we proceed regardless;
                # if verdict==fail we log a preflight_overridden event after apply.
                _pf_result: dict = {}
                try:
                    from services.mcp.preflight_tools import run_auto_preflight
                    _pf_result = run_auto_preflight(d, "add_features", ops)
                except Exception as exc:  # noqa: BLE001
                    if getattr(exc, "code", None) == "engine_connection_lost":
                        raise
                    pass

                r = cb.call("doc_apply_ops", {"doc_id": d, "ops": ops})
                fs = (r.get("rebuild") or {}).get("feature_status", [])
                try:
                    _enrich_zero_volume_errors(fs, ops, r.get("id_map") or {})
                except Exception as exc:  # noqa: BLE001
                    if getattr(exc, "code", None) == "engine_connection_lost":
                        raise
                    pass
                # doc_apply_ops already returns the rebuilt tree.  Match each status against
                # that authoritative tree instead of paying for a second doc_tree RPC just to
                # tell callers which feature their batch created or changed.
                tree = r.get("rejected_tree") or r.get("tree")
                tree = tree if isinstance(tree, dict) else {}
                feature_meta = {
                    feature.get("id"): {
                        "name": feature.get("name"),
                        "type": feature.get("type"),
                        "suppressed": bool(feature.get("suppressed", False)),
                    }
                    for feature in tree.get("features", [])
                    if isinstance(feature, dict) and isinstance(feature.get("id"), str)
                }
                feats = []
                for status in fs:
                    feature = {"id": status.get("id"), "ok": status.get("ok"),
                               "error": status.get("error"), "warning": status.get("warning")}
                    meta = feature_meta.get(status.get("id"))
                    if meta:
                        feature.update(meta)
                    feats.append(feature)
                bodies = (r.get("tess") or {}).get("bodies", [])
                ok = all(s.get("ok") for s in fs) if fs else True

                # READBACK: the engine reports an asm_edit_component pose as applied even
                # when the mate-solve warm start (Session::asm_warm) overwrote it on the
                # rebuild this very call ran. `tree` IS the post-rebuild document, so the
                # requested matrix is checkable for free — never report a pose as set when
                # the document does not carry it.
                if ok:
                    rejected = _asm_transform_rejects(ops, tree)
                    if rejected:
                        _publish_current(d)
                        return {
                            "ok": False,
                            "reason": "transform_not_applied",
                            "doc_id": d,
                            "revision": r.get("revision"),
                            "components": rejected,
                            "applied": False,
                            "error": (
                                "asm_edit_component accepted the pose and the engine's "
                                "assembly rebuild discarded it: "
                                + ", ".join(str(x["component"]) for x in rejected)
                                + " still carries its previous transform (the mate solve is "
                                "seeded from the last solve's warm state, which wins over an "
                                "authored transform). Nothing moved."),
                            "hint": (
                                "place the component at insert time instead — "
                                "asm_add_component(path=…, at=…, rotate=…, angle=…) is honored "
                                "because the component is not in the warm state yet — or drive "
                                "its pose through a mate (asm_mate value / "
                                '{"op":"asm_edit_mate","id":…,"value":{"value":…}} + '
                                "solve_mates), which is the supported way to move a placed "
                                "component. Do not re-send the same transform: it will be "
                                "discarded again."),
                        }

                if not ok:
                    # PART compile rejection is atomic; other failures need a guarded rollback.
                    reverted = bool(r.get("rejected_tree"))
                    if not reverted:
                        reverted = cb.revert_own_batch(d, ckpt, r.get("revision"))
                    fail = [{"id": s.get("id"), "error": s.get("error")} for s in fs
                            if not s.get("ok")]
                    _publish_current(d)  # mirror the rolled-back (or current) state to the app
                    # The agent card renders `error` TRUNCATED (~160 chars) and never
                    # reads `failed_features`, so a constant "batch rejected" told the
                    # model nothing and it resent the identical ops. Carry the engine's
                    # actual per-feature reasons — named by feature — in `error` itself.
                    fnames = {}
                    idmap = r.get("id_map") or {}
                    for i, o in enumerate(ops):
                        f = o.get("feature") if isinstance(o, dict) else None
                        fid = idmap.get("$" + str(i))
                        if isinstance(f, dict) and f.get("name") and isinstance(fid, str) and fid:
                            fnames[fid] = str(f["name"])
                    parts = []
                    for f in fail:
                        fid = str(f.get("id") or "?")
                        why = str(f.get("error") or "").strip() or "no reason reported"
                        nm = fnames.get(f.get("id"))
                        parts.append(f"{fid} ({nm}): {why}" if nm else f"{fid}: {why}")
                    head = ("batch rejected, nothing was kept" if reverted
                            else "batch failed; failed features were left in the tree")
                    fail_result = {
                        "ok": False,
                        "doc_id": d,
                        "rolled_back": reverted,
                        "error": (head + " — " + "; ".join(parts)) if parts else head,
                        "failed_features": fail,
                        "features": feats,
                    }
                    if interpreted:
                        # The batch failed, so the rewrite is now a SUSPECT: say what we
                        # read the ops as before the model re-sends the same spelling.
                        fail_result["interpreted"] = interpreted
                    if mint_requested_name is not None:
                        fail_result["requested_name"] = mint_requested_name
                        fail_result["note"] = (
                            f'dropped the version token from "{mint_requested_name}" '
                            f'because revisions are tracked in PLM '
                            f'(plm_checkpoint / plm_timeline); this document was minted '
                            f'as "{mint_actual_name}", so keep calling it '
                            f'"{mint_actual_name}" from now on')
                    # Rebuild failure -> IP-18 regen_diagnose fix-preview card.
                    # Diagnoses the FAILED tree from the ops response (the
                    # doc itself was already rolled back above), logs the model call as an
                    # IP-18 correction trace, and attaches the accept/edit/reject card to
                    # the failure response for the copilot/caller to surface. Best-effort:
                    # any error (no provider, no tree) degrades to the plain failure dict.
                    try:
                        failed_tree = r.get("rejected_tree")
                        if failed_tree:
                            from services.mcp.regen_tools import diagnose_failed_document
                            engine_err = "; ".join(parts) or None
                            diag = diagnose_failed_document(
                                {"revision": r.get("revision", 0), "tree": failed_tree,
                                 "owner_map": r.get("owner_map") or {},
                                 "rebuild": {"feature_status": fs}},
                                engine_error=engine_err)
                            if diag.get("ok"):
                                card = dict(diag["card"])
                                # strip in-process handles (not JSON-serializable intent)
                                card.pop("_corrupted_document", None)
                                card.pop("_fixed_document", None)
                                fail_result["regen_diagnosis"] = card
                            else:
                                fail_result["regen_diagnosis"] = diag
                    except Exception:  # noqa: BLE001 — diagnosis must never mask the failure
                        pass
                    return fail_result

                out = {
                    "ok": ok,
                    "doc_id": d,
                    "revision": r.get("revision"),
                    "body_count": len(bodies),
                    "volume_mm3": r.get("volume"),
                    "features": feats,
                    "id_map": _authored_batch_id_map(r.get("id_map") or {}, auto_define_indices),
                }
                if interpreted:
                    out["interpreted"] = interpreted
                if mint_requested_name is not None:
                    out["requested_name"] = mint_requested_name
                    out["note"] = (
                        f'dropped the version token from "{mint_requested_name}" because '
                        f'revisions are tracked in PLM (plm_checkpoint / plm_timeline); '
                        f'this document was minted as "{mint_actual_name}", so keep '
                        f'calling it "{mint_actual_name}" from now on')
                # GSL Stage 1a — SEMANTIC write-confirmation (W2): say what the op actually did
                # ("added 2 faces (+2 cylinder); 2 new bore/hole(s); volume 24000→23435") or warn
                # "no geometry change" so the model isn't left diffing a bare volume number.
                # The engine's per-feature warnings and the part's overall size travel with it:
                # a warning sitting alone in features[] among a column of nulls is not something
                # a model acts on, and without a bbox "is the part the right size" costs a
                # separate measure_part call the agent demonstrably never made.
                vol_after = r.get("volume")
                if ok:
                    try:
                        bbox_after = cb.call("doc_bbox", {"doc_id": d})
                    except Exception as exc:  # noqa: BLE001
                        if getattr(exc, "code", None) == "engine_connection_lost":
                            raise
                        bbox_after = None
                    try:
                        out["changes"] = _write_diff(
                            before_hist, _face_hist(d), before_vol, vol_after,
                            warnings=[f["warning"] for f in feats if f.get("warning")],
                            bbox=bbox_after)
                    except Exception as exc:  # noqa: BLE001
                        if getattr(exc, "code", None) == "engine_connection_lost":
                            raise
                        out["changes"] = {
                            "summary": "geometry changed but write-diff unavailable",
                            "faces_delta": None,
                        }
                    if isinstance(vol_after, (int, float)):
                        _LAST_VOLUME[d] = vol_after
                    # B3 — best-effort AUTO-WARNING: if this batch added feature(s), check the
                    # new batch against the rest of the tree for boss/cut overlap or a swallowed
                    # boss (features filter keeps this bounded to new-batch x existing pairs, not
                    # a full-tree scan). Never fails the batch — degrades to no warning.
                    added_ids = [fid for i, op in enumerate(ops)
                                 if isinstance(op, dict) and op.get("op") == "add_feature"
                                 for fid in [(r.get("id_map") or {}).get(f"${i}")] if fid]
                    if added_ids:
                        try:
                            ov = cb.call("eval_feature_overlap",
                                         {"doc_id": d, "features": added_ids})
                            pairs = ov.get("pairs") or []
                            # `dead_features` means "owns no surviving face", not "failed to
                            # build".  A later feature in THIS transaction legitimately takes
                            # ownership from every upstream feature: box→chamfer and
                            # sketch→cut→chamfer are normal examples.  Only the terminal added
                            # feature can still be an honest batch-local dead/no-op warning;
                            # genuine swallowed/cut failures remain visible through `pairs`.
                            dead_all = list(ov.get("dead_features") or [])
                            terminal_added_id = added_ids[-1]
                            dead = [fid for fid in dead_all if fid == terminal_added_id]
                            if pairs or dead:
                                summary = ov.get("summary")
                                if isinstance(summary, str) and len(dead) != len(dead_all):
                                    old = f"; {len(dead_all)} dead feature(s)"
                                    new = (f"; {len(dead)} dead feature(s)" if dead else "")
                                    summary = summary.replace(old, new, 1)
                                out["overlap_warnings"] = {
                                    "summary": summary,
                                    "pairs": pairs,
                                    "dead_features": dead,
                                }
                        except Exception as exc:  # noqa: BLE001
                            if getattr(exc, "code", None) == "engine_connection_lost":
                                raise
                            pass
                        # A rib/boss that floats, overhangs, or barely touches the body reads
                        # as "ok" in feature_status; name it in this reply (no extra call).
                        try:
                            att = cb.call("eval_feature_attachment",
                                          {"doc_id": d, "features": added_ids, "budget_s": 3.0})
                            bad = att.get("features") or []
                            if att.get("ok") is False and bad:
                                out["attachment_warnings"] = {
                                    "summary": att.get("summary"),
                                    "features": [{k: e.get(k) for k in
                                                  ("feature_id", "verdict", "detail", "location")}
                                                 for e in bad],
                                    "next": (f'render_part(target="{bad[0]["feature_id"]}") — '
                                             "perspective close-up of the flagged feature; fix "
                                             "its sketch plane/profile, then re-check"),
                                }
                        except Exception as exc:  # noqa: BLE001
                            if getattr(exc, "code", None) == "engine_connection_lost":
                                raise
                # Push the updated part to the desktop app's viewport (if it's open) so the user
                # watches it build. Best-effort; only when something actually got built.
                if ok and bodies:
                    pub = _publish_current(d)
                    if pub:
                        out["shown_in_app"] = True
                    _autosave(d)  # persist so a server restart can recover this doc
                # Refresh the deliverable .step+ in the user's project folder so the file on
                # disk tracks the latest revision (no-op unless the workspace is configured).
                if ok:
                    saved = _project_save(d)
                    if saved:
                        out["saved_path"] = saved
                # Attach preflight advisory items and log override event.
                if _pf_result:
                    adv_items = _pf_result.get("advisory_items") or []
                    if adv_items:
                        out["preflight_advisory"] = adv_items
                    pf_verdict = _pf_result.get("verdict", "skipped")
                    if pf_verdict == "fail":
                        # Applied despite failed preflight — log audit trigger (§11).
                        try:
                            from services.mcp.preflight_tools import log_preflight_override
                            log_preflight_override(d, "add_features", _pf_result)
                        except Exception:  # noqa: BLE001
                            pass
                # Auto-readback for 2D sketch mutations (compact ASCII + NL summary).
                # Best-effort: never fails the batch if the live sketch load is unavailable.
                if ok:
                    try:
                        from services.mcp.ascii2d import (  # noqa: PLC0415
                            sketch_ids_from_ops, live_sketch_readback, sketch_define_additions,
                        )
                        sids = sketch_ids_from_ops(ops)
                        # Also pick up newly-created sketch features referenced as $N
                        # once id_map has resolved them for sketch-bearing add_feature.
                        id_map = r.get("id_map") or {}
                        for i, op in enumerate(ops or []):
                            if not isinstance(op, dict):
                                continue
                            if op.get("op") != "add_feature":
                                continue
                            feat = op.get("feature") or {}
                            if (feat.get("type") or "").lower() != "sketch":
                                continue
                            fid = id_map.get(f"${i}")
                            if isinstance(fid, str) and fid not in sids:
                                sids.append(fid)
                        if sids:
                            readbacks = []
                            for sid in sids[:4]:
                                rb = live_sketch_readback(d, sid)
                                if rb:
                                    readbacks.append(rb)
                            if readbacks:
                                out["sketch_readbacks"] = readbacks
                                # Say each thing ONCE. Live 2026-07-30: this block also
                                # set out["ascii_readback"] = readbacks[0] (a full 7.6k
                                # clone of the object already in sketch_readbacks),
                                # out["ascii"] + out["ascii_block"] (two more copies of
                                # the same drawing) and out["sketch_summary"] (a second
                                # copy of the 3k summary) — 13k of a 25.9k reply was
                                # duplicate bytes. The readback lives at exactly one
                                # path now, and the hint names it.
                                out["readback_hint"] = (
                                    "sketch_readbacks[i] carries the ruled ASCII picture "
                                    "(ascii_ruled), the constraint summary, grid/index for "
                                    "cell-addressed edits, and findings. "
                                    + str(readbacks[0].get("hint") or ""))
                            by_sketch = {rb["sketch_id"]: rb for rb in readbacks}
                            definitions = []
                            for index, op in enumerate(ops):
                                if op.get("op") != "sketch_fully_define":
                                    continue
                                sid = id_map.get(op.get("sketch"), op.get("sketch"))
                                feature = next((row for row in tree.get("features") or []
                                                if row.get("id") == sid), {})
                                constraints = (feature.get("params") or {}).get("constraints") or []
                                added = sketch_define_additions(id_map, index, constraints)
                                rb = by_sketch.get(sid)
                                health = rb if rb is not None else cb.call(
                                    "sketch_health", {"doc_id": d, "sketch": sid})
                                definition = {"sketch": sid, **added, "dof": health.get("dof")}
                                definitions.append(definition)
                                if rb is not None:
                                    rb.setdefault("definition", []).append(definition)
                            if definitions:
                                out["sketch_definitions"] = definitions
                    except Exception as exc:  # noqa: BLE001
                        if getattr(exc, "code", None) == "engine_connection_lost":
                            raise
                        pass
                return out

            if background:
                jid = _start_job("add_features", _apply)
                out = {"ok": True, "doc_id": d, "job_id": jid, "status": "running",
                       "created_doc": created,
                       "note": f"applying {len(ops)} op(s) in the background — "
                               f"poll get_job('{jid}')."}
                if mint_requested_name is not None:
                    out["requested_name"] = mint_requested_name
                    out["note"] = (
                        f'dropped the version token from "{mint_requested_name}" because '
                        f'revisions are tracked in PLM (plm_checkpoint / plm_timeline); '
                        f'this document was minted as "{mint_actual_name}", so keep '
                        f'calling it "{mint_actual_name}" from now on; applying '
                        f"{len(ops)} op(s) in the background — poll get_job('{jid}').")
                return _dumps(out)
            # Run on a worker thread with auto-handoff: a fast batch returns inline exactly as
            # before; a heavy one that overruns the budget keeps running and returns a job_id so
            # the transport call returns in time and the session survives.
            raw = _solve_sync_or_handoff("add_features", _apply)
            if isinstance(raw, dict) and raw.get("code") == "engine_connection_lost":
                if interpreted:
                    raw["interpreted"] = interpreted
                return _dumps(raw)
            if created and isinstance(raw, dict):
                raw["created_doc"] = created
            if (mint_requested_name is not None and isinstance(raw, dict)
                    and "requested_name" not in raw):
                raw["requested_name"] = mint_requested_name
                normalized_note = (
                    f'dropped the version token from "{mint_requested_name}" because '
                    f'revisions are tracked in PLM (plm_checkpoint / plm_timeline); '
                    f'this document was minted as "{mint_actual_name}", so keep '
                    f'calling it "{mint_actual_name}" from now on')
                if raw.get("note"):
                    raw["note"] = normalized_note + "; " + str(raw["note"])
                else:
                    raw["note"] = normalized_note
            # _solve_sync_or_handoff CATCHES an engine throw and hands it back as
            # {"ok":false,"error":…}, so the outer `except` never sees a rejected batch. Echo
            # the rewrites here too: a validation rejection is exactly when the model needs to
            # know how its arguments were read before it re-sends the same spelling.
            if (isinstance(raw, dict) and not raw.get("ok") and interpreted
                    and "interpreted" not in raw):
                raw["interpreted"] = interpreted
            # mega-8 F7: enrich thin unknown-op errors with closest + cad_guide
            # (C++ path sometimes returns only "unknown op 'cylinder'").
            try:
                from services.mcp import op_vocab as _ov  # noqa: PLC0415
                raw = _ov.enrich_unknown_op_error(raw)
            except Exception:  # noqa: BLE001
                pass
            # A ref that never RESOLVED (no such edge/face, a query matching zero/many, a
            # prose phrase the normalizer declined to guess at) is exactly when the model
            # is flying blind: attach a real, capped inventory of what the document
            # actually has so it can act instead of guessing again (live root cause,
            # 2026-07-28 — see _enrich_ref_resolution_error).
            try:
                raw = _enrich_ref_resolution_error(raw, d, ops)
            except Exception:  # noqa: BLE001
                pass
            return _dumps(raw) if not isinstance(raw, str) else raw
        except Exception as e:  # noqa: BLE001
            # A rejected batch is exactly when the model most needs to see how its arguments
            # were read: an interpretation it did not expect is the first thing to check
            # before it re-sends the identical spelling.
            raw_err = _err(e)
            notes = _interpreted_notes(interpreted)
            if not notes:
                return raw_err
            try:
                obj = json.loads(raw_err)
                obj["interpreted"] = notes
                return _dumps(obj)
            except Exception:  # noqa: BLE001
                return raw_err



    @mcp.tool()
    def get_part(doc_id: Optional[str] = None, summary: bool = False) -> str:
        """Return the feature tree (features, parameters, ids) + revision for editing.

        Both forms share one layout: {ok, doc_id, revision, tree:{features:[...]}}, the same
        `tree` every engine read returns, so result["tree"]["features"] is the feature list
        whichever form you asked for.

        summary=True returns a LIGHTWEIGHT tree — feature id/name/type/suppressed + parameters,
        with heavy embedded geometry (the import features' base64 BREP blobs) and any oversized
        string value ELIDED, plus summary:true and feature_count. Use it on big
        assemblies/imported parts where the full tree blows the tool token limit (a 600k-char
        part lists in a few KB). Every value keeps its type. A list with more than 64 items
        keeps only its first 16 items. Its feature then gets
        truncated_lists:{"params.entities": {"kept": 16, "total": 312}}, keyed by the path
        in the feature. Do not send a summary params value back in an edit. Read the full
        form for complete lists. The full (default) form carries the complete tree verbatim
        for round-trip editing, plus owner_map (feature id -> owned faces/edges).
        """
        try:
            d = _doc(doc_id)
            r = cb.call("doc_tree", {"doc_id": d})
            if not summary:
                return _dumps({"ok": True, "doc_id": d, **r})

            def _trim(value: Any, path: str, truncated: dict) -> Any:
                # Elide base64 geometry blobs / very long strings that bloat the tree but carry
                # no design intent the caller would read in a summary. A long list stays a
                # list (c243: the old "<list of N elided>" string was iterated as characters);
                # its cut is recorded in `truncated` under the value's path in the feature.
                if isinstance(value, str) and len(value) > 200:
                    return f"<{len(value)} chars elided>"
                if isinstance(value, dict):
                    return {k: ("<elided>" if k == "embedded_brep"
                                else _trim(v, f"{path}.{k}", truncated))
                            for k, v in value.items()}
                if isinstance(value, list):
                    if len(value) <= _SUMMARY_LIST_MAX:
                        return [_trim(v, f"{path}[{i}]", truncated) for i, v in enumerate(value)]
                    truncated[path] = {"kept": _SUMMARY_LIST_KEEP, "total": len(value)}
                    return [_trim(v, f"{path}[{i}]", truncated)
                            for i, v in enumerate(value[:_SUMMARY_LIST_KEEP])]
                return value

            tree = r.get("tree") or {}
            feats = tree.get("features") or []
            out_feats = []
            for f in feats:
                if not isinstance(f, dict):
                    continue
                truncated: dict = {}
                params = _trim(f.get("params") or {}, "params", truncated)
                entry = {
                    "id": f.get("id"),
                    "name": f.get("name"),
                    "type": f.get("type"),
                    "suppressed": bool(f.get("suppressed", False)),
                    "params": params,
                }
                if truncated:
                    entry["truncated_lists"] = truncated
                out_feats.append(entry)
            # Same nesting as the full form: a caller that read one form reads the other
            # (2026-10-10: summary reads taught `["features"]`, then a full read raised
            # KeyError; c237 hit the reverse with `["tree"]`).
            return _dumps({"ok": True, "doc_id": d, "revision": r.get("revision"),
                           "summary": True, "feature_count": len(out_feats),
                           "tree": {"features": out_feats}})
        except Exception as e:  # noqa: BLE001
            return _err(e)






    @mcp.tool()
    def set_material(material: Union[str, dict, list],
                     body: Optional[Union[str, int]] = None,
                     doc_id: Optional[str] = None,
                     faces: Optional[list[Union[str, dict]]] = None) -> str:
        """Set physical material identity and rendered appearance for a part or body.

        A material NAME resolves through the engineering material registry. With body=,
        its key is saved in body_materials for only that body; the document material
        remains the default for other bodies. Without body=, the whole part receives
        the material and previous body overrides are cleared. An explicit appearance
        dict or RGB list changes visuals only, preserving physical material identity.

        material: registry name/designation (A36, 4140, 6061-T6, PETG, ...) or an explicit
                  appearance {"color":[r,g,b], "metallic"?:0..1, "roughness"?:0..1}.
        body: "b1", "b2", ... or ordinal; default all bodies.
        measure_part (and the Mass Properties panel) weigh each body with its applied
        material; FEA calls still take their own material input for the solve.
        The native tree exposes both the document default and body_materials overrides.

        faces: nonempty face SID strings (e.g. "F123abc") or complete face selector
        dicts from query_geometry, scoped to the required body. Face assignment accepts
        ONLY an explicit appearance dict/RGB and is visual-only. A SID must be unique;
        a complete fingerprint can distinguish faces sharing a SID by exact geometry.
        Ordinal hints and partial/fuzzy fingerprints cannot disambiguate a face.
        {"color": None} with a full selector clears only that exact override; a SID
        clears all stored overrides bearing that SID, including stale records.
        Stored face_appearances are exposed by get_part; stale fingerprints never paint
        a different face after a geometry edit. Assembly finishes belong in source parts.
        """
        try:
            d = _doc(doc_id)
            if faces is not None:
                if body is None or not isinstance(faces, list) or not faces:
                    raise ValueError("set_material: faces requires a body and nonempty explicit list")
                if not isinstance(material, (dict, list)):
                    raise ValueError("set_material: faces accepts explicit appearance dict/RGB only, not a material name")
            clear_faces = (faces is not None and isinstance(material, dict)
                           and "color" in material and material["color"] is None)
            ap = {"color": None} if clear_faces else _resolve_appearance(material)
            material_key = None
            registry_material = False
            unsupported_identity = None
            if isinstance(material, str) and material.strip():
                for candidate in _material_candidates(material):
                    resolved = cb.call("material_resolve_key", {"name": candidate})
                    material_key = resolved.get("result")
                    if isinstance(material_key, dict):
                        material_key = material_key.get("key")
                    if material_key is not None:
                        material_key = str(material_key).strip() or None
                    if material_key:
                        registry_material = True
                        break
                if not material_key and _material_not_modelled(material):
                    # The registry lacks solve properties for this known designation (e.g.
                    # 4140), but the document can and should still preserve the user's exact
                    # engineering identity instead of silently retaining default 6061.
                    material_key = _material_norm(material).replace(" ", "_")
                    unsupported_identity = material_key
            # Resolve the live body inventory before accepting a selected-body identity.
            info = cb.call("asm_world_bodies", {"doc_id": d})
            bodies = info.get("bodies", [])
            if body is None:
                n = len(bodies) or 1
                targets = [f"b{i + 1}" for i in range(n)]
            elif isinstance(body, int):
                targets = [f"b{body}"]
            else:
                targets = [str(body)]
            if body is not None and not clear_faces and targets[0] not in {
                    f"b{i + 1}" for i in range(len(bodies))}:
                raise ValueError(f"set_material: body {targets[0]!r} is not present in this part")
            ops = []
            if material_key:
                identity = {"op": "set_material", "key": material_key}
                if body is not None:
                    identity["id"] = targets[0]
                ops.append(identity)
            ops.extend({"op": "set_appearance", "id": key, **ap,
                        **({"faces": faces} if faces is not None else {})} for key in targets)
            r = cb.call("doc_apply_ops", {"doc_id": d, "ops": ops})
            out = {"ok": True, "doc_id": d, "bodies": targets,
                   "appearance": ap, "material_key": material_key,
                   "engineering_material_set": bool(material_key),
                   "material_registry_resolved": registry_material,
                   "revision": r.get("revision")}
            if faces is not None:
                out["faces"] = faces
            if unsupported_identity:
                out["warning"] = (
                    f"stored engineering material identity {unsupported_identity!r}, but the "
                    "registry has no solve-property row for it; analysis needs an explicit "
                    "material property dict")
            elif isinstance(material, str) and not material_key:
                out["warning"] = (
                    f"{material!r} has a visual preset but no engineering registry identity; "
                    "appearance changed, document material metadata did not")
            return _dumps(out)
        except Exception as e:  # noqa: BLE001
            return _err(e)



    @mcp.tool()
    def undo(doc_id: Optional[str] = None) -> str:
        """Undo the last edit on the current part.

        Multi-user note: from mcp/agent connections the daemon UNCONDITIONALLY denies
        this (code ``undo_denied``) — there is no agent-side way around it, and
        retrying will fail identically. The refusal below is caught and expanded into
        full recovery guidance; the callable tools are ``add_features(ops=[{"op":
        "delete_feature"|"edit_feature", ...}])`` to remove/alter a feature you just
        added, or ``plm_timeline()`` / ``plm_rollback(to="-1")`` to roll the whole
        document back. ``doc_checkpoint``/``doc_revert`` are engine-internal RPCs with
        no tool binding — do not try to call them.
        Set env ``CHUDWARE_UNDO_AGENTS=1`` only as a deliberate kill-switch.

        VERIFY LOOP: after undo, call render_part()/render_views() (and inspect_feature on the
        affected region) to confirm the rolled-back state, then verify_battery before done.
        """
        try:
            d = _doc(doc_id)
            r = cb.call("doc_undo", {"doc_id": d})
            _publish_current(d)
            return _dumps(r)
        except Exception as e:  # noqa: BLE001
            return _err(e)



    @mcp.tool()
    def redo(doc_id: Optional[str] = None) -> str:
        """Redo the last undone edit on the current part.

        Multi-user note: from mcp/agent connections the daemon UNCONDITIONALLY denies
        this (code ``undo_denied``) — there is no agent-side way around it, and
        retrying will fail identically. The refusal below is caught and expanded into
        full recovery guidance; the callable tools are ``add_features(ops=[{"op":
        "delete_feature"|"edit_feature", ...}])`` to remove/alter a feature you just
        added, or ``plm_timeline()`` / ``plm_rollback(to="-1")`` to roll the whole
        document back. ``doc_checkpoint``/``doc_revert`` are engine-internal RPCs with
        no tool binding — do not try to call them.
        Set env ``CHUDWARE_UNDO_AGENTS=1`` only as a deliberate kill-switch.

        VERIFY LOOP: after redo, call render_part()/render_views() to SEE the restored state
        and verify_battery before declaring done.
        """
        try:
            d = _doc(doc_id)
            r = cb.call("doc_redo", {"doc_id": d})
            _publish_current(d)
            return _dumps(r)
        except Exception as e:  # noqa: BLE001
            return _err(e)



    @mcp.tool()
    def show_in_app(doc_id: Optional[str] = None) -> str:
        """Make sure the OPEN Chudware desktop app can show the part to the user.

        Daemon mode (the default): the app shares the engine's documents live — every open
        part is mirrored in its own tab and redrawn after each change, so there is nothing to
        push. The reply is {ok, doc_id, name, revision, live: true}. It does not switch the
        user's active tab: the app reveals an agent part's tab by itself, waits while the user
        is mid-edit, and keeps a tab the user closed closed.
        Legacy in-process engine (CHUDWARE_ENGINE_DAEMON=0): writes the part to the handoff
        folder the app loads — shown whenever the app next opens if it is not running now.
        """
        try:
            d = _doc(doc_id)
            if cb.active_daemon_mode():
                # The GUI mirrors daemon docs from hostd's doc.opened/doc.changed events and
                # ignores handoff files, so _publish_current deliberately does nothing here;
                # reporting that as "could not publish" failed a doc the user could see (c247).
                entry = next((e for e in (cb.call("doc_list", {}) or {}).get("docs", [])
                              if isinstance(e, dict) and e.get("doc_id") == d), None)
                if entry is None:
                    return _dumps({"ok": False, "doc_id": d,
                                   "error": f"{d} is not open in the shared engine"})
                return _dumps({"ok": True, "doc_id": d, "name": entry.get("name"),
                               "revision": entry.get("revision"), "live": True,
                               "note": "The desktop app mirrors this document live; "
                                       "no publish is needed."})
            pub = _publish_current(d)
            if not pub:
                return _dumps({"ok": False, "error": "could not publish (no geometry yet?)"})
            return _dumps({"ok": True, "doc_id": d, **pub})
        except Exception as e:  # noqa: BLE001
            return _err(e)



    @mcp.tool()
    def list_parts() -> str:
        """List all open part/drawing documents in this engine session."""
        try:
            return _dumps(cb.call("doc_list", {}))
        except Exception as e:  # noqa: BLE001
            return _err(e)



    @mcp.tool()
    def close_part(doc_id: Optional[str] = None, discard: bool = False) -> str:
        """Close a part document and free it. Clears the current part if it was current.

        SAVE DISCIPLINE: docs you (the AI) create are AI-owned. The desktop app never warns
        the user about YOUR unsaved docs — so YOU are responsible for saving deliverables
        before closing. If the doc has unsaved changes this returns {"ok": false,
        "code": "unsaved_changes"} WITHOUT closing; call save_part(path=...) to deliver it,
        or close_part(discard=True) to intentionally drop scratch work.

        If another client (e.g. the user, who adopted this doc by editing it) is still
        editing, the daemon degrades the close to a detach and returns {"detached": true} —
        your live view drops but their doc lives on.
        """
        try:
            d = _doc(doc_id)
            if not discard:
                # "Undelivered work" = mutated since the last save_part/export_part. Revision is
                # the reliable signal (the engine `dirty` flag is cleared by the MCP autosave's
                # doc_save to a temp path, so it would false-negative here).
                name = d
                for e in cb.call("doc_list", {}).get("docs", []):
                    if isinstance(e, dict) and e.get("doc_id") == d:
                        name = e.get("name") or d
                        break
                rev = _doc_revision(d)
                delivered = _LAST_DELIVER_REV.get(d)
                if rev is not None and rev > 0 and delivered != rev:
                    return _dumps({
                        "ok": False, "code": "unsaved_changes", "doc_id": d, "dirty": True,
                        "message": f"{name} has unsaved changes — call save_part(path=...) "
                                   f"to deliver it, or close_part(discard=True) to drop it",
                    })
            r = cb.call("doc_close", {"doc_id": d})
            # A completed close (including daemon detach while another client keeps the
            # document alive) ends this process's ownership. Drop every per-doc cache now;
            # the unsaved-changes refusal above returns before reaching this point.
            _LAST_VOLUME.pop(d, None)
            _PROCESS_DOCS.discard(d)
            if d in _TOUCHED_DOCS:
                _TOUCHED_DOCS.remove(d)
            if r.get("detached"):
                # Another client (or this agent run itself) keeps it alive: our view
                # detached, the doc lives on. An explicit discard must survive that — the
                # terminal save sweep would otherwise write the dropped state back over its
                # file (live 2026-09-24: a stale Wheel End overwrote the rebuilt one).
                if discard:
                    note_discarded_doc(d)
                _unpublish(d)
                if _CUR["doc_id"] == d:
                    _CUR["doc_id"] = None
                _LAST_DELIVER_REV.pop(d, None)
                return _dumps({"ok": True, "detached": True, "doc_id": d,
                               "note": r.get("note")})
            _unpublish(d)  # drop its mirror tab in the app
            if _CUR["doc_id"] == d:
                _CUR["doc_id"] = None
            # Prune recovery aliases that resolve to this now-closed doc. The bridge
            # outlives daemon restarts (which reset the doc-id counter), so a stale
            # old_id -> d alias could later misroute an agent's call to a REUSED d.
            for k in [k for k in _DOC_ALIASES if k == d or _resolve_alias(k) == d]:
                _DOC_ALIASES.pop(k, None)
            _LAST_DELIVER_REV.pop(d, None)
            return _dumps({"ok": True, "closed": r.get("closed", True), "doc_id": d})
        except Exception as e:  # noqa: BLE001
            return _err(e)



    @mcp.tool()
    def inspect_native_files(paths: list[str], output_path: Optional[str] = None) -> str:
        """Inspect native .step+ metadata WITHOUT opening a document or evaluating geometry.

        paths: absolute native file paths inside CHUDWARE_FS_ROOT. Returns native schema,
        document name/id, canonical document_hash (FNV, NOT SHA256), feature ids/labels/types,
        component ids/names/sources/transforms/fixed flags and literal/resolved dependencies.
        Unsupported dependency formats and per-file errors are reported, never guessed.
        Dependencies are metadata only: their files are not imported, compiled or solved.

        For large inventories supply output_path: a NEW absolute .json report path inside
        the project root, with an existing parent. Persists the FULL native result and
        returns counts, bounded failure previews and output_path. Existing files are never
        overwritten. Without output_path, replies above 32 KiB become an explicit summary;
        repeat with output_path or smaller batches for full metadata. Native CAD is read-only.
        """
        try:
            if not isinstance(paths, list) or not paths:
                raise ValueError("paths must be a non-empty list of absolute .step+ paths")
            guarded = [_native_file_path(p, f"paths[{i}]", native=True)
                       for i, p in enumerate(paths)]
            report = _native_report_path(output_path) if output_path is not None else None
            result = cb.call("doc_file_operations", {"action": "inspect", "paths": guarded})
            return _native_file_reply(result, report)
        except Exception as exc:  # noqa: BLE001
            return _native_file_error(exc, "inspect")

    @mcp.tool()
    def read_native_file(path: str, sections: Optional[list[str]] = None,
                         feature_ids: Optional[list[str]] = None,
                         component_ids: Optional[list[str]] = None,
                         include_params: bool = True) -> str:
        """Read a saved .step+ file's feature tree, components, mates, materials and metadata WITHOUT opening it.

        Path may be absolute or relative to CHUDWARE_FS_ROOT, but must remain inside it.
        Choose sections or feature_ids/component_ids to narrow the reply. sections:
        features, components, mates, explode, material, body_materials, appearances,
        face_appearances, meta, equations, configurations, rollback_after, studies. Large strings
        (including embedded BREP) are omitted; the saved file is never modified.
        Each component carries its stored `source` and `source_resolved`, the absolute file
        path. To insert the same file into another assembly, use `source_resolved`.
        """
        return _native_read_file(path, sections, feature_ids, component_ids, include_params)

    @mcp.tool()
    def relocate_native_files(plan_path: str) -> str:
        """Copy native .step+ files with metadata-only renames/rebased references; NEVER solve.

        plan_path: absolute, project-jailed UTF-8 JSON file (at most 16 MiB), containing
        {"entries":[{"path":"/old.step+","destination":"/new.step+","name":"Human name",
                     "component_names":{"component_id":"Human component name"}}],
         "path_map":{"/old/dependency.step+":"/new/dependency.step+"}}.
        name/component_names are optional; path_map may be empty. All source, destination,
        and map key/value paths must be absolute and inside CHUDWARE_FS_ROOT. The wrapper
        supplies action="relocate". Do not include feature edits or feature-name renames.

        Native preflight checks the WHOLE plan for malformed maps/collisions/overwrites
        before writing any CAD. Original files remain untouched. Each copy changes ONLY
        document/component display names and declared file-reference strings: resolve old
        relative references at the old parent, apply path_map, then rebase at the new
        parent, including unmapped dependencies. IDs, feature parameters except file refs,
        materials/finishes, mates, reference frames, poses and embedded model data survive;
        stale optional display/BREP caches are invalidated, not re-provenanced.
        Unsupported dependency forms are surfaced rather than guessed. No geometry
        compilation, assembly solve, flattening, or Python native-container editing occurs.

        I/O failures can leave honest partial copies; inspect failures before retrying.
        The full native outcome is saved to a unique .result-<id>.json beside the plan;
        reply contains counts, bounded failures and output_path. A report-write failure
        preserves native_ok/counts and must NOT be treated as proof nothing was copied.
        """
        try:
            source = _native_file_path(plan_path, "plan_path")
            if not Path(source).is_file():
                raise ValueError("plan_path must name an existing regular JSON file")
            with open(source, "rb") as stream:
                data = stream.read(_NATIVE_FILE_PLAN_LIMIT + 1)
            if len(data) > _NATIVE_FILE_PLAN_LIMIT:
                raise ValueError("relocation plan exceeds 16 MiB")
            plan = json.loads(data.decode("utf-8"), object_pairs_hook=_native_plan_object)
            if not isinstance(plan, dict) or set(plan) != {"entries", "path_map"}:
                raise ValueError("plan must contain exactly entries and path_map")
            if not isinstance(plan["entries"], list) or not plan["entries"]:
                raise ValueError("entries must be a non-empty list")
            if not isinstance(plan["path_map"], dict):
                raise ValueError("path_map must be an object of absolute paths")
            entries = []
            for i, entry in enumerate(plan["entries"]):
                if (not isinstance(entry, dict) or not {"path", "destination"} <= set(entry)
                        or set(entry) - {"path", "destination", "name", "component_names"}):
                    raise ValueError(f"entries[{i}] requires path/destination; only name/"
                                     "component_names are optional")
                if "name" in entry and not isinstance(entry["name"], str):
                    raise ValueError(f"entries[{i}].name must be a string")
                names = entry.get("component_names", {})
                if not isinstance(names, dict) or any(
                        not isinstance(v, str) for v in names.values()):
                    raise ValueError(f"entries[{i}].component_names must map ids to strings")
                source_path = _native_file_path(
                    entry["path"], f"entries[{i}].path", native=True)
                destination = _native_file_path(
                    entry["destination"], f"entries[{i}].destination", native=True)
                if os.path.lexists(entry["destination"]):
                    raise FileExistsError(
                        f"entries[{i}].destination already exists; no files were copied")
                entries.append({**entry, "path": source_path, "destination": destination})
            path_map = {}
            for old, new in plan["path_map"].items():
                guarded_old = _native_file_path(old, "path_map key")
                guarded_new = _native_file_path(new, "path_map value")
                if guarded_old in path_map:
                    raise ValueError("path_map contains duplicate normalized source paths")
                path_map[guarded_old] = guarded_new
            report = _native_report_path(str(
                Path(source).with_name(f"native-relocation.result-{uuid4().hex}.json")))
            result = cb.call("doc_file_operations", {
                "action": "relocate", "entries": entries, "path_map": path_map,
            })
            return _native_file_reply(result, report)
        except Exception as exc:  # noqa: BLE001
            return _native_file_error(exc, "relocate")

    @mcp.tool()
    def save_part(path: Optional[str] = None, doc_id: Optional[str] = None,
                  allow_empty: bool = False) -> str:
        """Save the part as Chudware's NATIVE .step+ — the editable, reopenable format. PREFER THIS.

        .step+ is the only format that round-trips the full PARAMETRIC feature tree (so the user
        can keep editing parameters/features), its SIMULATION STUDIES (FEA/CFD setups + results),
        and any associated 2D drawings — all in one file the desktop app opens fully editable. Use
        this to deliver a part the user will keep working on. (export_part writes flat interchange
        or mesh files: a plain STEP/STL has geometry only, no feature history or studies.)

        path : where to write. With a configured project, omitting it saves an established
               native file in place;
               a detached native import retains that behavior after authored design edits,
               but study-only scratch receives an unused project filename. Other new docs
               use CHUDWARE_PROJECT_SAVE_DIR and the document name; only external/headless
               clients without a project folder fall back to a temp .step+.
               A ".step+" extension is added when absent; another CAD extension is REPLACED,
               so save_part("bracket.step") writes "bracket.step+", never
               "bracket.step.step+". Returns the written path and bytes, plus a note when
               the requested extension was rewritten. An explicit path may save a scratch
               import back to its source, subject to the user-tab protection below.

        Explicit paths are destinations, not design names: save_part(path="arm_revD_final5.step+")
        writes that exact requested filename (subject only to path canonicalization and the
        native .step+ extension rule above). Version-token normalization applies only when
        MINTING a new document, not to saved files or already-open documents. Saving never
        renames or removes a user-authored revision file; an explicit destination can retire
        only a stale copy this harness itself placed for the SAME document. To revise an
        existing design, open/edit that document and save it in place; an explicitly chosen
        different path is not silently merged with or redirected to the series base.

        On an assembly (or any doc with 2+ world bodies) the reply carries non-blocking
        `warnings:["placement_unverified: N component(s) placed by transform with no
        check_interference since last mutation"]`: the save happened, but N component poses
        asserted via asm_add_component(at=/rotate=) have not been re-validated since the last
        mutation. Run check_interference plus measure_between / list_world_bodies against the
        expected envelope, then save again.

        Save every deliverable BEFORE your session ends: the user is never warned about your
        unsaved docs, so anything you don't save (and don't close_part(discard=True)) is only
        recoverable from a fallback crash-recovery tier — not a delivery mechanism.
        Empty documents (no bodies AND no studies) are refused without writing;
        allow_empty=True explicitly saves one. A document whose only content is
        engineering studies (hydraulic_circuit, piping_network, vessel, duct_system, …)
        is not empty: it saves without allow_empty.
        A file the user has open in a tab is refused too (`path_open_in_user_tab`, naming the
        open doc_id): edit that document in place so the user sees the change live, then save
        it — never rebuild it in a second document and save that over its file.
        """
        try:
            d = _doc(doc_id)
            if not allow_empty and not cb.call(
                    "asm_world_bodies", {"doc_id": d, "explode": False}).get("bodies") \
                    and not (cb.call("sim_study_list", {"doc_id": d}) or {}).get("studies"):
                return _dumps({"ok": False, "reason": "no_bodies_empty_document",
                               "doc_id": d,
                               "content": "Save refused: document has 0 bodies and no studies."})
            # K3: canonicalise the caller-supplied path so any '..' traversal sequences are
            # fully resolved before we pass them to the engine.  This means the caller sees
            # exactly where the file will be written (no hidden navigation).
            out = (_sanitize_output_path(path) if path
                   else _default_deliverable_path(d, ".step+"))
            open_docs = [e for e in (cb.call("doc_list", {}) or {}).get("docs", [])
                         if isinstance(e, dict)]
            prior_path = next((e.get("path") for e in open_docs if e.get("doc_id") == d), None)
            note = None
            if not out.lower().endswith(".step+"):
                # A caller who wrote "bracket.step" asked for a CAD file by name and gets the
                # native format regardless — but blindly APPENDING produced "bracket.step.step+"
                # (measured live 2026-07-29; the user has a lexus_f_keychain.step.step+ from a
                # real session). A doubled extension is confusing in a file browser and makes the
                # part look like two nested formats. So: REPLACE a recognised CAD extension,
                # APPEND when there is none. A dotted base name that is not a known extension
                # ("v1.2_bracket") is left intact by construction, because only CAD_EXTS match.
                stem, dot, ext = out.rpartition(".")
                if dot and stem and ext.lower() in _CAD_EXTS_FOR_SAVE:
                    out = f"{stem}.step+"
                    note = (f"requested extension '.{ext}' replaced with '.step+' — save_part "
                            f"always writes the native editable format (use export_part for a "
                            f"flat .{ext.lower()})")
                else:
                    out += ".step+"
            # One design is one document: the file the USER has open in a tab is edited in
            # place, never replaced from a second document. Overwriting it from another doc left
            # the user's tab showing the old model (no revision of THAT doc ever changed, so
            # nothing reached the viewport) and a later save of the tab would silently put the
            # old model back over the agent's file (2026-09-27: a new "sixaxis_arm_revD"
            # assembly was saved over sixaxis_arm.step+ while the user watched rev C for 80 min).
            holder = user_doc_open_at(open_docs, out, d)
            if holder is not None:
                return _dumps({
                    "ok": False, "reason": "path_open_in_user_tab", "doc_id": d,
                    "path": out, "open_doc_id": holder.get("doc_id"),
                    "open_doc_name": holder.get("name"),
                    "content": (
                        f"Save refused: {os.path.basename(out)} is open in the user's tab as "
                        f"{holder.get('doc_id')}. Apply your changes to {holder.get('doc_id')} "
                        f"in place (add_features / asm_* with doc_id={holder.get('doc_id')}) so "
                        "the user sees them live, then save_part(doc_id="
                        f"{holder.get('doc_id')}); or save this document under a new name.")})
            r = cb.call("doc_save", {"doc_id": d, "path": out})
            written = r.get("path", out)
            ok = os.path.exists(written)
            retired = None
            if ok:
                # Delivered: record the revision so close_part won't warn until further edits.
                rev = _doc_revision(d)
                if rev is not None:
                    _LAST_DELIVER_REV[d] = rev
                if path:
                    # An explicit destination supersedes a copy the harness placed itself —
                    # only the file THIS doc lived in until now (doc ids are reused across
                    # engine restarts, so the record alone never authorizes a delete).
                    retired = retire_harness_copy(d, written, prior_path)
                elif prior_path != written:
                    note_harness_saved_path(d, written)
            res = {"ok": ok, "format": "step+", "native": True, "doc_id": d,
                   "path": written,
                   "bytes": (os.path.getsize(written) if ok else 0)}
            if retired:
                res["removed_stale_copy"] = retired
            if note:
                res["note"] = note
            # Non-blocking placement provenance (assembly-merge discipline): a
            # transform-placed component nobody re-checked is how a merge ships off-frame.
            warnings = placement_warnings(d)
            if warnings:
                res["warnings"] = warnings
            return _dumps(res)
        except Exception as e:  # noqa: BLE001
            return _err(e)


    @mcp.tool()
    def get_job(job_id: str) -> str:
        """Poll a background job started by run_cfd_flow/run_fea_*(background=True).

        Returns {status: "running"|"done"|"error"}; when done, the solve `result` is included.
        Polling a long solve this way never trips the per-call transport timeout that would
        otherwise drop the whole session.
        For durable simulation jobs, progress.probe contains the live fill counters.
        progress.last_advance_s is time since counters or phase last changed, not
        time since a heartbeat. progress.stalled is true only while running with
        more than 120 seconds without advancement. Cancel for lack of progress
        only when stalled is true, not for a healthy long mesh probe.
        """
        # The process-local mapping lookup is the only work allowed under _JOB_LOCK.
        # Durable lookup can touch SQLite; a DONE snapshot can additionally wait for a
        # cross-process collect claim, call the engine, and persist a study.
        with _JOB_LOCK:
            thread_job = _JOBS.get(job_id)
            snap = dict(thread_job) if thread_job is not None else None
        if snap is None and isinstance(job_id, str) and job_id.startswith(
                _DURABLE_JOB_PREFIX):
            durable_job = durable_job_snapshot(job_id)
            snap = dict(durable_job) if durable_job is not None else None
        if snap is None:
            return _dumps({"ok": False, "error": f"unknown job_id {job_id!r}"})
        out = {"ok": True, "job_id": job_id, "status": snap["status"], "kind": snap["kind"]}
        if snap.get("progress"):
            progress = dict(snap["progress"])  # retain the final phase on done/error, too
            # LBM reports progress via a file the engine writes each checkpoint — fold it in so the
            # built-in CFD path gets an ETA-style readout like OpenFOAM (report footnote).
            pf = progress.pop("progress_file", None)
            if pf:
                try:
                    with open(pf, encoding="utf-8") as fh:
                        progress.update(json.load(fh))
                except (OSError, ValueError):
                    pass  # not written yet / mid-write — keep the seed
            out["progress"] = progress
        if snap["status"] == "done":
            out["result"] = snap["result"]
        elif snap["status"] == "error":
            out["ok"] = False
            out["error"] = snap["error"]
            out.update({key: snap[key] for key in ("code", "detail") if key in snap})
        return _dumps(out)



    @mcp.tool()
    def wait_job(job_id: str, timeout_s: float = 120.0, poll_s: float = 2.0,
                 cancel=None, wake=None) -> str:
        """Block until a background job (get_job id) finishes or timeout_s passes — use instead of sleep/poll loops.

        Waiting does not cancel the underlying job. A host-supplied cancel callable
        interrupts only this wait; call wait_job again to retrieve its eventual result.
        A host-supplied wake callable (the user sent a message) ends the wait early with
        woken_by set: read the message first, then decide whether the job still matters.
        timeout_s is clamped to 0..900 s; still_running:true means the job is still going.

        Call wait_job as its OWN tool call, not from eval code. An eval cell's timeout counts
        time spent inside tool calls (and the runtime may clamp the timeout you ask for), so
        a long wait interrupts the cell before the job finishes. The finished result stays
        retrievable (get_job(job_id)), so read it in a short eval afterwards.
        """
        timeout_s = max(0.0, min(900.0, timeout_s))
        poll_s = max(0.5, min(30.0, poll_s))
        start = time.monotonic()
        deadline = start + timeout_s
        next_poll = start
        while True:
            now = time.monotonic()
            if now >= next_poll:
                snapshot = json.loads(get_job(job_id))
                if "status" not in snapshot or snapshot["status"] != "running":
                    return _dumps({**snapshot, "waited_s": time.monotonic() - start})
                next_poll = time.monotonic() + poll_s
            if cancel is not None and cancel():
                snapshot = json.loads(get_job(job_id))
                return _dumps({**snapshot, "waited_s": time.monotonic() - start,
                               "cancelled_wait": True})
            woken_by = wake() if wake is not None else None
            if woken_by:
                snapshot = json.loads(get_job(job_id))
                result = {**snapshot, "waited_s": time.monotonic() - start,
                          "woken_by": woken_by}
                if snapshot.get("status") == "running":
                    result.update(
                        still_running=True,
                        hint=(f"Wait ended early because the user sent a message; the job "
                              f"keeps running. Read the message, then call "
                              f"wait_job('{job_id}') again only if its result still matters."))
                return _dumps(result)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                snapshot = json.loads(get_job(job_id))
                result = {**snapshot, "waited_s": time.monotonic() - start}
                if snapshot.get("status") == "running":
                    result.update(still_running=True,
                                  hint=f"Call wait_job('{job_id}') again for the result.")
                return _dumps(result)
            time.sleep(min(remaining, max(0.0, next_poll - time.monotonic()), 0.1))

    @mcp.tool()
    def recover_parts(all: bool = False, limit: int = 0) -> str:
        """Reopen auto-saved parts after a server restart (recover a dropped session).

        Every successful add_features auto-persists the part to a recoverable .step+; if the
        server was restarted (e.g. a client timeout killed it) the in-memory docs are gone — call
        this to reload them (each gets a fresh doc_id). list_parts() then shows them.

        By default this recovers ONLY the most-recent prior session's parts (the one that was
        dropped) — not the entire autosave history, so you don't drag in unrelated old docs.
        Pass all=True to reach every autosaved part. In daemon mode this also reopens parts the
        daemon retired (unclosed docs from ended sessions — they were saved and closed instead of
        lingering forever; nothing reopens them except this call or the GUI's recovery prompt).
        Each entry returns its fresh `doc_id` plus the saved `name` and `original_doc_id` so you
        can re-map old handles. Stale autosaves (older than CHUDWARE_AUTOSAVE_MAX_AGE_DAYS,
        default 7) are pruned automatically.

        limit: at most this many docs are reopened, NEWEST FIRST (0 = the default bound,
        CHUDWARE_RECOVER_MAX_DOCS, 8). Recovery is BOUNDED because a big autosave pile made
        this tool a footgun: one real session held 270 snapshots and all=True reached 1,803,
        and once the engine holds hundreds of similarly-named parts no later call can resolve
        "the part". `skipped` reports how many were left ON DISK (nothing is deleted) — raise
        `limit` to reach them, or name a specific part after a first small recovery.
        """
        try:
            recovered = recover_autosaved(scope="all" if all else "recent",
                                          include_orphaned=True, limit=limit)
            skipped = recover_skipped()
            out = {"ok": True, "recovered": recovered, "count": len(recovered),
                   "scope": "all" if all else "recent", "skipped": skipped}
            if skipped:
                out["note"] = (f"{skipped} older autosaved doc(s) were NOT reopened (they are "
                               "still on disk). Reopening them all leaves no way to identify "
                               "'the part'. Call recover_parts(limit=N) if you need more.")
            return _dumps(out)
        except Exception as e:  # noqa: BLE001
            return _err(e)



    # ---------------------------------------------------------------- configurations

    @mcp.tool()
    def list_configurations(doc_id: Optional[str] = None) -> str:
        """List the part's configurations (parametric variants) and which one is active.

        A configuration is a named variant of the SAME feature tree with its own parameter
        overrides and feature suppressions — e.g. "Long" / "Short" of one bracket, or a family of
        sizes. Returns {configurations:[names], active, detail:[{name, param_overrides,
        suppressions}]}. A fresh part has just the implicit "Default". Build variants with
        add_configuration + set_config_override / set_config_suppression, switch with
        activate_configuration; measure_part/render_part then reflect the active variant.
        """
        try:
            return _dumps(cb.call("config_list", {"doc_id": _doc(doc_id)}))
        except Exception as e:  # noqa: BLE001
            return _err(e)



    @mcp.tool()
    def add_configuration(name: str, copy_from: Optional[str] = None,
                          doc_id: Optional[str] = None) -> str:
        """Add a new configuration (a named parametric variant) to the current part.

        name      : the new configuration's name (must be unique).
        copy_from : an existing configuration name to seed its overrides/suppressions from
                    (omit to start empty = identical to the base part). Then tailor it with
                    set_config_override / set_config_suppression, and activate_configuration to
                    make it current. The new config is created but NOT activated.
        """
        try:
            d = _doc(doc_id)
            r = cb.call("config_add", {"doc_id": d, "name": name, "copy_from": copy_from})
            _publish_current(d)
            return _dumps({"ok": True, "doc_id": d, "added": name,
                           "active": (r.get("tree") or {}).get("active_configuration"),
                           "configurations": [c.get("name") for c in
                                              (r.get("tree") or {}).get("configurations", [])]})
        except Exception as e:  # noqa: BLE001
            return _err(e)



    @mcp.tool()
    def activate_configuration(name: str, doc_id: Optional[str] = None) -> str:
        """Switch the active configuration. The part rebuilds to that variant's parameters.

        After this, measure_part / render_part / export_part reflect the chosen variant. Returns
        the new active configuration name and the rebuilt body count/volume so you can confirm the
        switch took (e.g. a suppressed feature drops a body, an override changes the volume).
        """
        try:
            d = _doc(doc_id)
            r = cb.call("config_activate", {"doc_id": d, "name": name})
            bodies = (r.get("tess") or {}).get("bodies", [])
            _publish_current(d)
            _autosave(d)
            return _dumps({"ok": True, "doc_id": d, "active": name,
                           "body_count": len(bodies),
                           "revision": r.get("revision")})
        except Exception as e:  # noqa: BLE001
            return _err(e)



    @mcp.tool()
    def set_config_override(config: str, feature_id: str, param: str,
                            value: Optional[Union[float, list]] = None, clear: bool = False,
                            doc_id: Optional[str] = None) -> str:
        """Override one feature parameter in a configuration (the parametric-variant edit).

        config     : the configuration name to edit (see list_configurations).
        feature_id : the feature's id from get_part (e.g. "f1").
        param      : the parameter name on that feature (e.g. "length", "radius", "translate").
        value      : the override value for this configuration only — a plain NUMBER for a scalar
                     param (length/radius/angle…) OR a LIST for a vector param, e.g.
                     translate:[x,y,z] on a `transform` feature (so one assembly file can hold a
                     Default and a STOWED config that differ only in a placement vector).
        clear      : pass True (and omit value) to REMOVE the override so the config falls back to
                     the base value.
        The base/Default config and other configurations are untouched. Activate the configuration
        (or it may already be active) and measure_part to see the effect.
        """
        try:
            d = _doc(doc_id)
            params: dict = {"doc_id": d, "name": config, "feature_id": feature_id,
                            "param": param}
            if clear or value is None:
                params["clear"] = True
            elif isinstance(value, (list, tuple)):
                # Vector/array param (e.g. transform.translate): the engine reads it as a raw
                # array (opt_vec3), so the override must be the bare list — NOT wrapped in a
                # {value, expr} scalar object. Coerce numeric components to float.
                params["value"] = [float(x) if isinstance(x, (int, float)) else x for x in value]
            else:
                # Scalar params are {value, expr} objects in the engine; wrap a bare number.
                params["value"] = {"value": float(value), "expr": None}
            cb.call("config_set_override", params)
            _publish_current(d)
            _autosave(d)
            echoed = None
            if not (clear or value is None):
                echoed = (params["value"] if isinstance(value, (list, tuple)) else float(value))
            return _dumps({"ok": True, "doc_id": d, "config": config,
                           "feature_id": feature_id, "param": param,
                           "value": echoed,
                           "cleared": bool(clear or value is None)})
        except Exception as e:  # noqa: BLE001
            return _err(e)



    @mcp.tool()
    def set_config_suppression(config: str, feature_id: str, suppressed: bool = True,
                               doc_id: Optional[str] = None) -> str:
        """Suppress (or un-suppress) a feature in a configuration — turn a feature ON/OFF per variant.

        config     : the configuration name (see list_configurations).
        feature_id : the feature id from get_part (e.g. "f3" — a fillet, a hole, a boss).
        suppressed : True to remove that feature in this configuration; False to bring it back.
        Lets one variant include a feature (a mounting boss, a chamfer) that another omits, without
        forking the feature tree. Activate the configuration + render_part to confirm.
        """
        try:
            d = _doc(doc_id)
            cb.call("config_set_suppression", {"doc_id": d, "name": config,
                                               "feature_id": feature_id,
                                               "suppressed": bool(suppressed)})
            _publish_current(d)
            _autosave(d)
            return _dumps({"ok": True, "doc_id": d, "config": config,
                           "feature_id": feature_id, "suppressed": bool(suppressed)})
        except Exception as e:  # noqa: BLE001
            return _err(e)
