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
_RELIEF_KEY = "_sheet_metal_reliefs"
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

    # --- CUT PROPAGATION + CORNER RELIEF -------------------------------------------------------
    # Carry every cut (punched hole / slot) and every corner-relief notch through the unfold so the
    # flat blank + its DXF show them at the developed position.
    #   * base-region holes already developed into the flat SOLID (the base broad face extrudes WITH
    #     its inner wires) — we harvest those loops for the export.
    #   * flange-WALL holes are re-developed onto the strip and cut from the flat solid.
    #   * corner reliefs (recorded by the edge-flange compiler) are cut from the flat blank too.
    cut_loops: list[list[list[float]]] = []
    # A broad-face area floor: ignore the thin thickness strips, keep the real flat broad faces.
    min_broad_area = max(4.0 * thickness * thickness, 1.0)

    # Wall holes: develop + cut from the flat solid (must run BEFORE harvesting base loops so any
    # wall hole that lands in the base plane is visible to the harvest).
    for rec in bends:
        ba_rec = float(rec["bend_allowance"])
        try:
            tools, loops = _wall_hole_loops(body, rec, n_base, face_pt, ba_rec)
        except Exception:  # noqa: BLE001
            tools, loops = [], []
        for tool in tools:
            try:
                cut = _to_solid_normalized(flat - tool)
                if getattr(cut, "volume", 0.0) < getattr(flat, "volume", 0.0) - 1e-9:
                    flat = cut
            except Exception:  # noqa: BLE001
                pass
        cut_loops.extend(loops)

    # Corner reliefs developed into the flat blank.
    relief_store = ctx.datums.get(_RELIEF_KEY)
    reliefs = list(relief_store.obj) if (relief_store and relief_store.obj) else []
    if reliefs:
        try:
            rtools, rloops = _relief_flat_loops(reliefs, n_base, face_pt, thickness)
        except Exception:  # noqa: BLE001
            rtools, rloops = [], []
        for tool in rtools:
            try:
                cut = _to_solid_normalized(flat - tool)
                if getattr(cut, "volume", 0.0) < getattr(flat, "volume", 0.0) - 1e-9:
                    flat = cut
            except Exception:  # noqa: BLE001
                pass
        cut_loops.extend(rloops)

    flat = _to_solid_normalized(flat)

    # Base-region holes (everything still showing as an inner wire on the flat broad face). This
    # also picks up the wall holes + reliefs we just cut, so harvest is the single source of truth
    # for what the blank's broad face actually carries — then merge with any wall loops the harvest
    # could not see (a hole fully inside the developed strip away from the base face).
    harvested = _base_region_cut_loops(flat, n_base, face_pt, min_broad_area)
    cut_loops = _merge_cut_loops(harvested, cut_loops)

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
        # CUT loops (holes / slots / corner-relief notches) developed into the flat blank. Each is a
        # closed 3-D polyline on the base plane. ``export2d.flat_pattern_layers`` should draw these
        # on a CUTS layer (one-line addition — see the module note); the flat SOLID already carries
        # them as inner wires, so they are present regardless of the export wiring.
        "cut_loops": cut_loops,
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
        "cut_count": float(len(cut_loops)),
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


# --------------------------------------------------------------------------- cut propagation
#
# A flat pattern must carry CUTS (punched holes / slots) through the unfold so every cut appears in
# the flat blank at its developed position. Two cases:
#
#   * a cut in the BASE region — it already lands in the flat SOLID because ``_flat_base_block``
#     extrudes the base broad face WITH its inner wires (holes). We just harvest those inner wires
#     as developed loops so the export (DXF) carries them too.
#   * a cut in a FLANGE WALL — the wall is dropped and replaced by a developed strip, so the cut
#     must be RE-DEVELOPED: each point on the wall at distance ``d`` from the bend tangent maps to
#     flat position ``tangent + length_dir·(BA + d)`` (the wall unrolls into the base plane). We
#     find wall holes on the folded body, develop their loops, and cut them from the flat solid.


