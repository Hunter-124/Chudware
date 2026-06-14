"""Sheet-metal FLAT PATTERN (unfold) — develop a folded sheet-metal part into its flat blank.

feature_params.md §Sheet Metal → ``sm_flat_pattern``.

A flat pattern is the developed (unfolded) blank a sheet-metal part is cut from before bending.
SolidWorks shows it as a derived body/view; the developed length across each bend equals the
**Bend Allowance** (BA), not the outside corner length. This feature reconstructs that flat blank
from the per-bend records the edge-flange compiler already stores — no re-derivation from the
solid is needed (the records carry BA + the bend-line frame, which is sufficient; see below).

Unfold method (from the stored bend records)
--------------------------------------------
The base flange is the FIXED face. Each edge flange's bend record carries everything the unfold
needs (``engine/doc/features/sheetmetal.py`` stores it):

    angle, radius (R), thickness (T), k_factor (K), neutral_radius (Rn=R+K·T),
    bend_allowance (BA = angle_rad·Rn), wall_length, position, flip,
    bend_line:[[x,y,z],[x,y,z]], e_dir, length_dir, fold_dir

The flange was built in the 2-D cross-section frame ``(u = length_dir, v = fold_dir)`` anchored at
``p_start`` (a point on the picked thickness-face bend edge). In that frame the bend's inner arc is
tangent to the base broad face at ``u = x0`` where, mirroring the builder's flange-position offset:

    material_inside  : x0 = -(R + T)
    material_outside : x0 = -R
    bend_outside     : x0 = 0

``x0`` is the **setback** of the bend tangent line from the picked edge — i.e. how far the bend
"eats into" the base along ``length_dir``. To unfold, we keep the base sheet and replace the folded
bend + wall with a FLAT developed strip lying in the base plane:

    * it starts at the bend tangent line  ``tangent = p_start + length_dir · x0``
    * it extends along ``length_dir`` (the direction the flat sheet continues across the bend) by
      the developed length ``BA + wall_length`` — the bend region develops to BA (neutral-fibre arc
      length), the flat wall keeps its physical ``wall_length``.
    * it spans ``e_dir`` for the bend-line length, at sheet thickness ``T`` along the base normal.

The flat body is the union of the base sheet with every such developed strip — a single planar
sheet of thickness ``T`` in the base-flange plane. The base-plane normal is ``e_dir × length_dir``
(both lie in the base broad face), so the construction needs only the record, never the solid.

Developed-length check (the number a SolidWorks user verifies first)
-------------------------------------------------------------------
For a single-bend L-bracket, the total flat length along the flange direction is

    L_flat = base_length_to_bend + BA + wall_length

where ``base_length_to_bend`` is the distance from the base's far edge to the bend tangent line
(= base extent − |x0| setback). The bend's projected/outer corner length is REPLACED by BA. For
the canonical 60×40×2 sheet + 90° flange (R=2, K=0.45, wall=20, material_inside):
``x0 = -(R+T) = -4`` so base_to_bend = 40 − 4 = 36, BA = (π/2)(2 + 0.45·2) = 4.5553, and
``L_flat = 36 + 4.5553 + 20 = 60.5553 mm``.

Outputs (stored on the CompileOutput.resolved + on ctx datums for the later flat-DXF wave)
-----------------------------------------------------------------------------------------
- the FLAT solid (a planar sheet of thickness T) as the body,
- ``bend_lines`` : per bend, the two flat lines (3-D polylines) bounding the developed bend region
  (the "up" and "down" bend lines a flat drawing annotates), plus the bend angle/BA,
- ``outline``    : the boundary polyline(s) of the flat sheet (feeds the flat-DXF export),
- ``flat_area`` / ``flat_volume`` : developed area and area·T (volume) for verification.

Single vs multi-bend
--------------------
Single-level flanges (base + one or more edge flanges) unfold exactly. A flange off a flange
(multi-level) is detected by a bend whose tangent line does not lie on the base plane; such a bend
is developed about its own parent direction as a best-effort continuation, and a note is recorded.
"""

