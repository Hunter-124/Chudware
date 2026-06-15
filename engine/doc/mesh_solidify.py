"""Convert an imported mesh body into an OCCT B-rep solid (FORMATS_PLAN.md §2 D1 — "Convert to
Solid", the SolidWorks ``Convert to Solid Body`` equivalent).

A :class:`~engine.doc.mesh_body.MeshBody` is a triangle soup with no topology — it renders and
measures, but can't be filleted, cut, or booleaned. :func:`solidify` sews its triangles into a real
OCCT solid (one planar face per triangle → sewn shell → solid), so the result flows through the
normal B-rep pipeline. The trade-off is a high face count (one face per triangle), so it is gated by
a triangle cap; above that, or when the mesh isn't watertight enough to close, it returns ``None``
and the caller keeps the mesh body.

OCP path (verified on OCP 7.8.1): BRepBuilderAPI_MakePolygon (triangle wire) → MakeFace →
BRepBuilderAPI_Sewing(tol) → the sewn SHELL → BRepBuilderAPI_MakeSolid → ShapeFix_Solid (orientation)
→ build123d Solid. Sewing tolerance scales with the bounding-box diagonal so it works at any unit
scale (trimesh already merges coincident vertices on import, so the shell usually closes cleanly).
"""

from __future__ import annotations

# Default cap: one OCCT face per triangle gets expensive fast, so refuse very dense meshes rather
# than hang. ~200k faces sews in a few seconds; beyond that the user should decimate first.
DEFAULT_MAX_TRIS = 200_000


def solidify(mesh, max_tris: int = DEFAULT_MAX_TRIS):
    """Sew ``mesh`` (a MeshBody) into a watertight OCCT solid.

    Returns a build123d ``Solid`` (positive volume, valid topology) on success, or ``None`` when the
    mesh is empty / over the triangle cap / not closeable into a valid solid. Never raises."""
    try:
        import numpy as np

        from OCP.BRep import BRep_Tool  # noqa: F401  (import parity / availability check)
        from OCP.BRepBuilderAPI import (
            BRepBuilderAPI_MakeFace,
            BRepBuilderAPI_MakePolygon,
            BRepBuilderAPI_MakeSolid,
            BRepBuilderAPI_Sewing,
        )
        from OCP.BRepCheck import BRepCheck_Analyzer
        from OCP.BRepGProp import BRepGProp
        from OCP.GProp import GProp_GProps
        from OCP.gp import gp_Pnt
        from OCP.ShapeFix import ShapeFix_Solid
        from OCP.TopAbs import TopAbs_SHELL
        from OCP.TopExp import TopExp
        from OCP.TopoDS import TopoDS
        from OCP.TopTools import TopTools_IndexedMapOfShape
        from build123d import Solid

        verts = np.asarray(mesh.verts, dtype=float).reshape(-1, 3)
        tris = np.asarray(mesh.tris).reshape(-1, 3)
        if len(tris) == 0 or len(tris) > max_tris or len(verts) < 4:
            return None

        diag = float(np.linalg.norm(verts.max(axis=0) - verts.min(axis=0))) if len(verts) else 1.0
        tol = max(diag * 1e-5, 1e-6)

        pnts = [gp_Pnt(float(v[0]), float(v[1]), float(v[2])) for v in verts]
        sew = BRepBuilderAPI_Sewing(tol)
        nfaces = 0
        for t in tris:
            a, b, c = int(t[0]), int(t[1]), int(t[2])
            if a == b or b == c or a == c:
                continue
            poly = BRepBuilderAPI_MakePolygon(pnts[a], pnts[b], pnts[c], True)
            if not poly.IsDone():
                continue
            mf = BRepBuilderAPI_MakeFace(poly.Wire(), True)
            if not mf.IsDone():
                continue
            sew.Add(mf.Face())
            nfaces += 1
        if nfaces == 0:
            return None

        sew.Perform()
        sewn = sew.SewedShape()

        shells = TopTools_IndexedMapOfShape()
        TopExp.MapShapes_s(sewn, TopAbs_SHELL, shells)
        if shells.Extent() == 0:
            return None  # didn't close into a shell — not a solid candidate
        shell = TopoDS.Shell_s(shells.FindKey(1))

        solid = BRepBuilderAPI_MakeSolid(shell).Solid()
        # Orient + close the solid (ShapeFix flips inward-facing shells so volume comes out positive).
        fixer = ShapeFix_Solid(solid)
        fixer.Perform()
        solid = fixer.Solid()

        if not BRepCheck_Analyzer(solid).IsValid():
            return None

        props = GProp_GProps()
        BRepGProp.VolumeProperties_s(solid, props)
        vol = props.Mass()
        if vol < 0:                      # inverted orientation → reverse so volume is positive
            solid = TopoDS.Solid_s(solid.Reversed())
        if abs(vol) < 1e-9:
            return None

        return Solid(solid)
    except Exception:  # noqa: BLE001 — conversion is best-effort; caller keeps the mesh on failure
        return None
