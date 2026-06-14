"""Evaluate compute layer — C12 deliverable.

Pure functions over OCP/build123d shapes and BodyTess objects.
No worker wiring; a later card (C19/worker integration) registers the eval_* handlers.

All geometry/units: mm, degrees, grams (canonical: docs/contracts/doc_schema.md §0).

Imports:  ``from engine.doc.evaluate import mass_props, measure, ...``
DO NOT import via ``engine.doc`` package — the __init__ is OCP-free by design.
"""
from __future__ import annotations

import math
from typing import Any, Optional, Sequence

import numpy as np

# ---------------------------------------------------------------------------
# OCP imports (all verified present on OCP 7.8.1 / build123d 0.10.0)
# ---------------------------------------------------------------------------
from OCP.BRepAlgoAPI import BRepAlgoAPI_Section
from OCP.BRepCheck import BRepCheck_Analyzer, BRepCheck_NoError
from OCP.BRepExtrema import BRepExtrema_DistShapeShape
from OCP.BRepGProp import BRepGProp
from OCP.BRepLProp import BRepLProp_SLProps
from OCP.GProp import GProp_GProps
from OCP.GeomAbs import GeomAbs_Plane
from OCP.GeomLProp import GeomLProp_SurfaceTool
from OCP.ShapeAnalysis import ShapeAnalysis_Surface
from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE, TopAbs_FORWARD, TopAbs_REVERSED
from OCP.TopExp import TopExp
from OCP.TopTools import TopTools_IndexedMapOfShape
from OCP.TopoDS import TopoDS
from OCP.BRep import BRep_Tool
from OCP.BRepAdaptor import BRepAdaptor_Surface
from OCP.gp import gp_Dir, gp_Pln, gp_Vec, gp_Pnt

# Local (C02 dependency)
from engine.doc.tessellate import BodyTess, scalar_chunk


# ===========================================================================
# 1. Mass properties
# ===========================================================================

def mass_props(
    shape,
    density_g_cm3: float = 7.85,
) -> dict:
    """Compute volume, mass, centre of mass, and inertia tensor.

    Parameters
    ----------
    shape:
        OCP TopoDS_Shape or build123d Shape.
    density_g_cm3:
        Material density in g/cm³ (default: steel 7.85).

    Returns
    -------
    dict with keys:
        volume_mm3  : float
        mass_g      : float
        com         : [x, y, z]  mm
        inertia     : [9 floats]  kg·mm² (row-major Ixx Ixy Ixz ...)
        material    : {"density_g_cm3": ...}

    SURFACE bodies (open shells / lone faces — not SOLID/COMPSOLID, or solids whose volume is
    ≈0 while their surface area is positive) have no enclosed volume, so this returns an AREA
    report instead: ``{"is_surface": True, "area_mm2", "volume_mm3": 0.0, "mass_g": 0.0,
    "com": <surface centroid>, "inertia": [0]*9, "material": {...}}``. The solid path below is
    untouched, so solid mass-props stay byte-identical.
    """
    raw = _unwrap(shape)

    # Linear density conversion: 1 g/cm³ = 1e-3 g/mm³
    density_g_mm3 = density_g_cm3 * 1e-3

    if _is_surface_shape(raw):
        return _surface_props(raw, density_g_cm3)

    props = GProp_GProps()
    BRepGProp.VolumeProperties_s(raw, props)

    volume_mm3 = props.Mass()  # BRepGProp returns mm³ for unit density
    mass_g = volume_mm3 * density_g_mm3

    com_pt = props.CentreOfMass()
    com = [com_pt.X(), com_pt.Y(), com_pt.Z()]

    # Inertia tensor at CoM
    inertia_mat = props.MatrixOfInertia()
    # MatrixOfInertia returns a gp_Mat (column-major); rows = x,y,z
    inertia: list[float] = []
    for row in range(1, 4):
        for col in range(1, 4):
            inertia.append(inertia_mat.Value(row, col) * density_g_mm3)

    return {
        "volume_mm3": float(volume_mm3),
        "mass_g": float(mass_g),
        "com": [float(v) for v in com],
        "inertia": [float(v) for v in inertia],
        "material": {"density_g_cm3": float(density_g_cm3)},
    }