from __future__ import annotations

import math
from typing import Any

from build123d import Edge, Face, Solid, Vector, Wire
from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace
from OCP.BRepPrimAPI import BRepPrimAPI_MakePrism
from OCP.gp import gp_Vec

from .base import CompileContext, CompileOutput, Datum, FeatureError, register
from .extrude import _store_topo_owners, _to_solid_normalized

# Keys the base/edge-flange compilers write (see sheetmetal.py).
_SM_KEY = "_sheet_metal"
_BEND_KEY = "_sheet_metal_bends"
# Key this feature writes for the later flat-DXF / flat-view wave.
_FLAT_KEY = "_sheet_metal_flat"


def _v(seq) -> Vector:
    return Vector(float(seq[0]), float(seq[1]), float(seq[2]))


def _setback(position: str, R: float, T: float) -> float:
    """Signed offset (along length_dir) of the bend tangent line from the picked edge.

    Mirrors ``_build_flange_solid``'s ``x0`` so the unfold lands the developed strip exactly where
    the bend began on the base broad face."""
    if position == "material_inside":
        return -(R + T)
    if position == "material_outside":
        return -R
    return 0.0  # bend_outside


def _make_rect_prism(c0: Vector, c1: Vector, c2: Vector, c3: Vector,
                     up: Vector, thickness: float) -> Solid:
    """A thickness-``T`` prism over the planar quad ``c0→c1→c2→c3``, extruded along ``up``."""
    edges = [Edge.make_line(c0, c1), Edge.make_line(c1, c2),
             Edge.make_line(c2, c3), Edge.make_line(c3, c0)]
    wire = Wire(edges)
    mf = BRepBuilderAPI_MakeFace(wire.wrapped, True)
    if not mf.IsDone():
        raise FeatureError("sm_flat_pattern: failed to build a developed-strip face")
    face = Face(mf.Face())
    vec = up * thickness
    mk = BRepPrimAPI_MakePrism(face.wrapped, gp_Vec(vec.X, vec.Y, vec.Z), False, True)
    if not mk.IsDone():
        raise FeatureError("sm_flat_pattern: developed-strip prism build failed")
    return Solid(mk.Shape()).clean()


def _base_plane_frame(rec: dict) -> tuple[Vector, Vector, Vector, Vector]:
    """Return ``(e_dir, length_dir, n_base, n_up)`` unit vectors from a bend record.

    ``n_base`` is the base broad-face normal (``e_dir × length_dir``); ``n_up`` is its negation
    (the direction the developed strip's thickness is extruded so the flat lands on the SAME side
    of the base plane as the base material)."""
    e_dir = _v(rec["e_dir"])
    length_dir = _v(rec["length_dir"])
    e_dir = e_dir * (1.0 / max(e_dir.length, 1e-12))
    length_dir = length_dir * (1.0 / max(length_dir.length, 1e-12))
    n_base = e_dir.cross(length_dir)
    n_base = n_base * (1.0 / max(n_base.length, 1e-12))
    return e_dir, length_dir, n_base, n_base * -1.0


