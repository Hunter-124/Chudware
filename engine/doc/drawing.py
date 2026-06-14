"""Drawing documents — 2D orthographic + isometric VIEWS of a part/assembly via HLR (W12).

A ``aicad.drawing/1`` document (doc_schema.md §8) PROJECTS a source part/assembly into 2D views
using OCCT's hidden-line-removal (HLR) algorithm. It is the foundation of the DRAWINGS module:
later waves add annotations/dimensions/BOM (W13) and PDF/DXF output (W14).

This module owns three things:

1. **The drawing-doc lifecycle in the session registry** — ``new_drawing`` builds a blank drawing
   doc that references an OPEN source doc by id; ``DrawingSession`` wraps it next to the part/asm
   :class:`~engine.doc.session.DocSession` registry (a drawing has no compiler/rebuild of its own —
   its "geometry" is the projection of the source's last good bodies, recomputed on fetch so the
   drawing stays associative).

2. **HLR projection** (:func:`project_view`) — given the source's compiled solids and a view
   orientation, run ``HLRBRep_Algo`` / ``HLRBRep_HLRToShape`` and extract VISIBLE + HIDDEN sharp
   edges (plus outline/smooth) as 2D polylines in the sheet plane, scaled by the view scale.

3. **The ``drawing_*`` worker RPCs** (:data:`WORKER_METHODS`) — registered into ``engine.worker``.

Orientation → projection direction (Z-up, matching engine/doc/features/datum.py reserved_datums):

    | orientation | view dir (eye→model) | screen +x | screen +y (= dir × x) |
    |-------------|----------------------|-----------|-----------------------|
    | front       | +Y                   | +X        | +Z                    |
    | back        | -Y                   | -X        | +Z                    |
    | top         | -Z                   | +X        | +Y                    |
    | bottom      | +Z                   | +X        | -Y                    |
    | right       | -X                   | +Y        | +Z                    |
    | left        | +X                   | -Y        | +Z                    |
    | iso         | (1,1,1) eye→origin   | isometric standard                |

The view direction is the HLR projector's line of sight (the ``gp_Ax2`` Z). ``front`` looks along
+Y exactly as ``plane.front`` (normal -Y, so the viewer sits at -Y looking toward +Y). HLR output
arrives already in the projector's local plane (x = screen +x, y = screen +y, z ≈ 0), so a
projected point's (X, Y) IS the sheet coordinate before scaling.

Canonical units mm throughout. Every projection is contained: a failure on one view returns that
view with ``error`` set + empty geometry, never crashing the whole drawing.
"""

from __future__ import annotations

import math
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from engine.doc.ops import OpError
from engine.doc.schema import SCHEMA_DRAWING

# --------------------------------------------------------------------------- sheet sizes

# ISO A-series + ANSI sheet sizes in mm (landscape: width >= height). The drawing geometry does not
# depend on the sheet (views carry absolute sheet-mm positions), but the host needs the page extent
# to lay out the border; we expose it on drawing_get.
SHEET_DIMS_MM: dict[str, tuple[float, float]] = {
    "A4": (297.0, 210.0),
    "A3": (420.0, 297.0),
    "A2": (594.0, 420.0),
    "A1": (841.0, 594.0),
    "A0": (1189.0, 841.0),
    # ANSI
    "A": (279.4, 215.9),
    "B": (431.8, 279.4),
    "C": (558.8, 431.8),
    "D": (863.6, 558.8),
    "E": (1117.6, 863.6),
}


# --------------------------------------------------------------------------- orientation table

# orientation -> (view_dir, screen_x). screen_y is derived as view_dir × screen_x so the frame is
# right-handed and (screen_x, screen_y) is the on-sheet basis. These are the HLR projector axes.
_ORIENT: dict[str, tuple[tuple[float, float, float], tuple[float, float, float]]] = {
    "front":  ((0.0, 1.0, 0.0),  (1.0, 0.0, 0.0)),
    "back":   ((0.0, -1.0, 0.0), (-1.0, 0.0, 0.0)),
    "top":    ((0.0, 0.0, -1.0), (1.0, 0.0, 0.0)),
    "bottom": ((0.0, 0.0, 1.0),  (1.0, 0.0, 0.0)),
    "right":  ((-1.0, 0.0, 0.0), (0.0, 1.0, 0.0)),
    "left":   ((1.0, 0.0, 0.0),  (0.0, -1.0, 0.0)),
    # iso handled specially (eye on (1,1,1), screen_x chosen so +Z points up-ish on the sheet)
}


def _iso_axes() -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    """View dir + screen-x for the standard isometric (SE-up) view.

    Eye at (1,1,1) looking at the origin -> view dir = -(1,1,1) normalized. screen_x is the
    horizontal of the iso frame; screen_y (= dir × x) then points "up" on the sheet with world +Z
    projecting to the vertical, which is the SolidWorks default isometric look.
    """
    import numpy as np

    d = np.array([-1.0, -1.0, -1.0])
    d = d / np.linalg.norm(d)
    up = np.array([0.0, 0.0, 1.0])
    # screen_x = up × dir (horizontal, perpendicular to both the up hint and the line of sight)
    sx = np.cross(up, d)
    sx = sx / np.linalg.norm(sx)
    return (float(d[0]), float(d[1]), float(d[2])), (float(sx[0]), float(sx[1]), float(sx[2]))


def _orient_frame(orientation: str):
    """Return (view_dir, screen_x, screen_y) unit numpy vectors for a named orientation.

    ``screen_y`` = ``view_dir × screen_x`` (the on-sheet +y / "up"). This is the SAME basis
    ``project_view`` hands the HLR projector, exposed so the section/detail code can map a 2D
    sheet point in a parent view back to a 3D world ray (W15+ section/detail views).
    """
    import numpy as np

    if orientation == "iso":
        view_dir, screen_x = _iso_axes()
    else:
        spec = _ORIENT.get(orientation)
        if spec is None:
            raise OpError("invalid_op", f"unknown orientation '{orientation}'",
                          {"reason": "bad orientation"})
        view_dir, screen_x = spec
    d = np.array(view_dir, dtype=float)
    sx = np.array(screen_x, dtype=float)
    d = d / (np.linalg.norm(d) or 1.0)
    sx = sx / (np.linalg.norm(sx) or 1.0)
    sy = np.cross(d, sx)
    sy = sy / (np.linalg.norm(sy) or 1.0)
    return d, sx, sy


# --------------------------------------------------------------------------- HLR projection


def _edge_to_polyline(edge, lin_defl: float, ang_defl: float) -> list[list[float]]:
    """Discretize one projected (2D, z≈0) edge into a list of [x, y] sheet points.

    Lines stay 2-point; curves are sampled with GCPnts_TangentialDeflection. Returns the points in
    PROJECTION-plane coords (already 2D — the projector flattened them); the caller applies scale.
    """
    from OCP.BRepAdaptor import BRepAdaptor_Curve
    from OCP.GCPnts import GCPnts_TangentialDeflection
    from OCP.GeomAbs import GeomAbs_Line

    ad = BRepAdaptor_Curve(edge)
    pts: list[list[float]] = []
    if ad.GetType() == GeomAbs_Line:
        a = ad.Value(ad.FirstParameter())
        b = ad.Value(ad.LastParameter())
        pts = [[a.X(), a.Y()], [b.X(), b.Y()]]
    else:
        try:
            disc = GCPnts_TangentialDeflection(ad, lin_defl, ang_defl)
            for i in range(1, disc.NbPoints() + 1):
                p = disc.Value(i)
                pts.append([p.X(), p.Y()])
        except Exception:  # noqa: BLE001
            a = ad.Value(ad.FirstParameter())
            b = ad.Value(ad.LastParameter())
            pts = [[a.X(), a.Y()], [b.X(), b.Y()]]
    return pts