def _is_surface_shape(raw) -> bool:
    """True when *raw* has no enclosed volume — an open SHELL / lone FACE, or a SOLID whose
    VolumeProperties mass is ≈0 while its SurfaceProperties area is positive.

    A SOLID/COMPSOLID with real volume returns False (the classic mass path). Anything else
    (FACE, SHELL, or a degenerate solid) is treated as a surface so mass_props never divides a
    zero volume or reports a spurious mass. Robust to OCP errors (defaults to solid path)."""
    from OCP.TopAbs import TopAbs_COMPSOLID, TopAbs_SOLID
    try:
        stype = raw.ShapeType()
    except Exception:  # noqa: BLE001
        return False
    if stype in (TopAbs_SOLID, TopAbs_COMPSOLID):
        # A true solid still might be a degenerate sheet (vol≈0, area>0) — treat that as surface.
        try:
            vp = GProp_GProps()
            BRepGProp.VolumeProperties_s(raw, vp)
            sp = GProp_GProps()
            BRepGProp.SurfaceProperties_s(raw, sp)
            return abs(vp.Mass()) < 1e-9 and sp.Mass() > 1e-9
        except Exception:  # noqa: BLE001
            return False
    # A COMPOUND may wrap solids — only call it a surface when it has no solid sub-shape.
    from OCP.TopAbs import TopAbs_COMPOUND
    if stype == TopAbs_COMPOUND:
        try:
            solids = TopTools_IndexedMapOfShape()
            TopExp.MapShapes_s(raw, TopAbs_SOLID, solids)
            if solids.Extent() > 0:
                return False
        except Exception:  # noqa: BLE001
            return False
    # FACE / SHELL / WIRE / solid-free compound → surface report.
    return True


def _surface_props(raw, density_g_cm3: float) -> dict:
    """Area report for a surface body: area (mm²), surface centroid, zero volume/mass/inertia."""
    sp = GProp_GProps()
    BRepGProp.SurfaceProperties_s(raw, sp)
    area_mm2 = float(sp.Mass())  # unit-density surface "mass" = area
    com_pt = sp.CentreOfMass()
    return {
        "is_surface": True,
        "area_mm2": area_mm2,
        "volume_mm3": 0.0,
        "mass_g": 0.0,
        "com": [float(com_pt.X()), float(com_pt.Y()), float(com_pt.Z())],
        "inertia": [0.0] * 9,
        "material": {"density_g_cm3": float(density_g_cm3)},
    }


# ===========================================================================
# 2. Measure
# ===========================================================================

def measure(infos: list) -> dict:
    """Measure geometric quantities for 1–2 shape/topo elements.

    Each entry in ``infos`` is either:
    - an OCP/build123d shape (face, edge, vertex, solid)
    - a dict ``{"shape": ..., "kind": "face"|"edge"|"vertex"|"body"}``

    Supported combinations:
    - 1 edge  → {"kind": "length", "length": mm} or {"kind": "radius", "radius": mm}
    - 1 face  → {"kind": "area", "area": mm²}
    - 2 items → distance (min gap via BRepExtrema) and/or angle (face-face, edge-edge)

    Returns the union shape from worker_rpc.md §2 Evaluate contract.
    """
    shapes = [_resolve_info(i) for i in infos]

    if len(shapes) == 1:
        return _measure_single(shapes[0], infos[0])
    elif len(shapes) == 2:
        return _measure_pair(shapes[0], shapes[1], infos[0], infos[1])
    else:
        raise ValueError(f"measure() takes 1 or 2 items, got {len(infos)}")


def _resolve_info(info) -> Any:
    if isinstance(info, dict):
        return _unwrap(info["shape"])
    return _unwrap(info)


def _unwrap(shape):
    """Return raw OCP TopoDS_Shape; unwrap build123d wrappers transparently."""
    if hasattr(shape, "wrapped"):
        return shape.wrapped
    return shape


def _measure_single(raw, info) -> dict:
    kind = info.get("kind", "") if isinstance(info, dict) else ""
    from OCP.TopAbs import TopAbs_ShapeEnum

    stype = raw.ShapeType()
    from OCP.TopAbs import TopAbs_EDGE as T_EDGE, TopAbs_FACE as T_FACE, TopAbs_VERTEX as T_VERT

    if stype == T_EDGE:
        edge = TopoDS.Edge_s(raw)
        crv_adapt = _curve_adaptor(edge)
        if crv_adapt is not None:
            from OCP.GeomAbs import GeomAbs_Circle
            if crv_adapt.GetType() == GeomAbs_Circle:
                r = crv_adapt.Circle().Radius()
                return {"kind": "radius", "radius": float(r)}
        # Length via GCPnts
        length = _edge_length(edge)
        return {"kind": "length", "length": float(length)}

    elif stype == T_FACE:
        face = TopoDS.Face_s(raw)
        gprops = GProp_GProps()
        BRepGProp.SurfaceProperties_s(raw, gprops)
        area = gprops.Mass()
        return {"kind": "area", "area": float(area)}

    elif stype == T_VERT:
        v = TopoDS.Vertex_s(raw)
        pt = BRep_Tool.Pnt_s(v)
        return {"kind": "point", "points": [[pt.X(), pt.Y(), pt.Z()]]}

    else:
        # Volume properties for solid/compound
        gprops = GProp_GProps()
        BRepGProp.VolumeProperties_s(raw, gprops)
        return {"kind": "volume", "volume": float(gprops.Mass())}


