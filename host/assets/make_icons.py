"""Generate AI-CAD icon atlases (light + dark) with Pillow.

Run:  python host/assets/make_icons.py
Outputs into host/assets/icons/:
  atlas_dark.png   atlas_light.png   atlas.json
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
from typing import Callable

from PIL import Image, ImageDraw

# ---------------------------------------------------------------------------
# Contract inventory  (parsed from docs/contracts/icons.md — mirrored here so
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
    "appr_paint", "appr_default",
    # Sketch shape drop-ins (polygon dropdown variants + star) and airfoil
    "poly_triangle", "poly_square", "poly_pentagon", "poly_octagon", "poly_star",
    "airfoil",
]

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

DARK = Palette(
    stroke  = (229, 234, 241),   # #E5EAF1
    accent  = (45, 212, 191),    # #2DD4BF
    fill    = (255, 255, 255, 30),
    bg      = (0, 0, 0, 0),
    accent2 = (94, 234, 212),
    ok      = (52, 211, 153),
    warn    = (251, 191, 36),
    err     = (239, 68, 68),
)
LIGHT = Palette(
    stroke  = (58, 67, 80),      # #3A4350
    accent  = (26, 140, 127),    # #1A8C7F
    fill    = (0, 0, 0, 20),
    bg      = (0, 0, 0, 0),
    accent2 = (31, 184, 166),
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
    """Isometric cube — the AI-CAD app icon glyph."""
    c = cell * SS / 2
    r = _s(cell, 0.30)
    lw = stroke_width(cell)
    iso_box(d, c, c, r, p.accent, lw)

@icon("part")
def draw_part(d, cell, p):
    """Shaded generic solid body."""
    s = cell * SS
    shaded_prism(d, s, s*0.20, s*0.34, s*0.47, s*0.37, s*0.15, s*0.11, p, accent=True)
    d.line([(s*0.25, s*0.50), (s*0.62, s*0.50)], fill=_rgba(p.stroke, 120),
           width=max(1, stroke_width(cell)//2))

@icon("assembly")
def draw_assembly(d, cell, p):
    """Three small parts connected — assembly."""
    s = cell * SS
    lw = stroke_width(cell)
    r = s * 0.14
    cx, cy = s/2, s/2
    # three small circles
    for ox, oy in [(0, -0.3), (-0.28, 0.18), (0.28, 0.18)]:
        xx, yy = cx + ox*s, cy + oy*s
        circle(d, xx, yy, r, outline=p.stroke, lw=lw)
    # connecting lines
    for ox, oy in [(0, -0.3), (-0.28, 0.18), (0.28, 0.18)]:
        d.line([(cx, cy), (cx + ox*s, cy + oy*s)], fill=p.accent, width=lw)

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
    """Vertical dashed axis line."""
    s = cell * SS
    lw = stroke_width(cell)
    arrow(d, s/2, s*0.85, s/2, s*0.1, p.accent, lw)

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
    arrow(d, ox, oy, ox - s*0.22, oy + s*0.22, p.accent, lw)  # Z teal

@icon("origin")
def draw_origin(d, cell, p):
    """Small sphere at crossing of 3 axes."""
    s = cell * SS
    lw = stroke_width(cell)
    cx, cy = s/2, s/2
    # simplified: crosshair + circle
    circle(d, cx, cy, s*0.12, outline=p.accent, lw=lw)
    d.line([(s*0.12, cy), (s*0.88, cy)], fill=p.stroke, width=lw)
    d.line([(cx, s*0.12), (cx, s*0.88)], fill=p.stroke, width=lw)

@icon("equations")
def draw_equations(d, cell, p):
    """'=' sign with small 'f(x)' letters implied — just two horizontal bars."""
    s = cell * SS
    lw = stroke_width(cell) * 2
    y1, y2 = s*0.40, s*0.60
    d.line([(s*0.2, y1), (s*0.8, y1)], fill=p.stroke, width=lw)
    d.line([(s*0.2, y2), (s*0.8, y2)], fill=p.stroke, width=lw)
    # small vertical on left = 'f'
    d.line([(s*0.25, s*0.2), (s*0.25, s*0.8)], fill=p.accent, width=lw)

@icon("material")
def draw_material(d, cell, p):
    """Layered stack = material swatch."""
    s = cell * SS
    lw = stroke_width(cell)
    for i, frac in enumerate([0.25, 0.45, 0.65]):
        y = s * frac
        col = p.accent if i == 1 else p.stroke
        d.rectangle([s*0.2, y, s*0.8, y + s*0.15], outline=col, width=lw)

@icon("mates_folder")
def draw_mates_folder(d, cell, p):
    """Folder outline with link symbol."""
    s = cell * SS
    lw = stroke_width(cell)
    # folder
    d.rounded_rectangle([s*0.1, s*0.3, s*0.9, s*0.82], radius=int(s*0.05),
                         outline=p.stroke, width=lw)
    d.rectangle([s*0.1, s*0.25, s*0.45, s*0.35], outline=p.stroke, width=lw)
    # link circles inside
    circle(d, s*0.42, s*0.58, s*0.09, outline=p.accent, lw=lw)
    circle(d, s*0.62, s*0.58, s*0.09, outline=p.accent, lw=lw)
    d.line([(s*0.51, s*0.58), (s*0.53, s*0.58)], fill=p.accent, width=lw)

@icon("body_folder")
def draw_body_folder(d, cell, p):
    """Folder with small solid body inside."""
    s = cell * SS
    lw = stroke_width(cell)
    d.rounded_rectangle([s*0.1, s*0.3, s*0.9, s*0.82], radius=int(s*0.05),
                         outline=p.stroke, width=lw)
    d.rectangle([s*0.1, s*0.25, s*0.45, s*0.35], outline=p.stroke, width=lw)
    # small rect body
    d.rounded_rectangle([s*0.35, s*0.46, s*0.68, s*0.70],
                         radius=int(s*0.04), outline=p.accent, width=lw)

@icon("history")
def draw_history(d, cell, p):
    """Clock/history: circle + arrow going counter-clockwise."""
    s = cell * SS
    lw = stroke_width(cell)
    cx, cy = s/2, s/2
    r = s * 0.32
    circle(d, cx, cy, r, outline=p.stroke, lw=lw)
    arc_segment(d, cx, cy, r, 0, 270, p.accent, lw*2)
    # arrow tip
    d.line([(cx + r*0.05, cy - r), (cx - r*0.22, cy - r*0.6)], fill=p.accent, width=lw)

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
    """Translucent parallelogram (reference plane)."""
    s = cell * SS
    lw = stroke_width(cell)
    pts = [(s*0.1, s*0.60), (s*0.45, s*0.22), (s*0.9, s*0.36), (s*0.55, s*0.74)]
    d.polygon(pts, fill=None, outline=p.accent)
    d.line([pts[0], pts[1], pts[2], pts[3], pts[0]], fill=p.accent, width=lw)

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

@icon("display_shaded")
def draw_display_shaded(d, cell, p):
    """Filled rounded square."""
    s = cell * SS
    lw = stroke_width(cell)
    d.rounded_rectangle([s*0.15, s*0.15, s*0.85, s*0.85], radius=int(s*0.12),
                         fill=(*p.accent[:3], 120), outline=p.stroke, width=lw)

@icon("display_shaded_edges")
def draw_display_shaded_edges(d, cell, p):
    """Filled cube with dark edges outline."""
    s = cell * SS
    lw = stroke_width(cell)
    # filled cube
    d.rounded_rectangle([s*0.15, s*0.15, s*0.85, s*0.85], radius=int(s*0.12),
                         fill=(*p.accent[:3], 120), outline=None)
    # dark outline for edges
    d.rounded_rectangle([s*0.15, s*0.15, s*0.85, s*0.85], radius=int(s*0.12),
                         fill=None, outline=p.stroke, width=lw*2)

@icon("display_hlr")
def draw_display_hlr(d, cell, p):
    """Outline cube (hidden line removal - visible edges only)."""
    s = cell * SS
    lw = stroke_width(cell)
    iso_box(d, s/2, s/2, s*0.28, p.stroke, lw)

@icon("display_hlv")
def draw_display_hlv(d, cell, p):
    """Outline cube with dashed hidden edges (hidden line visible)."""
    s = cell * SS
    lw = stroke_width(cell)
    # solid visible edges
    iso_box(d, s/2, s/2, s*0.28, p.stroke, lw)
    # dashed hidden edges
    c = s / 2
    r = s * 0.28
    t = (c, c - r * 0.7)
    rt = (c + r, c - r * 0.35)
    rm = (c + r, c + r * 0.15)
    # dashed line from left to right back edge
    segs = 3
    for i in range(segs):
        t0 = i / segs
        t1 = (i + 0.6) / segs
        x0 = t[0] + (rm[0] - t[0]) * t0
        y0 = t[1] + (rm[1] - t[1]) * t0
        x1 = t[0] + (rm[0] - t[0]) * t1
        y1 = t[1] + (rm[1] - t[1]) * t1
        col = p.accent if i % 2 == 0 else (p.accent[0], p.accent[1], p.accent[2], 80)
        d.line([(x0, y0), (x1, y1)], fill=col, width=lw)

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
    s = cell * SS
    lw = stroke_width(cell) * 2
    check_mark(d, s/2, s/2, s*0.32, p.ok, lw)

@icon("cancel_x")
def draw_cancel_x(d, cell, p):
    s = cell * SS
    lw = stroke_width(cell) * 2
    x_sign(d, s/2, s/2, s*0.32, p.err, lw)

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
    s = cell * SS
    lw = stroke_width(cell)
    cx, cy = s*0.52, s*0.52
    r = s*0.30
    arc_segment(d, cx, cy, r, 182, 360, p.stroke, lw*2)
    arc_segment(d, cx, cy, r, 0, 92, _rgba(p.accent, 190), lw*2)
    arrow(d, cx - r, cy, cx - r - s*0.11, cy - s*0.14, p.stroke, lw*2)
    filled_circle(d, cx + r*0.62, cy - r*0.52, max(1, lw//2), p.accent)

@icon("redo")
def draw_redo(d, cell, p):
    s = cell * SS
    lw = stroke_width(cell)
    cx, cy = s*0.48, s*0.52
    r = s*0.30
    arc_segment(d, cx, cy, r, 180, 358, p.stroke, lw*2)
    arc_segment(d, cx, cy, r, 88, 180, _rgba(p.accent, 190), lw*2)
    arrow(d, cx + r, cy, cx + r + s*0.11, cy - s*0.14, p.stroke, lw*2)
    filled_circle(d, cx - r*0.62, cy - r*0.52, max(1, lw//2), p.accent)

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
    """Workspace tab: Features — an extruded boss solid."""
    s = cell * SS
    shaded_prism(d, s, s * 0.22, s * 0.40, s * 0.42, s * 0.34, s * 0.18, s * 0.14, p, accent=True)

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

# ---- Appearance ----

@icon("color_picker")
def draw_color_picker(d, cell, p):
    """Eyedropper color picker over a color swatch."""
    s = cell * SS
    lw = stroke_width(cell)
    # color swatch (bottom-left)
    d.rounded_rectangle([s * 0.14, s * 0.62, s * 0.44, s * 0.86], radius=int(s * 0.04),
                        fill=_rgba(p.accent, 130), outline=p.stroke, width=max(1, lw // 2))
    # eyedropper body (diagonal) with squeeze bulb at the top-right
    bx0, by0 = s * 0.78, s * 0.18
    tx, ty = s * 0.40, s * 0.58
    d.line([(bx0, by0), (tx, ty)], fill=p.stroke, width=lw * 2)
    d.rounded_rectangle([bx0 - s * 0.04, by0 - s * 0.08, bx0 + s * 0.10, by0 + s * 0.06],
                        radius=int(s * 0.03), fill=_rgba(p.stroke, 60), outline=p.stroke, width=max(1, lw // 2))
    # picked-color drop at the tip
    filled_circle(d, tx, ty, lw * 1.1, p.accent)

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
    sizes = CELL_SIZES  # [32, 20]
    # layout: rows = names, columns = sizes
    # padding between cells
    pad = 2
    col_w = [s + pad for s in sizes]
    total_w = sum(col_w)
    row_h = [max(sizes) + pad] * len(names)  # uniform row height = 32+pad
    # actually pack tightly per row
    # each row height = the cell size for that row? No — rows contain both sizes.
    # We'll put @32 in column 0, @20 in column 1 — both in the same row, vertically centered.
    row_heights = [max(sizes) + pad] * len(names)
    total_h = sum(row_heights)

    atlas = Image.new("RGBA", (total_w, total_h), (0, 0, 0, 0))
    cell_map: dict[str, dict] = {}

    y = 0
    for i, name in enumerate(names):
        rh = row_heights[i]
        x = 0
        for j, sz in enumerate(sizes):
            icon_img = _render_icon(name, sz, palette)
            # center vertically in row
            y_off = (rh - sz) // 2
            atlas.paste(icon_img, (x, y + y_off), icon_img)
            key = f"{name}@{sz}"
            cell_map[key] = {"x": x, "y": y + y_off, "w": sz, "h": sz}
            x += col_w[j]
        y += rh

    return atlas, cell_map


def main():
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

    dark_atlas.save(OUT_DIR / "atlas_dark.png")
    light_atlas.save(OUT_DIR / "atlas_light.png")

    json_data = {
        "atlas_px": [atlas_w, atlas_h],
        "cell": dark_map,
    }
    with open(OUT_DIR / "atlas.json", "w", encoding="utf-8") as f:
        json.dump(json_data, f, indent=2)

    print(f"Wrote atlas_dark.png, atlas_light.png, atlas.json to {OUT_DIR}")
    print(f"  {len(CONTRACT_NAMES)} icons × {len(CELL_SIZES)} sizes = "
          f"{len(CONTRACT_NAMES)*len(CELL_SIZES)} cells")
    print(f"  Atlas dimensions: {atlas_w} × {atlas_h} px")

    if missing:
        raise RuntimeError(f"Missing draw functions: {missing}")


if __name__ == "__main__":
    main()