@register("sm_flat_pattern")
def compile_sm_flat_pattern(feat: dict, ctx: CompileContext) -> CompileOutput:
    """Unfold the current sheet-metal body into its FLAT PATTERN (developed blank).

    params: ``{}`` — operates on the running sheet-metal body + the stored bend records.
    refs:   none.
    """
    fid = feat.get("id", "?")

    bodies = list(ctx.bodies)
    if not bodies:
        raise FeatureError("sm_flat_pattern: no body to flatten — add a sheet-metal part first")
    body = bodies[-1]

    # --- sheet-metal context (thickness / k / default radius) --------------
    sm_store = ctx.datums.get(_SM_KEY)
    if sm_store is None or not getattr(sm_store, "obj", None):
        raise FeatureError(
            "sm_flat_pattern: the active body is not sheet metal — a flat pattern needs a "
            "sheet-metal part (start with a base flange)"
        )
    sheet_ctx = sm_store.obj[list(sm_store.obj.keys())[-1]]
    thickness = float(sheet_ctx["thickness"])

    # --- bend records ------------------------------------------------------
    bend_store = ctx.datums.get(_BEND_KEY)
    bends: list[dict] = list(bend_store.obj) if (bend_store and bend_store.obj) else []
    if not bends:
        # A flat sheet's "flat pattern" is itself — but with no bends there is nothing to unfold,
        # which is a no-op the caller almost never wants. Contain it as a clean error (per spec).
        raise FeatureError(
            "sm_flat_pattern: the part has no bends — a flat sheet is already its own flat "
            "pattern (nothing to unfold)"
        )

    # --- choose the base-plane frame from the FIRST bend (all single-level flanges fold off the
    # base, so they share the base broad plane). Build a point on the base plane to classify which
    # bends are single-level (tangent line lies on the base plane) vs multi-level (off a flange).
    e0, l0, n_base, n_up = _base_plane_frame(bends[0])
    # A point on the base broad plane: the first bend's tangent line start.
    R0 = float(bends[0]["radius"]); T0 = float(bends[0]["thickness"])
    base_pt = _v(bends[0]["bend_line"][0]) + l0 * _setback(bends[0]["position"], R0, T0)

    def _on_base_plane(pt: Vector) -> bool:
        return abs((pt - base_pt).dot(n_base)) < 0.5 * thickness + 1e-6

    # --- start the flat from the FLAT BASE BLOCK (not the folded body) -----
    # The folded body carries the bent walls; a flat pattern must DROP them and keep only the
    # developed material in one plane. We rebuild the flat base by extruding the base's broad face
    # (the largest planar face whose normal ∥ n_base — it is the full flat base region; for
    # material_inside the top broad face spans the entire base, the bend only eats the bottom).
    base_block, into_dir, face_pt = _flat_base_block(body, n_base, thickness)
    # ``into_dir`` is the unit direction the base material extends across its thickness (from the
    # picked broad face into the solid); ``face_pt`` is a point on that picked broad-face plane.
    # Developed strips are projected onto that plane and extruded the SAME way, so the flat lands as
    # one coherent sheet in the base plane.
    flat = base_block

    def _to_face_plane(pt: Vector) -> Vector:
        """Project *pt* onto the picked broad-face plane (so strips are coplanar with the base)."""
        return pt - n_base * ((pt - face_pt).dot(n_base))
    bend_lines_out: list[dict] = []
    multibend_notes: list[str] = []

    for rec in bends:
        R = float(rec["radius"]); T = float(rec["thickness"])
        ba = float(rec["bend_allowance"]); wall = float(rec["wall_length"])
        angle = float(rec["angle"]); position = rec.get("position", "material_inside")

        e_dir = _v(rec["e_dir"]); length_dir = _v(rec["length_dir"])
        e_dir = e_dir * (1.0 / max(e_dir.length, 1e-12))
        length_dir = length_dir * (1.0 / max(length_dir.length, 1e-12))

        p0 = _v(rec["bend_line"][0])
        p1 = _v(rec["bend_line"][1])
        edge_len = (p1 - p0).length
        x0 = _setback(position, R, T)
        raw_tangent = p0 + length_dir * x0  # bend tangent line on the picked thickness-face edge

        if not _on_base_plane(raw_tangent):
            # Multi-level flange (off another flange): not co-planar with the base. We still develop
            # it about its own local frame so the strip is the right SIZE, but its placement is
            # best-effort (it lands on its own parent plane, not re-folded onto the base).
            multibend_notes.append(rec.get("feature", "?"))

        # Snap the tangent line onto the picked broad-face plane so the developed strip is coplanar
        # with the flat base block, then extrude it across the thickness the same way as the base.
        tangent_start = _to_face_plane(raw_tangent)

        dev_len = ba + wall  # developed length: bend region -> BA, wall keeps its length
        # Developed-strip quad in the base plane, at sheet thickness.
        c0 = tangent_start
        c1 = tangent_start + length_dir * dev_len
        c2 = c1 + e_dir * edge_len
        c3 = c0 + e_dir * edge_len

        try:
            strip = _make_rect_prism(c0, c1, c2, c3, into_dir, T)
        except FeatureError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise FeatureError(f"sm_flat_pattern: develop failed for bend "
                               f"{rec.get('feature', '?')}: {exc}")

        try:
            flat = flat + strip
        except Exception as exc:  # noqa: BLE001
            raise FeatureError(f"sm_flat_pattern: union of developed strip failed: {exc}")
        flat = _to_solid_normalized(flat)

        # Bend lines on the flat: the START line (at the tangent) and the END line (at +BA along the
        # unfold). These are the two lines a flat drawing annotates per bend.
        bl_start = [[tangent_start.X, tangent_start.Y, tangent_start.Z],
                    [(tangent_start + e_dir * edge_len).X,
                     (tangent_start + e_dir * edge_len).Y,
                     (tangent_start + e_dir * edge_len).Z]]
        end_pt = tangent_start + length_dir * ba
        bl_end = [[end_pt.X, end_pt.Y, end_pt.Z],
                  [(end_pt + e_dir * edge_len).X,
                   (end_pt + e_dir * edge_len).Y,
                   (end_pt + e_dir * edge_len).Z]]
        bend_lines_out.append({
            "feature": rec.get("feature", "?"),
            "angle": angle,
            "bend_allowance": ba,
            "radius": R,
            "line_start": bl_start,   # tangent line (bend region begins)
            "line_end": bl_end,       # bend region ends (BA developed)
        })

    flat = _to_solid_normalized(flat)
    if getattr(flat, "volume", 0.0) <= 1e-9:
        raise FeatureError("sm_flat_pattern: produced an empty flat body")

    # --- flat outline: the boundary edges of the flat sheet's largest broad face --------------
    outline = _flat_outline(flat, n_base)

    flat_area = _broad_face_area(flat, n_base)
    flat_volume = float(getattr(flat, "volume", 0.0) or 0.0)

    # Replace the running body with the flat body (the flat pattern IS the derived body).
    bodies[-1] = flat
    _store_topo_owners(ctx, fid, flat, body)

    # Persist flat-pattern artifacts for the flat-DXF / flat-view wave.
    flat_payload = {
        "feature": fid,
        "thickness": thickness,
        "bend_lines": bend_lines_out,
        "outline": outline,
        "flat_area": flat_area,
        "flat_volume": flat_volume,
        "n_base": [n_base.X, n_base.Y, n_base.Z],
        "multibend": multibend_notes,
    }
    if _FLAT_KEY not in ctx.datums:
        ctx.datums[_FLAT_KEY] = Datum(kind=_FLAT_KEY, obj={}, feature_id="__sys__")
    ctx.datums[_FLAT_KEY].obj[fid] = flat_payload

    resolved = {
        "bend_count": float(len(bends)),
        "thickness": thickness,
        "flat_area": flat_area,
        "flat_volume": flat_volume,
        "volume": flat_volume,
    }

    return CompileOutput(bodies=bodies, tool_body=flat, op="add", resolved=resolved)