def _measure_pair(raw_a, raw_b, info_a, info_b) -> dict:
    result: dict = {}

    # Distance
    dss = BRepExtrema_DistShapeShape(raw_a, raw_b)
    if dss.IsDone():
        dist = float(dss.Value())
        result["distance"] = dist
        result["kind"] = "distance"
        if dss.NbSolution() > 0:
            p1 = dss.PointOnShape1(1)
            p2 = dss.PointOnShape2(1)
            result["points"] = [
                [p1.X(), p1.Y(), p1.Z()],
                [p2.X(), p2.Y(), p2.Z()],
            ]

    # Angle (face-face or edge-edge)
    angle = _try_angle(raw_a, raw_b)
    if angle is not None:
        result["angle"] = float(angle)
        if "kind" not in result:
            result["kind"] = "angle"

    if not result:
        result["kind"] = "unknown"

    return result


def _try_angle(raw_a, raw_b) -> Optional[float]:
    """Return angle in degrees between two faces or two edges; None if not applicable."""
    from OCP.TopAbs import TopAbs_FACE as T_FACE, TopAbs_EDGE as T_EDGE
    try:
        if raw_a.ShapeType() == T_FACE and raw_b.ShapeType() == T_FACE:
            n1 = _face_normal_at_center(TopoDS.Face_s(raw_a))
            n2 = _face_normal_at_center(TopoDS.Face_s(raw_b))
            if n1 is not None and n2 is not None:
                dot = max(-1.0, min(1.0, n1.Dot(n2)))
                return math.degrees(math.acos(abs(dot)))
        elif raw_a.ShapeType() == T_EDGE and raw_b.ShapeType() == T_EDGE:
            d1 = _edge_tangent_at_mid(TopoDS.Edge_s(raw_a))
            d2 = _edge_tangent_at_mid(TopoDS.Edge_s(raw_b))
            if d1 is not None and d2 is not None:
                dot = max(-1.0, min(1.0, d1.Dot(d2)))
                return math.degrees(math.acos(abs(dot)))
    except Exception:
        pass
    return None


def _face_normal_at_center(face) -> Optional[gp_Dir]:
    try:
        surf_adapt = BRepAdaptor_Surface(face)
        u_mid = (surf_adapt.FirstUParameter() + surf_adapt.LastUParameter()) / 2.0
        v_mid = (surf_adapt.FirstVParameter() + surf_adapt.LastVParameter()) / 2.0
        sl = BRepLProp_SLProps(surf_adapt, u_mid, v_mid, 1, 1e-7)
        if sl.IsNormalDefined():
            return sl.Normal()
    except Exception:
        pass
    return None


def _edge_tangent_at_mid(edge) -> Optional[gp_Dir]:
    try:
        from OCP.BRepAdaptor import BRepAdaptor_Curve
        ca = BRepAdaptor_Curve(edge)
        t_mid = (ca.FirstParameter() + ca.LastParameter()) / 2.0
        from OCP.gp import gp_Pnt as gp_P
        p = gp_P()
        tang = gp_Vec()
        ca.D1(t_mid, p, tang)
        if tang.Magnitude() > 1e-12:
            return gp_Dir(tang)
    except Exception:
        pass
    return None


def _curve_adaptor(edge):
    try:
        from OCP.BRepAdaptor import BRepAdaptor_Curve
        return BRepAdaptor_Curve(edge)
    except Exception:
        return None


def _edge_length(edge) -> float:
    try:
        from OCP.BRepAdaptor import BRepAdaptor_Curve
        from OCP.GCPnts import GCPnts_AbscissaPoint
        ca = BRepAdaptor_Curve(edge)
        return GCPnts_AbscissaPoint.Length_s(ca, ca.FirstParameter(), ca.LastParameter(), 1e-4)
    except Exception:
        return 0.0


# ===========================================================================
# 3. Section properties
# ===========================================================================

