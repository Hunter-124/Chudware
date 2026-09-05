"""CAD render + close-up inspection MCP tools (T-RF8 split of cad_tools.py): render_part, contact_sheet, render_views, inspect_part, look_around, inspect_feature."""
from __future__ import annotations

from mcp.server.fastmcp import FastMCP  # noqa: F401 — register signature
from mcp.server.fastmcp import Image
from typing import Optional
from typing import Union
import os
import math

from .cad_common import (
    _CLOSEUP_H,
    _CLOSEUP_MARGIN,
    _CLOSEUP_W,
    _annotate_render,
    _auto_section_for_target,
    _build_emphasis_palette,
    _camera_dir_for_target,
    _captioned_render,
    _describe_part_excerpt,
    _doc,
    _orbit_view_dirs,
    _frame_from_rpc,
    _handoff_doc_name,
    _ideal_views,
    _inspect_caption,
    _normalize_view_list,
    _palette_caption_block,
    _render_annotation_caption,
    _render_view_png,
    _resolve_inspect_target,
    _target_bbox,
    _target_is_internal,
    _tile_pngs_grid,
    _tmp,
    _vnorm,
    cb,
    render_output_path,
)


def _render_stem(doc_id: str, label: str = "") -> str:
    """Filename stem naming what a render is OF: the resolved document name (else the doc
    id) joined with the view / target label, so a file lands as
    ``renders/2026-07-28/mudbucket-iso-3f2a9c11.png`` and is self-describing both on disk
    and in the GUI's Artifacts panel."""
    base = (_handoff_doc_name(doc_id) or "").strip() or str(doc_id or "part")
    label = str(label or "").strip()
    return f"{base}-{label}" if label else base


def _annotated_view_tiles(d: str, view_list: list, highlight=None, annotate: bool = True,
                          part_bb: Optional[dict] = None, target_bb: Optional[dict] = None,
                          matches: Optional[list] = None,
                          emphasis_mode: Optional[str] = None,
                          margin: float = _CLOSEUP_MARGIN,
                          clip_near: bool = False) -> tuple:
    """Render each view on its own, overlay the mesh annotation, return (tiles, labels, infos).

    The native doc_render_contact_sheet RPC tiles inside the engine, so there is no per-tile
    image to draw B-rep edges or a highlight onto. Rendering view-by-view costs ~11 ms of
    engine time per tile and buys every tile the same annotation a single render_part gets.

    With `target_bb` every tile is FRAMED on the target by the engine (doc_render_view's
    `focus`), not cropped out of a whole-part shot — the difference between a 32x32 patch
    upscaled 16x and a real close-up — and the overlay reprojects through the camera the
    engine reports, so the emphasis mask lands on the actual target pixels. `clip_near` cuts
    the near material away in every tile, which is the only way an ortho sweep of an INTERNAL
    target shows anything at all — uncut, six of the seven tiles are a flat slab."""
    focused = bool(target_bb)
    emphasis = ({"matches": list(matches or []), "mode": emphasis_mode or "emphasis",
                 "label": None} if focused and emphasis_mode and matches else None)
    tiles, labels, infos = [], [], []
    for v in view_list:
        try:
            vd = _camera_dir_for_target(matches or [], v if v != "auto" else "auto")
        except Exception:  # noqa: BLE001
            vd = _vnorm((1.0, 1.0, 0.8))
        name = v if isinstance(v, str) else "custom"
        # Internal plumbing: every tile is composed into ONE sheet by the caller
        # (_tile_pngs_grid) and is never handed to the model, so tiles stay on the scratch
        # prefix — only the finished sheet gets a durable render_output_path.
        png_tile = _tmp(".png")
        try:
            png_tile, frame, _r = _render_view_png(
                d, vd, png_tile, name=name.upper(),
                focus=target_bb if focused else None, margin=margin,
                clip_near=bool(clip_near) and focused,
                W=_CLOSEUP_W if focused else 512, H=_CLOSEUP_H if focused else 400,
                annotate=bool(annotate))
        except ValueError:
            continue
        info = None
        if annotate or highlight is not None or emphasis is not None:
            png_tile, info = _annotate_render(
                png_tile, d, vd, highlight=highlight, edges=bool(annotate),
                callouts=bool(annotate) and not focused,
                scale=bool(annotate), frame=frame, emphasis=emphasis)
        tiles.append(png_tile)
        labels.append(name)
        infos.append(info)
    return tiles, labels, infos


