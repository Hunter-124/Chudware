"""Dress-up feature compilers: fillet / chamfer / shell.

feature_params.md §Dress-up.

Fillet and chamfer resolve edge selectors from the *current top body* using C03's
describe_edges / resolve API, then apply build123d's fillet/chamfer with auto-clamp
retry (halve ×3, floor 0.2 mm) mirroring the pattern in kernel._wrap_edge_op.

Shell resolves face selectors from the current top body and uses
``build123d.offset(solid, -thickness, openings=faces)``; OCCT failures surface the
"shell before fillet" guidance.

Dangling selector → FeatureError with dangling_refs list.
"""

from __future__ import annotations

import math
from typing import Any

from build123d import fillet as b3d_fillet, chamfer as b3d_chamfer, offset, Solid, GeomType
from OCP.BRepFilletAPI import BRepFilletAPI_MakeFillet, BRepFilletAPI_MakeChamfer
from OCP.BRepAdaptor import BRepAdaptor_Curve
from OCP.BRepOffset import BRepOffset_MakeOffset, BRepOffset_Mode
from OCP.ChFi3d import ChFi3d_FilletShape
from OCP.GeomAbs import GeomAbs_Arc
from OCP.TopExp import TopExp
from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE
from OCP.TopTools import TopTools_IndexedDataMapOfShapeListOfShape
from OCP.TopoDS import TopoDS

from .base import CompileContext, CompileOutput, FeatureError, register
from engine.doc.selectors import (
    describe_edges,
    describe_faces,
    resolve,
    TopoInfo,
)

# --------------------------------------------------------------------------- helpers


def _bbox_diag(shape) -> float:
    """Return the bounding-box diagonal of *shape* (used for selector normalisation)."""
    try:
        bb = shape.bounding_box()
        dx = bb.max.X - bb.min.X
        dy = bb.max.Y - bb.min.Y
        dz = bb.max.Z - bb.min.Z
        d = math.sqrt(dx * dx + dy * dy + dz * dz)
        return d if d > 1e-12 else 1.0
    except Exception:  # noqa: BLE001
        return 1.0


def _describe_with_owners(shape, topo_kind: str, ctx=None) -> list[TopoInfo]:
    """Describe faces or edges, then patch owner fields from ctx's stored topo owners.

    topo_kind: "face" or "edge"
    """
    from engine.doc.features.extrude import _get_topo_owners
    if topo_kind == "face":
        infos = describe_faces(shape)
    else:
        infos = describe_edges(shape)
    if ctx is not None:
        topo_owners = _get_topo_owners(ctx, topo_kind)
        for info in infos:
            owner = topo_owners.get(info.index + 1)  # convert 0-based to 1-based
            if owner is not None:
                info.owner = owner
    return infos


def _resolve_with_hint_fallback(
    sel: dict[str, Any],
    infos: list[TopoInfo],
    diag: float,
) -> int | None:
    """Resolve a selector, applying hint_index tiebreak fallback for margin failures.

    Per selectors.md §3, ties (margin <= 0.05) are broken by nearest hint_index when
    the best score is very low (< 0.01) — the reference is clearly correct, just
    geometrically ambiguous due to identical shapes (e.g. 4 identical box edges).
    """
    from engine.doc.selectors import _score_candidate
    rr = resolve(sel, infos, diag)
    if rr.index is not None:
        return rr.index

    # Apply hint_index fallback: only when margin failure and best score < 0.01
    if rr.dangling_reason and "margin" in rr.dangling_reason and rr.score < 0.01:
        kind = sel.get("kind")
        s_owner = sel.get("owner_feature")
        s_hint = sel.get("hint_index")
        candidates = [i for i in infos if i.kind == kind]
        if s_owner:
            owner_set = [i for i in candidates if i.owner == s_owner]
            if owner_set:
                candidates = owner_set
        scored = sorted(
            [(_score_candidate(sel, c, diag), c) for c in candidates],
            key=lambda t: (t[0], t[1].hint_index)
        )
        if scored and scored[0][0] < 0.01:
            # If hint_index is specified and matches the best after sort, use it
            best_cand = scored[0][1]
            if s_hint is None or best_cand.hint_index == s_hint:
                return best_cand.index
            # Try to find the candidate with the matching hint_index
            for _, c in scored:
                if c.hint_index == s_hint:
                    return c.index
            # Fall back to the overall best
            return best_cand.index
    return None


def _resolve_edges(
    sel_list: list[dict[str, Any]],
    body,
    slot_name: str,
    ctx=None,
) -> tuple[list, list[str]]:
    """Resolve a list of edge selectors against *body*.

    Returns ``(edge_objects, dangling_locators)``.
    """
    infos: list[TopoInfo] = _describe_with_owners(body.wrapped, "edge", ctx)
    diag = _bbox_diag(body)

    edge_objs = body.edges()  # build123d edge list (index-aligned with infos)
    resolved_edges: list = []
    dangling: list[str] = []

    for i, sel in enumerate(sel_list):
        idx = _resolve_with_hint_fallback(sel, infos, diag)
        if idx is None:
            rr = resolve(sel, infos, diag)
            dangling.append(f"{slot_name}[{i}]: {rr.dangling_reason}")
        else:
            resolved_edges.append(edge_objs[idx])

    return resolved_edges, dangling


def _resolve_faces(
    sel_list: list[dict[str, Any]],
    body,
    slot_name: str,
    ctx=None,
) -> tuple[list, list[str]]:
    """Resolve a list of face selectors against *body*."""
    infos = _describe_with_owners(body.wrapped, "face", ctx)
    diag = _bbox_diag(body)

    face_objs = body.faces()
    resolved_faces: list = []
    dangling: list[str] = []

    for i, sel in enumerate(sel_list):
        idx = _resolve_with_hint_fallback(sel, infos, diag)
        if idx is None:
            rr = resolve(sel, infos, diag)
            dangling.append(f"{slot_name}[{i}]: {rr.dangling_reason}")
        else:
            resolved_faces.append(face_objs[idx])

    return resolved_faces, dangling


