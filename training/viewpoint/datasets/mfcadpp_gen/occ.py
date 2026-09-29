"""OCP ports of the generator's OCC helpers (Utils/occ_utils.py, Utils/shape_factory.py face
maps, OCCUtils edge/wire helpers, Features/machining_features.py dihedral helpers).

Shape collections are TopExp ordered and de-duplicated with IsSame semantics (the original's
list_edge went through a Python set of pythonocc hashes, so its order — and every random.choice
over it — changed between runs)."""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from OCP.BRep import BRep_Tool
from OCP.BRepAdaptor import BRepAdaptor_Curve, BRepAdaptor_Surface
from OCP.BRepBuilderAPI import (BRepBuilderAPI_MakeEdge, BRepBuilderAPI_MakeFace,
                                BRepBuilderAPI_MakeVertex, BRepBuilderAPI_MakeWire)
from OCP.BRepCheck import BRepCheck_Analyzer
from OCP.BRepExtrema import BRepExtrema_DistShapeShape
from OCP.BRepGProp import BRepGProp
from OCP.BRepMesh import BRepMesh_IncrementalMesh
from OCP.BRepTools import BRepTools, BRepTools_WireExplorer
from OCP.GCPnts import GCPnts_AbscissaPoint
from OCP.GeomAbs import GeomAbs_CurveType, GeomAbs_SurfaceType
from OCP.GeomLProp import GeomLProp_SLProps
from OCP.GProp import GProp_GProps
from OCP.gp import gp_Dir, gp_Pnt, gp_Vec
from OCP.ShapeAnalysis import ShapeAnalysis_Surface
from OCP.TopAbs import (TopAbs_EDGE, TopAbs_FACE, TopAbs_FORWARD, TopAbs_REVERSED, TopAbs_SOLID,
                        TopAbs_WIRE)
from OCP.TopExp import TopExp, TopExp_Explorer
from OCP.TopLoc import TopLoc_Location
from OCP.TopoDS import TopoDS
from OCP.TopTools import TopTools_IndexedDataMapOfShapeListOfShape, TopTools_IndexedMapOfShape

SURF_NAMES = {GeomAbs_SurfaceType.GeomAbs_Plane: "plane",
              GeomAbs_SurfaceType.GeomAbs_Cylinder: "cylinder",
              GeomAbs_SurfaceType.GeomAbs_Cone: "cone",
              GeomAbs_SurfaceType.GeomAbs_Sphere: "sphere",
              GeomAbs_SurfaceType.GeomAbs_Torus: "torus",
              GeomAbs_SurfaceType.GeomAbs_BSplineSurface: "bspline"}


# ---------------------------------------------------------------------------- collections

def _unique(shape, kind) -> TopTools_IndexedMapOfShape:
    m = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(shape, kind, m)
    return m


def list_face(shape) -> list:
    m = _unique(shape, TopAbs_FACE)
    return [TopoDS.Face_s(m.FindKey(i)) for i in range(1, m.Extent() + 1)]


def list_edge(shape) -> list:
    m = _unique(shape, TopAbs_EDGE)
    return [TopoDS.Edge_s(m.FindKey(i)) for i in range(1, m.Extent() + 1)]


def edge_set(edges) -> TopTools_IndexedMapOfShape:
    m = TopTools_IndexedMapOfShape()
    for e in edges:
        m.Add(e)
    return m


def list_of_shapes(lst) -> list:
    return list(lst)


def face_wires(face) -> list:
    out, seen = [], TopTools_IndexedMapOfShape()
    exp = TopExp_Explorer(face, TopAbs_WIRE)
    while exp.More():
        w = exp.Current()
        exp.Next()
        if not seen.Contains(w):
            seen.Add(w)
            out.append(TopoDS.Wire_s(w))
    return out


def wire_ordered_edges(wire) -> list:
    """OCCUtils WireExplorer.ordered_edges: BRepTools_WireExplorer order, each edge once."""
    out, seen = [], TopTools_IndexedMapOfShape()
    exp = BRepTools_WireExplorer(wire)
    while exp.More():
        e = exp.Current()
        if not seen.Contains(e):
            seen.Add(e)
            out.append(e)
        exp.Next()
    return out


def number_of_solids(shape) -> int:
    return _unique(shape, TopAbs_SOLID).Extent()