def _compound_polylines(compound, scale: float, position: tuple[float, float],
                        lin_defl: float, ang_defl: float) -> list[list[list[float]]]:
    """Every edge in an HLR result compound -> a list of scaled, positioned 2D polylines.

    Each point is ``(proj_x * scale + position[0], proj_y * scale + position[1])``: the projector
    output is centered on the model's projected origin, then scaled and translated to the view's
    sheet position. Empty compounds (None / no edges) yield ``[]``.
    """
    from OCP.TopAbs import TopAbs_EDGE
    from OCP.TopExp import TopExp
    from OCP.TopoDS import TopoDS
    from OCP.TopTools import TopTools_IndexedMapOfShape

    if compound is None:
        return []
    emap = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(compound, TopAbs_EDGE, emap)
    px, py = position
    out: list[list[list[float]]] = []
    for i in range(1, emap.Extent() + 1):
        edge = TopoDS.Edge_s(emap.FindKey(i))
        raw = _edge_to_polyline(edge, lin_defl / max(scale, 1e-6), ang_defl)
        if len(raw) < 2:
            continue
        out.append([[p[0] * scale + px, p[1] * scale + py] for p in raw])
    return out


def _bbox_of_polylines(polylines: list[list[list[float]]]) -> Optional[list[float]]:
    """[min_x, min_y, max_x, max_y] over all points, or None when there are none."""
    xs: list[float] = []
    ys: list[float] = []
    for pl in polylines:
        for x, y in pl:
            xs.append(x)
            ys.append(y)
    if not xs:
        return None
    return [min(xs), min(ys), max(xs), max(ys)]


def project_view(shapes: list, orientation: str, scale: float = 1.0,
                 position: tuple[float, float] = (0.0, 0.0), show_hidden: bool = True,
                 view_id: str = "v?") -> dict:
    """HLR-project ``shapes`` (build123d Solids/Compounds) for one view → 2D geometry JSON.

    Returns::

        {"view_id", "scale", "orientation",
         "visible": [polyline, …],   # solid sharp edges + outlines (the drawn-bold lines)
         "hidden":  [polyline, …],   # hidden sharp edges (empty when show_hidden=False)
         "bbox": [min_x, min_y, max_x, max_y] | null,   # in sheet mm
         "ms": <float>,              # HLR timing
         "error": str | null}

    A polyline is a list of ``[x, y]`` points in sheet millimeters (already scaled + positioned).
    Every failure is contained: an HLR/OCCT exception sets ``error`` and returns empty geometry, so
    one bad view never crashes the whole drawing.
    """
    t0 = time.perf_counter()
    base = {"view_id": view_id, "scale": float(scale), "orientation": orientation,
            "visible": [], "hidden": [], "bbox": None, "ms": 0.0, "error": None}
    if not shapes:
        base["error"] = "source has no bodies to project"
        return base

    try:
        from OCP.gp import gp_Ax2, gp_Pnt, gp_Dir
        from OCP.HLRAlgo import HLRAlgo_Projector
        from OCP.HLRBRep import HLRBRep_Algo, HLRBRep_HLRToShape
        from OCP.TopoDS import TopoDS_Compound
        from OCP.BRep import BRep_Builder

        if orientation == "iso":
            view_dir, screen_x = _iso_axes()
        else:
            spec = _ORIENT.get(orientation)
            if spec is None:
                base["error"] = f"unknown orientation '{orientation}'"
                return base
            view_dir, screen_x = spec

        ax = gp_Ax2(gp_Pnt(0.0, 0.0, 0.0),
                    gp_Dir(view_dir[0], view_dir[1], view_dir[2]),
                    gp_Dir(screen_x[0], screen_x[1], screen_x[2]))
        projector = HLRAlgo_Projector(ax)

        algo = HLRBRep_Algo()
        for sh in shapes:
            raw = sh.wrapped if hasattr(sh, "wrapped") else sh
            algo.Add(raw)
        algo.Projector(projector)
        algo.Update()
        algo.Hide()
        hlr = HLRBRep_HLRToShape(algo)

        # VISIBLE: sharp edges (VCompound) + the silhouette outline (OutLineVCompound). Smooth edges
        # (Rg1LineVCompound) are tangent/transition lines — included so a fillet's tangent shows.
        visible_compounds = [hlr.VCompound(), hlr.OutLineVCompound(), hlr.Rg1LineVCompound()]
        hidden_compounds = ([hlr.HCompound(), hlr.OutLineHCompound()] if show_hidden else [])

        lin_defl, ang_defl = 0.05, 0.35
        visible: list[list[list[float]]] = []
        for c in visible_compounds:
            visible.extend(_compound_polylines(c, scale, position, lin_defl, ang_defl))
        hidden: list[list[list[float]]] = []
        for c in hidden_compounds:
            hidden.extend(_compound_polylines(c, scale, position, lin_defl, ang_defl))

        base["visible"] = visible
        base["hidden"] = hidden
        base["bbox"] = _bbox_of_polylines(visible + hidden)
    except Exception as exc:  # noqa: BLE001 — contained: one view's failure never breaks the doc
        base["error"] = f"{type(exc).__name__}: {exc}"
    base["ms"] = round((time.perf_counter() - t0) * 1000.0, 3)
    return base


# --------------------------------------------------------------------------- SECTION views (W15)


def _half_space_cut(raw_shape, normal, point):
    """Cut ``raw_shape`` (a TopoDS shape) by the half-space on the +``normal`` side of the plane
    through ``point``: a ``BRepAlgoAPI_Cut`` against a large box covering that half-space.

    The retained half is the one the section LOOKS AT (we remove the material between the eye and
    the cut so the bore/cavity is revealed). Returns the cut TopoDS shape. ``normal``/``point`` are
    numpy 3-vectors in WORLD mm.
    """
    import numpy as np
    from OCP.gp import gp_Pnt, gp_Ax2, gp_Dir
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut

    n = np.array(normal, dtype=float)
    n = n / (np.linalg.norm(n) or 1.0)
    p = np.array(point, dtype=float)

    # A big box occupying the +n half-space: corner = point, local Z = +n, depth = BIG along +n,
    # spanning ±BIG in the two in-plane directions. Anything on the +n side of the plane is removed.
    BIG = 100000.0
    # An orthonormal in-plane basis (u, v) for the box footprint.
    seed = np.array([1.0, 0.0, 0.0])
    if abs(float(np.dot(seed, n))) > 0.9:
        seed = np.array([0.0, 1.0, 0.0])
    u = np.cross(n, seed)
    u = u / (np.linalg.norm(u) or 1.0)
    # Box origin: shift back along u and v by BIG so the footprint is centered on `point`.
    origin = p - u * BIG - np.cross(n, u) * BIG
    ax = gp_Ax2(gp_Pnt(*origin), gp_Dir(*n), gp_Dir(*u))
    tool = BRepPrimAPI_MakeBox(ax, 2 * BIG, 2 * BIG, BIG).Shape()
    cut = BRepAlgoAPI_Cut(raw_shape, tool)
    cut.Build()
    if not cut.IsDone():
        raise OpError("invalid_op", "section half-space cut failed", {"reason": "boolean failed"})
    return cut.Shape()