def section_props(shape, plane) -> dict:
    """Cross-section at *plane* using BRepAlgoAPI_Section.

    Parameters
    ----------
    shape:
        The solid (OCP or build123d).
    plane:
        OCP TopoDS_Shape representing the cutting plane, OR a build123d Plane/Face,
        OR a gp_Pln.

    Returns
    -------
    dict: area_mm2, centroid:[x,y,z], Ixx, Iyy, Ixy (2nd moments in mm⁴)
    """
    raw_shape = _unwrap(shape)

    # Resolve a gp_Pln for later wire→face build and a TopoDS face for the boolean
    if isinstance(plane, gp_Pln):
        cutting_pln: Optional[gp_Pln] = plane
        from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace
        face_maker = BRepBuilderAPI_MakeFace(plane, -1e4, 1e4, -1e4, 1e4)
        raw_plane = face_maker.Face()
    else:
        raw_plane = _unwrap(plane)
        # Try to recover a gp_Pln from the face
        try:
            adapt_tmp = BRepAdaptor_Surface(TopoDS.Face_s(raw_plane))
            cutting_pln = adapt_tmp.Plane() if adapt_tmp.GetType() == GeomAbs_Plane else None
        except Exception:
            cutting_pln = None

    # Section
    section_op = BRepAlgoAPI_Section(raw_shape, raw_plane)
    section_op.Build()
    if not section_op.IsDone():
        return {"area_mm2": 0.0, "centroid": [0.0, 0.0, 0.0], "Ixx": 0.0, "Iyy": 0.0, "Ixy": 0.0}

    section_shape = section_op.Shape()

    # The section result is typically a COMPOUND of edges (no wires/faces).
    # Strategy:
    #   1. Build a wire from the section edges (BRepBuilderAPI_MakeWire auto-connects).
    #   2. Build a planar face from that wire + the cutting gp_Pln.
    #   3. Compute SurfaceProperties on the face for area/moments.
    #   4. Fallback to LinearProperties for centroid when face build fails.
    try:
        from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace, BRepBuilderAPI_MakeWire
        from OCP.TopAbs import TopAbs_EDGE as T_EDGE

        # Collect edges from the section compound
        edge_map2 = TopTools_IndexedMapOfShape()
        TopExp.MapShapes_s(section_shape, T_EDGE, edge_map2)

        wire_builder = BRepBuilderAPI_MakeWire()
        for ei in range(1, edge_map2.Size() + 1):
            wire_builder.Add(TopoDS.Edge_s(edge_map2.FindKey(ei)))

        area = 0.0
        Ixx = Iyy = Ixy = 0.0
        cx = cy = cz = 0.0

        if wire_builder.IsDone():
            wire = wire_builder.Wire()
            # Build a face on the cutting plane from the wire
            if cutting_pln is not None:
                face_from_section = BRepBuilderAPI_MakeFace(cutting_pln, wire)
            else:
                face_from_section = None

            if face_from_section is not None and face_from_section.IsDone():
                sec_face = face_from_section.Face()
                face_gprops = GProp_GProps()
                BRepGProp.SurfaceProperties_s(sec_face, face_gprops)
                area = float(abs(face_gprops.Mass()))
                com_pt = face_gprops.CentreOfMass()
                cx, cy, cz = com_pt.X(), com_pt.Y(), com_pt.Z()
                mat = face_gprops.MatrixOfInertia()
                Ixx = float(mat.Value(1, 1))
                Iyy = float(mat.Value(2, 2))
                Ixy = float(mat.Value(1, 2))

        # Fallback centroid from linear (wire) props
        if area == 0.0 or (cx == 0.0 and cy == 0.0 and cz == 0.0):
            lin_props = GProp_GProps()
            BRepGProp.LinearProperties_s(section_shape, lin_props)
            com_lin = lin_props.CentreOfMass()
            if cx == 0.0 and cy == 0.0 and cz == 0.0:
                cx, cy, cz = com_lin.X(), com_lin.Y(), com_lin.Z()

        return {
            "area_mm2": float(area),
            "centroid": [float(cx), float(cy), float(cz)],
            "Ixx": float(Ixx),
            "Iyy": float(Iyy),
            "Ixy": float(Ixy),
        }
    except Exception:
        # Ultimate fallback: return wire centroid, zero area/moments
        try:
            wire_props = GProp_GProps()
            BRepGProp.LinearProperties_s(section_shape, wire_props)
            com = wire_props.CentreOfMass()
            return {
                "area_mm2": 0.0,
                "centroid": [float(com.X()), float(com.Y()), float(com.Z())],
                "Ixx": 0.0, "Iyy": 0.0, "Ixy": 0.0,
            }
        except Exception:
            return {"area_mm2": 0.0, "centroid": [0.0, 0.0, 0.0], "Ixx": 0.0, "Iyy": 0.0, "Ixy": 0.0}


# ===========================================================================
# 4. Interference
# ===========================================================================

def interference(bodies: list) -> dict:
    """Pairwise interference check for a list of build123d/OCP shapes.

    Ports the ``_fit_report`` pairwise logic from engine/kernel.py to accept
    a plain list of (name, shape) tuples OR plain shapes (auto-named Body1…N).

    Returns the same dict shape as kernel._fit_report:
        ok, summary, parts:[{name,volume,bbox}], interferences:[{a,b,volume}],
        clearances:[{a,b,distance}]
    """
    from engine.kernel import _fit_report
    members = _normalise_body_list(bodies)
    warnings_sink: list = []
    return _fit_report(members, warnings_sink)


def _normalise_body_list(bodies: list) -> list:
    """Accept [(name, shape), ...] or [shape, ...] → [(name, shape), ...]."""
    out = []
    for i, item in enumerate(bodies):
        if isinstance(item, (tuple, list)) and len(item) == 2 and isinstance(item[0], str):
            name, shape = item
        else:
            name = f"Body{i + 1}"
            shape = item
        # wrap in build123d if needed so _intersect_volume / _min_distance can call .wrapped
        out.append((name, _ensure_b123d(shape)))
    return out


