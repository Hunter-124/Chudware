"""CAD render + close-up inspection MCP tools (split out of cad_tools.py): render_part, contact_sheet, render_views, inspect_part, look_around, inspect_feature."""
from __future__ import annotations

from mcp.server.fastmcp import FastMCP  # noqa: F401 — register signature
from mcp.server.fastmcp import Image
from typing import Callable, Optional
from typing import Union
import hashlib
import json
import os
import math

from . import cad_viewpoint as _vp
from . import shot_planner as _sp
from .cad_common import (
    _CLOSEUP_H,
    _CLOSEUP_MARGIN,
    _CLOSEUP_W,
    _CONTEXT_DESAT,
    _EMPHASIS_GLOW,
    _HIGHLIGHT_PALETTE,
    _SID_RE,
    _as_face_query,
    _annot_font,
    _annotate_render,
    _auto_section_for_target,
    _build_emphasis_palette,
    _camera_dir_for_target,
    _captioned_render,
    _describe_part_excerpt,
    _doc,
    _doc_mesh,
    _doc_topo_maps,
    _orbit_view_dirs,
    _frame_from_rpc,
    _handoff_doc_name,
    _inspect_caption,
    _match_point_cloud,
    _normalize_view_list,
    _palette_caption_block,
    _render_annotation_caption,
    _render_view_png,
    _resolve_inspect_target,
    _screen_xyz,
    _sanitize_output_path,
    _target_bbox,
    _target_is_internal,
    _tile_pngs_grid,
    _tmp,
    _vnorm,
    cb,
    render_background,
    render_output_path,
    require_list,
    resolve_agent_path,
)
# The FEA setup resolver stack, reused verbatim by render_study_setup so a study FIGURE
# and the study run_fea_static would solve can never diverge (no second interpretation of
# the same face words, and the same refusals).
from .cad_sim_tools import (
    _BC_SHAPE_HELP,
    _StudyRefusal,
    _build_load_specs,
    _norm_bc_list,
    _reject_fully_constrained_loads,
    _sim_face_points,
)
from .tool_registry import RISK_WRITE, is_risk_denied

def _study_face_refs(study: dict):
    """The persisted MCP queries and GUI selectors, not solver node/point sets."""
    for key in ("fixtures", "loads", "hot_faces", "cold_faces"):
        for item in study.get(key) or ():
            if isinstance(item, dict) and "face" in item:
                yield item["face"]
            elif isinstance(item, dict) and "faces" in item:
                faces = item["faces"]
                yield from faces if isinstance(faces, list) else [faces]
            elif key == "fixtures":
                yield item


def _mate_face_matches(ref: dict, components: dict, topo: dict, *,
                       metrics: Optional[dict] = None,
                       body_components: Optional[dict] = None) -> list:
    """Join mate picks by component-local topology, independent of solved placement.

    Native assembly tessellation preserves each component's local topology indices. Its
    body/component table disambiguates coincident faces, even when their SIDs coincide.
    Without this table only an unambiguous exact geometric selector can be joined.
    """
    component = components.get(ref.get("component"))
    sel = ref.get("selector") or {}
    if component is None or component.get("suppressed") or not isinstance(sel, dict):
        return []
    kind = sel.get("kind")
    if kind not in ("face", "edge") or not sel.get("centroid"):
        return []
    if metrics is not None and body_components is not None:
        local = sel.get("hint_index")
        if not isinstance(local, int):
            return []
        table = metrics["faces" if kind == "face" else "edges"]
        id_key = "face_id" if kind == "face" else "edge_id"
        for row in table:
            if (body_components.get(row.get("body")) == ref.get("component")
                    and row.get(id_key) == local + 1 and row.get("ordinal") is not None):
                match = topo[kind][int(row["ordinal"])]
                if (match.get("surf") == sel.get("surf")
                        and math.isclose(float(match.get("area_or_len") or 0),
                                         float(sel.get("area_or_len") or 0),
                                         rel_tol=1e-5, abs_tol=1e-4)):
                    return [match]
        return []
    transform = component.get("transform") or [1, 0, 0, 0, 0, 1, 0, 0,
                                               0, 0, 1, 0, 0, 0, 0, 1]
    if len(transform) != 16:
        return []
    c = sel["centroid"]
    world = [sum(float(transform[4 * i + j]) * float(c[j]) for j in range(3))
             + float(transform[4 * i + 3]) for i in range(3)]
    direction = sel.get("axis") if sel.get("surf") == "cylinder" else sel.get("normal")
    world_direction = (_vnorm([sum(float(transform[4 * i + j]) * float(direction[j])
                                  for j in range(3)) for i in range(3)])
                       if direction else None)
    candidates = []
    for match in topo[kind]:
        mc = match.get("centroid")
        if not mc or match.get("surf") != sel.get("surf"):
            continue
        if math.dist(world, mc) > 1e-4:
            continue
        md = match.get("axis") if sel.get("surf") == "cylinder" else match.get("normal")
        if world_direction and md:
            alignment = sum(a * b for a, b in zip(world_direction, _vnorm(md)))
            if (abs(alignment) if sel.get("surf") == "cylinder" else alignment) < .99999:
                continue
        area = sel.get("area_or_len")
        if area is not None and not math.isclose(
                float(area), float(match.get("area_or_len") or 0), rel_tol=1e-5, abs_tol=1e-4):
            continue
        candidates.append(match)
    return candidates if len(candidates) == 1 else []

def _planning_face_ordinals(metrics: dict, matches: list) -> list:
    """Resolve live query ordinals, not SID-only joins (assembly SIDs can coincide)."""
    faces = metrics.get("faces") or []
    edges = metrics.get("edges") or []
    adjacency = metrics.get("edge_faces") or []
    out = set()
    for match in matches:
        kind = match.get("kind")
        table = edges if kind == "edge" else faces
        ordinal = match.get("hint_index")
        # Filtered doc_query replies can renumber hint_index; accept it only when
        # the SID agrees with the whole-document roster, otherwise join that SID.
        rows = [(i, row) for i, row in enumerate(table)
                if row.get("ordinal") == ordinal and row.get("sid") == match.get("sid")]
        if not rows:
            rows = [(i, row) for i, row in enumerate(table)
                    if row.get("sid") == match.get("sid")]
        if len(rows) != 1:
            continue
        i, row = rows[0]
        if kind == "edge" and i < len(adjacency):
            out.update(int(faces[fi]["ordinal"]) for fi in adjacency[i]
                       if faces[fi].get("ordinal") is not None)
        elif kind != "edge" and row.get("ordinal") is not None:
            out.add(int(row["ordinal"]))
    return sorted(out)



def _planning_roles(doc_id: str, matches: Optional[list] = None) -> dict:
    """Use one tree read for studies/mates and the planner's existing topology/adjacency.

    SID/GUI-selector BCs join locally. Legacy query BCs use the canonical engine resolver
    once per distinct query (the engine has no batch geometry-query RPC). Missing setup or
    stale references must never prevent a geometry render.
    """
    matches = matches or []
    try:
        tree = (cb.call("doc_tree", {"doc_id": doc_id}) or {}).get("tree") or {}
    except Exception:  # noqa: BLE001 — a document without setup is still renderable
        tree = {}
    studies = [s for s in tree.get("studies") or () if isinstance(s, dict)]
    mates = [m for m in tree.get("mates") or ()
             if isinstance(m, dict) and not m.get("suppressed")]
    if not matches and not studies and not mates:
        return {"bc": []}
    topo = _doc_topo_maps(doc_id)
    mesh = _doc_mesh(doc_id)
    metrics = {"faces": [dict(f) for f in (mesh or {}).get("faces") or ()],
               "edges": [dict(e) for e in (mesh or {}).get("edges") or ()]}
    if mesh is not None:
        _sp._join_topology(doc_id, metrics, topo=topo)
        metrics["edge_faces"] = _sp._edge_faces(doc_id, metrics, mesh=mesh)
    else:
        metrics["faces"] = [dict(f, ordinal=i) for i, f in enumerate(topo["face"])]
    roles = _sp._roles(metrics, [], {"target": _planning_face_ordinals(metrics, matches)})
    bc_matches = []
    resolved = {}
    for study in studies:
        if study.get("suppressed"):
            continue
        for ref in _study_face_refs(study):
            try:
                key = json.dumps(ref, sort_keys=True)
                if key not in resolved:
                    sid = ref.get("sid") if isinstance(ref, dict) else ref
                    if isinstance(sid, str) and sid in topo["face_sid"]:
                        resolved[key] = [topo["face"][topo["face_sid"][sid] - 1]]
                    elif isinstance(ref, dict) and ref.get("kind") == "face":
                        # GUI pick selectors can predate SID stamping; join their exact
                        # geometry, never treat a readback selector as an all-face query.
                        resolved[key] = ([] if sid else _mate_face_matches(
                            {"component": "document", "selector": ref},
                            {"document": {}}, topo))
                    elif isinstance(sid, str) and _SID_RE.fullmatch(sid):
                        resolved[key] = []
                    else:
                        result = cb.call("doc_query", {"doc_id": doc_id,
                                                       "query": _as_face_query(ref)}) or {}
                        resolved[key] = [m for m in result.get("matches") or ()
                                         if m.get("kind") == "face"]
                bc_matches.extend(resolved[key])
            except Exception:  # noqa: BLE001 — old/stale studies are best effort
                continue
    components = {c.get("id"): c for c in tree.get("components") or ()
                  if isinstance(c, dict)}
    target_bodies = {f.get("body") for f in metrics["faces"]
                     if f.get("ordinal") in roles["target"]}
    for mate in mates:
        try:
            refs = [_mate_face_matches(r, components, topo, metrics=metrics,
                                       body_components=(mesh or {}).get("body_components"))
                    for r in mate.get("refs") or () if isinstance(r, dict)]
            mate_matches = [m for group in refs for m in group]
            mate_roles = _planning_face_ordinals(metrics, mate_matches)
            if not target_bodies or any(f.get("body") in target_bodies
                                       and f.get("ordinal") in mate_roles
                                       for f in metrics["faces"]):
                bc_matches.extend(mate_matches)
        except Exception:  # noqa: BLE001 — an unresolved mate cannot break rendering
            continue
    roles["bc"] = _planning_face_ordinals(metrics, bc_matches)
    return roles if matches else {"bc": roles["bc"]}



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
                          clip_near: bool = False,
                          background: Optional[list] = None) -> tuple:
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
                annotate=bool(annotate), background=background)
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


# Close-ups are what the verify loop looks at after nearly every mutation, so their size is
# a token budget: 512x384 is ~260 image tokens against ~590 for the old 768x576, and at
# close-up framing the feature still spans hundreds of real pixels.
_CLOSEUP_TOKEN_W, _CLOSEUP_TOKEN_H = 512, 384


# Perspective close-ups fit the target's own surface points; 0.22 per side puts it at ~70 %
# of the frame, leaving the top band for the chip and the bottom for triad + scale bar.
_PERSP_MARGIN = 0.22