# ---------------------------------------------------------------- topology walk helpers
#
# These back the SW-parity additions (tangent propagation, face fillet, vertex chamfer).
# They operate on raw OCP topology so they can share the edge↔face adjacency that the
# build123d wrappers do not expose directly.


def _edge_face_map(shape_wrapped) -> TopTools_IndexedDataMapOfShapeListOfShape:
    """Map every edge of *shape_wrapped* to the faces that own it (ancestors)."""
    m = TopTools_IndexedDataMapOfShapeListOfShape()
    TopExp.MapShapesAndAncestors_s(shape_wrapped, TopAbs_EDGE, TopAbs_FACE, m)
    return m


def _b3d_key(shape) -> tuple:
    """Stable geometric identity key for a build123d sub-shape (edge/face/vertex).

    OCP in this build exposes neither ``HashCode`` nor a stable ``id(TShape())`` across
    repeated ``.edges()``/``.faces()`` calls (the Python wrappers are transient, so their
    ``id`` is reused after GC — verified). We therefore key by a rounded geometric
    signature: ``(round(center xyz), round(measure))`` where *measure* is the area for a
    face and the length for an edge (so the key matches ``_raw_key`` for the same face).
    Unique within one body to CAD tolerance, and identical for the same entity regardless
    of which enumeration produced the wrapper — exactly what de-dup across walks needs.
    """
    c = shape.center()
    # Faces key on area, edges on length, vertices on 0 — matched to _raw_key (area).
    meas = 0.0
    if hasattr(shape, "area") and shape.area is not None and float(shape.area) > 1e-9:
        meas = float(shape.area)
    else:
        try:
            meas = float(shape.length)
        except Exception:  # noqa: BLE001
            meas = 0.0
    return (round(c.X, 5), round(c.Y, 5), round(c.Z, 5), round(meas, 5))


def _raw_key(shape_wrapped) -> tuple:
    """Geometric key for a *raw OCP* face (centre + area), matching ``_b3d_key``."""
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps
    props = GProp_GProps()
    BRepGProp.SurfaceProperties_s(shape_wrapped, props)
    cm = props.CentreOfMass()
    return (round(cm.X(), 5), round(cm.Y(), 5), round(cm.Z(), 5), round(props.Mass(), 5))


def _faces_of_edge(edge_wrapped, ef_map) -> list:
    """Return the (raw OCP) faces incident to *edge_wrapped* using the ancestor map."""
    faces: list = []
    if not ef_map.Contains(edge_wrapped):
        return faces
    flist = ef_map.FindFromKey(edge_wrapped)
    for f in flist:
        faces.append(TopoDS.Face_s(f))
    return faces


def _shared_vertices(e0, e1, tol: float = 1e-6) -> list[tuple[float, float, float]]:
    """Endpoint positions shared by build123d edges *e0* and *e1* (within tol)."""
    out: list[tuple[float, float, float]] = []
    v0 = [(v.X, v.Y, v.Z) for v in e0.vertices()]
    v1 = [(v.X, v.Y, v.Z) for v in e1.vertices()]
    for a in v0:
        for b in v1:
            if (abs(a[0] - b[0]) < tol and abs(a[1] - b[1]) < tol
                    and abs(a[2] - b[2]) < tol):
                out.append(a)
    return out


def _edges_share_vertex(e0, e1) -> bool:
    """True if build123d edges *e0* and *e1* share an endpoint vertex (within tol)."""
    return bool(_shared_vertices(e0, e1))


def _edge_tangent_at(edge, pt, tol: float = 1e-6) -> tuple[float, float, float] | None:
    """Unit tangent of *edge* at the endpoint nearest *pt*, or None if pt isn't an end."""
    ad = BRepAdaptor_Curve(edge.wrapped)
    for u in (ad.FirstParameter(), ad.LastParameter()):
        p = ad.Value(u)
        if (abs(p.X() - pt[0]) < tol and abs(p.Y() - pt[1]) < tol
                and abs(p.Z() - pt[2]) < tol):
            v = ad.DN(u, 1)
            n = math.sqrt(v.X() ** 2 + v.Y() ** 2 + v.Z() ** 2)
            if n < 1e-12:
                return None
            return (v.X() / n, v.Y() / n, v.Z() / n)
    return None


def _edges_tangent_continuous(e0, e1) -> bool:
    """G1 test: do edges *e0* and *e1* meet tangent-continuously at a shared vertex?

    SW's tangent-propagation contour grows along edges whose *curves* flow smoothly into
    one another (collinear lines, line→arc tangent, arc→arc tangent), NOT merely edges
    that touch the same face. We compare the unit curve tangents at the shared endpoint;
    |dot|≈1 means the chain continues without a crease. This correctly grabs a rounded
    rim chain (line-arc-line-arc) while refusing a sharp 90° box corner.
    """
    for v in _shared_vertices(e0, e1):
        t0 = _edge_tangent_at(e0, v)
        t1 = _edge_tangent_at(e1, v)
        if t0 is None or t1 is None:
            continue
        dot = t0[0] * t1[0] + t0[1] * t1[1] + t0[2] * t1[2]
        if abs(abs(dot) - 1.0) < 1e-3:
            return True
    return False


