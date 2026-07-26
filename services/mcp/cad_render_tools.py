"""CAD render + close-up inspection MCP tools (T-RF8 split of cad_tools.py): render_part, contact_sheet, render_views, inspect_part, look_around, inspect_feature."""
from __future__ import annotations

from mcp.server.fastmcp import FastMCP  # noqa: F401 — register signature
from mcp.server.fastmcp import Image
from typing import Optional
from typing import Union
import os
import math

from .cad_common import (
    _annotate_render,
    _apply_emphasis_overlay,
    _auto_section_for_target,
    _camera_dir_for_target,
    _crop_frame,
    _doc_mesh,
    _mesh_render_frame,
    _captioned_render,
    _crop_png_to_rect,
    _crop_rect_for_target,
    _describe_part_excerpt,
    _doc,
    _inspect_caption,
    _normalize_view_list,
    _orbit_view_dirs,
    _palette_caption_block,
    _render_annotation_caption,
    _resolve_inspect_target,
    _target_bbox,
    _target_is_internal,
    _tile_pngs,
    _tile_pngs_grid,
    _tmp,
    _vnorm,
    cb,
)


def _annotated_view_tiles(d: str, view_list: list, highlight=None, annotate: bool = True,
                          part_bb: Optional[dict] = None, target_bb: Optional[dict] = None,
                          matches: Optional[list] = None, zoom_factor: float = 1.25) -> tuple:
    """Render each view on its own, overlay the mesh annotation, return (tiles, labels, infos).

    The native doc_render_contact_sheet RPC tiles inside the engine, so there is no per-tile
    image to draw B-rep edges or a highlight onto. Rendering view-by-view costs ~11 ms of
    engine time per tile and buys every tile the same annotation a single render_part gets.
    When `target_bb` is given each tile is cropped to the target first and the overlay is
    reprojected through the CROPPED frame (_crop_frame), so edges stay 1 px crisp instead of
    being smeared by the upscale."""
    cropping = bool(target_bb and part_bb)
    tiles, labels, infos = [], [], []
    for v in view_list:
        try:
            vd = _camera_dir_for_target(matches or [], v if v != "auto" else "auto")
        except Exception:  # noqa: BLE001
            vd = _vnorm((1.0, 1.0, 0.8))
        png_full = _tmp(".png")
        r = cb.call("doc_render_view", {"doc_id": d, "out_path": png_full,
                                        "view": {"dir": list(vd)}, "annotate": bool(annotate)})
        if not r.get("ok") or not os.path.exists(png_full):
            continue
        png_tile, frame = png_full, None
        if cropping:
            rect = _crop_rect_for_target(part_bb, target_bb, vd,
                                         margin=0.18, zoom_factor=zoom_factor)
            png_tile = _tmp(".png")
            _crop_png_to_rect(png_full, rect, png_tile)
            mesh = _doc_mesh(d)
            if mesh is not None:
                frame = _crop_frame(_mesh_render_frame(mesh, vd), rect)
        info = None
        if annotate or highlight is not None:
            png_tile, info = _annotate_render(
                png_tile, d, vd, highlight=highlight, edges=bool(annotate),
                callouts=bool(annotate) and not cropping,
                scale=bool(annotate) and not cropping, frame=frame)
        tiles.append(png_tile)
        labels.append(v if isinstance(v, str) else "custom")
        infos.append(info)
    return tiles, labels, infos