def _discretize_wire(wire, max_seg_len: float = 1.0) -> list[Vector]:
    """Sample a wire (hole/slot boundary) to an ordered list of 3-D points (arcs → polylines)."""
    pts: list[Vector] = []
    try:
        edges = wire.order_edges() if hasattr(wire, "order_edges") else wire.edges()
    except Exception:  # noqa: BLE001
        edges = wire.edges()
    for e in edges:
        try:
            length = float(e.length)
        except Exception:  # noqa: BLE001
            length = 0.0
        nseg = max(1, int(math.ceil(length / max(max_seg_len, 1e-6))))
        # Circles/curves need finer sampling; lines just need endpoints.
        try:
            is_line = (e.geom_type.name == "LINE")
        except Exception:  # noqa: BLE001
            is_line = False
        if is_line:
            nseg = 1
        for i in range(nseg):
            t = i / nseg
            try:
                p = e @ t
                q = Vector(p.X, p.Y, p.Z)
            except Exception:  # noqa: BLE001
                vs = e.vertices()
                q = Vector(vs[0].X, vs[0].Y, vs[0].Z)
            if not pts or (q - pts[-1]).length > 1e-7:
                pts.append(q)
    # Close the loop.
    if len(pts) >= 2 and (pts[0] - pts[-1]).length > 1e-7:
        pts.append(pts[0])
    return pts


def _broad_faces(body, n_base: Vector, min_area: float):
    """Planar faces whose normal ∥ n_base with area >= min_area (the broad faces of a region)."""
    from build123d import GeomType
    out = []
    for f in body.faces():
        try:
            if f.geom_type != GeomType.PLANE:
                continue
            n = f.normal_at()
            n = n * (1.0 / max(n.length, 1e-12))
            if abs(abs(n.dot(n_base)) - 1.0) < 1e-4 and float(f.area) >= min_area:
                out.append(f)
        except Exception:  # noqa: BLE001
            continue
    return out


def _base_region_cut_loops(flat: Solid, n_base: Vector, face_pt: Vector,
                           min_area: float) -> list[list[list[float]]]:
    """Harvest every hole (inner wire) on the flat sheet's broad faces, projected to the base plane.

    These are cuts that already developed into the flat SOLID (base-region holes survive the
    ``_flat_base_block`` extrude). We return them as 3-D loops on the picked broad-face plane so the
    DXF export draws them on the blank."""
    loops: list[list[list[float]]] = []
    # Take inner wires from the broad face that carries them (one side is enough — the hole is the
    # same on both skins). Pick the broad face on the picked-face side (closest to face_pt).
    faces = _broad_faces(flat, n_base, min_area)
    if not faces:
        return loops
    # Deduplicate by side: a through-hole appears on BOTH broad faces; harvest from the side nearest
    # face_pt only so we don't emit each hole twice.
    target_side = min(faces, key=lambda f: abs((f.center() - face_pt).dot(n_base)))
    side_sign = (target_side.center() - face_pt).dot(n_base)
    for f in faces:
        if ((f.center() - face_pt).dot(n_base)) * 1.0 - side_sign > 0.5:  # other skin
            continue
        if abs((f.center() - face_pt).dot(n_base) - side_sign) > 1e-3:
            continue
        try:
            inners = f.inner_wires()
        except Exception:  # noqa: BLE001
            inners = []
        for w in inners:
            pts = _discretize_wire(w)
            if len(pts) < 3:
                continue
            # Project onto the picked broad-face plane.
            proj = [p - n_base * ((p - face_pt).dot(n_base)) for p in pts]
            loops.append([[p.X, p.Y, p.Z] for p in proj])
    return loops