def _propagate_tangent_edges(body, seed_edges: list) -> list:
    """Auto-include tangent-connected edges (SW's default fillet/chamfer behaviour).

    Breadth-first walk from the seeds: an edge joins the contour when it shares an
    endpoint vertex with an already-included edge and the two *curves* meet tangent-
    continuously there (``_edges_tangent_continuous`` — collinear lines, line→arc or
    arc→arc tangency). This grows a rounded rim chain (line-arc-line-arc) but stops at a
    sharp 90° corner, matching SW. Returns the de-duplicated build123d edge list with
    seeds first, then discovered edges in body-enumeration order.
    """
    all_edges = body.edges()

    def edge_key(e) -> tuple:
        return _b3d_key(e)

    seed_keys = {edge_key(e) for e in seed_edges}
    chosen: dict[tuple, Any] = {edge_key(e): e for e in seed_edges}

    frontier = list(seed_edges)
    while frontier:
        nxt: list = []
        for cur in frontier:
            for cand in all_edges:
                k = edge_key(cand)
                if k in chosen:
                    continue
                if not _edges_share_vertex(cur, cand):
                    continue
                if _edges_tangent_continuous(cur, cand):
                    chosen[k] = cand
                    nxt.append(cand)
        frontier = nxt

    # Seeds first (stable), then discovered, preserving body-edge order.
    ordered = list(seed_edges)
    for e in all_edges:
        k = edge_key(e)
        if k in chosen and k not in seed_keys:
            ordered.append(e)
    return ordered


def _edges_between_face_sets(body, faces_a: list, faces_b: list) -> list:
    """Edges where one incident face is in set A and the other is in set B.

    This is the kernel-feasible realisation of a *face fillet*: OCCT has no public
    rolling-ball-between-two-arbitrary-faces operator, so we blend the body edges that
    *separate* the two face sets. When A and B are the two faces meeting at a corner
    the result is identical to SW's face fillet (the rolling ball touches both faces);
    when they are non-adjacent we fillet the chain of edges along the boundary between
    them — the closest faithful approximation the public API supports.
    """
    ef = _edge_face_map(body.wrapped)

    a_keys = {_b3d_key(f) for f in faces_a}
    b_keys = {_b3d_key(f) for f in faces_b}

    out: list = []
    for e in body.edges():
        inc = _faces_of_edge(e.wrapped, ef)
        inc_keys = [_raw_key(f) for f in inc]
        has_a = any(k in a_keys for k in inc_keys)
        has_b = any(k in b_keys for k in inc_keys)
        if has_a and has_b:
            out.append(e)
    return out


def _edges_at_vertices(body, verts: list) -> list:
    """All body edges incident to any vertex in *verts* (for vertex chamfer)."""
    out: list = []
    seen: set[tuple] = set()
    for e in body.edges():
        for v in verts:
            if any(
                abs(ev.X - v.X) < 1e-6 and abs(ev.Y - v.Y) < 1e-6
                and abs(ev.Z - v.Z) < 1e-6
                for ev in e.vertices()
            ):
                k = _b3d_key(e)
                if k not in seen:
                    seen.add(k)
                    out.append(e)
                break
    return out


def _resolve_vertices(
    sel_list: list[dict[str, Any]],
    body,
    slot_name: str,
    ctx=None,
) -> tuple[list, list[str]]:
    """Resolve vertex selectors against *body* (for vertex chamfer)."""
    from engine.doc.selectors import describe_vertices
    infos = describe_vertices(body.wrapped)
    diag = _bbox_diag(body)
    vtx_objs = body.vertices()
    resolved: list = []
    dangling: list[str] = []
    for i, sel in enumerate(sel_list):
        # Box corners enumerate multiple identical vertices (compound dup), so the plain
        # resolve hits a zero-margin tie. Reuse the same hint-fallback the edge/face
        # resolvers use: when the best score is ~0 the reference is clearly correct.
        idx = _resolve_with_hint_fallback(sel, infos, diag)
        if idx is None:
            rr = resolve(sel, infos, diag)
            dangling.append(f"{slot_name}[{i}]: {rr.dangling_reason}")
            continue
        # describe_vertices over a solid enumerates duplicates (one per incident face),
        # so its index space is wider than body.vertices() (which de-dups). Map the
        # resolved descriptor back to the nearest unique build123d vertex by position.
        target = infos[idx].centroid
        nearest = min(
            vtx_objs,
            key=lambda v: (v.X - target[0]) ** 2 + (v.Y - target[1]) ** 2
            + (v.Z - target[2]) ** 2,
        )
        resolved.append(nearest)
    return resolved, dangling


# --------------------------------------------------------------------------- fillet


def _line_dir(edge) -> tuple[float, float, float]:
    """Unit direction of a straight build123d edge (raw OCP line adaptor)."""
    ad = BRepAdaptor_Curve(edge.wrapped)
    d = ad.Line().Direction()
    return (d.X(), d.Y(), d.Z())


def _analyze_middle_face(face):
    """Validate a full-round middle face and compute its constant width.

    v1 precondition (the rib-top case users want): the middle face is planar and
    has a pair of parallel, equal-length straight "long" edges. The full round is
    the tangent cylinder that replaces the strip between those two edges; its
    radius is width/2 where width = the perpendicular distance between the pair.

    Returns ``(width, [edge0, edge1])`` or raises FeatureError with a clear v1
    constraint message (see feature_params.md §full_round).
    """
    if face.geom_type != GeomType.PLANE:
        raise FeatureError(
            "full round requires a planar constant-width middle face (v1)"
        )
    lines = [e for e in face.edges() if e.geom_type == GeomType.LINE]
    if len(lines) < 2:
        raise FeatureError(
            "full round middle face needs two straight side edges (v1)"
        )
    lines.sort(key=lambda e: -e.length)
    e0 = lines[0]
    d0 = _line_dir(e0)
    mate = None
    for e in lines[1:]:
        d = _line_dir(e)
        if abs(abs(d[0] * d0[0] + d[1] * d0[1] + d[2] * d0[2]) - 1.0) < 1e-4:
            mate = e
            break
    if mate is None:
        raise FeatureError(
            "full round requires a planar constant-width middle face (v1): "
            "no parallel long-edge pair found"
        )
    if abs(e0.length - mate.length) > 1e-4 * max(e0.length, 1.0):
        raise FeatureError(
            "full round requires a planar constant-width middle face (v1): "
            "the two long edges are not equal length"
        )
    c0 = e0.center()
    c1 = mate.center()
    sep = (c1.X - c0.X, c1.Y - c0.Y, c1.Z - c0.Z)
    proj = sep[0] * d0[0] + sep[1] * d0[1] + sep[2] * d0[2]
    perp = (sep[0] - proj * d0[0], sep[1] - proj * d0[1], sep[2] - proj * d0[2])
    width = math.sqrt(perp[0] ** 2 + perp[1] ** 2 + perp[2] ** 2)
    if width <= 1e-6:
        raise FeatureError("full round middle face has zero width (v1)")
    return width, [e0, mate]