def _ensure_b123d(shape):
    """If shape is a raw OCP TopoDS, wrap it in a build123d Solid (best-effort)."""
    if hasattr(shape, "wrapped"):
        return shape
    # Try to wrap it
    try:
        import build123d as bd
        return bd.Shape(shape)
    except Exception:
        # Return something with .wrapped attribute
        class _Wrap:
            wrapped = shape
        return _Wrap()


# ===========================================================================
# 5. Check
# ===========================================================================

def check(shape) -> dict:
    """Run BRepCheck_Analyzer and free/short edge census.

    Returns
    -------
    dict: {"issues": [{"kind": str, "severity": "error"|"warning", "text": str, "ref": ...}]}
    """
    raw = _unwrap(shape)
    issues: list[dict] = []

    # BRepCheck
    analyzer = BRepCheck_Analyzer(raw)
    if not analyzer.IsValid():
        # Walk all sub-shapes for check results
        _collect_brep_issues(analyzer, raw, issues)

    # Free-edge census: edges with only one adjacent face
    from OCP.TopTools import TopTools_IndexedDataMapOfShapeListOfShape
    anc_map = TopTools_IndexedDataMapOfShapeListOfShape()
    TopExp.MapShapesAndAncestors_s(raw, TopAbs_EDGE, TopAbs_FACE, anc_map)
    edge_map = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(raw, TopAbs_EDGE, edge_map)

    free_edges = 0
    for ei in range(1, edge_map.Size() + 1):
        edge = edge_map.FindKey(ei)
        if anc_map.Contains(edge):
            anc_list = anc_map.FindFromKey(edge)
            n_faces = anc_list.Size()
        else:
            n_faces = 0
        if n_faces < 2:
            free_edges += 1

    if free_edges > 0:
        issues.append({
            "kind": "free_edges",
            "severity": "warning",
            "text": f"{free_edges} free edge(s) detected (open shell)",
        })

    # Tiny-edge / sliver-face census
    face_map = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(raw, TopAbs_FACE, face_map)
    TINY_EDGE_MM = 0.01   # edges shorter than this are flagged
    SLIVER_AREA_MM2 = 0.01

    tiny_edges = 0
    for ei in range(1, edge_map.Size() + 1):
        edge = TopoDS.Edge_s(edge_map.FindKey(ei))
        if _edge_length(edge) < TINY_EDGE_MM:
            tiny_edges += 1
    if tiny_edges > 0:
        issues.append({
            "kind": "tiny_edges",
            "severity": "warning",
            "text": f"{tiny_edges} tiny edge(s) shorter than {TINY_EDGE_MM} mm",
        })

    sliver_faces = 0
    for fi in range(1, face_map.Size() + 1):
        face = face_map.FindKey(fi)
        gp = GProp_GProps()
        BRepGProp.SurfaceProperties_s(face, gp)
        if gp.Mass() < SLIVER_AREA_MM2:
            sliver_faces += 1
    if sliver_faces > 0:
        issues.append({
            "kind": "sliver_faces",
            "severity": "warning",
            "text": f"{sliver_faces} sliver face(s) smaller than {SLIVER_AREA_MM2} mm²",
        })

    return {"issues": issues}


def _collect_brep_issues(analyzer, raw, issues: list) -> None:
    """Walk the BRepCheck_Analyzer result tree and collect error descriptions."""
    try:
        from OCP.BRepCheck import BRepCheck_Result
        from OCP.TopAbs import (
            TopAbs_COMPOUND, TopAbs_COMPSOLID, TopAbs_SOLID,
            TopAbs_SHELL, TopAbs_FACE, TopAbs_WIRE,
        )
        result = analyzer.Result(raw)
        if result is not None:
            result.InitContextIterator()
            while result.MoreContext():
                s = result.ContextualShape()
                result.NextContext()
                try:
                    res2 = analyzer.Result(s)
                    if res2 is not None:
                        res2.Init(s)
                        while res2.More():
                            st = res2.Status()
                            if st != BRepCheck_NoError:
                                issues.append({
                                    "kind": "brep_check",
                                    "severity": "error",
                                    "text": f"BRepCheck status {int(st)}",
                                })
                            res2.Next()
                except Exception:
                    pass
    except Exception:
        pass


# ===========================================================================
# 6. Symmetry
# ===========================================================================