def _wall_hole_loops(body, rec: dict, n_base: Vector, face_pt: Vector,
                     ba: float) -> tuple[list[Solid], list[list[list[float]]]]:
    """Develop holes that lie in flange ``rec``'s WALL onto the flat, as (cut tools, loops).

    The wall is the planar pair perpendicular to the base on the fold side. A hole on the wall sits
    at some distance ``d`` along the wall from the bend tangent; unrolled, it maps to flat position
    ``tangent + length_dir·(BA + d)`` keeping its position along the bend axis. We rebuild each hole
    as a flat loop and a through-thickness cut tool so the flat SOLID carries it too.
    """
    from build123d import GeomType
    e_dir = _v(rec["e_dir"]); length_dir = _v(rec["length_dir"]); fold_dir = _v(rec["fold_dir"])
    e_dir = e_dir * (1.0 / max(e_dir.length, 1e-12))
    length_dir = length_dir * (1.0 / max(length_dir.length, 1e-12))
    fold_dir = fold_dir * (1.0 / max(fold_dir.length, 1e-12))
    R = float(rec["radius"]); T = float(rec["thickness"])
    angle = float(rec["angle"])
    position = rec.get("position", "material_inside")
    x0 = _setback(position, R, T)
    p0 = _v(rec["bend_line"][0])
    # Bend tangent line (on the base plane): where the bend region begins. Developed distance is
    # measured along +length_dir from here: the bend develops to BA, the wall keeps its length.
    tangent = p0 + length_dir * x0
    tangent = tangent - n_base * ((tangent - face_pt).dot(n_base))  # on the base plane
    # Physical drop the bend region consumes perpendicular to the base before the wall begins. For
    # the canonical 90° material_inside bend the wall starts (R+T) below the base plane; in general
    # the bend's perpendicular rise is (R+T)·sin(angle). Beyond that, the wall is straight and its
    # surface-distance equals the developed distance past BA.
    ang = math.radians(angle)
    bend_drop = (R + T) * math.sin(ang) if angle < 179.999 else (R + T)
    # The wall's broad-face normal is perpendicular to BOTH the base normal and e_dir, i.e. it lies
    # in the (length_dir, fold_dir) plane rotated by the bend angle. We detect wall faces as planar
    # faces whose normal is ⟂ n_base and ⟂ e_dir (a vertical wall for 90°), and NOT the base broad
    # faces themselves.
    tools: list[Solid] = []
    loops: list[list[list[float]]] = []
    seen_centroids: list[tuple[float, float, float]] = []

    def _develop(pts: list[Vector]) -> list[Vector]:
        out: list[Vector] = []
        for p in pts:
            rel = p - tangent
            lat = rel.dot(e_dir)                 # bend-axis position is preserved by the unroll
            drop = abs(rel.dot(n_base))          # perpendicular drop below the base plane
            # The bend region (drop ≤ bend_drop) develops linearly to BA; the straight wall beyond
            # develops 1:1.
            if bend_drop > 1e-9 and drop <= bend_drop + 1e-9:
                dev = ba * (drop / bend_drop)
            else:
                dev = ba + (drop - bend_drop)
            out.append(tangent + e_dir * lat + length_dir * dev)
        return out

    for f in body.faces():
        try:
            if f.geom_type != GeomType.PLANE:
                continue
            n = f.normal_at(); n = n * (1.0 / max(n.length, 1e-12))
            # A wall broad face: normal perpendicular to the base normal (not a base/parallel face)
            # and perpendicular to the bend axis (so it is a wall of THIS flange, spanning e_dir).
            if abs(n.dot(n_base)) > 1e-2 or abs(n.dot(e_dir)) > 1e-2:
                continue
            inners = f.inner_wires()
        except Exception:  # noqa: BLE001
            continue
        if not inners:
            continue
        for w in inners:
            pts = _discretize_wire(w)
            if len(pts) < 3:
                continue
            flat_pts = _develop(pts)
            if (flat_pts[0] - flat_pts[-1]).length > 1e-7:
                flat_pts.append(flat_pts[0])
            cx = sum(p.X for p in flat_pts) / len(flat_pts)
            cy = sum(p.Y for p in flat_pts) / len(flat_pts)
            cz = sum(p.Z for p in flat_pts) / len(flat_pts)
            # A through-hole shows on BOTH wall skins; develop it from one skin only.
            if any(math.dist((cx, cy, cz), s) <= max(0.5 * T, 0.5) for s in seen_centroids):
                continue
            seen_centroids.append((cx, cy, cz))
            loops.append([[p.X, p.Y, p.Z] for p in flat_pts])
            try:
                tools.append(_loop_cut_tool(flat_pts, n_base, T))
            except Exception:  # noqa: BLE001
                pass
    return tools, loops


