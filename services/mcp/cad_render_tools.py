"""CAD render + close-up inspection MCP tools (T-RF8 split of cad_tools.py): render_part, contact_sheet, render_views, inspect_part, look_around, inspect_feature."""
from __future__ import annotations

from mcp.server.fastmcp import FastMCP  # noqa: F401 — register signature
from mcp.server.fastmcp import Image
from typing import Optional
from typing import Union
import hashlib
import os
import math

from .cad_common import (
    _CLOSEUP_H,
    _CLOSEUP_MARGIN,
    _CLOSEUP_W,
    _CONTEXT_DESAT,
    _EMPHASIS_GLOW,
    _HIGHLIGHT_PALETTE,
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
    _match_point_cloud,
    _normalize_view_list,
    _palette_caption_block,
    _render_annotation_caption,
    _render_view_png,
    _resolve_inspect_target,
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


def _closeup(d: str, matches: list, target_bb: dict, view_dir, *, label: str,
             stem: str, emphasis_mode: Optional[str] = "emphasis",
             margin: float = _CLOSEUP_MARGIN, clip_near: bool = False,
             annotate: bool = True, W: int = _CLOSEUP_W, H: int = _CLOSEUP_H,
             highlight=None, background: Optional[list] = None) -> tuple:
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
                                      W=W, H=H, annotate=annotate, background=background)
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
    """[(env var, absolute root)] for every configured workspace/project directory."""
    out: list = []
    for var in _RENDER_SAVE_ROOT_VARS:
        raw = (os.environ.get(var) or "").strip()
        if raw:
            out.append((var, os.path.abspath(os.path.expanduser(raw))))
    return out