def symmetry(shape, plane) -> dict:
    """Test whether *shape* is symmetric about *plane*.

    plane: gp_Pln, or build123d Plane, or TopoDS_Face (must be planar).

    Returns
    -------
    dict: {"symmetric": bool, "deviation_mm3": float}
    """
    raw = _unwrap(shape)

    # Build mirror transform from the plane
    gp_plane = _to_gp_plane(plane)
    if gp_plane is None:
        raise ValueError("symmetry(): could not extract gp_Pln from plane argument")

    from OCP.gp import gp_Trsf, gp_Ax2, gp_Ax3
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform

    # Mirror about the plane: SetMirror(gp_Ax2) creates planar symmetry
    # gp_Pln.Position() returns gp_Ax3; convert to gp_Ax2 via its XAxis and origin
    pos3 = gp_plane.Position()  # gp_Ax3
    ax2_plane = gp_Ax2(pos3.Location(), pos3.Direction(), pos3.XDirection())

    mirror_trsf2 = gp_Trsf()
    mirror_trsf2.SetMirror(ax2_plane)

    mirrored_builder = BRepBuilderAPI_Transform(raw, mirror_trsf2, True)
    mirrored = mirrored_builder.Shape()

    # Deviation = vol(A - mirror(A)) + vol(mirror(A) - A)
    # Use symmetric difference: vol(A ∩ mirror(A)) as intersection, then deviation = vol(A) + vol(mirror) - 2*vol(intersection)
    try:
        from OCP.BRepAlgoAPI import BRepAlgoAPI_Common, BRepAlgoAPI_Cut

        common_op = BRepAlgoAPI_Common(raw, mirrored)
        common_op.Build()
        cut1_op = BRepAlgoAPI_Cut(raw, mirrored)
        cut1_op.Build()
        cut2_op = BRepAlgoAPI_Cut(mirrored, raw)
        cut2_op.Build()

        vol_a = _vol_or_zero(raw)
        vol_mirror = _vol_or_zero(mirrored)

        if common_op.IsDone():
            vol_common = _vol_or_zero(common_op.Shape())
        else:
            vol_common = 0.0

        deviation = (vol_a - vol_common) + (vol_mirror - vol_common)
        deviation = max(0.0, deviation)

        # Symmetric if deviation is very small relative to total volume
        threshold = 1e-3 * max(vol_a, 1.0)
        return {
            "symmetric": bool(deviation <= threshold),
            "deviation_mm3": float(deviation),
        }
    except Exception as exc:
        return {"symmetric": False, "deviation_mm3": -1.0, "error": str(exc)}


def _to_gp_plane(plane) -> Optional[gp_Pln]:
    """Convert various plane representations to gp_Pln."""
    if isinstance(plane, gp_Pln):
        return plane
    # build123d Plane
    if hasattr(plane, "wrapped"):
        raw = plane.wrapped
    else:
        raw = plane
    if hasattr(raw, "ShapeType"):
        try:
            face = TopoDS.Face_s(raw)
            surf_adapt = BRepAdaptor_Surface(face)
            if surf_adapt.GetType() == GeomAbs_Plane:
                return surf_adapt.Plane()
        except Exception:
            pass
    # build123d Plane object may have origin/x_dir/z_dir attributes
    try:
        from OCP.gp import gp_Ax3, gp_Dir as gD, gp_Pnt as gP
        origin = plane.origin
        normal = plane.z_dir
        loc = gP(float(origin.X), float(origin.Y), float(origin.Z))
        nrm = gD(float(normal.X), float(normal.Y), float(normal.Z))
        return gp_Pln(loc, nrm)
    except Exception:
        pass
    return None


def _vol_or_zero(shape) -> float:
    try:
        gp = GProp_GProps()
        BRepGProp.VolumeProperties_s(shape, gp)
        return max(0.0, float(gp.Mass()))
    except Exception:
        return 0.0


# ===========================================================================
# 7. Per-vertex scalar fields
# ===========================================================================

def draft_field(
    tess: BodyTess,
    shape,
    pull_dir: Sequence[float],
    angle_deg: float = 0.0,
) -> tuple[np.ndarray, float, float, bytes]:
    """Per-vertex draft angle field.

    Computes the angle between each vertex normal and the pull direction.
    Returns signed degrees: positive = face opens toward pull, negative = undercut.
    *angle_deg* is the nominal draft threshold (informational; included in vmin/vmax range).

    Returns
    -------
    (values, vmin, vmax, sclr_chunk_bytes)
    """
    pull = np.asarray(pull_dir, dtype=np.float64)
    pull = pull / (np.linalg.norm(pull) + 1e-12)

    nrm = tess.nrm.astype(np.float64)  # (n, 3)
    nrm_len = np.linalg.norm(nrm, axis=1, keepdims=True)
    nrm_safe = nrm / np.where(nrm_len > 1e-12, nrm_len, 1.0)

    # Angle between vertex normal and pull direction (0–90°)
    # Positive: normal points toward pull (no undercut), negative: undercut
    cos_theta = nrm_safe @ pull  # (n,)
    # Draft angle = 90° - angle_between(normal, pull)
    # = angle between the face surface (tangent plane) and the pull direction
    # Signed: positive means material can be pulled (no lock), negative means undercut
    draft_angles = np.degrees(np.arcsin(np.clip(cos_theta, -1.0, 1.0))).astype(np.float32)

    finite = draft_angles[np.isfinite(draft_angles)]
    vmin = float(finite.min()) if len(finite) else -90.0
    vmax = float(finite.max()) if len(finite) else 90.0
    chunk = scalar_chunk(tess.body_id, "draft", draft_angles, vmin=vmin, vmax=vmax)
    return draft_angles, vmin, vmax, chunk