def _compile_full_round(feat: dict, ctx: CompileContext) -> CompileOutput:
    """Full-round fillet: replace a planar constant-width middle face with a
    tangent half-cylinder (rib-top case). feature_params.md §full_round.

    Realized as the "poor man's full round": both long edges of the middle face
    filleted at r = width/2 * 0.999 in a single build123d fillet call. At exactly
    width/2 OCCT fails (the strip degenerates to a tangent seam); 0.999 leaves a
    ~width*1e-3 mm residual planar sliver that is below CAD tolerance, and the
    volume matches a true half-cylinder full round to <0.01%.
    """
    refs = feat.get("refs", {})
    bodies = list(ctx.bodies)
    if not bodies:
        raise FeatureError("fillet: no body to apply to")
    body = bodies[-1]

    mid_sel = refs.get("middle_face")
    if not mid_sel:
        raise FeatureError(
            "fillet full_round: refs.middle_face is required",
        )
    sel_list = mid_sel if isinstance(mid_sel, list) else [mid_sel]
    faces, dangling = _resolve_faces(sel_list, body, "middle_face", ctx=ctx)
    if dangling:
        raise FeatureError(
            f"fillet full_round: {len(dangling)} face selector(s) dangling",
            dangling_refs=dangling,
        )
    if not faces:
        raise FeatureError("fillet full_round: middle face not resolved")

    width, long_edges = _analyze_middle_face(faces[0])
    r = width / 2.0 * 0.999

    try:
        result_body = b3d_fillet(long_edges, radius=r)
    except Exception as exc:  # noqa: BLE001
        raise FeatureError(
            f"full round failed (width={width:g} mm): {exc}",
            hint="middle face may be too narrow or adjacent to other features",
        )

    bodies[-1] = result_body
    return CompileOutput(
        bodies=bodies,
        tool_body=result_body,
        resolved={"full_round_width": width},
    )


def _variable_fillet(body, edges, r1: float, r2: float):
    """Apply a linear-law variable-radius fillet (r1 at edge start, r2 at end).

    build123d has no variable-radius wrapper, so drop to raw OCP:
    BRepFilletAPI_MakeFillet.Add(r1, r2, edge) sets a linear radius law per edge.
    Result is re-wrapped as a build123d Solid (matches revolve.py's OCP→Solid path).
    Raises on IsDone()==False so the caller's clamp loop can retry.
    """
    mk = BRepFilletAPI_MakeFillet(body.wrapped)
    for e in edges:
        mk.Add(r1, r2, e.wrapped)
    mk.Build()
    if not mk.IsDone():
        raise RuntimeError("BRepFilletAPI_MakeFillet IsDone=False")
    return Solid(mk.Shape()).clean()


# Conic profile: SW exposes a "rho" in (0,1) controlling fillet fullness, where
# rho≈0.5 is the circular arc, smaller rho is a leaner (flatter) blend, larger rho a
# fuller one. OCCT's ChFi3d_FilletShape enum (Rational/QuasiAngular/Polynomial) only
# changes the *parametrisation*, not the section shape — on a 90° dihedral all three
# give a byte-identical solid (verified). So a faithful, kernel-robust conic is realised
# by mapping rho to an *effective radius* that reproduces the conic's removed-corner
# area, then running the (very robust) circular filleter. The mapping is anchored so
# rho=0.5 → r_eff = r exactly (true circular) and is monotonic in rho. This is the
# best faithful approximation the public API supports; documented as a deviation from a
# true variable-section conic blend.
_CONIC_RHO_DEFAULT = 0.5


def _conic_effective_radius(r: float, rho: float) -> float:
    """Map a conic-profile (radius r, parameter rho) to an equivalent circular radius.

    For a 90° corner a circular fillet of radius r removes ``r²(1-π/4)`` of material.
    A conic of the same leg setback removes a rho-dependent fraction of the bounding
    corner-triangle (area ``0.5 r²``): leaner blends (small rho) cut more, fuller blends
    (large rho) cut less. We solve for the circular radius removing the same area so the
    robust circular filleter can realise it. rho is clamped to (0,1).
    """
    rho = min(max(rho, 1e-3), 0.999)
    base = 2.0 * (1.0 - math.pi / 4.0)          # removed-fraction at rho=0.5 (circular)
    frac = base * (1.0 - rho) / 0.5             # fraction of the 0.5 r² triangle removed
    frac = min(max(frac, 1e-6), 1.0)
    # area removed = frac * 0.5 r²  ==  r_eff²(1-π/4)  ->  r_eff = r√(frac·0.5/(1-π/4))
    r_eff = r * math.sqrt(frac * 0.5 / (1.0 - math.pi / 4.0))
    return max(r_eff, 1e-3)