def _section_face_polygons(cut_shape, normal, point, screen_x, screen_y, scale, position):
    """Outer-wire polygons of every face of ``cut_shape`` LYING IN the cut plane, projected to the
    section view's sheet frame (``screen_x``/``screen_y``) and scaled/translated like the edges.

    These are the hatched cross-section areas. A face is "in the cut plane" when it is planar, its
    normal is parallel to the section ``normal``, and its centroid lies on the plane. The hole/cavity
    splits the cut face into several pieces — each is returned as its own closed polygon, so the bore
    reads as a gap in the hatch. Returns a list of ``[[x, y], …]`` closed polygons in sheet mm.
    """
    import numpy as np
    from OCP.TopoDS import TopoDS
    from OCP.TopExp import TopExp, TopExp_Explorer
    from OCP.TopAbs import TopAbs_FACE, TopAbs_EDGE
    from OCP.TopTools import TopTools_IndexedMapOfShape
    from OCP.BRepAdaptor import BRepAdaptor_Surface, BRepAdaptor_Curve
    from OCP.GeomAbs import GeomAbs_Plane, GeomAbs_Line
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps
    from OCP.BRepTools import BRepTools
    from OCP.GCPnts import GCPnts_TangentialDeflection

    n = np.array(normal, dtype=float)
    n = n / (np.linalg.norm(n) or 1.0)
    p = np.array(point, dtype=float)
    sx = np.array(screen_x, dtype=float)
    sy = np.array(screen_y, dtype=float)
    px, py = position

    fmap = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(cut_shape, TopAbs_FACE, fmap)
    polys: list[list[list[float]]] = []
    for i in range(1, fmap.Extent() + 1):
        f = TopoDS.Face_s(fmap.FindKey(i))
        surf = BRepAdaptor_Surface(f)
        if surf.GetType() != GeomAbs_Plane:
            continue
        g = GProp_GProps()
        BRepGProp.SurfaceProperties_s(f, g)
        c = g.CentreOfMass()
        cvec = np.array([c.X(), c.Y(), c.Z()])
        fn = surf.Plane().Axis().Direction()
        fnv = np.array([fn.X(), fn.Y(), fn.Z()])
        # in-plane (centroid on the cut plane) AND coplanar (face normal ∥ cut normal)?
        if abs(float(np.dot(cvec - p, n))) > 1e-6:
            continue
        if abs(abs(float(np.dot(fnv, n))) - 1.0) > 1e-6:
            continue
        try:
            ow = BRepTools.OuterWire_s(f)
        except Exception:  # noqa: BLE001
            continue
        exp = TopExp_Explorer(ow, TopAbs_EDGE)
        poly: list[list[float]] = []
        while exp.More():
            e = TopoDS.Edge_s(exp.Current())
            ad = BRepAdaptor_Curve(e)
            pts3 = []
            if ad.GetType() == GeomAbs_Line:
                a = ad.Value(ad.FirstParameter())
                b = ad.Value(ad.LastParameter())
                pts3 = [a, b]
            else:
                try:
                    disc = GCPnts_TangentialDeflection(ad, 0.05 / max(scale, 1e-6), 0.3)
                    pts3 = [disc.Value(k) for k in range(1, disc.NbPoints() + 1)]
                except Exception:  # noqa: BLE001
                    a = ad.Value(ad.FirstParameter())
                    b = ad.Value(ad.LastParameter())
                    pts3 = [a, b]
            for q in pts3:
                v = np.array([q.X(), q.Y(), q.Z()])
                poly.append([float(np.dot(v, sx)) * scale + px,
                             float(np.dot(v, sy)) * scale + py])
            exp.Next()
        if len(poly) >= 3:
            polys.append(poly)
    return polys


def _resolve_section_plane(view: dict, parent_view: dict):
    """Resolve a section view's cut plane → (world_normal, world_point, look_dir, screen_x).

    Two definition forms (both end at a world plane + a look direction = the section view dir):

    * explicit world plane — ``view["plane"] = {normal:[x,y,z], point:[x,y,z]}``. The look direction
      is along the plane normal (you view the cut face-on); screen_x is chosen so the result reads
      upright relative to the parent.
    * a 2D ``cut_line`` in the PARENT view's sheet coords — ``view["cut_line"] = [[x1,y1],[x2,y2]]``
      (sheet mm, the parent's scaled+positioned frame). The two endpoints are un-projected onto the
      parent's view frame to world points; the cut plane CONTAINS the cut-line direction and the
      parent's view direction (a vertical cutting plane through the part), and the section looks along
      the in-plane perpendicular — exactly the SolidWorks "drop a cutting line, look sideways" model.

    Returns numpy vectors. Raises ``OpError`` on a malformed/degenerate definition.
    """
    import numpy as np

    plane = view.get("plane")
    if isinstance(plane, dict) and plane.get("normal") is not None and plane.get("point") is not None:
        n = np.array(plane["normal"], dtype=float)
        pnt = np.array(plane["point"], dtype=float)
        if n.shape != (3,) or pnt.shape != (3,) or float(np.linalg.norm(n)) < 1e-9:
            raise OpError("invalid_op", "section plane.normal/point must be 3-vectors",
                          {"reason": "bad plane"})
        n = n / np.linalg.norm(n)
        look = n
        # screen_x: pick a world axis most perpendicular to the look so the projection is non-degenerate.
        seed = np.array([0.0, 0.0, 1.0])
        if abs(float(np.dot(seed, look))) > 0.9:
            seed = np.array([1.0, 0.0, 0.0])
        sx = np.cross(seed, look)
        sx = sx / (np.linalg.norm(sx) or 1.0)
        return n, pnt, look, sx

    cut_line = view.get("cut_line")
    if not (isinstance(cut_line, (list, tuple)) and len(cut_line) == 2
            and all(isinstance(q, (list, tuple)) and len(q) == 2 for q in cut_line)):
        raise OpError("invalid_op",
                      "section needs a world 'plane' {normal, point} or a 2D 'cut_line' [[x,y],[x,y]]",
                      {"reason": "no plane"})

    p_orient = parent_view.get("orientation", "front")
    p_scale = float(parent_view.get("scale", 1.0)) or 1.0
    p_pos = parent_view.get("position", [0.0, 0.0]) or [0.0, 0.0]
    d, sx_p, sy_p = _orient_frame(p_orient)
    # Un-project a parent sheet-mm point to a world point on the cut plane: the HLR projector centers
    # the model's projected origin at the parent position, so model_world ≈ (sheet - pos)/scale mapped
    # back through (screen_x, screen_y). Depth along the view dir is arbitrary (we build a plane that
    # spans the whole part along the look), so we anchor at the world origin's depth.
    def unproj(qxy):
        u = (float(qxy[0]) - float(p_pos[0])) / p_scale
        v = (float(qxy[1]) - float(p_pos[1])) / p_scale
        return sx_p * u + sy_p * v

    a = unproj(cut_line[0])
    b = unproj(cut_line[1])
    line = b - a
    if float(np.linalg.norm(line)) < 1e-9:
        raise OpError("invalid_op", "section cut_line endpoints coincide", {"reason": "degenerate"})
    line = line / np.linalg.norm(line)
    # The cutting plane contains the on-sheet cut line AND the parent's view direction (it cuts
    # straight INTO the part). Its normal is line × view_dir; the section looks along that normal's
    # in-plane companion — we look perpendicular to the cut line, parallel to the sheet — i.e. along
    # ``line × d`` projected to be perpendicular to the cut plane normal. Concretely:
    #   plane_normal = line × d   (perpendicular to both the cut line and the look-into direction)
    #   look (section view dir)   = plane_normal  (you view the freshly-cut face head-on)
    plane_normal = np.cross(line, d)
    if float(np.linalg.norm(plane_normal)) < 1e-9:
        raise OpError("invalid_op", "section cut line is parallel to the view direction",
                      {"reason": "degenerate"})
    plane_normal = plane_normal / np.linalg.norm(plane_normal)
    look = plane_normal
    point = a  # any point on the cut line lies on the plane
    # screen_x for the section: keep the cut-line direction horizontal on the section sheet.
    sx = line
    return plane_normal, point, look, sx


