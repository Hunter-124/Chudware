"""Curve feature compilers — helix / spiral (feature_params.md §Sketched features > helix).

``helix`` is a CURVE / reference feature: it produces a wire (a build123d ``Helix`` for the
constant-pitch case, or a sampled B-spline for the variable-pitch / flat-spiral cases) and is
consumed downstream by a ``sweep`` as its path. It does NOT add a solid body.

Consumption contract (so sweep can use a helix feature as ``refs.path``)
------------------------------------------------------------------------
``sweep`` resolves a path of the form ``{"kind":"region","sketch":<fid>}`` by looking up
``ctx.datums["_sketch_regions"][fid]`` and lifting that entry's ``all_edges`` through its
``plane`` (``plane.location * edge``). The sketch compiler populates that store; we mirror it
exactly: helix registers its own feature id into ``_sketch_regions`` with

    {"all_edges": [<helix/spiral edges in the base local frame>],
     "plane": <base plane>, "faces": [], "descs": []}

so a downstream ``sweep`` whose ``refs.path`` is ``{"kind":"region","sketch":<helix_fid>}``
consumes the helix/spiral as its spine with no change to sweep.py.

Parameters (feature_params.md §helix)
-------------------------------------
Constant-pitch helix (existing, unchanged):
``pitch`` (5) · ``height`` (20) · ``turns`` (null → height/pitch) · ``taper`` (0,
degrees → cone angle) · ``start_angle`` (0, degrees, about the helix axis) · ``ccw`` (true).
When ``turns`` is given it drives the height (``height = turns * pitch``) so the wire carries
exactly that many revolutions.

SolidWorks-parity additions
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
* **Variable pitch** — ``pitch_table``: a per-region table whose rows define pitch (and,
  optionally, diameter) at a given turn count, e.g.::

      "pitch_table": [
          {"turn": 0, "pitch": 5,  "dia": 16},
          {"turn": 3, "pitch": 5,  "dia": 16},
          {"turn": 6, "pitch": 10, "dia": 24},
      ]

  (rows may also be bare ``[turn, pitch, dia]`` / ``[turn, pitch]`` lists, and each scalar may
  be a ``{value, expr}`` dim). The pitch is treated as piecewise-LINEAR in turn between rows;
  the local height contribution of a turn is its pitch, so the helix's total height is the
  INTEGRAL of pitch over the turn range. Diameter (hence radius) is linearly interpolated the
  same way. The curve is built as a B-spline sampled densely along the turn parameter. Constant
  ``start_angle`` / ``ccw`` still apply; ``taper`` is ignored for variable pitch (the table's
  diameter column already governs radius — documented approximation).

* **Flat spiral** (Archimedean) — ``spiral: true`` (or ``shape: "spiral"``): a PLANAR spiral in
  the base plane (no height), ``r = start_radius + (pitch / 2π)·θ``. Driven by ``pitch`` (radial
  growth per turn) and ``turns`` (revolution count); ``start_radius`` (default = ``radius``)
  seeds the inner radius. Used for spiral springs / scrolls. ``height`` / ``taper`` are ignored.

``refs.base`` (default ``plane.top``) positions/orients the helix axis: a datum plane / csys
gives the origin + axis (its +Z) and, when it is a circle-edge / cylinder-face model selector,
its radius. Standalone helices (no pickable circle) fall back to a ``radius`` param (default
10) — see the report's contract-deviation note.
"""

from __future__ import annotations

import math
from typing import Any

from build123d import Axis, Edge, Helix, Plane, Vector, Wire

from .base import CompileContext, CompileOutput, Datum, FeatureError, register
from engine.doc.selectors import describe_edges, describe_faces, resolve


_DEFAULT_RADIUS = 10.0

# Samples per turn for the variable-pitch / spiral B-spline. Dense enough that the spline
# tracks the analytic curve to well under typical CAD tolerance; cheap to evaluate.
_SAMPLES_PER_TURN = 48