def _closeup(d: str, matches: list, target_bb: dict, view_dir, *, label: str,
             stem: str, emphasis_mode: Optional[str] = "emphasis",
             margin: Optional[float] = None, clip_near: bool = False,
             annotate: bool = True, W: int = _CLOSEUP_TOKEN_W, H: int = _CLOSEUP_TOKEN_H,
             highlight=None, background: Optional[list] = None,
             projection: str = "perspective", fov_deg: Optional[float] = None,
             distance: Optional[float] = None, scene_bbox: Optional[dict] = None,
             point: Optional[tuple] = None, defect: Optional[dict] = None,
             section: Optional[dict] = None) -> tuple:
    """ONE idealized close-up: engine-framed render + exact-mask emphasis overlay.

    This is the single close-up implementation every inspect/render tool goes through.
    `projection="perspective"` (default) renders through an exact pinhole camera
    (cad_viewpoint.perspective_camera) from `view_dir`, fitted to the target's own
    tessellation vertices (plus a `defect`'s evidence points) so the feature fills ~70 % of
    the frame. The engine's direction words / tint are off in a close-up (they cluttered the
    target); a world triad, the chip and the scale bar carry orientation and scale.
    `distance` / `fov_deg` override the fit; `clip_near` moves the near plane to the front of
    the target (cutaway). "ortho" keeps the engine's focused orthographic frame. The overlay
    reprojects through the engine's echoed frame; occluded target pixels are ghosted.
    `defect` (an eval_feature_attachment entry) paints its evidence: the unsupported base
    patch hatched orange, or the gap to the part as a cyan dimension.
    `section` ({"point", "normal"}, a planner section view) renders the engine's cut with the
    removed half ghosted; the Python hidden-line edge overlay is skipped there because the
    engine already draws the ghost's edges.
    Returns (png_path, info) — `info["emphasis"]` carries the palette and the MEASURED
    coverage/occlusion, `info["camera"]` the pose used. The palette is always reported, even
    when the tessellation is missing and the mask could not be painted."""
    png = render_output_path(stem, ".png")
    camera = None
    persp = projection == "perspective"
    if margin is None:
        margin = _PERSP_MARGIN if persp else _CLOSEUP_MARGIN
    if persp:
        fit_pts = _sp.target_points(d, matches)
        if fit_pts is not None:
            for key in ("nearest_point", "nearest_point_on_part", "location"):
                if defect and defect.get(key):
                    fit_pts.append(list(defect[key]))
        camera = _vp.perspective_camera(
            target_bb, view_dir, W, H,
            fov_deg=fov_deg if fov_deg is not None else _vp.PERSPECTIVE_FOV_DEG,
            margin=margin, distance=distance, scene_bbox=scene_bbox, clip_near=clip_near,
            points=fit_pts)
    png, frame, _r = _render_view_png(d, view_dir, png, name=label.upper()[:28],
                                      focus=target_bb, margin=margin, clip_near=clip_near,
                                      W=W, H=H, annotate=bool(annotate) and not persp,
                                      background=background, camera=camera, section=section)
    palette = (_build_emphasis_palette(matches or [], mode=emphasis_mode)
               if emphasis_mode and matches else None)
    emphasis = ({"matches": list(matches or []), "mode": emphasis_mode, "label": label,
                 "palette": palette, "draw_label": not persp}
                if palette is not None else None)
    png, info = _annotate_render(png, d, view_dir, highlight=highlight,
                                 edges=not section, callouts=False, scale=True,
                                 frame=frame, emphasis=emphasis,
                                 out_png=render_output_path(stem, ".png"))
    if palette is not None and not (info.get("emphasis") or {}).get("palette"):
        info["emphasis"] = dict(info.get("emphasis") or {},
                                palette=palette, mode=palette.get("mode"), label=label,
                                target_px=0, coverage=0.0,
                                note=(info.get("note")
                                      or "no tessellation — emphasis mask not painted"))
    info["camera"] = ({k: camera[k] for k in ("position", "target", "vertical_fov_deg",
                                              "distance", "fit_distance")}
                      if camera else {"projection": "ortho"})
    _draw_closeup_chip(png, frame, label, target_bb, camera, point,
                       show_text=bool(annotate), defect=defect)
    return png, info


def _attachment_defect(d: str, resolved: dict) -> Optional[dict]:
    """The eval_feature_attachment finding for a FEATURE target, if it has one."""
    fid = resolved.get("feature_id")
    if not fid:
        return None
    try:
        r = cb.call("eval_feature_attachment",
                    {"doc_id": d, "features": [fid], "budget_s": 3.0}) or {}
    except Exception:  # noqa: BLE001
        return None
    return next(iter(r.get("features") or []), None)


def _defect_view_dir(defect: dict, part_bb: dict, d: Optional[str] = None) -> tuple:
    """The camera that SHOWS an attachment defect.

    overhang: from outside and BELOW, across the unsupported edge, so the bare underside is
    in view against the support's rim. floating: near-level, perpendicular to the gap vector
    and slightly below its middle, on the bearing whose sight line through the gap crosses the
    least other geometry (a clear gap, not a rib behind it). barely_touching: outside, above."""
    c = part_bb.get("center") or (0.0, 0.0, 0.0)
    loc = defect.get("location") or c
    h = (float(loc[0]) - float(c[0]), float(loc[1]) - float(c[1]), 0.0)
    if math.hypot(h[0], h[1]) < 1e-6:
        h = (0.0, -1.0, 0.0)
    h = _vnorm(h)
    verdict = defect.get("verdict")
    a, b = defect.get("nearest_point"), defect.get("nearest_point_on_part")
    if verdict == "floating" and a and b:
        g = _vnorm(tuple(float(x) - float(y) for x, y in zip(a, b)))
        h = _clearest_bearing(d, h, g, a, b)
        el = -8.0
    elif verdict == "overhang":
        el = -28.0
    else:
        el = 15.0
    ce, se = math.cos(math.radians(el)), math.sin(math.radians(el))
    return _vnorm((h[0] * ce, h[1] * ce, h[2] * ce + se))


def _clearest_bearing(d: Optional[str], h: tuple, g: tuple, a, b) -> tuple:
    """Of 12 bearings perpendicular to the gap `g`, the one whose sight line through the gap
    slab (strictly between the two nearest points) meets the fewest mesh vertices; ties go to
    the one facing out of the part along `h`."""
    try:
        import numpy as np  # noqa: PLC0415
        mesh = _doc_mesh(d) if d else None
    except Exception:  # noqa: BLE001
        mesh = None
    ref = (0.0, 0.0, 1.0) if abs(g[2]) < 0.9 else (1.0, 0.0, 0.0)
    p0 = _vnorm((ref[1] * g[2] - ref[2] * g[1], ref[2] * g[0] - ref[0] * g[2],
                 ref[0] * g[1] - ref[1] * g[0]))
    p1 = (g[1] * p0[2] - g[2] * p0[1], g[2] * p0[0] - g[0] * p0[2],
          g[0] * p0[1] - g[1] * p0[0])
    best, best_key = h, None
    for k in range(12):
        t = math.radians(30 * k)
        cand = tuple(math.cos(t) * p0[i] + math.sin(t) * p1[i] for i in range(3))
        hits = 0
        if mesh is not None:
            gv = np.asarray(g, dtype=float)
            mid = (np.asarray(a, float) + np.asarray(b, float)) * 0.5
            half = float(np.linalg.norm(np.asarray(a, float) - np.asarray(b, float))) * 0.5
            rel = mesh["pos"] - mid
            along = (rel @ gv)[mesh["idx"]]                      # per-triangle, 3 corners
            lat = (rel @ np.cross(np.asarray(cand), gv))[mesh["idx"]]
            # Triangles that cross the gap slab AND the vertical plane of this sight line
            # (vertices alone miss a wall whose corners sit exactly on the slab faces).
            hits = int(np.count_nonzero(
                (along.min(axis=1) < half * 0.9) & (along.max(axis=1) > -half * 0.9)
                & (lat.min(axis=1) < 3.0 * half) & (lat.max(axis=1) > -3.0 * half)))
        key = (hits, -sum(cand[i] * h[i] for i in range(3)))
        if best_key is None or key < best_key:
            best, best_key = cand, key
    return _vnorm(best)


def _auto_view(d: str, resolved: dict, matches: list, target_bb: dict, part_bb: dict,
               point: Optional[tuple]) -> tuple:
    """(view_dir, label, defect, section) for an explicit view="auto" single shot: a
    defect-showing camera when the target feature fails eval_feature_attachment, else the
    planner's single best view of the target (shot_planner.plan_shots, k=1) — which is a
    section through the target when no exterior camera shows it well."""
    defect = _attachment_defect(d, resolved)
    if defect:
        return (_defect_view_dir(defect, part_bb, d), f"shows-{defect.get('verdict')}",
                defect, None)
    if matches:
        best = _sp.plan_shots(d, target={"matches": matches, "bbox": target_bb,
                                         "label": resolved.get("label") or "target",
                                         "kind": resolved.get("kind")},
                              roles=_planning_roles(d, matches), k=1)["views"][0]
        return tuple(best["dir"]), best.get("label") or "auto", None, best.get("section")
    if point is not None:
        return _point_view_dir(point, part_bb), "outside-in", None, None
    return _vnorm((1.0, 1.0, 0.8)), "iso", None, None


def _defect_lines(defect: Optional[dict]) -> list:
    if not defect:
        return []
    v = defect.get("verdict")
    if v == "overhang":
        return [(f"OVERHANG {100 * (defect.get('overhang_frac') or 0):.0f}% of base unsupported, "
                 f"{defect.get('overhang_mm') or 0:.2g} mm past support (hatched)",
                 (255, 150, 40))]
    if v == "floating":
        return [(f"FLOATING: {defect.get('gap_mm') or 0:.3g} mm gap to the part (cyan)",
                 (60, 220, 255))]
    return [("TOUCHES ONLY AT AN EDGE/POINT (cyan marker)", (60, 220, 255))]