def project_section(shapes: list, view: dict, parent_view: dict, scale: float = 1.0,
                    position: tuple[float, float] = (0.0, 0.0), show_hidden: bool = False,
                    view_id: str = "v?") -> dict:
    """HLR-project the CUT model for a section view + the hatched section-face polygons.

    Cuts the source ``shapes`` by the section half-space (revealing the cavity), then HLR-projects
    the remaining solid along the section look direction. ``section_faces`` carries the cut-plane
    cross-section outlines (closed polygons, sheet mm) for the host to hatch. Returns the normal
    ViewGeometry shape (``visible``/``hidden``/``bbox``/``ms``/``error``) plus ``section_faces``.
    Every failure is contained (sets ``error``, empties geometry).
    """
    t0 = time.perf_counter()
    base = {"view_id": view_id, "scale": float(scale), "orientation": "section",
            "visible": [], "hidden": [], "section_faces": [], "bbox": None, "ms": 0.0,
            "error": None}
    if not shapes:
        base["error"] = "source has no bodies to project"
        return base
    # Resolve the cut plane OUTSIDE the contained try: a malformed plane/cut_line is a CALLER error
    # and must surface as a structured RPC error, not a quietly-empty view.
    import numpy as np
    normal, point, look, sx = _resolve_section_plane(view, parent_view)
    try:
        from OCP.gp import gp_Ax2, gp_Pnt, gp_Dir
        from OCP.HLRAlgo import HLRAlgo_Projector
        from OCP.HLRBRep import HLRBRep_Algo, HLRBRep_HLRToShape

        # The section look direction is along the cut normal; build the projector frame.
        look = look / (np.linalg.norm(look) or 1.0)
        sx = sx / (np.linalg.norm(sx) or 1.0)
        # Re-orthogonalize screen_x against the look so the gp_Ax2 is valid.
        sx = sx - look * float(np.dot(sx, look))
        if float(np.linalg.norm(sx)) < 1e-9:
            seed = np.array([0.0, 0.0, 1.0])
            if abs(float(np.dot(seed, look))) > 0.9:
                seed = np.array([1.0, 0.0, 0.0])
            sx = np.cross(seed, look)
        sx = sx / np.linalg.norm(sx)
        sy = np.cross(look, sx)
        sy = sy / (np.linalg.norm(sy) or 1.0)

        # Cut each solid by the half-space (remove the material on the +look side, i.e. between the
        # eye and the cut plane), then collect the retained pieces + the section faces.
        cut_shapes = []
        section_faces: list[list[list[float]]] = []
        for sh in shapes:
            raw = sh.wrapped if hasattr(sh, "wrapped") else sh
            try:
                cs = _half_space_cut(raw, look, point)
            except Exception:  # noqa: BLE001 — a body the plane misses entirely keeps its full shape
                cs = raw
            cut_shapes.append(cs)
            section_faces.extend(
                _section_face_polygons(cs, normal, point, sx, sy, scale, position))

        # HLR-project the cut solids along the look direction.
        ax = gp_Ax2(gp_Pnt(0.0, 0.0, 0.0), gp_Dir(*look), gp_Dir(*sx))
        projector = HLRAlgo_Projector(ax)
        algo = HLRBRep_Algo()
        for cs in cut_shapes:
            algo.Add(cs)
        algo.Projector(projector)
        algo.Update()
        algo.Hide()
        hlr = HLRBRep_HLRToShape(algo)
        visible_compounds = [hlr.VCompound(), hlr.OutLineVCompound(), hlr.Rg1LineVCompound()]
        hidden_compounds = ([hlr.HCompound(), hlr.OutLineHCompound()] if show_hidden else [])
        lin_defl, ang_defl = 0.05, 0.35
        visible: list[list[list[float]]] = []
        for c in visible_compounds:
            visible.extend(_compound_polylines(c, scale, position, lin_defl, ang_defl))
        hidden: list[list[list[float]]] = []
        for c in hidden_compounds:
            hidden.extend(_compound_polylines(c, scale, position, lin_defl, ang_defl))

        base["visible"] = visible
        base["hidden"] = hidden
        base["section_faces"] = section_faces
        base["bbox"] = _bbox_of_polylines(visible + hidden + section_faces)
        if not section_faces:
            base["error"] = "cut plane does not intersect the model"
    except Exception as exc:  # noqa: BLE001 — contained
        base["error"] = f"{type(exc).__name__}: {exc}"
    base["ms"] = round((time.perf_counter() - t0) * 1000.0, 3)
    return base


# --------------------------------------------------------------------------- DETAIL views (W15)


def _clip_polyline_to_circle(pl, cx, cy, r):
    """Clip a sheet-mm polyline to the disc of radius ``r`` about (``cx``,``cy``); return the list of
    sub-polylines (in the same coords) that lie inside. Segment endpoints crossing the circle are
    truncated at the intersection so the detail crop has clean edges (SolidWorks detail-circle clip).
    """
    import math as _m

    r2 = r * r

    def inside(pt):
        return (pt[0] - cx) ** 2 + (pt[1] - cy) ** 2 <= r2

    def intersect(p0, p1):
        # Parametric point p0 + t*(p1-p0) hitting the circle; return the t in [0,1] for the crossing
        # nearest p0 that lands on the boundary (the relevant root for an entering/leaving segment).
        dx, dy = p1[0] - p0[0], p1[1] - p0[1]
        fx, fy = p0[0] - cx, p0[1] - cy
        a = dx * dx + dy * dy
        if a < 1e-12:
            return None
        b = 2 * (fx * dx + fy * dy)
        c = fx * fx + fy * fy - r2
        disc = b * b - 4 * a * c
        if disc < 0:
            return None
        sq = _m.sqrt(disc)
        roots = sorted(t for t in ((-b - sq) / (2 * a), (-b + sq) / (2 * a)) if 0.0 <= t <= 1.0)
        return roots[0] if roots else None

    out: list[list[list[float]]] = []
    cur: list[list[float]] = []
    for i in range(len(pl)):
        pt = pl[i]
        if inside(pt):
            if not cur and i > 0:
                # entering: add the crossing point from the previous (outside) vertex.
                t = intersect(pl[i - 1], pt)
                if t is not None:
                    cur.append([pl[i - 1][0] + t * (pt[0] - pl[i - 1][0]),
                                pl[i - 1][1] + t * (pt[1] - pl[i - 1][1])])
            cur.append([pt[0], pt[1]])
        else:
            if cur:
                # leaving: truncate at the crossing from the previous (inside) vertex.
                t = intersect(pl[i - 1], pt)
                if t is not None:
                    cur.append([pl[i - 1][0] + t * (pt[0] - pl[i - 1][0]),
                                pl[i - 1][1] + t * (pt[1] - pl[i - 1][1])])
                if len(cur) >= 2:
                    out.append(cur)
                cur = []
            else:
                # both endpoints outside — the segment may still pass THROUGH the disc (chord).
                if i > 0:
                    p0, p1 = pl[i - 1], pt
                    dx, dy = p1[0] - p0[0], p1[1] - p0[1]
                    fx, fy = p0[0] - cx, p0[1] - cy
                    a = dx * dx + dy * dy
                    if a >= 1e-12:
                        b = 2 * (fx * dx + fy * dy)
                        cc = fx * fx + fy * fy - r2
                        dsc = b * b - 4 * a * cc
                        if dsc > 0:
                            sq = _m.sqrt(dsc)
                            t0 = (-b - sq) / (2 * a)
                            t1 = (-b + sq) / (2 * a)
                            if 0.0 <= t0 <= 1.0 and 0.0 <= t1 <= 1.0 and t1 > t0:
                                out.append([
                                    [p0[0] + t0 * dx, p0[1] + t0 * dy],
                                    [p0[0] + t1 * dx, p0[1] + t1 * dy],
                                ])
    if len(cur) >= 2:
        out.append(cur)
    return out


def project_detail(parent_geom: dict, center, radius: float, scale: float,
                   position: tuple[float, float], view_id: str = "v?") -> dict:
    """A magnified detail crop of a parent view's already-projected geometry.

    Reuses the parent's HLR polylines (no re-projection): clips both ``visible`` and ``hidden`` to
    the circle of ``radius`` about ``center`` (both in the parent's sheet mm), then RE-ORIGINS the
    kept geometry on the circle center, applies the detail ``scale`` magnification (relative to the
    parent's own scale), and translates to the detail view ``position``. Returns the normal
    ViewGeometry shape. The clipped detail is associative because it derives from the live parent
    projection on every fetch.
    """
    t0 = time.perf_counter()
    base = {"view_id": view_id, "scale": float(scale), "orientation": "detail",
            "visible": [], "hidden": [], "bbox": None, "ms": 0.0, "error": None}
    cx, cy = float(center[0]), float(center[1])
    r = float(radius)
    if r <= 0:
        base["error"] = "detail radius must be positive"
        base["ms"] = round((time.perf_counter() - t0) * 1000.0, 3)
        return base
    parent_scale = float(parent_geom.get("scale", 1.0)) or 1.0
    # Magnification of the parent geometry: the detail is drawn at `scale`, the parent at
    # `parent_scale`, so a kept sheet-mm point is re-scaled by (scale / parent_scale) about center.
    mag = scale / parent_scale
    px, py = float(position[0]), float(position[1])

    def remap(pl):
        return [[(x - cx) * mag + px, (y - cy) * mag + py] for x, y in pl]

    try:
        for layer in ("visible", "hidden"):
            src = parent_geom.get(layer, []) or []
            kept: list[list[list[float]]] = []
            for pl in src:
                for sub in _clip_polyline_to_circle(pl, cx, cy, r):
                    if len(sub) >= 2:
                        kept.append(remap(sub))
            base[layer] = kept
        base["bbox"] = _bbox_of_polylines(base["visible"] + base["hidden"])
        if not base["visible"] and not base["hidden"]:
            base["error"] = "detail circle encloses no parent geometry"
    except Exception as exc:  # noqa: BLE001 — contained
        base["error"] = f"{type(exc).__name__}: {exc}"
    base["ms"] = round((time.perf_counter() - t0) * 1000.0, 3)
    return base