def _loop_cut_tool(loop_pts: list[Vector], n_base: Vector, thickness: float) -> Solid:
    """A through-thickness prism over a closed loop (over-driven beyond both skins) — a cut tool."""
    base = [p - n_base * (thickness + 1.0) for p in loop_pts]
    # Drop near-coincident consecutive points (and the closing duplicate) so make_line never sees a
    # zero-length segment.
    clean: list[Vector] = []
    for v in base:
        if not clean or (v - clean[-1]).length > 1e-7:
            clean.append(v)
    if len(clean) >= 2 and (clean[0] - clean[-1]).length <= 1e-7:
        clean.pop()
    if len(clean) < 3:
        raise FeatureError("sm_flat_pattern: degenerate cut loop")
    edges = [Edge.make_line(clean[i], clean[(i + 1) % len(clean)]) for i in range(len(clean))]
    wire = Wire(edges)
    mf = BRepBuilderAPI_MakeFace(wire.wrapped, True)
    if not mf.IsDone():
        raise FeatureError("sm_flat_pattern: cut-loop face build failed")
    face = Face(mf.Face())
    vec = n_base * (2.0 * thickness + 2.0)
    mk = BRepPrimAPI_MakePrism(face.wrapped, gp_Vec(vec.X, vec.Y, vec.Z), False, True)
    if not mk.IsDone():
        raise FeatureError("sm_flat_pattern: cut-loop prism build failed")
    return Solid(mk.Shape()).clean()


def _relief_flat_loops(reliefs: list[dict], n_base: Vector, face_pt: Vector,
                       thickness: float) -> tuple[list[Solid], list[list[list[float]]]]:
    """Corner-relief notches developed into the flat blank: (cut tools, loops).

    Each relief record already carries the notch ``loop`` on the base plane (the edge-flange
    compiler built it there). We project it to the picked broad-face plane and build a cut tool so
    the flat blank shows the same relief notch the folded part has."""
    tools: list[Solid] = []
    loops: list[list[list[float]]] = []
    for r in reliefs:
        pts = [_v(p) for p in r.get("loop", [])]
        if len(pts) < 3:
            continue
        proj = [p - n_base * ((p - face_pt).dot(n_base)) for p in pts]
        loops.append([[p.X, p.Y, p.Z] for p in proj])
        try:
            tools.append(_loop_cut_tool(proj, n_base, thickness))
        except Exception:  # noqa: BLE001
            pass
    return tools, loops


def _loop_centroid(loop: list[list[float]]) -> tuple[float, float, float]:
    n = max(len(loop), 1)
    sx = sum(p[0] for p in loop) / n
    sy = sum(p[1] for p in loop) / n
    sz = sum(p[2] for p in loop) / n
    return sx, sy, sz


def _merge_cut_loops(primary: list[list[list[float]]],
                     extra: list[list[list[float]]],
                     tol: float = 1.0) -> list[list[list[float]]]:
    """Union two cut-loop lists, dropping near-duplicate loops (same centroid within ``tol`` mm).

    ``primary`` (harvested from the flat solid's broad face) is authoritative; ``extra`` (developed
    wall/relief loops) is appended only when it is not already represented in ``primary``."""
    out = list(primary)
    cents = [_loop_centroid(lp) for lp in out]
    for lp in extra:
        if len(lp) < 3:
            continue
        c = _loop_centroid(lp)
        dup = any(math.dist(c, pc) <= tol for pc in cents)
        if not dup:
            out.append(lp)
            cents.append(c)
    return out