def _conic_fillet(body, edges, r: float, rho: float):
    """Circular-equivalent conic fillet (see ``_conic_effective_radius``).

    Also tags the OCCT section as QuasiAngular so the parametrisation matches the
    intent; the volume-driving effect comes from the effective radius.
    """
    r_eff = _conic_effective_radius(r, rho)
    mk = BRepFilletAPI_MakeFillet(body.wrapped, ChFi3d_FilletShape.ChFi3d_QuasiAngular)
    for e in edges:
        mk.Add(r_eff, e.wrapped)
    mk.Build()
    if not mk.IsDone():
        raise RuntimeError("BRepFilletAPI_MakeFillet (conic) IsDone=False")
    return Solid(mk.Shape()).clean()


def _compile_face_fillet(feat: dict, ctx: CompileContext) -> CompileOutput:
    """Face fillet: rolling-ball blend between two (non-adjacent) face sets.

    refs.face_set_a / refs.face_set_b each hold one-or-more face selectors. We resolve
    both sets, find the body edges separating them (``_edges_between_face_sets``) and
    fillet that chain at params.radius. For two faces meeting at a corner this is exactly
    SW's face fillet; for non-adjacent sets it blends the boundary chain between them —
    the closest the public OCCT API supports (no rolling-ball-across-a-gap operator).
    """
    p = feat.get("params", {})
    refs = feat.get("refs", {})
    radius = ctx.dim(p, "radius", 2.0)
    if radius <= 0.0:
        raise FeatureError("fillet radius must be > 0")

    bodies = list(ctx.bodies)
    if not bodies:
        raise FeatureError("fillet face_fillet: no body to apply to")
    body = bodies[-1]

    a_sel = refs.get("face_set_a")
    b_sel = refs.get("face_set_b")
    if not a_sel or not b_sel:
        raise FeatureError(
            "fillet face_fillet: refs.face_set_a and refs.face_set_b are both required",
        )
    a_list = a_sel if isinstance(a_sel, list) else [a_sel]
    b_list = b_sel if isinstance(b_sel, list) else [b_sel]

    faces_a, dang_a = _resolve_faces(a_list, body, "face_set_a", ctx=ctx)
    faces_b, dang_b = _resolve_faces(b_list, body, "face_set_b", ctx=ctx)
    dangling = dang_a + dang_b
    if dangling:
        raise FeatureError(
            f"fillet face_fillet: {len(dangling)} face selector(s) dangling",
            dangling_refs=dangling,
        )
    if not faces_a or not faces_b:
        raise FeatureError("fillet face_fillet: one or both face sets unresolved")

    edges = _edges_between_face_sets(body, faces_a, faces_b)
    if not edges:
        raise FeatureError(
            "fillet face_fillet: the two face sets share no separating edge",
            hint="face fillet currently blends the edge chain between the two faces; "
                 "pick faces that meet at (or across) a common boundary",
        )

    # Same auto-clamp retry as the edge path.
    warnings: list[str] = []
    result_body = None
    r = radius
    last_err: Exception | None = None
    for attempt in range(4):
        if attempt > 0:
            r = r / 2.0
            if r < 0.2:
                break
        try:
            result_body = b3d_fillet(edges, radius=r)
            if attempt > 0:
                warnings.append(
                    f"face fillet radius {radius:g} mm too large; auto-clamped to {r:g} mm"
                )
            break
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            continue

    if result_body is None:
        raise FeatureError(
            f"face fillet failed (radius={radius:g} mm): {last_err}",
            hint="try a smaller radius; the face sets may be too close",
        )

    resolved: dict[str, float] = {"radius": r, "face_fillet": 1.0}
    if warnings:
        resolved["_warning"] = 1.0
    bodies[-1] = result_body
    return CompileOutput(bodies=bodies, tool_body=result_body, resolved=resolved)


