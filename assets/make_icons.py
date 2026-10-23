"""Generate Chudware icon atlases (light + dark) with Pillow.

Run:  python host/assets/make_icons.py
Outputs into host/assets/icons/:
  atlas_dark.png   atlas_light.png   atlas.json
"""
from __future__ import annotations

import json
import math
import os
import sys
from pathlib import Path
from typing import Callable

from PIL import Image, ImageDraw

# ---------------------------------------------------------------------------
# Contract inventory  (parsed from contracts/icons.md — mirrored here so
# we can assert at build-time that draw functions cover every name).
# ---------------------------------------------------------------------------
CONTRACT_NAMES: list[str] = [
    # App/tree
    "app", "part", "assembly", "sketch_node", "plane", "axis", "point",
    "csys", "origin", "equations", "material", "mates_folder", "body_folder",
    "history", "rollback", "error_badge", "warning_badge", "ok_badge",
    "suppressed",
    "box_feature", "cylinder_feature", "sphere_feature", "cone_feature",
    "torus_feature",
    # Features tab
    "extrude_boss", "extrude_cut", "revolve_boss", "revolve_cut", "sweep",
    "sweep_cut", "loft", "loft_cut", "boundary", "fillet", "chamfer",
    "shell", "draft", "rib", "hole_wizard", "linear_pattern",
    "circular_pattern", "mirror", "ref_plane", "ref_axis", "ref_point",
    "ref_csys", "curve_helix", "combine", "intersect", "subtract", "split",
    "wrap",
    # Sketch tab
    "sketch", "smart_dim", "line", "rect_corner", "rect_center", "circle",
    "arc_3pt", "arc_center", "arc_tangent", "polygon", "slot", "spline",
    "ellipse", "point_ent", "text_ent", "trim", "extend", "offset_ent",
    "convert_ent", "mirror_ent", "pattern_lin_ent", "pattern_circ_ent",
    "move_ent", "construction", "relations", "fix",
    "display_relations", "fully_define",
    # Assembly tab
    "insert_component", "mate", "move_component", "rotate_component",
    "pattern_component", "smart_fasteners", "exploded_view", "bom",
    "interference",
    # Evaluate tab
    "measure", "mass_props", "section_props", "interference_eval",
    "draft_analysis", "thickness", "curvature", "zebra", "symmetry_check",
    "check", "geometry_analysis", "performance",
    # Simulation tab
    "study_static", "study_modal", "study_flow", "fixture", "load_force",
    "load_pressure", "mesh", "run_study", "results", "flow_arrow",
    # Viewport HUD
    "zoom_fit", "view_cube", "display_shaded", "display_shaded_edges",
    "display_hlr", "display_hlv", "display_wireframe", "display_edges",
    "display_wire", "section_view", "pan_view", "rotate_view", "zoom_area",
    "view_selector", "normal_to", "orientation",
    # General
    "ok_check", "cancel_x", "settings", "save", "open", "new_doc",
    "undo", "redo", "theme_toggle", "ai_panel", "activity", "prompt_edit",
    "msg_edit",
    "copy", "paste", "delete", "folder_new", "refresh",
    # C25: sketch flyout variant icons (append-only)
    "rect_3pt_corner", "rect_3pt_center", "parallelogram",
    "circle_perimeter",
    "slot_ctr_str", "slot_arc_3pt", "slot_arc_ctr",
    "spline_style", "spline_surface", "spline_equation",
    "ellipse_partial", "parabola", "conic",
    # C25b: HUD hide/show visibility toggle (normal_to read as "arrow", not "visibility")
    "hide_show_eye",
    # ---- Overhaul contract (append-only; new rows packed after existing → existing UVs unchanged) ----
    # Workspace tabs
    "ws_features", "ws_sketch", "ws_evaluate", "ws_assembly",
    "ws_sheetmetal", "ws_weldments", "ws_surfaces", "ws_appearance",
    # Simulation study types (extended)
    "study_thermal", "study_fatigue",
    # Mesh types
    "mesh_tet", "mesh_hex", "mesh_auto",
    # Sheet metal
    "sm_base_flange", "sm_edge_flange", "sm_hem", "sm_jog", "sm_flat_pattern",
    # Weldments
    "weld_member", "weld_gusset", "weld_end_cap",
    # Appearance
    "color_picker", "appr_metal", "appr_plastic", "appr_glass",
    "appr_paint", "appr_default", "appr_steel", "appr_brass", "appr_dark",
    # Sketch shape drop-ins (polygon dropdown variants + star) and airfoil
    "poly_triangle", "poly_square", "poly_pentagon", "poly_octagon", "poly_star",
    "airfoil",
    # ---- (append-only): Manufacture toolpath tiles. The shipped art is the
    # photographic toolpath render from scripts/cam_toolpath_icons.py (real cam_generate
    # output); these drawers are line-art FALLBACKS so a cell is never blank.
    "cam_face", "cam_contour2d", "cam_pocket2d", "cam_adaptive2d", "cam_slot",
    "cam_chamfer2d", "cam_engrave",
    "cam_drill", "cam_thread_mill",
    "cam_parallel3d", "cam_waterline3d", "cam_scallop3d", "cam_pocket3d",
    "cam_turn_rough", "cam_turn_finish", "cam_turn_face", "cam_turn_part",
    "cam_cut2d", "cam_probe",
    # ---- Parts library (GUI V2, append-only): taxonomy category glyphs + panel chrome ----
    "parts_all", "cat_fasteners", "cat_bearings", "cat_stock_metal",
    "cat_drivetrain", "cat_wheels", "cat_tools", "cat_cutting_tools",
    "cat_electronics", "cat_hydraulics", "cat_hardware",
    "vendor_globe", "browser_panel", "filter_funnel", "health_check",
    # ---- PLM Project Hub (append-only): planning/collaboration glyphs ----
    "plan_board", "issue_bug", "milestone_flag", "team",
    # ---- PLM Git tab (append-only) ----
    "git",
    "thread",
    # ---- Chat context refs (append-only): composer attach + context picker ----
    "paperclip", "context_cube",
    # ---- Copilot modes (append-only): mode-picker Ask + Eng. Firm glyphs ----
    "ask", "eng_firm",
    # ---- Workspace rail tabs (append-only; appended LAST so every existing
    # row keeps its packed UVs). The app logged "unknown icon: 'ws_parts' /
    # 'ws_manufacture'" every run and fell through to procedural glyphs. ----
    "ws_parts", "ws_manufacture",
    # ---- Copilot Artifacts + PLM Graph (append-only; appended
    # LAST so every pre-existing row keeps its packed UVs). The Artifacts panel
    # drew one 2 px grey dot per row regardless of kind and borrowed `team` for
    # its tab header; the PLM Graph toolbar had no glyphs at all. ----
    "file_image", "file_mesh", "file_step", "file_doc", "file_data",
    "file_code", "file_archive", "file_generic",
    "artifacts",
    "orbit_auto", "depth_3d", "legend", "isolate",
    # ---- 2026-08-17 full 3D-render coverage (append-only; appended LAST so
    # every pre-existing row keeps its packed UVs). Engine-rendered tiles via
    # scripts/icon_pipeline.py recipes; the drawers below are FALLBACK glyphs. ----
    "rotate_ent", "stock",
    "ws_model", "ws_drawing", "ws_electronics", "ws_simulate",
    "ecad_pcb", "ecad_board3d", "ecad_footprint", "ecad_route", "ecad_via",
    "ecad_zone", "ecad_autoroute", "ecad_fill", "ecad_gerber", "ecad_drill",
    "ecad_drc", "ecad_update",
    "sch_new", "sch_symbol", "sch_wire", "sch_power", "sch_label", "sch_erc",
    "sch_simulate",
    # ---- 2026-10-05 Simulation GUI overhaul (append-only; appended LAST so every
    # pre-existing row keeps its packed order). Simulate tabs/commands borrowed
    # unrelated glyphs (Topology/EM on study_static, Heat Loads on load_pressure,
    # Piping on sweep, Hydraulics sizing on library categories). Line-art. ----
    "study_topology", "study_em", "study_buckling", "study_contact",
    "study_vibration", "study_thermal_fea", "study_thermal_cfd",
    "study_piping", "study_hydraulics",
    "load_heat", "fluid", "flow_bc", "em_port", "em_boundary", "freq_range",
    "new_network", "new_circuit", "route_pipe",
    "size_actuator", "size_pump", "size_motor", "pump_drive",
    "vessel", "duct", "spec_check", "schematic", "plots_view",
    "media_first", "media_prev", "media_play", "media_pause", "media_next", "media_last",
]

# FM + ribbon-adjacent refresh scope (Agent 3 — fm_copilot_gui_rectify).
# Every name here must exist in CONTRACT_NAMES; draw_* is tuned for tree/ribbon legibility.
FM_REFRESH_MANIFEST: list[str] = [
    # FM tree chrome
    "origin", "ref_plane", "ref_axis", "ref_point", "ref_csys",
    "part", "assembly", "sketch_node",
    "equations", "material", "body_folder", "mates_folder", "mate",
    "history", "rollback", "suppressed",
    "error_badge", "warning_badge", "ok_badge",
    "box_feature", "cylinder_feature", "sphere_feature", "cone_feature", "torus_feature",
    # FM document toolbar
    "new_doc", "open", "save", "undo", "redo",
    # Ribbon Features tab (workflow adjacent to FM)
    "sketch",
    "extrude_boss", "extrude_cut", "revolve_boss", "revolve_cut",
    "sweep", "sweep_cut", "loft", "loft_cut", "boundary",
    "fillet", "chamfer", "shell", "draft", "rib", "hole_wizard",
    "linear_pattern", "circular_pattern", "mirror",
    "combine", "subtract", "intersect", "split", "wrap",
    "curve_helix",
    # Workspace rail (left pane tabs)
    "ws_features", "ws_sketch", "ws_evaluate", "ws_assembly",
    "ws_sheetmetal", "ws_weldments", "ws_surfaces", "ws_appearance",
    # FM row visibility + assembly tree
    "hide_show_eye", "insert_component", "move_component",
    # Sheet-metal / weldment tree rows
    "sm_base_flange", "sm_edge_flange", "weld_member",
]


def validate_fm_manifest() -> list[str]:
    """Return FM manifest names missing from CONTRACT_NAMES."""
    contract = set(CONTRACT_NAMES)
    return [n for n in FM_REFRESH_MANIFEST if n not in contract]

# ---------------------------------------------------------------------------
# Palettes
# ---------------------------------------------------------------------------
class Palette:
    def __init__(self, stroke, accent, fill, bg, accent2, ok, warn, err):
        self.stroke  = stroke   # main outline colour
        self.accent  = accent   # highlight / active colour
        self.fill    = fill     # light interior fill
        self.bg      = bg       # cell background (RGBA)
        self.accent2 = accent2  # secondary accent
        self.ok      = ok
        self.warn    = warn
        self.err     = err

# Accent = the PRIMARY role of the two-role scheme: "what the operation produced".
# accent2 = SECONDARY, the direction/annotation cue (arrows, sweep paths, mirror
# planes), which keeps the blue family so a cue never competes with the result.
#
# Scope warning, so nobody debugs the wrong file: these baked pixels are largely
# COSMETIC. icons_sdf.cpp re-rasterizes every line-art glyph into an SDF and
# ribbon.cpp::ribbon_icon tints it at draw time from the live palette, so what
# makes a line-art icon orange at runtime is ui_theme.cpp, NOT these constants.
# They still have to be right: they keep the source honest, and they are what any
# path that samples the atlas RGB directly (contact sheets, the raster_only blit
# fallback, docs screenshots) actually gets.
DARK = Palette(
    stroke  = (229, 234, 241),   # #E5EAF1
    accent  = (242, 130, 22),    # #F28216
    fill    = (255, 255, 255, 30),
    bg      = (0, 0, 0, 0),
    accent2 = (56, 148, 227),    # #3894E3 — #0078D7 lifted for dark-bg contrast
    ok      = (52, 211, 153),
    warn    = (251, 191, 36),
    err     = (239, 68, 68),
)
LIGHT = Palette(
    stroke  = (58, 67, 80),      # #3A4350
    # #C25E05 — the primary orange DEEPENED, not reused. #F28216 measures 2.4:1
    # against the light ribbon sheet (#F3F5F7), well under the 3:1 a 2 px hairline
    # needs at @20 — a light-theme accent stroke in it reads as a smudge. Holding
    # the hue (30 deg -> 28 deg) while pushing saturation to full and dropping
    # value to 0.76 gives 3.9:1, which also beats the 3.8:1 of the teal #1A8C7F it
    # replaces, so nothing regresses on the light sheet.
    accent  = (194, 94, 5),
    fill    = (0, 0, 0, 20),
    bg      = (0, 0, 0, 0),
    accent2 = (0, 94, 168),      # #005EA8 — secondary blue darkened to match
    ok      = (22, 163, 74),
    warn    = (217, 119, 6),
    err     = (220, 38, 38),
)

SS = 8   # supersample factor

# ---------------------------------------------------------------------------
# Low-level draw helpers  (all coordinates in supersampled space)
# ---------------------------------------------------------------------------

def _sw(cell: int) -> int:
    """stroke width: 2 px equivalent at final resolution"""
    return max(2, int(cell * SS * 2 / cell))   # = 2*SS


def stroke_width(cell: int) -> int:
    return 2 * SS


def polyline(d: ImageDraw.ImageDraw, pts, color, lw: int):
    for i in range(len(pts) - 1):
        d.line([pts[i], pts[i + 1]], fill=color, width=lw)


def arrow(d: ImageDraw.ImageDraw, x0, y0, x1, y1, color, lw: int):
    """Straight arrow from (x0,y0) to (x1,y1)."""
    d.line([(x0, y0), (x1, y1)], fill=color, width=lw)
    # arrowhead
    dx, dy = x1 - x0, y1 - y0
    angle = math.atan2(dy, dx)
    head = lw * 2.5
    for side in (math.pi / 6, -math.pi / 6):
        bx = x1 - head * math.cos(angle - side)
        by = y1 - head * math.sin(angle - side)
        d.line([(x1, y1), (bx, by)], fill=color, width=lw)


def filled_rect(d, x0, y0, x1, y1, fill, outline, lw):
    d.rectangle([x0, y0, x1, y1], fill=fill, outline=outline, width=lw)


def circle(d, cx, cy, r, fill=None, outline=None, lw=2):
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=fill, outline=outline, width=lw)


def arc_segment(d, cx, cy, r, start_deg, end_deg, color, lw):
    bb = [cx - r, cy - r, cx + r, cy + r]
    d.arc(bb, start=start_deg, end=end_deg, fill=color, width=lw)


def filled_circle(d, cx, cy, r, fill):
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=fill)


def plus_sign(d, cx, cy, r, color, lw):
    d.line([(cx - r, cy), (cx + r, cy)], fill=color, width=lw)
    d.line([(cx, cy - r), (cx, cy + r)], fill=color, width=lw)


def x_sign(d, cx, cy, r, color, lw):
    d.line([(cx - r, cy - r), (cx + r, cy + r)], fill=color, width=lw)
    d.line([(cx + r, cy - r), (cx - r, cy + r)], fill=color, width=lw)


def check_mark(d, cx, cy, r, color, lw):
    x0, y0 = cx - r * 0.6, cy
    x1, y1 = cx - r * 0.1, cy + r * 0.6
    x2, y2 = cx + r * 0.7, cy - r * 0.5
    d.line([(x0, y0), (x1, y1), (x2, y2)], fill=color, width=lw)


def iso_box(d, cx, cy, r, stroke, lw):
    """Isometric-ish box outline (top + front two faces)."""
    # top diamond
    t  = (cx,        cy - r * 0.7)
    rt = (cx + r,    cy - r * 0.35)
    lt = (cx - r,    cy - r * 0.35)
    rm = (cx + r,    cy + r * 0.15)
    lm = (cx - r,    cy + r * 0.15)
    b  = (cx,        cy + r * 0.5)
    cm = (cx,        cy)           # center mid
    polyline(d, [lt, t, rt, rm, b, lm, lt], stroke, lw)
    polyline(d, [lt, cm, rt], stroke, lw)
    polyline(d, [cm, b], stroke, lw)


def _iso_pts(cx, cy, r):
    """The 7 canonical isometric-cube silhouette points (2:1 projection).
    Mirrors assets/make_logo.py so the atlas glyphs match the brand mark."""
    w, h = r, r * 0.5
    return {
        "top": (cx, cy - 2 * h), "rt": (cx + w, cy - h), "lt": (cx - w, cy - h),
        "c": (cx, cy), "rb": (cx + w, cy + h), "lb": (cx - w, cy + h),
        "bot": (cx, cy + 2 * h),
    }


def brand_cube(d, cx, cy, r, p, lw, notch=True, accent=True, spark=None):
    """The Chudware brand mark in atlas line-art: isometric cube (2:1 projection)
    with a cubic corner notch boolean-cut out of the top corner and, optionally,
    a 4-point spark seated in the notch ("star", or "diamond" for tiny cells).
    notch=False draws the plain cube in the same style (assembly sub-bodies)."""
    pt = _iso_pts(cx, cy, r)
    top_fill = _rgba(p.accent, 72) if accent else _rgba(p.stroke, 40)
    if notch:
        # notch sub-cube seated at the top corner (half the edge length)
        q = _iso_pts(cx, cy - r + r * 0.5, r * 0.5)
        # top face, flat accent fill, minus the notched quarter
        d.polygon([pt["lt"], q["lt"], q["c"], q["rt"], pt["rt"], pt["c"]], fill=top_fill)
    else:
        d.polygon([pt["lt"], pt["top"], pt["rt"], pt["c"]], fill=top_fill)
    # left / right faces — faint, matching the shaded_prism idiom
    d.polygon([pt["lt"], pt["c"], pt["bot"], pt["lb"]], fill=_rgba(p.stroke, 30))
    d.polygon([pt["rt"], pt["c"], pt["bot"], pt["rb"]], fill=_rgba(p.stroke, 46))
    if notch:
        # notch interior faces — fainter still, reads as material removed
        d.polygon([q["lt"], q["top"], q["rt"], q["c"]], fill=_rgba(p.accent, 26))
        d.polygon([q["lt"], q["c"], q["bot"], q["lb"]], fill=_rgba(p.stroke, 14))
        d.polygon([q["rt"], q["c"], q["bot"], q["rb"]], fill=_rgba(p.stroke, 20))
    # outer edges: hexagon silhouette minus the notched corner + interior edges
    edges = [(pt["rt"], pt["rb"]), (pt["rb"], pt["bot"]), (pt["bot"], pt["lb"]),
             (pt["lb"], pt["lt"]), (pt["lt"], pt["c"]), (pt["c"], pt["rt"]),
             (pt["c"], pt["bot"])]
    if notch:
        edges += [(pt["lt"], q["lt"]), (q["rt"], pt["rt"])]
    else:
        edges += [(pt["lt"], pt["top"]), (pt["top"], pt["rt"])]
    for a, b in edges:
        d.line([a, b], fill=p.stroke, width=lw)
    if notch:
        # the "AI cut" edges get the accent, slightly thinner
        nlw = max(1, int(lw * 0.66))
        for a, b in [(q["lt"], q["c"]), (q["c"], q["rt"]), (q["c"], q["bot"]),
                     (q["lt"], q["lb"]), (q["rt"], q["rb"]), (q["lb"], q["bot"]),
                     (q["rb"], q["bot"]), (q["lt"], q["top"]), (q["rt"], q["top"])]:
            d.line([a, b], fill=p.accent, width=nlw)
    if spark:
        sx, sy = cx, cy - r * 0.52          # seated at the notch center
        sr = r * 0.34
        if spark == "star":
            pts = []
            for i in range(8):
                ang = i * math.pi / 4 - math.pi / 2
                rad = sr if i % 2 == 0 else sr * 0.35
                pts.append((sx + rad * math.cos(ang), sy + rad * math.sin(ang)))
        else:  # "diamond" — simplified so it doesn't smear at 20 px
            pts = [(sx, sy - sr), (sx + sr * 0.6, sy), (sx, sy + sr), (sx - sr * 0.6, sy)]
        d.polygon(pts, fill=p.stroke)


def diagonal_box(d, x0, y0, x1, y1, stroke, lw):
    """MISSING placeholder — rectangle with diagonal cross."""
    d.rectangle([x0, y0, x1, y1], outline=stroke, width=lw)
    d.line([(x0, y0), (x1, y1)], fill=stroke, width=lw)
    d.line([(x1, y0), (x0, y1)], fill=stroke, width=lw)


def _rgba(c, a):
    return (c[0], c[1], c[2], a)