# --------------------------------------------------------------------------- drawing session


@dataclass
class DrawingSession:
    """A live drawing document. Holds the doc JSON + a per-(source_rev, orientation, scale) HLR
    cache so re-fetching an unchanged view is cheap. The drawing has no geometry of its own — it
    projects ``source_doc``'s last good bodies on fetch."""

    doc_id: str
    doc: dict
    source_doc_id: str
    revision: int = 0
    path: Optional[str] = None
    dirty: bool = False
    # cache: (source_revision, orientation, round(scale,6), show_hidden) -> projected-view dict
    _proj_cache: dict[tuple, dict] = field(default_factory=dict)


_DRAWINGS: dict[str, DrawingSession] = {}


def get_drawing(doc_id: str) -> DrawingSession:
    s = _DRAWINGS.get(doc_id)
    if s is None:
        raise OpError("unknown_doc", f"no open drawing '{doc_id}'", {"doc_id": doc_id})
    return s


def close_drawing(doc_id: str) -> bool:
    return _DRAWINGS.pop(doc_id, None) is not None


def _blank_drawing(name: str, source_doc_id: str) -> dict:
    """A new drawing doc with one default A3 sheet and no views."""
    return {
        "schema": SCHEMA_DRAWING,
        "id": "doc-" + uuid.uuid4().hex[:8],
        "name": name,
        "units": {"system": "MMGS"},
        "source": source_doc_id,
        "sheets": [{"id": "s1", "size": "A3", "views": []}],
    }


# --------------------------------------------------------------------------- persistence (W12+)
#
# A DrawingDocument stores NO geometry — only the source-doc reference + the view definitions and
# annotations (all plain JSON). So a drawing round-trips through a part/asm ``.step+`` as the doc
# JSON: stepplus.save embeds one ``drawings/<drawing_id>.json`` per open drawing whose ``source``
# is the saved doc; stepplus.open re-registers each as a fresh DrawingSession and re-points its
# ``source`` to the reopened part's NEW doc_id (the old source id is meaningless once the part is
# reloaded under a new session id).


def drawings_for_source(source_doc_id: str) -> list[DrawingSession]:
    """Every open drawing session whose ``source`` is ``source_doc_id`` (save-time embed list)."""
    return [s for s in _DRAWINGS.values() if s.source_doc_id == source_doc_id]


def serialize_drawing(s: DrawingSession) -> dict:
    """The DrawingDocument JSON for one open drawing (a deep copy — pure data, no geometry)."""
    import copy
    return copy.deepcopy(s.doc)


def register_restored_drawing(doc: dict, source_doc_id: str) -> DrawingSession:
    """Adopt a DrawingDocument restored from a ``.step+`` as a FRESH DrawingSession.

    Assigns a brand-new drawing doc_id (the persisted ``doc['id']`` is the in-document id, not the
    session registry key) and re-points the doc's ``source`` to ``source_doc_id`` — the reopened
    part/assembly's new session id — so the drawing projects against the just-reloaded bodies. The
    per-projection cache starts empty (it's keyed on the live source revision). Returns the session.
    """
    import copy
    d = copy.deepcopy(doc)
    d["source"] = source_doc_id
    doc_id = uuid.uuid4().hex[:12]
    s = DrawingSession(doc_id=doc_id, doc=d, source_doc_id=source_doc_id, revision=0)
    _DRAWINGS[doc_id] = s
    return s


# --------------------------------------------------------------------------- source geometry


def _source_shapes(source_doc_id: str) -> tuple[list, int]:
    """Resolve an open source part/assembly's last good bodies + its current revision.

    For a part: the compiled solid bodies. For an assembly: world-placed component bodies (so the
    projection reflects the mated pose). Raises ``OpError`` when the source is not an open session.
    """
    from engine.doc import session as docsess

    s = docsess.get_session(source_doc_id)  # raises unknown_doc if absent
    if s.is_assembly:
        members = docsess.asm_world_bodies(s, explode=False)
        return [shape for _name, shape in members], s.revision
    result = docsess._ensure_built(s)
    return [bo.shape for bo in result.bodies], s.revision


# --------------------------------------------------------------------------- view helpers


def _all_views(doc: dict) -> list[tuple[dict, dict]]:
    """Flatten to (sheet, view) pairs across all sheets, in document order."""
    out: list[tuple[dict, dict]] = []
    for sh in doc.get("sheets", []) or []:
        for v in sh.get("views", []) or []:
            out.append((sh, v))
    return out


def _find_view(doc: dict, view_id: str) -> tuple[Optional[dict], Optional[dict]]:
    for sh, v in _all_views(doc):
        if v.get("id") == view_id:
            return sh, v
    return None, None


def _find_sheet(doc: dict, sheet_id: Optional[str]) -> dict:
    sheets = doc.get("sheets", []) or []
    if sheet_id is None:
        if not sheets:
            raise OpError("invalid_op", "drawing has no sheets", {"reason": "no sheet"})
        return sheets[0]
    for sh in sheets:
        if sh.get("id") == sheet_id:
            return sh
    raise OpError("invalid_op", f"no sheet '{sheet_id}'", {"reason": "unknown sheet"})


def _next_view_id(doc: dict) -> str:
    hi = 0
    for _sh, v in _all_views(doc):
        vid = v.get("id", "")
        if isinstance(vid, str) and vid.startswith("v") and vid[1:].isdigit():
            hi = max(hi, int(vid[1:]))
    return f"v{hi + 1}"


def _project_one(s: DrawingSession, view: dict, src_shapes: list, src_rev: int) -> dict:
    """Project a single view, using/filling the per-(rev, orient, scale, hidden) cache.

    Dispatches on the view ``type``: ``section`` cuts the model then HLR-projects the cut (+ section
    faces); ``detail`` clips its parent view's projected geometry to a magnifier circle. Ortho/iso
    views go through the cached :func:`project_view`. Section/detail are NOT position-cached (each is
    cheap relative to a full HLR, or derives from the live parent), so they re-project on every fetch
    and stay associative.
    """
    vtype = view.get("type")
    scale = float(view.get("scale", 1.0))
    show_hidden = bool(view.get("show_hidden", True))
    pos = view.get("position", [0.0, 0.0]) or [0.0, 0.0]

    if vtype == "section":
        parent_view = _resolve_parent(s.doc, view)
        proj = project_section(src_shapes, view, parent_view or {}, scale=scale,
                               position=(float(pos[0]), float(pos[1])),
                               show_hidden=show_hidden, view_id=view.get("id", "v?"))
    elif vtype == "detail":
        parent_view = _resolve_parent(s.doc, view)
        center = view.get("center", [0.0, 0.0]) or [0.0, 0.0]
        radius = float(view.get("radius", 0.0) or 0.0)
        if parent_view is None:
            proj = {"view_id": view.get("id"), "scale": scale, "orientation": "detail",
                    "visible": [], "hidden": [], "bbox": None, "ms": 0.0,
                    "error": "detail view has no resolvable parent"}
        else:
            parent_geom = _project_one(s, parent_view, src_shapes, src_rev)
            proj = project_detail(parent_geom, center, radius, scale,
                                  (float(pos[0]), float(pos[1])), view_id=view.get("id", "v?"))
    else:
        orient = view.get("orientation", "front")
        key = (src_rev, orient, round(scale, 6), show_hidden)
        cached = s._proj_cache.get(key)
        if cached is not None:
            # Re-apply this view's position (cache is keyed on rev/orient/scale/hidden, not position —
            # position is a cheap translate that doesn't need a re-HLR).
            proj = _shift_to_position(cached, pos)
        else:
            proj = project_view(src_shapes, orient, scale=scale, position=(0.0, 0.0),
                                show_hidden=show_hidden, view_id=view.get("id", "v?"))
            s._proj_cache[key] = proj
            proj = _shift_to_position(proj, pos)

    out = dict(proj)
    out["view_id"] = view.get("id")
    out["type"] = vtype
    out["position"] = [float(pos[0]), float(pos[1])]
    out["show_hidden"] = show_hidden
    # Section/detail carry their defining fields so the host can draw the cutting line / detail circle
    # on the parent and label the view ("SECTION A-A" / "DETAIL B").
    if vtype == "section":
        out["parent"] = view.get("parent")
        if view.get("cut_line") is not None:
            out["cut_line"] = view.get("cut_line")
        if view.get("plane") is not None:
            out["plane"] = view.get("plane")
    elif vtype == "detail":
        out["parent"] = view.get("parent")
        out["center"] = [float((view.get("center") or [0.0, 0.0])[0]),
                         float((view.get("center") or [0.0, 0.0])[1])]
        out["radius"] = float(view.get("radius", 0.0) or 0.0)
    # W13: the view's stored annotations (dimensions/notes) ride along with the projected geometry so
    # drawing_get / add_view round-trip them. They are sheet-mm coords, independent of the HLR cache.
    import copy as _copy
    out["annotations"] = _copy.deepcopy(view.get("annotations", []) or [])
    if view.get("label") is not None:
        out["label"] = view.get("label")
    return out