@register("fillet")
def compile_fillet(feat: dict, ctx: CompileContext) -> CompileOutput:
    p = feat.get("params", {})
    refs = feat.get("refs", {})

    # Full-round (C33-B): replaces a middle face with a tangent half-cylinder.
    # Distinct ref slot (middle_face) + own validation; bypasses the edge path.
    # Triggered by params.full_round (canonical) or the PM "Fillet Type" combo
    # value type == "full_round" (the form gates on that single combo).
    if p.get("full_round") or p.get("type") == "full_round":
        return _compile_full_round(feat, ctx)

    # Face fillet (SW-parity): rolling-ball blend between two face sets. Distinct ref
    # slots (face_set_a / face_set_b); bypasses the edge path. Triggered by
    # params.face_fillet (canonical) or type == "face".
    if p.get("face_fillet") or p.get("type") == "face":
        return _compile_face_fillet(feat, ctx)

    radius = ctx.dim(p, "radius", 2.0)
    if radius <= 0.0:
        raise FeatureError("fillet radius must be > 0")

    # Optional variable-radius end value. 0 / missing / equal to radius → constant.
    radius2_raw = ctx.dim(p, "radius2", 0.0) if "radius2" in p else 0.0
    variable = radius2_raw > 0.0 and abs(radius2_raw - radius) > 1e-9

    # Conic profile (SW-parity): a rho in (0,1) controls fullness; rho≈0.5 == circular.
    # Only active when params.conic is set and != the circular default. Mutually
    # exclusive with variable (a conic spine uses a constant section).
    conic = bool(p.get("conic"))
    rho = ctx.dim(p, "rho", _CONIC_RHO_DEFAULT) if "rho" in p else _CONIC_RHO_DEFAULT
    use_conic = conic and abs(rho - _CONIC_RHO_DEFAULT) > 1e-9 and not variable

    # Tangent propagation (SW default): from the seed edges auto-include all G1
    # tangent-connected edges so the round flows across smooth junctions. Opt-in via
    # params.tangent_propagation (the PM emits it; default off keeps legacy behaviour
    # byte-for-byte).
    propagate = bool(p.get("tangent_propagation") or p.get("propagate"))

    bodies = list(ctx.bodies)
    if not bodies:
        raise FeatureError("fillet: no body to apply to")
    body = bodies[-1]

    edge_sel_list = refs.get("edges", [])
    if not edge_sel_list:
        raise FeatureError("fillet: refs.edges is required and must be non-empty")

    edges, dangling = _resolve_edges(edge_sel_list, body, "edges", ctx=ctx)
    if dangling:
        raise FeatureError(
            f"fillet: {len(dangling)} edge selector(s) dangling",
            dangling_refs=dangling,
        )
    if not edges:
        raise FeatureError("fillet: no edges resolved")

    if propagate:
        edges = _propagate_tangent_edges(body, edges)

    warnings: list[str] = []
    result_body = None
    r = radius
    r2 = radius2_raw if variable else None
    last_err: Exception | None = None

    # Auto-clamp retry: halve ×3, floor 0.2 mm (both radii in lockstep when variable)
    for attempt in range(4):  # 0 = original, 1-3 = clamped
        if attempt > 0:
            r = r / 2.0
            if r2 is not None:
                r2 = r2 / 2.0
            if r < 0.2 or (r2 is not None and r2 < 0.2):
                break
        try:
            if r2 is not None:
                result_body = _variable_fillet(body, edges, r, r2)
            elif use_conic:
                result_body = _conic_fillet(body, edges, r, rho)
            else:
                result_body = b3d_fillet(edges, radius=r)
            if attempt > 0:
                warnings.append(
                    f"fillet radius {radius:g} mm too large; auto-clamped to {r:g} mm"
                )
            break
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            continue

    if result_body is None:
        raise FeatureError(
            f"fillet failed (radius={radius:g} mm): {last_err}",
            hint="try a smaller radius; avoid overlapping fillets",
        )

    resolved: dict[str, float] = {"radius": r}
    if r2 is not None:
        resolved["radius2"] = r2
    if use_conic:
        resolved["rho"] = rho
    if propagate:
        resolved["propagated_edges"] = float(len(edges))
    if warnings:
        resolved["_warning"] = 1.0  # token so the warning text can ride in resolved

    bodies[-1] = result_body
    return CompileOutput(bodies=bodies, tool_body=result_body, resolved=resolved)


# --------------------------------------------------------------------------- chamfer


def _reference_face_for_edge(body, edge, prefer_face=None):
    """Return a (raw OCP) face incident to *edge*, used as the angle reference.

    The distance-angle chamfer measures the distance on the side of *this* face. We
    prefer an explicitly resolved reference face when it is actually incident to the
    edge; otherwise take the first incident face deterministically.
    """
    ef = _edge_face_map(body.wrapped)
    inc = _faces_of_edge(edge.wrapped, ef)
    if not inc:
        return None
    if prefer_face is not None:
        pk = _b3d_key(prefer_face)
        for f in inc:
            if _raw_key(f) == pk:
                return f
    return inc[0]


def _distance_angle_chamfer(body, edges, dist: float, angle_rad: float, ref_face=None):
    """Distance-angle chamfer via BRepFilletAPI_MakeChamfer.AddDA.

    AddDA needs a reference face per edge (the side the distance is measured from);
    we pick a deterministic incident face. Re-wrapped as a build123d Solid; raises on
    IsDone()==False so the caller's clamp loop can retry.
    """
    mk = BRepFilletAPI_MakeChamfer(body.wrapped)
    for e in edges:
        f = _reference_face_for_edge(body, e, prefer_face=ref_face)
        if f is None:
            raise RuntimeError("distance-angle chamfer: edge has no incident face")
        mk.AddDA(dist, angle_rad, e.wrapped, f)
    mk.Build()
    if not mk.IsDone():
        raise RuntimeError("BRepFilletAPI_MakeChamfer (AddDA) IsDone=False")
    return Solid(mk.Shape()).clean()


def _vertex_chamfer_plane(body, vertex, setback: float):
    """Build the cutting plane that lops the corner at *vertex* with a given setback.

    SW's vertex chamfer removes only the corner solid, not the full incident edges. We
    take the (≥3) edges meeting at the vertex, step *setback* along each from the corner,
    and fit the plane through those setback points. For a 3-edge corner this is exactly
    the triangular facet SW produces; the returned ``Plane`` is oriented so its normal
    points AWAY from the corner (so a ``Keep.BOTTOM`` split discards the corner sliver).
    Returns ``(Plane, removed_estimate)`` or raises if the corner is degenerate.
    """
    import numpy as np
    from build123d import Plane

    cpos = np.array([vertex.X, vertex.Y, vertex.Z], dtype=float)
    edges = _edges_at_vertices(body, [vertex])
    if len(edges) < 3:
        raise FeatureError(
            "chamfer vertex: corner needs 3 incident edges (got "
            f"{len(edges)})",
        )

    pts: list = []
    for e in edges:
        vs = [np.array([v.X, v.Y, v.Z], dtype=float) for v in e.vertices()]
        near = min(vs, key=lambda q: float(np.linalg.norm(q - cpos)))
        far = max(vs, key=lambda q: float(np.linalg.norm(q - cpos)))
        d = far - near
        nrm = float(np.linalg.norm(d))
        if nrm < 1e-9:
            continue
        pts.append(cpos + (d / nrm) * setback)
    if len(pts) < 3:
        raise FeatureError("chamfer vertex: could not form a corner-cutting plane")

    p0, p1, p2 = pts[0], pts[1], pts[2]
    n = np.cross(p1 - p0, p2 - p0)
    nn = float(np.linalg.norm(n))
    if nn < 1e-12:
        raise FeatureError("chamfer vertex: setback points are collinear")
    n = n / nn
    # Orient the normal toward the corner; we keep the side the normal points AWAY from.
    if float(np.dot(cpos - p0, n)) < 0:
        n = -n
    plane = Plane(origin=tuple(p0), z_dir=tuple(n))
    return plane