def _resolve_base(ref: Any, ctx: CompileContext) -> tuple[Plane, float | None]:
    """Resolve ``refs.base`` to a (plane, radius_or_None).

    - datum plane / csys → (its plane, None) : origin + axis come from the datum, radius unknown.
    - model circle-edge selector → (plane on the circle, circle radius).
    - model cylinder-face selector → (plane at the face centre along its axis, cylinder radius).
    """
    if ref is None:
        ref = "plane.top"

    # Datum plane / csys (reserved string id or {"kind":"datum",...}).
    if isinstance(ref, str) or (isinstance(ref, dict) and ref.get("kind") == "datum"):
        d = ctx.datum(ref)
        if d.kind in ("plane", "csys"):
            return d.obj, None
        if d.kind == "point":
            return Plane(origin=Vector(d.obj)), None
        raise FeatureError(f"helix: datum base '{ref}' (kind {d.kind}) is not a plane")

    bodies = ctx.bodies
    if not bodies:
        raise FeatureError("helix: geometry base ref but no bodies exist")
    body = bodies[-1]
    try:
        bb = body.bounding_box()
        diag = max(((bb.max.X - bb.min.X) ** 2 + (bb.max.Y - bb.min.Y) ** 2
                    + (bb.max.Z - bb.min.Z) ** 2) ** 0.5, 1e-3)
    except Exception:  # noqa: BLE001
        diag = 100.0

    if isinstance(ref, dict) and ref.get("kind") == "edge":
        rr = resolve(ref, describe_edges(body.wrapped), diag)
        if rr.index is None:
            raise FeatureError(f"helix: base edge dangling — {rr.dangling_reason}",
                               dangling_refs=[f"base: {rr.dangling_reason}"])
        edge = body.edges()[rr.index]
        try:
            radius = float(edge.radius)
            center = Vector(edge.arc_center)
            axis = Vector(edge.normal())
        except Exception as exc:  # noqa: BLE001
            raise FeatureError(f"helix: base edge is not a circle: {exc}")
        return Plane(origin=center, z_dir=axis), radius

    if isinstance(ref, dict) and ref.get("kind") == "face":
        rr = resolve(ref, describe_faces(body.wrapped), diag)
        if rr.index is None:
            raise FeatureError(f"helix: base face dangling — {rr.dangling_reason}",
                               dangling_refs=[f"base: {rr.dangling_reason}"])
        face = body.faces()[rr.index]
        try:
            surf = face.geom_adaptor()
            radius = float(surf.Cylinder().Radius())
            ax = surf.Cylinder().Axis()
            loc = ax.Location()
            center = Vector(loc.X(), loc.Y(), loc.Z())
            axdir = ax.Direction()
            axis = Vector(axdir.X(), axdir.Y(), axdir.Z())
        except Exception as exc:  # noqa: BLE001
            raise FeatureError(f"helix: base face is not a cylinder: {exc}")
        return Plane(origin=center, z_dir=axis), radius

    raise FeatureError(f"helix: unsupported base reference {ref!r}")


# --------------------------------------------------------------------------- dim helpers


def _scalar(ctx: CompileContext, v: Any) -> float:
    """Resolve one scalar that may be a ``{value, expr}`` dim, a bare number, or numeric str."""
    if isinstance(v, dict):
        val = v.get("value")
    else:
        val = v
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        raise FeatureError(f"helix: expected a numeric value, got {v!r}")
    return float(val)