def _resolve_parent(doc: dict, view: dict) -> Optional[dict]:
    """The stored View dict named by ``view['parent']`` (a view id), or None when absent/unknown."""
    pid = view.get("parent")
    if not isinstance(pid, str):
        return None
    _sh, pv = _find_view(doc, pid)
    return pv


def _shift_to_position(proj: dict, pos) -> dict:
    """Translate a cached (origin-centered) projection to a sheet position. Cheap; no re-HLR."""
    dx, dy = float(pos[0]), float(pos[1])
    if dx == 0.0 and dy == 0.0:
        return dict(proj)
    out = dict(proj)
    out["visible"] = [[[x + dx, y + dy] for x, y in pl] for pl in proj.get("visible", [])]
    out["hidden"] = [[[x + dx, y + dy] for x, y in pl] for pl in proj.get("hidden", [])]
    bb = proj.get("bbox")
    out["bbox"] = [bb[0] + dx, bb[1] + dy, bb[2] + dx, bb[3] + dy] if bb else None
    return out


# --------------------------------------------------------------------------- RPC entry points


def new_drawing(source_doc_id: str, name: Optional[str] = None) -> dict:
    """drawing_new — create a drawing referencing an OPEN source part/assembly.

    The source must be an open session (so the projection has bodies). Returns the drawing doc id +
    the doc JSON (no views yet — the host adds them with drawing_add_view)."""
    from engine.doc import session as docsess

    src = docsess.get_session(source_doc_id)  # validates the source is open
    nm = name or f"{src.doc.get('name', 'Part')} - Drawing"
    doc = _blank_drawing(nm, source_doc_id)
    doc_id = uuid.uuid4().hex[:12]
    s = DrawingSession(doc_id=doc_id, doc=doc, source_doc_id=source_doc_id, revision=0)
    _DRAWINGS[doc_id] = s
    return {"doc_id": doc_id, "drawing": _tree(s)}


def add_view(doc_id: str, *, type: str, orientation: str, sheet_id: Optional[str] = None,
             scale: Optional[float] = None, position: Optional[list] = None,
             show_hidden: Optional[bool] = None) -> dict:
    """drawing_add_view — append a view to a sheet. Returns the new view id + the projected geometry
    for THAT view (so the host can render it immediately without a full drawing_get)."""
    s = get_drawing(doc_id)
    sheet = _find_sheet(s.doc, sheet_id)
    if type not in ("base", "projected", "iso"):
        raise OpError("invalid_op", f"view type '{type}' must be base|projected|iso",
                      {"reason": "bad type"})
    if type == "iso":
        orientation = "iso"
    view = {
        "id": _next_view_id(s.doc),
        "type": type,
        "orientation": orientation,
        "scale": float(scale) if scale is not None else 1.0,
        "position": [float(position[0]), float(position[1])] if position else [0.0, 0.0],
        "show_hidden": bool(show_hidden) if show_hidden is not None else True,
    }
    sheet.setdefault("views", []).append(view)
    s.revision += 1
    s.dirty = True
    src_shapes, src_rev = _source_shapes(s.source_doc_id)
    geom = _project_one(s, view, src_shapes, src_rev)
    return {"view_id": view["id"], "revision": s.revision, "view": geom}


def _next_label(doc: dict) -> str:
    """The next free single-letter view label (A, B, C, …) across the whole drawing.

    Section/detail views are labelled with a capital letter ("SECTION A-A", "DETAIL B"); we scan the
    existing labels and return the lowest unused letter so the host doesn't have to manage them.
    """
    used = set()
    for _sh, v in _all_views(doc):
        lab = v.get("label")
        if isinstance(lab, str) and len(lab) == 1 and lab.isalpha():
            used.add(lab.upper())
    for code in range(ord("A"), ord("Z") + 1):
        c = chr(code)
        if c not in used:
            return c
    return "A"


def _default_section_position(parent_view: dict) -> list:
    """A default sheet position for a new section view: offset to the RIGHT of the parent view's
    position by a margin so it doesn't overlap (the host usually overrides this on placement)."""
    pp = parent_view.get("position", [0.0, 0.0]) or [0.0, 0.0]
    return [float(pp[0]) + 120.0, float(pp[1])]


def add_section(doc_id: str, parent_view_id: str, *, plane: Optional[dict] = None,
                cut_line: Optional[list] = None, label: Optional[str] = None,
                scale: Optional[float] = None, position: Optional[list] = None,
                sheet_id: Optional[str] = None, show_hidden: Optional[bool] = None) -> dict:
    """drawing_add_section — add a SECTION view cutting the model relative to a parent view.

    The cut is given either as an explicit world ``plane`` ``{normal:[x,y,z], point:[x,y,z]}`` or as
    a 2D ``cut_line`` ``[[x1,y1],[x2,y2]]`` in the PARENT view's sheet-mm coords (a cutting line
    dropped across the parent — the SolidWorks workflow). Returns ``{view_id, revision, view}`` where
    ``view`` is the projected ViewGeometry including ``section_faces`` (the hatched cut polygons)."""
    s = get_drawing(doc_id)
    sheet = _find_sheet(s.doc, sheet_id)
    _psh, parent = _find_view(s.doc, parent_view_id)
    if parent is None:
        raise OpError("invalid_op", f"section parent view '{parent_view_id}' not found",
                      {"reason": "unknown parent"})
    if plane is None and cut_line is None:
        raise OpError("invalid_op",
                      "section needs a world 'plane' {normal, point} or a 2D 'cut_line'",
                      {"reason": "no plane"})
    lab = label if isinstance(label, str) and label else _next_label(s.doc)
    view = {
        "id": _next_view_id(s.doc),
        "type": "section",
        "orientation": "section",
        "parent": parent_view_id,
        "label": lab,
        "scale": float(scale) if scale is not None else float(parent.get("scale", 1.0) or 1.0),
        "position": ([float(position[0]), float(position[1])] if position
                     else _default_section_position(parent)),
        "show_hidden": bool(show_hidden) if show_hidden is not None else False,
    }
    if plane is not None:
        view["plane"] = plane
    if cut_line is not None:
        view["cut_line"] = [[float(cut_line[0][0]), float(cut_line[0][1])],
                            [float(cut_line[1][0]), float(cut_line[1][1])]]
    sheet.setdefault("views", []).append(view)
    s.revision += 1
    s.dirty = True
    src_shapes, src_rev = _source_shapes(s.source_doc_id)
    geom = _project_one(s, view, src_shapes, src_rev)
    return {"view_id": view["id"], "revision": s.revision, "view": geom}