def thickness_field(
    tess: BodyTess,
    shape,
) -> tuple[np.ndarray, float, float, bytes]:
    """Per-vertex wall thickness estimate via inward ray casting on the tessellation.

    Casts a ray from each surface vertex along −normal; the distance to the first
    re-intersection gives the local wall thickness.

    Primary path: trimesh (available in the venv) with its BVH ray caster.
    Fallback: pure-numpy Möller–Trumbore ray-triangle test when trimesh's rtree
    dependency is absent.  The fallback is O(V*T) and batches over triangles,
    so it is feasible for typical part meshes (<50k verts × <100k tris).

    Returns
    -------
    (values, vmin, vmax, sclr_chunk_bytes)
    """
    pos = tess.pos.astype(np.float64)
    nrm = tess.nrm.astype(np.float64)
    nrm_len = np.linalg.norm(nrm, axis=1, keepdims=True)
    nrm_safe = nrm / np.where(nrm_len > 1e-12, nrm_len, 1.0)

    n_verts = len(pos)
    values = np.full(n_verts, np.nan, dtype=np.float32)

    # Offset slightly inward to avoid self-intersection at the origin vertex
    OFFSET = 1e-3
    origins = pos - nrm_safe * OFFSET
    directions = -nrm_safe  # inward

    hits = _raycast_thickness(
        tess.idx.astype(np.int64),
        pos,
        origins,
        directions,
    )

    for vi in range(n_verts):
        d = hits[vi]
        if d < np.inf and d > OFFSET:
            values[vi] = float(d - OFFSET)

    finite = values[np.isfinite(values)]
    vmin = float(finite.min()) if len(finite) else 0.0
    vmax = float(finite.max()) if len(finite) else 0.0
    chunk = scalar_chunk(tess.body_id, "thickness", values, vmin=vmin, vmax=vmax)
    return values, vmin, vmax, chunk


def _raycast_thickness(
    faces: np.ndarray,     # (T, 3) int64
    verts: np.ndarray,     # (V, 3) float64
    origins: np.ndarray,   # (V, 3) float64
    directions: np.ndarray,  # (V, 3) float64
) -> np.ndarray:
    """Return per-ray nearest intersection distance (inf = no hit).

    Attempts trimesh's vectorised path first; falls back to a chunked
    pure-numpy Möller–Trumbore implementation when rtree is unavailable.
    """
    # --- trimesh path ---
    try:
        import trimesh  # type: ignore
        mesh = trimesh.Trimesh(vertices=verts, faces=faces, process=False)
        locations, index_ray, _ = mesh.ray.intersects_location(
            ray_origins=origins,
            ray_directions=directions,
            multiple_hits=False,
        )
        dist = np.full(len(origins), np.inf)
        for k, ri in enumerate(index_ray):
            d = float(np.linalg.norm(locations[k] - origins[ri]))
            if d < dist[ri]:
                dist[ri] = d
        return dist
    except (ImportError, ModuleNotFoundError, Exception):
        pass

    # --- pure-numpy Möller–Trumbore fallback ---
    return _moller_trumbore_all(verts, faces, origins, directions)


def _moller_trumbore_all(
    verts: np.ndarray,
    faces: np.ndarray,
    origins: np.ndarray,
    directions: np.ndarray,
) -> np.ndarray:
    """Möller–Trumbore ray-triangle intersection, batched over triangles.

    For each ray, records the smallest positive t (distance along ray).
    Processes triangles in chunks to bound peak memory usage.
    """
    EPS = 1e-10
    CHUNK = 4096  # triangles per chunk

    V0 = verts[faces[:, 0]]  # (T, 3)
    V1 = verts[faces[:, 1]]
    V2 = verts[faces[:, 2]]
    E1 = V1 - V0             # (T, 3)
    E2 = V2 - V0             # (T, 3)

    n_rays = len(origins)
    n_tris = len(faces)
    dist = np.full(n_rays, np.inf)

    for t_start in range(0, n_tris, CHUNK):
        t_end = min(t_start + CHUNK, n_tris)
        e1 = E1[t_start:t_end]  # (C, 3)
        e2 = E2[t_start:t_end]  # (C, 3)
        v0 = V0[t_start:t_end]  # (C, 3)

        for ri in range(n_rays):
            d = directions[ri]       # (3,)
            o = origins[ri]          # (3,)

            # h = cross(d, e2)  → (C, 3)
            h = np.cross(d[np.newaxis, :], e2)        # (C, 3)
            a = np.einsum("ci,ci->c", e1, h)          # (C,)
            mask = np.abs(a) > EPS
            if not np.any(mask):
                continue

            inv_a = np.where(mask, 1.0 / np.where(mask, a, 1.0), 0.0)  # (C,)
            s = o[np.newaxis, :] - v0                                    # (C, 3)
            u = inv_a * np.einsum("ci,ci->c", s, h)                     # (C,)

            mask &= (u >= 0.0) & (u <= 1.0)
            if not np.any(mask):
                continue

            q = np.cross(s, e1)                                          # (C, 3)
            v = inv_a * np.einsum("ci,ci->c", q, d[np.newaxis, :])      # (C,)

            mask &= (v >= 0.0) & (u + v <= 1.0)
            if not np.any(mask):
                continue

            t_val = inv_a * np.einsum("ci,ci->c", e2, q)                # (C,)
            mask &= t_val > EPS

            if np.any(mask):
                t_hits = np.where(mask, t_val, np.inf)
                min_t = float(np.min(t_hits))
                if min_t < dist[ri]:
                    dist[ri] = min_t

    return dist