def _parse_pitch_table(table: Any, ctx: CompileContext,
                       default_radius: float) -> list[tuple[float, float, float]]:
    """Normalise ``pitch_table`` rows into a sorted ``[(turn, pitch, radius), …]`` list.

    Each row may be a mapping (``{"turn", "pitch", "dia"|"diameter"|"radius"}``) or a bare
    ``[turn, pitch, dia?]`` sequence. ``dia`` is a DIAMETER (SolidWorks convention); when a row
    omits it the running radius carries forward (first row falls back to ``default_radius``).
    Scalars may be ``{value, expr}`` dims. The result is sorted by turn and de-duplicated.
    """
    if not isinstance(table, (list, tuple)) or not table:
        raise FeatureError("helix: pitch_table must be a non-empty list of rows")

    rows: list[tuple[float, float, float | None]] = []
    for i, row in enumerate(table):
        if isinstance(row, dict):
            if "turn" not in row or "pitch" not in row:
                raise FeatureError(
                    f"helix: pitch_table row {i} needs 'turn' and 'pitch' keys; got {row!r}")
            turn = _scalar(ctx, row["turn"])
            pitch = _scalar(ctx, row["pitch"])
            dia_src: Any = None
            for k in ("dia", "diameter"):
                if k in row and row[k] is not None:
                    dia_src = ("dia", row[k])
                    break
            if dia_src is None and row.get("radius") is not None:
                dia_src = ("radius", row["radius"])
            if dia_src is None:
                rad: float | None = None
            elif dia_src[0] == "dia":
                rad = _scalar(ctx, dia_src[1]) / 2.0
            else:
                rad = _scalar(ctx, dia_src[1])
        elif isinstance(row, (list, tuple)):
            if len(row) < 2:
                raise FeatureError(
                    f"helix: pitch_table row {i} needs at least [turn, pitch]; got {row!r}")
            turn = _scalar(ctx, row[0])
            pitch = _scalar(ctx, row[1])
            rad = (_scalar(ctx, row[2]) / 2.0) if len(row) >= 3 and row[2] is not None else None
        else:
            raise FeatureError(f"helix: pitch_table row {i} must be a mapping or list; got {row!r}")

        if pitch <= 0.0:
            raise FeatureError(f"helix: pitch_table row {i} pitch must be > 0; got {pitch}")
        if turn < 0.0:
            raise FeatureError(f"helix: pitch_table row {i} turn must be >= 0; got {turn}")
        rows.append((turn, pitch, rad))

    rows.sort(key=lambda r: r[0])
    # Collapse exact-duplicate turn keys to the last row given for that turn (deterministic).
    deduped: list[tuple[float, float, float | None]] = []
    for r in rows:
        if deduped and abs(deduped[-1][0] - r[0]) < 1e-12:
            deduped[-1] = r
        else:
            deduped.append(r)
    rows = deduped

    if len(rows) < 2:
        raise FeatureError("helix: pitch_table needs at least two distinct turn rows "
                           "to define a varying region")
    if rows[0][0] > 1e-9:
        # Anchor the table at turn 0 with the first row's pitch/radius so integration starts at 0.
        rows.insert(0, (0.0, rows[0][1], rows[0][2]))

    # Fill missing radii. First explicit radius (if any) seeds the leading rows; otherwise the
    # whole table falls back to default_radius. Then forward-fill across the remaining rows.
    first_explicit = next((rad for (_, _, rad) in rows if rad is not None), None)
    seed = first_explicit if first_explicit is not None else default_radius
    out: list[tuple[float, float, float]] = []
    running = seed
    for turn, pitch, rad in rows:
        if rad is not None:
            if rad <= 0.0:
                raise FeatureError(f"helix: pitch_table radius/diameter must be > 0; got {rad}")
            running = rad
        out.append((turn, pitch, running))
    return out


def _interp_pitch(turn: float, rows: list[tuple[float, float, float]]) -> float:
    """Piecewise-linear pitch at ``turn`` (clamped to the table's turn range)."""
    if turn <= rows[0][0]:
        return rows[0][1]
    if turn >= rows[-1][0]:
        return rows[-1][1]
    for (t0, p0, _), (t1, p1, _) in zip(rows, rows[1:]):
        if t0 <= turn <= t1:
            if t1 - t0 < 1e-12:
                return p1
            f = (turn - t0) / (t1 - t0)
            return p0 + f * (p1 - p0)
    return rows[-1][1]


