"""AICM v1 tessellation writer and parser (contract: contracts/mesh_format.md).

Exports:
  tessellate_body(shape, body_id, name, color, lin_deflection, ang_deflection) -> BodyTess
  write_aicm(bodies, extra_chunks=()) -> bytes
  write_aicm_file(path, bodies, extra_chunks=())
  read_aicm(data) -> dict
  scalar_chunk(body_id, name, values, vmin=None, vmax=None) -> bytes
  disp_chunk(body_id, scale_hint, vectors) -> bytes
  lines_chunk(name, polylines, scalars=None) -> bytes

OCP call paths (verified on OCP 7.8.1):
  BRepMesh_IncrementalMesh(shape, lin_defl, False, ang_defl, True).Perform()
  TopExp.MapShapes_s for FACE/EDGE/VERTEX index maps
  BRep_Tool.Triangulation_s(face, loc) + loc.Transformation() to world-space nodes
  BRepLib_ToolTriangulatedShape.ComputeNormals_s for per-vertex normals
  Flip normal AND winding when face.Orientation() == TopAbs_REVERSED
  BRep_Tool.PolygonOnTriangulation_s for edge polylines (NOT Polygon3D)
  TopExp.MapShapesAndAncestors_s to find adjacent face triangulation
  GCPnts_TangentialDeflection fallback when PolygonOnTriangulation returns None
  BRepAdaptor_Surface/Curve for surf/curve types
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Sequence

import numpy as np

from OCP.BRep import BRep_Tool
from OCP.BRepAdaptor import BRepAdaptor_Curve, BRepAdaptor_Surface
from OCP.BRepLib import BRepLib_ToolTriangulatedShape
from OCP.BRepMesh import BRepMesh_IncrementalMesh
from OCP.GCPnts import GCPnts_TangentialDeflection
from OCP.GeomAbs import (
    GeomAbs_BSplineCurve,
    GeomAbs_BSplineSurface,
    GeomAbs_Circle,
    GeomAbs_Cone,
    GeomAbs_Cylinder,
    GeomAbs_Ellipse,
    GeomAbs_Line,
    GeomAbs_Plane,
    GeomAbs_Sphere,
    GeomAbs_Torus,
)
from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE, TopAbs_REVERSED, TopAbs_VERTEX
from OCP.TopExp import TopExp
from OCP.TopLoc import TopLoc_Location
from OCP.TopTools import (
    TopTools_IndexedDataMapOfShapeListOfShape,
    TopTools_IndexedMapOfShape,
)
from OCP.TopoDS import TopoDS

# ---------------------------------------------------------------------------
# Surface / curve type codes (mesh_format.md §2)
# ---------------------------------------------------------------------------
_SURF_TYPE: dict = {
    GeomAbs_Plane: 0,
    GeomAbs_Cylinder: 1,
    GeomAbs_Cone: 2,
    GeomAbs_Sphere: 3,
    GeomAbs_Torus: 4,
    GeomAbs_BSplineSurface: 5,
}
_CURVE_TYPE: dict = {
    GeomAbs_Line: 0,
    GeomAbs_Circle: 1,
    GeomAbs_Ellipse: 2,
    GeomAbs_BSplineCurve: 3,
}


# ---------------------------------------------------------------------------
# Data container
# ---------------------------------------------------------------------------
@dataclass
class FaceRec:
    face_id: int
    first_tri: int
    tri_count: int
    surf_type: int  # 0–6


@dataclass
class EdgeRec:
    edge_id: int
    first_pt: int
    pt_count: int
    curve_type: int  # 0–4


@dataclass
class VertRec:
    vertex_id: int
    xyz: tuple[float, float, float]


@dataclass
class BodyTess:
    body_id: int
    name: str
    color: tuple[float, float, float]
    pos: np.ndarray          # shape (n_verts, 3) float32
    nrm: np.ndarray          # shape (n_verts, 3) float32
    idx: np.ndarray          # shape (n_tris, 3) uint32
    faces: list[FaceRec] = field(default_factory=list)
    edges: list[EdgeRec] = field(default_factory=list)
    edge_pts: np.ndarray = field(default_factory=lambda: np.zeros((0, 3), np.float32))
    verts: list[VertRec] = field(default_factory=list)
    # C32-A RealView material (dielectric default = matches the C30 reflection look exactly).
    metallic: float = 0.0
    roughness: float = 0.5


# ---------------------------------------------------------------------------
# Low-level encoding helpers
# ---------------------------------------------------------------------------
def _str(s: str) -> bytes:
    b = s.encode("utf-8")
    return struct.pack("<H", len(b)) + b


def _chunk(fourcc: bytes, payload: bytes) -> bytes:
    assert len(fourcc) == 4
    return fourcc + struct.pack("<Q", len(payload)) + payload


# ---------------------------------------------------------------------------
# Hidden-edge filter (seam + degenerate edges are topology, not visible edges)
# ---------------------------------------------------------------------------
def _is_hidden_edge(edge, anc_map) -> bool:
    """True if *edge* is a seam or degenerate edge that should not be drawn as a model edge.

    A *seam* edge is the curve where a periodic surface (cylinder/cone/torus/sphere) wraps its
    u-parameter back to its start — it bounds a single face on both sides. ``BRep_Tool.IsClosed``
    is the canonical test (the edge carries two pcurves on that one face). A *degenerate* edge is a
    zero-length curve collapsing to a point (a cone apex, a sphere pole). Neither is a feature line
    the user expects to see on a smooth body, so we skip them when emitting edge polylines.
    """
    try:
        if BRep_Tool.Degenerated_s(edge):
            return True
    except Exception:
        pass
    if not anc_map.Contains(edge):
        return False
    faces_list = anc_map.FindFromKey(edge)
    if faces_list.IsEmpty():
        return False
    # A seam edge's only neighbour is the periodic face it wraps, so checking the first ancestor is
    # sufficient; iterate defensively in case OCCT lists more than one.
    try:
        faces = list(faces_list)
    except Exception:
        faces = [faces_list.First()]
    for fs in faces:
        try:
            if BRep_Tool.IsClosed_s(edge, TopoDS.Face_s(fs)):
                return True
        except Exception:
            continue
    return False


# ---------------------------------------------------------------------------
# Tessellate one OCP shape into a BodyTess
# ---------------------------------------------------------------------------
def tessellate_body(
    shape,
    body_id: int,
    name: str,
    color: tuple[float, float, float],
    lin_deflection: float = 0.05,
    ang_deflection: float = 0.35,
    metallic: float = 0.0,
    roughness: float = 0.5,
    two_sided: bool = False,
) -> BodyTess:
    """Tessellate an OCP/build123d shape to AICM geometry.

    Parameters
    ----------
    shape:
        OCP TopoDS_Shape *or* build123d Shape (uses .wrapped automatically).
    body_id:
        Stable integer id for this body within a single rebuild.
    name:
        Display name, e.g. "Body1".
    color:
        sRGB triple in [0, 1].
    lin_deflection, ang_deflection:
        BRepMesh tolerances (mm, radians).
    two_sided:
        When True, after the normal triangle emit each triangle gets a reversed-winding
        duplicate with a flipped normal, so an OPEN surface body renders from both sides
        (an open shell otherwise vanishes when viewed from its back). Solid bodies leave this
        False so their output is byte-identical to before.
    """
    # Accept both OCP shapes and build123d wrappers
    if hasattr(shape, "wrapped"):
        shape = shape.wrapped

    # Mesh the shape
    BRepMesh_IncrementalMesh(shape, lin_deflection, False, ang_deflection, True).Perform()

    # Index maps — stable within a single rebuild
    face_map = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(shape, TopAbs_FACE, face_map)

    edge_map = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(shape, TopAbs_EDGE, edge_map)

    vert_map = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(shape, TopAbs_VERTEX, vert_map)

    # Edge→face ancestor map (for PolygonOnTriangulation)
    anc_map = TopTools_IndexedDataMapOfShapeListOfShape()
    TopExp.MapShapesAndAncestors_s(shape, TopAbs_EDGE, TopAbs_FACE, anc_map)

    # ------------------------------------------------------------------
    # Per-face geometry — triangles MUST be grouped per face (contiguous)
    # ------------------------------------------------------------------
    all_pos: list[float] = []
    all_nrm: list[float] = []
    all_idx: list[int] = []
    face_recs: list[FaceRec] = []

    # Map face shape -> tri object for edge reuse
    face_triangulations: dict = {}

    for fi in range(1, face_map.Size() + 1):
        face = TopoDS.Face_s(face_map.FindKey(fi))
        loc = TopLoc_Location()
        tri = BRep_Tool.Triangulation_s(face, loc)
        if tri is None:
            # No triangulation on this face — record empty range
            face_recs.append(FaceRec(fi, len(all_idx) // 3, 0, 0))
            continue

        BRepLib_ToolTriangulatedShape.ComputeNormals_s(face, tri)
        is_reversed = face.Orientation() == TopAbs_REVERSED
        trsf = loc.Transformation()
        is_identity = loc.IsIdentity()

        first_tri = len(all_idx) // 3

        def _emit_orientation(flip_normals: bool, reverse_winding: bool) -> None:
            """Emit one copy of this face's vertices (+normals) and its triangles.

            ``flip_normals`` negates the per-vertex normal; ``reverse_winding`` swaps triangle
            winding. The normal/winding-vs-REVERSED logic below is XOR-composed with these so the
            two-sided second pass produces the geometric back face (flipped normal + reversed
            winding) of the first pass.
            """
            base_vert = len(all_pos) // 3
            for ni in range(1, tri.NbNodes() + 1):
                nd = tri.Node(ni)
                if not is_identity:
                    nd.Transform(trsf)
                all_pos.extend([nd.X(), nd.Y(), nd.Z()])
                nv = tri.Normal(ni)
                neg = is_reversed ^ flip_normals
                if neg:
                    all_nrm.extend([-nv.X(), -nv.Y(), -nv.Z()])
                else:
                    all_nrm.extend([nv.X(), nv.Y(), nv.Z()])

            for ti in range(1, tri.NbTriangles() + 1):
                n1, n2, n3 = tri.Triangle(ti).Get()
                rev = is_reversed ^ reverse_winding
                if rev:
                    all_idx.extend([base_vert + n1 - 1, base_vert + n3 - 1, base_vert + n2 - 1])
                else:
                    all_idx.extend([base_vert + n1 - 1, base_vert + n2 - 1, base_vert + n3 - 1])

        # First (front) pass — byte-identical to the single-sided emit.
        _emit_orientation(flip_normals=False, reverse_winding=False)
        # Second (back) pass for an open surface body: same vertices, flipped normals + reversed
        # winding, so the surface is visible from behind.
        if two_sided:
            _emit_orientation(flip_normals=True, reverse_winding=True)

        tri_count = tri.NbTriangles() * (2 if two_sided else 1)
        surf_type = _SURF_TYPE.get(BRepAdaptor_Surface(face).GetType(), 6)
        face_recs.append(FaceRec(fi, first_tri, tri_count, surf_type))
        face_triangulations[face_map.FindKey(fi).__hash__()] = (tri, loc)

    pos_arr = np.array(all_pos, dtype=np.float32).reshape(-1, 3)
    nrm_arr = np.array(all_nrm, dtype=np.float32).reshape(-1, 3)
    idx_arr = np.array(all_idx, dtype=np.uint32).reshape(-1, 3)

    # ------------------------------------------------------------------
    # Edge polylines — PolygonOnTriangulation preferred, fallback GCPnts
    # ------------------------------------------------------------------
    edge_recs: list[EdgeRec] = []
    edge_pts_list: list[float] = []

    loc_shared = TopLoc_Location()

    for ei in range(1, edge_map.Size() + 1):
        edge = TopoDS.Edge_s(edge_map.FindKey(ei))
        first_pt = len(edge_pts_list) // 3
        pts_for_edge: list[float] = []

        # Skip non-model edges: seam edges of periodic faces (the "vertical line" running up a
        # cylinder/cone/torus where the surface's u-parametrization wraps back on itself) and
        # degenerate edges (the apex of a cone, the poles of a sphere — zero-length curves). These
        # are real B-rep topology but are NOT visible model edges, so SolidWorks-class viewers do
        # not draw them. We still record the edge with zero points so edge indices stay stable.
        if _is_hidden_edge(edge, anc_map):
            edge_recs.append(EdgeRec(ei, first_pt, 0, 4))
            continue

        # Try PolygonOnTriangulation via any adjacent face
        if anc_map.Contains(edge):
            faces_list = anc_map.FindFromKey(edge)
            if not faces_list.IsEmpty():
                adj_face = TopoDS.Face_s(faces_list.First())
                loc_e = TopLoc_Location()
                adj_tri = BRep_Tool.Triangulation_s(adj_face, loc_e)
                if adj_tri is not None:
                    poly = BRep_Tool.PolygonOnTriangulation_s(edge, adj_tri, loc_e)
                    if poly is not None:
                        trsf_e = loc_e.Transformation()
                        is_id_e = loc_e.IsIdentity()
                        for ni in range(1, poly.NbNodes() + 1):
                            node_idx = poly.Node(ni)
                            nd = adj_tri.Node(node_idx)
                            if not is_id_e:
                                nd.Transform(trsf_e)
                            pts_for_edge.extend([nd.X(), nd.Y(), nd.Z()])

        # Fallback: GCPnts_TangentialDeflection
        if not pts_for_edge:
            try:
                crv = BRepAdaptor_Curve(edge)
                disc = GCPnts_TangentialDeflection(crv, lin_deflection, ang_deflection)
                for ni in range(1, disc.NbPoints() + 1):
                    p = disc.Value(ni)
                    pts_for_edge.extend([p.X(), p.Y(), p.Z()])
            except Exception:
                pass

        if pts_for_edge:
            curve_type = _CURVE_TYPE.get(BRepAdaptor_Curve(edge).GetType(), 4)
            edge_recs.append(
                EdgeRec(ei, first_pt, len(pts_for_edge) // 3, curve_type)
            )
            edge_pts_list.extend(pts_for_edge)
        else:
            # Still record the edge with zero points so index is stable
            edge_recs.append(EdgeRec(ei, first_pt, 0, 4))

    edge_pts_arr = (
        np.array(edge_pts_list, dtype=np.float32).reshape(-1, 3)
        if edge_pts_list
        else np.zeros((0, 3), dtype=np.float32)
    )

    # ------------------------------------------------------------------
    # B-rep vertices
    # ------------------------------------------------------------------
    vert_recs: list[VertRec] = []
    for vi in range(1, vert_map.Size() + 1):
        v = TopoDS.Vertex_s(vert_map.FindKey(vi))
        pt = BRep_Tool.Pnt_s(v)
        vert_recs.append(VertRec(vi, (pt.X(), pt.Y(), pt.Z())))

    return BodyTess(
        body_id=body_id,
        name=name,
        color=color,
        pos=pos_arr,
        nrm=nrm_arr,
        idx=idx_arr,
        faces=face_recs,
        edges=edge_recs,
        edge_pts=edge_pts_arr,
        verts=vert_recs,
        metallic=float(metallic),
        roughness=float(roughness),
    )


# ---------------------------------------------------------------------------
# Mesh-body tessellation (FORMATS_PLAN.md §2 D1) — a triangle soup straight to AICM,
# bypassing BRepMesh. Used for imported STL / OBJ / PLY / 3MF / glTF (and Rhino / SolidWorks
# mesh fallbacks). A MeshBody has no OCCT topology, so there are no per-face/edge B-rep records:
# one FaceRec spans all triangles, and the wireframe is the mesh's naked + sharp-dihedral edges.
# ---------------------------------------------------------------------------
def _mesh_wire_edges(
    verts: np.ndarray,
    tris: np.ndarray,
    angle_deg: float = 35.0,
    max_tris: int = 300_000,
    max_edges: int = 200_000,
) -> tuple[np.ndarray, list[EdgeRec]]:
    """Extract the drawn wireframe of a triangle mesh: every *naked* edge (used by one triangle)
    plus every *sharp* edge (shared by two triangles whose dihedral angle exceeds ``angle_deg``).

    Each drawn edge becomes a 2-point polyline. Returns ``(edge_pts (2K,3) float32, [EdgeRec]*K)``.
    Skips extraction (shaded-only, no wireframe) above ``max_tris`` / ``max_edges`` so a huge import
    stays responsive."""
    from services.formats.mesh_body import face_normals

    if len(tris) == 0 or len(tris) > max_tris:
        return np.zeros((0, 3), dtype=np.float32), []

    # All 3 undirected edges of every triangle, sorted per-row so (a,b)==(b,a) collide.
    e = np.concatenate([tris[:, [0, 1]], tris[:, [1, 2]], tris[:, [2, 0]]], axis=0).astype(np.int64)
    e.sort(axis=1)
    tri_of = np.tile(np.arange(len(tris), dtype=np.int64), 3)

    order = np.lexsort((e[:, 1], e[:, 0]))
    ek = e[order]
    et = tri_of[order]
    # Group boundaries where the (sorted) edge key changes.
    changed = np.any(ek[1:] != ek[:-1], axis=1)
    starts = np.concatenate([[0], np.nonzero(changed)[0] + 1])
    ends = np.concatenate([starts[1:], [len(ek)]])
    counts = ends - starts

    keys = ek[starts]  # one (a,b) per unique edge

    naked = counts == 1
    shared = counts == 2
    other = counts > 2  # non-manifold — always draw

    drawn_mask = naked | other
    if np.any(shared):
        tn = face_normals(verts, tris)
        s_starts = starts[shared]
        ta = et[s_starts]
        tb = et[s_starts + 1]
        dots = np.clip(np.einsum("ij,ij->i", tn[ta], tn[tb]), -1.0, 1.0)
        sharp = np.degrees(np.arccos(dots)) > angle_deg
        shared_idx = np.nonzero(shared)[0]
        drawn_mask = drawn_mask.copy()
        drawn_mask[shared_idx] = sharp

    drawn = keys[drawn_mask]
    if len(drawn) == 0 or len(drawn) > max_edges:
        return np.zeros((0, 3), dtype=np.float32), []

    p0 = verts[drawn[:, 0]]
    p1 = verts[drawn[:, 1]]
    edge_pts = np.stack([p0, p1], axis=1).reshape(-1, 3).astype(np.float32)
    edge_recs = [EdgeRec(edge_id=i + 1, first_pt=2 * i, pt_count=2, curve_type=0)
                 for i in range(len(drawn))]
    return edge_pts, edge_recs


def tessellate_mesh_body(
    mesh,
    body_id: int,
    name: str,
    color: tuple[float, float, float],
    metallic: float = 0.0,
    roughness: float = 0.5,
) -> BodyTess:
    """Tessellate a :class:`services.formats.mesh_body.MeshBody` directly into AICM geometry.

    Unlike :func:`tessellate_body` this does NOT run ``BRepMesh`` — the triangles ARE the mesh. It
    emits one FaceRec spanning all triangles (so a pick selects the whole body), area-weighted
    per-vertex normals for smooth shading, and the naked + sharp-dihedral wireframe."""
    verts = np.asarray(mesh.verts, dtype=np.float32).reshape(-1, 3)
    tris = np.asarray(mesh.tris, dtype=np.uint32).reshape(-1, 3)
    nrm = np.asarray(mesh.vertex_normals(), dtype=np.float32).reshape(-1, 3)
    faces = [FaceRec(face_id=1, first_tri=0, tri_count=int(len(tris)), surf_type=6)]
    edge_pts, edge_recs = _mesh_wire_edges(verts, tris)
    return BodyTess(
        body_id=body_id,
        name=name,
        color=color,
        pos=verts,
        nrm=nrm,
        idx=tris,
        faces=faces,
        edges=edge_recs,
        edge_pts=edge_pts,
        verts=[],
        metallic=float(metallic),
        roughness=float(roughness),
    )


# ---------------------------------------------------------------------------
# AICM binary writer
# ---------------------------------------------------------------------------
def _encode_body(bt: BodyTess) -> bytes:
    n_verts = len(bt.pos)
    n_tris = len(bt.idx)
    pos_flat = bt.pos.astype(np.float32).flatten()
    nrm_flat = bt.nrm.astype(np.float32).flatten()
    idx_flat = bt.idx.astype(np.uint32).flatten()
    return (
        struct.pack("<I", bt.body_id)
        + _str(bt.name)
        + struct.pack("<3f", *bt.color)
        + struct.pack("<II", n_verts, n_tris)
        + pos_flat.tobytes()
        + nrm_flat.tobytes()
        + idx_flat.tobytes()
        # C32-A: RealView material trailer (metallic, roughness). Appended after idx; the BODY chunk
        # is length-prefixed and readers re-sync to the chunk boundary, so pre-C32 readers skip these
        # two floats and post-C32 readers pick them up (back-compat both ways).
        + struct.pack("<2f", float(bt.metallic), float(bt.roughness))
    )


def _encode_face(bt: BodyTess) -> bytes:
    payload = struct.pack("<I", len(bt.faces))
    for fr in bt.faces:
        payload += struct.pack("<IIIB", fr.face_id, fr.first_tri, fr.tri_count, fr.surf_type)
    return payload


def _encode_edge(bt: BodyTess) -> bytes:
    payload = struct.pack("<I", len(bt.edges))
    for er in bt.edges:
        payload += struct.pack("<IIIB", er.edge_id, er.first_pt, er.pt_count, er.curve_type)
    total_pts = len(bt.edge_pts)
    payload += struct.pack("<I", total_pts)
    if total_pts > 0:
        payload += bt.edge_pts.astype(np.float32).flatten().tobytes()
    return payload


def _encode_vert(bt: BodyTess) -> bytes:
    payload = struct.pack("<I", len(bt.verts))
    for vr in bt.verts:
        payload += struct.pack("<I", vr.vertex_id) + struct.pack("<3f", *vr.xyz)
    return payload


def write_aicm(bodies: list[BodyTess], extra_chunks: Sequence[bytes] = ()) -> bytes:
    """Encode a list of BodyTess objects (+ optional pre-encoded extra chunks) to AICM bytes."""
    body_chunks: list[bytes] = []
    for bt in bodies:
        body_chunks.append(_chunk(b"BODY", _encode_body(bt)))
        body_chunks.append(_chunk(b"FACE", _encode_face(bt)))
        body_chunks.append(_chunk(b"EDGE", _encode_edge(bt)))
        body_chunks.append(_chunk(b"VERT", _encode_vert(bt)))

    all_chunks = body_chunks + list(extra_chunks)
    header = b"AICM" + struct.pack("<II", 1, len(all_chunks))
    return header + b"".join(all_chunks)


def write_aicm_file(
    path,
    bodies: list[BodyTess],
    extra_chunks: Sequence[bytes] = (),
) -> None:
    """Write AICM to a file path (str or Path)."""
    Path(path).write_bytes(write_aicm(bodies, extra_chunks))


# ---------------------------------------------------------------------------
# Overlay chunk helpers (for C12 Evaluate, C08 FEA, C07 CFD)
# ---------------------------------------------------------------------------
def scalar_chunk(
    body_id: int,
    name: str,
    values: "np.ndarray | Sequence[float]",
    vmin: Optional[float] = None,
    vmax: Optional[float] = None,
) -> bytes:
    """Build a SCLR chunk payload-wrapped chunk ready for extra_chunks=[].

    values: per-vertex scalars, NaN where no data.
    vmin/vmax default to data min/max (ignoring NaN).
    """
    v = np.asarray(values, dtype=np.float32)
    finite = v[np.isfinite(v)]
    _vmin = float(finite.min()) if len(finite) else 0.0 if vmin is None else float(vmin)
    _vmax = float(finite.max()) if len(finite) else 0.0 if vmax is None else float(vmax)
    if vmin is not None:
        _vmin = float(vmin)
    if vmax is not None:
        _vmax = float(vmax)
    payload = (
        struct.pack("<I", body_id)
        + _str(name)
        + struct.pack("<ff", _vmin, _vmax)
        + struct.pack("<I", len(v))
        + v.tobytes()
    )
    return _chunk(b"SCLR", payload)


def disp_chunk(
    body_id: int,
    scale_hint: float,
    vectors: "np.ndarray | Sequence",
) -> bytes:
    """Build a DISP chunk.  vectors shape (n, 3) or (3*n,)."""
    vec = np.asarray(vectors, dtype=np.float32).flatten()
    n = len(vec) // 3
    payload = (
        struct.pack("<I", body_id)
        + struct.pack("<f", scale_hint)
        + struct.pack("<I", n)
        + vec.tobytes()
    )
    return _chunk(b"DISP", payload)


def lines_chunk(
    name: str,
    polylines: "list[np.ndarray | Sequence]",
    scalars: "Optional[list[np.ndarray | Sequence]]" = None,
) -> bytes:
    """Build a LINS chunk.

    polylines: list of (N_i, 3) arrays or (3*N_i,) flat arrays.
    scalars:   if provided, list of (N_i,) float arrays, one per polyline.
    """
    segs = [np.asarray(pl, dtype=np.float32).reshape(-1, 3) for pl in polylines]
    pts_per_line = [len(s) for s in segs]
    total_pts = sum(pts_per_line)
    all_pts = np.concatenate(segs, axis=0) if segs else np.zeros((0, 3), np.float32)

    has_scalar: int = 1 if scalars is not None else 0
    payload = (
        _str(name)
        + struct.pack("<I", len(polylines))
        + struct.pack(f"<{len(pts_per_line)}I", *pts_per_line)
        + struct.pack("<I", total_pts)
        + all_pts.flatten().tobytes()
        + struct.pack("<B", has_scalar)
    )
    if scalars is not None:
        all_s = np.concatenate([np.asarray(s, dtype=np.float32) for s in scalars])
        payload += all_s.tobytes()
    return _chunk(b"LINS", payload)


# ---------------------------------------------------------------------------
# AICM parser — tolerant of unknown fourccs
# ---------------------------------------------------------------------------
def read_aicm(data: bytes) -> dict:
    """Parse AICM binary, returning a structured dict.

    Returns::

        {
          "version": 1,
          "bodies": [
            {
              "body_id": int,
              "name": str,
              "color": (f,f,f),
              "n_verts": int,
              "n_tris": int,
              "pos": np.ndarray (n_verts, 3),
              "nrm": np.ndarray (n_verts, 3),
              "idx": np.ndarray (n_tris, 3),
              "faces": [{"face_id","first_tri","tri_count","surf_type"}, ...],
              "edges": [{"edge_id","first_pt","pt_count","curve_type"}, ...],
              "edge_pts": np.ndarray (total_pts, 3),
              "verts": [{"vertex_id","xyz"}, ...],
            }, ...
          ],
          "scalars": [{"body_id","name","vmin","vmax","values"}, ...],
          "displacements": [{"body_id","scale_hint","vectors"}, ...],
          "lines": [{"name","polylines":[np.ndarray],"scalars":[np.ndarray]|None}, ...],
        }
    """
    offset = 0

    def _read(n: int) -> bytes:
        nonlocal offset
        chunk_bytes = data[offset : offset + n]
        offset += n
        return chunk_bytes

    def _u32() -> int:
        return struct.unpack_from("<I", _read(4))[0]

    def _u64() -> int:
        return struct.unpack_from("<Q", _read(8))[0]

    def _f32() -> float:
        return struct.unpack_from("<f", _read(4))[0]

    def _read_str() -> str:
        blen = struct.unpack_from("<H", _read(2))[0]
        return _read(blen).decode("utf-8")

    def _floats(n: int) -> np.ndarray:
        return np.frombuffer(_read(n * 4), dtype=np.float32).copy()

    def _uints(n: int) -> np.ndarray:
        return np.frombuffer(_read(n * 4), dtype=np.uint32).copy()

    # File header
    magic = _read(4)
    assert magic == b"AICM", f"Bad magic: {magic!r}"
    version = _u32()
    chunk_count = _u32()

    result: dict = {
        "version": version,
        "bodies": [],
        "scalars": [],
        "displacements": [],
        "lines": [],
    }

    # Current body index for chunks that bind to the preceding BODY
    current_body: Optional[dict] = None

    for _ in range(chunk_count):
        fourcc = _read(4)
        payload_len = _u64()
        payload_start = offset

        if fourcc == b"BODY":
            body_id = _u32()
            name = _read_str()
            color = struct.unpack_from("<3f", _read(12))
            n_verts = _u32()
            n_tris = _u32()
            pos = _floats(3 * n_verts).reshape(n_verts, 3)
            nrm = _floats(3 * n_verts).reshape(n_verts, 3)
            idx = _uints(3 * n_tris).reshape(n_tris, 3)
            # C32-A material trailer: present iff the chunk has >=8 bytes left after idx (old files
            # without it fall back to the dielectric default and are skipped by the boundary re-sync).
            metallic, roughness = 0.0, 0.5
            if offset - payload_start + 8 <= payload_len:
                metallic = _f32()
                roughness = _f32()
            current_body = {
                "body_id": body_id,
                "name": name,
                "color": color,
                "metallic": metallic,
                "roughness": roughness,
                "n_verts": n_verts,
                "n_tris": n_tris,
                "pos": pos,
                "nrm": nrm,
                "idx": idx,
                "faces": [],
                "edges": [],
                "edge_pts": np.zeros((0, 3), dtype=np.float32),
                "verts": [],
            }
            result["bodies"].append(current_body)

        elif fourcc == b"FACE":
            n_faces = _u32()
            faces = []
            for _ in range(n_faces):
                face_id = _u32()
                first_tri = _u32()
                tri_count = _u32()
                surf_type = struct.unpack_from("<B", _read(1))[0]
                faces.append(
                    {
                        "face_id": face_id,
                        "first_tri": first_tri,
                        "tri_count": tri_count,
                        "surf_type": surf_type,
                    }
                )
            if current_body is not None:
                current_body["faces"] = faces

        elif fourcc == b"EDGE":
            n_edges = _u32()
            edges = []
            for _ in range(n_edges):
                edge_id = _u32()
                first_pt = _u32()
                pt_count = _u32()
                curve_type = struct.unpack_from("<B", _read(1))[0]
                edges.append(
                    {
                        "edge_id": edge_id,
                        "first_pt": first_pt,
                        "pt_count": pt_count,
                        "curve_type": curve_type,
                    }
                )
            total_pts = _u32()
            edge_pts = _floats(3 * total_pts).reshape(total_pts, 3) if total_pts > 0 else np.zeros((0, 3), dtype=np.float32)
            if current_body is not None:
                current_body["edges"] = edges
                current_body["edge_pts"] = edge_pts

        elif fourcc == b"VERT":
            n = _u32()
            verts = []
            for _ in range(n):
                vid = _u32()
                xyz = struct.unpack_from("<3f", _read(12))
                verts.append({"vertex_id": vid, "xyz": xyz})
            if current_body is not None:
                current_body["verts"] = verts

        elif fourcc == b"SCLR":
            body_id = _u32()
            name = _read_str()
            vmin = _f32()
            vmax = _f32()
            n = _u32()
            values = _floats(n)
            result["scalars"].append(
                {"body_id": body_id, "name": name, "vmin": vmin, "vmax": vmax, "values": values}
            )

        elif fourcc == b"DISP":
            body_id = _u32()
            scale_hint = _f32()
            n = _u32()
            vectors = _floats(3 * n).reshape(n, 3)
            result["displacements"].append(
                {"body_id": body_id, "scale_hint": scale_hint, "vectors": vectors}
            )

        elif fourcc == b"LINS":
            name = _read_str()
            n_lines = _u32()
            pts_per_line = list(_uints(n_lines))
            total_pts = _u32()
            all_pts = _floats(3 * total_pts).reshape(total_pts, 3) if total_pts > 0 else np.zeros((0, 3), dtype=np.float32)
            has_scalar = struct.unpack_from("<B", _read(1))[0]
            polylines = []
            cursor = 0
            for count in pts_per_line:
                polylines.append(all_pts[cursor : cursor + count])
                cursor += count
            scalars_out: Optional[list] = None
            if has_scalar:
                all_s = _floats(total_pts)
                scalars_out = []
                cursor = 0
                for count in pts_per_line:
                    scalars_out.append(all_s[cursor : cursor + count])
                    cursor += count
            result["lines"].append(
                {"name": name, "polylines": polylines, "scalars": scalars_out}
            )

        else:
            # Unknown fourcc — skip per spec
            _read(payload_len - (offset - payload_start))

        # Advance to next chunk boundary (guards against any parse drift)
        drift = offset - payload_start - payload_len
        if drift != 0:
            # Should not happen; clamp to prevent cascade errors
            offset = payload_start + payload_len

    return result