def _closeup(d: str, matches: list, target_bb: dict, view_dir, *, label: str,
             stem: str, emphasis_mode: Optional[str] = "emphasis",
             margin: float = _CLOSEUP_MARGIN, clip_near: bool = False,
             annotate: bool = True, W: int = _CLOSEUP_W, H: int = _CLOSEUP_H,
             highlight=None) -> tuple:
    """ONE idealized close-up: engine-framed render + exact-mask emphasis overlay.

    This is the single close-up implementation every inspect/render tool goes through. The
    camera frames the target's own AABB inside the engine, so the feature is rendered at the
    resolution the model reads it at; the overlay then reprojects through the engine's echoed
    frame, which is what makes the emphasis paint land on the target instead of beside it.
    Returns (png_path, info) — `info["emphasis"]` carries the palette and the MEASURED
    coverage. The palette is built here and always reported, even when the tessellation is
    missing and the mask could not be painted: the caption's palette block is the machine-
    readable half of the contract, and losing it silently would leave a caller unable to say
    which colour means what."""
    png = render_output_path(stem, ".png")
    png, frame, _r = _render_view_png(d, view_dir, png, name=label.upper()[:28],
                                      focus=target_bb, margin=margin, clip_near=clip_near,
                                      W=W, H=H, annotate=annotate)
    palette = (_build_emphasis_palette(matches or [], mode=emphasis_mode)
               if emphasis_mode else None)
    emphasis = ({"matches": list(matches or []), "mode": emphasis_mode, "label": label,
                 "palette": palette} if emphasis_mode else None)
    png, info = _annotate_render(png, d, view_dir, highlight=highlight,
                                 edges=True, callouts=False, scale=True,
                                 frame=frame, emphasis=emphasis,
                                 out_png=render_output_path(stem, ".png"))
    if palette is not None and not (info.get("emphasis") or {}).get("palette"):
        info["emphasis"] = dict(info.get("emphasis") or {},
                                palette=palette, mode=palette.get("mode"), label=label,
                                target_px=0, coverage=0.0,
                                note=(info.get("note")
                                      or "no tessellation — emphasis mask not painted"))
    return png, info