def _interp_radius(turn: float, rows: list[tuple[float, float, float]]) -> float:
    """Piecewise-linear radius at ``turn`` (clamped to the table's turn range)."""
    if turn <= rows[0][0]:
        return rows[0][2]
    if turn >= rows[-1][0]:
        return rows[-1][2]
    for (t0, _, r0), (t1, _, r1) in zip(rows, rows[1:]):
        if t0 <= turn <= t1:
            if t1 - t0 < 1e-12:
                return r1
            f = (turn - t0) / (t1 - t0)
            return r0 + f * (r1 - r0)
    return rows[-1][2]


def _integrate_height(turn: float, rows: list[tuple[float, float, float]]) -> float:
    """Cumulative axial height at ``turn`` = ∫ pitch(t) dt over t∈[turn0, turn].

    Pitch is piecewise-linear, so the integral over each segment is the trapezoid area
    ``0.5·(p0+p1)·(t1-t0)`` (in turn units → directly axial length since pitch is per-turn).
    """
    if turn <= rows[0][0]:
        return 0.0
    z = 0.0
    for (t0, p0, _), (t1, p1, _) in zip(rows, rows[1:]):
        if turn <= t0:
            break
        seg_end = min(turn, t1)
        if seg_end <= t0:
            continue
        span = seg_end - t0
        # pitch at seg_end via linear interp on this segment.
        if t1 - t0 < 1e-12:
            p_end = p1
        else:
            p_end = p0 + (seg_end - t0) / (t1 - t0) * (p1 - p0)
        z += 0.5 * (p0 + p_end) * span
    # If turn extends past the last row, extend with the final (constant) pitch.
    if turn > rows[-1][0]:
        z += rows[-1][1] * (turn - rows[-1][0])
    return z


# --------------------------------------------------------------------------- builders


def _build_variable_pitch(rows: list[tuple[float, float, float]], total_turns: float,
                          start_angle: float, ccw: bool) -> tuple[Wire, float]:
    """Build a variable-pitch helix as a sampled B-spline in the base local frame.

    Returns ``(wire, total_height)``. The helix winds about local +Z; ``r(θ)`` and the axial
    advance follow the pitch/diameter table. ``ccw`` flips the angular direction (handedness);
    ``start_angle`` (degrees) offsets the starting angular position.
    """
    sign = 1.0 if ccw else -1.0
    a0 = math.radians(start_angle)
    n = max(2, int(math.ceil(total_turns * _SAMPLES_PER_TURN)))
    pts: list[tuple[float, float, float]] = []
    for i in range(n + 1):
        turn = total_turns * i / n
        ang = a0 + sign * 2.0 * math.pi * turn
        r = _interp_radius(turn, rows)
        z = _integrate_height(turn, rows)
        pts.append((r * math.cos(ang), r * math.sin(ang), z))

    try:
        edge = Edge.make_spline(pts)
        wire = Wire([edge])
    except Exception as exc:  # noqa: BLE001
        raise FeatureError(f"helix: could not build variable-pitch curve: {exc}",
                           hint="check pitch_table rows define positive pitch/diameter")
    total_height = _integrate_height(total_turns, rows)
    return wire, total_height