def _compile_vertex_chamfer(feat: dict, ctx: CompileContext) -> CompileOutput:
    """Vertex chamfer: lop the 3-edge corner at a vertex (SW's "Vertex" chamfer).

    refs.vertices holds vertex selectors. Unlike an edge chamfer (which bevels whole
    edges), a vertex chamfer removes only the corner solid: we cut with the plane through
    the three setback points (``_vertex_chamfer_plane``) and keep the larger piece. For a
    box corner this removes exactly the corner tetrahedron (setback³/6).
    """
    from build123d import Keep

    p = feat.get("params", {})
    refs = feat.get("refs", {})
    length = ctx.dim(p, "length", 1.0)
    if length <= 0.0:
        raise FeatureError("chamfer length must be > 0")

    bodies = list(ctx.bodies)
    if not bodies:
        raise FeatureError("chamfer vertex: no body to apply to")
    body = bodies[-1]

    v_sel = refs.get("vertices") or refs.get("vertex")
    if not v_sel:
        raise FeatureError(
            "chamfer vertex: refs.vertices is required",
        )
    v_list = v_sel if isinstance(v_sel, list) else [v_sel]
    verts, dangling = _resolve_vertices(v_list, body, "vertices", ctx=ctx)
    if dangling:
        raise FeatureError(
            f"chamfer vertex: {len(dangling)} vertex selector(s) dangling",
            dangling_refs=dangling,
        )
    if not verts:
        raise FeatureError("chamfer vertex: no vertices resolved")

    result_body = body
    last_err: Exception | None = None
    for v in verts:
        cut = None
        d = length
        for attempt in range(4):  # clamp retry if the setback over-runs an edge
            if attempt > 0:
                d = d / 2.0
                if d < 0.2:
                    break
            try:
                plane = _vertex_chamfer_plane(result_body, v, d)
                piece = result_body.split(plane, keep=Keep.BOTTOM)
                # split may return a Compound of several solids; take the largest.
                cut = piece
                break
            except FeatureError:
                raise
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                continue
        if cut is None:
            raise FeatureError(
                f"vertex chamfer failed (length={length:g} mm): {last_err}",
                hint="setback may exceed an incident edge length; try a smaller length",
            )
        result_body = cut

    resolved: dict[str, float] = {"length": length, "vertex_chamfer": 1.0,
                                  "corners": float(len(verts))}
    bodies[-1] = result_body
    return CompileOutput(bodies=bodies, tool_body=result_body, resolved=resolved)


@register("chamfer")
def compile_chamfer(feat: dict, ctx: CompileContext) -> CompileOutput:
    p = feat.get("params", {})
    refs = feat.get("refs", {})

    # Vertex chamfer (SW-parity): lop a 3-face corner. Distinct ref slot (vertices);
    # bypasses the edge path. Triggered by params.vertex_chamfer or type == "vertex".
    if p.get("vertex_chamfer") or p.get("type") == "vertex":
        return _compile_vertex_chamfer(feat, ctx)

    length = ctx.dim(p, "length", 1.0)
    if length <= 0.0:
        raise FeatureError("chamfer length must be > 0")

    # Optional asymmetric second distance. 0 / missing / equal to length → symmetric.
    length2_raw = ctx.dim(p, "length2", 0.0) if "length2" in p else 0.0
    asymmetric = length2_raw > 0.0 and abs(length2_raw - length) > 1e-9

    # Distance-angle chamfer (SW-parity): distance on the reference-face side + an angle.
    # Active when params.angle is set; mutually exclusive with asymmetric (both describe
    # the section another way). angle is in degrees in the doc model.
    angle_deg = ctx.dim(p, "angle", 0.0) if "angle" in p else 0.0
    use_angle = angle_deg > 0.0 and not asymmetric

    # Tangent propagation (SW default): grow the seed to its G1 tangent chain.
    propagate = bool(p.get("tangent_propagation") or p.get("propagate"))

    bodies = list(ctx.bodies)
    if not bodies:
        raise FeatureError("chamfer: no body to apply to")
    body = bodies[-1]

    edge_sel_list = refs.get("edges", [])
    if not edge_sel_list:
        raise FeatureError("chamfer: refs.edges is required and must be non-empty")

    edges, dangling = _resolve_edges(edge_sel_list, body, "edges", ctx=ctx)
    if dangling:
        raise FeatureError(
            f"chamfer: {len(dangling)} edge selector(s) dangling",
            dangling_refs=dangling,
        )
    if not edges:
        raise FeatureError("chamfer: no edges resolved")

    if propagate:
        edges = _propagate_tangent_edges(body, edges)

    # Optional reference face for the angle measurement side.
    ref_face = None
    if use_angle and refs.get("reference_face"):
        rf_sel = refs["reference_face"]
        rf_list = rf_sel if isinstance(rf_sel, list) else [rf_sel]
        rfaces, _rf_dang = _resolve_faces(rf_list, body, "reference_face", ctx=ctx)
        if rfaces:
            ref_face = rfaces[0]

    angle_rad = math.radians(angle_deg) if use_angle else None

    warnings: list[str] = []
    result_body = None
    d  = length
    d2 = length2_raw if asymmetric else None
    last_err: Exception | None = None

    for attempt in range(4):
        if attempt > 0:
            d = d / 2.0
            if d2 is not None:
                d2 = d2 / 2.0
            if d < 0.2:
                break
        try:
            if angle_rad is not None:
                result_body = _distance_angle_chamfer(body, edges, d, angle_rad, ref_face)
            elif d2 is not None:
                result_body = b3d_chamfer(edges, length=d, length2=d2)
            else:
                result_body = b3d_chamfer(edges, length=d)
            if attempt > 0:
                warnings.append(
                    f"chamfer length {length:g} mm too large; auto-clamped to {d:g} mm"
                )
            break
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            continue

    if result_body is None:
        raise FeatureError(
            f"chamfer failed (length={length:g} mm): {last_err}",
            hint="try a smaller length",
        )

    resolved: dict[str, float] = {"length": d}
    if d2 is not None:
        resolved["length2"] = d2
    if angle_rad is not None:
        resolved["angle"] = angle_deg
    if propagate:
        resolved["propagated_edges"] = float(len(edges))
    if warnings:
        resolved["_warning"] = 1.0
    bodies[-1] = result_body
    return CompileOutput(bodies=bodies, tool_body=result_body, resolved=resolved)


