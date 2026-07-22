"""CAD 2D-drawing MCP tools (T-RF8 split of cad_tools.py): make_drawing, export_drawing, review_drawing, view/section/detail/dimension/gdt/datum/surface-finish/titleblock tools."""
from __future__ import annotations

from mcp.server.fastmcp import FastMCP  # noqa: F401 — register signature
from mcp.server.fastmcp import Image
from typing import Optional
import os

from .cad_common import (
    _doc,
    _dumps,
    _err,
    _resolve_drawing_view_id,
    _tmp,
    cb,
    sh,
)

def register_cad_drawing_tools(mcp: FastMCP) -> None:
    """CAD 2D-drawing MCP tools (T-RF8 split of cad_tools.py): make_drawing, export_drawing, review_drawing, view/section/detail/dimension/gdt/datum/surface-finish/titleblock tools."""


    # ---------------------------------------------------------------- drawings & parts

    @mcp.tool()
    def make_drawing(doc_id: Optional[str] = None, views: Optional[list] = None,
                     sections: Optional[list] = None, sheet: Optional[str] = None,
                     hole_callouts: bool = True, model_items: bool = True,
                     title: Optional[dict] = None) -> str:
        """Create a 2D engineering drawing of the current part in ONE shot, then refine it.

        Builds projected views (hidden-line removed), auto-dimensions the ortho views, calls out
        holes, and fills the title block. Returns the new drawing_doc_id.

        views   : list of orientations, e.g. ["front","top","right","iso"] (default this set).
        sections: list of auto-section requests, each a dict
                  {"parent":"front","axis":"vertical"|"horizontal","mode":"full"|"half",
                   "reverse":false,"label":"A"} — cuts the parent view through its centre, no
                  sheet-mm math needed.
        sheet   : paper size "A4".."A0" or "A".."E" (default A3).
        title   : title-block fields {"title":..,"material":..,"scale":"1:1","rev":"A",...}.

        AFTER making it: call review_drawing(drawing_doc_id) to SEE the sheet, then refine with
        add_dimension / add_gdt / add_datum / add_surface_finish / add_section_view /
        add_detail_view / set_view (move/scale) / set_titleblock, reviewing as you go. Deliver
        with export_drawing('pdf'|'dxf', drawing_doc_id).
        """
        try:
            d = _doc(doc_id)
            tree = cb.call("doc_tree", {"doc_id": d})
            tree_node = tree.get("tree") or {}
            name = tree_node.get("name", "") if isinstance(tree_node, dict) else ""

            # D2: reject make_drawing on a doc that has no geometry so we don't silently
            # draw a stale "last-used" part.  We consider the doc empty when it has no
            # features in the tree AND no tessellated bodies (body_count==0 or missing).
            features = tree_node.get("features", []) if isinstance(tree_node, dict) else []
            body_count = tree.get("body_count", tree_node.get("body_count", None))
            if body_count is None:
                # Some engine versions put body_count at the top level of doc_tree,
                # others bury it inside the tree node.  If we can't read it, check features.
                body_count = len(features)
            if not features and not body_count:
                return _dumps({"ok": False,
                               "error": f"make_drawing: document {d!r} has no geometry to draw"})

            spec = {"views": views or ["front", "top", "right", "iso"],
                    "model_items": bool(model_items), "hole_callouts": bool(hole_callouts)}
            if sections:
                spec["sections"] = sections
            if sheet:
                spec["sheet"] = sheet
            tb = dict(title) if isinstance(title, dict) else {}
            if name and "title" not in tb:
                tb["title"] = name
            if tb:
                spec["title"] = tb
            r = cb.call("drawing_make", {"source_doc_id": d, "spec": spec})
            drawing_doc_id = r.get("doc_id")
            # D7: include the view_id ↔ orientation map so callers can pass either a view_id
            # or an orientation string to add_gdt / add_datum / add_dimension without having
            # to call drawing_get first.
            view_map: dict = {}
            if drawing_doc_id:
                try:
                    full = cb.call("drawing_get", {"doc_id": drawing_doc_id})
                    for sh in full.get("sheets", []):
                        for v in sh.get("views", []):
                            vid = v.get("view_id") or v.get("id")
                            ori = v.get("orientation") or v.get("type", "")
                            if vid:
                                view_map[vid] = ori
                except Exception:  # noqa: BLE001
                    pass
            return _dumps({"ok": True, "source_doc_id": d,
                           "drawing_doc_id": drawing_doc_id, "name": name,
                           "views": view_map or None,
                           "next_actions": [
                               "review_drawing(drawing_doc_id)  # SEE the drawing, then refine",
                               "add_gdt / add_dimension / set_view / set_titleblock  # refine",
                               "export_drawing('pdf', drawing_doc_id)  # or 'dxf'"]})
        except Exception as e:  # noqa: BLE001
            return _err(e)



    @mcp.tool()
    def export_drawing(fmt: str = "pdf", doc_id: Optional[str] = None,
                       path: Optional[str] = None) -> str:
        """Export a 2D DRAWING document to a vector file so you can deliver it.

        fmt    : "pdf" (a viewable sheet) or "dxf" (CAD interchange). Pass the drawing_doc_id from
                 make_drawing as doc_id. To SEE a drawing inline use render_part(drawing_doc_id).
        Returns the written path + entity counts. (export_part is for 3D B-rep/mesh only — a
        drawing is 2D, so it has its own export here.)
        """
        try:
            d = _doc(doc_id)
            fmt = (fmt or "pdf").lower()
            if fmt not in ("pdf", "dxf"):
                return _dumps({"ok": False, "error": f"fmt must be 'pdf' or 'dxf', got {fmt!r}"})
            out_path = path or _tmp("." + fmt)
            r = cb.call("export_drawing", {"doc_id": d, "path": out_path, "fmt": fmt})
            return _dumps({"ok": True, "doc_id": d, "format": fmt,
                           "path": r.get("path", out_path),
                           "visible": r.get("visible"), "hidden": r.get("hidden"),
                           "entities": r.get("entities")})
        except Exception as e:  # noqa: BLE001
            return _err(e)



    @mcp.tool()
    def review_drawing(doc_id: Optional[str] = None) -> Image:
        """Render a 2D DRAWING document to an image so you can SEE it and self-review.

        Pass the drawing_doc_id from make_drawing. The render shows the projected views, every
        dimension / hole callout / GD&T frame / surface-finish symbol / section hatch and the
        title block — exactly what the DXF/PDF export will contain. Use it to check for: missing
        or overlapping dimensions, views colliding on the sheet, GD&T placed off the feature, an
        empty title block. Then fix with the granular drawing tools (add_dimension, add_gdt,
        set_view to move/scale a view, set_titleblock) and review again.
        """
        d = _doc(doc_id)
        png = _tmp(".png")
        rd = cb.call("drawing_render_png", {"doc_id": d, "out_path": png})
        if not rd.get("ok") or not os.path.exists(png):
            raise ValueError(f"Drawing render failed: {rd.get('reason', 'not a drawing doc?')}")
        return Image(path=png)



    @mcp.tool()
    def add_drawing_view(orientation: str = "front", doc_id: Optional[str] = None,
                         view_type: str = "projected", x: float = 150.0, y: float = 150.0) -> str:
        """Add ONE projected view to a drawing. doc_id = the drawing_doc_id.

        orientation: front|back|top|bottom|left|right|iso. view_type: base (the first/anchor
        ortho view) | projected | iso. x,y = the view centre on the sheet in millimetres.
        """
        try:
            d = _doc(doc_id)
            r = cb.call("drawing_add_view", {"doc_id": d, "type": view_type,
                                             "orientation": orientation, "position": [x, y]})
            return _dumps({"ok": True, "view_id": r.get("view_id")})
        except Exception as e:  # noqa: BLE001
            return _err(e)



    @mcp.tool()
    def add_section_view(parent: str = "front", axis: str = "vertical", mode: str = "full",
                         reverse: bool = False, label: Optional[str] = None,
                         doc_id: Optional[str] = None) -> str:
        """Add a SECTION view by cutting a parent view through its centre. doc_id = drawing_doc_id.

        parent: the orientation ("front"/"top"/...) or view_id of the view to cut.
        axis  : "vertical" or "horizontal" cut line through the part centre.
        mode  : "full" (whole part sectioned) or "half" (half sectioned, half external view).
        reverse: flip which side of the cut is removed.
        No sheet-mm math needed — the engine places the cut line across the parent's bbox.
        """
        try:
            d = _doc(doc_id)
            # Reuse make-style auto-section via a tiny spec on the existing drawing.
            full = cb.call("drawing_get", {"doc_id": d})
            vid, bbox = None, None
            for sh in full.get("sheets", []):
                for v in sh.get("views", []):
                    if v.get("type") in ("section", "detail"):
                        continue
                    if parent in (v.get("view_id"), v.get("id"), v.get("orientation")):
                        bb = v.get("bbox")
                        if isinstance(bb, list) and len(bb) == 4:
                            vid, bbox = (v.get("view_id") or v.get("id")), bb
            if not vid:
                return _dumps({"ok": False, "error": f"no parent view {parent!r} to cut"})
            x0, y0, x1, y1 = bbox
            cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
            pad = 0.1 * max(x1 - x0, y1 - y0) + 2.0
            cut = ([[x0 - pad, cy], [x1 + pad, cy]] if axis == "horizontal"
                   else [[cx, y0 - pad], [cx, y1 + pad]])
            params = {"doc_id": d, "parent_view_id": vid, "cut_line": cut,
                      "opts": {"mode": mode, "reverse": reverse}}
            if label:
                params["label"] = label
            r = cb.call("drawing_add_section", params)
            return _dumps({"ok": True, "view_id": r.get("view_id")})
        except Exception as e:  # noqa: BLE001
            return _err(e)



    @mcp.tool()
    def add_detail_view(parent_view_id: str, cx: float, cy: float, radius: float,
                        doc_id: Optional[str] = None) -> str:
        """Add a magnified DETAIL view of a circular region of a parent view. doc_id = drawing_doc_id.
        cx,cy,radius are in the parent view's sheet millimetres (read them from review_drawing)."""
        try:
            d = _doc(doc_id)
            r = cb.call("drawing_add_detail", {"doc_id": d, "parent_view_id": parent_view_id,
                                               "center": [cx, cy], "radius": radius})
            return _dumps({"ok": True, "view_id": r.get("view_id")})
        except Exception as e:  # noqa: BLE001
            return _err(e)



    @mcp.tool()
    def add_dimension(view_id: str, a: list, b: list, dtype: str = "linear_dim",
                      pos: Optional[list] = None, doc_id: Optional[str] = None) -> str:
        """Add a dimension to a view. doc_id = drawing_doc_id. a,b are sheet-mm points (snap to
        projected vertices you read from review_drawing). dtype: linear_dim|aligned_dim. pos is
        the optional text position. For auto-dimensioning a whole view use the drawing spec's
        model_items instead.

        view_id accepts a numeric id (v1, v2, …) OR an orientation string ("front", "top", …)
        matching the view labels returned by review_drawing / make_drawing.
        """
        try:
            d = _doc(doc_id)
            # D7: resolve orientation strings ("front"/"top"/…) to numeric view ids.
            view_id = _resolve_drawing_view_id(d, view_id)
            params = {"doc_id": d, "view_id": view_id, "type": dtype, "a": a, "b": b}
            if pos:
                params["pos"] = pos
            r = cb.call("drawing_add_dim", params)
            return _dumps({"ok": True, "annotation": (r.get("annotation") or {}).get("id")})
        except Exception as e:  # noqa: BLE001
            return _err(e)



    @mcp.tool()
    def add_gdt(view_id: str, symbol: str, tolerance: str, a: list, pos: list,
                datums: Optional[list] = None, diameter: bool = False,
                modifier: str = "none", doc_id: Optional[str] = None) -> str:
        """Add an ASME Y14.5 feature control frame. doc_id = drawing_doc_id.

        symbol  : position|flatness|perpendicularity|parallelism|angularity|concentricity|
                  symmetry|profile_surface|profile_line|circularity|cylindricity|straightness|
                  circular_runout|total_runout
        tolerance: the zone value, e.g. "0.1".  diameter=True for a ⌀ tolerance zone.
        modifier : none|mmc|lmc|projected (material condition on the tolerance).
        datums   : a list of datum letters ["A","B","C"] (or dicts {"ref":"A","modifier":"mmc"}).
        a   : the feature point on the view the frame points at (sheet mm, from review_drawing).
        pos : where the frame sits on the sheet (sheet mm).

        view_id accepts a numeric id (v1, v2, …) OR an orientation string ("front"/"top"/…).
        """
        try:
            d = _doc(doc_id)
            # D7: resolve orientation strings ("front"/"top"/…) to numeric view ids.
            view_id = _resolve_drawing_view_id(d, view_id)
            dl = []
            for it in (datums or []):
                dl.append(it if isinstance(it, dict) else {"ref": str(it)})
            spec = {"symbol": symbol, "tol_value": tolerance, "tol_diameter": bool(diameter),
                    "tol_modifier": modifier, "datums": dl, "a": a, "pos": pos}
            r = cb.call("drawing_add_gdt", {"doc_id": d, "view_id": view_id, "spec": spec})
            return _dumps({"ok": True, "annotation": (r.get("annotation") or {}).get("id")})
        except Exception as e:  # noqa: BLE001
            return _err(e)



    @mcp.tool()
    def add_datum(view_id: str, label: str, a: list, pos: list,
                  doc_id: Optional[str] = None) -> str:
        """Add a datum feature symbol (triangle + boxed letter). doc_id = drawing_doc_id.
        label is the datum letter ("A"); a is the feature point, pos the label box (sheet mm).

        view_id accepts a numeric id (v1, v2, …) OR an orientation string ("front"/"top"/…).
        """
        try:
            d = _doc(doc_id)
            # D7: resolve orientation strings ("front"/"top"/…) to numeric view ids.
            view_id = _resolve_drawing_view_id(d, view_id)
            spec = {"label": label, "a": a, "pos": pos}
            r = cb.call("drawing_add_datum", {"doc_id": d, "view_id": view_id, "spec": spec})
            return _dumps({"ok": True, "annotation": (r.get("annotation") or {}).get("id")})
        except Exception as e:  # noqa: BLE001
            return _err(e)



    @mcp.tool()
    def add_surface_finish(view_id: str, ra: str, a: list, pos: list, machining: str = "required",
                           all_around: bool = False, doc_id: Optional[str] = None) -> str:
        """Add a surface-finish symbol. doc_id = drawing_doc_id. ra is the roughness value ("1.6");
        machining: any|required|prohibited. a is the surface point, pos the symbol (sheet mm)."""
        try:
            d = _doc(doc_id)
            spec = {"value": ra, "machining": machining, "all_around": bool(all_around),
                    "a": a, "pos": pos}
            r = cb.call("drawing_add_surface_finish", {"doc_id": d, "view_id": view_id, "spec": spec})
            return _dumps({"ok": True, "annotation": (r.get("annotation") or {}).get("id")})
        except Exception as e:  # noqa: BLE001
            return _err(e)



    @mcp.tool()
    def set_view(view_id: str, x: Optional[float] = None, y: Optional[float] = None,
                 scale: Optional[float] = None, doc_id: Optional[str] = None) -> str:
        """Move and/or rescale a view. doc_id = drawing_doc_id. x,y reposition the view on the
        sheet (mm); scale sets the view scale (1.0 = 1:1, 0.5 = 1:2, 2.0 = 2:1). Use this to
        spread overlapping views apart after review_drawing."""
        try:
            d = _doc(doc_id)
            params = {"doc_id": d, "view_id": view_id}
            if x is not None and y is not None:
                params["position"] = [x, y]
            if scale is not None:
                params["scale"] = scale
            cb.call("drawing_set_view", params)
            return _dumps({"ok": True, "view_id": view_id})
        except Exception as e:  # noqa: BLE001
            return _err(e)



    @mcp.tool()
    def set_titleblock(fields: dict, doc_id: Optional[str] = None) -> str:
        """Fill the drawing's title block. doc_id = drawing_doc_id. fields is a dict of any of:
        title, drawn_by, date, material, finish, scale, rev, sheet, part_number, company."""
        try:
            d = _doc(doc_id)
            cb.call("drawing_set_titleblock", {"doc_id": d, "sheet_id": "s1", "fields": fields})
            return _dumps({"ok": True})
        except Exception as e:  # noqa: BLE001
            return _err(e)