def curvature_field(
    tess: BodyTess,
    shape,
    kind: str = "mean",
) -> tuple[np.ndarray, float, float, bytes]:
    """Per-vertex curvature field using BRepLProp_SLProps exact evaluation.

    Path used: BRepLProp_SLProps at each vertex's UV on its owning face.
    UV is obtained via ShapeAnalysis_Surface.ValueOfUV projection (vertex 3D → UV).
    This matches the path described in C12's card since the BodyTess does not
    currently store UV nodes — we project via ShapeAnalysis_Surface.

    kind: "mean" | "gaussian" | "max"
        mean     = (κ₁ + κ₂) / 2
        gaussian = κ₁ * κ₂
        max      = max(|κ₁|, |κ₂|)

    Returns
    -------
    (values, vmin, vmax, sclr_chunk_bytes)
    """
    raw_shape = _unwrap(shape)

    # Build face map matching tessellate_body's face ordering
    from OCP.BRepMesh import BRepMesh_IncrementalMesh
    BRepMesh_IncrementalMesh(raw_shape, 0.1, False, 0.5, True).Perform()

    face_map = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(raw_shape, TopAbs_FACE, face_map)

    n_verts = len(tess.pos)
    values = np.full(n_verts, np.nan, dtype=np.float32)

    for fr in tess.faces:
        fi = fr.face_id  # 1-based index into face_map
        if fi < 1 or fi > face_map.Size():
            continue
        if fr.tri_count == 0:
            continue

        face = TopoDS.Face_s(face_map.FindKey(fi))
        surf_adapt = BRepAdaptor_Surface(face)

        # Project each vertex in this face's range to UV
        # Vertex range: all vertices in triangles [fr.first_tri .. fr.first_tri + fr.tri_count)
        tri_start = fr.first_tri
        tri_end = fr.first_tri + fr.tri_count
        if tri_end > len(tess.idx):
            tri_end = len(tess.idx)

        vert_indices = np.unique(tess.idx[tri_start:tri_end].flatten())

        # Get the underlying surface via BRep_Tool for ShapeAnalysis_Surface
        from OCP.BRep import BRep_Tool
        from OCP.TopLoc import TopLoc_Location
        loc = TopLoc_Location()
        occ_surf = BRep_Tool.Surface_s(face, loc)
        if occ_surf is None:
            continue

        sas = ShapeAnalysis_Surface(occ_surf)

        u_first = surf_adapt.FirstUParameter()
        u_last = surf_adapt.LastUParameter()
        v_first = surf_adapt.FirstVParameter()
        v_last = surf_adapt.LastVParameter()

        for vi in vert_indices:
            if vi >= n_verts:
                continue
            pt3d = tess.pos[vi]
            gp_pt = gp_Pnt(float(pt3d[0]), float(pt3d[1]), float(pt3d[2]))
            uv = sas.ValueOfUV(gp_pt, 1e-3)
            u_val = float(uv.X())
            v_val = float(uv.Y())
            # Clamp UV to valid domain
            u_val = max(u_first, min(u_last, u_val))
            v_val = max(v_first, min(v_last, v_val))
            try:
                sl = BRepLProp_SLProps(surf_adapt, u_val, v_val, 2, 1e-7)
                if sl.IsCurvatureDefined():
                    k1 = float(sl.MinCurvature())
                    k2 = float(sl.MaxCurvature())
                    if kind == "gaussian":
                        val = k1 * k2
                    elif kind == "max":
                        val = max(abs(k1), abs(k2))
                    else:  # mean
                        val = (k1 + k2) / 2.0
                    values[vi] = float(val)
            except Exception:
                pass

    finite = values[np.isfinite(values)]
    vmin = float(finite.min()) if len(finite) else 0.0
    vmax = float(finite.max()) if len(finite) else 0.0
    chunk = scalar_chunk(tess.body_id, f"curvature_{kind}", values, vmin=vmin, vmax=vmax)
    return values, vmin, vmax, chunk