# --------------------------------------------------------------------------- flat geometry helpers


def _flat_base_block(body: Solid, n_base: Vector,
                     thickness: float) -> tuple[Solid, Vector, Vector]:
    """The FLAT base region of the folded body, re-extruded as a thickness-``T`` block.

    The base sheet's broad face (the largest planar face whose normal ∥ ``n_base``) is the full
    flat base region — for ``material_inside`` the bend only eats the OPPOSITE broad face, so the
    picked face still spans the whole base. We extrude that face by ``thickness`` INTO the solid to
    rebuild just the flat base, dropping every bent wall.

    Returns ``(base_block, into_dir, face_pt)``: the flat block, the unit direction the face was
    extruded (into the material), and a point on the picked broad-face plane.
    """
    from build123d import GeomType
    broad = None
    best_area = -1.0
    for f in body.faces():
        try:
            if f.geom_type != GeomType.PLANE:
                continue
            n = f.normal_at()
            n = n * (1.0 / max(n.length, 1e-12))
            if abs(abs(n.dot(n_base)) - 1.0) < 1e-4 and float(f.area) > best_area:
                best_area = float(f.area)
                broad = f
        except Exception:  # noqa: BLE001
            continue
    if broad is None:
        raise FeatureError("sm_flat_pattern: could not find the base broad face to flatten")

    face_n = broad.normal_at()
    face_n = face_n * (1.0 / max(face_n.length, 1e-12))
    face_pt = broad.center()
    # Extrude INTO the solid: opposite the OUTWARD face normal. The picked broad face's outward
    # normal points away from the body centre; the base material lies on the other side.
    bb = body.bounding_box()
    body_c = Vector((bb.min.X + bb.max.X) / 2.0, (bb.min.Y + bb.max.Y) / 2.0,
                    (bb.min.Z + bb.max.Z) / 2.0)
    into_dir = face_n if face_n.dot(body_c - face_pt) > 0 else face_n * -1.0
    into_dir = into_dir * (1.0 / max(into_dir.length, 1e-12))

    try:
        block = Solid.extrude(broad, into_dir * thickness)
    except Exception as exc:  # noqa: BLE001
        raise FeatureError(f"sm_flat_pattern: failed to rebuild the flat base block: {exc}")
    return _to_solid_normalized(block), into_dir, face_pt