def _build_flat_spiral(start_radius: float, pitch: float, turns: float,
                       start_angle: float, ccw: bool) -> tuple[Wire, float]:
    """Build a planar Archimedean spiral ``r = start_radius + (pitch/2π)·θ`` in local z=0.

    Returns ``(wire, outer_radius)``. ``pitch`` is the radial growth per full turn; ``turns`` the
    revolution count. The spiral lies in the base XY plane (local z = 0), so after the sweep lift
    through ``base_plane.location`` it lies in the base plane. ``ccw`` flips winding direction;
    ``start_angle`` offsets the start.
    """
    if turns <= 0.0:
        raise FeatureError("helix: spiral turns must be > 0")
    sign = 1.0 if ccw else -1.0
    a0 = math.radians(start_angle)
    b = pitch / (2.0 * math.pi)            # radial growth per radian
    total_theta = 2.0 * math.pi * turns
    n = max(2, int(math.ceil(turns * _SAMPLES_PER_TURN)))
    pts: list[tuple[float, float, float]] = []
    for i in range(n + 1):
        theta = total_theta * i / n
        r = start_radius + b * theta
        ang = a0 + sign * theta
        pts.append((r * math.cos(ang), r * math.sin(ang), 0.0))

    try:
        edge = Edge.make_spline(pts)
        wire = Wire([edge])
    except Exception as exc:  # noqa: BLE001
        raise FeatureError(f"helix: could not build flat spiral: {exc}",
                           hint="check pitch/turns/start_radius are positive")
    outer_radius = start_radius + b * total_theta
    return wire, outer_radius