def _merge_annotation_infos(infos: list) -> Optional[dict]:
    """Fold per-tile annotation info into ONE record for a sheet caption: a feature is
    "visible" if any tile saw it, and only a feature no tile could see is reported hidden."""
    live = [i for i in infos if i and i.get("available")]
    if not live:
        return None
    out = dict(live[0])
    out["callouts"] = sorted({c for i in live for c in (i.get("callouts") or [])})
    out["callouts_omitted"] = sorted({c for i in live for c in (i.get("callouts_omitted") or [])
                                      if c not in out["callouts"]})
    seen = set(out["callouts"])
    out["hidden_features"] = sorted({h for i in live for h in (i.get("hidden_features") or [])
                                     if h not in seen})
    out["hidden_faces"] = min(int(i.get("hidden_faces") or 0) for i in live)
    out["hidden_face_list"] = []
    merged_hl = []
    for k, h in enumerate(live[0].get("highlights") or []):
        h = dict(h)
        h["visible"] = any((i.get("highlights") or [{}] * (k + 1))[k].get("visible")
                           for i in live if len(i.get("highlights") or []) > k)
        merged_hl.append(h)
    out["highlights"] = merged_hl
    return out


def register_cad_render_tools(mcp: FastMCP) -> None:
    """CAD render + close-up inspection MCP tools (T-RF8 split of cad_tools.py): render_part, contact_sheet, render_views, inspect_part, look_around, inspect_feature."""


    @mcp.tool()
    def render_part(doc_id: Optional[str] = None,
                    view: Optional[Union[str, dict]] = None,
                    target: Optional[Union[str, dict]] = None,
                    emphasize: Optional[Union[str, dict, list]] = None,
                    emphasis_mode: str = "emphasis",
                    highlight: Optional[Union[str, dict, list]] = None,
                    annotate: bool = True) -> list:
        """Render the current part to an ANNOTATED image and return it so you can SEE the result.

        The render is colour-coded and labelled exactly like the in-app vision reviewer: an XYZ
        orientation gizmo, flat faces tinted by direction with the principal ones labelled
        (TOP +Z blue, BOTTOM −Z teal, FRONT −Y yellow, BACK +Y green, RIGHT +X red, LEFT −X
        orange), curved faces left grey. Use it to verify features landed on the face the request
        named and the proportions look right. See cad_guide("review") for the full key.

        view (optional): request a SPECIFIC angle instead of the default iso. Either a named view —
        "front", "back", "top", "bottom", "left", "right", "iso" — or a dict for a custom camera:
        {"azimuth": deg, "elevation": deg}  (azimuth 0 = front, 90 = right; elevation 90 = top), or
        {"dir": [x, y, z]}  (an explicit scene->camera direction). The image is headed with the
        resolved view name + bounding-box dimensions. Omit `view` for the labelled iso self-review.
        Use render_views(...) for the full 6-ortho+iso labelled grid, or contact_sheet(...) for a
        smaller multi-angle sheet.

        highlight (optional — point AT something without changing the framing):
          a feature NAME ("Boss"), a feature id ("f2"), a sid ("F58db6d"), a geometry-query
          dict, or a LIST of any of those. Each highlighted target is painted in a colour
          RESERVED for emphasis (magenta, then violet / hot-pink / purple / white — none of
          which the side palette can produce), outlined, and connected by a leader line to a
          label naming it. Unlike `emphasize`, the camera does NOT re-frame or crop, so you
          keep the whole part in view; the caption names every highlight and its colour.

        Feature emphasis (optional — manufacturing-feature VLM legibility):
          target / emphasize: plain-word face/edge query or feature id ("f2") whose owned faces
            render with EMPHASIS — high-contrast fill + glow outline; the rest of the part is
            desaturated so the target pops. Camera frames the target AABB with margin.
          emphasis_mode: "emphasis" (default — shared high-contrast colour) or "per_face"
            (each target face a distinct deterministic colour for disambiguation; legend in caption).

        annotate=True (default) additionally overlays, from the engine's own tessellation:
          * the B-rep EDGES with real hidden-line removal, so a fillet, a chamfer, a shallow
            pocket or two coincident faces actually read against the flat shading;
          * feature CALLOUTS — id + name on a leader line for the features visible in frame
            (capped for legibility; the caption names any that were omitted);
          * a SCALE BAR + exact bbox, so you can tell a 4 mm part from a 400 mm one.
        annotate=False suppresses all of that (and the engine's own tint/gizmo/labels) for a
        clean product shot; an explicit `highlight` is still drawn because you asked for it.

        VERIFY LOOP: call after every mutation-adjacent change (add_features / edit / delete) so
        you can SEE whether the op landed; pair with measure/inspect_feature and verify_battery
        before declaring done.

        Returns [image, caption]: the PNG followed by a SELF-SUFFICIENT text caption — part name
        and bbox, the features visible in this view as id+name, what is highlighted and in what
        colour, the side-palette colour key, and an explicit note of what this camera CANNOT see
        plus the tool to call for it. A model that reads only the caption still knows the scene.
        """
        d = _doc(doc_id)
        png = _tmp(".png")
        view_name = view if isinstance(view, str) else ("iso" if view is None else "custom")

        # Resolve emphasis target (accept either `target` or `emphasize`).
        emp_spec = emphasize if emphasize is not None else target
        emp_matches: list = []
        emp_resolved: Optional[dict] = None
        palette: Optional[dict] = None
        if emp_spec is not None:
            # A list of specs → union of matches.
            specs = emp_spec if isinstance(emp_spec, list) else [emp_spec]
            emp_matches = []
            labels = []
            for sp in specs:
                rsl = _resolve_inspect_target(d, sp)
                emp_matches.extend(rsl.get("matches") or [])
                labels.append(rsl.get("label") or str(sp))
            emp_resolved = {
                "matches": emp_matches,
                "label": ", ".join(labels) if labels else "target",
                "kind": "emphasis",
            }

        # Camera: when emphasizing, frame along the target's auto normal (or explicit view).
        view_dir = None
        if emp_matches and (view is None or view == "auto"):
            view_dir = _camera_dir_for_target(emp_matches, "auto")
            view_name = "emphasis-auto"
        elif view is not None:
            if emp_matches and isinstance(view, str) and view.lower() == "auto":
                view_dir = _camera_dir_for_target(emp_matches, "auto")
            else:
                # Named / dict view — let the engine resolve for non-emphasis; for emphasis we
                # still need a dir for the overlay projection.
                try:
                    view_dir = _camera_dir_for_target(emp_matches or [], view)
                except Exception:  # noqa: BLE001
                    view_dir = _vnorm((1.0, 1.0, 0.8))

        if view_dir is not None:
            r = cb.call("doc_render_view", {
                "doc_id": d, "out_path": png,
                "view": {"dir": list(view_dir)}, "annotate": bool(annotate),
            })
            if not r.get("ok") or not os.path.exists(png):
                raise ValueError(
                    f"Render failed: {r.get('reason', r.get('error', 'no bodies to render'))}")
        elif view is not None:
            r = cb.call("doc_render_view", {"doc_id": d, "out_path": png, "view": view,
                                           "annotate": bool(annotate)})
            if not r.get("ok") or not os.path.exists(png):
                raise ValueError(
                    f"Render failed: {r.get('reason', r.get('error', 'no bodies to render'))}")
            # Recover dir for optional crop when target present.
            if emp_matches:
                try:
                    view_dir = _camera_dir_for_target(emp_matches, view)
                except Exception:  # noqa: BLE001
                    view_dir = _vnorm((1.0, 1.0, 0.8))
        else:
            r = cb.call("doc_render_png", {"doc_id": d, "out_path": png, "annotate": bool(annotate)})
            if (not r.get("ok") or not os.path.exists(png)):
                try:
                    rd = cb.call("drawing_render_png", {"doc_id": d, "out_path": png})
                except Exception:  # noqa: BLE001
                    rd = {"ok": False}
                if rd.get("ok") and os.path.exists(png):
                    return _captioned_render(png, d, [view_name])
                raise ValueError(f"Render failed: {r.get('reason', 'no bodies to render')}")
            if emp_matches:
                view_dir = _vnorm((1.0, 1.0, 0.8))

        # Mesh-accurate overlay: B-rep edges (hidden-line removed), explicit `highlight`
        # targets, feature callouts and a scale bar, reprojected through the SAME camera the
        # engine just rendered. Runs on the FULL frame — before the emphasis crop, so a
        # target-framed render keeps its edges — and degrades to a no-op if the tessellation
        # or numpy is unavailable. Callouts/scale are suppressed when the image is about to be
        # cropped and rescaled (their placement and the bar length would no longer be true).
        cropping = bool(emp_matches) and view_dir is not None
        ann_info = None
        if annotate or highlight is not None:
            render_dir = view_dir if view_dir is not None else _vnorm((1.0, 1.0, 0.8))
            png, ann_info = _annotate_render(
                png, d, render_dir,
                highlight=highlight,
                edges=bool(annotate),
                callouts=bool(annotate) and not cropping,
                scale=bool(annotate) and not cropping)

        caption_extra = ""
        if emp_matches and view_dir is not None:
            try:
                part_bb = cb.call("doc_bbox", {"doc_id": d})
            except Exception:  # noqa: BLE001
                part_bb = None
            if part_bb and part_bb.get("size"):
                target_bb = _target_bbox(d, emp_matches)
                # Frame camera to feature AABB via crop (engine has no eye/bbox override).
                rect = _crop_rect_for_target(part_bb, target_bb, view_dir,
                                            margin=0.18, zoom_factor=1.35)
                png_crop = _tmp(".png")
                _crop_png_to_rect(png, rect, png_crop)
                png_emp, palette = _apply_emphasis_overlay(
                    png_crop, d, emp_matches, part_bb, view_dir,
                    mode=emphasis_mode or "emphasis")
                png = png_emp
                # Auto-section when the target is internal.
                if _target_is_internal(emp_matches, part_bb):
                    sec = _auto_section_for_target(d, emp_matches, target_bb)
                    caption_extra += f" Auto-section: {sec.get('summary') or sec.get('plane')}."
                caption_extra += _palette_caption_block(palette)
                if emp_resolved:
                    caption_extra = (
                        f" Emphasized {emp_resolved.get('label')!r}"
                        f" ({len(emp_matches)} subshape"
                        f"{'s' if len(emp_matches) != 1 else ''})."
                    ) + caption_extra

        result = _captioned_render(png, d, [view_name], annotations=ann_info,
                                   extra=caption_extra.strip())
        if isinstance(result, list) and len(result) == 2:
            return result
        return [result, caption_extra.strip()]



    @mcp.tool()
    def contact_sheet(doc_id: Optional[str] = None,
                      views: Optional[list] = None,
                      highlight: Optional[Union[str, dict, list]] = None,
                      annotate: bool = True) -> list:
        """Render SEVERAL labelled views tiled into ONE image — the fastest multi-angle self-check.

        Each tile is colour-coded, headed with its view name and the part's bounding-box
        dimensions, and (with annotate=True) carries the B-rep EDGE outline with hidden lines
        removed, feature CALLOUTS and a SCALE BAR — so you can read orientation, scale and
        which feature is which at a glance, and catch a feature that's right from one side but
        wrong from another.

        views: a list of view specs, each either a named view ("front"/"back"/"top"/"bottom"/
        "left"/"right"/"iso") or a custom-camera dict ({"azimuth","elevation"} or {"dir":[x,y,z]} —
        see render_part). Omit for the standard set: front + right + top + iso.
        Prefer render_views() for the full manufacturing 6-ortho+iso grid.

        highlight: same target vocabulary as render_part — a feature NAME ("Boss"), a feature id
        ("f2"), a sid ("F58db6d"), a geometry-query dict, or a list of those. The target is
        painted in a reserved emphasis colour and labelled IN EVERY TILE, which is the fastest
        way to see which views actually show it.

        annotate=False falls back to the engine's own tiled sheet with no overlay.

        VERIFY LOOP: use after mutations when a single iso is not enough to confirm a feature
        on a back/bottom face. Pair with verify_battery before declaring done.

        Returns [image, caption]: the tiled PNG followed by a SELF-SUFFICIENT text caption —
        views, part name and bbox, features seen across the sheet, what is highlighted and in
        what colour, the side-palette colour key, and what no tile could see.
        """
        d = _doc(doc_id)
        view_list = views or ["front", "right", "top", "iso"]
        if not annotate and highlight is None:
            png = _tmp(".png")
            r = cb.call("doc_render_contact_sheet",
                        {"doc_id": d, "out_path": png, "views": views or []})
            if not r.get("ok") or not os.path.exists(png):
                raise ValueError(
                    f"Contact sheet failed: "
                    f"{r.get('reason', r.get('error', 'no bodies to render'))}")
            return _captioned_render(png, d, view_list)

        tiles, labels, infos = _annotated_view_tiles(d, view_list, highlight=highlight,
                                                     annotate=annotate)
        if not tiles:
            raise ValueError("Contact sheet failed: no bodies to render")
        out = _tmp(".png")
        _tile_pngs_grid(tiles, out, labels=labels,
                        cols=max(1, int(math.ceil(math.sqrt(len(tiles))))))
        return _captioned_render(out, d, labels,
                                 annotations=_merge_annotation_infos(infos),
                                 extra=f"Contact sheet, {len(tiles)} tiles: "
                                       + ", ".join(labels) + ".")



    @mcp.tool()
    def render_views(doc_id: Optional[str] = None,
                     views: Optional[list] = None,
                     target: Optional[Union[str, dict]] = None,
                     highlight: Optional[Union[str, dict, list]] = None,
                     annotate: bool = True) -> list:
        """Multi-view contact sheet — 6 orthographic + iso by default, each tile labelled.

        The VLM-standard multi-view set for manufacturing feature recognition (holes, bosses,
        undercuts on back/bottom faces a single iso never shows). Default views:
          front, back, left, right, top, bottom, iso
        Override with `views=[...]` (named views and/or custom {"azimuth","elevation"} /
        {"dir":[x,y,z]} specs — same vocabulary as render_part).

        target (optional): when set, each tile is framed/cropped to the target AABB + margin
        (feature-focused multi-view). Plain-word face/edge query or feature id ("f2").

        highlight (optional): same target vocabulary as render_part — a feature NAME ("Boss"),
        a feature id ("f2"), a sid ("F58db6d"), a query dict, or a list of those. Painted in a
        reserved emphasis colour and labelled in EVERY tile, so you can see at a glance which
        of the seven views actually show it. Unlike `target` it does not change the framing.

        annotate (default True): face labels / triad in the engine render, PLUS the B-rep edge
        outline (hidden lines removed), feature callouts and a scale bar on every tile.
        annotate=False gives the engine's plain tiled sheet.

        VERIFY LOOP: call after mutations when you need full angular coverage; pair with
        inspect_feature for a single emphasized close-up and verify_battery before done.

        Returns [image, caption]. The caption is self-sufficient: resolved views, part name and
        bbox, the features seen across the sheet, what is highlighted and in what colour, the
        side-palette colour key, and what no view could see plus the tool to call for it.
        """
        d = _doc(doc_id)
        view_list = _normalize_view_list(views)
        if target is None:
            # Native engine sheet only when there is nothing to overlay (it tiles internally,
            # so there is no per-tile image to draw edges or a highlight onto).
            if not annotate and highlight is None:
                png = _tmp(".png")
                r = cb.call("doc_render_contact_sheet",
                            {"doc_id": d, "out_path": png, "views": view_list})
                if not r.get("ok") or not os.path.exists(png):
                    raise ValueError(
                        f"render_views failed: {r.get('reason', r.get('error', 'no bodies'))}")
                names = r.get("views") or [
                    (v if isinstance(v, str) else "custom") for v in view_list
                ]
                return _captioned_render(png, d, list(names),
                                         extra=f"render_views grid ({len(names)} tiles: "
                                               + ", ".join(str(n) for n in names) + ").")
            tiles, labels, infos = _annotated_view_tiles(d, view_list, highlight=highlight,
                                                         annotate=annotate)
            if not tiles:
                raise ValueError("render_views failed: no bodies to render")
            out = _tmp(".png")
            _tile_pngs_grid(tiles, out, labels=labels, cols=3)
            return _captioned_render(out, d, labels,
                                     annotations=_merge_annotation_infos(infos),
                                     extra=f"render_views grid ({len(tiles)} tiles: "
                                           + ", ".join(labels) + ").")

        # Target-framed path: per-view render + crop + tile with labels.
        resolved = _resolve_inspect_target(d, target)
        matches = resolved["matches"]
        target_bb = _target_bbox(d, matches)
        part_bb = cb.call("doc_bbox", {"doc_id": d})
        if not part_bb or not part_bb.get("size"):
            raise ValueError("no part bounding box — build geometry first")

        tile_paths, labels, infos = _annotated_view_tiles(
            d, view_list, highlight=highlight, annotate=annotate,
            part_bb=part_bb, target_bb=target_bb, matches=matches)
        if not tile_paths:
            raise ValueError("render_views: every tile render failed")
        out = _tmp(".png")
        # 3-column grid reads better for 6–7 views than the 2-col look_around default.
        _tile_pngs_grid(tile_paths, out, labels=labels, cols=3)
        caption = (
            f"render_views of {resolved.get('label')!r}: {len(tile_paths)} labelled tiles "
            f"({', '.join(labels)}), each cropped to the target. "
            + _inspect_caption(d, resolved, target_bb, _camera_dir_for_target(matches, "auto"),
                               method="crop", n_matches=len(matches))
        )
        ann = _merge_annotation_infos(infos)
        if ann:
            caption += " " + _render_annotation_caption("target-framed grid", ann)
        return [Image(path=out), caption]



    @mcp.tool()
    def inspect_part(target: Union[str, dict],
                     doc_id: Optional[str] = None,
                     view: Optional[Union[str, dict]] = "auto",
                     zoom_factor: float = 1.5,
                     margin: float = 0.15,
                     annotate: bool = True,
                     emphasize: bool = False,
                     emphasis_mode: str = "emphasis") -> list:
        """CLOSE-UP inspection of a face/edge/feature — zoom/pan like a human scanning a part.

        `render_part` / `contact_sheet` / `render_views` show the WHOLE part (or multi-view).
        Use this when you are working on a specific feature and need the same kind of view a
        person gets by panning onto it: the target framed tight (feature AABB + margin), from a
        sensible direction, with key dims in the caption.

        target: plain-word face/edge query (same vocabulary as query_geometry / measure_between
          — e.g. "top", {"of":"face","side":"top"}, {"of":"edge","surf":"circle"},
          "the filleted edge") OR a feature id from the tree ("f2").
        view: "auto" (default) = look along the target's face normal (or a mild iso for edges);
          or a named view / {"azimuth","elevation"} / {"dir":[x,y,z]} like render_part.
        zoom_factor: >1 tightens the crop (more zoom). Default 1.5.
        margin: fractional padding around the projected target bbox (default 0.15).
        annotate: pass through to the underlying render (face labels / triad on by default).
        emphasize: when True, paint the target with EMPHASIS (high-contrast fill + glow) and
          desaturate context; palette metadata is embedded in the caption.
        emphasis_mode: "emphasis" (shared colour) or "per_face" (distinct deterministic colours).

        Camera frames the feature AABB with margin; when the target is internal (bore/pocket)
        an auto-section plane is selected via the existing section machinery and reported in
        the caption. Method note: the render RPC only accepts a whole-part camera *direction*
        (no bbox/eye override), so the close-up is a whole-part render + projected-bbox CROP.

        VERIFY LOOP: use after mutations that touch this feature; prefer inspect_feature(target)
        for the one-call (render + describe excerpt + dims). Run verify_battery before done.

        Returns [image, caption]. Caption names what is framed + key dims (+ palette when
        emphasized). For multi-angle scanning of the same target, use look_around(...).
        """
        d = _doc(doc_id)
        resolved = _resolve_inspect_target(d, target)
        matches = resolved["matches"]
        target_bb = _target_bbox(d, matches)
        try:
            part_bb = cb.call("doc_bbox", {"doc_id": d})
        except Exception as e:  # noqa: BLE001
            raise ValueError(f"doc_bbox failed: {e}") from e
        if not part_bb or not part_bb.get("size"):
            raise ValueError("no part bounding box — build geometry first")

        view_dir = _camera_dir_for_target(matches, view)
        png_full = _tmp(".png")
        r = cb.call("doc_render_view", {
            "doc_id": d, "out_path": png_full,
            "view": {"dir": list(view_dir)},
            "annotate": bool(annotate),
        })
        if not r.get("ok") or not os.path.exists(png_full):
            r2 = cb.call("doc_render_png",
                         {"doc_id": d, "out_path": png_full, "annotate": bool(annotate)})
            if not r2.get("ok") or not os.path.exists(png_full):
                raise ValueError(
                    f"Inspect render failed: {r.get('reason', r.get('error', 'no bodies'))}")
            view_dir = _vnorm((1.0, 1.0, 0.8))

        rect = _crop_rect_for_target(part_bb, target_bb, view_dir,
                                    margin=float(margin), zoom_factor=float(zoom_factor))
        png_out = _tmp(".png")
        _crop_png_to_rect(png_full, rect, png_out)

        palette = None
        if emphasize:
            png_out, palette = _apply_emphasis_overlay(
                png_out, d, matches, part_bb, view_dir,
                mode=emphasis_mode or "emphasis")

        caption = _inspect_caption(d, resolved, target_bb, view_dir,
                                   method="crop", n_matches=len(matches))
        if _target_is_internal(matches, part_bb):
            sec = _auto_section_for_target(d, matches, target_bb)
            caption += f" Auto-section: {sec.get('summary') or sec.get('plane')}."
        if palette:
            caption += _palette_caption_block(palette)
        return [Image(path=png_out), caption]



    @mcp.tool()
    def look_around(target: Union[str, dict],
                    doc_id: Optional[str] = None,
                    n_views: int = 4,
                    zoom_factor: float = 1.25,
                    margin: float = 0.2,
                    annotate: bool = True) -> list:
        """Orbit close-ups of a target as a contact sheet — 'scan the part' around one feature.

        Resolves `target` the same way as inspect_part, then renders N close-ups (default 4 at
        90° steps) orbiting the auto face-normal view and tiles them into one image.

        VERIFY LOOP: use after mutations when a single close-up is ambiguous; pair with
        inspect_feature for dims/describe excerpt and verify_battery before done.

        Returns [image, caption]. Caption lists the orbit dirs + target dims. Same crop-fallback
        method as inspect_part (whole-part render + projected-bbox crop per tile).
        """
        d = _doc(doc_id)
        resolved = _resolve_inspect_target(d, target)
        matches = resolved["matches"]
        target_bb = _target_bbox(d, matches)
        part_bb = cb.call("doc_bbox", {"doc_id": d})
        if not part_bb or not part_bb.get("size"):
            raise ValueError("no part bounding box — build geometry first")

        base = _camera_dir_for_target(matches, "auto")
        dirs = _orbit_view_dirs(base, n=max(int(n_views), 1))
        tile_paths = []
        labels = []
        for i, vd in enumerate(dirs):
            png_full = _tmp(".png")
            r = cb.call("doc_render_view", {
                "doc_id": d, "out_path": png_full,
                "view": {"dir": list(vd)}, "annotate": bool(annotate),
            })
            if not r.get("ok") or not os.path.exists(png_full):
                continue
            rect = _crop_rect_for_target(part_bb, target_bb, vd,
                                        margin=float(margin), zoom_factor=float(zoom_factor))
            png_tile = _tmp(".png")
            _crop_png_to_rect(png_full, rect, png_tile)
            tile_paths.append(png_tile)
            labels.append(f"orbit {i * (360 // max(len(dirs), 1))}°")
        if not tile_paths:
            raise ValueError("look_around: every orbit render failed")
        out = _tmp(".png")
        _tile_pngs(tile_paths, out, labels=labels)
        caption = (
            f"Look-around of {resolved.get('label')!r}: {len(tile_paths)} orbit close-ups. "
            + _inspect_caption(d, resolved, target_bb, base, method="crop",
                               n_matches=len(matches))
        )
        return [Image(path=out), caption]



    @mcp.tool()
    def inspect_feature(target: Union[str, dict],
                        doc_id: Optional[str] = None,
                        view: Optional[Union[str, dict]] = "auto",
                        emphasis_mode: str = "emphasis",
                        annotate: bool = True) -> list:
        """One-call feature inspection: framed EMPHASIS render + describe_part excerpt + dims.

        Resolves `target` (plain-word face/edge query or feature id "f2"), frames the camera
        to the feature AABB with margin, paints the target with high-contrast emphasis
        (desaturated context + glow outline; `emphasis_mode` "per_face" for distinct colours),
        auto-sections when the target is internal, and returns a caption that already carries:
          * key dims of the target (area/length/radius)
          * a describe_part outline EXCERPT (feature placement / sids)
          * the emphasis palette metadata (assertable without pixel hashing)

        Prefer this over chaining inspect_part + describe_part + measure yourself when reviewing
        a single feature after a mutation.

        VERIFY LOOP: call after add_features/edit that touched this feature; then
        verify_battery(doc_id) before declaring the design done.

        Returns [image, caption].
        """
        d = _doc(doc_id)
        resolved = _resolve_inspect_target(d, target)
        matches = resolved["matches"]
        target_bb = _target_bbox(d, matches)
        part_bb = cb.call("doc_bbox", {"doc_id": d})
        if not part_bb or not part_bb.get("size"):
            raise ValueError("no part bounding box — build geometry first")

        view_dir = _camera_dir_for_target(matches, view)
        png_full = _tmp(".png")
        r = cb.call("doc_render_view", {
            "doc_id": d, "out_path": png_full,
            "view": {"dir": list(view_dir)}, "annotate": bool(annotate),
        })
        if not r.get("ok") or not os.path.exists(png_full):
            r2 = cb.call("doc_render_png",
                         {"doc_id": d, "out_path": png_full, "annotate": bool(annotate)})
            if not r2.get("ok") or not os.path.exists(png_full):
                raise ValueError(
                    f"inspect_feature render failed: {r.get('reason', r.get('error', 'no bodies'))}")
            view_dir = _vnorm((1.0, 1.0, 0.8))

        rect = _crop_rect_for_target(part_bb, target_bb, view_dir,
                                    margin=0.18, zoom_factor=1.45)
        png_crop = _tmp(".png")
        _crop_png_to_rect(png_full, rect, png_crop)
        png_out, palette = _apply_emphasis_overlay(
            png_crop, d, matches, part_bb, view_dir,
            mode=emphasis_mode or "emphasis")

        caption = _inspect_caption(d, resolved, target_bb, view_dir,
                                   method="crop", n_matches=len(matches))
        excerpt = _describe_part_excerpt(d)
        caption += f" describe_part excerpt: {excerpt}"
        if _target_is_internal(matches, part_bb):
            sec = _auto_section_for_target(d, matches, target_bb)
            caption += f" Auto-section: {sec.get('summary') or sec.get('plane')}."
        caption += _palette_caption_block(palette)
        return [Image(path=png_out), caption]