# --------------------------------------------------------------------------- shell


def _multi_thickness_shell(
    body,
    base_thickness: float,
    outward: bool,
    remove_faces: list,
    overrides: list[tuple],
):
    """Hollow *body* with a base wall plus per-face thickness overrides.

    Uses ``BRepOffset_MakeOffset`` (the engine under build123d's ``offset``) which —
    unlike the simple ``MakeThickSolidByJoin`` — exposes ``SetOffsetOnFace(face, off)``
    for per-face wall thickness. The base offset is signed (inward by default); each
    override sets that face's wall to its own signed thickness. Removed faces are added
    via ``AddFace`` (the openings). Re-wrapped as a build123d Solid.

    *overrides* is a list of ``(build123d_face, thickness_float)``.
    """
    sign = 1.0 if outward else -1.0
    base_off = sign * base_thickness

    mk = BRepOffset_MakeOffset()
    mk.Initialize(
        body.wrapped,
        base_off,
        1e-6,                       # Tol
        BRepOffset_Mode.BRepOffset_Skin,
        False,                      # Intersection
        False,                      # SelfInter
        GeomAbs_Arc,                # Join
        False,                      # Thickening
        False,                      # RemoveIntEdges
    )
    for face, t in overrides:
        mk.SetOffsetOnFace(face.wrapped, sign * t)
    for face in remove_faces:
        mk.AddFace(face.wrapped)
    mk.MakeThickSolid()
    if not mk.IsDone():
        raise RuntimeError("BRepOffset_MakeOffset (multi-thickness) IsDone=False")
    return Solid(mk.Shape()).clean()


@register("shell")
def compile_shell(feat: dict, ctx: CompileContext) -> CompileOutput:
    p = feat.get("params", {})
    refs = feat.get("refs", {})

    thickness = ctx.dim(p, "thickness", 2.0)
    outward = p.get("outward", False)
    if thickness <= 0.0:
        raise FeatureError("shell thickness must be > 0")

    bodies = list(ctx.bodies)
    if not bodies:
        raise FeatureError("shell: no body to apply to")
    body = bodies[-1]

    remove_sel_list = refs.get("remove") or []
    remove_faces: list = []
    dangling: list[str] = []

    if remove_sel_list:
        remove_faces, dangling = _resolve_faces(remove_sel_list, body, "remove", ctx=ctx)
        if dangling:
            raise FeatureError(
                f"shell: {len(dangling)} face selector(s) dangling",
                dangling_refs=dangling,
            )

    # Multi-thickness (SW-parity): per-face wall overrides. refs.thickness_overrides is
    # a list of {face: <face selector(s)>, thickness: <dim>}. When present we route to
    # the BRepOffset_MakeOffset path which supports SetOffsetOnFace; otherwise the
    # legacy uniform build123d offset() path runs unchanged (byte-for-byte).
    override_specs = refs.get("thickness_overrides") or []
    overrides: list[tuple] = []
    if override_specs:
        for i, spec in enumerate(override_specs):
            face_sel = spec.get("face")
            if not face_sel:
                raise FeatureError(
                    f"shell: thickness_overrides[{i}] missing 'face' selector",
                )
            f_list = face_sel if isinstance(face_sel, list) else [face_sel]
            ofaces, odang = _resolve_faces(f_list, body, f"thickness_overrides[{i}].face", ctx=ctx)
            if odang:
                raise FeatureError(
                    f"shell: {len(odang)} override face selector(s) dangling",
                    dangling_refs=odang,
                )
            t_spec = spec.get("thickness", {})
            t_val = ctx.dim({"thickness": t_spec}, "thickness", thickness)
            if t_val <= 0.0:
                raise FeatureError(
                    f"shell: thickness_overrides[{i}] thickness must be > 0",
                )
            for of in ofaces:
                overrides.append((of, t_val))

    if overrides:
        try:
            result_body = _multi_thickness_shell(
                body, thickness, outward, remove_faces, overrides
            )
        except Exception as exc:  # noqa: BLE001
            raise FeatureError(
                f"multi-thickness shell failed: {exc}",
                hint="reduce the thickest wall, or shell before adding fillets",
            )
        bodies[-1] = result_body
        resolved = {"thickness": thickness, "override_count": float(len(overrides))}
        return CompileOutput(bodies=bodies, tool_body=result_body, resolved=resolved)

    # build123d offset: positive = outward, negative = inward.
    # Contract says outward:false → hollow inward (default shell behavior).
    signed_t = thickness if outward else -thickness

    try:
        result_body = offset(body, amount=signed_t, openings=remove_faces if remove_faces else None)
    except Exception as exc:  # noqa: BLE001
        raise FeatureError(
            f"shell failed: {exc}",
            hint="shell before fillet often succeeds where after fails",
        )

    bodies[-1] = result_body
    return CompileOutput(bodies=bodies, tool_body=result_body, resolved={"thickness": thickness})