def is_valid_single_solid(shape) -> bool:
    return shape is not None and not shape.IsNull() and number_of_solids(shape) == 1 \
        and BRepCheck_Analyzer(shape).IsValid()


# ---------------------------------------------------------------------------- conversions

def as_list(obj) -> list:
    if isinstance(obj, (gp_Pnt, gp_Dir, gp_Vec)):
        return [obj.X(), obj.Y(), obj.Z()]
    p = BRep_Tool.Pnt_s(obj)
    return [p.X(), p.Y(), p.Z()]


def pnt(v) -> gp_Pnt:
    return gp_Pnt(float(v[0]), float(v[1]), float(v[2]))


def gdir(v) -> gp_Dir:
    return gp_Dir(float(v[0]), float(v[1]), float(v[2]))


def first_vertex(edge) -> np.ndarray:
    return np.array(as_list(TopExp.FirstVertex_s(edge, True)), dtype=np.float64)


def last_vertex(edge) -> np.ndarray:
    return np.array(as_list(TopExp.LastVertex_s(edge, True)), dtype=np.float64)


# ---------------------------------------------------------------------------- queries

def is_plane(face) -> bool:
    return BRepAdaptor_Surface(face, True).GetType() == GeomAbs_SurfaceType.GeomAbs_Plane


def is_line(edge) -> bool:
    return BRepAdaptor_Curve(edge).GetType() == GeomAbs_CurveType.GeomAbs_Line


def edge_length(edge) -> float:
    return GCPnts_AbscissaPoint.Length_s(BRepAdaptor_Curve(edge))


def normal_to_face_center(face) -> np.ndarray:
    u_min, u_max, v_min, v_max = BRepTools.UVBounds_s(face)
    surf = BRep_Tool.Surface_s(face)
    n = GeomLProp_SLProps(surf, (u_min + u_max) / 2., (v_min + v_max) / 2., 1, 0.01).Normal()
    if face.Orientation() == TopAbs_REVERSED:
        n.Reverse()
    return np.array([n.X(), n.Y(), n.Z()], dtype=np.float64)


def dist_point_to_edges(the_pnt, edges) -> tuple:
    min_d, nearest = math.inf, None
    vert = BRepBuilderAPI_MakeVertex(pnt(the_pnt)).Vertex()
    for edge in edges:
        dss = BRepExtrema_DistShapeShape(vert, edge)
        if not dss.IsDone() or dss.NbSolution() < 1:
            continue
        if dss.Value() < min_d:
            min_d = dss.Value()
            nearest = as_list(dss.PointOnShape2(1))
    return min_d, nearest


def _point_normal(xyz, face) -> np.ndarray:
    """ask_point_uv2 + ask_point_normal_face."""
    surf = BRep_Tool.Surface_s(face)
    uv = ShapeAnalysis_Surface(surf).ValueOfUV(pnt(xyz), 0.01)
    n = GeomLProp_SLProps(surf, uv.X(), uv.Y(), 1, 1e-6).Normal()
    if face.Orientation() == TopAbs_REVERSED:
        n.Reverse()
    return np.array([n.X(), n.Y(), n.Z()], dtype=np.float64)


def _edge_dihedral(edge, faces) -> float:
    c = BRepAdaptor_Curve(edge)
    tmid = (c.FirstParameter() + c.LastParameter()) / 2
    p, v1 = gp_Pnt(), gp_Vec()
    c.D1(tmid, p, v1)
    mid = [p.X(), p.Y(), p.Z()]
    tangent = np.array([v1.X(), v1.Y(), v1.Z()])
    n0 = _point_normal(mid, faces[0])
    n1 = _point_normal(mid, faces[1])
    cp = np.cross(n0, n1) if edge.Orientation() == TopAbs_FORWARD else np.cross(n1, n0)
    return float(np.sign(np.dot(cp, tangent)))


def concave_edges(shape) -> TopTools_IndexedMapOfShape:
    """Edges whose two faces meet concavely (machining_features._find_concave_edges)."""
    anc = TopTools_IndexedDataMapOfShapeListOfShape()
    TopExp.MapShapesAndAncestors_s(shape, TopAbs_EDGE, TopAbs_FACE, anc)
    out = TopTools_IndexedMapOfShape()
    for edge in list_edge(shape):
        faces = []
        for f in list_of_shapes(anc.FindFromKey(edge)):
            if not any(f.IsSame(g) for g in faces):
                faces.append(f)
        if len(faces) == 1:
            continue
        faces = [TopoDS.Face_s(f) for f in faces]
        if _edge_dihedral(edge, faces) == -1:
            out.Add(edge)
    return out