@register("helix")
def compile_helix(feat: dict, ctx: CompileContext) -> CompileOutput:
    p = feat.get("params", {})
    refs = feat.get("refs", {})
    fid = feat.get("id", "?")

    start_angle = ctx.dim(p, "start_angle", 0.0)  # degrees, about the helix axis
    ccw = bool(p.get("ccw", True))

    base_plane, base_radius = _resolve_base(refs.get("base"), ctx)

    # Mode selection. A pitch_table → variable pitch; spiral:true / shape=="spiral" → flat spiral;
    # otherwise the existing constant-pitch helix.
    pitch_table = p.get("pitch_table")
    is_spiral = bool(p.get("spiral", False)) or (str(p.get("shape", "")).lower() == "spiral")

    # ------------------------------------------------------------------ variable pitch
    if pitch_table is not None:
        default_radius = base_radius if base_radius is not None \
            else ctx.dim(p, "radius", _DEFAULT_RADIUS)
        rows = _parse_pitch_table(pitch_table, ctx, default_radius)
        total_turns = rows[-1][0]
        if total_turns <= 0.0:
            raise FeatureError("helix: pitch_table must span a positive number of turns")
        wire, total_height = _build_variable_pitch(rows, total_turns, start_angle, ccw)

        edges: list[Edge] = list(wire.edges())
        if not edges:
            raise FeatureError("helix: variable-pitch produced no curve geometry")
        _register_path(ctx, fid, base_plane, edges)
        return CompileOutput(
            bodies=list(ctx.bodies),
            tool_body=wire,
            op=None,
            resolved={"mode": 1.0, "turns": total_turns, "height": total_height,
                      "pitch_start": rows[0][1], "pitch_end": rows[-1][1],
                      "radius_start": rows[0][2], "radius_end": rows[-1][2]},
        )

    # ------------------------------------------------------------------ flat spiral
    if is_spiral:
        pitch = ctx.dim(p, "pitch", 5.0)
        if pitch <= 0.0:
            raise FeatureError("helix: spiral pitch (radial growth/turn) must be > 0")
        turns = ctx.dim(p, "turns", 0.0)
        if turns <= 0.0:
            raise FeatureError("helix: spiral needs turns > 0")
        if base_radius is not None:
            start_radius = base_radius
        else:
            # start_radius defaults to the radius param (inner seed radius).
            start_radius = ctx.dim(p, "start_radius", ctx.dim(p, "radius", _DEFAULT_RADIUS))
        if start_radius < 0.0:
            raise FeatureError("helix: spiral start_radius must be >= 0")
        wire, outer_radius = _build_flat_spiral(start_radius, pitch, turns, start_angle, ccw)

        edges = list(wire.edges())
        if not edges:
            raise FeatureError("helix: flat spiral produced no curve geometry")
        _register_path(ctx, fid, base_plane, edges)
        return CompileOutput(
            bodies=list(ctx.bodies),
            tool_body=wire,
            op=None,
            resolved={"mode": 2.0, "turns": turns, "height": 0.0,
                      "pitch": pitch, "start_radius": start_radius,
                      "outer_radius": outer_radius},
        )

    # ------------------------------------------------------------------ constant pitch (existing)
    pitch = ctx.dim(p, "pitch", 5.0)
    if pitch <= 0.0:
        raise FeatureError("helix: pitch must be > 0")
    height = ctx.dim(p, "height", 20.0)
    taper = ctx.dim(p, "taper", 0.0)            # degrees → cone_angle

    # turns: null → height/pitch; given → drives the height so the wire has that many turns.
    turns_param = p.get("turns")
    if turns_param is not None:
        turns = ctx.dim(p, "turns", 0.0)
        if turns <= 0.0:
            raise FeatureError("helix: turns must be > 0")
        height = turns * pitch
    else:
        if height <= 0.0:
            raise FeatureError("helix: height must be > 0")
        turns = height / pitch

    radius = base_radius if base_radius is not None else ctx.dim(p, "radius", _DEFAULT_RADIUS)
    if radius <= 0.0:
        raise FeatureError("helix: radius must be > 0")

    # Build the helix in the base LOCAL frame (axis +Z at the local origin); sweep lifts it
    # through base_plane.location, so storing local edges keeps placement consistent with the
    # sketch-region path contract. ccw (right-handed default) → lefthand=False.
    try:
        wire = Helix(
            pitch=pitch,
            height=height,
            radius=radius,
            center=(0.0, 0.0, 0.0),
            direction=(0.0, 0.0, 1.0),
            cone_angle=taper,
            lefthand=(not ccw),
        )
    except Exception as exc:  # noqa: BLE001
        raise FeatureError(f"helix: could not build helix: {exc}",
                           hint="check pitch/height/turns/radius are positive and finite")

    # start_angle: rotate the local helix about its own axis (Z).
    if abs(start_angle) > 1e-9:
        try:
            wire = wire.rotate(Axis.Z, start_angle)
        except Exception as exc:  # noqa: BLE001
            raise FeatureError(f"helix: applying start_angle failed: {exc}")

    edges = list(wire.edges())
    if not edges:
        raise FeatureError("helix: produced no curve geometry")
    # Normalise the Helix object to a plain Wire carrier (clean type for tool_body / AICM).
    try:
        wire = Wire(edges)
    except Exception:  # noqa: BLE001
        pass

    _register_path(ctx, fid, base_plane, edges)

    # A curve feature adds NO solid body. Store the wire as tool_body so the AICM path can find
    # it (same convention sketches use), but leave the rolling body list untouched.
    return CompileOutput(
        bodies=list(ctx.bodies),
        tool_body=wire,
        op=None,
        resolved={"mode": 0.0, "pitch": pitch, "height": height, "turns": turns,
                  "taper": taper, "radius": radius},
    )


def _register_path(ctx: CompileContext, fid: str, base_plane: Plane, edges: list[Edge]) -> None:
    """Register edges into the ``_sketch_regions`` store so sweep can consume them as a path.

    Mirrors the sketch compiler's payload shape ``{"all_edges", "plane", "faces", "descs"}`` so a
    downstream ``sweep`` whose ``refs.path`` is ``{"kind":"region","sketch":<fid>}`` lifts these
    edges through ``base_plane.location`` with no change to sweep.py.
    """
    _SKETCH_KEY = "_sketch_regions"
    if _SKETCH_KEY not in ctx.datums:
        ctx.datums[_SKETCH_KEY] = Datum(kind="_sketch_regions", obj={}, feature_id="__sys__")
    ctx.datums[_SKETCH_KEY].obj[fid] = {
        "faces": [],
        "descs": [],
        "plane": base_plane,
        "all_edges": edges,
    }