def _draw_defect(im, draw, frame: dict, defect: dict) -> None:
    """Paint an attachment defect's evidence through the render's own camera."""
    import numpy as np  # noqa: PLC0415
    from PIL import Image as PILImage, ImageDraw  # noqa: PLC0415
    W, H = im.size
    pts = defect.get("unsupported_points") or []
    if defect.get("verdict") == "overhang" and pts:
        S = _screen_xyz(pts, frame)
        ok = np.isfinite(S[:, 0]) & np.isfinite(S[:, 1])
        pitch = float(defect.get("sample_pitch_mm") or 1.0)
        if frame.get("projection") == "perspective":
            px = pitch * float(frame["focal"]) * np.where(ok, S[:, 2], 0.0)
        else:
            px = np.full(len(S), pitch * float(frame["scale"]))
        mask = PILImage.new("L", (W, H), 0)
        md = ImageDraw.Draw(mask)
        for (x, y, _z), r, good in zip(S, px, ok):
            if good:
                rr = max(2.0, 0.8 * float(r))
                md.ellipse([x - rr, y - rr, x + rr, y + rr], fill=255)
        m = np.asarray(mask) > 0
        arr = np.asarray(im, dtype=np.float64)
        yy, xx = np.mgrid[0:H, 0:W]
        hatch = m & (((xx + yy) % 8) < 3)
        arr[m] = arr[m] * 0.35 + np.asarray((255, 150, 40)) * 0.65
        arr[hatch] = (120, 50, 0)
        im.paste(PILImage.fromarray(arr.clip(0, 255).astype(np.uint8), "RGB"))
        edge = m & ~(np.roll(m, 1, 0) & np.roll(m, -1, 0) & np.roll(m, 1, 1) & np.roll(m, -1, 1))
        ys, xs = np.nonzero(edge)
        for x, y in zip(xs.tolist(), ys.tolist()):
            draw.point((x, y), fill=(255, 235, 200))
        return
    a, b = defect.get("nearest_point"), defect.get("nearest_point_on_part")
    cyan = (60, 220, 255)
    if a and b:
        S = _screen_xyz([a, b], frame)
        if np.all(np.isfinite(S[:, :2])):
            (x0, y0), (x1, y1) = S[0, :2], S[1, :2]
            dx, dy = x1 - x0, y1 - y0
            n = math.hypot(dx, dy) or 1.0
            ux, uy, wx, wy = dx / n, dy / n, -dy / n * 14, dx / n * 14
            band = [(x0 + wx, y0 + wy), (x1 + wx, y1 + wy), (x1 - wx, y1 - wy),
                    (x0 - wx, y0 - wy)]
            draw.polygon(band, outline=cyan)
            for t in range(0, int(n) + 1, 6):
                cx, cy = x0 + ux * t, y0 + uy * t
                draw.line([(cx - wx, cy - wy), (cx + wx, cy + wy)], fill=(20, 110, 140))
            draw.line([(x0, y0), (x1, y1)], fill=cyan, width=3)
            for (px_, py_), sgn in (((x0, y0), 1), ((x1, y1), -1)):
                draw.polygon([(px_, py_), (px_ + sgn * (ux * 9 - uy * 5), py_ + sgn * (uy * 9 + ux * 5)),
                              (px_ + sgn * (ux * 9 + uy * 5), py_ + sgn * (uy * 9 - ux * 5))],
                             fill=cyan)
            return
    loc = defect.get("location")
    if loc:
        S = _screen_xyz([loc], frame)[0]
        if math.isfinite(float(S[0])) and math.isfinite(float(S[1])):
            x, y = float(S[0]), float(S[1])
            draw.ellipse([x - 10, y - 10, x + 10, y + 10], outline=cyan, width=3)


def _draw_closeup_chip(png: str, frame: Optional[dict], label: str, target_bb: dict,
                       camera: Optional[dict], point: Optional[tuple], *,
                       show_text: bool = True, defect: Optional[dict] = None) -> None:
    """Burn the key numbers into the close-up itself, in a top band clear of the target:
    WHAT is framed, its world extents and Z range (the number that says "this rib starts
    4 mm above the plate"), the camera, and any attachment defect in words; the defect's
    evidence painted on the geometry; a world triad; a crosshair for a point target."""
    try:
        from PIL import Image as PILImage, ImageDraw  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return
    if not show_text and point is None and not defect:
        return
    size = target_bb.get("size") or (0, 0, 0)
    lo, hi = target_bb.get("min") or (0, 0, 0), target_bb.get("max") or (0, 0, 0)
    cam = (f"persp {camera['vertical_fov_deg']:.0f}deg @ {camera['distance']:.3g} mm"
           if camera else "ortho")
    lines = [(label[:48], (240, 242, 248)),
             (f"extent {size[0]:.3g} x {size[1]:.3g} x {size[2]:.3g} mm  "
              f"z {lo[2]:.3g}..{hi[2]:.3g}  {cam}", (200, 206, 218))] + _defect_lines(defect)
    with PILImage.open(png) as im0:
        im = im0.convert("RGB")
    draw = ImageDraw.Draw(im)
    if defect and frame is not None:
        try:
            _draw_defect(im, draw, frame, defect)
        except Exception:  # noqa: BLE001 — evidence paint must never cost the render
            pass
        draw = ImageDraw.Draw(im)
    font = _annot_font(12)
    y = 4
    for line, rgb in (lines if show_text else ()):
        try:
            bb = draw.textbbox((0, 0), line, font=font)
            tw, th = bb[2] - bb[0], bb[3] - bb[1]
        except Exception:  # noqa: BLE001
            tw, th = 7 * len(line), 12
        draw.rectangle([4, y - 2, 10 + tw, y + th + 3], fill=(24, 26, 32))
        draw.text((7, y), line, font=font, fill=rgb)
        y += th + 6
    if show_text and camera and frame is not None and frame.get("r"):
        _sp.draw_triad(draw, frame, im.size[1])
    if point is not None and frame is not None:
        _draw_crosshair(draw, frame, point)
    im.save(png, format="PNG")


def _draw_crosshair(draw, frame: dict, point: tuple) -> None:
    """Ringed crosshair on a world-point target, through the render's own camera."""
    s = _screen_xyz([point], frame)[0]
    if math.isfinite(float(s[0])) and math.isfinite(float(s[1])):
        x, yy = int(round(float(s[0]))), int(round(float(s[1])))
        rgb = _HIGHLIGHT_PALETTE[0][0]
        draw.ellipse([x - 8, yy - 8, x + 8, yy + 8], outline=rgb, width=2)
        draw.line([(x - 13, yy), (x + 13, yy)], fill=rgb, width=2)
        draw.line([(x, yy - 13), (x, yy + 13)], fill=rgb, width=2)



def _split_view(view) -> tuple:
    """Split a render `view` into (direction spec, camera options).

    Direction: "auto"/None, a named view, {"azimuth","elevation"} or {"dir"}. Camera options
    ride in the same dict so no new tool argument is needed: "distance" (mm from the target
    centre), "fov" (vertical degrees) and "projection" ("perspective" | "ortho")."""
    opts = {"projection": None, "distance": None, "fov": None}
    if not isinstance(view, dict):
        return (view if view is not None else "auto"), opts
    rest = dict(view)
    for key in ("projection", "distance", "fov"):
        if key in rest:
            opts[key] = rest.pop(key)
    if "fov_deg" in rest:
        opts["fov"] = rest.pop("fov_deg")
    if opts["projection"] is not None:
        proj = str(opts["projection"]).strip().lower()
        if proj in ("orthographic", "ortho"):
            opts["projection"] = "ortho"
        elif proj == "perspective":
            opts["projection"] = "perspective"
        else:
            raise ValueError(f"view.projection must be 'perspective' or 'ortho', got {proj!r}")
    for key in ("distance", "fov"):
        if opts[key] is not None:
            try:
                opts[key] = float(opts[key])
            except (TypeError, ValueError):
                raise ValueError(f"view.{key} must be a number (got {opts[key]!r})") from None
            if not math.isfinite(opts[key]) or opts[key] <= 0:
                raise ValueError(f"view.{key} must be a positive number")
    direction = rest if any(k in rest for k in ("dir", "azimuth", "elevation")) else "auto"
    return direction, opts


def _union_bbox(boxes: list) -> dict:
    """Axis-aligned union of target boxes ({"min","max"} dicts)."""
    lo = tuple(min(float(b["min"][k]) for b in boxes) for k in range(3))
    hi = tuple(max(float(b["max"][k]) for b in boxes) for k in range(3))
    return {"min": lo, "max": hi, "size": tuple(hi[k] - lo[k] for k in range(3)),
            "center": tuple((hi[k] + lo[k]) * 0.5 for k in range(3))}


def _point_of(target) -> Optional[tuple]:
    """A world-point target: [x, y, z] or {"point"|"xyz"|"at": [x, y, z], "radius"?}."""
    raw = target
    if isinstance(target, dict):
        raw = next((target[k] for k in ("point", "xyz", "at") if k in target), None)
    if isinstance(raw, (list, tuple)) and len(raw) == 3:
        try:
            p = tuple(float(v) for v in raw)
        except (TypeError, ValueError):
            return None
        return p if all(math.isfinite(v) for v in p) else None
    return None


def _resolve_camera_target(d: str, target, part_bb: Optional[dict]) -> tuple:
    """(resolved, target_bb, point) for any close-up target: a feature / face / edge / sid
    through _resolve_inspect_target, or a bare world point framed as a cube of `radius`
    (default 8 % of the part diagonal, at least 2 mm)."""
    p = _point_of(target)
    if p is None:
        resolved = _resolve_inspect_target(d, target)
        return resolved, _target_bbox(d, resolved["matches"]), None
    radius = target.get("radius") if isinstance(target, dict) else None
    if radius is None:
        diag = math.sqrt(sum(float(s) ** 2 for s in (part_bb or {}).get("size") or (20, 20, 20)))
        radius = max(2.0, 0.08 * diag)
    r = float(radius)
    bb = {"min": tuple(v - r for v in p), "max": tuple(v + r for v in p),
          "size": (2 * r,) * 3, "center": p}
    label = f"point [{p[0]:.4g}, {p[1]:.4g}, {p[2]:.4g}]"
    return {"matches": [], "label": label, "kind": "point"}, bb, p


def _render_target(d: str, spec, part_bb: dict) -> tuple:
    """(resolved, target_bb, point) for render_part's target/emphasize argument. A list of
    feature/face specs is a union target (AABB union, kind "emphasis"); a 3-number list is a
    point. A single spec keeps its resolver record (feature_id feeds the defect lookup)."""
    specs = spec if isinstance(spec, list) and _point_of(spec) is None else [spec]
    parts = [_resolve_camera_target(d, sp, part_bb) for sp in specs]
    if len(parts) == 1:
        return dict(parts[0][0]), parts[0][1], parts[0][2]
    matches = [m for rs, _bb, _p in parts for m in (rs.get("matches") or [])]
    resolved = {"matches": matches,
                "label": ", ".join(str(rs.get("label") or sp)
                                   for (rs, _bb, _p), sp in zip(parts, specs)),
                "kind": "emphasis"}
    return resolved, _union_bbox([bb for _rs, bb, _p in parts]), None


def _point_view_dir(point: tuple, part_bb: Optional[dict]) -> tuple:
    """Default camera for a point: from outside the part, looking back at the point from
    above — the direction from the part centre through the point, lifted toward +Z."""
    c = (part_bb or {}).get("center") or (0.0, 0.0, 0.0)
    out = (point[0] - c[0], point[1] - c[1], point[2] - c[2])
    n = math.sqrt(sum(v * v for v in out))
    if n < 1e-9:
        return _vnorm((1.0, 1.0, 0.8))
    return _vnorm((out[0] / n, out[1] / n, out[2] / n + 0.8))


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
    occ = emp.get("occluded_frac")
    if occ and occ >= 0.02:
        bits.append(f"{occ * 100:.0f}% of the target's silhouette is behind other material "
                    "(drawn ghosted through it)")
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


# --------------------------------------------------------------------------- save_path
# O-16: a render the model can SEE is not yet a figure it can PUT IN a document. The
# tools keep returning the image inline (that is what the vision loop reads) and, when
# `save_path` is given, ALSO write those exact bytes to the caller's own file so a report
# can reference the PNG by path. The write is byte-identical to the inline image — the
# caption carries the length and a sha256 prefix so a reader can prove it.
_RENDER_SAVE_ANYWHERE = "CHUDWARE_RENDER_SAVE_ANYWHERE"
# The workspace/project roots a figure may be written into, in the order project_context
# resolves them for every plm_* tool. A render is a deliverable of the CURRENT project;
# writing one into an arbitrary directory is how an agent scatters files a user never
# finds (and is a path-traversal surface when the doc_id came from a remote client).
_RENDER_SAVE_ROOT_VARS = ("CHUDWARE_PROJECT_SAVE_DIR", "CHUDWARE_PROJECT_DIR",
                          "CHUDWARE_FS_ROOT")