def shaded_prism(d, s, x, y, w, h, dx, dy, p, accent=False, cut=False):
    """Small isometric solid with distinct top/front/side faces."""
    lw = max(1, int(1.6 * SS))
    base = p.accent if accent else p.stroke
    err = p.err if cut else base
    top = [(x, y), (x + dx, y - dy), (x + w + dx, y - dy), (x + w, y)]
    side = [(x + w, y), (x + w + dx, y - dy), (x + w + dx, y + h - dy), (x + w, y + h)]
    front = [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]
    d.polygon(front, fill=_rgba(base, 36 if not cut else 18))
    d.polygon(top, fill=_rgba(p.accent, 72 if not cut else 26))
    d.polygon(side, fill=_rgba(base, 54 if not cut else 22))
    for pts, col in [(front, err), (top, err if cut else p.accent), (side, err)]:
        d.line(pts + [pts[0]], fill=col, width=lw)
    d.line([(x + dx, y - dy), (x + dx, y + h - dy)], fill=_rgba(base, 105), width=max(1, lw // 2))


def shaded_cylinder(d, s, cx, cy, w, h, p, accent=False):
    lw = max(1, int(1.6 * SS))
    col = p.accent if accent else p.stroke
    fill = _rgba(col, 46)
    top = [cx - w / 2, cy - h / 2, cx + w / 2, cy - h / 2 + w * 0.34]
    bot = [cx - w / 2, cy + h / 2 - w * 0.34, cx + w / 2, cy + h / 2]
    d.rectangle([cx - w / 2, cy - h / 2 + w * 0.17, cx + w / 2, cy + h / 2 - w * 0.17],
                fill=fill)
    d.ellipse(top, fill=_rgba(p.accent, 60), outline=col, width=lw)
    d.line([(cx - w / 2, cy - h / 2 + w * 0.17), (cx - w / 2, cy + h / 2 - w * 0.17)],
           fill=col, width=lw)
    d.line([(cx + w / 2, cy - h / 2 + w * 0.17), (cx + w / 2, cy + h / 2 - w * 0.17)],
           fill=col, width=lw)
    d.arc(bot, start=0, end=180, fill=_rgba(col, 150), width=lw)
    d.arc(bot, start=180, end=360, fill=col, width=lw)


def draw_pencil(d, s, p, x0, y0, x1, y1, width):
    """Detailed diagonal pencil with wood tip and eraser."""
    lw = max(1, int(1.4 * SS))
    dx, dy = x1 - x0, y1 - y0
    ang = math.atan2(dy, dx)
    nx, ny = -math.sin(ang), math.cos(ang)
    tip_len = width * 2.3
    er_len = width * 1.35
    bx0, by0 = x0 + math.cos(ang) * er_len, y0 + math.sin(ang) * er_len
    tx0, ty0 = x1 - math.cos(ang) * tip_len, y1 - math.sin(ang) * tip_len
    body = [(bx0 + nx*width, by0 + ny*width), (tx0 + nx*width, ty0 + ny*width),
            (tx0 - nx*width, ty0 - ny*width), (bx0 - nx*width, by0 - ny*width)]
    eraser = [(x0 + nx*width, y0 + ny*width), (bx0 + nx*width, by0 + ny*width),
              (bx0 - nx*width, by0 - ny*width), (x0 - nx*width, y0 - ny*width)]
    tip = [(tx0 + nx*width, ty0 + ny*width), (x1, y1), (tx0 - nx*width, ty0 - ny*width)]
    d.polygon(body, fill=_rgba(p.accent, 95))
    d.line(body + [body[0]], fill=p.accent, width=lw)
    d.line([(bx0, by0), (tx0, ty0)], fill=_rgba(p.stroke, 155), width=max(1, lw // 2))
    d.polygon(eraser, fill=_rgba(p.warn, 150))
    d.line(eraser + [eraser[0]], fill=p.stroke, width=lw)
    d.polygon(tip, fill=_rgba(p.stroke, 50))
    d.line(tip + [tip[0]], fill=p.stroke, width=lw)
    filled_circle(d, x1, y1, max(1, lw // 2), p.stroke)

# ---------------------------------------------------------------------------
# Draw functions — one per icon name.
# Each receives: d (ImageDraw on SS*cell canvas), cell (target px), palette
# Canvas is already cell*SS square; draw in that space.
# ---------------------------------------------------------------------------

DrawFn = Callable[[ImageDraw.ImageDraw, int, Palette], None]
_REGISTRY: dict[str, DrawFn] = {}


def icon(name: str):
    def decorator(fn: DrawFn) -> DrawFn:
        _REGISTRY[name] = fn
        return fn
    return decorator


def _s(cell, frac):
    """fraction of SS*cell canvas"""
    return cell * SS * frac


# ---- App / tree ----

@icon("app")
def draw_app(d, cell, p):
    """The Chudware brand mark: iso cube, corner notch, spark in the notch."""
    s = cell * SS
    brand_cube(d, s * 0.5, s * 0.5, s * 0.36, p, stroke_width(cell),
               spark="star" if cell > 20 else "diamond")

@icon("part")
def draw_part(d, cell, p):
    """Single solid body echoing the brand cube: corner-notch, no spark."""
    s = cell * SS
    brand_cube(d, s * 0.5, s * 0.5, s * 0.33, p, stroke_width(cell))

@icon("assembly")
def draw_assembly(d, cell, p):
    """Three mated bodies; the main one carries the brand corner-notch."""
    s = cell * SS
    lw = max(1, int(stroke_width(cell) * 0.8))
    brand_cube(d, s * 0.50, s * 0.30, s * 0.17, p, lw)
    brand_cube(d, s * 0.26, s * 0.68, s * 0.13, p, lw, notch=False, accent=False)
    brand_cube(d, s * 0.74, s * 0.68, s * 0.13, p, lw, notch=False, accent=False)
    mlw = max(1, stroke_width(cell) // 2)
    d.line([(s * 0.50, s * 0.48), (s * 0.26, s * 0.54)], fill=p.accent, width=mlw)
    d.line([(s * 0.50, s * 0.48), (s * 0.74, s * 0.54)], fill=p.accent, width=mlw)

@icon("sketch_node")
def draw_sketch_node(d, cell, p):
    """Professional pencil on a sketch plane."""
    s = cell * SS
    lw = stroke_width(cell)
    pts = [(s*0.14, s*0.65), (s*0.42, s*0.28), (s*0.88, s*0.38), (s*0.60, s*0.76)]
    d.polygon(pts, fill=_rgba(p.stroke, 20))
    d.line(pts + [pts[0]], fill=p.stroke, width=lw)
    for t in [0.34, 0.50, 0.66]:
        d.line([(s*(0.14 + (0.88-0.14)*t), s*(0.65 + (0.38-0.65)*t)),
                (s*(0.42 + (0.60-0.42)*t), s*(0.28 + (0.76-0.28)*t))],
               fill=_rgba(p.stroke, 75), width=max(1, lw//2))
    draw_pencil(d, s, p, s*0.20, s*0.78, s*0.82, s*0.18, s*0.045)

@icon("plane")
def draw_plane(d, cell, p):
    """Parallelogram representing a construction plane."""
    s = cell * SS
    lw = stroke_width(cell)
    pts = [(s*0.1, s*0.65), (s*0.45, s*0.25), (s*0.9, s*0.35), (s*0.55, s*0.75)]
    d.polygon(pts, fill=None, outline=p.stroke)
    d.line([pts[0], pts[1], pts[2], pts[3], pts[0]], fill=p.stroke, width=lw)
    # normal arrow
    cx, cy = s*0.5, s*0.5
    arrow(d, cx, cy, cx, cy - s*0.32, p.accent, lw)

@icon("axis")
def draw_axis(d, cell, p):
    """Datum axis — dashed centerline with endpoint ticks."""
    s = cell * SS
    lw = stroke_width(cell)
    x0, y0, x1, y1 = s*0.50, s*0.82, s*0.50, s*0.14
    _dash(d, x0, y0, x1, y1, p.accent, lw)
    tick = s * 0.08
    for y in (y0, y1):
        d.line([(x0 - tick, y), (x0 + tick, y)], fill=p.stroke, width=lw)
    arrow(d, x0, y1 + s*0.04, x0, y1, p.accent, lw)

@icon("point")
def draw_point(d, cell, p):
    """A filled dot with crosshair."""
    s = cell * SS
    lw = stroke_width(cell)
    cx, cy = s/2, s/2
    r = s * 0.08
    filled_circle(d, cx, cy, r, p.accent)
    cross = s * 0.22
    d.line([(cx-cross, cy), (cx+cross, cy)], fill=p.stroke, width=lw)
    d.line([(cx, cy-cross), (cx, cy+cross)], fill=p.stroke, width=lw)

@icon("csys")
def draw_csys(d, cell, p):
    """RGB XYZ triad arrows."""
    s = cell * SS
    lw = stroke_width(cell)
    ox, oy = s*0.35, s*0.65
    arrow(d, ox, oy, ox + s*0.45, oy, p.err, lw)        # X red
    arrow(d, ox, oy, ox, oy - s*0.45, p.ok, lw)         # Y green
    arrow(d, ox, oy, ox - s*0.22, oy + s*0.22, p.accent, lw)  # Z accent

@icon("origin")
def draw_origin(d, cell, p):
    """Part origin: three RGB axis arrows fanning from one point."""
    s = cell * SS
    lw = int(stroke_width(cell) * 1.35)
    ox, oy = s * 0.40, s * 0.60          # origin low-left so the axes read up/right
    L = s * 0.34
    RED, GRN, BLU = (229, 72, 72), (72, 190, 110), (86, 132, 240)
    arrow(d, ox, oy, ox - L * 0.60, oy + L * 0.60, BLU, lw)   # +Z (iso, drawn first/behind)
    arrow(d, ox, oy, ox + L, oy, RED, lw)                     # +X (right)
    arrow(d, ox, oy, ox, oy - L, GRN, lw)                     # +Y (up)
    filled_circle(d, ox, oy, s * 0.058, p.stroke)
    filled_circle(d, ox, oy, s * 0.028, p.accent)

@icon("equations")
def draw_equations(d, cell, p):
    """Global variables / equations: a bold Σ with an equals sign."""
    s = cell * SS
    lw = int(stroke_width(cell) * 1.7)
    # Bold sigma (zig-zag) on the left-center — deep middle vertex so it reads as Σ, not "["
    top, bot = s * 0.24, s * 0.76
    xr, xl = s * 0.50, s * 0.18
    xm, ym = s * 0.44, s * 0.50
    polyline(d, [(xr, top), (xl, top), (xm, ym), (xl, bot), (xr, bot)], p.accent, lw)
    # Equals sign to the right (two even bars)
    ex0, ex1 = s * 0.60, s * 0.82
    d.line([(ex0, s * 0.42), (ex1, s * 0.42)], fill=p.stroke, width=lw)
    d.line([(ex0, s * 0.58), (ex1, s * 0.58)], fill=p.stroke, width=lw)

@icon("material")
def draw_material(d, cell, p):
    """Glossy material swatch sphere (the screenshot preset icons supersede it)."""
    s = cell * SS
    lw = stroke_width(cell)
    cx, cy, r = s * 0.50, s * 0.50, s * 0.32
    # NOTE: this pipeline's ImageDraw REPLACES pixels (no alpha blend), so translucent shapes
    # must NOT be stacked — a later fill overwrites, it does not composite. Draw one solid body.
    filled_circle(d, cx, cy, r, _rgba(p.accent, 235))
    circle(d, cx, cy, r, outline=p.stroke, lw=lw)
    # Specular highlight (opaque white) reads as gloss.
    filled_circle(d, cx - r * 0.33, cy - r * 0.35, r * 0.19, (255, 255, 255, 235))
    filled_circle(d, cx - r * 0.29, cy - r * 0.31, r * 0.08, (255, 255, 255, 255))
    # Bottom rim shading arc for a rounded, seated read.
    d.arc([cx - r, cy - r, cx + r, cy + r], start=25, end=150,
          fill=_rgba(p.stroke, 150), width=max(1, lw // 2))

@icon("mates_folder")
def draw_mates_folder(d, cell, p):
    """Folder with coincident mate constraint glyph."""
    s = cell * SS
    lw = stroke_width(cell)
    d.rounded_rectangle([s*0.1, s*0.3, s*0.9, s*0.82], radius=int(s*0.05),
                         outline=p.stroke, width=lw)
    d.rectangle([s*0.1, s*0.25, s*0.45, s*0.35], outline=p.stroke, width=lw)
    # two facing faces + alignment arrows
    d.line([(s*0.30, s*0.50), (s*0.30, s*0.72)], fill=p.stroke, width=lw)
    d.line([(s*0.70, s*0.50), (s*0.70, s*0.72)], fill=p.stroke, width=lw)
    arrow(d, s*0.38, s*0.61, s*0.46, s*0.61, p.accent, lw)
    arrow(d, s*0.62, s*0.61, s*0.54, s*0.61, p.accent, lw)
    filled_circle(d, s*0.50, s*0.61, lw*0.9, p.accent)

@icon("body_folder")
def draw_body_folder(d, cell, p):
    """Folder with iso solid body inside."""
    s = cell * SS
    lw = stroke_width(cell)
    d.rounded_rectangle([s*0.1, s*0.3, s*0.9, s*0.82], radius=int(s*0.05),
                         outline=p.stroke, width=lw)
    d.rectangle([s*0.1, s*0.25, s*0.45, s*0.35], outline=p.stroke, width=lw)
    shaded_prism(d, s, s*0.32, s*0.46, s*0.30, s*0.22, s*0.08, s*0.06, p, accent=True)

@icon("history")
def draw_history(d, cell, p):
    """Clock face wrapped in a counter-clockwise arrow — the universal "history" glyph.

    Replaces a vertical node timeline whose half-width, alpha-120 spine and 1 px
    nodes collapsed into a dotted hairline at 14-20 px. Everything here is a
    full-opacity 2 px stroke on one bold ring, so it survives the @20 downsample:
    the ring runs clockwise from 9 o'clock round to ~8 o'clock, leaving a gap at
    lower-left where a solid arrowhead points DOWN (counter-clockwise travel);
    two clock hands (12 and ~4 o'clock) sit inside."""
    s = cell * SS
    lw = stroke_width(cell)
    cx, cy, r = s * 0.54, s * 0.52, s * 0.33
    arc_segment(d, cx, cy, r, 180, 360, p.stroke, lw)
    arc_segment(d, cx, cy, r, 0, 128, p.stroke, lw)
    # Arrowhead on the ring's 9 o'clock end, pointing along the CCW tangent (down).
    ax, ah, aw = cx - r, s * 0.17, s * 0.14
    d.polygon([(ax - aw, cy - s * 0.02), (ax + aw, cy - s * 0.02), (ax, cy - s * 0.02 + ah)],
              fill=p.accent)
    # Hands: minute to 12, hour toward ~4 o'clock, meeting on a centre hub.
    d.line([(cx, cy), (cx, cy - r * 0.58)], fill=p.stroke, width=lw)
    d.line([(cx, cy), (cx + r * 0.42, cy + r * 0.30)], fill=p.stroke, width=lw)
    filled_circle(d, cx, cy, lw * 0.6, p.stroke)

@icon("rollback")
def draw_rollback(d, cell, p):
    """Left-pointing curved arrow."""
    s = cell * SS
    lw = stroke_width(cell)
    cx, cy = s/2, s/2
    r = s * 0.32
    arc_segment(d, cx, cy, r, 180, 360, p.accent, lw)
    arrow(d, cx - r, cy, cx - r - s*0.12, cy - s*0.15, p.accent, lw)

@icon("error_badge")
def draw_error_badge(d, cell, p):
    s = cell * SS
    lw = stroke_width(cell)
    cx, cy = s/2, s/2
    r = s * 0.32
    filled_circle(d, cx, cy, r, p.err)
    x_sign(d, cx, cy, r*0.5, (255,255,255), lw)

@icon("warning_badge")
def draw_warning_badge(d, cell, p):
    s = cell * SS
    lw = stroke_width(cell)
    cx, cy = s/2, s/2
    r = s * 0.35
    pts = [(cx, cy - r), (cx + r*0.87, cy + r*0.5), (cx - r*0.87, cy + r*0.5)]
    d.polygon(pts, fill=p.warn)
    # '!' bar and dot
    d.line([(cx, cy - r*0.35), (cx, cy + r*0.1)], fill=(255,255,255), width=lw)
    filled_circle(d, cx, cy + r*0.32, lw*0.8, (255,255,255))

@icon("ok_badge")
def draw_ok_badge(d, cell, p):
    s = cell * SS
    lw = stroke_width(cell)
    cx, cy = s/2, s/2
    r = s * 0.32
    filled_circle(d, cx, cy, r, p.ok)
    check_mark(d, cx, cy, r, (255,255,255), lw)

@icon("suppressed")
def draw_suppressed(d, cell, p):
    """Grey strikethrough rectangle."""
    s = cell * SS
    lw = stroke_width(cell)
    d.rounded_rectangle([s*0.15, s*0.25, s*0.85, s*0.75],
                         radius=int(s*0.06), outline=p.stroke, width=lw)
    # diagonal strikethrough
    d.line([(s*0.15, s*0.25), (s*0.85, s*0.75)], fill=p.warn, width=lw)

@icon("box_feature")
def draw_box_feature(d, cell, p):
    s = cell * SS
    shaded_prism(d, s, s*0.19, s*0.36, s*0.48, s*0.36, s*0.17, s*0.13, p, accent=True)

@icon("cylinder_feature")
def draw_cylinder_feature(d, cell, p):
    s = cell * SS
    shaded_cylinder(d, s, s*0.50, s*0.54, s*0.48, s*0.58, p, accent=True)

@icon("sphere_feature")
def draw_sphere_feature(d, cell, p):
    s = cell * SS
    lw = stroke_width(cell)
    cx, cy, r = s*0.50, s*0.52, s*0.31
    d.ellipse([cx-r, cy-r, cx+r, cy+r], fill=_rgba(p.accent, 54), outline=p.stroke, width=lw)
    d.arc([cx-r*0.70, cy-r, cx+r*0.70, cy+r], start=90, end=270, fill=_rgba(p.stroke, 130), width=lw)
    d.arc([cx-r*0.70, cy-r, cx+r*0.70, cy+r], start=270, end=90, fill=p.accent, width=lw)
    d.arc([cx-r, cy-r*0.42, cx+r, cy+r*0.42], start=0, end=360, fill=_rgba(p.stroke, 120), width=lw)
    filled_circle(d, cx-r*0.35, cy-r*0.35, lw, _rgba((255, 255, 255), 155))

@icon("cone_feature")
def draw_cone_feature(d, cell, p):
    s = cell * SS
    lw = stroke_width(cell)
    apex = (s*0.50, s*0.16)
    left = (s*0.26, s*0.72)
    right = (s*0.74, s*0.72)
    d.polygon([apex, right, left], fill=_rgba(p.accent, 45))
    d.line([apex, right], fill=p.stroke, width=lw)
    d.line([apex, left], fill=p.stroke, width=lw)
    d.ellipse([s*0.26, s*0.62, s*0.74, s*0.82], fill=_rgba(p.stroke, 28), outline=p.accent, width=lw)

@icon("torus_feature")
def draw_torus_feature(d, cell, p):
    s = cell * SS
    lw = stroke_width(cell)
    d.ellipse([s*0.15, s*0.28, s*0.85, s*0.72], fill=_rgba(p.accent, 45), outline=p.stroke, width=lw*2)
    d.ellipse([s*0.34, s*0.39, s*0.66, s*0.61], fill=(0, 0, 0, 0), outline=p.accent, width=lw*2)
    d.arc([s*0.18, s*0.30, s*0.82, s*0.70], start=15, end=165, fill=_rgba((255,255,255), 130), width=lw)

# ---- Features tab ----

@icon("extrude_boss")
def draw_extrude_boss(d, cell, p):
    """Mini preview: sketched profile pulled into a solid boss."""
    s = cell * SS
    lw = stroke_width(cell)
    shaded_prism(d, s, s*0.24, s*0.40, s*0.42, s*0.33, s*0.16, s*0.13, p, accent=True)
    d.rounded_rectangle([s*0.20, s*0.70, s*0.62, s*0.84], radius=int(s*0.035),
                         outline=p.stroke, width=lw)
    arrow(d, s*0.43, s*0.67, s*0.43, s*0.18, p.accent, lw)
    plus_sign(d, s*0.74, s*0.26, s*0.08, p.accent, lw)

@icon("extrude_cut")
def draw_extrude_cut(d, cell, p):
    """Mini preview: red cut profile passing through a solid block."""
    s = cell * SS
    lw = stroke_width(cell)
    shaded_prism(d, s, s*0.18, s*0.38, s*0.50, s*0.36, s*0.16, s*0.12, p)
    d.rounded_rectangle([s*0.42, s*0.30, s*0.62, s*0.62], radius=int(s*0.04),
                         outline=p.err, width=lw)
    d.line([(s*0.52, s*0.18), (s*0.52, s*0.78)], fill=p.err, width=lw)
    arrow(d, s*0.52, s*0.20, s*0.52, s*0.78, p.err, lw)
    x_sign(d, s*0.77, s*0.28, s*0.07, p.err, lw)

@icon("revolve_boss")
def draw_revolve_boss(d, cell, p):
    """Lathe-style revolved boss preview."""
    s = cell * SS
    lw = stroke_width(cell)
    shaded_cylinder(d, s, s*0.55, s*0.54, s*0.42, s*0.55, p, accent=True)
    d.line([(s*0.27, s*0.14), (s*0.27, s*0.86)], fill=p.accent, width=lw)
    polyline(d, [(s*0.31, s*0.34), (s*0.46, s*0.34), (s*0.46, s*0.70), (s*0.31, s*0.70)],
             p.stroke, lw)
    arc_segment(d, s*0.27, s*0.52, s*0.32, 275, 85, p.accent, lw)

@icon("revolve_cut")
def draw_revolve_cut(d, cell, p):
    s = cell * SS
    lw = stroke_width(cell)
    shaded_cylinder(d, s, s*0.54, s*0.54, s*0.43, s*0.55, p)
    d.line([(s*0.28, s*0.14), (s*0.28, s*0.86)], fill=p.accent, width=lw)
    d.polygon([(s*0.36, s*0.35), (s*0.54, s*0.44), (s*0.54, s*0.64), (s*0.36, s*0.73)],
              fill=_rgba(p.err, 28))
    d.line([(s*0.36, s*0.35), (s*0.54, s*0.44), (s*0.54, s*0.64), (s*0.36, s*0.73),
            (s*0.36, s*0.35)], fill=p.err, width=lw)
    arc_segment(d, s*0.28, s*0.52, s*0.32, 275, 85, p.err, lw)

@icon("sweep")
def draw_sweep(d, cell, p):
    """Shaded tube swept along a curved path."""
    s = cell * SS
    lw = stroke_width(cell)
    pts = []
    p0, p1, p2, p3 = (s*0.17, s*0.66), (s*0.30, s*0.18), (s*0.73, s*0.18), (s*0.83, s*0.58)
    for i in range(28):
        t = i / 27.0
        mt = 1.0 - t
        x = mt**3*p0[0] + 3*mt*mt*t*p1[0] + 3*mt*t*t*p2[0] + t**3*p3[0]
        y = mt**3*p0[1] + 3*mt*mt*t*p1[1] + 3*mt*t*t*p2[1] + t**3*p3[1]
        pts.append((x, y))
    polyline(d, pts, _rgba(p.stroke, 88), lw*6)
    polyline(d, pts, _rgba(p.accent, 120), lw*5)
    polyline(d, pts, p.accent, lw*3)
    polyline(d, [(x, y - s*0.055) for x, y in pts[5:22]], _rgba((255,255,255), 105), max(1, lw))
    d.ellipse([s*0.08, s*0.55, s*0.31, s*0.77], fill=_rgba(p.stroke, 46),
              outline=p.stroke, width=lw)
    d.ellipse([s*0.70, s*0.47, s*0.93, s*0.69], fill=_rgba(p.accent, 54),
              outline=p.accent, width=lw)
    circle(d, s*0.195, s*0.66, s*0.048, fill=p.stroke)
    circle(d, s*0.815, s*0.58, s*0.048, fill=p.accent)

@icon("sweep_cut")
def draw_sweep_cut(d, cell, p):
    s = cell * SS
    lw = stroke_width(cell)
    shaded_prism(d, s, s*0.18, s*0.42, s*0.47, s*0.30, s*0.15, s*0.10, p)
    pts = []
    p0, p1, p2, p3 = (s*0.15, s*0.30), (s*0.31, s*0.13), (s*0.68, s*0.17), (s*0.82, s*0.42)
    for i in range(24):
        t = i / 23.0
        mt = 1.0 - t
        x = mt**3*p0[0] + 3*mt*mt*t*p1[0] + 3*mt*t*t*p2[0] + t**3*p3[0]
        y = mt**3*p0[1] + 3*mt*mt*t*p1[1] + 3*mt*t*t*p2[1] + t**3*p3[1]
        pts.append((x, y))
    polyline(d, pts, _rgba(p.err, 78), lw*5)
    polyline(d, pts, p.err, lw*3)
    d.ellipse([s*0.07, s*0.22, s*0.27, s*0.39], fill=_rgba(p.err, 28),
              outline=p.err, width=lw)
    d.ellipse([s*0.72, s*0.34, s*0.92, s*0.51], fill=_rgba(p.err, 28),
              outline=p.err, width=lw)
    x_sign(d, s*0.79, s*0.25, s*0.065, p.err, lw)

@icon("loft")
def draw_loft(d, cell, p):
    """Shaded lofted body between a round and rectangular profile."""
    s = cell * SS
    lw = stroke_width(cell)
    front = [s*0.10, s*0.38, s*0.43, s*0.75]
    rear = [s*0.55, s*0.22, s*0.92, s*0.70]
    # silhouette and shaded loft faces
    d.polygon([(front[0]+s*0.14, front[1]), (rear[0], rear[1]),
               (rear[2], rear[1]+s*0.05), (front[2], front[1]+s*0.09),
               (front[2], front[3]-s*0.08), (rear[2], rear[3]-s*0.04),
               (rear[0], rear[3]), (front[0]+s*0.12, front[3])],
              fill=_rgba(p.stroke, 44))
    d.polygon([(front[0]+s*0.14, front[1]), (rear[0], rear[1]),
               (rear[2], rear[1]+s*0.05), (front[2], front[1]+s*0.09)],
              fill=_rgba(p.accent, 72))
    d.polygon([(front[2], front[1]+s*0.09), (rear[2], rear[1]+s*0.05),
               (rear[2], rear[3]-s*0.04), (front[2], front[3]-s*0.08)],
              fill=_rgba(p.accent, 44))
    d.polygon([(front[0]+s*0.12, front[3]), (rear[0], rear[3]),
               (rear[2], rear[3]-s*0.04), (front[2], front[3]-s*0.08)],
              fill=_rgba(p.stroke, 58))
    # profile rims
    d.rounded_rectangle(rear, radius=int(s*0.035), outline=p.accent, width=lw)
    d.ellipse(front, fill=_rgba(p.stroke, 24), outline=p.stroke, width=lw)
    d.arc(front, start=286, end=74, fill=p.accent, width=lw)
    # guide edges stay sparse so the icon reads at 20 px too
    d.line([(front[0]+s*0.14, front[1]), (rear[0], rear[1])], fill=p.accent, width=lw)
    d.line([(front[2], front[1]+s*0.09), (rear[2], rear[1]+s*0.05)], fill=p.accent, width=lw)
    d.line([(front[2], front[3]-s*0.08), (rear[2], rear[3]-s*0.04)], fill=p.stroke, width=lw)
    d.line([(front[0]+s*0.12, front[3]), (rear[0], rear[3])], fill=p.stroke, width=lw)

@icon("loft_cut")
def draw_loft_cut(d, cell, p):
    s = cell * SS
    lw = stroke_width(cell)
    shaded_prism(d, s, s*0.16, s*0.42, s*0.50, s*0.31, s*0.15, s*0.10, p)
    front = [s*0.21, s*0.28, s*0.49, s*0.58]
    rear = [s*0.54, s*0.47, s*0.84, s*0.73]
    d.polygon([(front[0]+s*0.11, front[1]), (rear[0], rear[1]),
               (rear[2], rear[1]+s*0.03), (front[2], front[1]+s*0.08),
               (front[2], front[3]-s*0.06), (rear[2], rear[3]-s*0.03),
               (rear[0], rear[3]), (front[0]+s*0.09, front[3])],
              fill=_rgba(p.err, 42))
    d.rounded_rectangle(rear, radius=int(s*0.025), outline=p.err, width=lw)
    d.ellipse(front, outline=p.err, width=lw)
    d.line([(front[0]+s*0.11, front[1]), (rear[0], rear[1])], fill=p.err, width=lw)
    d.line([(front[2], front[1]+s*0.08), (rear[2], rear[1]+s*0.03)], fill=p.err, width=lw)
    d.line([(front[0]+s*0.09, front[3]), (rear[0], rear[3])], fill=p.err, width=lw)
    x_sign(d, s*0.78, s*0.27, s*0.07, p.err, lw)

@icon("boundary")
def draw_boundary(d, cell, p):
    """Boundary surface patch with U/V guide curves."""
    s = cell * SS
    lw = stroke_width(cell)
    patch = [(s*0.15, s*0.68), (s*0.36, s*0.25), (s*0.82, s*0.32), (s*0.68, s*0.78)]
    d.polygon(patch, fill=_rgba(p.accent, 34))
    d.line(patch + [patch[0]], fill=p.stroke, width=lw)
    for t in [0.33, 0.56, 0.76]:
        d.line([(s*(0.15 + (0.36-0.15)*t), s*(0.68 + (0.25-0.68)*t)),
                (s*(0.68 + (0.82-0.68)*t), s*(0.78 + (0.32-0.78)*t))],
               fill=_rgba(p.accent, 145), width=lw)
        d.line([(s*(0.15 + (0.68-0.15)*t), s*(0.68 + (0.78-0.68)*t)),
                (s*(0.36 + (0.82-0.36)*t), s*(0.25 + (0.32-0.25)*t))],
               fill=_rgba(p.stroke, 135), width=max(1, lw//2))

@icon("fillet")
def draw_fillet(d, cell, p):
    """Part corner with highlighted rounded edge."""
    s = cell * SS
    lw = stroke_width(cell)
    shaded_prism(d, s, s*0.20, s*0.38, s*0.48, s*0.34, s*0.15, s*0.11, p)
    d.arc([s*0.45, s*0.31, s*0.78, s*0.63], start=265, end=360, fill=p.accent, width=lw*2)
    d.line([(s*0.68, s*0.40), (s*0.83, s*0.29)], fill=p.accent, width=lw)

@icon("chamfer")
def draw_chamfer(d, cell, p):
    """Part corner with highlighted beveled face."""
    s = cell * SS
    lw = stroke_width(cell)
    shaded_prism(d, s, s*0.19, s*0.38, s*0.48, s*0.34, s*0.15, s*0.11, p)
    face = [(s*0.54, s*0.31), (s*0.80, s*0.37), (s*0.66, s*0.52), (s*0.45, s*0.46)]
    d.polygon(face, fill=_rgba(p.accent, 90))
    d.line(face + [face[0]], fill=p.accent, width=lw)

@icon("shell")
def draw_shell(d, cell, p):
    """Hollow box with wall thickness."""
    s = cell * SS
    lw = stroke_width(cell)
    shaded_prism(d, s, s*0.18, s*0.37, s*0.50, s*0.36, s*0.15, s*0.11, p)
    d.rounded_rectangle([s*0.32, s*0.43, s*0.58, s*0.64], radius=int(s*0.035),
                         fill=(0, 0, 0, 0), outline=p.accent, width=lw*2)
    d.line([(s*0.58, s*0.43), (s*0.72, s*0.33)], fill=p.accent, width=lw)
    d.line([(s*0.58, s*0.64), (s*0.72, s*0.54)], fill=p.accent, width=lw)

@icon("draft")
def draw_draft(d, cell, p):
    """Shaded draft preview with tapered side face."""
    s = cell * SS
    lw = stroke_width(cell)
    base = [(s*0.25, s*0.76), (s*0.78, s*0.70), (s*0.66, s*0.29), (s*0.38, s*0.22)]
    d.polygon(base, fill=_rgba(p.stroke, 30))
    d.line(base + [base[0]], fill=p.stroke, width=lw)
    face = [(s*0.38, s*0.22), (s*0.66, s*0.29), (s*0.78, s*0.70), (s*0.54, s*0.62)]
    d.polygon(face, fill=_rgba(p.accent, 70))
    d.line(face + [face[0]], fill=p.accent, width=lw)
    arc_segment(d, s*0.30, s*0.75, s*0.23, 285, 340, p.warn, lw)
    d.line([(s*0.30, s*0.75), (s*0.54, s*0.62)], fill=p.warn, width=lw)

@icon("rib")
def draw_rib(d, cell, p):
    """Support rib grown from a base plate."""
    s = cell * SS
    lw = stroke_width(cell)
    shaded_prism(d, s, s*0.16, s*0.62, s*0.56, s*0.13, s*0.13, s*0.08, p)
    rib = [(s*0.36, s*0.62), (s*0.58, s*0.62), (s*0.47, s*0.23)]
    d.polygon(rib, fill=_rgba(p.accent, 78))
    d.line(rib + [rib[0]], fill=p.accent, width=lw)
    d.line([(s*0.47, s*0.23), (s*0.58, s*0.62)], fill=p.stroke, width=max(1, lw//2))

@icon("hole_wizard")
def draw_hole_wizard(d, cell, p):
    """Solid plate with a drilled counterbore."""
    s = cell * SS
    lw = stroke_width(cell)
    shaded_prism(d, s, s*0.18, s*0.43, s*0.52, s*0.28, s*0.15, s*0.10, p)
    d.ellipse([s*0.36, s*0.33, s*0.58, s*0.47], fill=(0, 0, 0, 0), outline=p.accent, width=lw*2)
    d.ellipse([s*0.42, s*0.37, s*0.52, s*0.43], fill=_rgba(p.stroke, 90), outline=p.stroke, width=lw)
    arrow(d, s*0.47, s*0.18, s*0.47, s*0.34, p.accent, lw)

@icon("linear_pattern")
def draw_linear_pattern(d, cell, p):
    """Three patterned mini bodies along a direction arrow."""
    s = cell * SS
    lw = stroke_width(cell)
    for i in range(3):
        x = s*(0.14 + i*0.25)
        shaded_prism(d, s, x, s*0.45, s*0.13, s*0.12, s*0.05, s*0.04, p, accent=(i == 0))
    arrow(d, s*0.18, s*0.78, s*0.82, s*0.78, p.accent, lw)
    d.line([(s*0.14, s*0.34), (s*0.76, s*0.34)], fill=_rgba(p.stroke, 105), width=max(1, lw//2))

@icon("circular_pattern")
def draw_circular_pattern(d, cell, p):
    """Bodies repeated around an axis."""
    s = cell * SS
    lw = stroke_width(cell)
    cx, cy = s/2, s/2
    ring_r = s * 0.30
    circle(d, cx, cy, ring_r, outline=_rgba(p.stroke, 130), lw=lw)
    for i in range(6):
        angle = math.radians(-90 + i * 60)
        dx = cx + ring_r * math.cos(angle)
        dy = cy + ring_r * math.sin(angle)
        d.rounded_rectangle([dx-s*0.045, dy-s*0.045, dx+s*0.045, dy+s*0.045],
                             radius=int(s*0.015), fill=_rgba(p.accent if i == 0 else p.stroke, 70),
                             outline=p.accent if i == 0 else p.stroke, width=lw)
    arrow(d, cx + ring_r*0.55, cy - ring_r*0.78, cx + ring_r*0.82, cy - ring_r*0.28, p.accent, lw)

@icon("mirror")
def draw_mirror(d, cell, p):
    """Mirrored solid feature across a reference plane."""
    s = cell * SS
    lw = stroke_width(cell)
    plane = [(s*0.48, s*0.12), (s*0.54, s*0.16), (s*0.54, s*0.88), (s*0.48, s*0.84)]
    d.polygon(plane, fill=_rgba(p.accent, 45))
    d.line(plane + [plane[0]], fill=p.accent, width=lw)
    shaded_prism(d, s, s*0.17, s*0.42, s*0.17, s*0.20, s*0.06, s*0.05, p)
    shaded_prism(d, s, s*0.63, s*0.42, s*0.17, s*0.20, s*0.06, s*0.05, p, accent=True)
    d.line([(s*0.39, s*0.52), (s*0.61, s*0.52)], fill=_rgba(p.stroke, 120), width=max(1, lw//2))

@icon("ref_plane")
def draw_ref_plane(d, cell, p):
    """Translucent datum plane with construction hatch (distinct from plane glyph)."""
    s = cell * SS
    lw = stroke_width(cell)
    pts = [(s*0.1, s*0.60), (s*0.45, s*0.22), (s*0.9, s*0.36), (s*0.55, s*0.74)]
    d.polygon(pts, fill=_rgba(p.accent, 35), outline=p.accent)
    d.line([pts[0], pts[1], pts[2], pts[3], pts[0]], fill=p.accent, width=lw)
    for t in (0.2, 0.4, 0.6, 0.8):
        d.line([(pts[0][0] + (pts[2][0]-pts[0][0])*t, pts[0][1] + (pts[2][1]-pts[0][1])*t),
                (pts[1][0] + (pts[3][0]-pts[1][0])*t, pts[1][1] + (pts[3][1]-pts[1][1])*t)],
               fill=_rgba(p.stroke, 95), width=max(1, lw//2))
    filled_circle(d, pts[0][0], pts[0][1], max(2, lw), p.stroke)

@icon("ref_axis")
def draw_ref_axis(d, cell, p):
    """Reference axis through two circular handles."""
    s = cell * SS
    lw = stroke_width(cell)
    x0, y0 = s*0.25, s*0.78
    x1, y1 = s*0.76, s*0.20
    for i in range(6):
        t0 = i / 6.0
        t1 = min(1.0, t0 + 0.10)
        d.line([(x0 + (x1-x0)*t0, y0 + (y1-y0)*t0),
                (x0 + (x1-x0)*t1, y0 + (y1-y0)*t1)],
               fill=p.accent, width=lw)
    circle(d, x0, y0, s*0.10, outline=p.stroke, lw=lw)
    circle(d, x1, y1, s*0.10, outline=p.stroke, lw=lw)
    arrow(d, s*0.38, s*0.64, s*0.61, s*0.38, p.accent, lw)

@icon("ref_point")
def draw_ref_point(d, cell, p):
    """Reference point with target crosshair."""
    s = cell * SS
    lw = stroke_width(cell)
    cx, cy = s/2, s/2
    r = s * 0.18
    pts = [(cx, cy-r), (cx+r, cy), (cx, cy+r), (cx-r, cy)]
    d.polygon(pts, fill=_rgba(p.accent, 58), outline=p.accent)
    d.line([pts[0], pts[1], pts[2], pts[3], pts[0]], fill=p.accent, width=lw)
    d.line([(cx-s*0.34, cy), (cx-s*0.23, cy)], fill=p.stroke, width=lw)
    d.line([(cx+s*0.23, cy), (cx+s*0.34, cy)], fill=p.stroke, width=lw)
    d.line([(cx, cy-s*0.34), (cx, cy-s*0.23)], fill=p.stroke, width=lw)
    d.line([(cx, cy+s*0.23), (cx, cy+s*0.34)], fill=p.stroke, width=lw)

@icon("ref_csys")
def draw_ref_csys(d, cell, p):
    """Coordinate-system triad with origin plate."""
    s = cell * SS
    lw = stroke_width(cell)
    ox, oy = s*0.34, s*0.68
    d.ellipse([ox-s*0.10, oy-s*0.05, ox+s*0.10, oy+s*0.05], fill=_rgba(p.stroke, 35),
              outline=p.stroke, width=lw)
    arrow(d, ox, oy, s*0.82, oy, p.err, lw)
    arrow(d, ox, oy, ox, s*0.18, p.ok, lw)
    arrow(d, ox, oy, s*0.18, s*0.84, p.accent, lw)
    filled_circle(d, ox, oy, lw, p.stroke)

@icon("curve_helix")
def draw_curve_helix(d, cell, p):
    """Helix curve around a ghost cylinder."""
    s = cell * SS
    lw = stroke_width(cell)
    cx = s / 2
    d.ellipse([s*0.28, s*0.14, s*0.72, s*0.30], outline=_rgba(p.stroke, 95), width=lw)
    d.line([(s*0.28, s*0.22), (s*0.28, s*0.78)], fill=_rgba(p.stroke, 70), width=max(1, lw//2))
    d.line([(s*0.72, s*0.22), (s*0.72, s*0.78)], fill=_rgba(p.stroke, 70), width=max(1, lw//2))
    d.ellipse([s*0.28, s*0.70, s*0.72, s*0.86], outline=_rgba(p.stroke, 95), width=lw)
    for y in [0.22, 0.36, 0.50, 0.64]:
        d.arc([s*0.25, s*(y-0.08), s*0.75, s*(y+0.10)], start=0, end=180,
              fill=p.accent, width=lw)
        d.arc([s*0.25, s*(y+0.02), s*0.75, s*(y+0.20)], start=180, end=360,
              fill=p.accent, width=lw)

@icon("thread")
def draw_thread(d, cell, p):
    """Screw thread: a bold shaft whose two edges are SAWTOOTH thread teeth, with a flat
    head on top. Reads as a threaded fastener even at 20 px (dense hatching mushes out)."""
    s = cell * SS
    lw = stroke_width(cell)
    cx = s * 0.50
    xnl, xnr = s*0.46, s*0.54       # neck (valley) x, left/right
    xcl, xcr = s*0.34, s*0.66       # crest (peak) x, left/right — teeth stick well out
    ytop, ybot = s*0.30, s*0.80     # threaded shaft span (below the head)
    n = 4                            # thread teeth per side
    half = (ybot - ytop) / (2 * n)
    # Flat fastener head + a short neck so it reads "bolt", not "spring".
    d.rectangle([s*0.28, s*0.13, s*0.72, s*0.26], fill=_rgba(p.accent, 70), outline=p.stroke, width=lw)
    d.line([(cx, s*0.26), (cx, ytop)], fill=p.stroke, width=lw)
    # Build a SOLID serrated silhouette: a triangle-wave down the right edge, a pointed
    # lead-in tip, then a triangle-wave back up the left edge. Filled so the sawtooth thread
    # crests ARE the outline — legible even at 20 px.
    right = []
    for i in range(2 * n + 1):
        y = ytop + i * half
        right.append((xcr if i % 2 == 1 else xnr, y))
    left = []
    for i in range(2 * n, -1, -1):
        y = ytop + i * half
        left.append((xcl if i % 2 == 1 else xnl, y))
    poly = right + [(cx, ybot + s*0.08)] + left    # pointed tip between the two edges
    d.polygon(poly, fill=_rgba(p.accent, 95))
    d.line(poly + [poly[0]], fill=p.stroke, width=lw)

@icon("combine")
def draw_combine(d, cell, p):
    """Two bodies joined into one."""
    s = cell * SS
    lw = stroke_width(cell)
    shaded_prism(d, s, s*0.14, s*0.44, s*0.30, s*0.22, s*0.09, s*0.07, p)
    shaded_cylinder(d, s, s*0.65, s*0.56, s*0.27, s*0.34, p, accent=True)
    plus_sign(d, s*0.50, s*0.52, s*0.08, p.accent, lw)

@icon("intersect")
def draw_intersect(d, cell, p):
    """Two bodies with highlighted overlap."""
    s = cell * SS
    lw = stroke_width(cell)
    d.rounded_rectangle([s*0.18, s*0.34, s*0.61, s*0.70], radius=int(s*0.04),
                         fill=_rgba(p.stroke, 25), outline=p.stroke, width=lw)
    d.ellipse([s*0.40, s*0.25, s*0.83, s*0.75], fill=_rgba(p.accent, 25),
              outline=p.accent, width=lw)
    d.rounded_rectangle([s*0.40, s*0.34, s*0.61, s*0.70], radius=int(s*0.03),
                         fill=_rgba(p.accent, 90), outline=p.accent, width=lw)

@icon("subtract")
def draw_subtract(d, cell, p):
    """Body subtraction with red cutter."""
    s = cell * SS
    lw = stroke_width(cell)
    shaded_prism(d, s, s*0.15, s*0.42, s*0.45, s*0.28, s*0.12, s*0.09, p)
    d.ellipse([s*0.48, s*0.34, s*0.82, s*0.66], fill=_rgba(p.err, 26), outline=p.err, width=lw)
    d.line([(s*0.55, s*0.50), (s*0.75, s*0.50)], fill=p.err, width=lw*2)

@icon("split")
def draw_split(d, cell, p):
    """Solid body split by a cutting plane."""
    s = cell * SS
    lw = stroke_width(cell)
    shaded_prism(d, s, s*0.18, s*0.40, s*0.50, s*0.31, s*0.14, s*0.10, p)
    plane = [(s*0.49, s*0.23), (s*0.58, s*0.28), (s*0.58, s*0.82), (s*0.49, s*0.77)]
    d.polygon(plane, fill=_rgba(p.err, 58))
    d.line(plane + [plane[0]], fill=p.err, width=lw)

@icon("wrap")
def draw_wrap(d, cell, p):
    """Sketch contour wrapped onto a cylinder."""
    s = cell * SS
    lw = stroke_width(cell)
    shaded_cylinder(d, s, s*0.55, s*0.54, s*0.42, s*0.58, p)
    d.arc([s*0.32, s*0.30, s*0.78, s*0.58], start=8, end=180, fill=p.accent, width=lw*2)
    d.arc([s*0.32, s*0.43, s*0.78, s*0.71], start=180, end=352, fill=p.accent, width=lw*2)
    d.rounded_rectangle([s*0.10, s*0.30, s*0.36, s*0.50], radius=int(s*0.025),
                         fill=_rgba(p.stroke, 15), outline=p.stroke, width=lw)
    arrow(d, s*0.34, s*0.40, s*0.48, s*0.42, p.accent, lw)

# ---- Sketch tab ----
#
# Sketch-tool glyphs depict the RESULT/action of each tool: the produced
# geometry is the bold p.stroke shape; defining picks / centerlines / dimension
# arrows are p.accent cues. Tuned to read at 20 px and 32 px in both themes.

def _cr(points, samples=18):
    """Centripetal-ish Catmull-Rom spline through `points` -> dense polyline."""
    pts = list(points)
    if len(pts) < 3:
        return pts
    P = [pts[0]] + pts + [pts[-1]]
    out = []
    for i in range(1, len(P) - 2):
        p0, p1, p2, p3 = P[i - 1], P[i], P[i + 1], P[i + 2]
        for j in range(samples):
            t = j / samples
            t2 = t * t
            t3 = t2 * t
            x = 0.5 * ((2 * p1[0]) + (-p0[0] + p2[0]) * t +
                       (2 * p0[0] - 5 * p1[0] + 4 * p2[0] - p3[0]) * t2 +
                       (-p0[0] + 3 * p1[0] - 3 * p2[0] + p3[0]) * t3)
            y = 0.5 * ((2 * p1[1]) + (-p0[1] + p2[1]) * t +
                       (2 * p0[1] - 5 * p1[1] + 4 * p2[1] - p3[1]) * t2 +
                       (-p0[1] + 3 * p1[1] - 3 * p2[1] + p3[1]) * t3)
            out.append((x, y))
    out.append(pts[-1])
    return out


def _dash(d, x0, y0, x1, y1, color, lw, dash=None, gap=None):
    """Dashed straight line from (x0,y0) to (x1,y1)."""
    L = math.hypot(x1 - x0, y1 - y0)
    if L <= 0:
        return
    if dash is None:
        dash = lw * 1.8
    if gap is None:
        gap = lw * 1.4
    ux, uy = (x1 - x0) / L, (y1 - y0) / L
    t = 0.0
    while t < L:
        b = min(t + dash, L)
        d.line([(x0 + ux * t, y0 + uy * t), (x0 + ux * b, y0 + uy * b)], fill=color, width=lw)
        t += dash + gap


def _dashdot(d, x0, y0, x1, y1, color, lw):
    """Dash-dot construction centerline."""
    L = math.hypot(x1 - x0, y1 - y0)
    if L <= 0:
        return
    ux, uy = (x1 - x0) / L, (y1 - y0) / L
    dash = lw * 2.4
    dot = max(2, int(lw * 0.5))
    gap = lw * 1.3
    t = 0.0
    long_seg = True
    while t < L:
        seg = dash if long_seg else dot
        b = min(t + seg, L)
        d.line([(x0 + ux * t, y0 + uy * t), (x0 + ux * b, y0 + uy * b)], fill=color, width=lw)
        t = b + gap
        long_seg = not long_seg


@icon("sketch")
def draw_sketch(d, cell, p):
    """Pencil over a lightly gridded sketch plane."""
    s = cell * SS
    lw = stroke_width(cell)
    plane = [(s*0.13, s*0.67), (s*0.40, s*0.30), (s*0.86, s*0.40), (s*0.59, s*0.78)]
    d.polygon(plane, fill=_rgba(p.stroke, 18))
    d.line(plane + [plane[0]], fill=p.stroke, width=lw)
    for a, b in [((0.23,0.60),(0.50,0.24)), ((0.36,0.69),(0.63,0.33)),
                 ((0.27,0.44),(0.73,0.54)), ((0.19,0.56),(0.65,0.66))]:
        d.line([(s*a[0], s*a[1]), (s*b[0], s*b[1])], fill=_rgba(p.stroke, 65),
               width=max(1, lw//2))
    draw_pencil(d, s, p, s*0.21, s*0.79, s*0.82, s*0.19, s*0.050)

@icon("smart_dim")
def draw_smart_dim(d, cell, p):
    """Dimension across an edge: extension lines + dim line w/ arrows + value tag."""
    s = cell * SS
    lw = stroke_width(cell)
    # measured edge
    d.line([(s*0.18, s*0.76), (s*0.82, s*0.76)], fill=p.stroke, width=lw)
    # extension lines rising from each end
    for x in (0.18, 0.82):
        d.line([(s*x, s*0.74), (s*x, s*0.42)], fill=_rgba(p.stroke, 150), width=max(1, lw//2))
    # dimension line — arrowheads at both ends
    yd = s*0.47
    arrow(d, s*0.50, yd, s*0.20, yd, p.accent, lw)
    arrow(d, s*0.50, yd, s*0.80, yd, p.accent, lw)
    # value tag above the dimension line (digits downsample to a clean label)
    d.rounded_rectangle([s*0.34, s*0.15, s*0.66, s*0.39], radius=int(s*0.05),
                         fill=_rgba(p.accent, 36), outline=p.accent, width=max(1, lw//2))
    d.line([(s*0.44, s*0.21), (s*0.44, s*0.33)], fill=p.accent, width=max(1, lw//2))   # '1'
    d.line([(s*0.51, s*0.215), (s*0.58, s*0.215)], fill=p.accent, width=max(1, lw//2)) # '2'
    d.line([(s*0.58, s*0.215), (s*0.58, s*0.27)], fill=p.accent, width=max(1, lw//2))
    d.line([(s*0.58, s*0.27), (s*0.51, s*0.27)], fill=p.accent, width=max(1, lw//2))
    d.line([(s*0.51, s*0.27), (s*0.51, s*0.33)], fill=p.accent, width=max(1, lw//2))
    d.line([(s*0.51, s*0.33), (s*0.58, s*0.33)], fill=p.accent, width=max(1, lw//2))

@icon("line")
def draw_line(d, cell, p):
    """A drawn line segment with its two endpoints."""
    s = cell * SS
    lw = stroke_width(cell)
    a, b = (s*0.16, s*0.78), (s*0.84, s*0.22)
    d.line([a, b], fill=p.stroke, width=lw*2)
    for x, y in (a, b):
        filled_circle(d, x, y, lw*1.05, p.accent)

@icon("rect_corner")
def draw_rect_corner(d, cell, p):
    """Rectangle drawn corner-to-corner — two opposite corner handles."""
    s = cell * SS
    lw = stroke_width(cell)
    x0, y0, x1, y1 = s*0.18, s*0.28, s*0.82, s*0.72
    d.rectangle([x0, y0, x1, y1], outline=p.stroke, width=lw)
    filled_circle(d, x0, y1, lw*1.2, p.accent)   # first corner
    filled_circle(d, x1, y0, lw*1.2, p.accent)   # opposite corner

@icon("rect_center")
def draw_rect_center(d, cell, p):
    """Rectangle drawn from its center — center point + corner handle."""
    s = cell * SS
    lw = stroke_width(cell)
    x0, y0, x1, y1 = s*0.18, s*0.28, s*0.82, s*0.72
    d.rectangle([x0, y0, x1, y1], outline=p.stroke, width=lw)
    cx, cy = s*0.5, s*0.5
    d.line([(cx - s*0.10, cy), (cx + s*0.10, cy)], fill=_rgba(p.accent, 150), width=max(1, lw//2))
    d.line([(cx, cy - s*0.10), (cx, cy + s*0.10)], fill=_rgba(p.accent, 150), width=max(1, lw//2))
    filled_circle(d, cx, cy, lw*0.95, p.accent)   # center
    filled_circle(d, x1, y0, lw*1.1, p.accent)    # corner handle

@icon("circle")
def draw_circle(d, cell, p):
    """Circle by center + radius point on the rim."""
    s = cell * SS
    lw = stroke_width(cell)
    cx, cy, r = s*0.5, s*0.5, s*0.32
    circle(d, cx, cy, r, outline=p.stroke, lw=lw*2)
    rx = cx + r*math.cos(math.radians(-35))
    ry = cy + r*math.sin(math.radians(-35))
    d.line([(cx, cy), (rx, ry)], fill=_rgba(p.accent, 150), width=max(1, lw//2))
    filled_circle(d, cx, cy, lw*0.9, p.accent)   # center
    filled_circle(d, rx, ry, lw*0.95, p.accent)  # rim point

@icon("arc_3pt")
def draw_arc_3pt(d, cell, p):
    """Arc through three points (start, mid, end)."""
    s = cell * SS
    lw = stroke_width(cell)
    cx, cy, r = s*0.5, s*0.64, s*0.40
    a0, a1 = 205, 335
    arc_segment(d, cx, cy, r, a0, a1, p.stroke, lw*2)
    for ang in (a0, (a0 + a1) / 2, a1):
        x = cx + r*math.cos(math.radians(ang))
        y = cy + r*math.sin(math.radians(ang))
        filled_circle(d, x, y, lw*0.95, p.accent)

@icon("arc_center")
def draw_arc_center(d, cell, p):
    """Arc by center point + two radius endpoints."""
    s = cell * SS
    lw = stroke_width(cell)
    cx, cy, r = s*0.5, s*0.62, s*0.38
    a0, a1 = 205, 335
    arc_segment(d, cx, cy, r, a0, a1, p.stroke, lw*2)
    for ang in (a0, a1):
        x = cx + r*math.cos(math.radians(ang))
        y = cy + r*math.sin(math.radians(ang))
        d.line([(cx, cy), (x, y)], fill=_rgba(p.accent, 140), width=max(1, lw//2))
        filled_circle(d, x, y, lw*0.85, p.accent)
    filled_circle(d, cx, cy, lw*1.0, p.accent)   # center

@icon("arc_tangent")
def draw_arc_tangent(d, cell, p):
    """Arc starting tangent to an existing line at the shared endpoint."""
    s = cell * SS
    lw = stroke_width(cell)
    tx, ty = s*0.42, s*0.64
    d.line([(s*0.10, ty), (tx, ty)], fill=p.stroke, width=lw*2)   # base line
    r = s*0.32
    cx, cy = tx, ty - r                                            # center above tangent pt
    arc_segment(d, cx, cy, r, -30, 90, p.accent, lw*2)            # tangent arc up-right
    d.line([(tx, ty), (tx, ty - s*0.11)], fill=_rgba(p.stroke, 120), width=max(1, lw//2))
    filled_circle(d, tx, ty, lw*0.95, p.accent)                   # tangency point

@icon("polygon")
def draw_polygon(d, cell, p):
    """Regular hexagon by center + a vertex (radius)."""
    s = cell * SS
    lw = stroke_width(cell)
    cx, cy, r, n = s*0.5, s*0.5, s*0.36, 6
    pts = [(cx + r*math.cos(math.radians(-90 + i*360/n)),
            cy + r*math.sin(math.radians(-90 + i*360/n))) for i in range(n)]
    d.line(pts + [pts[0]], fill=p.stroke, width=lw)
    d.line([(cx, cy), pts[0]], fill=_rgba(p.accent, 150), width=max(1, lw//2))
    filled_circle(d, cx, cy, lw*0.9, p.accent)
    filled_circle(d, pts[0][0], pts[0][1], lw*0.9, p.accent)

@icon("slot")
def draw_slot(d, cell, p):
    """Straight slot (capsule) with centerline and end centers."""
    s = cell * SS
    lw = stroke_width(cell)
    d.rounded_rectangle([s*0.14, s*0.34, s*0.86, s*0.66], radius=int(s*0.16),
                         outline=p.stroke, width=lw)
    d.line([(s*0.28, s*0.50), (s*0.72, s*0.50)], fill=_rgba(p.accent, 150), width=max(1, lw//2))
    filled_circle(d, s*0.28, s*0.50, lw*0.9, p.accent)
    filled_circle(d, s*0.72, s*0.50, lw*0.9, p.accent)

@icon("spline")
def draw_spline(d, cell, p):
    """Smooth spline through control points (control polygon shown)."""
    s = cell * SS
    lw = stroke_width(cell)
    ctrl = [(0.14, 0.60), (0.36, 0.24), (0.62, 0.76), (0.86, 0.40)]
    cpts = [(s*x, s*y) for x, y in ctrl]
    d.line(_cr(cpts), fill=p.stroke, width=lw*2, joint="curve")
    for i in range(len(cpts) - 1):
        _dash(d, cpts[i][0], cpts[i][1], cpts[i+1][0], cpts[i+1][1],
              _rgba(p.accent, 110), max(1, lw//3), dash=s*0.05, gap=s*0.035)
    for x, y in cpts:
        filled_circle(d, x, y, lw*0.85, p.accent)

@icon("ellipse")
def draw_ellipse(d, cell, p):
    """Ellipse by center + major/minor axis endpoints."""
    s = cell * SS
    lw = stroke_width(cell)
    x0, y0, x1, y1 = s*0.12, s*0.30, s*0.88, s*0.70
    d.ellipse([x0, y0, x1, y1], outline=p.stroke, width=lw*2)
    cx, cy = s*0.5, s*0.5
    d.line([(x0, cy), (x1, cy)], fill=_rgba(p.accent, 130), width=max(1, lw//2))
    d.line([(cx, y0), (cx, y1)], fill=_rgba(p.accent, 130), width=max(1, lw//2))
    filled_circle(d, cx, cy, lw*0.8, p.accent)
    filled_circle(d, x1, cy, lw*0.85, p.accent)   # major endpoint
    filled_circle(d, cx, y0, lw*0.85, p.accent)   # minor endpoint

@icon("point_ent")
def draw_point_ent(d, cell, p):
    """A sketch point marked with a crosshair."""
    s = cell * SS
    lw = stroke_width(cell)
    cx, cy = s*0.5, s*0.5
    for ang in (0, 90, 180, 270):
        c, sn = math.cos(math.radians(ang)), math.sin(math.radians(ang))
        d.line([(cx + s*0.17*c, cy + s*0.17*sn), (cx + s*0.31*c, cy + s*0.31*sn)],
               fill=_rgba(p.stroke, 150), width=max(1, lw//2))
    filled_circle(d, cx, cy, lw*1.6, p.accent)

@icon("text_ent")
def draw_text_ent(d, cell, p):
    """Sketch text — an 'A' glyph on a baseline."""
    s = cell * SS
    lw = stroke_width(cell)
    apex = (s*0.5, s*0.22)
    ll, lr = (s*0.30, s*0.72), (s*0.70, s*0.72)
    d.line([apex, ll], fill=p.stroke, width=lw*2)
    d.line([apex, lr], fill=p.stroke, width=lw*2)
    d.line([(s*0.385, s*0.52), (s*0.615, s*0.52)], fill=p.stroke, width=lw)   # crossbar
    d.line([(s*0.16, s*0.84), (s*0.84, s*0.84)], fill=_rgba(p.accent, 160), width=max(1, lw//2))

@icon("trim")
def draw_trim(d, cell, p):
    """Scissors trimming a line — the cut-off part removed (red dashed)."""
    s = cell * SS
    lw = stroke_width(cell)
    y = s*0.50
    cross = s*0.44                                                            # blades cross here
    d.line([(s*0.06, y), (cross, y)], fill=p.stroke, width=lw*2)             # kept
    _dash(d, s*0.60, y, s*0.95, y, p.err, lw, dash=s*0.06, gap=s*0.045)      # trimmed off (right)
    # scissors: finger loops left, blades crossing on the line at the cut
    loop_r = s*0.095
    lc1, lc2 = (s*0.20, y - s*0.17), (s*0.20, y + s*0.17)
    circle(d, lc1[0], lc1[1], loop_r, outline=p.accent, lw=lw)
    circle(d, lc2[0], lc2[1], loop_r, outline=p.accent, lw=lw)
    d.line([lc1, (s*0.58, y + s*0.10)], fill=p.accent, width=lw)             # blade
    d.line([lc2, (s*0.58, y - s*0.10)], fill=p.accent, width=lw)             # blade
    filled_circle(d, cross, y, lw*0.7, p.accent)                            # pivot / cut

@icon("extend")
def draw_extend(d, cell, p):
    """Entity extended (dashed) to meet a boundary wall."""
    s = cell * SS
    lw = stroke_width(cell)
    y = s*0.50
    wall = s*0.86
    d.line([(s*0.10, y), (s*0.44, y)], fill=p.stroke, width=lw*2)        # existing entity
    _dash(d, s*0.44, y, wall - s*0.05, y, p.accent, lw, dash=s*0.06, gap=s*0.04)
    arrow(d, wall - s*0.16, y, wall - s*0.03, y, p.accent, lw)           # reaching the wall
    d.line([(wall, s*0.16), (wall, s*0.84)], fill=p.stroke, width=lw*2)  # boundary wall

@icon("offset_ent")
def draw_offset_ent(d, cell, p):
    """Offset: a curve duplicated parallel at a constant distance."""
    s = cell * SS
    lw = stroke_width(cell)
    cx, cy = s*0.5, s*0.84
    arc_segment(d, cx, cy, s*0.50, 230, 310, p.stroke, lw*2)   # base curve
    arc_segment(d, cx, cy, s*0.34, 230, 310, p.accent, lw*2)   # parallel offset
    arrow(d, cx, cy - s*0.50, cx, cy - s*0.34, p.accent, lw)   # offset distance

@icon("convert_ent")
def draw_convert_ent(d, cell, p):
    """Convert entities: project a model edge onto the sketch as new geometry."""
    s = cell * SS
    lw = stroke_width(cell)
    # model edge (top, stroke) with its end vertices
    d.line([(s*0.16, s*0.26), (s*0.84, s*0.26)], fill=p.stroke, width=lw*2)
    filled_circle(d, s*0.16, s*0.26, lw*0.8, p.stroke)
    filled_circle(d, s*0.84, s*0.26, lw*0.8, p.stroke)
    # projection arrows down
    for x in (0.30, 0.70):
        arrow(d, s*x, s*0.36, s*x, s*0.58, _rgba(p.accent, 150), max(1, lw//2))
    # converted sketch entity (accent) on a dashed sketch plane
    d.line([(s*0.16, s*0.70), (s*0.84, s*0.70)], fill=p.accent, width=lw*2)
    filled_circle(d, s*0.16, s*0.70, lw*0.9, p.accent)
    filled_circle(d, s*0.84, s*0.70, lw*0.9, p.accent)
    _dash(d, s*0.10, s*0.82, s*0.90, s*0.82, _rgba(p.stroke, 95), max(1, lw//3),
          dash=s*0.05, gap=s*0.04)

@icon("mirror_ent")
def draw_mirror_ent(d, cell, p):
    """Mirror entities across a dash-dot centerline."""
    s = cell * SS
    lw = stroke_width(cell)
    _dashdot(d, s*0.5, s*0.12, s*0.5, s*0.88, p.accent, lw)
    left = [(s*0.18, s*0.30), (s*0.40, s*0.30), (s*0.40, s*0.45),
            (s*0.26, s*0.45), (s*0.26, s*0.70)]
    right = [(s - x, y) for (x, y) in left]
    d.line(left, fill=p.stroke, width=lw)
    d.line(right, fill=p.stroke, width=lw)

@icon("pattern_lin_ent")
def draw_pattern_lin_ent(d, cell, p):
    """Linear pattern: a seed entity repeated along a direction."""
    s = cell * SS
    lw = stroke_width(cell)
    for i, x in enumerate([0.22, 0.50, 0.78]):
        circle(d, s*x, s*0.40, s*0.09, outline=p.accent if i == 0 else p.stroke, lw=lw)
    arrow(d, s*0.16, s*0.74, s*0.84, s*0.74, p.accent, lw)

@icon("pattern_circ_ent")
def draw_pattern_circ_ent(d, cell, p):
    """Circular pattern: a seed entity repeated around a center."""
    s = cell * SS
    lw = stroke_width(cell)
    cx, cy = s*0.5, s*0.54
    ring_r = s*0.30
    circle(d, cx, cy, ring_r, outline=_rgba(p.stroke, 120), lw=max(1, lw//2))
    for i in range(5):
        ang = math.radians(-90 + i*72)
        dx = cx + ring_r*math.cos(ang)
        dy = cy + ring_r*math.sin(ang)
        circle(d, dx, dy, s*0.06, outline=p.accent if i == 0 else p.stroke, lw=lw)
    filled_circle(d, cx, cy, lw*0.7, p.accent)
    arrow(d, cx + ring_r*0.62, cy - ring_r*0.92, cx + ring_r*1.02, cy - ring_r*0.48, p.accent, lw)

@icon("move_ent")
def draw_move_ent(d, cell, p):
    """Move/copy entities: a shape translated to a new position (ghost)."""
    s = cell * SS
    lw = stroke_width(cell)
    src = [(s*0.14, s*0.66), (s*0.40, s*0.66), (s*0.27, s*0.40)]
    d.line(src + [src[0]], fill=p.stroke, width=lw)
    dx, dy = s*0.36, s*-0.04
    dst = [(x + dx, y + dy) for x, y in src]
    for i in range(len(dst)):
        a, b = dst[i], dst[(i + 1) % len(dst)]
        _dash(d, a[0], a[1], b[0], b[1], p.accent, max(1, lw//2), dash=s*0.05, gap=s*0.035)
    arrow(d, s*0.30, s*0.57, s*0.30 + dx, s*0.57 + dy, p.accent, lw)

@icon("construction")
def draw_construction(d, cell, p):
    """Construction geometry — a dash-dot centerline."""
    s = cell * SS
    lw = stroke_width(cell)
    _dashdot(d, s*0.14, s*0.78, s*0.86, s*0.22, p.accent, lw*2)
    filled_circle(d, s*0.14, s*0.78, lw*0.95, p.accent)
    filled_circle(d, s*0.86, s*0.22, lw*0.95, p.accent)

@icon("relations")
def draw_relations(d, cell, p):
    """Add a geometric relation linking two entities (constraint badge)."""
    s = cell * SS
    lw = stroke_width(cell)
    a, b = (s*0.24, s*0.5), (s*0.76, s*0.5)
    d.line([a, b], fill=_rgba(p.stroke, 150), width=max(1, lw//2))
    filled_circle(d, a[0], a[1], lw*1.2, p.stroke)
    filled_circle(d, b[0], b[1], lw*1.2, p.stroke)
    d.rounded_rectangle([s*0.40, s*0.34, s*0.60, s*0.66], radius=int(s*0.04),
                         fill=_rgba(p.accent, 40), outline=p.accent, width=lw)
    d.line([(s*0.45, s*0.45), (s*0.55, s*0.45)], fill=p.accent, width=max(1, lw//2))
    d.line([(s*0.45, s*0.55), (s*0.55, s*0.55)], fill=p.accent, width=max(1, lw//2))

@icon("fix")
def draw_fix(d, cell, p):
    """Fixed relation lock anchored to a sketch point."""
    s = cell * SS
    lw = stroke_width(cell)
    arc_segment(d, s*0.5, s*0.42, s*0.2, 180, 360, p.stroke, lw)
    d.rounded_rectangle([s*0.3, s*0.48, s*0.7, s*0.80], radius=int(s*0.05),
                         fill=_rgba(p.stroke, 18), outline=p.stroke, width=lw)
    filled_circle(d, s*0.5, s*0.64, lw, p.accent)
    d.line([(s*0.25, s*0.86), (s*0.75, s*0.86)], fill=p.accent, width=lw)

@icon("display_relations")
def draw_display_relations(d, cell, p):
    """Relation badge above a compact list of inspectable constraints."""
    s = cell * SS
    lw = stroke_width(cell)
    a, b = (s*0.20, s*0.32), (s*0.80, s*0.32)
    d.line([a, b], fill=_rgba(p.stroke, 150), width=max(1, lw//2))
    filled_circle(d, *a, lw*1.1, p.stroke)
    filled_circle(d, *b, lw*1.1, p.stroke)
    d.rounded_rectangle([s*0.40, s*0.17, s*0.60, s*0.47],
                        radius=int(s*0.04), fill=_rgba(p.accent, 40),
                        outline=p.accent, width=lw)
    for y in (0.62, 0.80):
        filled_circle(d, s*0.25, s*y, lw*0.65, p.accent)
        d.line([(s*0.40, s*y), (s*0.78, s*y)], fill=p.stroke, width=lw)

@icon("fully_define")
def draw_fully_define(d, cell, p):
    """Closed sketch profile with constrained vertices and an approval check."""
    s = cell * SS
    lw = stroke_width(cell)
    profile = [(s*0.19, s*0.70), (s*0.19, s*0.24),
               (s*0.52, s*0.24), (s*0.70, s*0.43),
               (s*0.70, s*0.70)]
    d.line(profile + [profile[0]], fill=p.stroke, width=lw, joint="curve")
    for x, y in profile:
        filled_circle(d, x, y, lw*0.8, p.accent)
    d.line([(s*0.48, s*0.70), (s*0.62, s*0.84), (s*0.86, s*0.56)],
           fill=p.accent, width=lw*2, joint="curve")

# ---- Assembly tab ----

@icon("insert_component")
def draw_insert_component(d, cell, p):
    s = cell * SS
    lw = stroke_width(cell)
    shaded_prism(d, s, s*0.22, s*0.48, s*0.38, s*0.25, s*0.12, s*0.08, p)
    arrow(d, s*0.50, s*0.12, s*0.50, s*0.42, p.accent, lw)
    plus_sign(d, s*0.76, s*0.30, s*0.08, p.accent, lw)

@icon("mate")
def draw_mate(d, cell, p):
    """Two assembly parts constrained face-to-face."""
    s = cell * SS
    lw = stroke_width(cell)
    shaded_prism(d, s, s*0.13, s*0.44, s*0.27, s*0.20, s*0.08, s*0.06, p)
    shaded_prism(d, s, s*0.58, s*0.44, s*0.27, s*0.20, s*0.08, s*0.06, p, accent=True)
    d.line([(s*0.42, s*0.52), (s*0.58, s*0.52)], fill=p.accent, width=lw*2)
    d.line([(s*0.46, s*0.43), (s*0.54, s*0.43)], fill=p.stroke, width=lw)
    d.line([(s*0.46, s*0.61), (s*0.54, s*0.61)], fill=p.stroke, width=lw)

@icon("move_component")
def draw_move_component(d, cell, p):
    s = cell * SS
    lw = stroke_width(cell)
    cx, cy = s/2, s/2
    shaded_prism(d, s, cx - s*0.18, cy - s*0.08, s*0.28, s*0.20, s*0.08, s*0.06, p)
    m = s * 0.34
    arrow(d, cx, cy, cx + m, cy, p.accent, lw)
    arrow(d, cx, cy, cx, cy - m, p.accent, lw)
    arrow(d, cx, cy, cx - m*0.55, cy + m*0.45, p.accent, lw)

@icon("rotate_component")
def draw_rotate_component(d, cell, p):
    s = cell * SS
    lw = stroke_width(cell)
    cx, cy = s/2, s/2
    shaded_prism(d, s, cx - s*0.18, cy - s*0.05, s*0.27, s*0.20, s*0.08, s*0.06, p)
    r = s*0.36
    arc_segment(d, cx, cy, r, 205, 510, p.accent, lw)
    arrow(d, cx - r, cy, cx - r - s*0.1, cy - s*0.12, p.accent, lw)

@icon("pattern_component")
def draw_pattern_component(d, cell, p):
    s = cell * SS
    lw = stroke_width(cell)
    for xi in range(2):
        for yi in range(2):
            x = s*(0.18 + xi*0.35)
            y = s*(0.25 + yi*0.35)
            shaded_prism(d, s, x, y, s*0.14, s*0.12, s*0.05, s*0.04,
                         p, accent=(xi == 0 and yi == 0))
    d.line([(s*0.28, s*0.33), (s*0.63, s*0.33)], fill=_rgba(p.accent, 120), width=max(1, lw//2))
    d.line([(s*0.28, s*0.33), (s*0.28, s*0.68)], fill=_rgba(p.accent, 120), width=max(1, lw//2))

@icon("smart_fasteners")
def draw_smart_fasteners(d, cell, p):
    """Bolt and washer fastener stack."""
    s = cell * SS
    lw = stroke_width(cell)
    cx, cy = s/2, s*0.42
    r = s * 0.20
    pts = [(cx + r*math.cos(math.radians(-90 + i*60)),
            cy + r*math.sin(math.radians(-90 + i*60))) for i in range(6)]
    d.polygon(pts, fill=_rgba(p.stroke, 25), outline=p.stroke)
    d.line(pts + [pts[0]], fill=p.stroke, width=lw)
    d.rectangle([cx-lw*1.5, cy+r, cx+lw*1.5, cy+r+s*0.30], fill=p.accent)
    for y in [0.64,0.70,0.76]:
        d.line([(cx-s*0.10, s*y), (cx+s*0.10, s*(y+0.04))], fill=p.stroke, width=max(1, lw//2))
    d.ellipse([cx-s*0.19, s*0.77, cx+s*0.19, s*0.88], outline=p.accent, width=lw)

@icon("exploded_view")
def draw_exploded_view(d, cell, p):
    """Assembly pieces exploded outward from a center."""
    s = cell * SS
    lw = stroke_width(cell)
    cx, cy = s/2, s/2
    for angle, r_inner, r_outer in [(320, 0.12, 0.37), (185, 0.12, 0.38), (85, 0.12, 0.36)]:
        a = math.radians(angle)
        x0 = cx + r_inner*s*math.cos(a)
        y0 = cy + r_inner*s*math.sin(a)
        x1 = cx + r_outer*s*math.cos(a)
        y1 = cy + r_outer*s*math.sin(a)
        d.line([(x0, y0), (x1, y1)], fill=p.accent, width=lw)
        shaded_prism(d, s, x1 - s*0.07, y1 - s*0.03, s*0.12, s*0.10, s*0.04, s*0.03, p)
    filled_circle(d, cx, cy, lw, p.accent)

@icon("bom")
def draw_bom(d, cell, p):
    """Bill of materials table with part callout."""
    s = cell * SS
    lw = stroke_width(cell)
    d.rounded_rectangle([s*0.12, s*0.18, s*0.88, s*0.82], radius=int(s*0.035),
                         fill=_rgba(p.stroke, 12), outline=p.stroke, width=lw)
    d.rectangle([s*0.12, s*0.18, s*0.88, s*0.34], fill=_rgba(p.accent, 50), outline=p.accent, width=lw)
    for i in range(3):
        y = s*(0.38 + i*0.14)
        d.line([(s*0.16, y), (s*0.84, y)], fill=_rgba(p.stroke, 120), width=max(1, lw//2))
        d.line([(s*0.35, y), (s*0.35, y + s*0.14)], fill=p.stroke, width=lw)
        filled_circle(d, s*0.23, y+s*0.07, max(1, lw//2), p.accent if i == 0 else p.stroke)

@icon("interference")
def draw_interference(d, cell, p):
    """Interfering assembly solids with red collision volume."""
    s = cell * SS
    lw = stroke_width(cell)
    shaded_prism(d, s, s*0.14, s*0.42, s*0.35, s*0.26, s*0.10, s*0.07, p)
    shaded_prism(d, s, s*0.43, s*0.35, s*0.35, s*0.26, s*0.10, s*0.07, p, accent=True)
    d.rounded_rectangle([s*0.42, s*0.43, s*0.56, s*0.67], radius=int(s*0.02),
                         fill=_rgba(p.err, 92), outline=p.err, width=lw)

# ---- Evaluate tab ----

@icon("measure")
def draw_measure(d, cell, p):
    """Caliper-style measurement tool."""
    s = cell * SS
    lw = stroke_width(cell)
    d.line([(s*0.18, s*0.70), (s*0.78, s*0.22)], fill=p.stroke, width=lw*2)
    d.line([(s*0.18, s*0.70), (s*0.30, s*0.84)], fill=p.accent, width=lw*2)
    d.line([(s*0.78, s*0.22), (s*0.88, s*0.36)], fill=p.accent, width=lw*2)
    for i in range(4):
        t = (i + 1) / 5
        px = s*0.18 + (s*0.60)*t
        py = s*0.70 - (s*0.48)*t
        d.line([(px-s*0.035, py-s*0.045), (px+s*0.035, py+s*0.045)], fill=p.accent, width=lw)

@icon("mass_props")
def draw_mass_props(d, cell, p):
    """Mass properties scale with shaded body."""
    s = cell * SS
    lw = stroke_width(cell)
    cx = s/2
    shaded_prism(d, s, s*0.40, s*0.18, s*0.20, s*0.12, s*0.06, s*0.04, p, accent=True)
    d.line([(cx, s*0.30), (cx, s*0.66)], fill=p.stroke, width=lw)
    d.line([(s*0.18, s*0.42), (s*0.82, s*0.42)], fill=p.stroke, width=lw)
    for x in [s*0.22, s*0.78]:
        d.line([(x, s*0.42), (x, s*0.62)], fill=p.stroke, width=lw)
        d.ellipse([x - s*0.13, s*0.60, x + s*0.13, s*0.73], fill=_rgba(p.accent, 24),
                  outline=p.accent, width=lw)

@icon("section_props")
def draw_section_props(d, cell, p):
    """Section properties with centroid axes."""
    s = cell * SS
    lw = stroke_width(cell)
    d.polygon([(s*0.22,s*0.22),(s*0.78,s*0.22),(s*0.78,s*0.36),(s*0.57,s*0.36),
               (s*0.57,s*0.64),(s*0.78,s*0.64),(s*0.78,s*0.78),(s*0.22,s*0.78),
               (s*0.22,s*0.64),(s*0.43,s*0.64),(s*0.43,s*0.36),(s*0.22,s*0.36)],
              fill=_rgba(p.stroke, 22))
    pts = [(s*0.22,s*0.22),(s*0.78,s*0.22),(s*0.78,s*0.36),(s*0.57,s*0.36),
           (s*0.57,s*0.64),(s*0.78,s*0.64),(s*0.78,s*0.78),(s*0.22,s*0.78),
           (s*0.22,s*0.64),(s*0.43,s*0.64),(s*0.43,s*0.36),(s*0.22,s*0.36)]
    d.line(pts + [pts[0]], fill=p.stroke, width=lw)
    d.line([(s*0.50,s*0.15),(s*0.50,s*0.85)], fill=p.accent, width=max(1,lw//2))
    d.line([(s*0.15,s*0.50),(s*0.85,s*0.50)], fill=p.accent, width=max(1,lw//2))
    filled_circle(d, s*0.50, s*0.50, lw, p.accent)

@icon("interference_eval")
def draw_interference_eval(d, cell, p):
    """Geometry interference analysis with warning badge."""
    s = cell * SS
    lw = stroke_width(cell)
    shaded_prism(d, s, s*0.14, s*0.42, s*0.36, s*0.24, s*0.09, s*0.07, p)
    d.ellipse([s*0.40, s*0.32, s*0.80, s*0.72], fill=_rgba(p.accent, 18), outline=p.accent, width=lw)
    d.rounded_rectangle([s*0.40, s*0.43, s*0.56, s*0.66], radius=int(s*0.02),
                         fill=_rgba(p.err, 90), outline=p.err, width=lw)
    filled_circle(d, s*0.78, s*0.73, s*0.09, p.warn)
    d.line([(s*0.78,s*0.68),(s*0.78,s*0.74)], fill=(255,255,255), width=max(1,lw//2))

@icon("draft_analysis")
def draw_draft_analysis(d, cell, p):
    """Draft analysis color bands on a tapered face."""
    s = cell * SS
    lw = stroke_width(cell)
    pts = [(s*0.2, s*0.75), (s*0.8, s*0.75), (s*0.65, s*0.25), (s*0.35, s*0.25)]
    d.polygon(pts, fill=_rgba(p.accent, 44))
    d.line([pts[0], pts[1], pts[2], pts[3], pts[0]], fill=p.stroke, width=lw)
    for i, col in enumerate([p.ok, p.accent, p.warn, p.err]):
        y = s*(0.28 + i*0.12)
        d.rectangle([s*0.82, y, s*0.90, y + s*0.10], fill=col)
        d.line([(s*0.35, y+s*0.02), (s*0.67, y+s*0.04)], fill=col, width=max(1,lw//2))

@icon("thickness")
def draw_thickness(d, cell, p):
    """Wall thickness probe through a shell."""
    s = cell * SS
    lw = stroke_width(cell)
    d.rounded_rectangle([s*0.18, s*0.26, s*0.82, s*0.74], radius=int(s*0.04),
                         outline=p.stroke, width=lw*2)
    d.rounded_rectangle([s*0.32, s*0.40, s*0.68, s*0.60], radius=int(s*0.03),
                         outline=p.accent, width=lw*2)
    arrow(d, s*0.50, s*0.26, s*0.50, s*0.40, p.accent, lw)
    arrow(d, s*0.50, s*0.74, s*0.50, s*0.60, p.accent, lw)

@icon("curvature")
def draw_curvature(d, cell, p):
    """Surface curvature comb."""
    s = cell * SS
    lw = stroke_width(cell)
    arc_segment(d, s*0.5, s*1.0, s*0.65, 210, 330, p.stroke, lw*2)
    for angle in range(215, 330, 25):
        a = math.radians(angle)
        cx = s*0.5 + s*0.65*math.cos(a)
        cy = s*1.0 + s*0.65*math.sin(a)
        ex = cx + s*(0.10 + 0.08*math.sin(a*3))*math.cos(a)
        ey = cy + s*(0.10 + 0.08*math.sin(a*3))*math.sin(a)
        d.line([(cx, cy), (ex, ey)], fill=p.accent, width=lw)
        filled_circle(d, ex, ey, max(1,lw//2), p.accent)

@icon("zebra")
def draw_zebra(d, cell, p):
    """Zebra reflection stripes on a curved surface."""
    s = cell * SS
    lw = stroke_width(cell)
    r = s * 0.35
    cx, cy = s/2, s/2
    d.ellipse([cx-r, cy-r, cx+r, cy+r], fill=_rgba(p.stroke, 18), outline=p.stroke, width=lw)
    for i in range(5):
        y = cy - r + (2*r) * (i + 0.20) / 5
        half = math.sqrt(max(0, r*r - (y-cy)**2))
        col = p.stroke if i % 2 == 0 else p.accent
        d.arc([cx-half, y-s*0.07, cx+half, y+s*0.07], start=0, end=180, fill=col, width=lw*2)

@icon("symmetry_check")
def draw_symmetry_check(d, cell, p):
    s = cell * SS
    lw = stroke_width(cell)
    for y in [0.12,0.28,0.44,0.60,0.76]:
        d.line([(s*0.5, s*y), (s*0.5, s*(y+0.08))], fill=p.accent, width=lw)
    d.rounded_rectangle([s*0.55, s*0.3, s*0.82, s*0.7], radius=int(s*0.05),
                         outline=p.stroke, width=lw)
    d.rounded_rectangle([s*0.18, s*0.3, s*0.45, s*0.7], radius=int(s*0.05),
                         outline=p.stroke, width=lw)
    check_mark(d, s*0.68, s*0.5, s*0.1, p.ok, lw)

@icon("check")
def draw_check(d, cell, p):
    s = cell * SS
    lw = stroke_width(cell)
    shaded_prism(d, s, s*0.20, s*0.42, s*0.43, s*0.26, s*0.12, s*0.08, p)
    filled_circle(d, s*0.72, s*0.30, s*0.14, p.ok)
    check_mark(d, s*0.72, s*0.30, s*0.09, (255,255,255), lw)

@icon("geometry_analysis")
def draw_geometry_analysis(d, cell, p):
    """General geometry analysis scanner."""
    s = cell * SS
    lw = stroke_width(cell)
    shaded_prism(d, s, s*0.18, s*0.40, s*0.44, s*0.28, s*0.12, s*0.08, p)
    d.arc([s*0.50, s*0.16, s*0.90, s*0.56], start=95, end=350, fill=p.accent, width=lw)
    arrow(d, s*0.82, s*0.40, s*0.88, s*0.52, p.accent, lw)
    filled_circle(d, s*0.38, s*0.52, max(1,lw//2), p.warn)

@icon("performance")
def draw_performance(d, cell, p):
    """Speedometer needle."""
    s = cell * SS
    lw = stroke_width(cell)
    cx, cy = s/2, s*0.62
    r = s * 0.35
    arc_segment(d, cx, cy, r, 200, 340, p.stroke, lw*2)
    # needle
    angle = math.radians(260)
    arrow(d, cx, cy, cx + r*0.7*math.cos(angle), cy + r*0.7*math.sin(angle), p.accent, lw)
    filled_circle(d, cx, cy, lw*1.2, p.stroke)

# ---- Simulation tab ----

@icon("study_static")
def draw_study_static(d, cell, p):
    """Block under load arrows (static study)."""
    s = cell * SS
    lw = stroke_width(cell)
    d.rectangle([s*0.2, s*0.48, s*0.8, s*0.78], outline=p.stroke, width=lw)
    for x in [0.32, 0.50, 0.68]:
        arrow(d, s*x, s*0.20, s*x, s*0.48, p.accent, lw)

@icon("study_modal")
def draw_study_modal(d, cell, p):
    """Sine wave (modal frequencies)."""
    s = cell * SS
    lw = stroke_width(cell) * 2
    pts = []
    steps = 40
    for i in range(steps + 1):
        t = i / steps
        x = s*0.12 + t*(s*0.76)
        y = s*0.5 + math.sin(t * 2 * math.pi * 2) * s*0.25
        pts.append((x, y))
    for i in range(len(pts) - 1):
        col = p.accent if i < steps/2 else p.stroke
        d.line([pts[i], pts[i+1]], fill=col, width=lw)

@icon("study_flow")
def draw_study_flow(d, cell, p):
    """Flow streamlines."""
    s = cell * SS
    lw = stroke_width(cell)
    for oy in [0.28, 0.5, 0.72]:
        y = s * oy
        # slight sine distortion
        pts = [(s*(0.12 + i*0.17), y + s*0.05*math.sin(i*1.2)) for i in range(5)]
        polyline(d, pts, p.accent if oy == 0.5 else p.stroke, lw)

@icon("fixture")
def draw_fixture(d, cell, p):
    """Triangle base (fixed support)."""
    s = cell * SS
    lw = stroke_width(cell)
    pts = [(s*0.5, s*0.22), (s*0.82, s*0.72), (s*0.18, s*0.72)]
    d.polygon(pts, fill=None, outline=p.stroke)
    d.line([pts[0], pts[1], pts[2], pts[0]], fill=p.stroke, width=lw)
    # ground hatching
    for xi in range(5):
        x = s*(0.18 + xi*0.16)
        d.line([(x, s*0.72), (x - s*0.06, s*0.82)], fill=p.accent, width=lw)

@icon("load_force")
def draw_load_force(d, cell, p):
    """Arrow pointing at a surface."""
    s = cell * SS
    lw = stroke_width(cell) * 2
    d.line([(s*0.15, s*0.72), (s*0.85, s*0.72)], fill=p.stroke, width=lw)
    arrow(d, s*0.5, s*0.18, s*0.5, s*0.72, p.err, lw)

@icon("load_pressure")
def draw_load_pressure(d, cell, p):
    """Multiple parallel arrows at a surface."""
    s = cell * SS
    lw = stroke_width(cell)
    d.line([(s*0.15, s*0.72), (s*0.85, s*0.72)], fill=p.stroke, width=lw*2)
    for x in [0.28, 0.45, 0.62]:
        arrow(d, s*x, s*0.20, s*x, s*0.72, p.err, lw)

@icon("mesh")
def draw_mesh(d, cell, p):
    """Triangulated mesh pattern."""
    s = cell * SS
    lw = stroke_width(cell)
    # grid of triangles
    for row in range(3):
        for col in range(3):
            x0 = s*(0.15 + col*0.23)
            y0 = s*(0.18 + row*0.23)
            x1, y1 = x0 + s*0.23, y0 + s*0.23
            d.line([(x0, y0), (x1, y0), (x0, y1), (x0, y0)], fill=p.stroke, width=lw)
            d.line([(x1, y0), (x0, y1)], fill=p.accent, width=lw)

@icon("run_study")
def draw_run_study(d, cell, p):
    """Play button triangle."""
    s = cell * SS
    lw = stroke_width(cell)
    pts = [(s*0.25, s*0.18), (s*0.82, s*0.5), (s*0.25, s*0.82)]
    d.polygon(pts, fill=(*p.accent[:3], 180))
    d.line([pts[0], pts[1], pts[2], pts[0]], fill=p.accent, width=lw)

@icon("results")
def draw_results(d, cell, p):
    """Bar chart."""
    s = cell * SS
    lw = stroke_width(cell)
    d.line([(s*0.14, s*0.18), (s*0.14, s*0.80), (s*0.86, s*0.80)], fill=p.stroke, width=lw)
    for i, h in enumerate([0.45, 0.60, 0.35, 0.55]):
        x = s*(0.22 + i*0.16)
        y_top = s*(0.80 - h)
        col = p.accent if i == 1 else p.stroke
        d.rectangle([x, y_top, x + s*0.10, s*0.80], fill=col)

@icon("flow_arrow")
def draw_flow_arrow(d, cell, p):
    """Curved flow arrow."""
    s = cell * SS
    lw = stroke_width(cell) * 2
    arc_segment(d, s*0.5, s*0.65, s*0.32, 220, 320, p.accent, lw)
    arrow(d, s*0.82, s*0.38, s*0.85, s*0.25, p.accent, lw)

# ---- Viewport HUD ----

@icon("zoom_fit")
def draw_zoom_fit(d, cell, p):
    """Magnifier with plus."""
    s = cell * SS
    lw = stroke_width(cell)
    cx, cy, r = s*0.42, s*0.42, s*0.25
    circle(d, cx, cy, r, outline=p.stroke, lw=lw)
    plus_sign(d, cx, cy, r*0.5, p.accent, lw)
    arrow(d, cx + r * 0.72, cy + r * 0.72, s*0.82, s*0.82, p.stroke, lw)

@icon("view_cube")
def draw_view_cube(d, cell, p):
    """Small isometric cube for view orientation."""
    s = cell * SS
    lw = stroke_width(cell)
    iso_box(d, s/2, s/2, s*0.28, p.stroke, lw)

def _display_cube(d, s, p, lw, shaded: bool, interior_edges: bool, hidden_edges: bool = False):
    """Display-style cube family (shaded / shaded+edges / HLR / HLV): one true 2:1 iso
    cube (brand-mark projection) spanning ~2/3 of the cell like the neighbouring toolbar
    glyphs, all at a 1x stroke, so the view-toolbar button keeps one size and weight as
    the display style changes. Replaces a full-bleed rounded square with a 2x outline
    (shaded) and a flatter, smaller iso_box puck (HLR/HLV)."""
    q = _iso_pts(s * 0.5, s * 0.5, s * 0.33)
    if shaded:
        # Alphas look high but build_atlas() pastes with the icon as its own mask, which
        # squares alpha on the transparent atlas (200 -> ~157); these land as light tints.
        d.polygon([q["lt"], q["top"], q["rt"], q["c"]], fill=_rgba(p.accent, 200))
        d.polygon([q["lt"], q["c"], q["bot"], q["lb"]], fill=_rgba(p.accent, 150))
        d.polygon([q["c"], q["rt"], q["rb"], q["bot"]], fill=_rgba(p.accent, 100))
    if hidden_edges:
        # The three edges meeting at the hidden back-bottom corner, which a true iso
        # projection lands on the front-top corner `c`: dashed accent, drawn under the rim.
        for end in ("top", "lb", "rb"):   # dash from the rim inward, clear of the solid hub
            _dash(d, *q[end], *q["c"], p.accent, lw, dash=lw * 2.2, gap=lw * 1.5)
    rim = [q["lt"], q["top"], q["rt"], q["rb"], q["bot"], q["lb"]]
    d.line(rim + [rim[0]], fill=p.stroke, width=lw, joint="curve")
    if interior_edges:
        d.line([q["lt"], q["c"], q["rt"]], fill=p.stroke, width=lw, joint="curve")
        d.line([q["c"], q["bot"]], fill=p.stroke, width=lw)

@icon("display_shaded")
def draw_display_shaded(d, cell, p):
    """Shaded iso cube, silhouette only (no interior edges)."""
    s = cell * SS
    _display_cube(d, s, p, stroke_width(cell), shaded=True, interior_edges=False)

@icon("display_shaded_edges")
def draw_display_shaded_edges(d, cell, p):
    """Shaded iso cube with every visible edge stroked."""
    s = cell * SS
    _display_cube(d, s, p, stroke_width(cell), shaded=True, interior_edges=True)

@icon("display_hlr")
def draw_display_hlr(d, cell, p):
    """Unfilled iso cube, visible edges only (hidden line removal)."""
    s = cell * SS
    _display_cube(d, s, p, stroke_width(cell), shaded=False, interior_edges=True)

@icon("display_hlv")
def draw_display_hlv(d, cell, p):
    """Unfilled iso cube with its hidden edges dashed (hidden lines visible)."""
    s = cell * SS
    _display_cube(d, s, p, stroke_width(cell), shaded=False, interior_edges=True,
                  hidden_edges=True)

@icon("display_wireframe")
def draw_display_wireframe(d, cell, p):
    """Full wireframe cube with all edges visible."""
    s = cell * SS
    lw = stroke_width(cell)
    # front face
    d.rectangle([s*0.2, s*0.35, s*0.7, s*0.80], outline=p.stroke, width=lw)
    # back face offset
    d.rectangle([s*0.35, s*0.2, s*0.85, s*0.65], outline=p.accent, width=lw)
    # connecting lines
    for (x0, y0), (x1, y1) in [((s*0.2, s*0.35), (s*0.35, s*0.2)),
                                ((s*0.7, s*0.35), (s*0.85, s*0.2)),
                                ((s*0.7, s*0.80), (s*0.85, s*0.65))]:
        d.line([(x0, y0), (x1, y1)], fill=p.stroke, width=lw)

@icon("display_edges")
def draw_display_edges(d, cell, p):
    """Rounded square outline with visible edges."""
    s = cell * SS
    lw = stroke_width(cell) * 2
    d.rounded_rectangle([s*0.15, s*0.15, s*0.85, s*0.85], radius=int(s*0.12),
                         fill=None, outline=p.stroke, width=lw)

@icon("display_wire")
def draw_display_wire(d, cell, p):
    """Wireframe cube edges."""
    s = cell * SS
    lw = stroke_width(cell)
    # front face
    d.rectangle([s*0.2, s*0.35, s*0.7, s*0.80], outline=p.stroke, width=lw)
    # back face offset
    d.rectangle([s*0.35, s*0.2, s*0.85, s*0.65], outline=p.accent, width=lw)
    # connecting lines
    for (x0, y0), (x1, y1) in [((s*0.2, s*0.35), (s*0.35, s*0.2)),
                                ((s*0.7, s*0.35), (s*0.85, s*0.2)),
                                ((s*0.7, s*0.80), (s*0.85, s*0.65))]:
        d.line([(x0, y0), (x1, y1)], fill=p.stroke, width=lw)

@icon("section_view")
def draw_section_view(d, cell, p):
    """Rectangle cut in half by a line."""
    s = cell * SS
    lw = stroke_width(cell)
    d.rounded_rectangle([s*0.15, s*0.22, s*0.85, s*0.78], radius=int(s*0.06),
                         outline=p.stroke, width=lw)
    d.line([(s*0.5, s*0.14), (s*0.5, s*0.86)], fill=p.err, width=lw*2)
    d.rectangle([s*0.15, s*0.22, s*0.5, s*0.78], fill=(*p.accent[:3], 60))

@icon("pan_view")
def draw_pan_view(d, cell, p):
    """Four-way arrows pointing outward (pan/drag navigation)."""
    s = cell * SS
    lw = stroke_width(cell)
    cx, cy = s/2, s/2
    m = s * 0.32
    # four arrows: up, down, left, right
    arrow(d, cx, cy - m, cx, cy - m - s*0.10, p.accent, lw)
    arrow(d, cx, cy + m, cx, cy + m + s*0.10, p.accent, lw)
    arrow(d, cx - m, cy, cx - m - s*0.10, cy, p.accent, lw)
    arrow(d, cx + m, cy, cx + m + s*0.10, cy, p.accent, lw)
    # center circle
    circle(d, cx, cy, s*0.08, outline=p.stroke, lw=lw)

@icon("rotate_view")
def draw_rotate_view(d, cell, p):
    """Circular arrow around a sphere (rotate view)."""
    s = cell * SS
    lw = stroke_width(cell)
    cx, cy = s/2, s/2
    r_sphere = s * 0.22
    r_orbit = s * 0.35
    # sphere center
    circle(d, cx, cy, r_sphere, outline=p.stroke, lw=lw)
    # orbital arc (circular arrow)
    arc_segment(d, cx, cy, r_orbit, 45, 315, p.accent, lw)
    arrow(d, cx + r_orbit*0.707, cy - r_orbit*0.707,
          cx + r_orbit*0.8, cy - r_orbit*0.8, p.accent, lw)

@icon("zoom_area")
def draw_zoom_area(d, cell, p):
    """Magnifier glass over a dashed rectangle."""
    s = cell * SS
    lw = stroke_width(cell)
    # dashed rectangle (zoom area)
    segs = 4
    for i in range(segs):
        t0 = i / segs
        t1 = (i + 0.6) / segs
        # top edge
        d.line([(s*0.15 + (s*0.4)*t0, s*0.15), (s*0.15 + (s*0.4)*t1, s*0.15)], fill=p.accent, width=lw)
        # bottom edge
        d.line([(s*0.15 + (s*0.4)*t0, s*0.55), (s*0.15 + (s*0.4)*t1, s*0.55)], fill=p.accent, width=lw)
        # left edge
        d.line([(s*0.15, s*0.15 + (s*0.4)*t0), (s*0.15, s*0.15 + (s*0.4)*t1)], fill=p.accent, width=lw)
        # right edge
        d.line([(s*0.55, s*0.15 + (s*0.4)*t0), (s*0.55, s*0.15 + (s*0.4)*t1)], fill=p.accent, width=lw)
    # magnifier glass
    cx, cy, r = s*0.62, s*0.62, s*0.18
    circle(d, cx, cy, r, outline=p.stroke, lw=lw)
    arrow(d, cx + r * 0.72, cy + r * 0.72, s*0.82, s*0.82, p.stroke, lw)

@icon("view_selector")
def draw_view_selector(d, cell, p):
    """Small cube with one highlighted face."""
    s = cell * SS
    lw = stroke_width(cell)
    # isometric cube
    c = s / 2
    r = s * 0.25
    t = (c, c - r * 0.7)
    rt = (c + r, c - r * 0.35)
    lt = (c - r, c - r * 0.35)
    rm = (c + r, c + r * 0.15)
    lm = (c - r, c + r * 0.15)
    b = (c, c + r * 0.5)
    # top face (highlighted)
    d.polygon([lt, t, rt, rm, b, lm], fill=(*p.accent[:3], 100))
    # outlines
    polyline(d, [lt, t, rt, rm, b, lm, lt], p.stroke, lw)
    # internal edge
    d.line([lt, rm], fill=p.stroke, width=lw)
    d.line([t, b], fill=p.stroke, width=lw)

@icon("normal_to")
def draw_normal_to(d, cell, p):
    """Arrow perpendicular to a plane."""
    s = cell * SS
    lw = stroke_width(cell)
    # plane (parallelogram)
    pts = [(s*0.15, s*0.65), (s*0.55, s*0.28), (s*0.75, s*0.48), (s*0.35, s*0.85)]
    d.polygon(pts, fill=(*p.accent[:3], 60))
    d.line([pts[0], pts[1], pts[2], pts[3], pts[0]], fill=p.stroke, width=lw)
    # normal arrow perpendicular to plane (upward)
    cx, cy = (pts[0][0] + pts[1][0] + pts[2][0] + pts[3][0])/4, \
             (pts[0][1] + pts[1][1] + pts[2][1] + pts[3][1])/4
    arrow(d, cx, cy, cx + s*0.15, cy - s*0.25, p.accent, lw*2)

@icon("orientation")
def draw_orientation(d, cell, p):
    """Compass / orientation ring with N marker."""
    s = cell * SS
    lw = stroke_width(cell)
    cx, cy, r = s/2, s/2, s*0.35
    circle(d, cx, cy, r, outline=p.stroke, lw=lw)
    arrow(d, cx, cy, cx, cy - r*0.75, p.accent, lw)
    arrow(d, cx, cy, cx + r*0.75, cy, p.stroke, lw)

# ---- General ----

@icon("ok_check")
def draw_ok_check(d, cell, p):
    # Bold WHITE checkmark for the relocated PropertyManager OK button. The button supplies a
    # saturated statusOk-green fill in BOTH themes, so a white glyph reads cleanly on light + dark
    # (the old p.ok-green check was near-invisible on a green pill). Larger + thicker than before so
    # it stays legible in the wider chrome button.
    s = cell * SS
    lw = max(2, int(round(stroke_width(cell) * 2.6)))
    check_mark(d, s * 0.5, s * 0.54, s * 0.40, (255, 255, 255), lw)

@icon("cancel_x")
def draw_cancel_x(d, cell, p):
    # Bold WHITE X for the relocated PropertyManager Cancel button (the button fill supplies
    # the red — neutral at rest, statusErr on hover). White reads on both themes.
    s = cell * SS
    lw = max(2, int(round(stroke_width(cell) * 2.6)))
    x_sign(d, s * 0.5, s * 0.5, s * 0.36, (255, 255, 255), lw)

@icon("settings")
def draw_settings(d, cell, p):
    """Gear/cog."""
    s = cell * SS
    lw = stroke_width(cell)
    cx, cy = s/2, s/2
    outer_r, inner_r = s*0.35, s*0.20
    teeth = 8
    pts = []
    for i in range(teeth * 2):
        angle = math.radians(i * 180 / teeth)
        r = outer_r if i % 2 == 0 else inner_r + s*0.06
        pts.append((cx + r*math.cos(angle), cy + r*math.sin(angle)))
    d.polygon(pts, fill=None, outline=p.stroke)
    d.line(pts + [pts[0]], fill=p.stroke, width=lw)
    circle(d, cx, cy, s*0.12, outline=p.accent, lw=lw)

@icon("save")
def draw_save(d, cell, p):
    """Modern save card with inset label and corner notch."""
    s = cell * SS
    lw = stroke_width(cell)
    d.rounded_rectangle([s*0.15, s*0.13, s*0.85, s*0.87], radius=int(s*0.06),
                         fill=_rgba(p.stroke, 24), outline=p.stroke, width=lw)
    d.polygon([(s*0.63, s*0.13), (s*0.85, s*0.13), (s*0.85, s*0.35)],
              fill=_rgba(p.accent, 54))
    d.line([(s*0.63, s*0.13), (s*0.85, s*0.35)], fill=p.stroke, width=lw)
    d.rounded_rectangle([s*0.27, s*0.54, s*0.73, s*0.80], radius=int(s*0.035),
                         fill=_rgba(p.accent, 34), outline=p.accent, width=lw)
    d.line([(s*0.33, s*0.32), (s*0.61, s*0.32)], fill=p.stroke, width=lw)
    d.line([(s*0.33, s*0.42), (s*0.69, s*0.42)], fill=_rgba(p.stroke, 130), width=max(1, lw//2))

@icon("open")
def draw_open(d, cell, p):
    """Open folder with import arrow."""
    s = cell * SS
    lw = stroke_width(cell)
    d.rounded_rectangle([s*0.10, s*0.34, s*0.90, s*0.80], radius=int(s*0.06),
                         fill=_rgba(p.stroke, 24), outline=p.stroke, width=lw)
    d.rounded_rectangle([s*0.12, s*0.24, s*0.48, s*0.40], radius=int(s*0.035),
                         fill=_rgba(p.accent, 45), outline=p.stroke, width=lw)
    d.polygon([(s*0.15, s*0.43), (s*0.87, s*0.43), (s*0.76, s*0.82), (s*0.08, s*0.82)],
              fill=_rgba(p.accent, 35))
    d.line([(s*0.15, s*0.43), (s*0.87, s*0.43), (s*0.76, s*0.82), (s*0.08, s*0.82),
            (s*0.15, s*0.43)], fill=p.stroke, width=lw)
    arrow(d, s*0.53, s*0.67, s*0.53, s*0.47, p.accent, lw)

@icon("new_doc")
def draw_new_doc(d, cell, p):
    """New document page with folded corner and plus."""
    s = cell * SS
    lw = stroke_width(cell)
    fold = s * 0.22
    pts = [(s*0.22, s*0.12), (s*0.78 - fold, s*0.12),
           (s*0.78, s*0.12 + fold), (s*0.78, s*0.88),
           (s*0.22, s*0.88)]
    d.polygon(pts, fill=_rgba(p.stroke, 22), outline=p.stroke)
    d.line(pts + [pts[0]], fill=p.stroke, width=lw)
    d.polygon([(s*0.78 - fold, s*0.12), (s*0.78 - fold, s*0.12 + fold),
               (s*0.78, s*0.12 + fold)], fill=_rgba(p.accent, 48))
    d.line([(s*0.78 - fold, s*0.12), (s*0.78 - fold, s*0.12 + fold),
            (s*0.78, s*0.12 + fold)], fill=p.stroke, width=lw)
    d.rounded_rectangle([s*0.34, s*0.46, s*0.66, s*0.78], radius=int(s*0.04),
                         fill=_rgba(p.accent, 34), outline=p.accent, width=lw)
    plus_sign(d, s*0.5, s*0.62, s*0.12, p.accent, lw)

@icon("undo")
def draw_undo(d, cell, p):
    """Line-art counter-clockwise circular arrow. Uses the SAME 1x stroke as the neighbouring
    toolbar glyphs (save/open/new_doc) so undo/redo read as thin line icons, not the old bold
    double-weight arrows that were dropped."""
    s = cell * SS
    lw = stroke_width(cell)
    cx, cy = s*0.52, s*0.52
    r = s*0.31
    arc_segment(d, cx, cy, r, 182, 360, p.accent, lw)
    arc_segment(d, cx, cy, r, 0, 92, p.accent, lw)
    arrow(d, cx - r, cy, cx - r - s*0.13, cy - s*0.15, p.accent, lw)

@icon("redo")
def draw_redo(d, cell, p):
    """Line-art clockwise circular arrow — 1x stroke to match the other toolbar glyphs (see draw_undo)."""
    s = cell * SS
    lw = stroke_width(cell)
    cx, cy = s*0.48, s*0.52
    r = s*0.31
    arc_segment(d, cx, cy, r, 180, 358, p.accent, lw)
    arc_segment(d, cx, cy, r, 88, 180, p.accent, lw)
    arrow(d, cx + r, cy, cx + r + s*0.13, cy - s*0.15, p.accent, lw)

@icon("theme_toggle")
def draw_theme_toggle(d, cell, p):
    """Half sun / half moon circle."""
    s = cell * SS
    lw = stroke_width(cell)
    cx, cy, r = s/2, s/2, s*0.32
    # left half: moon
    d.arc([cx-r, cy-r, cx+r, cy+r], start=90, end=270, fill=p.stroke, width=lw*2)
    # right half: sun rays
    d.arc([cx-r, cy-r, cx+r, cy+r], start=270, end=90, fill=p.accent, width=lw*2)
    for ang in range(0, 360, 45):
        a = math.radians(ang)
        x0 = cx + (r + lw)*math.cos(a)
        y0 = cy + (r + lw)*math.sin(a)
        x1 = cx + (r + lw*4)*math.cos(a)
        y1 = cy + (r + lw*4)*math.sin(a)
        if -90 < ang < 90:
            d.line([(x0, y0), (x1, y1)], fill=p.accent, width=lw)

@icon("ai_panel")
def draw_ai_panel(d, cell, p):
    """Chat bubble with spark."""
    s = cell * SS
    lw = stroke_width(cell)
    d.rounded_rectangle([s*0.12, s*0.15, s*0.85, s*0.72], radius=int(s*0.12),
                         outline=p.accent, width=lw)
    # tail
    d.polygon([(s*0.28, s*0.72), (s*0.42, s*0.72), (s*0.22, s*0.85)],
              fill=None, outline=p.accent)
    d.line([(s*0.28, s*0.72), (s*0.42, s*0.72), (s*0.22, s*0.85), (s*0.28, s*0.72)],
           fill=p.accent, width=lw)
    # three dots inside
    for xi in [0.34, 0.5, 0.66]:
        filled_circle(d, s*xi, s*0.44, lw, p.accent)

@icon("activity")
def draw_activity(d, cell, p):
    """EKG/activity line."""
    s = cell * SS
    lw = stroke_width(cell) * 2
    pts = [(s*0.1, s*0.5), (s*0.28, s*0.5), (s*0.38, s*0.2),
           (s*0.5, s*0.78), (s*0.60, s*0.5), (s*0.90, s*0.5)]
    polyline(d, pts, p.accent, lw)

@icon("prompt_edit")
def draw_prompt_edit(d, cell, p):
    """Text cursor blinking inside a rect."""
    s = cell * SS
    lw = stroke_width(cell)
    d.rounded_rectangle([s*0.12, s*0.25, s*0.88, s*0.75], radius=int(s*0.06),
                         outline=p.stroke, width=lw)
    # text lines
    d.line([(s*0.22, s*0.44), (s*0.6, s*0.44)], fill=p.stroke, width=lw)
    d.line([(s*0.22, s*0.56), (s*0.45, s*0.56)], fill=p.stroke, width=lw)
    # cursor
    d.line([(s*0.65, s*0.38), (s*0.65, s*0.62)], fill=p.accent, width=lw*2)

@icon("msg_edit")
def draw_msg_edit(d, cell, p):
    """Diagonal pencil for chat message edit (user-bubble footer)."""
    s = cell * SS
    # Reuse the shared detailed pencil: bottom-left tip → top-right eraser.
    draw_pencil(d, s, p, s * 0.22, s * 0.78, s * 0.80, s * 0.20, s * 0.055)

@icon("ask")
def draw_ask(d, cell, p):
    """Speech bubble with a question mark — Copilot Ask mode (read-only Q&A)."""
    s = cell * SS
    lw = stroke_width(cell)
    # Rounded bubble body (stroke only — no fill, works on glass both themes).
    d.rounded_rectangle(
        [s * 0.14, s * 0.12, s * 0.86, s * 0.68],
        radius=int(s * 0.12),
        outline=p.stroke,
        width=lw,
    )
    # Tail pointing down-left (outline triangle, no fill).
    d.line(
        [(s * 0.30, s * 0.68), (s * 0.44, s * 0.68), (s * 0.24, s * 0.86), (s * 0.30, s * 0.68)],
        fill=p.stroke,
        width=lw,
        joint="curve",
    )
    # Question mark: curved top + stem + accent dot.
    # PIL arcs: 0° = 3 o'clock, angles increase clockwise.
    # Arc 200°→90° ends at bottom-center so the stem joins cleanly.
    cx = s * 0.50
    arc_r = s * 0.13
    arc_cy = s * 0.34
    d.arc(
        [cx - arc_r, arc_cy - arc_r, cx + arc_r, arc_cy + arc_r],
        start=200,
        end=90,
        fill=p.accent,
        width=lw,
    )
    # Stem from the bottom of the hook.
    d.line([(cx, arc_cy + arc_r), (cx, s * 0.52)], fill=p.accent, width=lw)
    # Dot under the stem.
    filled_circle(d, cx, s * 0.58, max(1, int(lw * 0.85)), p.accent)


@icon("eng_firm")
def draw_eng_firm(d, cell, p):
    """Classical portico (pediment + columns) — the engineering-firm org mode."""
    s = cell * SS
    lw = stroke_width(cell)
    # Pediment (roof triangle) over the columns.
    d.line(
        [(s * 0.12, s * 0.34), (s * 0.50, s * 0.12), (s * 0.88, s * 0.34)],
        fill=p.accent,
        width=lw,
        joint="curve",
    )
    # Architrave (horizontal beam under the pediment).
    d.line([(s * 0.14, s * 0.40), (s * 0.86, s * 0.40)], fill=p.stroke, width=lw)
    # Three columns.
    for cx in (s * 0.26, s * 0.50, s * 0.74):
        d.line([(cx, s * 0.44), (cx, s * 0.78)], fill=p.stroke, width=lw)
    # Base / stylobate.
    d.line([(s * 0.12, s * 0.82), (s * 0.88, s * 0.82)], fill=p.stroke, width=lw)


@icon("copy")
def draw_copy(d, cell, p):
    """Two overlapping sheets."""
    s = cell * SS
    lw = stroke_width(cell)
    d.rounded_rectangle([s*0.18, s*0.26, s*0.66, s*0.82], radius=int(s*0.04),
                         outline=p.stroke, width=lw)
    d.rounded_rectangle([s*0.34, s*0.14, s*0.82, s*0.70], radius=int(s*0.04),
                         outline=p.accent, width=lw)
    d.line([(s*0.44, s*0.34), (s*0.72, s*0.34)], fill=p.accent, width=lw)
    d.line([(s*0.44, s*0.48), (s*0.70, s*0.48)], fill=p.accent, width=lw)

@icon("paste")
def draw_paste(d, cell, p):
    """Clipboard with a sheet."""
    s = cell * SS
    lw = stroke_width(cell)
    d.rounded_rectangle([s*0.20, s*0.22, s*0.80, s*0.86], radius=int(s*0.06),
                         outline=p.stroke, width=lw)
    d.rounded_rectangle([s*0.36, s*0.10, s*0.64, s*0.28], radius=int(s*0.05),
                         outline=p.accent, width=lw)
    d.line([(s*0.32, s*0.45), (s*0.68, s*0.45)], fill=p.accent, width=lw)
    d.line([(s*0.32, s*0.60), (s*0.62, s*0.60)], fill=p.accent, width=lw)

@icon("delete")
def draw_delete(d, cell, p):
    """Trash can."""
    s = cell * SS
    lw = stroke_width(cell)
    d.line([(s*0.24, s*0.28), (s*0.76, s*0.28)], fill=p.err, width=lw)
    d.line([(s*0.40, s*0.18), (s*0.60, s*0.18)], fill=p.err, width=lw)
    d.rounded_rectangle([s*0.30, s*0.32, s*0.70, s*0.84], radius=int(s*0.04),
                         outline=p.stroke, width=lw)
    for x in [0.42, 0.50, 0.58]:
        d.line([(s*x, s*0.42), (s*x, s*0.74)], fill=p.stroke, width=max(1, lw//2))

@icon("folder_new")
def draw_folder_new(d, cell, p):
    """Folder with plus mark."""
    s = cell * SS
    lw = stroke_width(cell)
    d.rounded_rectangle([s*0.10, s*0.32, s*0.90, s*0.82], radius=int(s*0.05),
                         outline=p.stroke, width=lw)
    d.rectangle([s*0.10, s*0.25, s*0.45, s*0.36], outline=p.stroke, width=lw)
    plus_sign(d, s*0.64, s*0.58, s*0.14, p.accent, lw)

@icon("refresh")
def draw_refresh(d, cell, p):
    """Circular refresh arrows."""
    s = cell * SS
    lw = stroke_width(cell)
    cx, cy = s/2, s/2
    r = s*0.30
    arc_segment(d, cx, cy, r, 25, 185, p.stroke, lw)
    arrow(d, cx - r*0.98, cy, cx - r*0.88, cy - r*0.22, p.stroke, lw)
    arc_segment(d, cx, cy, r, 205, 365, p.accent, lw)
    arrow(d, cx + r*0.98, cy, cx + r*0.88, cy + r*0.22, p.accent, lw)

# ---------------------------------------------------------------------------
# C25: sketch flyout variant icons
# ---------------------------------------------------------------------------

@icon("rect_3pt_corner")
def draw_rect_3pt_corner(d, cell, p):
    """Rectangle by three corner picks (oriented) — first edge highlighted."""
    s = cell * SS
    lw = stroke_width(cell)
    quad = [(s*0.20, s*0.38), (s*0.66, s*0.22), (s*0.80, s*0.62), (s*0.34, s*0.78)]
    d.line(quad + [quad[0]], fill=p.stroke, width=lw)
    d.line([quad[0], quad[1]], fill=_rgba(p.accent, 160), width=max(1, lw//2))   # first edge
    for pt in (quad[0], quad[1], quad[2]):                                        # 3 picks
        filled_circle(d, pt[0], pt[1], lw*1.05, p.accent)

@icon("rect_3pt_center")
def draw_rect_3pt_center(d, cell, p):
    """Rectangle by center + two picks (oriented) — center crosshair."""
    s = cell * SS
    lw = stroke_width(cell)
    quad = [(s*0.22, s*0.38), (s*0.66, s*0.22), (s*0.78, s*0.62), (s*0.34, s*0.78)]
    d.line(quad + [quad[0]], fill=p.stroke, width=lw)
    cx = sum(q[0] for q in quad) / 4
    cy = sum(q[1] for q in quad) / 4
    m01 = ((quad[0][0]+quad[1][0])/2, (quad[0][1]+quad[1][1])/2)
    d.line([(cx, cy), m01], fill=_rgba(p.accent, 150), width=max(1, lw//2))
    filled_circle(d, cx, cy, lw*1.05, p.accent)        # center pick
    filled_circle(d, quad[1][0], quad[1][1], lw*0.95, p.accent)
    filled_circle(d, quad[2][0], quad[2][1], lw*0.95, p.accent)

@icon("parallelogram")
def draw_parallelogram(d, cell, p):
    """Parallelogram by three picks."""
    s = cell * SS
    lw = stroke_width(cell)
    pts = [(s*0.32, s*0.28), (s*0.86, s*0.28), (s*0.68, s*0.72), (s*0.14, s*0.72)]
    d.line(pts + [pts[0]], fill=p.stroke, width=lw)
    for pt in (pts[0], pts[1], pts[3]):
        filled_circle(d, pt[0], pt[1], lw*1.05, p.accent)

@icon("circle_perimeter")
def draw_circle_perimeter(d, cell, p):
    """Circle through three perimeter picks (no center)."""
    s = cell * SS
    lw = stroke_width(cell)
    cx, cy, r = s*0.5, s*0.5, s*0.33
    circle(d, cx, cy, r, outline=p.stroke, lw=lw*2)
    for ang in (-90, 35, 160):
        x = cx + r*math.cos(math.radians(ang))
        y = cy + r*math.sin(math.radians(ang))
        filled_circle(d, x, y, lw*1.0, p.accent)

@icon("slot_ctr_str")
def draw_slot_ctr_str(d, cell, p):
    """Centerpoint straight slot — emphasized center pick."""
    s = cell * SS
    lw = stroke_width(cell)
    d.rounded_rectangle([s*0.14, s*0.35, s*0.86, s*0.65], radius=int(s*0.15),
                         outline=p.stroke, width=lw)
    d.line([(s*0.14, s*0.5), (s*0.86, s*0.5)], fill=_rgba(p.accent, 150), width=max(1, lw//2))
    filled_circle(d, s*0.5, s*0.5, lw*1.1, p.accent)     # center pick
    filled_circle(d, s*0.28, s*0.5, lw*0.8, p.accent)
    filled_circle(d, s*0.72, s*0.5, lw*0.8, p.accent)

@icon("slot_arc_3pt")
def draw_slot_arc_3pt(d, cell, p):
    """3-point arc slot — curved capsule with 3 spine picks."""
    s = cell * SS
    lw = stroke_width(cell)
    cx, cy = s*0.5, s*0.80
    r_in, r_out = s*0.34, s*0.52
    a0, a1 = 208, 332
    arc_segment(d, cx, cy, r_in, a0, a1, p.stroke, lw)
    arc_segment(d, cx, cy, r_out, a0, a1, p.stroke, lw)
    for ang in (a0, a1):
        a = math.radians(ang)
        d.line([(cx + r_in*math.cos(a), cy + r_in*math.sin(a)),
                (cx + r_out*math.cos(a), cy + r_out*math.sin(a))], fill=p.stroke, width=lw)
    r_mid = (r_in + r_out) / 2
    for ang in (a0 + 5, (a0 + a1) / 2, a1 - 5):
        a = math.radians(ang)
        filled_circle(d, cx + r_mid*math.cos(a), cy + r_mid*math.sin(a), lw*0.85, p.accent)

@icon("slot_arc_ctr")
def draw_slot_arc_ctr(d, cell, p):
    """Centerpoint arc slot — center pick + radius to the spine."""
    s = cell * SS
    lw = stroke_width(cell)
    cx, cy = s*0.5, s*0.80
    r_in, r_out = s*0.34, s*0.52
    a0, a1 = 208, 332
    arc_segment(d, cx, cy, r_in, a0, a1, p.stroke, lw)
    arc_segment(d, cx, cy, r_out, a0, a1, p.stroke, lw)
    for ang in (a0, a1):
        a = math.radians(ang)
        d.line([(cx + r_in*math.cos(a), cy + r_in*math.sin(a)),
                (cx + r_out*math.cos(a), cy + r_out*math.sin(a))], fill=p.stroke, width=lw)
    am = math.radians((a0 + a1) / 2)
    r_mid = (r_in + r_out) / 2
    d.line([(cx, cy), (cx + r_mid*math.cos(am), cy + r_mid*math.sin(am))],
           fill=_rgba(p.accent, 140), width=max(1, lw//2))
    filled_circle(d, cx, cy, lw*1.1, p.accent)

@icon("spline_style")
def draw_spline_style(d, cell, p):
    """Style spline — smooth curve with square CV control handles."""
    s = cell * SS
    lw = stroke_width(cell)
    ctrl = [(0.16, 0.58), (0.36, 0.22), (0.62, 0.78), (0.84, 0.42)]
    cpts = [(s*x, s*y) for x, y in ctrl]
    d.line(_cr(cpts), fill=p.stroke, width=lw*2, joint="curve")
    for i in range(len(cpts) - 1):
        _dash(d, cpts[i][0], cpts[i][1], cpts[i+1][0], cpts[i+1][1],
              _rgba(p.accent, 120), max(1, lw//3), dash=s*0.045, gap=s*0.03)
    for x, y in cpts:
        d.rectangle([x - lw*0.8, y - lw*0.8, x + lw*0.8, y + lw*0.8], fill=p.accent)

@icon("spline_surface")
def draw_spline_surface(d, cell, p):
    """Spline on surface — a smooth curve lying on a plane patch."""
    s = cell * SS
    lw = stroke_width(cell)
    plane = [(s*0.10, s*0.62), (s*0.46, s*0.30), (s*0.90, s*0.40), (s*0.54, s*0.72)]
    d.polygon(plane, fill=_rgba(p.stroke, 26))
    d.line(plane + [plane[0]], fill=p.stroke, width=lw)
    curve = [(s*0.20, s*0.58), (s*0.40, s*0.42), (s*0.60, s*0.56), (s*0.80, s*0.44)]
    d.line(_cr(curve), fill=p.accent, width=lw*2, joint="curve")

@icon("spline_equation")
def draw_spline_equation(d, cell, p):
    """Equation-driven curve — smooth curve with an 'f(x)=' tag."""
    s = cell * SS
    lw = stroke_width(cell)
    curve = [(s*0.12, s*0.66), (s*0.34, s*0.34), (s*0.56, s*0.72), (s*0.80, s*0.40)]
    d.line(_cr(curve), fill=p.stroke, width=lw*2, joint="curve")
    d.rounded_rectangle([s*0.60, s*0.12, s*0.92, s*0.34], radius=int(s*0.04),
                         fill=_rgba(p.accent, 34), outline=p.accent, width=max(1, lw//3))
    d.line([(s*0.66, s*0.20), (s*0.86, s*0.20)], fill=p.accent, width=max(1, lw//2))
    d.line([(s*0.66, s*0.27), (s*0.86, s*0.27)], fill=p.accent, width=max(1, lw//2))

@icon("ellipse_partial")
def draw_ellipse_partial(d, cell, p):
    """Partial ellipse arc with endpoints + center."""
    s = cell * SS
    lw = stroke_width(cell)
    bb = [s*0.12, s*0.30, s*0.88, s*0.70]
    a0, a1 = 20, 250
    d.arc(bb, start=a0, end=a1, fill=p.stroke, width=lw*2)
    cx, cy, ax, ay = s*0.5, s*0.5, s*0.38, s*0.20
    for ang in (a0, a1):
        x = cx + ax*math.cos(math.radians(ang))
        y = cy + ay*math.sin(math.radians(ang))
        filled_circle(d, x, y, lw*0.9, p.accent)
    filled_circle(d, cx, cy, lw*0.8, p.accent)

@icon("parabola")
def draw_parabola(d, cell, p):
    """Parabolic curve — U opening upward, vertex + axis."""
    s = cell * SS
    lw = stroke_width(cell)
    pts = [(s*0.5 + ((i/32)*2 - 1)*s*0.36, s*0.74 - ((i/32)*2 - 1)**2 * s*0.50)
           for i in range(33)]
    d.line(pts, fill=p.stroke, width=lw*2, joint="curve")
    d.line([(s*0.5, s*0.16), (s*0.5, s*0.80)], fill=_rgba(p.accent, 120), width=max(1, lw//2))
    filled_circle(d, s*0.5, s*0.74, lw*0.9, p.accent)   # vertex

@icon("conic")
def draw_conic(d, cell, p):
    """Conic curve — rational arc shaped by a rho apex handle."""
    s = cell * SS
    lw = stroke_width(cell)
    p0, p2, apex = (s*0.16, s*0.74), (s*0.84, s*0.74), (s*0.5, s*0.18)
    rho = 0.45
    w = rho / (1 - rho)
    pts = []
    for i in range(33):
        t = i / 32
        den = (1 - t)**2 + 2*(1 - t)*t*w + t*t
        x = ((1 - t)**2*p0[0] + 2*(1 - t)*t*w*apex[0] + t*t*p2[0]) / den
        y = ((1 - t)**2*p0[1] + 2*(1 - t)*t*w*apex[1] + t*t*p2[1]) / den
        pts.append((x, y))
    d.line(pts, fill=p.stroke, width=lw*2, joint="curve")
    _dash(d, p0[0], p0[1], apex[0], apex[1], _rgba(p.accent, 120), max(1, lw//3),
          dash=s*0.04, gap=s*0.03)
    _dash(d, p2[0], p2[1], apex[0], apex[1], _rgba(p.accent, 120), max(1, lw//3),
          dash=s*0.04, gap=s*0.03)
    for pt in (p0, p2):
        filled_circle(d, pt[0], pt[1], lw*0.85, p.accent)
    filled_circle(d, apex[0], apex[1], lw*0.95, p.accent)   # rho handle

@icon("hide_show_eye")
def draw_hide_show_eye(d, cell, p):
    """Eye — the HUD Hide/Show Items visibility toggle."""
    s = cell * SS
    lw = stroke_width(cell)
    # almond eye outline: two arcs forming the lids
    d.arc([s*0.10, s*0.18, s*0.90, s*0.98], start=200, end=340, fill=p.stroke, width=lw)
    d.arc([s*0.10, s*0.02, s*0.90, s*0.82], start=20,  end=160, fill=p.stroke, width=lw)
    # iris + pupil
    circle(d, s*0.5, s*0.5, s*0.16, outline=p.stroke, lw=lw)
    filled_circle(d, s*0.5, s*0.5, s*0.06, p.accent)

# ---------------------------------------------------------------------------
# Overhaul contract icons — workspace tabs, sim study types, mesh types,
# sheet-metal, weldments, appearance. (append-only)
# ---------------------------------------------------------------------------

# ---- Simulation study types (extended) ----

@icon("study_thermal")
def draw_study_thermal(d, cell, p):
    """Thermometer with accent mercury bulb (thermal study). Distinct from
    study_flow streamlines."""
    s = cell * SS
    lw = stroke_width(cell)
    cx = s * 0.40
    half = s * 0.075          # tube half-width
    top = s * 0.18
    bulb_cy = s * 0.76
    bulb_r = s * 0.14
    # mercury: bulb + column rising to mid-tube (accent)
    filled_circle(d, cx, bulb_cy, bulb_r - lw * 0.5, p.accent)
    d.rectangle([cx - half + lw * 0.5, s * 0.48, cx + half - lw * 0.5, bulb_cy], fill=p.accent)
    # bulb outline
    circle(d, cx, bulb_cy, bulb_r, outline=p.stroke, lw=lw)
    # tube: two side walls + rounded cap on top
    d.line([(cx - half, top), (cx - half, bulb_cy - bulb_r * 0.55)], fill=p.stroke, width=lw)
    d.line([(cx + half, top), (cx + half, bulb_cy - bulb_r * 0.55)], fill=p.stroke, width=lw)
    d.arc([cx - half, top - half, cx + half, top + half], start=180, end=360, fill=p.stroke, width=lw)
    # scale ticks on the right
    for ty in (0.30, 0.40, 0.50):
        d.line([(cx + half + s * 0.04, s * ty), (cx + half + s * 0.12, s * ty)],
               fill=p.stroke, width=max(1, lw // 2))

@icon("study_fatigue")
def draw_study_fatigue(d, cell, p):
    """S-N (Wohler) fatigue curve: high stress at low cycles, decaying to an
    endurance plateau, with log-scale cycle hashes."""
    s = cell * SS
    lw = stroke_width(cell)
    ox, oy = s * 0.22, s * 0.80          # axis origin (bottom-left)
    d.line([(ox, s * 0.16), (ox, oy)], fill=p.stroke, width=lw)        # S (stress) axis
    d.line([(ox, oy), (s * 0.90, oy)], fill=p.stroke, width=lw)        # N (cycles) axis
    pts = []
    x0, x1 = ox + s * 0.04, s * 0.86
    for i in range(0, 21):
        t = i / 20.0
        x = x0 + (x1 - x0) * t
        y = s * 0.24 + (oy - s * 0.24) * (1 - math.exp(-3.0 * t)) * 0.78
        pts.append((x, y))
    polyline(d, pts, p.accent, lw)
    # log-scale cycle hash marks on the N axis
    for hx in (0.34, 0.48, 0.60, 0.70, 0.78):
        d.line([(s * hx, oy - s * 0.02), (s * hx, oy + s * 0.04)],
               fill=p.stroke, width=max(1, lw // 2))

# ---- Mesh types ----

@icon("mesh_tet")
def draw_mesh_tet(d, cell, p):
    """Wireframe tetrahedron (4 verts, 6 edges) — tet (Netgen Delaunay) mesh."""
    s = cell * SS
    lw = stroke_width(cell)
    apex = (s * 0.50, s * 0.16)
    a = (s * 0.18, s * 0.64)     # base left
    b = (s * 0.82, s * 0.56)     # base right
    c = (s * 0.54, s * 0.86)     # base front
    # back edge (a-b) lighter, as if behind
    d.line([a, b], fill=_rgba(p.accent, 95), width=max(1, lw // 2))
    for u, v in [(apex, a), (apex, b), (apex, c), (a, c), (b, c)]:
        d.line([u, v], fill=p.accent, width=lw)
    for v in (apex, a, b, c):
        filled_circle(d, v[0], v[1], lw * 0.9, p.stroke)

@icon("mesh_hex")
def draw_mesh_hex(d, cell, p):
    """Wireframe hexahedron (8 verts, 12 edges) — structured hex mesh;
    blockier/heavier than mesh_tet."""
    s = cell * SS
    lw = stroke_width(cell)
    dx, dy = s * 0.20, s * 0.14
    x0, y0, w, h = s * 0.16, s * 0.34, s * 0.44, s * 0.44
    fa, fb = (x0, y0), (x0 + w, y0)
    fc, fd = (x0 + w, y0 + h), (x0, y0 + h)
    ba, bb = (x0 + dx, y0 - dy), (x0 + w + dx, y0 - dy)
    bc, bd = (x0 + w + dx, y0 + h - dy), (x0 + dx, y0 + h - dy)
    # hidden back edges + lower-left connector, lighter
    for u, v in [(ba, bb), (bb, bc), (bc, bd), (bd, ba), (fd, bd)]:
        d.line([u, v], fill=_rgba(p.accent, 95), width=max(1, lw // 2))
    # visible front face + connecting edges
    for u, v in [(fa, fb), (fb, fc), (fc, fd), (fd, fa),
                 (fa, ba), (fb, bb), (fc, bc)]:
        d.line([u, v], fill=p.accent, width=lw)
    for v in (fa, fb, fc, fd, ba, bb, bc, bd):
        filled_circle(d, v[0], v[1], lw * 0.8, p.stroke)

@icon("mesh_auto")
def draw_mesh_auto(d, cell, p):
    """Auto mesh — triangulated patch with an auto-select spark."""
    s = cell * SS
    lw = stroke_width(cell)
    x0, y0, x1, y1 = s * 0.14, s * 0.30, s * 0.74, s * 0.86
    nx, ny = 3, 3
    for i in range(nx + 1):
        x = x0 + (x1 - x0) * i / nx
        d.line([(x, y0), (x, y1)], fill=_rgba(p.stroke, 150), width=max(1, lw // 2))
    for j in range(ny + 1):
        y = y0 + (y1 - y0) * j / ny
        d.line([(x0, y), (x1, y)], fill=_rgba(p.stroke, 150), width=max(1, lw // 2))
    for i in range(nx):
        for j in range(ny):
            ax = x0 + (x1 - x0) * i / nx
            ay = y0 + (y1 - y0) * (j + 1) / ny
            bx = x0 + (x1 - x0) * (i + 1) / nx
            by = y0 + (y1 - y0) * j / ny
            d.line([(ax, ay), (bx, by)], fill=_rgba(p.accent, 110), width=max(1, lw // 3))
    # auto spark (4-point star), top-right
    sx, sy, r = s * 0.80, s * 0.24, s * 0.13
    d.line([(sx, sy - r), (sx, sy + r)], fill=p.accent, width=lw)
    d.line([(sx - r, sy), (sx + r, sy)], fill=p.accent, width=lw)
    d.line([(sx - r * 0.5, sy - r * 0.5), (sx + r * 0.5, sy + r * 0.5)],
           fill=_rgba(p.accent, 150), width=max(1, lw // 2))
    d.line([(sx + r * 0.5, sy - r * 0.5), (sx - r * 0.5, sy + r * 0.5)],
           fill=_rgba(p.accent, 150), width=max(1, lw // 2))

# ---- Sheet metal ----

@icon("sm_base_flange")
def draw_sm_base_flange(d, cell, p):
    """Flat sheet (iso, accent top face) with a perpendicular wall folded up —
    the base flange / folded-sheet concept."""
    s = cell * SS
    lw = max(1, int(1.6 * SS))
    dx, dy = s * 0.26, s * 0.15
    bx, by, bw = s * 0.16, s * 0.62, s * 0.42
    base = [(bx, by), (bx + bw, by), (bx + bw + dx, by - dy), (bx + dx, by - dy)]
    d.polygon(base, fill=_rgba(p.accent, 70))
    d.line(base + [base[0]], fill=p.accent, width=lw)
    # perpendicular wall rising from the far edge
    wh = s * 0.34
    e0 = (bx + dx, by - dy)
    e1 = (bx + bw + dx, by - dy)
    wall = [e0, e1, (e1[0], e1[1] - wh), (e0[0], e0[1] - wh)]
    d.polygon(wall, fill=_rgba(p.stroke, 36))
    d.line(wall + [wall[0]], fill=p.stroke, width=lw)
    # bend highlight along the shared (fold) edge
    d.line([e0, e1], fill=p.accent, width=lw)

@icon("sm_edge_flange")
def draw_sm_edge_flange(d, cell, p):
    """Sheet in side profile with a rounded 90 deg bend at the right edge
    (bend arc + radius annotated). Distinct from sm_base_flange."""
    s = cell * SS
    lw = stroke_width(cell)
    t = s * 0.10
    x_left = s * 0.15
    yflat = s * 0.28
    y_bot = s * 0.85
    r = s * 0.16
    cbx, cby = s * 0.62, yflat + r       # bend arc center
    xv = cbx + r                          # outer vertical x
    ri = r - t
    # outer surface
    d.line([(x_left, yflat), (cbx, yflat)], fill=p.accent, width=lw)
    d.arc([cbx - r, cby - r, cbx + r, cby + r], start=270, end=360, fill=p.accent, width=lw)
    d.line([(xv, cby), (xv, y_bot)], fill=p.accent, width=lw)
    # inner surface
    d.line([(x_left, yflat + t), (cbx, yflat + t)], fill=p.accent, width=lw)
    d.arc([cbx - ri, cby - ri, cbx + ri, cby + ri], start=270, end=360, fill=p.accent, width=lw)
    d.line([(cbx + ri, cby), (cbx + ri, y_bot)], fill=p.accent, width=lw)
    # end caps
    d.line([(x_left, yflat), (x_left, yflat + t)], fill=p.accent, width=lw)
    d.line([(cbx + ri, y_bot), (xv, y_bot)], fill=p.accent, width=lw)
    # bend radius annotation (stroke)
    d.line([(cbx, cby), (cbx + r * 0.72, cby + r * 0.72)], fill=_rgba(p.stroke, 150), width=max(1, lw // 2))
    filled_circle(d, cbx, cby, max(1, lw // 2), p.stroke)

@icon("sm_hem")
def draw_sm_hem(d, cell, p):
    """Sheet edge folded 180 deg back on itself — a hem (centerline profile)."""
    s = cell * SS
    lw = stroke_width(cell)
    yc = s * 0.40
    x_left = s * 0.16
    x_fold = s * 0.74
    r = s * 0.11                          # hairpin radius
    # top run
    d.line([(x_left, yc), (x_fold, yc)], fill=p.accent, width=lw)
    # 180 deg hairpin (right semicircle)
    d.arc([x_fold - r, yc, x_fold + r, yc + 2 * r], start=270, end=90, fill=p.accent, width=lw)
    # returning flap (shorter), parallel below
    d.line([(x_fold, yc + 2 * r), (s * 0.40, yc + 2 * r)], fill=p.accent, width=lw)
    # left end cap of the main sheet
    d.line([(x_left, yc - s * 0.04), (x_left, yc + s * 0.04)], fill=_rgba(p.accent, 150), width=max(1, lw // 2))
    # thickness hint on the main run
    d.line([(x_left, yc + s * 0.05), (x_fold - r * 0.4, yc + s * 0.05)], fill=_rgba(p.accent, 110), width=max(1, lw // 2))

@icon("sm_jog")
def draw_sm_jog(d, cell, p):
    """Sheet side-profile with a jog — two bends forming a Z-shaped step."""
    s = cell * SS
    lw = stroke_width(cell)
    t = s * 0.085
    y1, y2 = s * 0.30, s * 0.66
    xa, xb, xc, xd = s * 0.16, s * 0.46, s * 0.60, s * 0.86
    top = [(xa, y1), (xb, y1), (xc, y2), (xd, y2)]
    bot = [(xa, y1 + t), (xb, y1 + t), (xc, y2 + t), (xd, y2 + t)]
    polyline(d, top, p.accent, lw)
    polyline(d, bot, _rgba(p.accent, 150), lw)
    d.line([(xa, y1), (xa, y1 + t)], fill=p.accent, width=lw)
    d.line([(xd, y2), (xd, y2 + t)], fill=p.accent, width=lw)

@icon("sm_flat_pattern")
def draw_sm_flat_pattern(d, cell, p):
    """Unfolded sheet — flat L plan view (accent fill) with dashed bend lines."""
    s = cell * SS
    lw = stroke_width(cell)
    # L-shaped flat outline
    poly = [(s * 0.16, s * 0.18), (s * 0.50, s * 0.18), (s * 0.50, s * 0.52),
            (s * 0.86, s * 0.52), (s * 0.86, s * 0.84), (s * 0.16, s * 0.84)]
    d.polygon(poly, fill=_rgba(p.accent, 55))
    d.line(poly + [poly[0]], fill=p.stroke, width=lw)
    # dashed bend lines (where the sheet would fold)
    def dashed(x0, y0, x1, y1):
        n = 7
        for i in range(n):
            if i % 2:
                continue
            t0, t1 = i / n, (i + 1) / n
            d.line([(x0 + (x1 - x0) * t0, y0 + (y1 - y0) * t0),
                    (x0 + (x1 - x0) * t1, y0 + (y1 - y0) * t1)],
                   fill=p.accent, width=max(1, lw // 2))
    dashed(s * 0.50, s * 0.18, s * 0.50, s * 0.84)   # vertical fold
    dashed(s * 0.16, s * 0.52, s * 0.86, s * 0.52)   # horizontal fold

# ---- Weldments ----

@icon("weld_member")
def draw_weld_member(d, cell, p):
    """Structural I-beam in iso (two flanges + web), reusing shaded_prism for
    the section faces."""
    s = cell * SS
    dx, dy = s * 0.20, s * 0.15
    fl_w, fl_h = s * 0.46, s * 0.09
    web_w, web_h = s * 0.14, s * 0.20
    xt = s * 0.15
    xw = xt + (fl_w - web_w) / 2
    yA = s * 0.27
    yWeb = yA + fl_h
    yBot = yWeb + web_h
    # painter order: bottom flange, web, top flange (upper elements overlay)
    shaded_prism(d, s, xt, yBot, fl_w, fl_h, dx, dy, p, accent=True)     # bottom flange
    shaded_prism(d, s, xw, yWeb, web_w, web_h, dx, dy, p, accent=True)   # web
    shaded_prism(d, s, xt, yA, fl_w, fl_h, dx, dy, p, accent=True)       # top flange

@icon("weld_gusset")
def draw_weld_gusset(d, cell, p):
    """Two plates at a right angle with a triangular gusset bracing the inside
    corner — stroke outline + light accent fill."""
    s = cell * SS
    lw = stroke_width(cell)
    t = s * 0.09
    vx = s * 0.20                         # vertical plate
    vy0, vy1 = s * 0.14, s * 0.84
    d.rectangle([vx, vy0, vx + t, vy1], fill=_rgba(p.stroke, 30), outline=p.stroke, width=lw)
    hx1, hy = s * 0.86, s * 0.84          # horizontal plate
    d.rectangle([vx, hy - t, hx1, hy], fill=_rgba(p.stroke, 30), outline=p.stroke, width=lw)
    # triangular gusset in the inside corner
    gx0, gy0 = vx + t, hy - t
    leg = s * 0.42
    tri = [(gx0, gy0), (gx0 + leg, gy0), (gx0, gy0 - leg)]
    d.polygon(tri, fill=_rgba(p.accent, 70))
    d.line(tri + [tri[0]], fill=p.stroke, width=lw)

@icon("weld_end_cap")
def draw_weld_end_cap(d, cell, p):
    """Plate cap closing the end of a hollow member (iso tube + accent cap)."""
    s = cell * SS
    lw = max(1, int(1.6 * SS))
    dx, dy = s * 0.18, s * 0.13
    x, y, w, h = s * 0.18, s * 0.40, s * 0.40, s * 0.36
    shaded_prism(d, s, x, y, w, h, dx, dy, p, accent=False)
    # cap plate over the end face, slightly oversized
    pad = s * 0.05
    cap = [(x - pad, y - pad), (x + w + pad, y - pad),
           (x + w + pad, y + h + pad), (x - pad, y + h + pad)]
    d.polygon(cap, fill=_rgba(p.accent, 80))
    d.line(cap + [cap[0]], fill=p.accent, width=lw)

# ---- Workspace tabs ----

@icon("ws_features")
def draw_ws_features(d, cell, p):
    """Workspace tab: Features — extruded boss with direction arrow."""
    s = cell * SS
    lw = stroke_width(cell)
    shaded_prism(d, s, s * 0.22, s * 0.40, s * 0.42, s * 0.34, s * 0.18, s * 0.14, p, accent=True)
    d.rounded_rectangle([s*0.18, s*0.68, s*0.58, s*0.82], radius=int(s*0.03),
                         outline=p.stroke, width=max(1, lw//2))
    arrow(d, s*0.40, s*0.66, s*0.40, s*0.22, p.accent, lw)

@icon("ws_sketch")
def draw_ws_sketch(d, cell, p):
    """Workspace tab: Sketch — a pencil drawing on an angled plane."""
    s = cell * SS
    lw = stroke_width(cell)
    plane = [(s * 0.16, s * 0.62), (s * 0.66, s * 0.62), (s * 0.84, s * 0.34), (s * 0.34, s * 0.34)]
    d.polygon(plane, fill=_rgba(p.accent, 36))
    d.line(plane + [plane[0]], fill=p.stroke, width=max(1, lw // 2))
    draw_pencil(d, s, p, s * 0.30, s * 0.74, s * 0.80, s * 0.22, s * 0.045)

@icon("ws_evaluate")
def draw_ws_evaluate(d, cell, p):
    """Workspace tab: Evaluate — a ruler with a measurement callout."""
    s = cell * SS
    lw = stroke_width(cell)
    rx0, ry0, rx1, ry1 = s * 0.14, s * 0.58, s * 0.70, s * 0.74
    d.rectangle([rx0, ry0, rx1, ry1], fill=_rgba(p.stroke, 28), outline=p.stroke, width=lw)
    n = 5
    for i in range(1, n):
        tx = rx0 + (rx1 - rx0) * i / n
        depth = s * 0.07 if i % 2 == 0 else s * 0.045
        d.line([(tx, ry0), (tx, ry0 + depth)], fill=p.stroke, width=max(1, lw // 2))
    # callout leader + dot + value chip
    d.line([(s * 0.50, s * 0.54), (s * 0.66, s * 0.30)], fill=p.accent, width=lw)
    filled_circle(d, s * 0.50, s * 0.54, lw * 0.9, p.accent)
    d.rounded_rectangle([s * 0.64, s * 0.20, s * 0.86, s * 0.34], radius=int(s * 0.03),
                        fill=_rgba(p.accent, 50), outline=p.accent, width=max(1, lw // 2))

@icon("ws_assembly")
def draw_ws_assembly(d, cell, p):
    """Workspace tab: Assembly — two parts being mated together."""
    s = cell * SS
    lw = stroke_width(cell)
    shaded_prism(d, s, s * 0.12, s * 0.40, s * 0.30, s * 0.30, s * 0.14, s * 0.11, p, accent=False)
    shaded_prism(d, s, s * 0.56, s * 0.46, s * 0.30, s * 0.30, s * 0.14, s * 0.11, p, accent=True)
    arrow(d, s * 0.40, s * 0.60, s * 0.50, s * 0.60, p.accent, lw)
    arrow(d, s * 0.66, s * 0.60, s * 0.56, s * 0.60, p.accent, lw)

@icon("ws_sheetmetal")
def draw_ws_sheetmetal(d, cell, p):
    """Workspace tab: Sheet Metal — a bent sheet seen edge-on."""
    s = cell * SS
    lw = stroke_width(cell)
    t = s * 0.11
    x_left = s * 0.16
    yflat = s * 0.34
    r = s * 0.16
    cbx, cby = s * 0.60, yflat + r
    xv = cbx + r
    y_bot = s * 0.84
    ri = r - t
    d.line([(x_left, yflat), (cbx, yflat)], fill=p.accent, width=lw)
    d.arc([cbx - r, cby - r, cbx + r, cby + r], start=270, end=360, fill=p.accent, width=lw)
    d.line([(xv, cby), (xv, y_bot)], fill=p.accent, width=lw)
    d.line([(x_left, yflat + t), (cbx, yflat + t)], fill=p.accent, width=lw)
    d.arc([cbx - ri, cby - ri, cbx + ri, cby + ri], start=270, end=360, fill=p.accent, width=lw)
    d.line([(cbx + ri, cby), (cbx + ri, y_bot)], fill=p.accent, width=lw)
    d.line([(x_left, yflat), (x_left, yflat + t)], fill=p.accent, width=lw)
    d.line([(cbx + ri, y_bot), (xv, y_bot)], fill=p.accent, width=lw)

@icon("ws_weldments")
def draw_ws_weldments(d, cell, p):
    """Workspace tab: Weldments — an I-beam cross-section."""
    s = cell * SS
    lw = stroke_width(cell)
    L, R = s * 0.22, s * 0.78
    midL, midR = s * 0.42, s * 0.58
    yt0, yt1 = s * 0.20, s * 0.32
    yb0, yb1 = s * 0.68, s * 0.80
    poly = [(L, yt0), (R, yt0), (R, yt1), (midR, yt1),
            (midR, yb0), (R, yb0), (R, yb1), (L, yb1),
            (L, yb0), (midL, yb0), (midL, yt1), (L, yt1)]
    d.polygon(poly, fill=_rgba(p.accent, 40))
    d.line(poly + [poly[0]], fill=p.accent, width=lw)

@icon("ws_surfaces")
def draw_ws_surfaces(d, cell, p):
    """Workspace tab: Surfaces — a wavy open-surface patch."""
    s = cell * SS
    lw = stroke_width(cell)
    x0, x1 = s * 0.16, s * 0.84
    base_y = s * 0.50
    amp = s * 0.12
    for k, (yoff, col) in enumerate([(s * 0.18, _rgba(p.stroke, 150)),
                                     (0.0, p.accent),
                                     (-s * 0.18, _rgba(p.stroke, 150))]):
        pts = []
        for i in range(0, 25):
            t = i / 24.0
            x = x0 + (x1 - x0) * t
            y = base_y + yoff + amp * math.sin(t * 2 * math.pi)
            pts.append((x, y))
        polyline(d, pts, col, lw if k == 1 else max(1, lw * 2 // 3))
    for t in (0.0, 0.5, 1.0):
        x = x0 + (x1 - x0) * t
        y_mid = base_y + amp * math.sin(t * 2 * math.pi)
        d.line([(x, y_mid - s * 0.18), (x, y_mid + s * 0.18)], fill=_rgba(p.stroke, 110), width=max(1, lw // 2))

@icon("ws_appearance")
def draw_ws_appearance(d, cell, p):
    """Workspace tab: Appearance — a shaded material sphere."""
    s = cell * SS
    lw = stroke_width(cell)
    cx, cy, r = s * 0.50, s * 0.52, s * 0.30
    filled_circle(d, cx, cy, r, _rgba(p.accent, 110))
    circle(d, cx, cy, r, outline=p.stroke, lw=lw)
    filled_circle(d, cx - r * 0.32, cy - r * 0.34, r * 0.20, _rgba((255, 255, 255), 150))
    d.arc([cx - r, cy - r, cx + r, cy + r], start=20, end=150, fill=_rgba(p.stroke, 120), width=max(1, lw // 2))

@icon("ws_parts")
def draw_ws_parts(d, cell, p):
    """Workspace tab: Parts Library — a stocked pair: hex bolt + cog.
    Only two silhouettes on purpose: the crate-of-everything motif is already
    `parts_all`, and a third body smears into mush at the 20 px rail size."""
    s = cell * SS
    lw = stroke_width(cell)
    # Hex bolt (neutral stroke) — the library's signature fastener. The head is a
    # FLAT-TOP hex squashed ~15% vertically: a regular point-up hex this small
    # LANCZOS-downsamples into a circle and the glyph then reads as a magnifier.
    bx = s * 0.30
    hrx, hry = s * 0.245, s * 0.205
    head = [(bx + hrx * math.cos(math.radians(i * 60)),
             s * 0.25 + hry * math.sin(math.radians(i * 60))) for i in range(6)]
    d.polygon(head, fill=_rgba(p.stroke, 70))
    d.line(head + [head[0]], fill=p.stroke, width=lw)
    d.rectangle([bx - s * 0.08, s * 0.44, bx + s * 0.08, s * 0.82],
                fill=_rgba(p.stroke, 55), outline=p.stroke, width=lw)
    for i in range(2):
        y = s * (0.56 + i * 0.13)
        d.line([(bx - s * 0.125, y), (bx + s * 0.125, y + s * 0.05)],
               fill=p.stroke, width=lw)
    # Cog (accent) — the second stocked component.
    cx, cy = s * 0.71, s * 0.63
    ro, ri = s * 0.23, s * 0.15
    teeth = 7
    cog = []
    for i in range(teeth * 2):
        a = math.radians(i * 180.0 / teeth - 90)
        rad = ro if i % 2 == 0 else ri
        cog.append((cx + rad * math.cos(a), cy + rad * math.sin(a)))
    d.polygon(cog, fill=_rgba(p.accent, 110))
    d.line(cog + [cog[0]], fill=p.accent, width=lw)
    # Bore punched clean through (alpha 0, as in draw_hole_wizard): a solid-filled
    # cog collapses into an accent-colored blob once downsampled to 20 px.
    filled_circle(d, cx, cy, s * 0.072, (0, 0, 0, 0))
    circle(d, cx, cy, s * 0.072, outline=p.accent, lw=max(1, lw // 2))

@icon("ws_manufacture")
def draw_ws_manufacture(d, cell, p):
    """Workspace tab: Manufacture — an end mill cutting into stock.
    The stock stays neutral and the cutter carries the accent; drawn in one
    tone the tool sinks into the block at 20 px and the pair reads as one lump."""
    s = cell * SS
    lw = stroke_width(cell)
    shaded_prism(d, s, s * 0.08, s * 0.74, s * 0.64, s * 0.14, s * 0.13, s * 0.10, p, accent=False)
    cx = s * 0.50
    # shank
    d.rectangle([cx - s * 0.07, s * 0.06, cx + s * 0.07, s * 0.32],
                fill=_rgba(p.stroke, 45), outline=p.stroke, width=lw)
    # cutter body with a chamfered tip biting the stock top face
    body = [(cx - s * 0.125, s * 0.32), (cx + s * 0.125, s * 0.32),
            (cx + s * 0.125, s * 0.62), (cx + s * 0.05, s * 0.72),
            (cx - s * 0.05, s * 0.72), (cx - s * 0.125, s * 0.62)]
    d.polygon(body, fill=_rgba(p.accent, 110))
    d.line(body + [body[0]], fill=p.accent, width=lw)
    # Two flutes only (three fill in solid at 20 px), drawn at FULL alpha: the
    # atlas paste premultiplies (RGB*a, a^2), so a translucent stroke here bakes
    # down into one dark bar instead of two bright helical lines.
    for i in range(2):
        x0 = cx - s * 0.085 + i * s * 0.085
        d.line([(x0, s * 0.62), (x0 + s * 0.055, s * 0.36)],
               fill=p.stroke, width=max(1, lw // 2))

# ---- Appearance ----

@icon("color_picker")
def draw_color_picker(d, cell, p):
    """Appearance opener — multi-swatch palette + eyedropper tip.
    Solid fills so the ribbon cell never reads as an empty white plate."""
    s = cell * SS
    lw = stroke_width(cell)
    # 2x2 color swatches (reads as a material/color picker even at 20px).
    pad = s * 0.14
    gap = s * 0.04
    sw = (s - 2 * pad - gap) * 0.5
    colors = [
        (pad, pad, (80, 170, 255)),                 # blue
        (pad + sw + gap, pad, (240, 90, 90)),       # red
        (pad, pad + sw + gap, (90, 210, 130)),      # green
        (pad + sw + gap, pad + sw + gap, p.accent), # accent
    ]
    for x, y, col in colors:
        d.rounded_rectangle([x, y, x + sw, y + sw], radius=int(s * 0.04),
                            fill=_rgba(col, 220), outline=p.stroke, width=max(1, lw // 2))
    # Eyedropper tip over the bottom-right swatch (reads "pick color").
    tip_x, tip_y = pad + sw + gap + sw * 0.55, pad + sw + gap + sw * 0.55
    handle_x, handle_y = s * 0.86, s * 0.14
    d.line([(handle_x, handle_y), (tip_x, tip_y)], fill=p.stroke, width=max(lw, int(2.2 * SS)))
    filled_circle(d, handle_x, handle_y, s * 0.07, _rgba(p.stroke, 200))
    filled_circle(d, tip_x, tip_y, s * 0.055, p.accent)

@icon("appr_metal")
def draw_appr_metal(d, cell, p):
    """Metal appearance — sphere with banded metallic reflections."""
    s = cell * SS
    lw = stroke_width(cell)
    cx, cy, r = s * 0.50, s * 0.50, s * 0.32
    circle(d, cx, cy, r, fill=_rgba(p.stroke, 60), outline=p.stroke, lw=lw)
    for frac, a in [(-0.45, 40), (-0.15, 150), (0.15, 70), (0.45, 120)]:
        yb = cy + r * frac
        half = (r * r - (r * frac) ** 2) ** 0.5
        d.line([(cx - half, yb), (cx + half, yb)], fill=_rgba((255, 255, 255), a), width=max(2, lw))
    filled_circle(d, cx - r * 0.30, cy - r * 0.35, r * 0.16, _rgba((255, 255, 255), 170))

def _appr_metal_variant(d, cell, p, tint, band=(255, 255, 255)):
    """Banded metallic sphere in a specific alloy tint — shared body for steel/brass/dark so the
    appearance material buttons read as distinct alloys instead of three identical spheres."""
    s = cell * SS
    lw = stroke_width(cell)
    cx, cy, r = s * 0.50, s * 0.50, s * 0.32
    filled_circle(d, cx, cy, r, _rgba(tint, 200))
    circle(d, cx, cy, r, outline=p.stroke, lw=lw)
    for frac, a in [(-0.45, 45), (-0.15, 165), (0.15, 80), (0.45, 130)]:
        yb = cy + r * frac
        half = (r * r - (r * frac) ** 2) ** 0.5
        d.line([(cx - half, yb), (cx + half, yb)], fill=_rgba(band, a), width=max(2, lw))
    filled_circle(d, cx - r * 0.30, cy - r * 0.35, r * 0.16, _rgba(band, 190))

@icon("appr_steel")
def draw_appr_steel(d, cell, p):
    """Steel appearance — cool blue-grey banded metallic sphere."""
    _appr_metal_variant(d, cell, p, (120, 140, 165))

@icon("appr_brass")
def draw_appr_brass(d, cell, p):
    """Brass appearance — warm golden banded metallic sphere."""
    _appr_metal_variant(d, cell, p, (198, 158, 70), band=(255, 240, 185))

@icon("appr_dark")
def draw_appr_dark(d, cell, p):
    """Dark-metal appearance — charcoal sphere with light reflection bands."""
    _appr_metal_variant(d, cell, p, (58, 64, 72), band=(205, 214, 226))

@icon("appr_plastic")
def draw_appr_plastic(d, cell, p):
    """Plastic appearance — smooth glossy sphere with a soft highlight."""
    s = cell * SS
    lw = stroke_width(cell)
    cx, cy, r = s * 0.50, s * 0.50, s * 0.32
    filled_circle(d, cx, cy, r, _rgba(p.accent, 120))
    circle(d, cx, cy, r, outline=p.stroke, lw=lw)
    filled_circle(d, cx - r * 0.28, cy - r * 0.30, r * 0.30, _rgba((255, 255, 255), 120))
    filled_circle(d, cx - r * 0.30, cy - r * 0.32, r * 0.16, _rgba((255, 255, 255), 185))
    filled_circle(d, cx + r * 0.34, cy + r * 0.36, r * 0.10, _rgba((255, 255, 255), 90))

@icon("appr_glass")
def draw_appr_glass(d, cell, p):
    """Glass appearance — translucent sphere with bright specular streaks."""
    s = cell * SS
    lw = stroke_width(cell)
    cx, cy, r = s * 0.50, s * 0.50, s * 0.32
    filled_circle(d, cx, cy, r, _rgba(p.accent, 40))
    circle(d, cx, cy, r, outline=p.stroke, lw=lw)
    d.arc([cx - r * 0.7, cy - r * 0.7, cx + r * 0.2, cy + r * 0.2], start=120, end=210,
          fill=_rgba((255, 255, 255), 200), width=max(2, lw))
    d.arc([cx - r * 0.5, cy - r * 0.5, cx + r * 0.4, cy + r * 0.4], start=120, end=200,
          fill=_rgba((255, 255, 255), 120), width=max(1, lw // 2))
    filled_circle(d, cx + r * 0.30, cy - r * 0.30, r * 0.10, _rgba((255, 255, 255), 150))

@icon("appr_paint")
def draw_appr_paint(d, cell, p):
    """Paint appearance — a paint drop."""
    s = cell * SS
    lw = stroke_width(cell)
    cx = s * 0.50
    r = s * 0.22
    cy = s * 0.60
    tip = (cx, s * 0.16)
    left = (cx - r, cy)
    right = (cx + r, cy)
    d.polygon([tip, left, right], fill=_rgba(p.accent, 150))
    filled_circle(d, cx, cy, r, _rgba(p.accent, 150))
    d.line([tip, left], fill=p.stroke, width=lw)
    d.line([tip, right], fill=p.stroke, width=lw)
    d.arc([cx - r, cy - r, cx + r, cy + r], start=0, end=180, fill=p.stroke, width=lw)
    filled_circle(d, cx - r * 0.30, cy - r * 0.05, r * 0.18, _rgba((255, 255, 255), 130))

@icon("appr_default")
def draw_appr_default(d, cell, p):
    """Default appearance — a plain neutral (matte) material sphere."""
    s = cell * SS
    lw = stroke_width(cell)
    cx, cy, r = s * 0.50, s * 0.50, s * 0.32
    filled_circle(d, cx, cy, r, _rgba(p.stroke, 55))
    circle(d, cx, cy, r, outline=p.stroke, lw=lw)
    filled_circle(d, cx - r * 0.30, cy - r * 0.32, r * 0.16, _rgba((255, 255, 255), 90))
    filled_circle(d, cx + r * 0.42, cy + r * 0.42, lw * 1.1, p.accent)

# ---------------------------------------------------------------------------
# MISSING placeholder
# ---------------------------------------------------------------------------

def draw_missing(d: ImageDraw.ImageDraw, cell: int, p: Palette):
    s = cell * SS
    lw = stroke_width(cell)
    diagonal_box(d, s*0.1, s*0.1, s*0.9, s*0.9, p.err, lw)


def _ngon(d, cell, p, n, start_deg=-90):
    """Regular n-gon by center + a vertex (radius), accent center+vertex dots."""
    s = cell * SS
    lw = stroke_width(cell)
    cx, cy, r = s*0.5, s*0.5, s*0.36
    pts = [(cx + r*math.cos(math.radians(start_deg + i*360/n)),
            cy + r*math.sin(math.radians(start_deg + i*360/n))) for i in range(n)]
    d.line(pts + [pts[0]], fill=p.stroke, width=lw)
    d.line([(cx, cy), pts[0]], fill=_rgba(p.accent, 150), width=max(1, lw//2))
    filled_circle(d, cx, cy, lw*0.9, p.accent)
    filled_circle(d, pts[0][0], pts[0][1], lw*0.9, p.accent)


@icon("poly_triangle")
def draw_poly_triangle(d, cell, p):
    """Equilateral triangle drop-in shape."""
    _ngon(d, cell, p, 3)


@icon("poly_square")
def draw_poly_square(d, cell, p):
    """Axis-aligned square drop-in shape."""
    _ngon(d, cell, p, 4, start_deg=-45)


@icon("poly_pentagon")
def draw_poly_pentagon(d, cell, p):
    """Regular pentagon drop-in shape."""
    _ngon(d, cell, p, 5)


@icon("poly_octagon")
def draw_poly_octagon(d, cell, p):
    """Regular octagon drop-in shape."""
    _ngon(d, cell, p, 8, start_deg=-67.5)


@icon("poly_star")
def draw_poly_star(d, cell, p):
    """Five-point star (alternating outer/inner radius)."""
    s = cell * SS
    lw = stroke_width(cell)
    cx, cy = s*0.5, s*0.5
    ro, ri, n = s*0.40, s*0.17, 5
    pts = []
    for i in range(n*2):
        rr = ro if i % 2 == 0 else ri
        ang = math.radians(-90 + i*180/n)
        pts.append((cx + rr*math.cos(ang), cy + rr*math.sin(ang)))
    d.polygon(pts, fill=_rgba(p.accent, 55))
    d.line(pts + [pts[0]], fill=p.stroke, width=lw)
    filled_circle(d, cx, cy, lw*0.85, p.accent)


@icon("airfoil")
def draw_airfoil(d, cell, p):
    """Side-on cambered wing cross-section with chord line + leading-edge dot."""
    s = cell * SS
    lw = stroke_width(cell)
    le = (s*0.12, s*0.52)
    te = (s*0.90, s*0.50)
    upper = [le, (s*0.26, s*0.34), (s*0.50, s*0.30), (s*0.72, s*0.39), te]
    lower = [le, (s*0.34, s*0.60), (s*0.60, s*0.585), (s*0.80, s*0.535), te]
    up = _cr(upper)
    lo = _cr(lower)
    d.polygon(up + list(reversed(lo)), fill=_rgba(p.accent, 45))
    polyline(d, up, p.stroke, lw)
    polyline(d, lo, p.stroke, lw)
    d.line([le, te], fill=_rgba(p.accent, 140), width=max(1, lw//2))
    filled_circle(d, le[0], le[1], lw*0.95, p.accent)


# ---------------------------------------------------------------------------
# Fallback drawers: Manufacture toolpath cells. Shipped art is the real
# cam_generate render (icon_pipeline.py); these read as "toolpath over stock".
# ---------------------------------------------------------------------------

def _cam_stock(d, cell, p, top=0.42):
    """Iso-ish stock slab: top face parallelogram + front edge."""
    s = cell * SS
    lw = stroke_width(cell)
    quad = [(s*0.14, s*top+s*0.16), (s*0.42, s*top), (s*0.88, s*top+s*0.10),
            (s*0.60, s*top+s*0.26)]
    d.polygon(quad, fill=_rgba(p.fill, 60), outline=None)
    polyline(d, quad + [quad[0]], p.stroke, max(1, lw//2))
    return quad


def _zig(cell, x0, y0, x1, y1, n):
    s_ = cell * SS
    pts = []
    for i in range(n + 1):
        t = i / n
        x = x0 + (x1 - x0) * t
        pts.append((s_*x, s_*(y0 if i % 2 == 0 else y1)))
    return pts


def _cam_25d(pattern):
    def draw(d, cell, p):
        _cam_stock(d, cell, p)
        lw = stroke_width(cell)
        s = cell * SS
        if pattern == "zig":
            polyline(d, _zig(cell, 0.24, 0.44, 0.74, 0.54, 6), p.accent, lw)
        elif pattern == "loop":
            polyline(d, [(s*0.28, s*0.50), (s*0.46, s*0.40), (s*0.78, s*0.46),
                         (s*0.58, s*0.58), (s*0.28, s*0.50)], p.accent, lw)
        elif pattern == "spiral":
            polyline(d, [(s*0.50, s*0.50), (s*0.60, s*0.46), (s*0.56, s*0.56),
                         (s*0.42, s*0.52), (s*0.48, s*0.40), (s*0.70, s*0.44),
                         (s*0.66, s*0.60), (s*0.34, s*0.56)], p.accent, lw)
        elif pattern == "slot":
            polyline(d, [(s*0.30, s*0.56), (s*0.70, s*0.42)], p.accent, lw + 2)
        elif pattern == "vee":
            polyline(d, [(s*0.30, s*0.44), (s*0.44, s*0.56), (s*0.58, s*0.42),
                         (s*0.72, s*0.54)], p.accent, lw)
    return draw


def _cam_drill_glyph(helix=False):
    def draw(d, cell, p):
        _cam_stock(d, cell, p, top=0.40)
        lw = stroke_width(cell)
        s = cell * SS
        for x in ((0.38, 0.62) if not helix else (0.5,)):
            d.line([(s*x, s*0.16), (s*x, s*0.62)], fill=_rgba(p.accent, 255), width=lw)
            d.polygon([(s*(x-0.05), s*0.56), (s*(x+0.05), s*0.56), (s*x, s*0.68)],
                      fill=_rgba(p.accent, 255))
        if helix:
            for i in range(3):
                y = 0.28 + i * 0.12
                d.arc([s*0.36, s*(y-0.05), s*0.64, s*(y+0.05)], start=0, end=180,
                      fill=_rgba(p.accent2, 230), width=max(1, lw//2))
    return draw


def _cam_3d(kind):
    def draw(d, cell, p):
        lw = stroke_width(cell)
        s = cell * SS
        # hump surface
        polyline(d, [(s*0.12, s*0.66), (s*0.30, s*0.62), (s*0.44, s*0.36),
                     (s*0.62, s*0.36), (s*0.74, s*0.60), (s*0.90, s*0.64)],
                 p.stroke, max(1, lw//2))
        if kind == "raster":
            for dy in (0.0, 0.10, 0.20):
                polyline(d, [(s*0.16, s*(0.60+dy*0.3)), (s*0.44, s*(0.42+dy*0.35)),
                             (s*0.62, s*(0.42+dy*0.35)), (s*0.86, s*(0.58+dy*0.3))],
                         p.accent, max(1, lw//2) if dy else lw)
        elif kind == "rings":
            for r, y in ((0.10, 0.42), (0.18, 0.50), (0.26, 0.58)):
                d.ellipse([s*(0.52-r), s*(y-r*0.36), s*(0.52+r), s*(y+r*0.36)],
                          outline=_rgba(p.accent, 255), width=max(1, lw//2))
        elif kind == "wave":
            for dy in (0.0, 0.10):
                polyline(d, _zig(cell, 0.16, 0.50+dy, 0.86, 0.58+dy, 8), p.accent,
                         max(1, lw//2) if dy else lw)
        elif kind == "bowl":
            d.arc([s*0.24, s*0.30, s*0.78, s*0.74], start=20, end=160,
                  fill=_rgba(p.accent, 255), width=lw)
            d.arc([s*0.34, s*0.38, s*0.68, s*0.66], start=20, end=160,
                  fill=_rgba(p.accent, 255), width=max(1, lw//2))
    return draw


def _cam_turn(kind):
    def draw(d, cell, p):
        lw = stroke_width(cell)
        s = cell * SS
        # bar stock silhouette (horizontal cylinder)
        d.rounded_rectangle([s*0.14, s*0.30, s*0.86, s*0.70], radius=s*0.10,
                            outline=_rgba(p.stroke, 255), width=max(1, lw//2))
        if kind == "rough":
            for i, x in enumerate((0.30, 0.44, 0.58)):
                d.line([(s*x, s*0.30), (s*x, s*(0.44 + i*0.06))],
                       fill=_rgba(p.accent, 255), width=lw)
        elif kind == "finish":
            polyline(d, [(s*0.20, s*0.34), (s*0.42, s*0.34), (s*0.52, s*0.42),
                         (s*0.72, s*0.42), (s*0.80, s*0.50)], p.accent, lw)
        elif kind == "face":
            d.line([(s*0.82, s*0.30), (s*0.82, s*0.70)], fill=_rgba(p.accent, 255),
                   width=lw)
        elif kind == "part":
            d.line([(s*0.52, s*0.24), (s*0.52, s*0.62)], fill=_rgba(p.accent, 255),
                   width=lw)
            d.polygon([(s*0.47, s*0.56), (s*0.57, s*0.56), (s*0.52, s*0.70)],
                      fill=_rgba(p.accent, 255))
    return draw


def _cam_cut(d, cell, p):
    quad = _cam_stock(d, cell, p, top=0.44)
    lw = stroke_width(cell)
    s = cell * SS
    polyline(d, [(s*0.34, s*0.52), (s*0.52, s*0.44), (s*0.70, s*0.50),
                 (s*0.52, s*0.58), (s*0.34, s*0.52)], p.accent, lw)
    d.line([(s*0.52, s*0.14), (s*0.52, s*0.44)], fill=_rgba(p.err, 255), width=lw)


def _cam_probe(d, cell, p):
    lw = stroke_width(cell)
    s = cell * SS
    d.line([(s*0.50, s*0.12), (s*0.50, s*0.52)], fill=_rgba(p.stroke, 255), width=lw)
    filled_circle(d, s*0.50, s*0.58, lw*1.6, p.accent)
    d.arc([s*0.30, s*0.44, s*0.70, s*0.78], start=200, end=340,
          fill=_rgba(p.accent2, 230), width=max(1, lw//2))


for _n, _fn in {
    "cam_face": _cam_25d("zig"), "cam_contour2d": _cam_25d("loop"),
    "cam_pocket2d": _cam_25d("spiral"), "cam_adaptive2d": _cam_25d("spiral"),
    "cam_slot": _cam_25d("slot"), "cam_chamfer2d": _cam_25d("loop"),
    "cam_engrave": _cam_25d("vee"),
    "cam_drill": _cam_drill_glyph(False), "cam_thread_mill": _cam_drill_glyph(True),
    "cam_parallel3d": _cam_3d("raster"), "cam_waterline3d": _cam_3d("rings"),
    "cam_scallop3d": _cam_3d("wave"), "cam_pocket3d": _cam_3d("bowl"),
    "cam_turn_rough": _cam_turn("rough"), "cam_turn_finish": _cam_turn("finish"),
    "cam_turn_face": _cam_turn("face"), "cam_turn_part": _cam_turn("part"),
    "cam_cut2d": _cam_cut, "cam_probe": _cam_probe,
}.items():
    _REGISTRY[_n] = _fn


# ---------------------------------------------------------------------------
# Parts library (GUI V2) — taxonomy category glyphs + panel chrome.
# ---------------------------------------------------------------------------

def _hexagon(cx, cy, r, rot=-90):
    return [(cx + r * math.cos(math.radians(rot + i * 60)),
             cy + r * math.sin(math.radians(rot + i * 60))) for i in range(6)]


@icon("parts_all")
def draw_parts_all(d, cell, p):
    """Parts bin: open crate with a bolt, washer and cube inside."""
    s = cell * SS
    lw = stroke_width(cell)
    # crate
    d.polygon([(s*0.14, s*0.50), (s*0.86, s*0.50), (s*0.80, s*0.86), (s*0.20, s*0.86)],
              fill=_rgba(p.stroke, 26), outline=p.stroke)
    d.line([(s*0.14, s*0.50), (s*0.86, s*0.50), (s*0.80, s*0.86), (s*0.20, s*0.86),
            (s*0.14, s*0.50)], fill=p.stroke, width=lw)
    # cube part
    shaded_prism(d, s, s*0.22, s*0.30, s*0.16, s*0.14, s*0.05, s*0.04, p)
    # washer part
    circle(d, s*0.56, s*0.32, s*0.10, outline=p.accent, lw=lw)
    circle(d, s*0.56, s*0.32, s*0.04, outline=p.accent, lw=max(1, lw//2))
    # bolt part
    pts = _hexagon(s*0.80, s*0.26, s*0.08)
    d.polygon(pts, fill=_rgba(p.stroke, 30))
    d.line(pts + [pts[0]], fill=p.stroke, width=max(1, lw//2))
    d.line([(s*0.80, s*0.34), (s*0.80, s*0.46)], fill=p.stroke, width=lw)


@icon("cat_fasteners")
def draw_cat_fasteners(d, cell, p):
    """Hex bolt: hexagonal head + threaded shank."""
    s = cell * SS
    lw = stroke_width(cell)
    cx = s * 0.5
    pts = _hexagon(cx, s*0.28, s*0.17)
    d.polygon(pts, fill=_rgba(p.stroke, 30))
    d.line(pts + [pts[0]], fill=p.stroke, width=lw)
    # shank
    d.rectangle([cx - s*0.06, s*0.42, cx + s*0.06, s*0.84], fill=_rgba(p.accent, 90),
                outline=p.accent, width=lw)
    # threads
    for i in range(4):
        y = s * (0.50 + i * 0.09)
        d.line([(cx - s*0.10, y), (cx + s*0.10, y + s*0.03)], fill=p.stroke,
               width=max(1, lw//2))


@icon("cat_bearings")
def draw_cat_bearings(d, cell, p):
    """Ball bearing: outer + inner race with rolling balls between."""
    s = cell * SS
    lw = stroke_width(cell)
    cx, cy = s*0.5, s*0.5
    circle(d, cx, cy, s*0.38, outline=p.stroke, lw=lw)
    circle(d, cx, cy, s*0.15, outline=p.stroke, lw=lw)
    circle(d, cx, cy, s*0.15, fill=_rgba(p.stroke, 22))
    for i in range(7):
        a = math.radians(i * 360 / 7 - 90)
        filled_circle(d, cx + s*0.265*math.cos(a), cy + s*0.265*math.sin(a),
                      s*0.045, p.accent)


@icon("cat_stock_metal")
def draw_cat_stock_metal(d, cell, p):
    """Raw stock: round tube + square bar + sheet stack."""
    s = cell * SS
    lw = stroke_width(cell)
    # sheet stack (bottom)
    for i in range(2):
        y = s*0.70 + i*s*0.09
        d.polygon([(s*0.16, y), (s*0.62, y), (s*0.84, y + s*0.07), (s*0.38, y + s*0.07)],
                  fill=_rgba(p.stroke, 26), outline=p.stroke)
    # square bar (middle-left, angled extrusion)
    shaded_prism(d, s, s*0.16, s*0.36, s*0.22, s*0.18, s*0.16, s*0.10, p)
    # round tube (right)
    d.ellipse([s*0.58, s*0.14, s*0.86, s*0.40], outline=p.accent, width=lw)
    d.ellipse([s*0.66, s*0.21, s*0.78, s*0.33], outline=p.accent, width=max(1, lw//2))


@icon("cat_drivetrain")
def draw_cat_drivetrain(d, cell, p):
    """Sprocket with a chain link."""
    s = cell * SS
    lw = stroke_width(cell)
    cx, cy = s*0.42, s*0.46
    r = s*0.26
    # teeth
    for i in range(8):
        a = math.radians(i * 45)
        d.line([(cx + r*math.cos(a), cy + r*math.sin(a)),
                (cx + (r + s*0.08)*math.cos(a), cy + (r + s*0.08)*math.sin(a))],
               fill=p.stroke, width=lw)
    circle(d, cx, cy, r, outline=p.stroke, lw=lw)
    circle(d, cx, cy, s*0.08, outline=p.stroke, lw=lw)
    # chain link (accent)
    d.rounded_rectangle([s*0.58, s*0.62, s*0.90, s*0.80], radius=int(s*0.09),
                        outline=p.accent, width=lw)
    d.rounded_rectangle([s*0.66, s*0.67, s*0.82, s*0.75], radius=int(s*0.04),
                        outline=p.accent, width=max(1, lw//2))


@icon("cat_wheels")
def draw_cat_wheels(d, cell, p):
    """Wheel: tire + rim + hub with spokes."""
    s = cell * SS
    lw = stroke_width(cell)
    cx, cy = s*0.5, s*0.5
    circle(d, cx, cy, s*0.38, fill=_rgba(p.stroke, 26), outline=p.stroke, lw=lw)
    circle(d, cx, cy, s*0.24, outline=p.accent, lw=lw)
    circle(d, cx, cy, s*0.06, fill=_rgba(p.accent, 160))
    for i in range(5):
        a = math.radians(i * 72 - 90)
        d.line([(cx + s*0.07*math.cos(a), cy + s*0.07*math.sin(a)),
                (cx + s*0.23*math.cos(a), cy + s*0.23*math.sin(a))],
               fill=p.accent, width=max(1, lw//2))


@icon("cat_tools")
def draw_cat_tools(d, cell, p):
    """Open-end wrench, diagonal."""
    s = cell * SS
    lw = stroke_width(cell)
    # handle
    d.line([(s*0.30, s*0.70), (s*0.72, s*0.28)], fill=p.stroke, width=lw*3)
    # head: open jaw (C shape) at top-right
    d.arc([s*0.58, s*0.10, s*0.92, s*0.44], start=80, end=340, fill=p.stroke, width=lw*2)
    # ring end at bottom-left
    circle(d, s*0.26, s*0.74, s*0.11, outline=p.stroke, lw=lw*2)
    circle(d, s*0.26, s*0.74, s*0.045, fill=_rgba(p.accent, 200))


@icon("cat_cutting_tools")
def draw_cat_cutting_tools(d, cell, p):
    """End mill silhouette: shank + fluted cutting end."""
    s = cell * SS
    lw = stroke_width(cell)
    cx = s*0.5
    # shank
    d.rectangle([cx - s*0.09, s*0.10, cx + s*0.09, s*0.42], fill=_rgba(p.stroke, 40),
                outline=p.stroke, width=lw)
    # cutter body
    d.rectangle([cx - s*0.13, s*0.42, cx + s*0.13, s*0.80], fill=_rgba(p.accent, 60),
                outline=p.accent, width=lw)
    # helical flutes
    for i in range(3):
        x0 = cx - s*0.13 + i * s*0.09
        d.line([(x0, s*0.80), (x0 + s*0.08, s*0.46)], fill=p.stroke, width=max(1, lw//2))
    # cutting tip corners
    d.line([(cx - s*0.13, s*0.80), (cx - s*0.06, s*0.88), (cx + s*0.06, s*0.88),
            (cx + s*0.13, s*0.80)], fill=p.accent, width=lw)


@icon("cat_electronics")
def draw_cat_electronics(d, cell, p):
    """IC chip with pins + pin-1 dot."""
    s = cell * SS
    lw = stroke_width(cell)
    d.rounded_rectangle([s*0.28, s*0.28, s*0.72, s*0.72], radius=int(s*0.04),
                        fill=_rgba(p.stroke, 30), outline=p.stroke, width=lw)
    for i in range(3):
        t = s*(0.36 + i*0.14)
        d.line([(s*0.14, t), (s*0.28, t)], fill=p.stroke, width=lw)
        d.line([(s*0.72, t), (s*0.86, t)], fill=p.stroke, width=lw)
        d.line([(t, s*0.14), (t, s*0.28)], fill=p.stroke, width=lw)
        d.line([(t, s*0.72), (t, s*0.86)], fill=p.stroke, width=lw)
    filled_circle(d, s*0.37, s*0.37, s*0.035, p.accent)


@icon("cat_hydraulics")
def draw_cat_hydraulics(d, cell, p):
    """Hydraulic cylinder: barrel + extended rod + ports."""
    s = cell * SS
    lw = stroke_width(cell)
    d.rounded_rectangle([s*0.10, s*0.36, s*0.58, s*0.64], radius=int(s*0.03),
                        fill=_rgba(p.stroke, 30), outline=p.stroke, width=lw)
    # piston line
    d.line([(s*0.42, s*0.36), (s*0.42, s*0.64)], fill=p.stroke, width=lw)
    # rod
    d.rectangle([s*0.58, s*0.46, s*0.86, s*0.54], fill=_rgba(p.accent, 120),
                outline=p.accent, width=max(1, lw//2))
    # rod eye
    circle(d, s*0.88, s*0.50, s*0.05, outline=p.accent, lw=lw)
    # ports
    d.line([(s*0.18, s*0.36), (s*0.18, s*0.24)], fill=p.accent, width=lw)
    d.line([(s*0.50, s*0.36), (s*0.50, s*0.24)], fill=p.accent, width=lw)


@icon("cat_hardware")
def draw_cat_hardware(d, cell, p):
    """Hex nut + washer pair."""
    s = cell * SS
    lw = stroke_width(cell)
    # nut (upper-left)
    pts = _hexagon(s*0.38, s*0.38, s*0.22)
    d.polygon(pts, fill=_rgba(p.stroke, 30))
    d.line(pts + [pts[0]], fill=p.stroke, width=lw)
    circle(d, s*0.38, s*0.38, s*0.09, outline=p.stroke, lw=lw)
    # washer (lower-right, accent)
    circle(d, s*0.68, s*0.68, s*0.18, fill=_rgba(p.accent, 50), outline=p.accent, lw=lw)
    circle(d, s*0.68, s*0.68, s*0.07, outline=p.accent, lw=max(1, lw//2))


@icon("vendor_globe")
def draw_vendor_globe(d, cell, p):
    """Globe: sphere with meridians + equator."""
    s = cell * SS
    lw = stroke_width(cell)
    cx, cy, r = s*0.5, s*0.5, s*0.34
    circle(d, cx, cy, r, outline=p.stroke, lw=lw)
    # equator + tropics
    d.line([(cx - r, cy), (cx + r, cy)], fill=p.stroke, width=max(1, lw//2))
    d.arc([cx - r, cy - r*0.55, cx + r, cy + r*1.45], start=215, end=325,
          fill=_rgba(p.stroke, 140), width=max(1, lw//2))
    d.arc([cx - r, cy - r*1.45, cx + r, cy + r*0.55], start=35, end=145,
          fill=_rgba(p.stroke, 140), width=max(1, lw//2))
    # meridians (accent)
    d.ellipse([cx - r*0.45, cy - r, cx + r*0.45, cy + r], outline=p.accent,
              width=max(1, lw//2))
    d.line([(cx, cy - r), (cx, cy + r)], fill=p.accent, width=max(1, lw//2))


@icon("browser_panel")
def draw_browser_panel(d, cell, p):
    """Embedded browser window: chrome bar + traffic dots + page."""
    s = cell * SS
    lw = stroke_width(cell)
    d.rounded_rectangle([s*0.12, s*0.16, s*0.88, s*0.84], radius=int(s*0.05),
                        outline=p.stroke, width=lw)
    d.line([(s*0.12, s*0.34), (s*0.88, s*0.34)], fill=p.stroke, width=lw)
    for i in range(3):
        filled_circle(d, s*(0.21 + i*0.09), s*0.25, s*0.028,
                      p.accent if i == 0 else p.stroke)
    # address pill
    d.rounded_rectangle([s*0.48, s*0.21, s*0.82, s*0.29], radius=int(s*0.035),
                        outline=_rgba(p.stroke, 140), width=max(1, lw//2))
    # page content lines
    d.line([(s*0.20, s*0.46), (s*0.80, s*0.46)], fill=_rgba(p.accent, 200), width=lw)
    d.line([(s*0.20, s*0.58), (s*0.70, s*0.58)], fill=_rgba(p.stroke, 140),
           width=max(1, lw//2))
    d.line([(s*0.20, s*0.68), (s*0.74, s*0.68)], fill=_rgba(p.stroke, 140),
           width=max(1, lw//2))


@icon("filter_funnel")
def draw_filter_funnel(d, cell, p):
    """Filter funnel with a drop."""
    s = cell * SS
    lw = stroke_width(cell)
    d.polygon([(s*0.16, s*0.18), (s*0.84, s*0.18), (s*0.58, s*0.50), (s*0.58, s*0.78),
               (s*0.42, s*0.70), (s*0.42, s*0.50)],
              fill=_rgba(p.stroke, 26))
    d.line([(s*0.16, s*0.18), (s*0.84, s*0.18), (s*0.58, s*0.50), (s*0.58, s*0.78),
            (s*0.42, s*0.70), (s*0.42, s*0.50), (s*0.16, s*0.18)],
           fill=p.stroke, width=lw)
    filled_circle(d, s*0.50, s*0.88, s*0.045, p.accent)


@icon("health_check")
def draw_health_check(d, cell, p):
    """Service health: pulse/ECG line in a rounded frame."""
    s = cell * SS
    lw = stroke_width(cell)
    d.rounded_rectangle([s*0.10, s*0.22, s*0.90, s*0.78], radius=int(s*0.06),
                        outline=p.stroke, width=lw)
    polyline(d, [(s*0.16, s*0.52), (s*0.34, s*0.52), (s*0.42, s*0.34),
                 (s*0.52, s*0.66), (s*0.60, s*0.44), (s*0.66, s*0.52),
                 (s*0.84, s*0.52)], p.accent, lw)
    filled_circle(d, s*0.84, s*0.52, max(1, lw), p.accent)


# ---- PLM Project Hub -------------------------------------------------------

@icon("plan_board")
def draw_plan_board(d, cell, p):
    """Kanban board: framed panel with three columns of cards."""
    s = cell * SS
    lw = stroke_width(cell)
    d.rounded_rectangle([s*0.13, s*0.20, s*0.87, s*0.80], radius=int(s*0.07),
                        outline=p.stroke, width=lw)
    for fx in (0.377, 0.623):
        d.line([(s*fx, s*0.22), (s*fx, s*0.78)], fill=_rgba(p.stroke, 150),
               width=max(1, lw // 2))

    def card(x0, x1, y0, y1, accent=False):
        col = p.accent if accent else p.stroke
        d.rounded_rectangle([s*x0, s*y0, s*x1, s*y1], radius=int(s*0.028),
                            fill=_rgba(col, 95), outline=col, width=max(1, lw // 2))
    card(0.175, 0.345, 0.28, 0.40, accent=True)
    card(0.175, 0.345, 0.47, 0.59)
    card(0.425, 0.595, 0.28, 0.40)
    card(0.655, 0.825, 0.28, 0.40, accent=True)


@icon("git")
def draw_git(d, cell, p):
    """Git branch glyph: a trunk node forking to a branch node (three nodes)."""
    s = cell * SS
    lw = stroke_width(cell)
    r = s * 0.085
    # trunk (left column): two nodes joined vertically
    tx = s * 0.32
    top = (tx, s * 0.24)
    bot = (tx, s * 0.76)
    # branch node (right column, mid height)
    bx, by = s * 0.68, s * 0.42
    # trunk line
    d.line([top, bot], fill=p.stroke, width=lw)
    # fork: from a point on the trunk up-and-over to the branch node
    fork = (tx, s * 0.50)
    polyline(d, [fork, (tx, by), (bx, by)], p.accent, lw)
    # nodes
    filled_circle(d, top[0], top[1], r, p.stroke)
    filled_circle(d, bot[0], bot[1], r, p.stroke)
    filled_circle(d, bx, by, r, p.accent)


@icon("issue_bug")
def draw_issue_bug(d, cell, p):
    """A bug — the 'issue' glyph: rounded body, head, antennae and legs."""
    s = cell * SS
    lw = stroke_width(cell)
    cx, cy = s*0.5, s*0.55
    bw, bh = s*0.17, s*0.24
    d.ellipse([cx-bw, cy-bh, cx+bw, cy+bh], outline=p.accent, width=lw,
              fill=_rgba(p.accent, 60))
    d.line([(cx, cy-bh*0.85), (cx, cy+bh*0.9)], fill=p.accent, width=max(1, lw // 2))
    filled_circle(d, cx, cy-s*0.30, s*0.075, p.stroke)
    polyline(d, [(cx-s*0.05, cy-s*0.36), (cx-s*0.13, cy-s*0.45)], p.stroke, max(1, lw // 2))
    polyline(d, [(cx+s*0.05, cy-s*0.36), (cx+s*0.13, cy-s*0.45)], p.stroke, max(1, lw // 2))
    for yy in (0.46, 0.56, 0.66):
        d.line([(cx-bw*0.9, s*yy), (cx-s*0.32, s*(yy-0.045))], fill=p.stroke, width=max(1, lw // 2))
        d.line([(cx+bw*0.9, s*yy), (cx+s*0.32, s*(yy-0.045))], fill=p.stroke, width=max(1, lw // 2))


@icon("milestone_flag")
def draw_milestone_flag(d, cell, p):
    """A pennant flag on a pole — a milestone marker."""
    s = cell * SS
    lw = stroke_width(cell)
    px = s*0.31
    d.line([(px, s*0.15), (px, s*0.87)], fill=p.stroke, width=lw)
    filled_circle(d, px, s*0.87, max(1, lw), p.stroke)
    flag = [(px, s*0.17), (px+s*0.45, s*0.31), (px, s*0.45)]
    d.polygon(flag, fill=_rgba(p.accent, 115))
    d.line(flag + [flag[0]], fill=p.accent, width=lw)


@icon("team")
def draw_team(d, cell, p):
    """Two people — the team / sharing glyph."""
    s = cell * SS
    lw = stroke_width(cell)
    hw = max(1, lw // 2)
    # back person (accent, offset right)
    bx = s*0.63
    filled_circle(d, bx, s*0.37, s*0.10, _rgba(p.accent, 130))
    d.pieslice([bx-s*0.16, s*0.52, bx+s*0.16, s*0.86], start=180, end=360,
               fill=_rgba(p.accent, 95), outline=p.accent, width=hw)
    # front person (stroke)
    fx = s*0.40
    d.ellipse([fx-s*0.13, s*0.40-s*0.13, fx+s*0.13, s*0.40+s*0.13],
              outline=p.stroke, width=lw, fill=_rgba(p.stroke, 30))
    d.pieslice([fx-s*0.18, s*0.55, fx+s*0.18, s*0.93], start=180, end=360,
               fill=_rgba(p.stroke, 30), outline=p.stroke, width=lw)



@icon("paperclip")
def draw_paperclip(d, cell, p):
    """Crisp diagonal paperclip for the copilot composer attach button.

    Classic open-loop clip: outer wire runs from a free top tip, down one long
    side, around the bottom U, up the other side, and over a tighter top U that
    leaves the free tip open. An inner parallel loop nested inside gives the
    classic double-wire read. Drawn at FULL cell size (not half-slot) so it
    stays legible at 20 px toolbar height in both themes — monochrome stroke so
    the SDF path tints with the icon/text role.
    """
    s = cell * SS
    lw = stroke_width(cell)
    # Diagonal orientation (~-35°) fills the slot better than axis-aligned.
    ang = math.radians(-35.0)
    ca, sa = math.cos(ang), math.sin(ang)
    cx, cy = s * 0.50, s * 0.50

    def xf(lx, ly):
        # Local coords: +x along the clip length (tip toward +ly?); rotate+center.
        return (cx + lx * ca - ly * sa, cy + lx * sa + ly * ca)

    # Geometry in local space (full-slot scale). Length along x, width along y.
    L = s * 0.34          # half-length of the straight sides
    Ro = s * 0.155        # outer loop radius
    Ri = s * 0.075        # inner loop radius (double-wire gap)
    tip = L + Ro * 0.55   # free tip extends past the top U

    def arc_pts(ox, oy, r, a0, a1, n=18):
        pts = []
        for i in range(n + 1):
            t = a0 + (a1 - a0) * (i / n)
            pts.append(xf(ox + r * math.cos(t), oy + r * math.sin(t)))
        return pts

    # Outer path, tip-free at top-right of local frame:
    # start free tip (right side, above top U) -> down outer right -> bottom U
    # -> up outer left -> top U (open, doesn't close to tip).
    outer = []
    outer.append(xf(tip, Ro))                         # free tip
    outer.append(xf(L, Ro))                           # top of right straight
    outer.append(xf(-L, Ro))                          # bottom of right straight
    outer += arc_pts(-L, 0.0, Ro, math.pi * 0.5, math.pi * 1.5)  # bottom U
    outer.append(xf(L, -Ro))                          # top of left straight
    # top U: from left side over to just short of free tip on the right
    outer += arc_pts(L, 0.0, Ro, -math.pi * 0.5, math.pi * 0.35)
    polyline(d, outer, p.stroke, lw)

    # Inner nested loop (closed oval) — the classic double-wire.
    Li = L * 0.55
    inner = []
    inner.append(xf(Li, Ri))
    inner.append(xf(-Li, Ri))
    inner += arc_pts(-Li, 0.0, Ri, math.pi * 0.5, math.pi * 1.5)
    inner.append(xf(Li, -Ri))
    inner += arc_pts(Li, 0.0, Ri, -math.pi * 0.5, math.pi * 0.5)
    polyline(d, inner, p.stroke, max(1, int(lw * 0.85)))


@icon("context_cube")
def draw_context_cube(d, cell, p):
    """Filled isometric cube for chat context-ref chips (status-tintable).

    Same 2:1 _iso_pts geometry as brand_cube. Top face is near-white / high-alpha
    so ImGui multiply-tint can recolor the chip by status; left/right faces are
    slightly darker parallelograms. Hex silhouette only — no circle/ellipse.
    Simplified notch at >=32 px; plain cube at 20 px for legibility.
    """
    s = cell * SS
    lw = max(1, int(stroke_width(cell) * 0.85))
    r = s * 0.38
    cx, cy = s * 0.50, s * 0.52
    pt = _iso_pts(cx, cy, r)
    # High-alpha light top so multiply-tint drives status color cleanly.
    top_fill = (255, 255, 255, 230) if sum(p.stroke[:3]) > 400 else _rgba(p.stroke, 210)
    # Optional simplified notch only when the cell has room (skip smear at 20 px).
    use_notch = cell > 20
    if use_notch:
        q = _iso_pts(cx, cy - r + r * 0.5, r * 0.5)
        d.polygon([pt["lt"], q["lt"], q["c"], q["rt"], pt["rt"], pt["c"]], fill=top_fill)
    else:
        d.polygon([pt["lt"], pt["top"], pt["rt"], pt["c"]], fill=top_fill)
    # Left darker, right mid — shaded parallelogram faces.
    d.polygon([pt["lt"], pt["c"], pt["bot"], pt["lb"]], fill=_rgba(p.stroke, 55))
    d.polygon([pt["rt"], pt["c"], pt["bot"], pt["rb"]], fill=_rgba(p.stroke, 90))
    if use_notch:
        d.polygon([q["lt"], q["top"], q["rt"], q["c"]], fill=_rgba((255, 255, 255), 90))
        d.polygon([q["lt"], q["c"], q["bot"], q["lb"]], fill=_rgba(p.stroke, 28))
        d.polygon([q["rt"], q["c"], q["bot"], q["rb"]], fill=_rgba(p.stroke, 40))
    # Hex outer silhouette + interior Y-junction.
    edges = [(pt["rt"], pt["rb"]), (pt["rb"], pt["bot"]), (pt["bot"], pt["lb"]),
             (pt["lb"], pt["lt"]), (pt["lt"], pt["c"]), (pt["c"], pt["rt"]),
             (pt["c"], pt["bot"])]
    if use_notch:
        edges += [(pt["lt"], q["lt"]), (q["rt"], pt["rt"])]
    else:
        edges += [(pt["lt"], pt["top"]), (pt["top"], pt["rt"])]
    for a, b in edges:
        d.line([a, b], fill=p.stroke, width=lw)
    if use_notch:
        nlw = max(1, int(lw * 0.7))
        for a, b in [(q["lt"], q["c"]), (q["c"], q["rt"]), (q["c"], q["bot"]),
                     (q["lt"], q["lb"]), (q["rt"], q["rb"]), (q["lb"], q["bot"]),
                     (q["rb"], q["bot"]), (q["lt"], q["top"]), (q["rt"], q["top"])]:
            d.line([a, b], fill=_rgba(p.stroke, 180), width=nlw)

# ---------------------------------------------------------------------------
# Copilot Artifacts + PLM Graph
# ---------------------------------------------------------------------------
# Every file_* glyph shares one page silhouette so a mixed artifact list scans
# as a single family; only the motif inked into the lower two thirds differs.
# The 20 px cell is the binding constraint (a page interior is ~10 px wide
# there), so each motif is one bold shape rather than fine detail.

def _artifact_page(d, s, p, lw, fold=0.24):
    """Portrait sheet with a folded top-right corner — the shared file_* body.

    Returns the interior box (x0, y0, x1, y1) below the fold that callers ink
    their kind motif into."""
    x0, x1 = s * 0.15, s * 0.85
    y0, y1 = s * 0.06, s * 0.94
    f = s * fold
    body = [(x0, y0), (x1 - f, y0), (x1, y0 + f), (x1, y1), (x0, y1)]
    d.polygon(body, fill=_rgba(p.stroke, 18))
    d.line(body + [body[0]], fill=p.stroke, width=lw)
    d.polygon([(x1 - f, y0), (x1 - f, y0 + f), (x1, y0 + f)], fill=_rgba(p.accent, 60))
    d.line([(x1 - f, y0), (x1 - f, y0 + f), (x1, y0 + f)], fill=p.stroke, width=lw)
    return x0, y0 + f, x1, y1


@icon("file_image")
def draw_file_image(d, cell, p):
    """Page + raster motif: a sun over a two-peak ridge (PNG/JPG/render)."""
    s = cell * SS
    lw = stroke_width(cell)
    _artifact_page(d, s, p, lw)
    filled_circle(d, s * 0.35, s * 0.55, s * 0.065, p.warn)
    ridge = [(s * 0.23, s * 0.85), (s * 0.42, s * 0.60), (s * 0.54, s * 0.73),
             (s * 0.64, s * 0.60), (s * 0.77, s * 0.85)]
    d.polygon(ridge, fill=_rgba(p.accent, 165))
    polyline(d, ridge, p.accent, max(1, int(lw * 0.8)))


@icon("file_mesh")
def draw_file_mesh(d, cell, p):
    """Page + a subdivided wireframe triangle (STL/OBJ/PLY).

    Edges only, no face fill — that open read is the whole distinction from
    file_step's opaque solid once both shrink to 20 px."""
    s = cell * SS
    lw = stroke_width(cell)
    mlw = max(1, int(lw * 0.8))
    _artifact_page(d, s, p, lw)
    a, b, c = (s * 0.24, s * 0.85), (s * 0.50, s * 0.42), (s * 0.76, s * 0.85)
    mid = lambda u, v: ((u[0] + v[0]) * 0.5, (u[1] + v[1]) * 0.5)
    ab, bc, ca = mid(a, b), mid(b, c), mid(c, a)
    for u, v in ((a, b), (b, c), (c, a), (ab, bc), (bc, ca), (ca, ab)):
        d.line([u, v], fill=p.accent, width=mlw)
    for pt in (a, b, c):
        filled_circle(d, pt[0], pt[1], mlw * 0.75, p.stroke)


@icon("file_step")
def draw_file_step(d, cell, p):
    """Page + an opaque shaded cube (STEP/IGES/SLDPRT) — B-rep solid.

    Hand-rolled rather than shaded_prism: that helper's face alphas (36/54/72)
    are transparent enough that the cube reads as a wireframe box, which is
    exactly file_mesh. Here the three faces are near-opaque and stepped in
    value, so solid-vs-wireframe survives the 20 px downsample."""
    s = cell * SS
    lw = stroke_width(cell)
    clw = max(1, int(lw * 0.8))
    _artifact_page(d, s, p, lw)
    x, y, w, h = s * 0.28, s * 0.62, s * 0.32, s * 0.22
    dx, dy = s * 0.14, s * 0.11
    top = [(x, y), (x + dx, y - dy), (x + w + dx, y - dy), (x + w, y)]
    side = [(x + w, y), (x + w + dx, y - dy), (x + w + dx, y + h - dy), (x + w, y + h)]
    front = [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]
    d.polygon(front, fill=_rgba(p.accent, 235))
    d.polygon(side, fill=_rgba(p.accent, 150))
    d.polygon(top, fill=_rgba(p.accent, 100))
    for pts in (front, side, top):
        d.line(pts + [pts[0]], fill=p.accent, width=clw)


@icon("file_doc")
def draw_file_doc(d, cell, p):
    """Page + text rules with a short accent title line (PDF/MD/TXT)."""
    s = cell * SS
    lw = stroke_width(cell)
    rlw = max(1, int(lw * 0.8))
    _artifact_page(d, s, p, lw)
    d.line([(s * 0.26, s * 0.47), (s * 0.56, s * 0.47)], fill=p.accent, width=lw)
    for y, x1 in ((0.65, 0.75), (0.82, 0.64)):
        d.line([(s * 0.26, s * y), (s * x1, s * y)], fill=_rgba(p.stroke, 195), width=rlw)


@icon("file_data")
def draw_file_data(d, cell, p):
    """Page + a table grid with a filled header band (JSON/CSV/XML/YAML).

    A grid survives the downsample where braces collapse into two blobs."""
    s = cell * SS
    lw = stroke_width(cell)
    glw = max(1, int(lw * 0.8))
    _artifact_page(d, s, p, lw)
    x0, x1, y0, y1 = s * 0.26, s * 0.74, s * 0.46, s * 0.85
    hdr = y0 + (y1 - y0) * 0.34
    d.rectangle([x0, y0, x1, hdr], fill=_rgba(p.accent, 165))
    d.rectangle([x0, y0, x1, y1], outline=p.accent, width=glw)
    d.line([(x0, hdr), (x1, hdr)], fill=p.accent, width=glw)
    d.line([((x0 + x1) * 0.5, hdr), ((x0 + x1) * 0.5, y1)], fill=p.accent, width=glw)


@icon("file_code")
def draw_file_code(d, cell, p):
    """Page + bold angle brackets — source / script artifacts."""
    s = cell * SS
    lw = stroke_width(cell)
    clw = max(2, int(lw * 1.05))
    _artifact_page(d, s, p, lw)
    polyline(d, [(s * 0.43, s * 0.48), (s * 0.27, s * 0.66), (s * 0.43, s * 0.84)],
             p.accent, clw)
    polyline(d, [(s * 0.57, s * 0.48), (s * 0.73, s * 0.66), (s * 0.57, s * 0.84)],
             p.accent, clw)


@icon("file_archive")
def draw_file_archive(d, cell, p):
    """Page + two stacked crates with a strap.

    A zipper's teeth mush into a grey bar below ~24 px, so the archive reads as
    boxed contents instead — same idea, one size class coarser."""
    s = cell * SS
    lw = stroke_width(cell)
    blw = max(1, int(lw * 0.8))
    _artifact_page(d, s, p, lw)
    d.rectangle([s * 0.22, s * 0.63, s * 0.78, s * 0.87],
                fill=_rgba(p.stroke, 60), outline=p.stroke, width=blw)
    d.rectangle([s * 0.32, s * 0.41, s * 0.68, s * 0.63],
                fill=_rgba(p.accent, 160), outline=p.accent, width=blw)
    # One strap running down both crates ties them into a single packed stack.
    d.line([(s * 0.50, s * 0.41), (s * 0.50, s * 0.87)], fill=_rgba(p.stroke, 225),
           width=blw)


@icon("file_generic")
def draw_file_generic(d, cell, p):
    """Plain folded page — the fallback for an unrecognised extension.

    Deliberately empty: an unknown kind must not imply one. The fold is
    oversized so the blank body still reads as a sheet, not a rectangle."""
    s = cell * SS
    _artifact_page(d, s, p, stroke_width(cell), fold=0.31)


@icon("artifacts")
def draw_artifacts(d, cell, p):
    """Stacked sheets with a folded corner — the Copilot Artifacts tab header.

    Supersedes that panel's misuse of `team` (a multi-person org glyph). Only
    two sheets: at 20 px a third layer lands inside its neighbour's stroke and
    the stack turns into a grey block."""
    s = cell * SS
    lw = stroke_width(cell)
    blw = max(1, int(lw * 0.85))
    back = [(s * 0.13, s * 0.08), (s * 0.61, s * 0.08), (s * 0.61, s * 0.70),
            (s * 0.13, s * 0.70)]
    d.line(back + [back[0]], fill=_rgba(p.stroke, 150), width=blw)
    f = s * 0.20
    fx0, fy0, fx1, fy1 = s * 0.31, s * 0.28, s * 0.87, s * 0.92
    front = [(fx0, fy0), (fx1 - f, fy0), (fx1, fy0 + f), (fx1, fy1), (fx0, fy1)]
    d.polygon(front, fill=_rgba(p.stroke, 30))
    d.line(front + [front[0]], fill=p.stroke, width=lw)
    d.polygon([(fx1 - f, fy0), (fx1 - f, fy0 + f), (fx1, fy0 + f)],
              fill=_rgba(p.accent, 90))
    d.line([(fx1 - f, fy0), (fx1 - f, fy0 + f), (fx1, fy0 + f)], fill=p.stroke, width=lw)
    d.line([(s * 0.40, s * 0.72), (s * 0.70, s * 0.72)], fill=p.accent, width=blw)


@icon("orbit_auto")
def draw_orbit_auto(d, cell, p):
    """Auto-rotate: a body inside a tilted orbit carrying a motion arrowhead.

    The orbit is deliberately FLATTER than the body is wide (ry < r_sphere), so
    the near half crosses in FRONT of the body and the far half is clipped
    behind it. A ring tangent to the sphere instead reads as an eye — which is
    already hide_show_eye."""
    s = cell * SS
    lw = stroke_width(cell)
    olw = max(1, int(lw * 0.85))
    cx, cy = s * 0.50, s * 0.52
    rx, ry = s * 0.44, s * 0.115
    rs = s * 0.205
    rot = math.radians(-16.0)
    ca, sa = math.cos(rot), math.sin(rot)

    def ep(t):
        x, y = rx * math.cos(t), ry * math.sin(t)
        return (cx + x * ca - y * sa, cy + x * sa + y * ca)

    n = 96
    near, far = [], []
    for i in range(n):
        t0, t1 = math.tau * i / n, math.tau * (i + 1) / n
        seg = (ep(t0), ep(t1))
        mx = ((seg[0][0] + seg[1][0]) * 0.5, (seg[0][1] + seg[1][1]) * 0.5)
        if math.sin((t0 + t1) * 0.5) < 0.0:          # upper = far side
            if math.hypot(mx[0] - cx, mx[1] - cy) > rs * 1.04:
                far.append(seg)
        else:
            near.append(seg)
    for a, b in far:
        d.line([a, b], fill=_rgba(p.accent, 150), width=olw)
    filled_circle(d, cx, cy, rs, _rgba(p.stroke, 85))
    circle(d, cx, cy, rs, outline=p.stroke, lw=olw)
    for a, b in near:
        d.line([a, b], fill=p.accent, width=olw)
    # Motion arrowhead riding the near half, pointing along travel.
    ta = math.pi * 0.80
    tip = ep(ta)
    tan = (ep(ta + 0.06)[0] - ep(ta - 0.06)[0], ep(ta + 0.06)[1] - ep(ta - 0.06)[1])
    tl = math.hypot(*tan) or 1.0
    ux, uy = tan[0] / tl, tan[1] / tl
    hl, hw = s * 0.16, s * 0.095
    d.polygon([tip,
               (tip[0] - ux * hl - uy * hw, tip[1] - uy * hl + ux * hw),
               (tip[0] - ux * hl + uy * hw, tip[1] - uy * hl - ux * hw)],
              fill=p.accent)


@icon("depth_3d")
def draw_depth_3d(d, cell, p):
    """Depth cue: three planes receding with perspective — the near plane wide,
    accent and opaque, the far ones narrower and dimmer."""
    s = cell * SS
    lw = stroke_width(cell)
    plw = max(1, int(lw * 0.7))
    cx = s * 0.50
    h = s * 0.055
    # Far-plane alphas bottom out around 110: below that the slate light-theme
    # stroke on a light panel disappears at 20 px and only two planes read.
    for yc, hw, alpha, col in ((0.26, 0.19, 115, p.stroke),
                               (0.51, 0.28, 165, p.stroke),
                               (0.78, 0.37, 210, p.accent)):
        y = s * yc
        w = s * hw
        sk = w * 0.34
        pts = [(cx - w + sk, y - h), (cx + w + sk, y - h),
               (cx + w - sk, y + h), (cx - w - sk, y + h)]
        d.polygon(pts, fill=_rgba(col, alpha))
        d.line(pts + [pts[0]], fill=_rgba(col, min(255, alpha + 65)), width=plw)


@icon("legend")
def draw_legend(d, cell, p):
    """Legend key: three colour dots, each labelled by a short rule."""
    s = cell * SS
    lw = stroke_width(cell)
    r = s * 0.078
    for y, x1, col in ((0.26, 0.86, p.accent), (0.50, 0.80, p.warn),
                       (0.74, 0.72, p.stroke)):
        filled_circle(d, s * 0.20, s * y, r, col)
        d.line([(s * 0.38, s * y), (s * x1, s * y)], fill=_rgba(p.stroke, 195), width=lw)


@icon("isolate")
def draw_isolate(d, cell, p):
    """Isolate subtree: the kept node sits filled inside a focus ring while its
    neighbours drop to dim outlines on dim links."""
    s = cell * SS
    lw = stroke_width(cell)
    dlw = max(1, int(lw * 0.75))
    cx, cy = s * 0.44, s * 0.53
    ring = s * 0.245
    nr = s * 0.095
    # Dim, not invisible: at 20 px an alpha below ~150 downsamples to nothing and
    # the glyph collapses into a plain target mark.
    dim = _rgba(p.stroke, 170)
    for nx, ny in ((0.84, 0.15), (0.85, 0.86), (0.13, 0.87)):
        px, py = s * nx, s * ny
        vx, vy = px - cx, py - cy
        vl = math.hypot(vx, vy) or 1.0
        d.line([(cx + vx / vl * ring, cy + vy / vl * ring),
                (px - vx / vl * nr, py - vy / vl * nr)],
               fill=_rgba(p.stroke, 110), width=dlw)
        circle(d, px, py, nr, outline=dim, lw=dlw)
    circle(d, cx, cy, ring, outline=p.accent, lw=max(1, int(lw * 0.9)))
    filled_circle(d, cx, cy, s * 0.12, p.accent)

# ---- 2026-08-17 fallback glyphs for engine-rendered tiles ----------------
# These are only ever seen if scratch/icon_render/<name>.png is missing, so they
# stay deliberately minimal: 2 px stroke, one motif, both themes come free.

def _board_rect(d, s, p, lw, fill_alpha=26):
    """Shared ECAD substrate: the PCB outline every ecad_* fallback sits on."""
    x0, y0, x1, y1 = s * 0.12, s * 0.20, s * 0.88, s * 0.80
    d.rounded_rectangle([x0, y0, x1, y1], radius=int(s * 0.05),
                        fill=_rgba(p.stroke, fill_alpha), outline=p.stroke, width=lw)
    return x0, y0, x1, y1


def _sheet_rect(d, s, p, lw):
    """Shared schematic sheet: the paper outline every sch_* fallback sits on."""
    x0, y0, x1, y1 = s * 0.16, s * 0.14, s * 0.84, s * 0.86
    d.rectangle([x0, y0, x1, y1], fill=_rgba(p.stroke, 22), outline=p.stroke, width=lw)
    return x0, y0, x1, y1


@icon("rotate_ent")
def draw_rotate_ent(d, cell, p):
    """Sketch rotate: curved arrow swinging a bar about a pivot."""
    s = cell * SS
    lw = stroke_width(cell)
    d.line([(s * 0.24, s * 0.78), (s * 0.78, s * 0.78)], fill=p.stroke, width=lw)
    filled_circle(d, s * 0.24, s * 0.78, lw * 1.1, p.stroke)
    arc_segment(d, s * 0.24, s * 0.78, s * 0.46, 250, 340, p.accent, lw)
    arrow(d, s * 0.62, s * 0.40, s * 0.70, s * 0.34, p.accent, lw)


@icon("stock")
def draw_stock(d, cell, p):
    """Stock body: a plain rectangular billet."""
    s = cell * SS
    shaded_prism(d, s, s * 0.16, s * 0.44, s * 0.50, s * 0.34, s * 0.18, s * 0.14,
                 p, accent=False)


@icon("ws_model")
def draw_ws_model(d, cell, p):
    """Workspace tab: Model — the solid part itself."""
    s = cell * SS
    lw = stroke_width(cell)
    iso_box(d, s * 0.50, s * 0.52, s * 0.34, p.stroke, lw)


@icon("ws_drawing")
def draw_ws_drawing(d, cell, p):
    """Workspace tab: Drawing — a sheet with a view and a dimension line."""
    s = cell * SS
    lw = stroke_width(cell)
    d.rectangle([s * 0.12, s * 0.16, s * 0.88, s * 0.84],
                fill=_rgba(p.stroke, 22), outline=p.stroke, width=lw)
    d.rectangle([s * 0.24, s * 0.28, s * 0.62, s * 0.56],
                fill=_rgba(p.accent, 55), outline=p.accent, width=max(1, lw // 2))
    d.line([(s * 0.24, s * 0.70), (s * 0.76, s * 0.70)], fill=p.accent, width=max(1, lw // 2))
    for x in (0.24, 0.76):
        d.line([(s * x, s * 0.64), (s * x, s * 0.76)], fill=p.accent, width=max(1, lw // 2))


@icon("ws_electronics")
def draw_ws_electronics(d, cell, p):
    """Workspace tab: Electronics — a board with a chip and traces."""
    s = cell * SS
    lw = stroke_width(cell)
    _board_rect(d, s, p, lw)
    d.rectangle([s * 0.36, s * 0.38, s * 0.64, s * 0.62],
                fill=_rgba(p.accent, 80), outline=p.accent, width=max(1, lw // 2))
    for y in (0.44, 0.56):
        d.line([(s * 0.20, s * y), (s * 0.36, s * y)], fill=p.accent, width=max(1, lw // 2))
        d.line([(s * 0.64, s * y), (s * 0.80, s * y)], fill=p.accent, width=max(1, lw // 2))


@icon("ws_simulate")
def draw_ws_simulate(d, cell, p):
    """Workspace tab: Simulate — a loaded, meshed body deflecting."""
    s = cell * SS
    lw = stroke_width(cell)
    d.rectangle([s * 0.16, s * 0.52, s * 0.84, s * 0.78],
                fill=_rgba(p.stroke, 26), outline=p.stroke, width=lw)
    for x in (0.39, 0.61):
        d.line([(s * x, s * 0.52), (s * x, s * 0.78)], fill=_rgba(p.stroke, 130),
               width=max(1, lw // 2))
    d.line([(s * 0.16, s * 0.65), (s * 0.84, s * 0.65)], fill=_rgba(p.stroke, 130),
           width=max(1, lw // 2))
    arrow(d, s * 0.50, s * 0.16, s * 0.50, s * 0.46, p.accent, lw)


@icon("ecad_pcb")
def draw_ecad_pcb(d, cell, p):
    """ECAD board editor: substrate with a chip footprint centred."""
    s = cell * SS
    lw = stroke_width(cell)
    _board_rect(d, s, p, lw)
    d.rectangle([s * 0.34, s * 0.36, s * 0.66, s * 0.64],
                fill=_rgba(p.accent, 70), outline=p.accent, width=max(1, lw // 2))
    for i in range(3):
        y = s * (0.42 + i * 0.08)
        d.line([(s * 0.24, y), (s * 0.34, y)], fill=p.accent, width=max(1, lw // 2))
        d.line([(s * 0.66, y), (s * 0.76, y)], fill=p.accent, width=max(1, lw // 2))


@icon("ecad_board3d")
def draw_ecad_board3d(d, cell, p):
    """3D board preview: the substrate seen in isometric with a component on top."""
    s = cell * SS
    lw = stroke_width(cell)
    board = [(s * 0.10, s * 0.60), (s * 0.52, s * 0.80), (s * 0.90, s * 0.58),
             (s * 0.48, s * 0.38)]
    d.polygon(board, fill=_rgba(p.stroke, 40))
    d.line(board + [board[0]], fill=p.stroke, width=lw)
    chip = [(s * 0.38, s * 0.48), (s * 0.56, s * 0.57), (s * 0.72, s * 0.48),
            (s * 0.54, s * 0.39)]
    d.polygon(chip, fill=_rgba(p.accent, 90))
    d.line(chip + [chip[0]], fill=p.accent, width=max(1, lw // 2))


@icon("ecad_footprint")
def draw_ecad_footprint(d, cell, p):
    """Footprint editor: pad rows framing an empty body outline."""
    s = cell * SS
    lw = stroke_width(cell)
    d.rectangle([s * 0.34, s * 0.28, s * 0.66, s * 0.72],
                outline=p.stroke, width=lw)
    for i in range(3):
        y0 = s * (0.32 + i * 0.16)
        d.rectangle([s * 0.14, y0, s * 0.30, y0 + s * 0.09],
                    fill=_rgba(p.accent, 110), outline=p.accent, width=max(1, lw // 2))
        d.rectangle([s * 0.70, y0, s * 0.86, y0 + s * 0.09],
                    fill=_rgba(p.accent, 110), outline=p.accent, width=max(1, lw // 2))


@icon("ecad_route")
def draw_ecad_route(d, cell, p):
    """Route trace: a manual dog-leg track between two pads."""
    s = cell * SS
    lw = stroke_width(cell)
    _board_rect(d, s, p, lw, fill_alpha=18)
    pts = [(s * 0.22, s * 0.68), (s * 0.44, s * 0.68), (s * 0.60, s * 0.40),
           (s * 0.80, s * 0.40)]
    polyline(d, pts, p.accent, lw)
    filled_circle(d, pts[0][0], pts[0][1], lw * 1.1, p.accent)
    filled_circle(d, pts[-1][0], pts[-1][1], lw * 1.1, p.accent)


@icon("ecad_via")
def draw_ecad_via(d, cell, p):
    """Via: an annular ring punched through the board, trace entering it."""
    s = cell * SS
    lw = stroke_width(cell)
    _board_rect(d, s, p, lw, fill_alpha=18)
    cx, cy = s * 0.56, s * 0.50
    circle(d, cx, cy, s * 0.20, fill=_rgba(p.accent, 90), outline=p.accent, lw=lw)
    filled_circle(d, cx, cy, s * 0.085, (0, 0, 0, 0))
    circle(d, cx, cy, s * 0.085, outline=p.accent, lw=max(1, lw // 2))
    d.line([(s * 0.18, s * 0.50), (cx - s * 0.20, s * 0.50)], fill=p.accent, width=lw)


@icon("ecad_zone")
def draw_ecad_zone(d, cell, p):
    """Copper zone: a hatched pour region inside the board outline."""
    s = cell * SS
    lw = stroke_width(cell)
    x0, y0, x1, y1 = _board_rect(d, s, p, lw, fill_alpha=18)
    zx0, zy0, zx1, zy1 = s * 0.22, s * 0.30, s * 0.78, s * 0.70
    d.rectangle([zx0, zy0, zx1, zy1], fill=_rgba(p.accent, 55),
                outline=p.accent, width=max(1, lw // 2))
    step = s * 0.12
    x = zx0
    while x < zx1 + (zy1 - zy0):
        a = (max(zx0, x - (zy1 - zy0)), min(zy1, zy0 + (x - zx0)))
        b = (min(zx1, x), max(zy0, zy1 - (zx1 - min(zx1, x))))
        d.line([a, b], fill=_rgba(p.accent, 150), width=max(1, lw // 2))
        x += step


@icon("ecad_autoroute")
def draw_ecad_autoroute(d, cell, p):
    """Autoroute: several tracks laid automatically across the board."""
    s = cell * SS
    lw = stroke_width(cell)
    _board_rect(d, s, p, lw, fill_alpha=18)
    for i, y in enumerate((0.34, 0.50, 0.66)):
        bend = s * (0.44 + i * 0.08)
        polyline(d, [(s * 0.20, s * y), (bend, s * y), (bend + s * 0.10, s * 0.50),
                     (s * 0.80, s * 0.50)], p.accent, max(1, lw * 2 // 3))


@icon("ecad_fill")
def draw_ecad_fill(d, cell, p):
    """Fill zones: solid copper pour flooding the board around a keep-out."""
    s = cell * SS
    lw = stroke_width(cell)
    x0, y0, x1, y1 = _board_rect(d, s, p, lw, fill_alpha=18)
    d.rectangle([s * 0.20, s * 0.28, s * 0.80, s * 0.72], fill=_rgba(p.accent, 120),
                outline=p.accent, width=max(1, lw // 2))
    d.rectangle([s * 0.42, s * 0.42, s * 0.58, s * 0.58], fill=(0, 0, 0, 0),
                outline=p.stroke, width=max(1, lw // 2))


@icon("ecad_gerber")
def draw_ecad_gerber(d, cell, p):
    """Gerber export: stacked layer sheets peeling off the board."""
    s = cell * SS
    lw = stroke_width(cell)
    for i, (dx, dy, col) in enumerate((( 0.00, 0.00, p.stroke),
                                       (-0.06, -0.13, p.stroke),
                                       (-0.12, -0.26, p.accent))):
        pts = [(s * (0.22 + dx), s * (0.74 + dy)), (s * (0.56 + dx), s * (0.88 + dy)),
               (s * (0.90 + dx), s * (0.74 + dy)), (s * (0.56 + dx), s * (0.60 + dy))]
        d.polygon(pts, fill=_rgba(col, 55 if i < 2 else 90))
        d.line(pts + [pts[0]], fill=col, width=max(1, lw // 2))


@icon("ecad_drill")
def draw_ecad_drill(d, cell, p):
    """Drill file: the board's hole pattern punched through."""
    s = cell * SS
    lw = stroke_width(cell)
    _board_rect(d, s, p, lw, fill_alpha=18)
    for cx, cy in ((0.30, 0.36), (0.50, 0.58), (0.70, 0.36), (0.70, 0.66)):
        circle(d, s * cx, s * cy, s * 0.085, fill=(0, 0, 0, 0), outline=p.accent, lw=lw)


@icon("ecad_drc")
def draw_ecad_drc(d, cell, p):
    """Design-rule check: the board validated with a check mark."""
    s = cell * SS
    lw = stroke_width(cell)
    _board_rect(d, s, p, lw, fill_alpha=18)
    check_mark(d, s * 0.52, s * 0.50, s * 0.28, p.accent, lw)


@icon("ecad_update")
def draw_ecad_update(d, cell, p):
    """Update board from schematic: the board with a refresh arc."""
    s = cell * SS
    lw = stroke_width(cell)
    _board_rect(d, s, p, lw, fill_alpha=18)
    cx, cy, r = s * 0.52, s * 0.50, s * 0.24
    arc_segment(d, cx, cy, r, 40, 320, p.accent, lw)
    arrow(d, cx + r * 0.55, cy - r * 0.95, cx + r * 1.05, cy - r * 0.45, p.accent, lw)


@icon("sch_new")
def draw_sch_new(d, cell, p):
    """New schematic: a blank sheet with a plus."""
    s = cell * SS
    lw = stroke_width(cell)
    _sheet_rect(d, s, p, lw)
    plus_sign(d, s * 0.50, s * 0.50, s * 0.20, p.accent, lw)


@icon("sch_symbol")
def draw_sch_symbol(d, cell, p):
    """Place symbol: a resistor zigzag with leads on the sheet."""
    s = cell * SS
    lw = stroke_width(cell)
    _sheet_rect(d, s, p, lw)
    y = s * 0.50
    pts = [(s * 0.22, y), (s * 0.34, y), (s * 0.40, s * 0.36), (s * 0.50, s * 0.64),
           (s * 0.60, s * 0.36), (s * 0.66, y), (s * 0.78, y)]
    polyline(d, pts, p.accent, lw)


@icon("sch_wire")
def draw_sch_wire(d, cell, p):
    """Draw wire: an orthogonal net with junction dots."""
    s = cell * SS
    lw = stroke_width(cell)
    _sheet_rect(d, s, p, lw)
    pts = [(s * 0.24, s * 0.32), (s * 0.50, s * 0.32), (s * 0.50, s * 0.68),
           (s * 0.76, s * 0.68)]
    polyline(d, pts, p.accent, lw)
    filled_circle(d, s * 0.50, s * 0.32, lw * 1.1, p.accent)
    filled_circle(d, s * 0.50, s * 0.68, lw * 1.1, p.accent)


@icon("sch_power")
def draw_sch_power(d, cell, p):
    """Power/ground port: a lead dropping into a ground symbol."""
    s = cell * SS
    lw = stroke_width(cell)
    _sheet_rect(d, s, p, lw)
    d.line([(s * 0.50, s * 0.26), (s * 0.50, s * 0.54)], fill=p.accent, width=lw)
    for i, half in enumerate((0.22, 0.14, 0.07)):
        y = s * (0.54 + i * 0.09)
        d.line([(s * (0.50 - half), y), (s * (0.50 + half), y)], fill=p.accent, width=lw)


@icon("sch_label")
def draw_sch_label(d, cell, p):
    """Net label: a tag flag attached to a wire."""
    s = cell * SS
    lw = stroke_width(cell)
    _sheet_rect(d, s, p, lw)
    d.line([(s * 0.22, s * 0.62), (s * 0.40, s * 0.62)], fill=p.stroke, width=lw)
    tag = [(s * 0.40, s * 0.62), (s * 0.52, s * 0.38), (s * 0.80, s * 0.38),
           (s * 0.80, s * 0.62)]
    d.polygon(tag, fill=_rgba(p.accent, 80))
    d.line(tag + [tag[0]], fill=p.accent, width=max(1, lw // 2))


@icon("sch_erc")
def draw_sch_erc(d, cell, p):
    """Electrical-rule check: the sheet validated with a check mark."""
    s = cell * SS
    lw = stroke_width(cell)
    _sheet_rect(d, s, p, lw)
    check_mark(d, s * 0.50, s * 0.50, s * 0.26, p.accent, lw)


@icon("sch_simulate")
def draw_sch_simulate(d, cell, p):
    """Simulate circuit: a waveform traced across the sheet."""
    s = cell * SS
    lw = stroke_width(cell)
    _sheet_rect(d, s, p, lw)
    pts = []
    for i in range(25):
        t = i / 24.0
        pts.append((s * (0.22 + 0.56 * t), s * (0.50 - 0.20 * math.sin(t * 2 * math.pi))))
    polyline(d, pts, p.accent, lw)


# ---- 2026-10-05 Simulation GUI overhaul (append-only) ----------------------
# Every Simulate tab / command used to borrow a glyph from an unrelated physics
# or from the modelling tabs (Topology/EM on study_static, Flow BCs on fixture,
# Heat Loads on load_pressure, Piping on sweep, Hydraulics sizing on library
# categories). Line-art in the sim idiom: p.stroke is the body/structure,
# p.accent is what the study or command acts on (the result, the hot face, the
# working fluid), p.accent2 is a direction or extent cue (load, flow, size).
# Tuned at @20 first: one motif per tile, nothing thinner than half a stroke.

def _pipe_path(d, pts, p, lw, bore):
    """Hollow pipe along `pts`: a stroke-coloured wall `bore` wide with a lightly
    filled bore, curved joints so bends read as elbows rather than mitres."""
    d.line(pts, fill=p.stroke, width=int(bore), joint="curve")
    d.line(pts, fill=_rgba(p.stroke, 34), width=max(1, int(bore - 2 * lw)), joint="curve")


def _elbow_pts(x0, y0, cx, cy, x1, y1, r, n=10):
    """Polyline (x0,y0) -> corner (cx,cy) -> (x1,y1) with the corner rounded by
    radius r. Legs must be axis-aligned (orthogonal pipe routing)."""
    def unit(ax, ay, bx, by):
        l = math.hypot(bx - ax, by - ay) or 1.0
        return (bx - ax) / l, (by - ay) / l
    ux0, uy0 = unit(cx, cy, x0, y0)     # corner -> start
    ux1, uy1 = unit(cx, cy, x1, y1)     # corner -> end
    a = (cx + ux0 * r, cy + uy0 * r)
    b = (cx + ux1 * r, cy + uy1 * r)
    # arc centre sits diagonally inside the bend
    ocx, ocy = cx + (ux0 + ux1) * r, cy + (uy0 + uy1) * r
    a0 = math.atan2(a[1] - ocy, a[0] - ocx)
    a1 = math.atan2(b[1] - ocy, b[0] - ocx)
    da = (a1 - a0 + math.pi) % (2 * math.pi) - math.pi
    arc = [(ocx + r * math.cos(a0 + da * i / n), ocy + r * math.sin(a0 + da * i / n))
           for i in range(n + 1)]
    return [(x0, y0)] + arc + [(x1, y1)]


def _pump_symbol(d, cx, cy, r, p, lw, inward=False):
    """ISO 1219 pump (triangle on the rim pointing OUT) or motor (pointing IN)."""
    circle(d, cx, cy, r, fill=_rgba(p.stroke, 24), outline=p.stroke, lw=lw)
    h = r * 0.62
    if inward:
        tri = [(cx, cy - r + lw * 0.5 + h), (cx - h * 0.62, cy - r + lw * 0.5),
               (cx + h * 0.62, cy - r + lw * 0.5)]
    else:
        tri = [(cx, cy - r + lw * 0.5), (cx - h * 0.62, cy - r + lw * 0.5 + h),
               (cx + h * 0.62, cy - r + lw * 0.5 + h)]
    d.polygon(tri, fill=p.accent)


def _size_arrow(d, x0, x1, y, p, lw):
    """Double-headed extent arrow with end ticks: the shared size_* cue."""
    arrow(d, (x0 + x1) * 0.5, y, x0, y, p.accent2, lw)
    arrow(d, (x0 + x1) * 0.5, y, x1, y, p.accent2, lw)
    t = lw * 1.6
    d.line([(x0, y - t), (x0, y + t)], fill=p.accent2, width=max(1, lw // 2))
    d.line([(x1, y - t), (x1, y + t)], fill=p.accent2, width=max(1, lw // 2))


def _wavy(x0, y0, x1, y1, amp, waves=1.5, n=24):
    """Sine-wiggled polyline from (x0,y0) to (x1,y1) (heat / radiation cue)."""
    dx, dy = x1 - x0, y1 - y0
    l = math.hypot(dx, dy) or 1.0
    nx, ny = -dy / l, dx / l
    return [(x0 + dx * t + nx * amp * math.sin(t * waves * 2 * math.pi),
             y0 + dy * t + ny * amp * math.sin(t * waves * 2 * math.pi))
            for t in (i / n for i in range(n + 1))]


def _plus_badge(d, s, p, lw):
    """Top-right accent plus shared by the new_* commands."""
    plus_sign(d, s * 0.80, s * 0.20, s * 0.12, p.accent, lw)


@icon("study_topology")
def draw_study_topology(d, cell, p):
    """Topology optimisation: the optimised load path (an accent truss) left
    inside the dashed design space, loaded from above."""
    s = cell * SS
    lw = stroke_width(cell)
    x0, y0, x1, y1 = s * 0.08, s * 0.38, s * 0.92, s * 0.86
    dash = s * 0.07
    for (ax, ay, bx, by) in ((x0, y0, x1, y0), (x1, y0, x1, y1), (x1, y1, x0, y1), (x0, y1, x0, y0)):
        l = math.hypot(bx - ax, by - ay)
        k = 0.0
        while k < l:
            e = min(l, k + dash)
            d.line([(ax + (bx - ax) * k / l, ay + (by - ay) * k / l),
                    (ax + (bx - ax) * e / l, ay + (by - ay) * e / l)],
                   fill=_rgba(p.stroke, 170), width=max(1, lw // 2))
            k += dash * 1.9
    # surviving material: chords + Warren diagonals, thick enough to read as solid
    mw = int(lw * 1.5)
    ty, by_ = y0 + lw * 1.6, y1 - lw * 1.6
    lx, rx = x0 + lw * 1.6, x1 - lw * 1.6
    xs = [lx + (rx - lx) * f for f in (0.0, 0.25, 0.5, 0.75, 1.0)]
    d.line([(lx, ty), (rx, ty)], fill=p.accent, width=mw)
    d.line([(lx, by_), (rx, by_)], fill=p.accent, width=mw)
    zig = [(xs[0], by_), (xs[1], ty), (xs[2], by_), (xs[3], ty), (xs[4], by_)]
    d.line(zig, fill=p.accent, width=mw, joint="curve")
    arrow(d, s * 0.50, s * 0.06, s * 0.50, y0 - lw * 0.5, p.accent2, lw)


@icon("study_em")
def draw_study_em(d, cell, p):
    """Electromagnetics: an antenna mast radiating wavefronts to both sides."""
    s = cell * SS
    lw = stroke_width(cell)
    cx, top = s * 0.50, s * 0.42
    d.line([(cx, top), (cx, s * 0.86)], fill=p.stroke, width=lw)
    d.line([(cx - s * 0.16, s * 0.86), (cx + s * 0.16, s * 0.86)], fill=p.stroke, width=lw)
    d.line([(cx, s * 0.58), (cx - s * 0.12, s * 0.86)], fill=p.stroke, width=max(1, lw // 2))
    d.line([(cx, s * 0.58), (cx + s * 0.12, s * 0.86)], fill=p.stroke, width=max(1, lw // 2))
    filled_circle(d, cx, top, lw * 1.1, p.accent)
    for r in (0.17, 0.32):
        arc_segment(d, cx, top, s * r, 140, 220, p.accent, lw)
        arc_segment(d, cx, top, s * r, 320, 40, p.accent, lw)


@icon("study_buckling")
def draw_study_buckling(d, cell, p):
    """Buckling: a column between a fixed base and a compressive load, its
    straight ghost and the bowed buckled mode shape (accent)."""
    s = cell * SS
    lw = stroke_width(cell)
    by = s * 0.86
    d.line([(s * 0.22, by), (s * 0.78, by)], fill=p.stroke, width=lw)
    for xi in range(4):
        x = s * (0.28 + xi * 0.15)
        d.line([(x, by), (x - s * 0.07, by + s * 0.09)], fill=p.stroke, width=max(1, lw // 2))
    ty = s * 0.30
    d.line([(s * 0.50, ty), (s * 0.50, by)], fill=_rgba(p.stroke, 110), width=max(1, lw // 2))
    pts = [(s * 0.50 + s * 0.20 * math.sin(math.pi * t), ty + (by - ty) * t)
           for t in (i / 20 for i in range(21))]
    polyline(d, pts, p.accent, lw)
    d.line([(s * 0.38, ty), (s * 0.62, ty)], fill=p.stroke, width=lw)
    arrow(d, s * 0.50, s * 0.06, s * 0.50, ty - lw * 0.6, p.accent2, lw)


@icon("study_contact")
def draw_study_contact(d, cell, p):
    """Contact: a roller pressed onto a block, the contact patch in accent."""
    s = cell * SS
    lw = stroke_width(cell)
    d.rectangle([s * 0.12, s * 0.62, s * 0.88, s * 0.86], fill=_rgba(p.stroke, 30),
                outline=p.stroke, width=lw)
    cx, cy, r = s * 0.50, s * 0.38, s * 0.24
    circle(d, cx, cy, r, fill=_rgba(p.stroke, 30), outline=p.stroke, lw=lw)
    filled_circle(d, cx, cy, lw * 0.9, p.stroke)
    # flattened contact patch + pressure fan
    d.ellipse([cx - s * 0.16, s * 0.58, cx + s * 0.16, s * 0.68], fill=p.accent)
    for ang in (-35, 35):
        a = math.radians(90 + ang)
        d.line([(cx + math.cos(a) * s * 0.07, s * 0.68 + math.sin(a) * s * 0.02),
                (cx + math.cos(a) * s * 0.20, s * 0.68 + math.sin(a) * s * 0.12)],
               fill=p.accent, width=max(1, lw // 2))


@icon("study_vibration")
def draw_study_vibration(d, cell, p):
    """Vibration / frequency response: a mass on a spring over a base, with
    accent motion marks either side of the mass."""
    s = cell * SS
    lw = stroke_width(cell)
    by = s * 0.88
    d.line([(s * 0.24, by), (s * 0.76, by)], fill=p.stroke, width=lw)
    # spring zig-zag from the base up to the mass
    top = s * 0.46
    n = 6
    pts = [(s * 0.50, by)]
    for i in range(1, n):
        y = by - (by - top) * i / n
        pts.append((s * 0.50 + (s * 0.11 if i % 2 else -s * 0.11), y))
    pts.append((s * 0.50, top))
    polyline(d, pts, p.stroke, lw)
    d.rectangle([s * 0.30, s * 0.16, s * 0.70, top], fill=p.accent)
    for x, sgn in ((0.16, -1), (0.84, 1)):
        for k in (0.0, 1.0):
            xx = s * x + sgn * k * s * 0.07
            d.line([(xx, s * 0.20), (xx, s * 0.42)], fill=_rgba(p.accent, 255 - int(k * 90)),
                   width=max(1, lw // 2) if k else lw)


@icon("study_thermal_fea")
def draw_study_thermal_fea(d, cell, p):
    """Thermal FEA (conduction in a solid): a meshed block heated from its base,
    the hot elements accent, heat rising off the top."""
    s = cell * SS
    lw = stroke_width(cell)
    x0, y0, x1, y1 = s * 0.14, s * 0.42, s * 0.86, s * 0.86
    ym = (y0 + y1) * 0.5
    d.rectangle([x0, ym, x1, y1], fill=p.accent)
    d.rectangle([x0, y0, x1, ym], fill=_rgba(p.accent, 70))
    d.rectangle([x0, y0, x1, y1], outline=p.stroke, width=lw)
    d.line([(x0, ym), (x1, ym)], fill=p.stroke, width=max(1, lw // 2))
    for fx in (1 / 3, 2 / 3):
        x = x0 + (x1 - x0) * fx
        d.line([(x, y0), (x, y1)], fill=p.stroke, width=max(1, lw // 2))
    for x in (0.32, 0.50, 0.68):
        polyline(d, _wavy(s * x, s * 0.34, s * x, s * 0.08, s * 0.035, waves=1.0), p.stroke,
                 max(1, int(lw * 0.75)))


@icon("study_thermal_cfd")
def draw_study_thermal_cfd(d, cell, p):
    """Thermal CFD (conjugate heat transfer): streamlines carrying heat away
    from a hot plate — the lowest, hot-washed streamline is accent."""
    s = cell * SS
    lw = stroke_width(cell)
    d.rectangle([s * 0.26, s * 0.76, s * 0.74, s * 0.88], fill=p.accent)
    for oy, col in ((0.20, p.stroke), (0.40, p.stroke), (0.60, p.accent)):
        pts = [(s * (0.08 + i * 0.07), s * oy - s * 0.06 * math.exp(-((i - 6) / 2.6) ** 2)
                * (1.0 if oy > 0.5 else 0.6)) for i in range(13)]
        polyline(d, pts, col, lw)
    arrow(d, s * 0.80, s * 0.60, s * 0.93, s * 0.60, p.accent, lw)


def _piping_motif(d, ox, oy, k, p, lw):
    """Pipe run through a 90-degree elbow with flanged ends, drawn in the box
    (ox, oy, ox+k, oy+k). Shared by study_piping and new_network."""
    bore = k * 0.20
    pts = _elbow_pts(ox + k * 0.10, oy + k * 0.70, ox + k * 0.68, oy + k * 0.70,
                     ox + k * 0.68, oy + k * 0.12, k * 0.20)
    _pipe_path(d, pts, p, lw, bore)
    fl = bore * 0.5 + lw * 1.2
    d.line([(ox + k * 0.10, oy + k * 0.70 - fl), (ox + k * 0.10, oy + k * 0.70 + fl)],
           fill=p.accent, width=lw * 2)
    d.line([(ox + k * 0.68 - fl, oy + k * 0.12), (ox + k * 0.68 + fl, oy + k * 0.12)],
           fill=p.accent, width=lw * 2)


def _hydraulics_motif(d, ox, oy, k, p, lw):
    """ISO pump feeding a double-acting cylinder, drawn in the box
    (ox, oy, ox+k, oy+k). Shared by study_hydraulics and new_circuit."""
    d.rectangle([ox + k * 0.10, oy + k * 0.12, ox + k * 0.64, oy + k * 0.40],
                fill=_rgba(p.stroke, 30), outline=p.stroke, width=lw)
    d.line([(ox + k * 0.42, oy + k * 0.12), (ox + k * 0.42, oy + k * 0.40)], fill=p.stroke, width=lw)
    d.rectangle([ox + k * 0.64, oy + k * 0.22, ox + k * 0.92, oy + k * 0.30], fill=p.accent)
    r = k * 0.20
    cx, cy = ox + k * 0.28, oy + k * 0.74
    d.line([(cx, cy - r), (cx, oy + k * 0.40)], fill=p.accent2, width=lw)
    _pump_symbol(d, cx, cy, r, p, lw)


@icon("study_piping")
def draw_study_piping(d, cell, p):
    """Piping: a pipe run turning through a 90-degree elbow, flanged ends."""
    s = cell * SS
    _piping_motif(d, 0.0, 0.0, s, p, stroke_width(cell))


@icon("study_hydraulics")
def draw_study_hydraulics(d, cell, p):
    """Hydraulics: a pump (ISO symbol) feeding a double-acting cylinder."""
    s = cell * SS
    _hydraulics_motif(d, 0.0, s * 0.04, s, p, stroke_width(cell))


@icon("load_heat")
def draw_load_heat(d, cell, p):
    """Heat load: wavy heat-flux arrows entering a face (cf. load_pressure's
    straight arrows)."""
    s = cell * SS
    lw = stroke_width(cell)
    sy = s * 0.80
    d.line([(s * 0.12, sy), (s * 0.88, sy)], fill=p.stroke, width=lw * 2)
    for x in (0.26, 0.50, 0.74):
        pts = _wavy(s * x, s * 0.12, s * x, sy - lw * 2.2, s * 0.05, waves=1.25)
        polyline(d, pts, p.accent, lw)
        tip = (s * x, sy - lw * 1.0)
        head = lw * 2.4
        d.polygon([tip, (tip[0] - head * 0.75, tip[1] - head),
                   (tip[0] + head * 0.75, tip[1] - head)], fill=p.accent)


@icon("fluid")
def draw_fluid(d, cell, p):
    """Fluid (working-fluid properties): a droplet with a highlight."""
    s = cell * SS
    lw = stroke_width(cell)
    cx, cy, r = s * 0.50, s * 0.62, s * 0.25
    tip = (cx, s * 0.08)
    # tangent points from the tip onto the round base
    dist = cy - tip[1]
    a = math.acos(r / dist)
    pts = [tip]
    a0 = -math.pi / 2 + a
    a1 = -math.pi / 2 - a + 2 * math.pi
    for i in range(33):
        t = a0 + (a1 - a0) * i / 32
        pts.append((cx + r * math.cos(t), cy + r * math.sin(t)))
    d.polygon(pts, fill=_rgba(p.accent, 90))
    d.line(pts + [pts[0], pts[1]], fill=p.accent, width=lw, joint="curve")
    arc_segment(d, cx, cy, r * 0.62, 110, 170, _rgba((255, 255, 255), 210), lw)


@icon("flow_bc")
def draw_flow_bc(d, cell, p):
    """Flow boundaries: a channel with the inlet arrow entering and the outlet
    arrow leaving."""
    s = cell * SS
    lw = stroke_width(cell)
    for y in (0.26, 0.74):
        d.line([(s * 0.30, s * y), (s * 0.70, s * y)], fill=p.stroke, width=lw)
    d.line([(s * 0.30, s * 0.26), (s * 0.30, s * 0.74)], fill=_rgba(p.accent2, 150),
           width=max(1, lw // 2))
    d.line([(s * 0.70, s * 0.26), (s * 0.70, s * 0.74)], fill=_rgba(p.accent, 150),
           width=max(1, lw // 2))
    arrow(d, s * 0.04, s * 0.50, s * 0.42, s * 0.50, p.accent2, lw)
    arrow(d, s * 0.58, s * 0.50, s * 0.96, s * 0.50, p.accent, lw)


@icon("em_port")
def draw_em_port(d, cell, p):
    """EM excitation port: a coax end (outer ring, accent centre conductor) with
    the incident wave arriving from the left."""
    s = cell * SS
    lw = stroke_width(cell)
    cx, cy = s * 0.64, s * 0.50
    circle(d, cx, cy, s * 0.26, fill=_rgba(p.stroke, 26), outline=p.stroke, lw=lw)
    filled_circle(d, cx, cy, s * 0.09, p.accent)
    polyline(d, _wavy(s * 0.04, cy, s * 0.30, cy, s * 0.06, waves=1.5), p.accent2, lw)
    head = lw * 2.4
    d.polygon([(s * 0.38, cy), (s * 0.38 - head, cy - head * 0.75),
               (s * 0.38 - head, cy + head * 0.75)], fill=p.accent2)


@icon("em_boundary")
def draw_em_boundary(d, cell, p):
    """EM boundary (radiation / PEC box): an accent dashed enclosure around the
    radiating body."""
    s = cell * SS
    lw = stroke_width(cell)
    x0, y0, x1, y1 = s * 0.10, s * 0.10, s * 0.90, s * 0.90
    seg = s * 0.13
    for (ax, ay, bx, by) in ((x0, y0, x1, y0), (x1, y0, x1, y1), (x1, y1, x0, y1), (x0, y1, x0, y0)):
        l = math.hypot(bx - ax, by - ay)
        k = 0.0
        while k < l:
            e = min(l, k + seg)
            d.line([(ax + (bx - ax) * k / l, ay + (by - ay) * k / l),
                    (ax + (bx - ax) * e / l, ay + (by - ay) * e / l)], fill=p.accent, width=lw)
            k += seg * 1.75
    d.rectangle([s * 0.38, s * 0.38, s * 0.62, s * 0.62], fill=_rgba(p.stroke, 40),
                outline=p.stroke, width=lw)
    arc_segment(d, s * 0.50, s * 0.50, s * 0.24, 300, 60, p.stroke, max(1, lw // 2))
    arc_segment(d, s * 0.50, s * 0.50, s * 0.24, 120, 240, p.stroke, max(1, lw // 2))


@icon("freq_range")
def draw_freq_range(d, cell, p):
    """Frequency range: a chirp (rising frequency) between two band markers."""
    s = cell * SS
    lw = stroke_width(cell)
    x0, x1 = s * 0.20, s * 0.80
    cy = s * 0.50
    pts = []
    n = 60
    for i in range(n + 1):
        t = i / n
        ph = 2 * math.pi * (1.0 * t + 1.6 * t * t)
        pts.append((x0 + (x1 - x0) * t, cy - s * 0.20 * math.sin(ph)))
    polyline(d, pts, p.accent, lw)
    for x in (s * 0.10, s * 0.90):
        d.line([(x, s * 0.18), (x, s * 0.82)], fill=p.accent2, width=lw)
    d.line([(s * 0.10, s * 0.86), (s * 0.90, s * 0.86)], fill=p.stroke, width=max(1, lw // 2))


@icon("new_network")
def draw_new_network(d, cell, p):
    """New piping network: the study_piping elbow, reduced, with a plus."""
    s = cell * SS
    lw = stroke_width(cell)
    _piping_motif(d, 0.0, s * 0.22, s * 0.78, p, lw)
    _plus_badge(d, s, p, lw)


@icon("new_circuit")
def draw_new_circuit(d, cell, p):
    """New hydraulic circuit: the study_hydraulics pump + cylinder, reduced,
    with a plus."""
    s = cell * SS
    lw = stroke_width(cell)
    _hydraulics_motif(d, 0.0, s * 0.24, s * 0.76, p, lw)
    _plus_badge(d, s, p, lw)


@icon("route_pipe")
def draw_route_pipe(d, cell, p):
    """Route pipe: a laid pipe ending at an elbow, its dashed centreline routed
    on to the next waypoint (accent target)."""
    s = cell * SS
    lw = stroke_width(cell)
    pts = _elbow_pts(s * 0.08, s * 0.76, s * 0.46, s * 0.76, s * 0.46, s * 0.46, s * 0.16)
    _pipe_path(d, pts, p, lw, s * 0.18)
    d.line([(s * 0.46, s * 0.46), (s * 0.46, s * 0.24)], fill=p.accent2, width=lw)
    x = s * 0.46
    while x < s * 0.74:
        d.line([(x, s * 0.24), (min(s * 0.74, x + s * 0.08), s * 0.24)], fill=p.accent2, width=lw)
        x += s * 0.14
    circle(d, s * 0.84, s * 0.24, s * 0.08, fill=p.accent, outline=p.accent, lw=lw)


@icon("size_actuator")
def draw_size_actuator(d, cell, p):
    """Size actuator: a hydraulic cylinder under a stroke-length dimension."""
    s = cell * SS
    lw = stroke_width(cell)
    d.rectangle([s * 0.08, s * 0.46, s * 0.58, s * 0.78], fill=_rgba(p.stroke, 30),
                outline=p.stroke, width=lw)
    d.line([(s * 0.36, s * 0.46), (s * 0.36, s * 0.78)], fill=p.stroke, width=lw)
    d.rectangle([s * 0.58, s * 0.58, s * 0.92, s * 0.66], fill=p.accent)
    _size_arrow(d, s * 0.08, s * 0.92, s * 0.22, p, lw)


@icon("size_pump")
def draw_size_pump(d, cell, p):
    """Size pump: the ISO pump symbol under a size dimension."""
    s = cell * SS
    lw = stroke_width(cell)
    _pump_symbol(d, s * 0.50, s * 0.64, s * 0.24, p, lw)
    _size_arrow(d, s * 0.12, s * 0.88, s * 0.16, p, lw)


@icon("size_motor")
def draw_size_motor(d, cell, p):
    """Size hydraulic motor: the ISO motor symbol (triangle pointing in) under a
    size dimension."""
    s = cell * SS
    lw = stroke_width(cell)
    _pump_symbol(d, s * 0.50, s * 0.64, s * 0.24, p, lw, inward=True)
    # output-shaft rotation arrow
    arc_segment(d, s * 0.50, s * 0.64, s * 0.32, 30, 100, p.accent2, max(1, lw // 2))
    _size_arrow(d, s * 0.12, s * 0.88, s * 0.16, p, lw)


@icon("pump_drive")
def draw_pump_drive(d, cell, p):
    """Pump drive: an electric motor — finned housing on a foot, accent shaft."""
    s = cell * SS
    lw = stroke_width(cell)
    d.rounded_rectangle([s * 0.10, s * 0.28, s * 0.66, s * 0.72], radius=int(s * 0.06),
                        fill=_rgba(p.stroke, 30), outline=p.stroke, width=lw)
    for x in (0.24, 0.36, 0.48):
        d.line([(s * x, s * 0.36), (s * x, s * 0.64)], fill=p.stroke, width=max(1, lw // 2))
    d.rectangle([s * 0.66, s * 0.38, s * 0.74, s * 0.62], fill=p.stroke)
    d.rectangle([s * 0.74, s * 0.46, s * 0.94, s * 0.54], fill=p.accent)
    d.line([(s * 0.18, s * 0.72), (s * 0.12, s * 0.84), (s * 0.64, s * 0.84), (s * 0.58, s * 0.72)],
           fill=p.stroke, width=lw)
    # lightning tick = electric
    polyline(d, [(s * 0.40, s * 0.06), (s * 0.32, s * 0.18), (s * 0.44, s * 0.18),
                 (s * 0.36, s * 0.28)], p.accent, max(1, lw // 2) + lw // 4)


@icon("vessel")
def draw_vessel(d, cell, p):
    """Pressure vessel: a vertical shell with dished heads, top nozzle, legs."""
    s = cell * SS
    lw = stroke_width(cell)
    x0, x1 = s * 0.30, s * 0.70
    yt, yb = s * 0.26, s * 0.70
    hh = s * 0.10
    body = []
    for i in range(17):  # bottom head (semi-ellipse)
        t = math.pi * i / 16
        body.append(((x0 + x1) / 2 - (x1 - x0) / 2 * math.cos(t), yb + hh * math.sin(t)))
    for i in range(17):  # top head
        t = math.pi * i / 16
        body.append(((x0 + x1) / 2 + (x1 - x0) / 2 * math.cos(t), yt - hh * math.sin(t)))
    d.polygon(body, fill=_rgba(p.stroke, 30))
    d.line(body + [body[0]], fill=p.stroke, width=lw, joint="curve")
    d.line([(x0, s * 0.48), (x1, s * 0.48)], fill=_rgba(p.stroke, 150), width=max(1, lw // 2))
    # nozzle
    d.rectangle([s * 0.45, s * 0.06, s * 0.55, yt - hh + lw * 0.5], fill=p.accent)
    d.line([(s * 0.40, s * 0.07), (s * 0.60, s * 0.07)], fill=p.accent, width=lw)
    # legs
    for x in (0.36, 0.64):
        d.line([(s * x, yb + hh * 0.6), (s * x, s * 0.92)], fill=p.stroke, width=lw)


@icon("duct")
def draw_duct(d, cell, p):
    """Duct: a rectangular duct section receding from its accent mouth, the
    airflow arrow running along it."""
    s = cell * SS
    lw = stroke_width(cell)
    fx0, fy0, fx1, fy1 = s * 0.08, s * 0.46, s * 0.50, s * 0.88   # mouth (front)
    ox, oy = s * 0.40, -s * 0.34                                   # recede up-right
    tl, tr, br = (fx0 + ox, fy0 + oy), (fx1 + ox, fy0 + oy), (fx1 + ox, fy1 + oy)
    d.polygon([(fx0, fy0), tl, tr, (fx1, fy0)], fill=_rgba(p.stroke, 40))
    d.polygon([(fx1, fy0), tr, br, (fx1, fy1)], fill=_rgba(p.stroke, 22))
    d.line([(fx0, fy0), tl, tr, br, (fx1, fy1)], fill=p.stroke, width=lw)
    d.line([tr, (fx1, fy0)], fill=p.stroke, width=lw)
    d.rectangle([fx0, fy0, fx1, fy1], fill=_rgba(p.accent, 50), outline=p.accent, width=lw)
    cx, cy = (fx0 + fx1) * 0.5, (fy0 + fy1) * 0.5
    arrow(d, cx, cy, cx + ox * 0.62, cy + oy * 0.62, p.accent2, lw)


@icon("spec_check")
def draw_spec_check(d, cell, p):
    """Spec / code check: a standard's page (text rules) with an accent check
    badge — the design checked against the code."""
    s = cell * SS
    lw = stroke_width(cell)
    x0, y0, x1, y1 = s * 0.12, s * 0.06, s * 0.68, s * 0.86
    d.rectangle([x0, y0, x1, y1], fill=_rgba(p.stroke, 22), outline=p.stroke, width=lw)
    rlw = max(1, lw // 2)
    for y, xe in ((0.22, 0.54), (0.36, 0.54), (0.50, 0.42)):
        d.line([(s * 0.24, s * y), (s * xe, s * y)], fill=p.stroke, width=rlw)
    cx, cy, r = s * 0.70, s * 0.70, s * 0.22
    filled_circle(d, cx, cy, r + lw * 0.6, (0, 0, 0, 0))
    filled_circle(d, cx, cy, r, p.accent)
    check_mark(d, cx, cy + s * 0.01, r * 0.62, (255, 255, 255), lw)


@icon("schematic")
def draw_schematic(d, cell, p):
    """Schematic view: a sheet carrying a pump wired to a valve block."""
    s = cell * SS
    lw = stroke_width(cell)
    _sheet_rect(d, s, p, lw)
    _pump_symbol(d, s * 0.34, s * 0.64, s * 0.11, p, max(1, lw // 2) + lw // 4)
    d.rectangle([s * 0.52, s * 0.28, s * 0.72, s * 0.44], outline=p.accent, width=max(1, lw // 2) + lw // 4)
    arrow(d, s * 0.55, s * 0.40, s * 0.69, s * 0.32, p.accent, max(1, lw // 2))
    polyline(d, [(s * 0.34, s * 0.53), (s * 0.34, s * 0.36), (s * 0.52, s * 0.36)], p.stroke,
             max(1, lw // 2) + lw // 4)


@icon("plots_view")
def draw_plots_view(d, cell, p):
    """Plots: axes carrying two response curves (cf. `results` bar chart)."""
    s = cell * SS
    lw = stroke_width(cell)
    ox, oy = s * 0.14, s * 0.84
    d.line([(ox, s * 0.12), (ox, oy), (s * 0.90, oy)], fill=p.stroke, width=lw)
    a = [(ox + s * 0.04 + s * 0.72 * t,
          oy - s * 0.10 - s * 0.52 * (1 - math.exp(-4 * t))) for t in (i / 24 for i in range(25))]
    b = [(ox + s * 0.04 + s * 0.72 * t,
          oy - s * 0.18 - s * 0.16 * math.sin(t * 2.4 * math.pi)) for t in (i / 24 for i in range(25))]
    polyline(d, b, _rgba(p.stroke, 200), max(1, lw // 2) + lw // 4)
    polyline(d, a, p.accent, lw)


def _media_tri(d, s, p, lw, x0, x1, cy, h, right=True):
    """Filled accent transport triangle with a stroke outline."""
    tip, base = (x1, x0) if right else (x0, x1)
    pts = [(base, cy - h), (tip, cy), (base, cy + h)]
    d.polygon(pts, fill=p.accent)
    polyline(d, pts + [pts[0]], p.stroke, max(1, lw // 2))


def _media_bar(d, s, p, lw, x, cy, h):
    d.line([(x, cy - h), (x, cy + h)], fill=p.stroke, width=lw + lw // 2)


@icon("media_first")
def draw_media_first(d, cell, p):
    """Go to first frame: |◀."""
    s = cell * SS
    lw = stroke_width(cell)
    _media_bar(d, s, p, lw, s * 0.24, s * 0.5, s * 0.28)
    _media_tri(d, s, p, lw, s * 0.34, s * 0.78, s * 0.5, s * 0.28, right=False)


@icon("media_prev")
def draw_media_prev(d, cell, p):
    """Step back one frame: ◀|."""
    s = cell * SS
    lw = stroke_width(cell)
    _media_tri(d, s, p, lw, s * 0.20, s * 0.64, s * 0.5, s * 0.28, right=False)
    _media_bar(d, s, p, lw, s * 0.76, s * 0.5, s * 0.28)


@icon("media_play")
def draw_media_play(d, cell, p):
    """Play: ▶."""
    s = cell * SS
    lw = stroke_width(cell)
    _media_tri(d, s, p, lw, s * 0.28, s * 0.80, s * 0.5, s * 0.32)


@icon("media_pause")
def draw_media_pause(d, cell, p):
    """Pause: ‖ (two accent bars)."""
    s = cell * SS
    lw = stroke_width(cell)
    for x0 in (0.26, 0.58):
        d.rectangle([s * x0, s * 0.20, s * (x0 + 0.16), s * 0.80], fill=p.accent,
                    outline=p.stroke, width=max(1, lw // 2))


@icon("media_next")
def draw_media_next(d, cell, p):
    """Step forward one frame: |▶."""
    s = cell * SS
    lw = stroke_width(cell)
    _media_bar(d, s, p, lw, s * 0.24, s * 0.5, s * 0.28)
    _media_tri(d, s, p, lw, s * 0.36, s * 0.80, s * 0.5, s * 0.28)


@icon("media_last")
def draw_media_last(d, cell, p):
    """Go to last frame: ▶|."""
    s = cell * SS
    lw = stroke_width(cell)
    _media_tri(d, s, p, lw, s * 0.22, s * 0.66, s * 0.5, s * 0.28)
    _media_bar(d, s, p, lw, s * 0.76, s * 0.5, s * 0.28)


# ---------------------------------------------------------------------------
# Verify coverage
# ---------------------------------------------------------------------------

def check_coverage() -> list[str]:
    missing = [n for n in CONTRACT_NAMES if n not in _REGISTRY]
    return missing


# ---------------------------------------------------------------------------
# Atlas builder
# ---------------------------------------------------------------------------

CELL_SIZES = [32, 20]
HERE = Path(__file__).resolve().parent
OUT_DIR = HERE / "icons"

# Atlas geometry. One "row block" per icon name carries that name's @32 and @20
# cells side by side; blocks are laid out top-to-bottom in bounded-height columns.
#
# The column bound is the whole point. This used to be a single column, so the
# atlas was 56 px wide and as tall as the contract was long — 277 names x 34 px
# = 9418 px. GLES3 and WebGL2 only GUARANTEE a 2048 px MAX_TEXTURE_SIZE, and
# ANGLE/SwiftShader plus plenty of real GPUs report 8192, so glTexImage2D failed
# outright in the browser client and every icon sampled as opaque black. Nothing
# logged it as an icon problem; the ribbon just filled with black squares.
#
# 2048 is therefore the budget for BOTH dimensions, not the observed 8192: an
# atlas that needs more than the guaranteed minimum is a bug waiting for the next
# device. At 34 px per block that is 60 rows per column, which leaves room for
# 3600 icons before the width even reaches 2048.
ATLAS_MAX_PX = 2048
ROW_PAD = 2

# Top-level atlas.json keys this script does NOT author but MUST NOT destroy.
# scripts/icon_pipeline.py --render injects them after the engine bakes the 3D
# tiles; main() and relayout_committed_atlas() both rewrite atlas.json wholesale,
# so anything not explicitly carried across is deleted.
#
# The failure is silent by construction, which is why this list exists instead of
# a convention. "accent_tinted" (the tiles whose baked pixels carry the accent)
# and "accent_ref" (the primary/secondary hues they were baked WITH) are the only
# inputs host/src/ui/icons.cpp::recolorIconAtlas has: with the keys gone it finds
# nothing to retint, returns immediately, and a user's chosen accent simply stops
# reaching the 3D tiles. No exception, no warning, no log line — the ribbon just
# keeps the factory orange forever. Same shape as the raster_only bug that made
# icons "break every rebuild" until that key was carried forward here.
#
# Adding a pipeline-owned key? Add it HERE, or the next glyph regen eats it.
PIPELINE_OWNED_ATLAS_KEYS = ("accent_tinted", "accent_ref")


def _block_size(sizes: list[int] | None = None) -> tuple[int, int]:
    """(width, height) of one name's row block, padding included."""
    sizes = sizes if sizes is not None else CELL_SIZES
    return sum(s + ROW_PAD for s in sizes), max(sizes) + ROW_PAD


def atlas_geometry(
    n_names: int, sizes: list[int] | None = None
) -> tuple[int, int, int]:
    """(rows_per_column, atlas_w, atlas_h) for ``n_names`` row blocks.

    Columns are filled to ``ATLAS_MAX_PX`` tall, then the row count is pulled
    back to what the resulting column count actually needs, so the atlas never
    carries a tall mostly-empty final column.

    Raises when the contract cannot fit in the budget at all. That is deliberate:
    the failure this replaces was silent — the atlas simply grew past the texture
    limit, the GL upload failed, and the ribbon filled with black squares with
    nothing in any log naming icons. A build-time exception is the only version of
    this that anyone notices.
    """
    block_w, block_h = _block_size(sizes)
    max_rows = max(1, ATLAS_MAX_PX // block_h)
    max_cols = max(1, ATLAS_MAX_PX // block_w)
    n = max(1, n_names)
    cols = -(-n // max_rows)
    if cols > max_cols:
        raise ValueError(
            f"{n} icons need {cols} columns of {block_w} px but only {max_cols} "
            f"fit in the {ATLAS_MAX_PX} px budget (capacity "
            f"{max_cols * max_rows} icons). Shrink CELL_SIZES, split the atlas, "
            f"or raise ATLAS_MAX_PX only after checking the GL_MAX_TEXTURE_SIZE "
            f"of every target — WebGL2 guarantees just 2048."
        )
    rows = -(-n // cols)
    return rows, cols * block_w, rows * block_h


def block_origin(
    index: int, rows_per_col: int, sizes: list[int] | None = None
) -> tuple[int, int]:
    """Top-left pixel of row block ``index``, filling each column before the next."""
    block_w, block_h = _block_size(sizes)
    return (index // rows_per_col) * block_w, (index % rows_per_col) * block_h


def layout_cells(
    names: list[str], sizes: list[int] | None = None
) -> tuple[int, int, dict[str, dict[str, int]]]:
    """(atlas_w, atlas_h, {"name@size": {x,y,w,h}}) for ``names`` in order.

    The single source of truth for where a cell lands. make_icons draws into
    this layout and scripts/icon_pipeline.py re-lays the committed atlas out
    through it when a recipe introduces a name, so the two can never disagree
    about the grid.
    """
    sizes = sizes if sizes is not None else CELL_SIZES
    rows_per_col, atlas_w, atlas_h = atlas_geometry(len(names), sizes)
    row_h = max(sizes) + ROW_PAD
    cells: dict[str, dict[str, int]] = {}
    for i, name in enumerate(names):
        bx, by = block_origin(i, rows_per_col, sizes)
        x = bx
        for sz in sizes:
            cells[f"{name}@{sz}"] = {
                "x": x, "y": by + (row_h - sz) // 2, "w": sz, "h": sz,
            }
            x += sz + ROW_PAD
    return atlas_w, atlas_h, cells


# ---------------------------------------------------------------------------
# Rendered-tile protection.
#
# These cells are NOT the flat SDF glyphs this script draws — they are real
# model screenshots baked in by scripts/icon_pipeline.py (--render) and the CAM
# toolpath renderer. A plain `python make_icons.py` full-atlas regen would glyph
# over them and silently roll back curated art (bit us 2026-07-07 via b004f801).
#
# So main() preserves the existing rendered pixels by name: after drawing the
# glyph atlas it pastes each rendered cell back from the CURRENT committed atlas.
# To intentionally reset these to glyphs, set MAKE_ICONS_NO_PRESERVE=1 (then
# re-render via icon_pipeline.py). Any name starting with "cam_" is treated as
# rendered too, so new toolpath tiles are protected automatically.
RENDERED_RECIPE_NAMES: set[str] = {
    "appr_brass", "appr_dark", "appr_default", "appr_metal", "appr_plastic",
    "appr_steel", "chamfer", "circular_pattern", "combine", "curve_helix",
    "draft", "extrude_boss", "extrude_cut", "fillet", "hole_wizard", "intersect",
    "linear_pattern", "loft", "loft_cut", "mirror", "revolve_boss",
    "revolve_cut", "rib", "shell",
    # Sheet metal + weldments — engine-rendered tiles (icon_pipeline recipes).
    "sm_base_flange", "sm_edge_flange", "sm_flat_pattern", "sm_hem", "sm_jog",
    "split", "stock", "subtract", "sweep", "sweep_cut", "thread",
    "weld_end_cap", "weld_gusset", "weld_member",
    # ---- 2026-08-17: full 3D-render coverage. Every ribbon tool icon across
    # Sketch / Evaluate / Simulation / Assembly / ECAD / Schematic / workspaces
    # now ships an engine-rendered tile from scripts/icon_pipeline.py recipes;
    # the make_icons drawers above are fallback-only. ----
    "sketch", "line", "rect_corner", "rect_center", "rect_3pt_corner",
    "rect_3pt_center", "parallelogram", "circle", "circle_perimeter",
    "arc_3pt", "arc_center", "arc_tangent", "polygon", "poly_triangle",
    "poly_square", "poly_pentagon", "poly_octagon", "poly_star",
    "slot", "slot_ctr_str", "slot_arc_3pt", "slot_arc_ctr",
    "spline", "spline_style", "spline_surface", "spline_equation",
    "ellipse", "ellipse_partial", "parabola", "conic", "airfoil",
    "trim", "extend", "offset_ent", "convert_ent", "mirror_ent",
    "pattern_lin_ent", "pattern_circ_ent", "move_ent", "rotate_ent",
    "construction", "relations", "fix", "smart_dim", "point_ent", "text_ent",
    "display_relations", "fully_define",
    "study_static", "study_modal", "study_flow", "study_thermal",
    "study_fatigue", "fixture", "load_force", "load_pressure",
    "mesh", "mesh_tet", "mesh_hex", "mesh_auto", "run_study", "results",
    "flow_arrow", "ws_simulate",
    "measure", "mass_props", "section_props", "interference_eval",
    "draft_analysis", "thickness", "curvature", "zebra", "symmetry_check",
    "check", "geometry_analysis", "performance",
    "insert_component", "mate", "move_component", "rotate_component",
    "pattern_component", "smart_fasteners", "exploded_view", "bom",
    "interference",
    "boundary", "wrap", "ref_plane", "ref_axis", "ref_point", "ref_csys",
    "ws_model", "ws_drawing", "ws_electronics",
    "ecad_pcb", "ecad_board3d", "ecad_footprint", "ecad_route", "ecad_via",
    "ecad_zone", "ecad_autoroute", "ecad_fill", "ecad_gerber", "ecad_drill",
    "ecad_drc", "ecad_update",
    "sch_new", "sch_symbol", "sch_wire", "sch_power", "sch_label", "sch_erc",
    "sch_simulate",
}

# Scratch dir of engine-rendered tiles. Prefer these over whatever is already in
# the committed atlas so a rebuild after SDF-flatten still recovers photo art.
RENDER_DIR = Path(__file__).resolve().parent.parent / "scratch" / "icon_render"


def _is_rendered_name(name: str) -> bool:
    return name in RENDERED_RECIPE_NAMES or name.startswith("cam_")


def _fit_render_tile(src: Image.Image, size: int, pad_frac: float = 0.08) -> Image.Image:
    """Fit a transparent engine render into a square atlas cell (same contract as
    icon_pipeline.inject): aspect-preserving, small inset, no baked plate."""
    src = src.convert("RGBA")
    # Trim near-empty margins so the part fills the cell.
    alpha = src.split()[-1]
    bbox = alpha.getbbox()
    if bbox:
        src = src.crop(bbox)
    tw, th = src.size
    if tw <= 0 or th <= 0:
        return Image.new("RGBA", (size, size), (0, 0, 0, 0))
    pad = max(1, int(round(size * pad_frac)))
    inner = max(1, size - 2 * pad)
    scale = min(inner / tw, inner / th)
    nw = max(1, int(round(tw * scale)))
    nh = max(1, int(round(th * scale)))
    resized = src.resize((nw, nh), Image.LANCZOS)
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(resized, ((size - nw) // 2, (size - nh) // 2), resized)
    return out


def _preserve_rendered_tiles(atlas: Image.Image, cell_map: dict,
                             old_atlas_path: Path, old_json_path: Path) -> int:
    """Paste rendered (non-glyph) tiles back so a full glyph regen never clobbers
    curated model screenshots. Prefers scratch/icon_render/<name>.png when present
    (recovers after SDF-flatten / empty atlas cells); else falls back to the
    existing committed atlas cell. Returns count of cells restored."""
    old_cells = {}
    old_img = None
    if old_atlas_path.exists() and old_json_path.exists():
        old_cells = json.loads(old_json_path.read_text(encoding="utf-8")).get("cell", {})
        old_img = Image.open(old_atlas_path).convert("RGBA")

    n = 0
    # Cache fitted scratch renders per (name, size) so @32/@20 each fit once.
    fitted: dict[tuple[str, int], Image.Image] = {}
    for key, c in cell_map.items():
        name = key.rsplit("@", 1)[0]
        if not _is_rendered_name(name):
            continue
        sz = int(c["w"])
        tile = None
        cache_key = (name, sz)
        if cache_key in fitted:
            tile = fitted[cache_key]
        else:
            render_path = RENDER_DIR / f"{name}.png"
            if render_path.exists():
                try:
                    tile = _fit_render_tile(Image.open(render_path), sz)
                    fitted[cache_key] = tile
                except Exception:
                    tile = None
        if tile is None and old_img is not None:
            oc = old_cells.get(key)
            if oc and oc["w"] == c["w"] and oc["h"] == c["h"]:
                tile = old_img.crop((oc["x"], oc["y"], oc["x"] + oc["w"], oc["y"] + oc["h"]))
        if tile is None:
            continue
        # Skip empty / near-empty tiles so we don't paste blanks over glyphs.
        a = tile.split()[-1]
        if a.getbbox() is None:
            continue
        # Full-cell replace (no alpha mask): engine tiles have transparent margins;
        # masking would let the freshly-drawn glyph bleed through those pixels.
        atlas.paste(tile, (c["x"], c["y"]))
        n += 1
    return n


def _render_icon(name: str, cell: int, palette: Palette) -> Image.Image:
    s = cell * SS
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    fn = _REGISTRY.get(name, None)
    if fn is None:
        draw_missing(d, cell, palette)
    else:
        fn(d, cell, palette)
    return img.resize((cell, cell), Image.LANCZOS)


def build_atlas(palette: Palette) -> tuple[Image.Image, dict]:
    """Returns (atlas_image, cell_map)."""
    names = CONTRACT_NAMES
    total_w, total_h, cell_map = layout_cells(names)
    atlas = Image.new("RGBA", (total_w, total_h), (0, 0, 0, 0))
    for key, c in cell_map.items():
        icon_img = _render_icon(key.rsplit("@", 1)[0], c["w"], palette)
        atlas.paste(icon_img, (c["x"], c["y"]), icon_img)
    return atlas, cell_map


def main():
    fm_bad = validate_fm_manifest()
    if fm_bad:
        print("ERROR: FM_REFRESH_MANIFEST names not in CONTRACT_NAMES:", fm_bad)
    missing = check_coverage()
    if missing:
        print("ERROR: no draw function for:", missing)
        # still render with MISSING placeholder but raise at end
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    dark_atlas, dark_map   = build_atlas(DARK)
    light_atlas, light_map = build_atlas(LIGHT)

    # verify layouts identical
    assert dark_map == light_map, "Layout mismatch between dark and light atlases!"
    atlas_w, atlas_h = dark_atlas.size

    # Non-destructive: keep curated model-screenshot / CAM tiles (see
    # RENDERED_RECIPE_NAMES) instead of glyph-ing over them. Override with
    # MAKE_ICONS_NO_PRESERVE=1 to force a pure glyph atlas.
    if os.environ.get("MAKE_ICONS_NO_PRESERVE") == "1":
        print("  MAKE_ICONS_NO_PRESERVE=1 -> rendered tiles NOT preserved (pure glyph regen)")
    else:
        pd = _preserve_rendered_tiles(dark_atlas, dark_map,
                                      OUT_DIR / "atlas_dark.png", OUT_DIR / "atlas.json")
        pl = _preserve_rendered_tiles(light_atlas, light_map,
                                      OUT_DIR / "atlas_light.png", OUT_DIR / "atlas.json")
        print(f"  preserved {pd} dark / {pl} light rendered tiles "
              f"(recipe+cam_*); re-render via scripts/icon_pipeline.py --render")

    dark_atlas.save(OUT_DIR / "atlas_dark.png")
    light_atlas.save(OUT_DIR / "atlas_light.png")

    # Carry forward raster_only so the C++ SDF loader keeps skipping photo tiles
    # after a glyph regen. Union with every preserved recipe name that still has
    # a non-empty cell (or a scratch render) so missing keys self-heal.
    # ROOT CAUSE of "icons break every rebuild": this writer used to emit only
    # {atlas_px, cell}, silently dropping raster_only that icon_pipeline injects.
    prev_ro: set[str] = set()
    prev_pipeline: dict[str, object] = {}
    old_json = OUT_DIR / "atlas.json"
    if old_json.exists():
        try:
            prev = json.loads(old_json.read_text(encoding="utf-8"))
            prev_ro = set(prev.get("raster_only", []))
            # Same class of bug, one generalization later: PIPELINE_OWNED_ATLAS_KEYS
            # are injected by icon_pipeline and would be deleted by the wholesale
            # rewrite below, disabling accent retinting with nothing in any log.
            prev_pipeline = {k: prev[k] for k in PIPELINE_OWNED_ATLAS_KEYS if k in prev}
        except Exception:
            prev_ro = set()
            prev_pipeline = {}
    ro = set(prev_ro)
    for name in CONTRACT_NAMES:
        if not _is_rendered_name(name):
            continue
        has_render = (RENDER_DIR / f"{name}.png").exists()
        cell = dark_map.get(f"{name}@32")
        has_px = False
        if cell is not None:
            x, y, w, h = cell["x"], cell["y"], cell["w"], cell["h"]
            sample = dark_atlas.crop((x, y, x + w, y + h)).split()[-1]
            has_px = sample.getbbox() is not None
        if has_render or has_px:
            ro.add(name)
    # ...but a carried-forward name is a LIE once this script draws that cell as a
    # glyph. _preserve_rendered_tiles() only restores _is_rendered_name() tiles, so
    # any other raster_only entry that we hold a drawer for was just overwritten
    # with flat line art — and icons_sdf::build() skips SDF for raster_only names,
    # which would ship the new glyph as an untintable blurry blit. Bit ws_parts /
    # ws_manufacture: icon_pipeline had listed them here years before they had any
    # cell at all. Drop exactly those; dangling names with no cell stay so a later
    # icon_pipeline injection still self-heals.
    ro -= {n for n in ro
           if not _is_rendered_name(n) and n in _REGISTRY and n in set(CONTRACT_NAMES)}

    json_data = {
        "atlas_px": [atlas_w, atlas_h],
        "cell": dark_map,
        "raster_only": sorted(ro),
        **prev_pipeline,
    }
    with open(OUT_DIR / "atlas.json", "w", encoding="utf-8") as f:
        json.dump(json_data, f, indent=2)

    print(f"Wrote atlas_dark.png, atlas_light.png, atlas.json to {OUT_DIR}")
    print(f"  {len(CONTRACT_NAMES)} icons × {len(CELL_SIZES)} sizes = "
          f"{len(CONTRACT_NAMES)*len(CELL_SIZES)} cells")
    print(f"  FM refresh manifest: {len(FM_REFRESH_MANIFEST)} glyphs")
    print(f"  Atlas dimensions: {atlas_w} × {atlas_h} px")
    print(f"  raster_only: {len(ro)} photo/model tiles")
    carried = [k for k in PIPELINE_OWNED_ATLAS_KEYS if k in json_data]
    print(f"  carried from icon_pipeline: {', '.join(carried) if carried else 'none'}"
          " (run scripts/icon_pipeline.py --render if that is not what you expect)")

    if fm_bad:
        raise RuntimeError(f"FM manifest invalid: {fm_bad}")
    if missing:
        raise RuntimeError(f"Missing draw functions: {missing}")


def relayout_committed_atlas() -> tuple[int, int]:
    """Re-pack the COMMITTED atlas into the current grid without redrawing anything.

    Every cell is moved by name and copied pixel-for-pixel, and every top-level key
    other than ``cell``/``atlas_px`` — ``raster_only`` plus
    ``PIPELINE_OWNED_ATLAS_KEYS`` — rides across untouched, so this changes
    coordinates and nothing else. That matters: a full ``main()`` regen re-fits the
    ~170 engine-rendered tiles from scratch/icon_render and re-derives raster_only,
    which is the right thing when you actually want fresh art and the wrong thing
    when all you need is an atlas that fits in a texture.

    Use it when the committed atlas outgrows ATLAS_MAX_PX. Returns the new size.
    """
    old = json.loads((OUT_DIR / "atlas.json").read_text(encoding="utf-8"))
    old_cells = old["cell"]
    order = sorted(
        {k.rsplit("@", 1)[0] for k in old_cells},
        key=lambda n: (old_cells[f"{n}@32"]["x"], old_cells[f"{n}@32"]["y"]),
    )
    new_w, new_h, new_cells = layout_cells(order)
    for fn in ("atlas_dark.png", "atlas_light.png"):
        src = Image.open(OUT_DIR / fn).convert("RGBA")
        dst = Image.new("RGBA", (new_w, new_h), (0, 0, 0, 0))
        for key, c in new_cells.items():
            o = old_cells[key]
            dst.paste(
                src.crop((o["x"], o["y"], o["x"] + o["w"], o["y"] + o["h"])),
                (c["x"], c["y"]),
            )
        dst.save(OUT_DIR / fn)
    # Copy-with-overrides, not a fresh dict: narrowing the written object to the
    # keys this function computes would strip accent_tinted/accent_ref and silently
    # stop accent retinting from reaching the baked 3D tiles — the same failure the
    # carry-forward in main() exists to prevent, and just as invisible.
    new_atlas = {**old, "cell": new_cells, "atlas_px": [new_w, new_h]}
    (OUT_DIR / "atlas.json").write_text(json.dumps(new_atlas, indent=2), encoding="utf-8")
    print(f"relaid {len(order)} icons -> {new_w} × {new_h} px "
          f"(budget {ATLAS_MAX_PX} per side)")
    return new_w, new_h


if __name__ == "__main__":
    if "--relayout" in sys.argv[1:]:
        relayout_committed_atlas()
    else:
        main()