def _broad_face_area(flat: Solid, n_base: Vector) -> float:
    """Total area of the flat sheet's broad faces on ONE side (the developed area).

    The flat is a planar sheet of thickness T; its two broad faces (normals ±n_base) carry the
    developed area. We sum the area of the faces whose normal is (anti)parallel to n_base and take
    half (one side) — robust even when the union splits a broad face into several coplanar faces.
    """
    from build123d import GeomType
    total = 0.0
    for f in flat.faces():
        try:
            if f.geom_type != GeomType.PLANE:
                continue
            n = f.normal_at()
            n = n * (1.0 / max(n.length, 1e-12))
            if abs(abs(n.dot(n_base)) - 1.0) < 1e-4:
                total += float(f.area)
        except Exception:  # noqa: BLE001
            continue
    return total / 2.0 if total > 0 else 0.0


def _flat_outline(flat: Solid, n_base: Vector) -> list[list[float]]:
    """Boundary polyline (ordered 3-D points) of the flat sheet's largest broad face.

    Feeds the later flat-DXF export. We pick the largest planar face whose normal ∥ n_base (a broad
    face) and walk its outer wire's vertices in order.
    """
    from build123d import GeomType
    broad = None
    best_area = -1.0
    for f in flat.faces():
        try:
            if f.geom_type != GeomType.PLANE:
                continue
            n = f.normal_at()
            n = n * (1.0 / max(n.length, 1e-12))
            if abs(abs(n.dot(n_base)) - 1.0) < 1e-4 and float(f.area) > best_area:
                best_area = float(f.area)
                broad = f
        except Exception:  # noqa: BLE001
            continue
    if broad is None:
        return []
    pts: list[list[float]] = []
    try:
        wire = broad.outer_wire()
        for v in wire.vertices():
            pts.append([v.X, v.Y, v.Z])
    except Exception:  # noqa: BLE001
        try:
            for v in broad.vertices():
                pts.append([v.X, v.Y, v.Z])
        except Exception:  # noqa: BLE001
            return []
    return pts