def _render_save_roots() -> list:
    """[(env var, real root)] for every configured workspace/project directory."""
    out: list = []
    for var in _RENDER_SAVE_ROOT_VARS:
        raw = (os.environ.get(var) or "").strip()
        if raw:
            out.append((var, os.path.realpath(os.path.expanduser(raw))))
    return out


def _render_save_target(save_path, *, directory: bool = False,
                        film: bool = False) -> Optional[str]:
    """Validate a file or frame directory before rendering and resolve its destination.

    A relative path is project-root relative (`resolve_agent_path`, the same rule as
    save_part and asm_add_component); with no project root it is refused. A supplied path
    needs write permission under the active tool policy. The destination and configured
    roots are resolved through symlinks before containment is checked. PNG files gain a
    .png suffix when omitted; films and directories retain theirs.
    """
    if save_path is None:
        return None
    if not isinstance(save_path, str) or not save_path.strip():
        raise ValueError("save_path must be a non-empty path, absolute or relative to the "
                         "project root, e.g. 'figures/fixture-setup.png'")
    if is_risk_denied(RISK_WRITE):
        raise ValueError("save_path is denied by the active write-risk policy")
    expanded = resolve_agent_path(save_path, "save_path")
    if not directory and not film and not expanded.lower().endswith(".png"):
        expanded += ".png"
    out = _sanitize_output_path(expanded)
    if (os.environ.get(_RENDER_SAVE_ANYWHERE) or "").strip().lower() in (
            "1", "true", "yes", "on"):
        return out
    roots = _render_save_roots()
    if not roots:
        raise ValueError(
            "save_path needs a project/workspace directory to save INTO, and none is "
            f"configured ({' / '.join(_RENDER_SAVE_ROOT_VARS)} are all unset). Set one for "
            f"this session, or set {_RENDER_SAVE_ANYWHERE}=1 to allow any absolute path.")
    for _var, root in roots:
        try:
            if os.path.commonpath((out, root)) == root:
                return out
        except ValueError:  # different drives on Windows
            continue
    raise ValueError(
        f"save_path {out!r} is outside this session's project/workspace directory "
        f"({', '.join(r for _v, r in roots)}). Save the figure under the project root so it "
        f"ships with the deliverable, or set {_RENDER_SAVE_ANYWHERE}=1 to override.")


def _render_save_note(png: str, target: Optional[str]) -> str:
    """Copy the render at `png` to `target` and return the caption clause proving it.

    Byte-for-byte: the saved file IS the inline image, so a figure embedded from the path
    and the picture the model looked at can never diverge. Parent directories are created.
    Returns "" when there is nothing to save."""
    if not target:
        return ""
    parent = os.path.dirname(target)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(png, "rb") as fh:
        data = fh.read()
    with open(target, "wb") as fh:
        fh.write(data)
    return (f"saved: {target} ({len(data)} B, "
            f"sha256 {hashlib.sha256(data).hexdigest()[:12]})")


def _with_save_note(result, note: str):
    """Append the save clause to a [image, caption] pair (or pair a bare Image with it)."""
    if not note:
        return result
    if isinstance(result, list):
        if len(result) >= 2:
            cap = str(result[1] or "").strip()
            return [result[0], (cap + " " + note).strip() if cap else note]
        if len(result) == 1:
            return [result[0], note]
        return [note]
    return [result, note]


# ------------------------------------------------------------------- planner shot grids
def _tile_decorator(point: Optional[tuple]) -> Callable:
    """Per-tile evidence for render_shot_grid: an attachment defect painted on the tile aimed
    at it (the pinned view carries "defect"), and a crosshair on a world-point target."""
    def _decorate(png: str, frame: Optional[dict], view: dict) -> None:
        defect = view.get("defect")
        if frame is None or (not defect and point is None):
            return
        from PIL import Image as PILImage, ImageDraw  # noqa: PLC0415
        with PILImage.open(png) as im0:
            im = im0.convert("RGB")
        draw = ImageDraw.Draw(im)
        if defect:
            try:
                _draw_defect(im, draw, frame, defect)
            except Exception:  # noqa: BLE001 — evidence paint must never cost the render
                pass
            draw = ImageDraw.Draw(im)
        if point is not None:
            _draw_crosshair(draw, frame, point)
        im.save(png, format="PNG")
    return _decorate


def _shot_grid(d: str, plan: dict, *, stem: str, highlight=None,
               emphasis_mode: Optional[str] = "emphasis", annotate: bool = True,
               background: Optional[list] = None, point: Optional[tuple] = None) -> tuple:
    """(png, caption) for a plan: the lettered grid and its self-sufficient caption, plus the
    emphasis palette block when a target is painted."""
    png, info = _sp.render_shot_grid(d, plan, highlight=highlight, emphasis_mode=emphasis_mode,
                                     annotate=annotate, background=background,
                                     decorate=_tile_decorator(point), stem=stem)
    return png, info["caption"] + _palette_caption_block(info.get("palette"))


# ------------------------------------------------------------------- study-setup figure
# The FIXTURE/LOAD figure an evidence document needs. Every face selection goes through
# cad_sim_tools' own resolver stack — _norm_bc_list, _sim_face_points, _build_load_specs,
# _reject_fully_constrained_loads — i.e. exactly the calls run_fea_static makes, so this
# figure is a picture of the study that WOULD solve rather than a second reading of the
# same words, and a setup the solver refuses is refused here too. The painted faces are
# additionally PROVEN to carry the solver's own node sets (_bc_group), because a figure
# showing the wrong face is worse than no figure: it is false evidence.
_FIXTURE_RGB, _FIXTURE_COLOUR = _HIGHLIGHT_PALETTE[0]   # magenta
_LOAD_RGB, _LOAD_COLOUR = _HIGHLIGHT_PALETTE[1]         # violet


def _bc_group(d: str, query, solver_sets: list, label: str) -> dict:
    """Pair ONE boundary-condition query's SOLVER node sets with the faces to paint.

    `solver_sets` are the doc_face_points sets cad_sim_tools already resolved for this
    query — the authority on which nodes the solve drives. The paint and the legend need
    sids and areas, which only the query MATCHES carry, so the two are cross-checked here:
    every sampled solver node must lie on a face this figure will colour. A mismatch means
    the picture and the study disagree, and we refuse instead of shipping it.
    Returns {query, matches, sids, n_points}."""
    solver_pts = {tuple(round(float(c), 6) for c in p[:3])
                  for s in solver_sets for p in (s.get("points") or [])
                  if isinstance(p, (list, tuple)) and len(p) >= 3}
    rsl = _resolve_inspect_target(d, query)
    matches = [m for m in (rsl.get("matches") or []) if m.get("kind") != "edge"]
    if not matches:
        raise ValueError(
            f"{label} query {query!r} resolved to solver nodes but to no renderable FACE "
            "— a study-setup figure can only colour faces. Name the face itself (a sid from "
            "describe_part, or a query_geometry face query).")
    painted_pts = {tuple(round(float(c), 6) for c in p[:3])
                   for m in matches for p in _match_point_cloud(d, m)}
    if not solver_pts <= painted_pts:
        raise ValueError(
            f"{label} query {query!r} does not colour what it solves: "
            f"{len(solver_pts - painted_pts)} of {len(solver_pts)} solver sample points are "
            f"not on the {len(matches)} face(s) this figure would colour "
            f"({', '.join(str(m.get('sid')) for m in matches)}). Pass the face SID from "
            "describe_part() so the figure and the study cannot disagree.")
    return {
        "query": query,
        "matches": matches,
        "sids": [str(m.get("sid")) for m in matches if m.get("sid")],
        "n_points": len(solver_pts),
    }


def _sids_areas_text(groups: list) -> str:
    """``Ftop 1200 mm2, Fbot 800 mm2`` across every group of one BC role."""
    bits: list = []
    for g in groups:
        for m in g["matches"]:
            sid = str(m.get("sid") or "unnamed face")
            try:
                area = float(m.get("area_or_len"))
            except (TypeError, ValueError):
                bits.append(sid)
                continue
            bits.append(f"{sid} {area:.4g} mm2")
    return ", ".join(bits) if bits else "no sid"


def _entry_anchor(group: dict) -> Optional[list]:
    """Area-weighted centroid of ONE load entry's faces — where its arrow lands.

    Weighted, not plain-mean: an entry naming a big barrel and a sliver chamfer must anchor
    on the barrel. Falls back to a plain mean when no face reports an area, and to None when
    no match carries a centroid at all (nothing to point at)."""
    num = [0.0, 0.0, 0.0]
    wsum = 0.0
    for m in group["matches"]:
        c = m.get("centroid")
        if not isinstance(c, (list, tuple)) or len(c) < 3:
            continue
        try:
            w = float(m.get("area_or_len"))
        except (TypeError, ValueError):
            w = 0.0
        w = w if w > 0.0 else 1.0
        wsum += w
        for i in range(3):
            num[i] += w * float(c[i])
    return [c / wsum for c in num] if wsum > 0.0 else None


def _load_resultant(load_specs: list, groups: list) -> tuple:
    """(resultant [Fx,Fy,Fz] N, per-load description, per-entry records) for the solver's own
    load payload.

    A force load contributes its vector as given. A pressure load contributes
    p[MPa] * area[mm2] = N along the INWARD face normal — what {"face": q, "pressure": p}
    means to the solver — computed from the face's own area and outward normal so the
    figure states a real number instead of "some pressure".

    The per-entry records ({vector, mag, anchor, sids}) are what the figure DRAWS: the
    resultant alone is not the load — four tangential tractions summing to zero are a couple,
    and a figure showing nothing for them is false evidence."""
    total = [0.0, 0.0, 0.0]
    terms: list = []
    entries: list = []

    def _entry(vec: list, g: dict) -> None:
        entries.append({"vector": [float(c) for c in vec],
                        "mag": math.sqrt(sum(float(c) * float(c) for c in vec)),
                        "anchor": _entry_anchor(g), "sids": list(g["sids"])})

    for spec, g in zip(load_specs, groups):
        if spec.get("type") == "force":
            f = [float(c) for c in spec["vector"]]
            for i in range(3):
                total[i] += f[i]
            terms.append(f"force [{f[0]:.4g}, {f[1]:.4g}, {f[2]:.4g}] N on "
                         f"{', '.join(g['sids']) or 'the resolved face(s)'}")
            _entry(f, g)
            continue
        p = float(spec.get("magnitude") or 0.0)
        area = 0.0
        vec = [0.0, 0.0, 0.0]
        for m in g["matches"]:
            try:
                a = float(m.get("area_or_len"))
            except (TypeError, ValueError):
                continue
            n = m.get("normal") or m.get("axis")
            if not isinstance(n, (list, tuple)) or len(n) < 3:
                continue
            u = _vnorm(n)
            area += a
            for i in range(3):
                vec[i] -= p * a * u[i]  # pressure pushes INWARD, along -outward normal
        for i in range(3):
            total[i] += vec[i]
        terms.append(f"pressure {p:.4g} MPa over {area:.4g} mm2 = "
                     f"{math.sqrt(sum(c * c for c in vec)):.4g} N inward along the face "
                     f"normal of {', '.join(g['sids']) or 'the resolved face(s)'}")
        _entry(vec, g)
    return total, terms, entries