def _render_save_target(save_path) -> Optional[str]:
    """Validate `save_path` BEFORE any engine work and return the absolute file to write.

    Returns None when the caller passed nothing. Refuses a relative path (the MCP server's
    cwd is the daemon's, not the caller's, so a relative destination lands somewhere nobody
    can point a report at) and any path outside a configured workspace/project root unless
    CHUDWARE_RENDER_SAVE_ANYWHERE=1. Canonicalisation goes through the same
    _sanitize_output_path every export_part / save_part destination uses, so '..' sequences
    are collapsed before the containment test rather than after."""
    if save_path is None:
        return None
    if not isinstance(save_path, str) or not save_path.strip():
        raise ValueError("save_path must be a non-empty absolute path, e.g. "
                         "'/home/you/project/figures/fixture-setup.png'")
    expanded = os.path.expanduser(save_path.strip())
    if not os.path.isabs(expanded):
        raise ValueError(
            f"save_path must be ABSOLUTE; got {save_path!r}. This server runs in its own "
            "process (its cwd is the engine daemon's), so a relative path lands somewhere "
            "you cannot reference. Pass the full path under your project folder.")
    out = _sanitize_output_path(expanded)
    if not out.lower().endswith(".png"):
        out += ".png"
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
        if out == root or out.startswith(root.rstrip(os.sep) + os.sep):
            return out
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
    """CAD render + close-up inspection MCP tools (T-RF8 split of cad_tools.py): render_part, contact_sheet, render_views, inspect_part, look_around, inspect_feature."""


    @mcp.tool()
    def render_part(doc_id: Optional[str] = None,
                    view: Optional[Union[str, dict]] = None,
                    target: Optional[Union[str, dict]] = None,
                    emphasize: Optional[Union[str, dict, list]] = None,
                    emphasis_mode: str = "emphasis",
                    highlight: Optional[Union[str, dict, list]] = None,
                    annotate: bool = True,
                    display_quality: Optional[str] = None,
                    background: Optional[Union[list, str]] = None,
                    save_path: Optional[str] = None) -> list:
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

        background (optional): the frame's clear colour, [r, g, b] in 0-255 or "#rrggbb".
          The engine renders straight onto it, so a dark plate for a slide, a poster or a
          dark-mode page comes out of the renderer instead of being keyed out of the default
          light grey afterwards. Shading, orientation tint, callouts, gizmo and scale bar are
          unchanged (their text carries its own halo, so it stays readable on a dark field).
          Omit for the engine's flat 245 light grey.

        save_path (optional): ALSO write this render to your own absolute .png path (parent
          directories are created; a missing ".png" suffix is added). The file contains
          exactly the bytes of the image returned inline — the finished figure: the engine
          render plus every overlay this call produced (tint/gizmo/labels unless
          annotate=False, B-rep edge outline, highlight/emphasis paint, feature callouts,
          scale bar). It is a flat RGB PNG with no layers, no alpha and no embedded
          metadata, so it can be dropped straight into a report. The caption gains
          `saved: <path> (<bytes> B, sha256 <first 12>)` so you can cite the file you
          actually got. The path must be ABSOLUTE and inside the session's
          project/workspace directory (CHUDWARE_PROJECT_SAVE_DIR / CHUDWARE_PROJECT_DIR /
          CHUDWARE_FS_ROOT); set CHUDWARE_RENDER_SAVE_ANYWHERE=1 to lift that restriction.

        Returns [image, caption]: the PNG followed by a SELF-SUFFICIENT text caption — part name
        and bbox, the features visible in this view as id+name, what is highlighted and in what
        colour, the side-palette colour key, and an explicit note of what this camera CANNOT see
        plus the tool to call for it. A model that reads only the caption still knows the scene.
        """
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
                clip_near=internal, annotate=bool(annotate), highlight=highlight,
                background=bg)
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
                                                  annotate=bool(annotate), background=bg)
            elif view is not None:
                params = {"doc_id": d, "out_path": png, "view": view,
                          "annotate": bool(annotate)}
                if bg is not None:
                    params["background"] = bg
                r = cb.call("doc_render_view", params)
                if not r.get("ok") or not os.path.exists(png):
                    raise ValueError(
                        f"Render failed: "
                        f"{r.get('reason', r.get('error', 'no bodies to render'))}")
                frame = _frame_from_rpc(r)
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

        result = _captioned_render(png, d, [view_name], annotations=ann_info,
                                   extra=caption_extra.strip())
        note = _render_save_note(png, save_to)
        if isinstance(result, list) and len(result) == 2:
            return _with_save_note(result, note)
        return _with_save_note([result, caption_extra.strip()], note)



    @mcp.tool()
    def contact_sheet(doc_id: Optional[str] = None,
                      views: Optional[list] = None,
                      highlight: Optional[Union[str, dict, list]] = None,
                      annotate: bool = True,
                      background: Optional[Union[list, str]] = None,
                      save_path: Optional[str] = None) -> list:
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

        background (optional): [r, g, b] 0-255 or "#rrggbb" — the clear colour every tile is
        rendered onto (see render_part). The inter-tile gutters are already dark.

        save_path (optional): ALSO write the finished SHEET to this absolute .png path —
        the tiled multi-view image with its per-tile labels and overlays, byte-identical to
        the image returned inline (one flat RGB PNG, not one file per tile). Same rules as
        render_part: absolute, inside the project/workspace dir unless
        CHUDWARE_RENDER_SAVE_ANYWHERE=1, and the caption gains
        `saved: <path> (<bytes> B, sha256 <first 12>)`.

        Returns [image, caption]: the tiled PNG followed by a SELF-SUFFICIENT text caption —
        views, part name and bbox, features seen across the sheet, what is highlighted and in
        what colour, the side-palette colour key, and what no tile could see.
        """
        save_to = _render_save_target(save_path)
        bg = render_background(background)
        d = _doc(doc_id)
        view_list = views or ["front", "right", "top", "iso"]
        if not annotate and highlight is None:
            png = render_output_path(_render_stem(d, "contact-sheet"), ".png")
            params = {"doc_id": d, "out_path": png, "views": views or []}
            if bg is not None:
                params["background"] = bg
            r = cb.call("doc_render_contact_sheet", params)
            if not r.get("ok") or not os.path.exists(png):
                raise ValueError(
                    f"Contact sheet failed: "
                    f"{r.get('reason', r.get('error', 'no bodies to render'))}")
            return _with_save_note(_captioned_render(png, d, view_list),
                                   _render_save_note(png, save_to))

        tiles, labels, infos = _annotated_view_tiles(d, view_list, highlight=highlight,
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

        background (optional): [r, g, b] 0-255 or "#rrggbb" — the clear colour every tile is
        rendered onto (see render_part).

        save_path (optional): ALSO write the finished GRID to this absolute .png path — the
        6-ortho+iso (or target-framed) tiled image with its tile labels and overlays,
        byte-identical to the image returned inline (one flat RGB PNG, not one file per
        view). Same rules as render_part: absolute, inside the project/workspace dir unless
        CHUDWARE_RENDER_SAVE_ANYWHERE=1, and the caption gains
        `saved: <path> (<bytes> B, sha256 <first 12>)`.

        Returns [image, caption]. The caption is self-sufficient: resolved views, part name and
        bbox, the features seen across the sheet, what is highlighted and in what colour, the
        side-palette colour key, and what no view could see plus the tool to call for it.
        """
        save_to = _render_save_target(save_path)
        bg = render_background(background)
        d = _doc(doc_id)
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
            clip_near=_target_is_internal(matches, part_bb), background=bg)
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
          Omit it and the camera is chosen by the occlusion scorer as the one direction that
          actually SEES the most of the fixture + load faces.

        save_path (optional): ALSO write this figure to your own absolute .png path. The
          file contains exactly the bytes returned inline — one flat RGB PNG of the shaded
          part with the fixture/load paint, glow outlines, per-face labels, the load arrows,
          the burned-in resultant band, the B-rep edge outline, feature callouts and scale
          bar already burned in (no layers, no alpha, no metadata), ready to embed in a
          report. Same rules as render_part: absolute,
          inside the project/workspace dir unless CHUDWARE_RENDER_SAVE_ANYWHERE=1.

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

        target_bb = _target_bbox(d, all_matches)
        if view is None:
            pick = _ideal_views(d, all_matches, target_bb, n=1)[0]
            view_dir = pick["dir"]
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