def add_detail(doc_id: str, parent_view_id: str, *, center: list, radius: float,
               scale: Optional[float] = None, label: Optional[str] = None,
               position: Optional[list] = None, sheet_id: Optional[str] = None,
               show_hidden: Optional[bool] = None) -> dict:
    """drawing_add_detail — add a magnified DETAIL view of a circular region of a parent view.

    ``center`` ([x,y] in the parent's sheet mm) + ``radius`` define the magnifier circle; the detail
    reuses the parent's HLR clipped to that circle, drawn at ``scale`` (default 2× the parent). The
    parent view's returned geometry gains a ``detail_circle`` marker so the host can draw the bubble.
    Returns ``{view_id, revision, view}``."""
    s = get_drawing(doc_id)
    sheet = _find_sheet(s.doc, sheet_id)
    _psh, parent = _find_view(s.doc, parent_view_id)
    if parent is None:
        raise OpError("invalid_op", f"detail parent view '{parent_view_id}' not found",
                      {"reason": "unknown parent"})
    if not (_is_xy(center)):
        raise OpError("invalid_op", "detail center must be [x,y] (parent sheet mm)",
                      {"reason": "bad center"})
    if not isinstance(radius, (int, float)) or radius <= 0:
        raise OpError("invalid_op", "detail radius must be a positive number",
                      {"reason": "bad radius"})
    lab = label if isinstance(label, str) and label else _next_label(s.doc)
    parent_scale = float(parent.get("scale", 1.0) or 1.0)
    view = {
        "id": _next_view_id(s.doc),
        "type": "detail",
        "orientation": "detail",
        "parent": parent_view_id,
        "label": lab,
        "center": [float(center[0]), float(center[1])],
        "radius": float(radius),
        "scale": float(scale) if scale is not None else parent_scale * 2.0,
        "position": ([float(position[0]), float(position[1])] if position
                     else [float((parent.get("position") or [0, 0])[0]) + 150.0,
                           float((parent.get("position") or [0, 0])[1])]),
        "show_hidden": bool(show_hidden) if show_hidden is not None else False,
    }
    sheet.setdefault("views", []).append(view)
    s.revision += 1
    s.dirty = True
    src_shapes, src_rev = _source_shapes(s.source_doc_id)
    geom = _project_one(s, view, src_shapes, src_rev)
    return {"view_id": view["id"], "revision": s.revision, "view": geom}


def remove_view(doc_id: str, view_id: str) -> dict:
    """drawing_remove_view — delete a view by id."""
    s = get_drawing(doc_id)
    removed = False
    for sh in s.doc.get("sheets", []) or []:
        before = len(sh.get("views", []) or [])
        sh["views"] = [v for v in sh.get("views", []) or [] if v.get("id") != view_id]
        if len(sh["views"]) != before:
            removed = True
    if not removed:
        raise OpError("invalid_op", f"no view '{view_id}'", {"reason": "unknown view"})
    s.revision += 1
    s.dirty = True
    return {"removed": True, "revision": s.revision}


def set_view(doc_id: str, view_id: str, *, scale: Optional[float] = None,
             position: Optional[list] = None, orientation: Optional[str] = None,
             show_hidden: Optional[bool] = None) -> dict:
    """drawing_set_view — move / rescale / re-orient a view. Returns the re-projected geometry."""
    s = get_drawing(doc_id)
    _sh, view = _find_view(s.doc, view_id)
    if view is None:
        raise OpError("invalid_op", f"no view '{view_id}'", {"reason": "unknown view"})
    if scale is not None:
        if float(scale) <= 0:
            raise OpError("invalid_op", "scale must be positive", {"reason": "bad scale"})
        view["scale"] = float(scale)
    if position is not None:
        view["position"] = [float(position[0]), float(position[1])]
    if orientation is not None and view.get("type") != "iso":
        view["orientation"] = orientation
    if show_hidden is not None:
        view["show_hidden"] = bool(show_hidden)
    s.revision += 1
    s.dirty = True
    src_shapes, src_rev = _source_shapes(s.source_doc_id)
    geom = _project_one(s, view, src_shapes, src_rev)
    return {"view_id": view_id, "revision": s.revision, "view": geom}


# --------------------------------------------------------------------------- annotations (W13)


_ANNOTATION_TYPES = {"linear_dim", "aligned_dim", "note"}


def _format_dim_value(value: float, units: str) -> str:
    """Format a measured MODEL length for the annotation's default ``text``.

    The engine is canonical mm; for MMGS we show millimetres, for IPS we convert to inches. The host
    re-formats on render (pmFormatDim) so this is only the stored default — keep it human-readable.
    """
    if units == "IPS":
        return f"{value / 25.4:.4g} in"
    return f"{value:.4g}"


def _next_ann_id(view: dict) -> str:
    hi = 0
    for a in view.get("annotations", []) or []:
        aid = a.get("id", "")
        if isinstance(aid, str) and aid.startswith("a") and aid[1:].isdigit():
            hi = max(hi, int(aid[1:]))
    return f"a{hi + 1}"


def _doc_units(doc: dict) -> str:
    u = (doc.get("units") or {}).get("system")
    return u if u in ("MMGS", "IPS") else "MMGS"


def add_dim(doc_id: str, view_id: str, *, type: str, a: list, b: list,
            pos: Optional[list] = None, text: Optional[str] = None) -> dict:
    """drawing_add_dim — append a dimension/note annotation to a view.

    ``a`` and ``b`` are SHEET-mm points (the host snaps them to projected polyline vertices). The
    displayed ``value`` is the TRUE MODEL length: ``|a - b| / view.scale`` — because the view geometry
    is drawn at ``scale`` (sheet = model * scale), dividing the sheet distance by the scale recovers
    the real measured model distance, so the value is SCALE-INDEPENDENT (a 2:1 view of a 50 mm edge
    still dimensions 50 mm). For a ``note`` the value is 0 and ``text`` is whatever the caller passed.

    Returns ``{annotation, drawing}`` (the new annotation + the full re-projected drawing_get so the
    host can re-render in one round-trip).
    """
    s = get_drawing(doc_id)
    if type not in _ANNOTATION_TYPES:
        raise OpError("invalid_op",
                      f"annotation type '{type}' must be one of {sorted(_ANNOTATION_TYPES)}",
                      {"reason": "bad type"})
    _sh, view = _find_view(s.doc, view_id)
    if view is None:
        raise OpError("invalid_op", f"no view '{view_id}'", {"reason": "unknown view"})
    if not (_is_xy(a) and _is_xy(b)):
        raise OpError("invalid_op", "dimension endpoints a,b must each be [x,y] sheet mm",
                      {"reason": "bad points"})

    ax, ay = float(a[0]), float(a[1])
    bx, by = float(b[0]), float(b[1])
    scale = float(view.get("scale", 1.0)) or 1.0
    if type == "linear_dim":
        # SolidWorks linear (smart) dimension: the shorter axis-aligned span. We store the larger of
        # the |dx|,|dy| sheet extents (the dominant ordinate) so a near-horizontal pick reads its
        # horizontal length; an aligned_dim instead measures the true point-to-point distance.
        sheet_dist = max(abs(bx - ax), abs(by - ay))
    elif type == "aligned_dim":
        sheet_dist = math.hypot(bx - ax, by - ay)
    else:  # note — no measured value
        sheet_dist = 0.0
    value = sheet_dist / scale   # TRUE model length (scale-independent)

    units = _doc_units(s.doc)
    if pos is None:
        # Default text position: offset the midpoint perpendicular to a→b by a small sheet margin so
        # the dimension line clears the geometry. The host may override on the third click.
        mx, my = (ax + bx) / 2.0, (ay + by) / 2.0
        dx, dy = bx - ax, by - ay
        n = math.hypot(dx, dy) or 1.0
        ppos = [mx - dy / n * 10.0, my + dx / n * 10.0]
    else:
        if not _is_xy(pos):
            raise OpError("invalid_op", "dimension pos must be [x,y] sheet mm or null",
                          {"reason": "bad pos"})
        ppos = [float(pos[0]), float(pos[1])]

    ann = {
        "id": _next_ann_id(view),
        "type": type,
        "a": [ax, ay],
        "b": [bx, by],
        "pos": ppos,
        "value": round(value, 6),
        "text": text if isinstance(text, str) and text else _format_dim_value(value, units),
    }
    view.setdefault("annotations", []).append(ann)
    s.revision += 1
    s.dirty = True
    return {"annotation": ann, "drawing": get_drawing_full(doc_id)}


def remove_annotation(doc_id: str, view_id: str, ann_id: str) -> dict:
    """drawing_remove_annotation — delete an annotation by id from a view."""
    s = get_drawing(doc_id)
    _sh, view = _find_view(s.doc, view_id)
    if view is None:
        raise OpError("invalid_op", f"no view '{view_id}'", {"reason": "unknown view"})
    anns = view.get("annotations", []) or []
    kept = [a for a in anns if a.get("id") != ann_id]
    if len(kept) == len(anns):
        raise OpError("invalid_op", f"no annotation '{ann_id}' on view '{view_id}'",
                      {"reason": "unknown annotation"})
    view["annotations"] = kept
    s.revision += 1
    s.dirty = True
    return {"removed": True, "view_id": view_id, "ann_id": ann_id, "revision": s.revision}