def register_cad_render_tools(mcp: FastMCP) -> None:
    """CAD render + close-up inspection MCP tools (split out of cad_tools.py): render_part, contact_sheet, render_views, inspect_part, look_around, inspect_feature."""


    @mcp.tool()
    def render_part(doc_id: Optional[str] = None,
                    view: Optional[Union[str, dict]] = None,
                    target: Optional[Union[str, dict, list]] = None,
                    emphasize: Optional[Union[str, dict, list]] = None,
                    emphasis_mode: str = "emphasis",
                    highlight: Optional[Union[str, dict, list]] = None,
                    annotate: bool = True,
                    display_quality: Optional[str] = None,
                    background: Optional[Union[list, str]] = None,
                    save_path: Optional[str] = None) -> list:
        """Render the part and return [image, caption] so you can SEE it.

        DEFAULT (no `view`): ONE image holding the FOUR most useful complementary shots, a
        2x2 grid of 512x384 perspective tiles lettered A-D. The shot planner measures ~200
        candidate cameras in the engine (exact visible area, edge contrast, occlusion) and
        picks the SET that together shows the most — so the back, the underside or a cut
        through an internal feature is in the grid when that is where the information is.
        Each tile carries SID tags on its most important visible faces (the same sids
        query_geometry / add_features refs accept) and a world X/Y/Z triad. The caption
        lists per tile the camera direction, whether it is a SECTION (cut face hatched,
        removed half ghosted), how much of the target it shows and which faces/features read
        best there; the union coverage; every face NO tile sees, by sid; the tag legend
        ("F58db6d — plane, +Z, 40x20 mm, f3 Boss"); and the ranking model used.
        Persisted simulation fixture/load faces and assembly-mate contact faces contribute
        boundary-condition coverage to the ranking; a target also includes its adjacent
        faces as surrounding context. Documents without studies or mates need no setup.

        target: frame the grid on ONE thing — a feature id or name ("f5", "Rib"), a face/edge
        sid or geometry query ({"of":"face","side":"top"}), a list of those, or a world point
        [x, y, z] / {"point": [x, y, z], "radius": mm}. The target is painted (shading kept),
        the rest dimmed, occluded parts of it ghosted; an attachment defect found by
        eval_feature_attachment gets tile A aimed at it with its evidence painted.
        emphasize is an alias; emphasis_mode "per_face" colours each face distinctly.

        view: ONE single shot instead of the grid — "auto" (the planner's best single view),
        "front"/"back"/"top"/"bottom"/"left"/"right"/"iso", or a dict: {"azimuth": deg,
        "elevation": deg} (az 0 = front, 90 = right) or {"dir": [x, y, z]} (scene -> camera),
        plus optional "distance" (mm, camera to target), "fov" (deg, default 30) and
        "projection" ("perspective" default for close-ups, "ortho" for a drawing-like framed
        view; {"projection": "perspective"} alone gives a perspective shot of the whole part).
        highlight: paint + label targets WITHOUT reframing (same vocabulary, list allowed).
        annotate=False: clean product shots (no edges, tags or scale bar). display_quality
        "draft"|"standard"|"fine" re-tessellates the display mesh (persists on the doc).
        background: [r,g,b] or "#rrggbb". save_path: also write the exact PNG to this path
        inside the project. A relative path is project-root relative. The caption gives the
        absolute saved path.

        Result: you see the image. Eval code (write xd://render_part) gets one mapping:
        {"ok": true, "caption": str, "images": [{"path", "mime", "width", "height"}],
        "saved_path": str (only with save_path)}. All image tools give this shape.

        Check numbers first (verify_battery, measure_*); render when a number is off or a
        shape needs judging — one call already gives four angles, so do not render after
        every mutation."""
        # Validate the destination BEFORE any engine work: a refused path must not cost a
        # render (and must be reported as the argument error it is, not as a render failure).
        save_to = _render_save_target(save_path)
        bg = render_background(background)
        d = _doc(doc_id)
        if display_quality is not None:
            # Persists on the document (view setting, no revision bump) — nothing to restore.
            # Let a bad tier name surface: the engine refuses instead of guessing.
            cb.call("doc_set_display_quality",
                    {"doc_id": d, "quality": str(display_quality).strip().lower()})
        view_spec, cam_opts = _split_view(view)
        view_name = view if isinstance(view, str) else "custom"
        emp_spec = emphasize if emphasize is not None else target

        if view is None:
            part_bb = cb.call("doc_bbox", {"doc_id": d}) or {}
            if not part_bb.get("size"):
                # A drawing document has no solid to plan cameras around; its sheet is the
                # picture.
                png = render_output_path(_render_stem(d, "drawing"), ".png")
                try:
                    rd = cb.call("drawing_render_png", {"doc_id": d, "out_path": png})
                except Exception:  # noqa: BLE001
                    rd = {"ok": False}
                if rd.get("ok") and os.path.exists(png):
                    return _with_save_note(_captioned_render(png, d, ["drawing"]),
                                           _render_save_note(png, save_to))
                raise ValueError("Render failed: no bodies to render")
            if emp_spec is None:
                plan = _sp.plan_shots(d, k=4, part_bbox=part_bb, roles=_planning_roles(d))
                png, caption = _shot_grid(d, plan, stem=_render_stem(d, "shots"),
                                          highlight=highlight, emphasis_mode=None,
                                          annotate=bool(annotate), background=bg)
                return _with_save_note([Image(path=png), caption],
                                       _render_save_note(png, save_to))
            _sp.prime_face_clouds(d)
            resolved, target_bb, point = _render_target(d, emp_spec, part_bb)
            label = str(resolved.get("label") or "target")
            defect = _attachment_defect(d, resolved)
            pinned = None
            if defect:
                pinned = [{"dir": list(_defect_view_dir(defect, part_bb, d)),
                           "label": f"shows-{defect.get('verdict')}", "defect": defect,
                           "fit_points": [defect[k] for k in ("nearest_point",
                                                              "nearest_point_on_part",
                                                              "location") if defect.get(k)]}]
            plan = _sp.plan_shots(d, target={"matches": resolved["matches"], "label": label,
                                             "bbox": target_bb, "kind": resolved.get("kind")},
                                  k=4, pinned=pinned, part_bbox=part_bb,
                                  roles=_planning_roles(d, resolved["matches"]))
            png, caption = _shot_grid(d, plan, stem=_render_stem(d, f"shots-{label}"),
                                      highlight=highlight,
                                      emphasis_mode=emphasis_mode or "emphasis",
                                      annotate=bool(annotate), background=bg, point=point)
            if defect:
                caption += (f" ATTACHMENT DEFECT ({defect.get('verdict')}): "
                            f"{defect.get('detail')} — tile A is aimed to show it, with the "
                            f"evidence painted.")
            return _with_save_note([Image(path=png), caption], _render_save_note(png, save_to))

        whole_persp = emp_spec is None and isinstance(view, dict) and (
            cam_opts["projection"] == "perspective" or cam_opts["distance"] is not None)
        if emp_spec is not None or whole_persp:
            part_bb = cb.call("doc_bbox", {"doc_id": d}) or {}
            if not part_bb.get("size"):
                raise ValueError("render_part: the part has no geometry to frame yet")
            point = None
            if whole_persp:
                resolved = {"matches": [], "label": "whole part", "kind": "part"}
                target_bb = part_bb
            else:
                resolved, target_bb, point = _render_target(d, emp_spec, part_bb)
            matches = resolved["matches"]
            internal = bool(matches) and _target_is_internal(matches, part_bb)
            section = None
            if isinstance(view_spec, str) and view_spec.lower() == "auto":
                view_dir, view_label, defect, section = _auto_view(
                    d, resolved if not whole_persp else {}, matches, target_bb, part_bb, point)
                clip = False
            else:
                view_dir = _camera_dir_for_target(matches, view_spec)
                view_label = view_spec if isinstance(view_spec, str) else "custom"
                defect = _attachment_defect(d, resolved)
                clip = internal
            projection = cam_opts["projection"] or "perspective"
            label = str(resolved.get("label") or "target")
            png, ann_info = _closeup(
                d, matches, target_bb, view_dir, label=label,
                stem=_render_stem(d, f"closeup-{view_label}"),
                emphasis_mode=emphasis_mode or "emphasis",
                clip_near=clip, annotate=bool(annotate), highlight=highlight,
                background=bg, projection=projection, fov_deg=cam_opts["fov"],
                distance=cam_opts["distance"], scene_bbox=part_bb, point=point,
                defect=defect, section=section)
            cam = ann_info.get("camera") or {}
            size = target_bb.get("size") or (0, 0, 0)
            caption_extra = (
                f"CLOSE-UP of {label!r} ({len(matches)} subshape"
                f"{'s' if len(matches) != 1 else ''}), target extent "
                f"{size[0]:.3g} x {size[1]:.3g} x {size[2]:.3g} mm, z "
                f"{target_bb['min'][2]:.3g}..{target_bb['max'][2]:.3g}. Camera {view_label} "
                f"dir [{view_dir[0]:.2f}, {view_dir[1]:.2f}, {view_dir[2]:.2f}]"
                + (f", perspective fov {cam['vertical_fov_deg']:.0f} deg at "
                   f"{cam['distance']:.4g} mm from the target centre (scale bar true at that "
                   f"depth)" if cam.get("distance") else ", orthographic")
                + (", near-plane cutaway (internal target)" if clip else "")
                + (", SECTION through the target (cut face hatched, removed half ghosted)"
                   if section else "") + "."
            ) + _visibility_note(ann_info)
            if defect:
                caption_extra += (f" ATTACHMENT DEFECT ({defect.get('verdict')}, camera chosen to "
                                  f"show it): {defect.get('detail')}")
            if internal:
                sec = _auto_section_for_target(d, matches, target_bb)
                caption_extra += f" Auto-section: {sec.get('summary') or sec.get('plane')}."
            caption_extra += _palette_caption_block(
                ((ann_info.get("emphasis") or {}).get("palette")) if ann_info else None)
            result = _captioned_render(png, d, [f"closeup-{view_label}"],
                                       annotations=ann_info, extra=caption_extra.strip())
            return _with_save_note(result, _render_save_note(png, save_to))

        # Durable, not scratch: this exact path is returned when annotate=False with nothing
        # emphasized, and by the drawing-render fallback below.
        png = render_output_path(_render_stem(d, view_name), ".png")
        ann_info = None
        view_dir = None
        if view_spec != "auto":
            try:
                view_dir = _camera_dir_for_target([], view_spec)
            except Exception:  # noqa: BLE001
                view_dir = _vnorm((1.0, 1.0, 0.8))
        if view_dir is not None:
            png, frame, _r = _render_view_png(d, view_dir, png, name=str(view_name).upper(),
                                              annotate=bool(annotate), background=bg)
        else:
            frame = None
            params = {"doc_id": d, "out_path": png, "annotate": bool(annotate)}
            if bg is not None:
                params["background"] = bg
            r = cb.call("doc_render_png", params)
            if not r.get("ok") or not os.path.exists(png):
                try:
                    rd = cb.call("drawing_render_png", {"doc_id": d, "out_path": png})
                except Exception:  # noqa: BLE001
                    rd = {"ok": False}
                if rd.get("ok") and os.path.exists(png):
                    return _with_save_note(_captioned_render(png, d, [view_name]),
                                           _render_save_note(png, save_to))
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

        result = _captioned_render(png, d, [view_name], annotations=ann_info)
        return _with_save_note(result, _render_save_note(png, save_to))



    @mcp.tool()
    def contact_sheet(doc_id: Optional[str] = None,
                      views: Optional[list] = None,
                      highlight: Optional[Union[str, dict, list]] = None,
                      annotate: bool = True,
                      background: Optional[Union[list, str]] = None,
                      save_path: Optional[str] = None) -> list:
        """Render SEVERAL labelled views tiled into ONE image — the fastest multi-angle self-check.

        Default (no `views`): the planner's 4 most useful complementary views of the whole part
        — the same measured, SID-tagged 2x2 grid render_part returns without a view, with its
        per-tile caption, union coverage and the faces no tile sees (by sid).

        views: a list of view specs instead, each either a named view ("front"/"back"/"top"/
        "bottom"/"left"/"right"/"iso") or a custom-camera dict ({"azimuth","elevation"} or
        {"dir":[x,y,z]} — see render_part). Each such tile is headed with its view name and
        (with annotate=True) carries the B-rep EDGE outline with hidden lines removed, feature
        CALLOUTS and a SCALE BAR.

        highlight: same target vocabulary as render_part — a feature NAME ("Boss"), a feature id
        ("f2"), a sid ("F58db6d"), a geometry-query dict, or a list of those. The target is
        painted in a reserved emphasis colour and labelled IN EVERY TILE, which is the fastest
        way to see which views actually show it.

        annotate=False with explicit `views` falls back to the engine's own tiled sheet with no
        overlay; without `views` it gives the planner grid with no edges, tags or scale bar.

        background (optional): [r, g, b] 0-255 or "#rrggbb" — the clear colour every tile is
        rendered onto (see render_part). The inter-tile gutters are already dark.

        save_path (optional): ALSO write the finished SHEET to this .png path —
        the tiled multi-view image with its per-tile labels and overlays, byte-identical to
        the image returned inline (one flat RGB PNG, not one file per tile). Same rules as
        render_part: a relative path is project-root relative, the file must be inside the
        project/workspace dir unless CHUDWARE_RENDER_SAVE_ANYWHERE=1, and the caption gains
        `saved: <absolute path> (<bytes> B, sha256 <first 12>)`.

        Returns [image, caption]: the tiled PNG followed by a SELF-SUFFICIENT text caption —
        what each tile shows, part name and bbox, what is highlighted and in what colour, and
        what no tile could see.
        """
        save_to = _render_save_target(save_path)
        bg = render_background(background)
        d = _doc(doc_id)
        if not views:
            plan = _sp.plan_shots(d, k=4)
            png, caption = _shot_grid(d, plan, stem=_render_stem(d, "contact-sheet"),
                                      highlight=highlight, emphasis_mode=None,
                                      annotate=bool(annotate), background=bg)
            return _with_save_note([Image(path=png), caption], _render_save_note(png, save_to))
        if not annotate and highlight is None:
            png = render_output_path(_render_stem(d, "contact-sheet"), ".png")
            params = {"doc_id": d, "out_path": png, "views": views}
            if bg is not None:
                params["background"] = bg
            r = cb.call("doc_render_contact_sheet", params)
            if not r.get("ok") or not os.path.exists(png):
                raise ValueError(
                    f"Contact sheet failed: "
                    f"{r.get('reason', r.get('error', 'no bodies to render'))}")
            return _with_save_note(_captioned_render(png, d, views),
                                   _render_save_note(png, save_to))

        tiles, labels, infos = _annotated_view_tiles(d, views, highlight=highlight,
                                                     annotate=annotate, background=bg)
        if not tiles:
            raise ValueError("Contact sheet failed: no bodies to render")
        out = render_output_path(_render_stem(d, "contact-sheet"), ".png")
        _tile_pngs_grid(tiles, out, labels=labels,
                        cols=max(1, int(math.ceil(math.sqrt(len(tiles))))))
        return _with_save_note(
            _captioned_render(out, d, labels,
                              annotations=_merge_annotation_infos(infos),
                              extra=f"Contact sheet, {len(tiles)} tiles: "
                                    + ", ".join(labels) + "."),
            _render_save_note(out, save_to))



    @mcp.tool()
    def render_views(doc_id: Optional[str] = None,
                     views: Optional[list] = None,
                     target: Optional[Union[str, dict]] = None,
                     highlight: Optional[Union[str, dict, list]] = None,
                     annotate: bool = True,
                     background: Optional[Union[list, str]] = None,
                     save_path: Optional[str] = None) -> list:
        """Multi-view sheet — by default the planner's 7 most useful complementary views.

        Default (no `views`): the shot planner measures ~200 candidate cameras in the engine
        and picks the 7-view SET that together shows the most of the part (or of `target`) —
        the angular coverage for manufacturing feature recognition (holes, bosses, undercuts
        on back/bottom faces), without the flat, redundant tiles of a fixed ortho set. Tiles
        are 512x384 perspective shots lettered A-G, SID-tagged, with a caption per tile (camera
        direction, section or not, target visible %, best-seen faces), union coverage, the
        faces no tile sees (by sid), the tag legend and the ranking model.
        views=[...]: exactly those views instead (named views and/or custom
        {"azimuth","elevation"} / {"dir":[x,y,z]} specs — same vocabulary as render_part), e.g.
        ["front","back","left","right","top","bottom","iso"] for the 6-ortho+iso drawing set.

        target (optional): frame every tile on the target (feature id "f2", sid, or a
        plain-word face/edge query); it is painted in the emphasis colour in every tile.

        highlight (optional): same target vocabulary as render_part — a feature NAME ("Boss"),
        a feature id ("f2"), a sid ("F58db6d"), a query dict, or a list of those. Painted in a
        reserved emphasis colour and labelled in EVERY tile, so you can see at a glance which
        views actually show it. Unlike `target` it does not change the framing.

        annotate (default True): B-rep edge outline (hidden lines removed), SID tags or
        feature callouts, and a scale bar on every tile. annotate=False with explicit `views`
        gives the engine's plain tiled sheet.

        Heavier than render_part's 4-tile grid: use it when four views leave a face unseen.

        background (optional): [r, g, b] 0-255 or "#rrggbb" — the clear colour every tile is
        rendered onto (see render_part).

        save_path (optional): ALSO write the finished GRID to this .png path —
        byte-identical to the image returned inline (one flat RGB PNG, not one file per
        view). Same rules as render_part: a relative path is project-root relative, the file
        must be inside the project/workspace dir unless CHUDWARE_RENDER_SAVE_ANYWHERE=1, and
        the caption gains `saved: <absolute path> (<bytes> B, sha256 <first 12>)`.

        Returns [image, caption]. The caption is self-sufficient: what each tile shows, what
        is highlighted and in what colour, and what no view could see plus the tool for it.
        """
        save_to = _render_save_target(save_path)
        bg = render_background(background)
        d = _doc(doc_id)
        if not views:
            plan_target = None
            label = "views"
            if target is not None:
                resolved = _resolve_inspect_target(d, target)
                label = str(resolved.get("label") or "target")
                plan_target = {"matches": resolved["matches"], "label": label,
                               "bbox": _target_bbox(d, resolved["matches"]),
                               "kind": resolved.get("kind")}
            plan = _sp.plan_shots(d, target=plan_target, k=7)
            png, caption = _shot_grid(
                d, plan, stem=_render_stem(d, f"{label}-views" if plan_target else "views"),
                highlight=highlight, emphasis_mode="emphasis" if plan_target else None,
                annotate=bool(annotate), background=bg)
            return _with_save_note([Image(path=png), f"render_views grid. {caption}"],
                                   _render_save_note(png, save_to))

        view_list = _normalize_view_list(views)
        if target is None:
            # Native engine sheet only when there is nothing to overlay (it tiles internally,
            # so there is no per-tile image to draw edges or a highlight onto).
            if not annotate and highlight is None:
                png = render_output_path(_render_stem(d, "views"), ".png")
                params = {"doc_id": d, "out_path": png, "views": view_list}
                if bg is not None:
                    params["background"] = bg
                r = cb.call("doc_render_contact_sheet", params)
                if not r.get("ok") or not os.path.exists(png):
                    raise ValueError(
                        f"render_views failed: {r.get('reason', r.get('error', 'no bodies'))}")
                names = r.get("views") or [
                    (v if isinstance(v, str) else "custom") for v in view_list
                ]
                return _with_save_note(
                    _captioned_render(png, d, list(names),
                                      extra=f"render_views grid ({len(names)} tiles: "
                                            + ", ".join(str(n) for n in names) + ")."),
                    _render_save_note(png, save_to))
            tiles, labels, infos = _annotated_view_tiles(d, view_list, highlight=highlight,
                                                         annotate=annotate, background=bg)
            if not tiles:
                raise ValueError("render_views failed: no bodies to render")
            out = render_output_path(_render_stem(d, "views"), ".png")
            _tile_pngs_grid(tiles, out, labels=labels, cols=3)
            return _with_save_note(
                _captioned_render(out, d, labels,
                                  annotations=_merge_annotation_infos(infos),
                                  extra=f"render_views grid ({len(tiles)} tiles: "
                                        + ", ".join(labels) + ")."),
                _render_save_note(out, save_to))

        # Target-framed explicit views: the ENGINE frames each tile on the target AABB (a real
        # close-up per view), and every tile carries the exact-mask emphasis so you can tell at
        # a glance which of the requested cameras actually sees the feature.
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
            clip_near=_target_is_internal(matches, part_bb), background=bg)
        if not tile_paths:
            raise ValueError("render_views: every tile render failed")
        out = render_output_path(
            _render_stem(d, f"{resolved.get('label') or 'target'}-views"), ".png")
        # 3-column grid reads better for 6–7 views than the 2-col look_around default.
        _tile_pngs_grid(tile_paths, out, labels=labels, cols=3)
        seen = [labels[i] for i, inf in enumerate(infos)
                if ((inf or {}).get("emphasis") or {}).get("target_px")]
        try:
            first_dir = _camera_dir_for_target(matches, view_list[0])
        except ValueError:
            first_dir = _vnorm((1.0, 1.0, 0.8))
        caption = (
            f"render_views of {resolved.get('label')!r}: {len(tile_paths)} labelled tiles "
            f"({', '.join(labels)}), each FRAMED on the target by the engine. "
            f"Views that actually show the target: "
            f"{', '.join(seen) if seen else 'none — try inspect_section'}. "
            + _inspect_caption(d, resolved, target_bb, first_dir,
                               method="focus", n_matches=len(matches))
        )
        ann = _merge_annotation_infos(infos)
        if ann:
            caption += " " + _render_annotation_caption("target-framed grid", ann)
        return _with_save_note([Image(path=out), caption],
                               _render_save_note(out, save_to))


    @mcp.tool()
    def render_study_setup(fixtures: list,
                           load: Union[list, dict, str],
                           view: Optional[Union[str, dict]] = None,
                           doc_id: Optional[str] = None,
                           save_path: Optional[str] = None) -> list:
        """The FIXTURE/LOAD figure for an FEA evidence document — what is held, what pushes.

        Renders the whole part with the study's boundary conditions painted on the actual
        faces the solver would use: every FIXTURE face in ONE reserved colour (magenta),
        every LOAD face in another (violet), the rest of the part desaturated so the two
        groups read at a glance, plus ONE ARROW PER LOAD ENTRY — tip on that entry's face
        centroid, aimed along that entry's own force vector, length scaled to its magnitude
        against the largest entry — and the resultant burned into a band at the bottom of
        the frame. This is the figure a report needs beside a stress plot: it shows the
        reader which faces the numbers assumed were bolted down and WHAT was pushed on them,
        so an axial and a radial case on the same faces are different pictures.

        fixtures / load: EXACTLY the run_fea_static dialect, resolved by the same code —
          fixtures = list of face queries held fully fixed, e.g. ["bottom"] or
            [{"of":"face","side":"bottom","pick":"largest"}] or a face SID ("F58db6d");
          load = the mechanical load(s): [{"face":"top","force":[0,0,-500]}] (newtons, world
            XYZ), the polar form [{"face":"top","magnitude":500,"direction":[0,0,-1]}], or
            [{"face":"top","pressure":0.5}] (MPa, inward along the face normal). A single
            dict is accepted as a one-item list.
          Because the resolution, the key normalization and the setup gates are the SAME
          ones run_fea_static runs, a setup this tool draws is a setup that solves: an
          unresolvable query, a zero load, or a load applied entirely on a fixed face is
          refused here with the solver's own message. Draw the figure first, then run the
          study with the identical arguments.

        view (optional): a named view ("front"/"top"/"iso"/...) or a custom camera dict
          ({"azimuth","elevation"} or {"dir":[x,y,z]}) — same vocabulary as render_part.
          Omit it and the shot planner picks the whole-part camera that shows the most of the
          fixture + load faces legibly (measured in the engine, BC faces weighted).

        save_path (optional): ALSO write this figure to your own .png path. The
          file contains exactly the bytes returned inline — one flat RGB PNG of the shaded
          part with the fixture/load paint, glow outlines, per-face labels, the load arrows,
          the burned-in resultant band, the B-rep edge outline, feature callouts and scale
          bar already burned in (no layers, no alpha, no metadata), ready to embed in a
          report. Same rules as render_part: a relative path is project-root relative, and
          the file must be inside the project/workspace dir unless
          CHUDWARE_RENDER_SAVE_ANYWHERE=1.

        Returns [image, caption]. The caption carries the legend in words —
        `FIXTURE (magenta): <sids + areas>` and `LOAD (violet): <resultant N, direction,
        sids + areas>` — plus which of those faces this camera can actually see, so a
        non-vision reader (and the document itself) can quote the setup without the picture.
        """
        save_to = _render_save_target(save_path)
        d = _doc(doc_id)
        notes: list = []
        fix_queries = _norm_bc_list(
            require_list(fixtures, "fixtures",
                         'a list of face queries held fixed, e.g. ["bottom"]'),
            "fixtures", unwrap_query=True, notes=notes)
        load_entries = _norm_bc_list(
            require_list(load, "load",
                         'a list of {face query + force or pressure}, e.g. '
                         '[{"face":"top","force":[0,0,-500]}]'),
            "load", notes=notes)
        if not fix_queries:
            raise ValueError(
                "render_study_setup needs at least one fixture face query — a study figure "
                "with nothing held shows a part in free flight. Pass the mounted face, e.g. "
                'fixtures=["bottom"].')
        if not load_entries:
            raise ValueError(
                "render_study_setup needs the load — pass load=[{\"face\":\"top\","
                "\"force\":[0,0,-500]}] (newtons) or [{\"face\":\"top\",\"pressure\":0.5}] "
                "(MPa). " + _BC_SHAPE_HELP)
        # The solver's own resolution + gates. _sim_face_points is the fixture loop of
        # _build_static_study verbatim; _build_load_specs is the load side (it rejects a
        # zero load); _reject_fully_constrained_loads rejects a load that sits entirely on a
        # fixed face. A refusal is re-raised as the argument error it is.
        try:
            fixture_sets = [_sim_face_points(d, q) for q in fix_queries]
            load_specs = _build_load_specs(d, load_entries, "study setup")
            _reject_fully_constrained_loads([s for sets in fixture_sets for s in sets],
                                            load_specs, "study setup")
        except _StudyRefusal as refusal:
            raise ValueError(refusal.payload.get("error", str(refusal))) from None
        fix_groups = [_bc_group(d, q, sets, "fixture")
                      for q, sets in zip(fix_queries, fixture_sets)]
        load_groups = [_bc_group(d, e["face"], [spec["faces"]], "load")
                       for e, spec in zip(load_entries, load_specs)]

        fix_matches = [m for g in fix_groups for m in g["matches"]]
        load_matches = [m for g in load_groups for m in g["matches"]]
        both = ({s for g in fix_groups for s in g["sids"]}
                & {s for g in load_groups for s in g["sids"]})
        if both:
            raise ValueError(
                f"face(s) {', '.join(sorted(both))} are in BOTH the fixture and the load "
                "set, so one colour would paint over the other and the figure would lie "
                "about the setup. A fully fixed face cannot also carry a load — fix the "
                "study, then draw it.")
        all_matches = fix_matches + load_matches
        # One palette entry per match, in the SAME order _annotate_render walks them, so a
        # group's faces all land on that group's reserved colour.
        palette = {
            "mode": "study_setup",
            "entries": [{"key": str(m.get("sid") or f"fixture{i}"), "sid": m.get("sid"),
                         "rgb": list(_FIXTURE_RGB), "role": "fixture"}
                        for i, m in enumerate(fix_matches)]
                       + [{"key": str(m.get("sid") or f"load{i}"), "sid": m.get("sid"),
                           "rgb": list(_LOAD_RGB), "role": "load"}
                          for i, m in enumerate(load_matches)],
            "legend": (f"FIXTURE={_FIXTURE_COLOUR} "
                       f"#{_FIXTURE_RGB[0]:02X}{_FIXTURE_RGB[1]:02X}{_FIXTURE_RGB[2]:02X}, "
                       f"LOAD={_LOAD_COLOUR} "
                       f"#{_LOAD_RGB[0]:02X}{_LOAD_RGB[1]:02X}{_LOAD_RGB[2]:02X}"),
            "context_desat": _CONTEXT_DESAT,
            "glow_rgb": list(_EMPHASIS_GLOW),
        }

        if view is None:
            face_sid = _doc_topo_maps(d)["face_sid"]
            bc = sorted({face_sid[s] - 1 for g in fix_groups + load_groups
                         for s in g["sids"] if s in face_sid})
            pick = _sp.plan_shots(d, roles={"bc": bc}, k=1)["views"][0]
            view_dir = tuple(pick["dir"])
            view_name = f"study-{pick.get('label') or 'auto'}"
        else:
            view_dir = _camera_dir_for_target(all_matches, view)
            view_name = view if isinstance(view, str) else "custom"
        # Whole part, NOT framed on the BCs: a setup figure has to show what is being held
        # and pushed IN the part, otherwise the reader cannot tell which end is which.
        stem = _render_stem(d, "study-setup")
        png = render_output_path(stem, ".png")
        png, frame, _r = _render_view_png(d, view_dir, png,
                                          name=f"STUDY {str(view_name).upper()}"[:28],
                                          annotate=True)

        # The load VECTOR, not just the loaded faces. One arrow per entry, tip on that
        # entry's own face centroid, aimed along its own force and scaled to its magnitude
        # against the largest entry — so an axial 136 N and a radial 980 N on the SAME face
        # pair are different pictures, and a couple that sums to zero still draws its four
        # tractions. The resultant is burned into the frame as well: a PNG lifted out of the
        # tool response into a report carries its own numbers.
        resultant, terms, entries = _load_resultant(load_specs, load_groups)
        mag = math.sqrt(sum(c * c for c in resultant))
        direction = (f"[{resultant[0] / mag:.3f}, {resultant[1] / mag:.3f}, "
                     f"{resultant[2] / mag:.3f}]" if mag > 1e-12
                     else "none (the load entries cancel to a zero resultant)")
        mag_max = max([e["mag"] for e in entries] or [0.0])
        arrows = [{"at": e["anchor"], "vec": e["vector"], "mag": e["mag"],
                   "rel": (e["mag"] / mag_max) if mag_max > 1e-12 else 1.0,
                   "rgb": list(_LOAD_RGB), "label": f"{e['mag']:.4g} N"}
                  for e in entries if e["anchor"] and e["mag"] > 1e-12]
        per_entry = "; ".join(
            f"{', '.join(e['sids']) or 'face'} {e['mag']:.4g} N "
            f"[{e['vector'][0]:.4g}, {e['vector'][1]:.4g}, {e['vector'][2]:.4g}]"
            for e in entries[:3])
        banner = [f"LOAD resultant {mag:.4g} N   direction {direction}",
                  f"{len(entries)} entry(ies): {per_entry}"
                  + (f"; +{len(entries) - 3} more" if len(entries) > 3 else "")]

        png, ann_info = _annotate_render(
            png, d, view_dir, edges=True, callouts=True, scale=True, frame=frame,
            emphasis={"matches": all_matches, "mode": "study_setup",
                      "label": "FIXTURE+LOAD", "palette": palette},
            arrows=arrows, banner=banner,
            out_png=render_output_path(stem, ".png"))

        emp = (ann_info or {}).get("emphasis") or {}
        visible = set(emp.get("visible_sids") or [])
        fix_nodes = sum(g["n_points"] for g in fix_groups)
        load_nodes = sum(g["n_points"] for g in load_groups)
        legend = (
            f"Study setup figure. FIXTURE ({_FIXTURE_COLOUR}): {_sids_areas_text(fix_groups)}"
            f" — fully fixed, all DOF, {fix_nodes} solver sample node(s) across "
            f"{len(fix_matches)} face(s). "
            f"LOAD ({_LOAD_COLOUR}): resultant {mag:.4g} N, direction {direction}, "
            f"vector [{resultant[0]:.4g}, {resultant[1]:.4g}, {resultant[2]:.4g}] N "
            f"({'; '.join(terms)}); {_sids_areas_text(load_groups)}"
            f" — {load_nodes} solver sample node(s) across {len(load_matches)} face(s). "
            f"{len(ann_info.get('arrows') or [])} of {len(entries)} load entry(ies) are "
            f"drawn as a {_LOAD_COLOUR} arrow whose tip sits on that entry's face centroid, "
            f"aimed along that entry's own force vector and scaled to its magnitude (a "
            f"vector pointing at the camera becomes a ringed dot/cross), with the resultant "
            f"burned into the bottom band."
        )
        if not emp.get("target_px"):
            # Name the REAL cause. "no tessellation" was printed for every empty mask,
            # including the ordinary case of a camera that simply faces none of the BC
            # faces — which reads as an engine failure and sends the caller nowhere.
            why = emp.get("note") or (ann_info or {}).get("note")
            if not why and emp.get("hidden_sids"):
                why = ("this camera faces none of them: "
                       + ", ".join(emp["hidden_sids"]) + " — re-call with view= a direction "
                       "that sees them")
            legend += (" WARNING: the colour overlay could NOT be painted ("
                       + str(why or "no tessellation")
                       + ") — the load arrows and the resultant band are still drawn, but "
                         "read the sids above rather than the painted pixels.")
        else:
            hidden = [s for g in (fix_groups + load_groups) for s in g["sids"]
                      if s not in visible]
            legend += (f" Visible from this camera: {', '.join(sorted(visible)) or 'none'}."
                       + (f" NOT visible from here: {', '.join(hidden)} — re-call with"
                          f" view= another direction for a figure that shows them."
                          if hidden else ""))
        if notes:
            legend += " Input normalization: " + " ".join(str(n) for n in notes)
        return _with_save_note(
            _captioned_render(png, d, [view_name], annotations=ann_info, extra=legend),
            _render_save_note(png, save_to))



    @mcp.tool()
    def inspect_part(target: Union[str, dict, list],
                     doc_id: Optional[str] = None,
                     view: Optional[Union[str, dict]] = "auto",
                     zoom_factor: float = 1.0,
                     margin: Optional[float] = None,
                     annotate: bool = True,
                     emphasize: bool = True,
                     emphasis_mode: str = "emphasis",
                     cutaway: Optional[bool] = None) -> list:
        """CLOSE-UP of one face/edge/feature/point with framing controls — render_part(target=,
        view="auto") plus zoom_factor (>1 tighter), margin (context padding per side, default
        0.22), cutaway (None = auto: the planner's best single view, which is a SECTION through
        the target when no exterior camera shows it well; True forces a near-plane cutaway,
        False forces an uncut view) and emphasize=False (no paint). Same target and view
        vocabulary as render_part, including "distance", "fov" and "projection" in a view dict;
        perspective by default. Returns [image, caption] with what is framed, key dims, how
        much of the target the camera saw, and the camera.
        """
        d = _doc(doc_id)
        try:
            part_bb = cb.call("doc_bbox", {"doc_id": d})
        except Exception as e:  # noqa: BLE001
            raise ValueError(f"doc_bbox failed: {e}") from e
        if not part_bb or not part_bb.get("size"):
            raise ValueError("no part bounding box — build geometry first")
        resolved, target_bb, point = _resolve_camera_target(d, target, part_bb)
        matches = resolved["matches"]
        view_spec, cam_opts = _split_view(view)

        internal = bool(matches) and _target_is_internal(matches, part_bb)
        section = None
        if view_spec is None or (isinstance(view_spec, str) and view_spec.lower() == "auto"):
            view_dir, view_label, defect, section = _auto_view(
                d, resolved, matches, target_bb, part_bb, point)
            clip = bool(cutaway)
            if cutaway is not None:
                section = None
        else:
            view_dir, view_label = _camera_dir_for_target(matches, view_spec), (
                view_spec if isinstance(view_spec, str) else "custom")
            defect = _attachment_defect(d, resolved)
            clip = internal if cutaway is None else bool(cutaway)

        label = str(resolved.get("label") or "target")
        png_out, info = _closeup(
            d, matches, target_bb, view_dir, label=label,
            stem=_render_stem(d, label),
            emphasis_mode=(emphasis_mode or "emphasis") if emphasize else None,
            margin=(None if margin is None and float(zoom_factor) == 1.0 else
                    float(_PERSP_MARGIN if margin is None else margin)
                    / max(float(zoom_factor), 0.25)),
            clip_near=clip, annotate=bool(annotate),
            projection=cam_opts["projection"] or "perspective", fov_deg=cam_opts["fov"],
            distance=cam_opts["distance"], scene_bbox=part_bb, point=point, defect=defect,
            section=section)

        caption = _inspect_caption(d, resolved, target_bb, view_dir,
                                   method="focus", n_matches=len(matches))
        cam = info.get("camera") or {}
        caption += f" Camera: {view_label}" + (
            f", perspective fov {cam['vertical_fov_deg']:.0f} deg at {cam['distance']:.4g} mm"
            if cam.get("distance") else ", orthographic") + "."
        if clip:
            caption += (" Near-clip CUTAWAY: material between the camera and the target has been"
                        " cut away so you can see into it.")
        if section:
            caption += (" SECTION through the target: the cut face is hatched and the removed "
                        "half is drawn as a ghost outline.")
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

        ranked=True (default): the angles are the planner's best complementary SET of N
          cameras — measured in the engine (visible target area, legibility, edge contrast)
          and chosen greedily on marginal utility, so N tiles carry N different pieces of
          information; an internal target may get a SECTION tile (cut face hatched, removed
          half ghosted).
        ranked=False: a plain 360°/N orbit around the planner's best direction (near-plane
          cutaway for an internal target) — use when you specifically want even angular
          coverage rather than the best views.

        Use when render_part's grid leaves the shape ambiguous; check numbers first.

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
        label = str(resolved.get("label") or "target")
        plan_target = {"matches": matches, "label": label, "bbox": target_bb,
                       "kind": resolved.get("kind")}
        clip = False
        if ranked:
            picks = [{"dir": v["dir"], "label": v.get("label"), "section": v.get("section")}
                     for v in _sp.plan_shots(d, target=plan_target, k=n)["views"]]
        else:
            # An internal target is invisible from most of an even orbit; cut the near
            # material away so every tile of the sweep shows it.
            clip = _target_is_internal(matches, part_bb)
            base = _sp.plan_shots(d, target=plan_target, k=1)["views"][0]["dir"]
            step = 360 // n
            picks = [{"dir": vd, "label": f"orbit {i * step}deg", "section": None}
                     for i, vd in enumerate(_orbit_view_dirs(tuple(base), n=n))]

        tiles, labels, infos, dirs = [], [], [], []
        for i, p in enumerate(picks):
            try:
                png, info = _closeup(
                    d, matches, target_bb, p["dir"], label=label,
                    stem=_render_stem(d, f"{label}-view{i + 1}"),
                    emphasis_mode="emphasis" if emphasize else None,
                    margin=float(margin) / max(float(zoom_factor), 0.25),
                    clip_near=clip, annotate=bool(annotate), section=p.get("section"))
            except ValueError:
                continue
            vis = ((info or {}).get("emphasis") or {}).get("coverage")
            tiles.append(png)
            labels.append(f"{p.get('label') or f'view {i + 1}'}"
                          + ("  SECTION" if p.get("section") else "")
                          + (f"  {vis * 100:.1f}% target" if vis else "  target hidden"))
            infos.append(info)
            dirs.append(p["dir"])
        if not tiles:
            raise ValueError("look_around: every close-up render failed")
        out = render_output_path(_render_stem(d, f"{label}-look-around"), ".png")
        _tile_pngs_grid(tiles, out, labels=labels, cols=2 if len(tiles) > 1 else 1)
        best = max(range(len(infos)),
                   key=lambda i: ((infos[i] or {}).get("emphasis") or {}).get("coverage") or 0)
        caption = (
            f"Look-around of {label!r}: {len(tiles)} engine-framed close-ups"
            f"{' (planner set, ranked)' if ranked else ' (even orbit)'} — "
            + "; ".join(labels) + f". Best angle: {labels[best]}. "
            + _inspect_caption(d, resolved, target_bb, dirs[best],
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
        built from measured, complementary panels, so a single call answers "what is it, what
        does it look like from useful angles, and where is it on the part":

          1-3. HERO / ALT / ALT2 — the shot planner's best complementary SET of close-ups of
                         the target: the engine measures ~200 candidate cameras (exact visible
                         target area, legibility, edge contrast) and the set is chosen greedily
                         on marginal utility, so each panel shows something the others do not.
                         For an INTERNAL target (bore, pocket, counterbore) — or one no exterior
                         camera sees well — a panel may be a SECTION through the target: cut face
                         hatched, the removed half ghosted so its outline stays readable.
          4. CONTEXT   — the WHOLE part from the planner's best camera for seeing the target,
                         target highlighted and labelled, so the close-ups are placeable.

        In every close-up panel the target's exact visible faces are painted in the emphasis
        palette (shading preserved, so curvature still reads), everything else is desaturated,
        and the boundary is outlined. `emphasis_mode="per_face"` gives each face its own colour.
        `panels` trims the sheet (1 = hero only, 2 = hero+alt, 3 = +alt2, 4 = +context).
        `view` pins the HERO camera; the planner fills the other panels around it.

        The caption already carries: key dims of the target (area/length/radius), a describe_part
        outline EXCERPT, the emphasis palette metadata, and — per panel — how much of the target
        that camera could actually see. A panel that shows nothing SAYS it shows nothing.

        Use it when render_part's grid leaves a feature's shape ambiguous; check numbers first.

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
        pinned = ([{"dir": list(_camera_dir_for_target(matches, hint)),
                    "label": hint if isinstance(hint, str) else "custom"}]
                  if hint is not None else None)
        plan = _sp.plan_shots(d, target={"matches": matches, "label": label, "bbox": target_bb,
                                         "kind": resolved.get("kind")},
                              k=min(want, 3), pinned=pinned)
        panels_spec = list(zip(("HERO", "ALT", "ALT2"), plan["views"][:min(want, 3)]))

        tiles, labels, infos = [], [], []
        for tag, pick in panels_spec:
            try:
                png, info = _closeup(
                    d, matches, target_bb, pick["dir"], label=label,
                    stem=_render_stem(d, f"{label}-{tag.lower()}"),
                    emphasis_mode=mode, annotate=bool(annotate), section=pick.get("section"))
            except ValueError:
                continue
            cov = ((info or {}).get("emphasis") or {}).get("coverage") or 0.0
            tiles.append(png)
            labels.append(f"{tag}  {pick.get('label') or ''}"
                          + ("  SECTION" if pick.get("section") else "")
                          + f"  {cov * 100:.1f}% target")
            infos.append(info)

        # CONTEXT: the whole part from the camera that best shows the target within it.
        if want >= 4:
            try:
                ctx_view = _sp.plan_shots(d, roles={"target": plan["roles"]["target"],
                                                    "context": plan["roles"]["context"]},
                                          k=1)["views"][0]
                cam = dict(ctx_view["camera"], width=_CLOSEUP_TOKEN_W, height=_CLOSEUP_TOKEN_H)
                ctx = _tmp(".png")
                ctx, ctx_frame, _r = _render_view_png(
                    d, ctx_view["dir"], ctx, name="CONTEXT", annotate=False, camera=cam)
                ctx, ctx_info = _annotate_render(
                    ctx, d, tuple(ctx_view["dir"]), highlight=target, frame=ctx_frame,
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
                    + "; ".join(labels) + f". Panels ranked by {plan['model']}"
                    + (f" ({plan['model_note']})" if plan.get("model_note") else "") + ".")
        caption += _visibility_note(infos[0] if infos else None)
        caption += f" describe_part excerpt: {_describe_part_excerpt(d)}"
        if internal:
            sec = _auto_section_for_target(d, matches, target_bb)
            caption += f" Auto-section: {sec.get('summary') or sec.get('plane')}."
        palette = ((infos[0].get("emphasis") or {}).get("palette")) if infos and infos[0] else None
        caption += _palette_caption_block(palette)
        return [Image(path=out), caption]