def triangulate_shape(shape) -> None:
    mesh = BRepMesh_IncrementalMesh(shape, 0.1, False, 0.5, False)
    mesh.Perform()
    if not mesh.IsDone():
        raise RuntimeError("BRepMesh_IncrementalMesh failed")


def triangulation_from_face(face) -> tuple:
    """(pts, triangles, vt_map, et_map) exactly as MachiningFeature._triangulation_from_face."""
    loc = TopLoc_Location()
    tri = BRep_Tool.Triangulation_s(face, loc)
    if tri is None:
        raise RuntimeError("face has no triangulation")
    trsf = loc.Transformation()
    pts = []
    for i in range(1, tri.NbNodes() + 1):
        p = tri.Node(i).Transformed(trsf)
        pts.append([p.X(), p.Y(), p.Z()])
    triangles, vt_map, et_map = [], {}, {}
    for i in range(1, tri.NbTriangles() + 1):
        pids = sorted(n - 1 for n in tri.Triangle(i).Get())
        triangles.append((pids[0], pids[1], pids[2]))
        for pid in pids:
            vt_map.setdefault(pid, []).append(i - 1)
        for e in ((pids[0], pids[1]), (pids[0], pids[2]), (pids[1], pids[2])):
            et_map.setdefault(e, []).append(i - 1)
    return pts, triangles, vt_map, et_map


def triangles_from_faces(faces) -> np.ndarray:
    out = []
    for face in faces:
        pts, triangles, _, _ = triangulation_from_face(face)
        p = np.asarray(pts, dtype=np.float64)
        if triangles:
            out.append(p[np.asarray(triangles)])
    return np.concatenate(out) if out else np.zeros((0, 3, 3))


# ---------------------------------------------------------------------------- construction

def face_polygon(pnts):
    wire = BRepBuilderAPI_MakeWire()
    verts = [BRepBuilderAPI_MakeVertex(pnt(p)).Vertex() for p in pnts]
    for i in range(len(verts)):
        wire.Add(BRepBuilderAPI_MakeEdge(verts[i], verts[(i + 1) % len(verts)]).Edge())
    return BRepBuilderAPI_MakeFace(wire.Wire()).Face()


# ---------------------------------------------------------------------------- labels

def face_geometry(face) -> dict:
    props = GProp_GProps()
    BRepGProp.SurfaceProperties_s(face, props)
    c = props.CentreOfMass()
    return {"surf": SURF_NAMES.get(BRepAdaptor_Surface(face).GetType(), "other"),
            "area": float(props.Mass()), "centroid": [c.X(), c.Y(), c.Z()]}


@dataclass
class FaceLabels:
    """Class index and feature instance of every face of `shape` (list_face order). Replaces the
    original's {TopoDS_Face: label} dict; instance ids are this port's addition."""
    shape: object
    faces: list
    cls: list
    inst: list

    @classmethod
    def uniform(cls_, shape, label: int, instance: int) -> "FaceLabels":
        faces = list_face(shape)
        return cls_(shape, faces, [label] * len(faces), [instance] * len(faces))

    def after(self, maker, new_shape, label: int, instance: int) -> "FaceLabels":
        """shape_factory.map_face_before_and_after_feat + map_from_shape_and_name: surviving and
        modified faces keep their label, every other face of `new_shape` is the new feature's."""
        new_faces = list_face(new_shape)
        index = _unique(new_shape, TopAbs_FACE)
        cls = [None] * len(new_faces)
        inst = [None] * len(new_faces)
        for face, c, k in zip(self.faces, self.cls, self.inst):
            if maker.IsDeleted(face):
                continue
            images = list_of_shapes(maker.Modified(face)) or [face]
            for img in images:
                j = index.FindIndex(img) - 1
                if j < 0 or cls[j] is not None:
                    continue
                cls[j], inst[j] = c, k
        for j in range(len(new_faces)):
            if cls[j] is None:
                cls[j], inst[j] = label, instance
        return FaceLabels(new_shape, new_faces, cls, inst)