def _is_xy(p: Any) -> bool:
    return (isinstance(p, (list, tuple)) and len(p) == 2
            and all(isinstance(c, (int, float)) and not isinstance(c, bool) for c in p))


# --------------------------------------------------------------------------- BOM (W13)


def drawing_bom(doc_id: str) -> dict:
    """drawing_bom — a bill of materials for the drawing's source document.

    For an ASSEMBLY source: one row per DISTINCT part (grouped by source path, falling back to the
    component name), with a running quantity, the item ordinal, the part name, and the source file.
    Reuses :func:`engine.doc.session.asm_component_bodies` (the same per-component listing the mass
    evaluator uses). For a PART source: an empty BOM (a part drawing has no components) — clean, not
    an error.

    Returns ``{rows, is_assembly, source_doc_id}`` where each row is
    ``{item, name, quantity, source}``.
    """
    from engine.doc import session as docsess

    s = get_drawing(doc_id)
    try:
        src = docsess.get_session(s.source_doc_id)
    except Exception:  # noqa: BLE001 — a closed source yields an empty BOM, never a crash
        return {"rows": [], "is_assembly": False, "source_doc_id": s.source_doc_id,
                "note": "source document is not open"}

    if not src.is_assembly:
        return {"rows": [], "is_assembly": False, "source_doc_id": s.source_doc_id,
                "note": "part drawings have no BOM"}

    try:
        comps = docsess.asm_component_bodies(src)
    except Exception as exc:  # noqa: BLE001 — assembly introspection failure -> empty BOM + note
        return {"rows": [], "is_assembly": True, "source_doc_id": s.source_doc_id,
                "note": f"assembly introspection failed: {type(exc).__name__}: {exc}"}

    # Group identical parts: same source path (or name when there is no path) is the same part.
    groups: dict[str, dict] = {}
    order: list[str] = []
    for c in comps:
        name = c.get("name") or c.get("component") or "Part"
        source = c.get("source") or ""
        key = source or f"name:{name}"
        g = groups.get(key)
        if g is None:
            g = {"name": name, "source": source, "quantity": 0}
            groups[key] = g
            order.append(key)
        g["quantity"] += 1

    rows = []
    for i, key in enumerate(order, start=1):
        g = groups[key]
        rows.append({"item": i, "name": g["name"], "quantity": g["quantity"],
                     "source": g["source"]})
    return {"rows": rows, "is_assembly": True, "source_doc_id": s.source_doc_id}


def get_drawing_full(doc_id: str) -> dict:
    """drawing_get — the full drawing doc + every view's projected 2D geometry.

    Re-projects all views against the source's CURRENT bodies (so an edit to the source reflows the
    drawing). The per-(rev, orient, scale) cache makes repeated fetches of an unchanged source cheap.
    """
    s = get_drawing(doc_id)
    src_shapes, src_rev = _source_shapes(s.source_doc_id)
    sheets_out: list[dict] = []
    total_ms = 0.0
    for sh in s.doc.get("sheets", []) or []:
        views_out: list[dict] = []
        for v in sh.get("views", []) or []:
            geom = _project_one(s, v, src_shapes, src_rev)
            total_ms += float(geom.get("ms", 0.0))
            views_out.append(geom)
        _attach_parent_markers(views_out)
        sheets_out.append({
            "id": sh.get("id"),
            "size": sh.get("size"),
            "size_mm": list(SHEET_DIMS_MM.get(sh.get("size"), (0.0, 0.0))),
            "views": views_out,
        })
    return {
        "doc_id": doc_id,
        "revision": s.revision,
        "source_doc_id": s.source_doc_id,
        "source_revision": src_rev,
        "tree": _tree(s),
        "sheets": sheets_out,
        "total_hlr_ms": round(total_ms, 3),
    }


def _attach_parent_markers(views_out: list[dict]) -> None:
    """Decorate parent views with markers for their child section/detail views (in place).

    A section child adds a ``section_lines`` entry to its parent's geometry — the cutting line drawn
    across the parent (sheet mm) + the label; a detail child adds a ``detail_circles`` entry — the
    magnifier circle (center+radius) + label. The host renders these on the parent so the drawing
    reads like SolidWorks (a cutting line with arrows, a labelled detail bubble)."""
    by_id = {g.get("view_id"): g for g in views_out}
    for g in views_out:
        if g.get("type") == "section" and g.get("cut_line") is not None:
            parent = by_id.get(g.get("parent"))
            if parent is not None:
                parent.setdefault("section_lines", []).append(
                    {"label": g.get("label"), "view_id": g.get("view_id"),
                     "line": g.get("cut_line")})
        elif g.get("type") == "detail":
            parent = by_id.get(g.get("parent"))
            if parent is not None and g.get("center") is not None:
                parent.setdefault("detail_circles", []).append(
                    {"label": g.get("label"), "view_id": g.get("view_id"),
                     "center": g.get("center"), "radius": g.get("radius")})


def _tree(s: DrawingSession) -> dict:
    import copy
    return copy.deepcopy(s.doc)


# --------------------------------------------------------------------------- worker adapters


def _drawing_new(params: dict, _emit) -> dict:
    return new_drawing(params["source_doc_id"], params.get("name"))


def _drawing_add_view(params: dict, _emit) -> dict:
    return add_view(
        params["doc_id"],
        type=params["type"],
        orientation=params.get("orientation", "front"),
        sheet_id=params.get("sheet_id"),
        scale=params.get("scale"),
        position=params.get("position"),
        show_hidden=params.get("show_hidden"),
    )


def _drawing_add_section(params: dict, _emit) -> dict:
    return add_section(
        params["doc_id"], params["parent_view_id"],
        plane=params.get("plane"),
        cut_line=params.get("cut_line"),
        label=params.get("label"),
        scale=params.get("scale"),
        position=params.get("position"),
        sheet_id=params.get("sheet_id"),
        show_hidden=params.get("show_hidden"),
    )


def _drawing_add_detail(params: dict, _emit) -> dict:
    return add_detail(
        params["doc_id"], params["parent_view_id"],
        center=params["center"], radius=params["radius"],
        scale=params.get("scale"),
        label=params.get("label"),
        position=params.get("position"),
        sheet_id=params.get("sheet_id"),
        show_hidden=params.get("show_hidden"),
    )


def _drawing_remove_view(params: dict, _emit) -> dict:
    return remove_view(params["doc_id"], params["view_id"])


def _drawing_set_view(params: dict, _emit) -> dict:
    return set_view(
        params["doc_id"], params["view_id"],
        scale=params.get("scale"),
        position=params.get("position"),
        orientation=params.get("orientation"),
        show_hidden=params.get("show_hidden"),
    )


def _drawing_get(params: dict, _emit) -> dict:
    return get_drawing_full(params["doc_id"])


def _drawing_close(params: dict, _emit) -> dict:
    return {"closed": close_drawing(params["doc_id"])}


def _drawing_add_dim(params: dict, _emit) -> dict:
    return add_dim(
        params["doc_id"], params["view_id"],
        type=params.get("type", "linear_dim"),
        a=params["a"], b=params["b"],
        pos=params.get("pos"),
        text=params.get("text"),
    )


def _drawing_remove_annotation(params: dict, _emit) -> dict:
    return remove_annotation(params["doc_id"], params["view_id"], params["ann_id"])


def _drawing_bom(params: dict, _emit) -> dict:
    return drawing_bom(params["doc_id"])


WORKER_METHODS = {
    "drawing_new": _drawing_new,
    "drawing_add_view": _drawing_add_view,
    "drawing_add_section": _drawing_add_section,
    "drawing_add_detail": _drawing_add_detail,
    "drawing_remove_view": _drawing_remove_view,
    "drawing_set_view": _drawing_set_view,
    "drawing_get": _drawing_get,
    "drawing_close": _drawing_close,
    "drawing_add_dim": _drawing_add_dim,
    "drawing_remove_annotation": _drawing_remove_annotation,
    "drawing_bom": _drawing_bom,
}
