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
    """Project a single view, using/filling the per-(rev, orient, scale, hidden) cache."""
    orient = view.get("orientation", "front")
    scale = float(view.get("scale", 1.0))
    show_hidden = bool(view.get("show_hidden", True))
    pos = view.get("position", [0.0, 0.0]) or [0.0, 0.0]
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
    out["type"] = view.get("type")
    out["position"] = [float(pos[0]), float(pos[1])]
    out["show_hidden"] = show_hidden
    # W13: the view's stored annotations (dimensions/notes) ride along with the projected geometry so
    # drawing_get / add_view round-trip them. They are sheet-mm coords, independent of the HLR cache.
    import copy as _copy
    out["annotations"] = _copy.deepcopy(view.get("annotations", []) or [])
    if view.get("label") is not None:
        out["label"] = view.get("label")
    return out


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
    "drawing_remove_view": _drawing_remove_view,
    "drawing_set_view": _drawing_set_view,
    "drawing_get": _drawing_get,
    "drawing_close": _drawing_close,
    "drawing_add_dim": _drawing_add_dim,
    "drawing_remove_annotation": _drawing_remove_annotation,
    "drawing_bom": _drawing_bom,
}