def _visibility_note(info: Optional[dict]) -> str:
    """What the camera ACTUALLY saw of the emphasized target, in words.

    A close-up that frames a face the camera cannot see is worse than no picture at all,
    because the model reads the empty frame as evidence. Say it instead."""
    emp = (info or {}).get("emphasis") or {}
    if not emp:
        return ""
    seen, hidden = emp.get("visible_sids") or [], emp.get("hidden_sids") or []
    edges = emp.get("edge_sids") or []
    bits = []
    if seen:
        bits.append(f"{len(seen)} of {len(seen) + len(hidden) + len(edges)} target subshape"
                    f"{'s' if len(seen) + len(hidden) + len(edges) != 1 else ''} visible from "
                    f"this camera ({emp.get('coverage', 0) * 100:.1f}% of the frame)")
    if edges:
        bits.append("emphasized edge(s) " + ", ".join(edges[:4]) + " stroked in target colour")
    if hidden:
        bits.append("NOT visible from here: " + ", ".join(hidden[:6])
                    + " — call inspect_section or look_around for another angle")
    if not seen and not edges:
        bits.append("the target is NOT visible from this camera")
    return (" " + "; ".join(bits) + ".") if bits else ""


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
                    annotate: bool = True,
                    display_quality: Optional[str] = None) -> list:
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

        display_quality (optional): re-tessellate the document's DISPLAY mesh before rendering —
          "draft", "standard" or "fine". Higher tiers use a finer chord/angular deflection, so
          curved geometry (tubes, wheels, fillets) stops looking faceted at the cost of triangles.
          The setting PERSISTS on the document (it is a view setting — no revision bump), so pass
          it once and every later render/viewport draw uses it. Documents default to "fine"
          (override the process default with CHUDWARE_DISPLAY_QUALITY).
          Tiers only affect B-rep bodies, which are tessellated on demand: a mesh-derived
          import (STL/OBJ, or a SolidWorks file opened from its saved display data) already
          carries baked facets, so its density does not change however fine the tier.

        Returns [image, caption]: the PNG followed by a SELF-SUFFICIENT text caption — part name
        and bbox, the features visible in this view as id+name, what is highlighted and in what
        colour, the side-palette colour key, and an explicit note of what this camera CANNOT see
        plus the tool to call for it. A model that reads only the caption still knows the scene.
        """
        d = _doc(doc_id)
        if display_quality is not None:
            # Persists on the document (view setting, no revision bump) — nothing to restore.
            # Let a bad tier name surface: the engine refuses instead of guessing.
            cb.call("doc_set_display_quality",
                    {"doc_id": d, "quality": str(display_quality).strip().lower()})
        view_name = view if isinstance(view, str) else ("iso" if view is None else "custom")
        # Durable, not scratch: this exact path is returned when annotate=False with nothing
        # emphasized, and by the drawing-render fallback below.
        png = render_output_path(_render_stem(d, view_name), ".png")

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

        # Camera: when emphasizing, pick the IDEALIZED direction (occlusion-scored) unless the
        # caller named one; the old mean-face-normal guess routinely looked straight down a bore
        # axis or into the wall the feature hides behind. Whether the render will cut away the
        # near material is decided FIRST, because the scorer has to measure the picture that
        # will actually be produced.
        view_dir = None
        target_bb = None
        internal = False
        part_bb = None
        if emp_matches:
            target_bb = _target_bbox(d, emp_matches)
            try:
                part_bb = cb.call("doc_bbox", {"doc_id": d})
            except Exception:  # noqa: BLE001
                pass
            internal = bool(part_bb and part_bb.get("size")
                            and _target_is_internal(emp_matches, part_bb))
        if emp_matches and (view is None or view == "auto"
                            or (isinstance(view, str) and view.lower() == "auto")):
            picks = _ideal_views(d, emp_matches, target_bb, n=1, clip_near=internal)
            view_dir = picks[0]["dir"]
            view_name = f"emphasis-{picks[0].get('label') or 'auto'}"
        elif view is not None:
            # Named / dict view — let the engine resolve for non-emphasis; for emphasis we
            # still need a dir for the overlay projection.
            try:
                view_dir = _camera_dir_for_target(emp_matches or [], view)
            except Exception:  # noqa: BLE001
                view_dir = _vnorm((1.0, 1.0, 0.8))

        caption_extra = ""
        ann_info = None
        if emp_matches and target_bb is not None:
            # A real close-up: the ENGINE frames the target AABB, so a Ø5 hole in a 120 mm plate
            # is rendered at 5 mm scale instead of cropped out of a whole-part shot and upscaled
            # 16x. Internal targets additionally get a near-clip cutaway so the camera can see in.
            png, ann_info = _closeup(
                d, emp_matches, target_bb, view_dir,
                label=str(emp_resolved.get("label") if emp_resolved else "target"),
                stem=_render_stem(d, view_name),
                emphasis_mode=emphasis_mode or "emphasis",
                clip_near=internal, annotate=bool(annotate), highlight=highlight)
            palette = ((ann_info.get("emphasis") or {}).get("palette")) if ann_info else None
            caption_extra = (
                f" Emphasized {emp_resolved.get('label')!r}"
                f" ({len(emp_matches)} subshape"
                f"{'s' if len(emp_matches) != 1 else ''}), camera framed on the target AABB"
                f"{' with a near-clip cutaway (internal target)' if internal else ''}."
            ) + _visibility_note(ann_info)
            if internal:
                sec = _auto_section_for_target(d, emp_matches, target_bb)
                caption_extra += f" Auto-section: {sec.get('summary') or sec.get('plane')}."
            caption_extra += _palette_caption_block(palette)
        else:
            if view_dir is not None:
                png, frame, _r = _render_view_png(d, view_dir, png, name=str(view_name).upper(),
                                                  annotate=bool(annotate))
            elif view is not None:
                r = cb.call("doc_render_view", {"doc_id": d, "out_path": png, "view": view,
                                                "annotate": bool(annotate)})
                if not r.get("ok") or not os.path.exists(png):
                    raise ValueError(
                        f"Render failed: "
                        f"{r.get('reason', r.get('error', 'no bodies to render'))}")
                frame = _frame_from_rpc(r)
            else:
                frame = None
                r = cb.call("doc_render_png",
                            {"doc_id": d, "out_path": png, "annotate": bool(annotate)})
                if not r.get("ok") or not os.path.exists(png):
                    try:
                        rd = cb.call("drawing_render_png", {"doc_id": d, "out_path": png})
                    except Exception:  # noqa: BLE001
                        rd = {"ok": False}
                    if rd.get("ok") and os.path.exists(png):
                        return _captioned_render(png, d, [view_name])
                    raise ValueError(f"Render failed: {r.get('reason', 'no bodies to render')}")
            # Mesh-accurate overlay: B-rep edges (hidden-line removed), explicit `highlight`
            # targets, feature callouts and a scale bar, reprojected through the SAME camera the
            # engine just rendered — its own echoed frame when it gave us one. Degrades to a
            # no-op if the tessellation or numpy is unavailable.
            if annotate or highlight is not None:
                render_dir = view_dir if view_dir is not None else _vnorm((1.0, 1.0, 0.8))
                png, ann_info = _annotate_render(
                    png, d, render_dir, highlight=highlight, frame=frame,
                    edges=bool(annotate), callouts=bool(annotate), scale=bool(annotate),
                    out_png=render_output_path(_render_stem(d, view_name), ".png"))

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
            png = render_output_path(_render_stem(d, "contact-sheet"), ".png")
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
        out = render_output_path(_render_stem(d, "contact-sheet"), ".png")
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
                png = render_output_path(_render_stem(d, "views"), ".png")
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
            out = render_output_path(_render_stem(d, "views"), ".png")
            _tile_pngs_grid(tiles, out, labels=labels, cols=3)
            return _captioned_render(out, d, labels,
                                     annotations=_merge_annotation_infos(infos),
                                     extra=f"render_views grid ({len(tiles)} tiles: "
                                           + ", ".join(labels) + ").")

        # Target-framed path: the ENGINE frames each tile on the target AABB (a real close-up
        # per view), and every tile carries the exact-mask emphasis so you can tell at a glance
        # which of the seven cameras actually sees the feature.
        resolved = _resolve_inspect_target(d, target)
        matches = resolved["matches"]
        target_bb = _target_bbox(d, matches)
        part_bb = cb.call("doc_bbox", {"doc_id": d})
        if not part_bb or not part_bb.get("size"):
            raise ValueError("no part bounding box — build geometry first")

        tile_paths, labels, infos = _annotated_view_tiles(
            d, view_list, highlight=highlight, annotate=annotate,
            part_bb=part_bb, target_bb=target_bb, matches=matches,
            emphasis_mode="emphasis",
            clip_near=_target_is_internal(matches, part_bb))
        if not tile_paths:
            raise ValueError("render_views: every tile render failed")
        out = render_output_path(
            _render_stem(d, f"{resolved.get('label') or 'target'}-views"), ".png")
        # 3-column grid reads better for 6–7 views than the 2-col look_around default.
        _tile_pngs_grid(tile_paths, out, labels=labels, cols=3)
        seen = [labels[i] for i, inf in enumerate(infos)
                if ((inf or {}).get("emphasis") or {}).get("target_px")]
        caption = (
            f"render_views of {resolved.get('label')!r}: {len(tile_paths)} labelled tiles "
            f"({', '.join(labels)}), each FRAMED on the target by the engine. "
            f"Views that actually show the target: "
            f"{', '.join(seen) if seen else 'none — try inspect_section'}. "
            + _inspect_caption(d, resolved, target_bb,
                               _ideal_views(d, matches, target_bb, n=1)[0]["dir"],
                               method="focus", n_matches=len(matches))
        )
        ann = _merge_annotation_infos(infos)
        if ann:
            caption += " " + _render_annotation_caption("target-framed grid", ann)
        return [Image(path=out), caption]



    @mcp.tool()
    def inspect_part(target: Union[str, dict],
                     doc_id: Optional[str] = None,
                     view: Optional[Union[str, dict]] = "auto",
                     zoom_factor: float = 1.0,
                     margin: float = _CLOSEUP_MARGIN,
                     annotate: bool = True,
                     emphasize: bool = True,
                     emphasis_mode: str = "emphasis",
                     cutaway: Optional[bool] = None) -> list:
        """CLOSE-UP inspection of a face/edge/feature — the camera flies to it and frames it.

        `render_part` / `contact_sheet` / `render_views` show the WHOLE part (or multi-view).
        Use this when you are working on a specific feature: the ENGINE frames the target's own
        bounding box, so a Ø5 hole in a 120 mm plate is RENDERED at 5 mm scale — real pixels,
        not a magnified patch of a whole-part shot.

        target: plain-word face/edge query (same vocabulary as query_geometry / measure_between
          — e.g. "top", {"of":"face","side":"top"}, {"of":"edge","surf":"circle"},
          "the filleted edge") OR a feature id from the tree ("f2").
        view: "auto" (default) = the IDEALIZED camera — every candidate direction is scored by
          how much of the target it can actually SEE (occlusion-tested against the tessellation)
          and how legibly (a dead-on view flattens a pocket into a boss, an edge-on view shows
          nothing), and the winner is deterministic. Or name one: a named view /
          {"azimuth","elevation"} / {"dir":[x,y,z]} like render_part.
        zoom_factor: >1 tightens the framing (1.0 = the target AABB plus `margin`).
        margin: fractional padding around the target box — how much surrounding part stays in
          frame for context. Default 0.35.
        annotate: face tint / triad in the engine render, plus B-rep edges and a scale bar.
        emphasize (default True): paint the target's EXACT visible faces in the emphasis palette
          with their shading preserved, desaturate the rest, and report measured coverage.
        emphasis_mode: "emphasis" (shared colour) or "per_face" (distinct deterministic colours).
        cutaway: None (default) = auto — an internal target (bore/pocket) gets a near-clip that
          cuts away whatever sits between the camera and it, so you see INTO it. True/False force.

        VERIFY LOOP: use after mutations that touch this feature; prefer inspect_feature(target)
        for the one-call multi-panel sheet. Run verify_battery before declaring done.

        Returns [image, caption]. The caption names what is framed, its key dims, HOW MUCH of
        the target this camera can actually see, and what it cannot.
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

        internal = _target_is_internal(matches, part_bb)
        clip = internal if cutaway is None else bool(cutaway)
        if view is None or (isinstance(view, str) and view.lower() == "auto"):
            # Score the picture that will actually be rendered — cutaway included.
            pick = _ideal_views(d, matches, target_bb, n=1, clip_near=clip)[0]
            view_dir, view_label = pick["dir"], pick.get("label") or "auto"
        else:
            view_dir, view_label = _camera_dir_for_target(matches, view), (
                view if isinstance(view, str) else "custom")

        label = str(resolved.get("label") or "target")
        png_out, info = _closeup(
            d, matches, target_bb, view_dir, label=label,
            stem=_render_stem(d, label),
            emphasis_mode=(emphasis_mode or "emphasis") if emphasize else None,
            margin=float(margin) / max(float(zoom_factor), 0.25),
            clip_near=clip, annotate=bool(annotate))

        caption = _inspect_caption(d, resolved, target_bb, view_dir,
                                   method="focus", n_matches=len(matches))
        caption += f" Camera: {view_label}."
        if clip:
            caption += (" Near-clip CUTAWAY: material between the camera and the target has been"
                        " cut away so you can see into it.")
        caption += _visibility_note(info)
        if internal:
            sec = _auto_section_for_target(d, matches, target_bb)
            caption += f" Auto-section: {sec.get('summary') or sec.get('plane')}."
        palette = ((info.get("emphasis") or {}).get("palette")) if info else None
        if palette:
            caption += _palette_caption_block(palette)
        return [Image(path=png_out), caption]



    @mcp.tool()
    def look_around(target: Union[str, dict],
                    doc_id: Optional[str] = None,
                    n_views: int = 4,
                    zoom_factor: float = 1.0,
                    margin: float = _CLOSEUP_MARGIN,
                    annotate: bool = True,
                    emphasize: bool = True,
                    ranked: bool = True) -> list:
        """Multi-angle close-ups of ONE target, tiled — 'walk around' the feature.

        Resolves `target` like inspect_part, then renders N close-ups of it. Every tile is
        FRAMED on the target by the engine (real close-up pixels, not a magnified crop) and
        carries the exact-mask emphasis, so you can see at a glance which angles show the
        feature and which do not.

        ranked=True (default): the angles are the top-N IDEALIZED cameras — scored by how much
          of the target each actually sees and how legibly, and forced apart from each other so
          four tiles carry four different pieces of information.
        ranked=False: a plain 360°/N orbit around the target's own normal (the legacy sweep) —
          use when you specifically want even angular coverage rather than the best views.

        VERIFY LOOP: use after mutations when a single close-up is ambiguous; pair with
        inspect_feature for the one-call sheet + dims, and verify_battery before done.

        Returns [image, caption]. The caption ranks the angles by measured target visibility.
        """
        d = _doc(doc_id)
        resolved = _resolve_inspect_target(d, target)
        matches = resolved["matches"]
        target_bb = _target_bbox(d, matches)
        part_bb = cb.call("doc_bbox", {"doc_id": d})
        if not part_bb or not part_bb.get("size"):
            raise ValueError("no part bounding box — build geometry first")

        n = max(int(n_views), 1)
        # An internal target is invisible from every exterior angle, so a walk-around of a bore
        # without the cutaway is four pictures of a plate. Cut, and score the cut picture.
        clip = _target_is_internal(matches, part_bb)
        if ranked:
            # A walk-around wants coverage, so it accepts closer angles than the 3-panel
            # inspect sheet does; at 75 deg the lattice only admits three mutually separated
            # axes and n_views=4 would silently come back as three tiles.
            picks = _ideal_views(d, matches, target_bb, n=n, clip_near=clip,
                                 min_sep_deg=35.0)
        else:
            base = _ideal_views(d, matches, target_bb, n=1, clip_near=clip)[0]["dir"]
            step = 360 // n
            picks = [{"dir": vd, "label": f"orbit {i * step}deg"}
                     for i, vd in enumerate(_orbit_view_dirs(base, n=n))]

        label = str(resolved.get("label") or "target")
        tiles, labels, infos = [], [], []
        for i, p in enumerate(picks):
            try:
                png, info = _closeup(
                    d, matches, target_bb, p["dir"], label=label,
                    stem=_render_stem(d, f"{label}-view{i + 1}"),
                    emphasis_mode="emphasis" if emphasize else None,
                    margin=float(margin) / max(float(zoom_factor), 0.25),
                    clip_near=clip, annotate=bool(annotate))
            except ValueError:
                continue
            vis = ((info or {}).get("emphasis") or {}).get("coverage")
            tiles.append(png)
            labels.append(f"{p.get('label') or f'view {i + 1}'}"
                          + (f"  {vis * 100:.1f}% target" if vis else "  target hidden"))
            infos.append(info)
        if not tiles:
            raise ValueError("look_around: every close-up render failed")
        out = render_output_path(_render_stem(d, f"{label}-look-around"), ".png")
        _tile_pngs_grid(tiles, out, labels=labels, cols=2 if len(tiles) > 1 else 1)
        best = max(range(len(infos)),
                   key=lambda i: ((infos[i] or {}).get("emphasis") or {}).get("coverage") or 0)
        caption = (
            f"Look-around of {label!r}: {len(tiles)} engine-framed close-ups"
            f"{' (idealized, ranked)' if ranked else ' (even orbit)'} — "
            + "; ".join(labels) + f". Best angle: {labels[best]}. "
            + _inspect_caption(d, resolved, target_bb, picks[best]["dir"],
                               method="focus", n_matches=len(matches))
        )
        return [Image(path=out), caption]



    @mcp.tool()
    def inspect_feature(target: Union[str, dict],
                        doc_id: Optional[str] = None,
                        view: Optional[Union[str, dict]] = "auto",
                        emphasis_mode: str = "emphasis",
                        annotate: bool = True,
                        panels: int = 4) -> list:
        """One-call feature inspection: a multi-panel EMPHASIS sheet + describe excerpt + dims.

        This is the tool to reach for when you want to KNOW what a feature looks like. It
        resolves `target` (plain-word face/edge query or feature id "f2") and returns ONE image
        built from deterministically chosen panels, so a single call answers "what is it, what
        does it look like from a useful angle, and where is it on the part":

          1. HERO      — the idealized camera: of all candidate directions, the one that
                         actually SEES the most of the target (occlusion-tested against the
                         tessellation) at a legible angle. Engine-framed on the target AABB, so
                         these are real close-up pixels rather than an upscaled crop.
          2. ALT       — the best camera at least 75° away from the hero (measured on the
                         undirected axis, so the mirror-image view does not count), so the
                         second panel carries new information instead of repeating the first.
          3. CUTAWAY / AS-BUILT — an INTERNAL target (bore, pocket, counterbore) is invisible
                         from outside, so HERO and ALT are already rendered with a near-clip
                         that removes everything between the camera and the feature, and this
                         third panel is the SAME camera uncut: what the part actually looks
                         like from there. For an external target it is a third idealized angle.
          4. CONTEXT   — the WHOLE part with the target highlighted and labelled, so the
                         close-ups above are placeable on the model.

        In every close-up panel the target's exact visible faces are painted in the emphasis
        palette (shading preserved, so curvature still reads), everything else is desaturated,
        and the boundary is outlined. `emphasis_mode="per_face"` gives each face its own colour.
        `panels` trims the sheet (1 = hero only, 2 = hero+alt, 3 = +cutaway/alt2, 4 = +context).
        `view` pins the HERO camera instead of letting the scorer choose.

        The caption already carries: key dims of the target (area/length/radius), a describe_part
        outline EXCERPT, the emphasis palette metadata, and — per panel — how much of the target
        that camera could actually see. A panel that shows nothing SAYS it shows nothing.

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

        want = max(1, min(int(panels), 4))
        internal = _target_is_internal(matches, part_bb)
        label = str(resolved.get("label") or "target")
        mode = emphasis_mode or "emphasis"

        hint = None if (view is None or (isinstance(view, str) and view.lower() == "auto")) else view
        # An internal target is only visible through a cutaway, so for a bore or a pocket the
        # close-up panels ARE cut and the scorer measures them cut; the as-built exterior is
        # kept as its own panel rather than wasted on the hero. 75 deg of separation, not the
        # 50 deg default: two panels 50 deg apart look like the same picture, and the point of
        # a second panel is that it says something new.
        picks = _ideal_views(d, matches, target_bb, n=3, min_sep_deg=75.0, clip_near=internal)
        if hint is not None:
            picks = [{"dir": _camera_dir_for_target(matches, hint),
                      "label": hint if isinstance(hint, str) else "custom"}] + picks

        panels_spec: list = [("HERO", picks[0], internal)]
        if want >= 2 and len(picks) > 1:
            panels_spec.append(("ALT", picks[1], internal))
        if want >= 3:
            if internal:
                # Same camera, uncut: what the part actually looks like from there.
                panels_spec.append(("AS-BUILT", picks[0], False))
            elif len(picks) > 2:
                panels_spec.append(("ALT2", picks[2], False))

        tiles, labels, infos = [], [], []
        for i, (tag, pick, clip) in enumerate(panels_spec):
            try:
                png, info = _closeup(
                    d, matches, target_bb, pick["dir"], label=label,
                    stem=_render_stem(d, f"{label}-{tag.lower()}"),
                    emphasis_mode=mode, clip_near=clip, annotate=bool(annotate))
            except ValueError:
                continue
            cov = ((info or {}).get("emphasis") or {}).get("coverage") or 0.0
            tiles.append(png)
            labels.append(f"{tag}  {pick.get('label') or ''}  {cov * 100:.1f}% target")
            infos.append(info)

        # CONTEXT: the whole part with the target highlighted, so the close-ups are placeable.
        if want >= 4:
            try:
                ctx = _tmp(".png")
                ctx, ctx_frame, _r = _render_view_png(
                    d, _vnorm((1.0, 1.0, 0.8)), ctx, name="CONTEXT",
                    W=_CLOSEUP_W, H=_CLOSEUP_H, annotate=bool(annotate))
                ctx, ctx_info = _annotate_render(
                    ctx, d, _vnorm((1.0, 1.0, 0.8)), highlight=target, frame=ctx_frame,
                    edges=True, callouts=False, scale=True,
                    out_png=render_output_path(_render_stem(d, f"{label}-context"), ".png"))
                tiles.append(ctx)
                labels.append(f"CONTEXT  whole part, {label} highlighted")
                infos.append(ctx_info)
            except ValueError:
                pass

        if not tiles:
            raise ValueError(
                f"inspect_feature: every panel render failed for {label!r} — call "
                f"render_part(doc_id={d!r}) to check the part renders at all.")
        out = (tiles[0] if len(tiles) == 1 else
               _tile_pngs_grid(tiles,
                               render_output_path(_render_stem(d, f"{label}-inspect"), ".png"),
                               labels=labels, cols=2))

        caption = _inspect_caption(d, resolved, target_bb, panels_spec[0][1]["dir"],
                                   method="focus", n_matches=len(matches))
        caption += (f" Sheet: {len(tiles)} panel{'s' if len(tiles) != 1 else ''} — "
                    + "; ".join(labels) + ".")
        caption += _visibility_note(infos[0] if infos else None)
        caption += f" describe_part excerpt: {_describe_part_excerpt(d)}"
        if internal:
            sec = _auto_section_for_target(d, matches, target_bb)
            caption += f" Auto-section: {sec.get('summary') or sec.get('plane')}."
        palette = ((infos[0].get("emphasis") or {}).get("palette")) if infos and infos[0] else None
        caption += _palette_caption_block(palette)
        return [Image(path=out), caption]
