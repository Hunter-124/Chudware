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
    # C25: sketch flyout variant icons (append-only)
    "rect_3pt_corner", "rect_3pt_center", "parallelogram",
    "circle_perimeter",
    "slot_ctr_str", "slot_arc_3pt", "slot_arc_ctr",
    "spline_style", "spline_surface", "spline_equation",
    "ellipse_partial", "parabola", "conic",
    # C25b: HUD hide/show visibility toggle (normal_to read as "arrow", not "visibility")
    "hide_show_eye",
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
    """Solid extrusion outline: rounded rect with depth lines."""
    s = cell * SS
    lw = stroke_width(cell)
    m = s * 0.15
    d.rounded_rectangle([m, m*1.5, s-m, s-m], radius=int(s*0.08),
                         outline=p.stroke, width=lw)
    # depth tab top-right
    offset = s * 0.12
    d.line([(s-m, m*1.5), (s-m+offset, m*1.2)], fill=p.accent, width=lw)
    d.line([(s-m+offset, m*1.2), (s-m+offset, s-m-offset)], fill=p.accent, width=lw)

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
    """Pencil on a grid square."""
    s = cell * SS
    lw = stroke_width(cell)
    m = s * 0.14
    d.rectangle([m, m, s-m, s-m], outline=p.stroke, width=lw)
    # pencil line diagonal
    d.line([(m*1.5, s-m*1.5), (s-m*1.5, m*1.5)], fill=p.accent, width=lw*2)

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

# ---- Features tab ----

@icon("extrude_boss")
def draw_extrude_boss(d, cell, p):
    """Profile square with up-arrow (add material)."""
    s = cell * SS
    lw = stroke_width(cell)
    d.rectangle([s*0.2, s*0.55, s*0.8, s*0.82], outline=p.stroke, width=lw)
    arrow(d, s/2, s*0.55, s/2, s*0.12, p.accent, lw)

@icon("extrude_cut")
def draw_extrude_cut(d, cell, p):
    """Profile square with down-arrow (remove material)."""
    s = cell * SS
    lw = stroke_width(cell)
    d.rectangle([s*0.2, s*0.18, s*0.8, s*0.48], outline=p.stroke, width=lw)
    arrow(d, s/2, s*0.48, s/2, s*0.85, p.err, lw)

@icon("revolve_boss")
def draw_revolve_boss(d, cell, p):
    """Half profile + revolution arc."""
    s = cell * SS
    lw = stroke_width(cell)
    d.line([(s*0.5, s*0.15), (s*0.5, s*0.85)], fill=p.accent, width=lw)  # axis
    d.rectangle([s*0.52, s*0.3, s*0.80, s*0.70], outline=p.stroke, width=lw)
    arc_segment(d, s*0.5, s*0.5, s*0.35, 270, 90, p.stroke, lw)

@icon("revolve_cut")
def draw_revolve_cut(d, cell, p):
    s = cell * SS
    lw = stroke_width(cell)
    d.line([(s*0.5, s*0.15), (s*0.5, s*0.85)], fill=p.accent, width=lw)
    d.rectangle([s*0.52, s*0.3, s*0.80, s*0.70], outline=p.err, width=lw)
    arc_segment(d, s*0.5, s*0.5, s*0.35, 270, 90, p.err, lw)

@icon("sweep")
def draw_sweep(d, cell, p):
    """Profile following a curved path."""
    s = cell * SS
    lw = stroke_width(cell)
    # path curve
    d.arc([s*0.1, s*0.1, s*0.9, s*0.9], start=200, end=340, fill=p.accent, width=lw)
    # profile rect at start
    d.rectangle([s*0.12, s*0.42, s*0.32, s*0.62], outline=p.stroke, width=lw)

@icon("sweep_cut")
def draw_sweep_cut(d, cell, p):
    s = cell * SS
    lw = stroke_width(cell)
    d.arc([s*0.1, s*0.1, s*0.9, s*0.9], start=200, end=340, fill=p.err, width=lw)
    d.rectangle([s*0.12, s*0.42, s*0.32, s*0.62], outline=p.err, width=lw)

@icon("loft")
def draw_loft(d, cell, p):
    """Two profiles connected with transition lines."""
    s = cell * SS
    lw = stroke_width(cell)
    d.rectangle([s*0.55, s*0.55, s*0.85, s*0.80], outline=p.stroke, width=lw)
    circle(d, s*0.3, s*0.32, s*0.14, outline=p.stroke, lw=lw)
    d.line([(s*0.44, s*0.32), (s*0.55, s*0.55)], fill=p.accent, width=lw)
    d.line([(s*0.44, s*0.35), (s*0.55, s*0.80)], fill=p.accent, width=lw)

@icon("loft_cut")
def draw_loft_cut(d, cell, p):
    s = cell * SS
    lw = stroke_width(cell)
    d.rectangle([s*0.55, s*0.55, s*0.85, s*0.80], outline=p.err, width=lw)
    circle(d, s*0.3, s*0.32, s*0.14, outline=p.err, lw=lw)
    d.line([(s*0.44, s*0.32), (s*0.55, s*0.55)], fill=p.err, width=lw)
    d.line([(s*0.44, s*0.35), (s*0.55, s*0.80)], fill=p.err, width=lw)

@icon("boundary")
def draw_boundary(d, cell, p):
    """Grid of boundary lines forming a patch."""
    s = cell * SS
    lw = stroke_width(cell)
    for frac in [0.3, 0.5, 0.7]:
        d.line([(s*0.15, s*frac), (s*0.85, s*frac)], fill=p.stroke, width=lw)
        d.line([(s*frac, s*0.15), (s*frac, s*0.85)], fill=p.accent, width=lw)

@icon("fillet")
def draw_fillet(d, cell, p):
    """Right-angle corner with rounded inner arc."""
    s = cell * SS
    lw = stroke_width(cell) * 2
    # two sides of corner
    d.line([(s*0.2, s*0.8), (s*0.2, s*0.3)], fill=p.stroke, width=lw)
    d.line([(s*0.2, s*0.8), (s*0.75, s*0.8)], fill=p.stroke, width=lw)
    # fillet arc
    r = s * 0.28
    d.arc([s*0.2, s*0.8 - 2*r, s*0.2 + 2*r, s*0.8], start=180, end=270, fill=p.accent, width=lw)

@icon("chamfer")
def draw_chamfer(d, cell, p):
    """Right-angle corner with cut diagonal."""
    s = cell * SS
    lw = stroke_width(cell) * 2
    d.line([(s*0.2, s*0.8), (s*0.2, s*0.45)], fill=p.stroke, width=lw)
    d.line([(s*0.55, s*0.8), (s*0.8, s*0.8)], fill=p.stroke, width=lw)
    d.line([(s*0.2, s*0.45), (s*0.55, s*0.80)], fill=p.accent, width=lw)

@icon("shell")
def draw_shell(d, cell, p):
    """Box with thin wall (hollow rectangle in rectangle)."""
    s = cell * SS
    lw = stroke_width(cell)
    d.rounded_rectangle([s*0.15, s*0.15, s*0.85, s*0.85], radius=int(s*0.06),
                         outline=p.stroke, width=lw)
    d.rounded_rectangle([s*0.30, s*0.30, s*0.70, s*0.70], radius=int(s*0.04),
                         outline=p.accent, width=lw)

@icon("draft")
def draw_draft(d, cell, p):
    """Tapered shape (trapezoid) indicating draft angle."""
    s = cell * SS
    lw = stroke_width(cell)
    pts = [(s*0.2, s*0.75), (s*0.8, s*0.75), (s*0.65, s*0.25), (s*0.35, s*0.25)]
    d.polygon(pts, fill=None, outline=p.stroke)
    d.line([pts[0], pts[1], pts[2], pts[3], pts[0]], fill=p.stroke, width=lw)
    # angle line
    d.line([(s*0.2, s*0.75), (s*0.35, s*0.25)], fill=p.accent, width=lw)

@icon("rib")
def draw_rib(d, cell, p):
    """T-shaped rib profile."""
    s = cell * SS
    lw = stroke_width(cell)
    # base
    d.line([(s*0.1, s*0.75), (s*0.9, s*0.75)], fill=p.stroke, width=lw*2)
    # rib stem
    d.line([(s*0.5, s*0.75), (s*0.5, s*0.25)], fill=p.accent, width=lw*2)

@icon("hole_wizard")
def draw_hole_wizard(d, cell, p):
    """Circle with depth lines — a drilled hole."""
    s = cell * SS
    lw = stroke_width(cell)
    cx, cy = s/2, s*0.42
    r = s * 0.28
    circle(d, cx, cy, r, outline=p.stroke, lw=lw)
    d.line([(cx - r*0.6, cy + r*0.8), (cx + r*0.6, cy + r*0.8)], fill=p.stroke, width=lw)
    arrow(d, cx, cy + r, cx, cy + r + s*0.22, p.accent, lw)  # depth arrow

@icon("linear_pattern")
def draw_linear_pattern(d, cell, p):
    """Row of three squares."""
    s = cell * SS
    lw = stroke_width(cell)
    sz = s * 0.20
    for i in range(3):
        x = s*(0.15 + i*0.28)
        d.rectangle([x, s*0.40, x+sz, s*0.60], outline=p.stroke if i > 0 else p.accent, width=lw)
    arrow(d, s*0.2, s*0.72, s*0.8, s*0.72, p.accent, lw)

@icon("circular_pattern")
def draw_circular_pattern(d, cell, p):
    """Five dots around a ring."""
    s = cell * SS
    lw = stroke_width(cell)
    cx, cy = s/2, s/2
    ring_r = s * 0.30
    dot_r  = s * 0.07
    for i in range(5):
        angle = math.radians(-90 + i * 72)
        dx = cx + ring_r * math.cos(angle)
        dy = cy + ring_r * math.sin(angle)
        col = p.accent if i == 0 else p.stroke
        filled_circle(d, dx, dy, dot_r, col)
    circle(d, cx, cy, ring_r, outline=p.stroke, lw=lw)

@icon("mirror")
def draw_mirror(d, cell, p):
    """Two mirrored L-shapes."""
    s = cell * SS
    lw = stroke_width(cell)
    # mirror line
    d.line([(s*0.5, s*0.1), (s*0.5, s*0.9)], fill=p.accent, width=lw)
    # left shape
    polyline(d, [(s*0.2, s*0.3), (s*0.4, s*0.3), (s*0.4, s*0.7)], p.stroke, lw)
    # right mirrored
    polyline(d, [(s*0.8, s*0.3), (s*0.6, s*0.3), (s*0.6, s*0.7)], p.stroke, lw)

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
    """Dashed reference axis."""
    s = cell * SS
    lw = stroke_width(cell)
    # dashed line (alternate segments)
    segs = 5
    for i in range(segs):
        t0 = i / segs
        t1 = (i + 0.6) / segs
        y0 = s*0.1 + (s*0.8) * t0
        y1 = s*0.1 + (s*0.8) * t1
        col = p.accent if i % 2 == 0 else (p.accent[0], p.accent[1], p.accent[2], 80)
        d.line([(s*0.5, y0), (s*0.5, y1)], fill=col, width=lw)

@icon("ref_point")
def draw_ref_point(d, cell, p):
    """Small diamond (reference point)."""
    s = cell * SS
    lw = stroke_width(cell)
    cx, cy = s/2, s/2
    r = s * 0.22
    pts = [(cx, cy-r), (cx+r, cy), (cx, cy+r), (cx-r, cy)]
    d.polygon(pts, fill=None, outline=p.accent)
    d.line([pts[0], pts[1], pts[2], pts[3], pts[0]], fill=p.accent, width=lw)

@icon("ref_csys")
def draw_ref_csys(d, cell, p):
    """Smaller XYZ triad with dashed lines — reference CSYS."""
    s = cell * SS
    lw = stroke_width(cell)
    ox, oy = s*0.38, s*0.62
    for color, ex, ey in [(p.err, s*0.75, s*0.62), (p.ok, s*0.38, s*0.25),
                           (p.accent, s*0.18, s*0.82)]:
        d.line([(ox, oy), (ex, ey)], fill=color, width=lw)
    circle(d, ox, oy, lw*0.8, fill=p.stroke)

@icon("curve_helix")
def draw_curve_helix(d, cell, p):
    """Spring/helix: stacked arcs."""
    s = cell * SS
    lw = stroke_width(cell)
    cx = s / 2
    r = s * 0.30
    for i, (y_off, a0, a1) in enumerate([
        (s*0.22, 0, 180), (s*0.40, 180, 360), (s*0.58, 0, 180)
    ]):
        d.arc([cx-r, y_off-r*0.4, cx+r, y_off+r*0.4], start=a0, end=a1,
              fill=p.accent if i % 2 else p.stroke, width=lw)

@icon("combine")
def draw_combine(d, cell, p):
    """Two overlapping circles with union-fill."""
    s = cell * SS
    lw = stroke_width(cell)
    r = s * 0.27
    circle(d, s*0.38, s*0.5, r, outline=p.stroke, lw=lw)
    circle(d, s*0.62, s*0.5, r, outline=p.accent, lw=lw)
    plus_sign(d, s*0.5, s*0.5, lw*1.5, p.accent, lw)

@icon("intersect")
def draw_intersect(d, cell, p):
    """Two circles, intersection region highlighted."""
    s = cell * SS
    lw = stroke_width(cell)
    r = s * 0.27
    circle(d, s*0.38, s*0.5, r, outline=p.stroke, lw=lw)
    circle(d, s*0.62, s*0.5, r, outline=p.accent, lw=lw)
    # intersection lens
    d.arc([s*0.62-r, s*0.5-r, s*0.62+r, s*0.5+r], start=130, end=230, fill=p.accent, width=lw*2)
    d.arc([s*0.38-r, s*0.5-r, s*0.38+r, s*0.5+r], start=310, end=50,  fill=p.accent, width=lw*2)

@icon("subtract")
def draw_subtract(d, cell, p):
    """Large circle minus small circle."""
    s = cell * SS
    lw = stroke_width(cell)
    r = s * 0.27
    circle(d, s*0.42, s*0.5, r, outline=p.stroke, lw=lw)
    circle(d, s*0.64, s*0.5, r*0.65, outline=p.err, lw=lw)
    # minus sign
    d.line([(s*0.56, s*0.5), (s*0.72, s*0.5)], fill=p.err, width=lw)

@icon("split")
def draw_split(d, cell, p):
    """Rectangle split by a vertical cutting line."""
    s = cell * SS
    lw = stroke_width(cell)
    d.rectangle([s*0.15, s*0.25, s*0.85, s*0.75], outline=p.stroke, width=lw)
    d.line([(s*0.5, s*0.18), (s*0.5, s*0.82)], fill=p.err, width=lw*2)

@icon("wrap")
def draw_wrap(d, cell, p):
    """Flat rectangle wrapping around a cylinder."""
    s = cell * SS
    lw = stroke_width(cell)
    # cylinder ellipse
    d.ellipse([s*0.22, s*0.15, s*0.78, s*0.45], outline=p.stroke, width=lw)
    d.line([(s*0.22, s*0.30), (s*0.22, s*0.72)], fill=p.stroke, width=lw)
    d.line([(s*0.78, s*0.30), (s*0.78, s*0.72)], fill=p.stroke, width=lw)
    d.ellipse([s*0.22, s*0.55, s*0.78, s*0.85], outline=p.accent, width=lw)
    # wrap line on surface
    d.arc([s*0.22, s*0.30, s*0.78, s*0.60], start=0, end=180, fill=p.accent, width=lw)

# ---- Sketch tab ----

@icon("sketch")
def draw_sketch(d, cell, p):
    """Pencil over a square plane."""
    s = cell * SS
    lw = stroke_width(cell)
    d.rectangle([s*0.15, s*0.15, s*0.75, s*0.75], outline=p.stroke, width=lw)
    # pencil diagonal
    pencil_pts = [(s*0.62, s*0.22), (s*0.82, s*0.42), (s*0.72, s*0.60), (s*0.52, s*0.40)]
    d.polygon(pencil_pts, fill=None, outline=p.accent)
    d.line([pencil_pts[0], pencil_pts[1], pencil_pts[2], pencil_pts[3], pencil_pts[0]],
           fill=p.accent, width=lw)

@icon("smart_dim")
def draw_smart_dim(d, cell, p):
    """Dimension line with arrows and value bar."""
    s = cell * SS
    lw = stroke_width(cell)
    y = s * 0.6
    arrow(d, s*0.15, y, s*0.85, y, p.accent, lw)
    d.line([(s*0.15, y - s*0.1), (s*0.15, y + s*0.1)], fill=p.stroke, width=lw)
    d.line([(s*0.85, y - s*0.1), (s*0.85, y + s*0.1)], fill=p.stroke, width=lw)
    d.line([(s*0.25, s*0.22), (s*0.25, y)], fill=p.stroke, width=lw)  # entity

@icon("line")
def draw_line(d, cell, p):
    s = cell * SS
    lw = stroke_width(cell) * 2
    d.line([(s*0.15, s*0.75), (s*0.85, s*0.25)], fill=p.stroke, width=lw)
    filled_circle(d, s*0.15, s*0.75, lw*0.8, p.accent)
    filled_circle(d, s*0.85, s*0.25, lw*0.8, p.accent)

@icon("rect_corner")
def draw_rect_corner(d, cell, p):
    s = cell * SS
    lw = stroke_width(cell)
    d.rectangle([s*0.15, s*0.25, s*0.85, s*0.75], outline=p.stroke, width=lw)
    filled_circle(d, s*0.15, s*0.25, lw, p.accent)

@icon("rect_center")
def draw_rect_center(d, cell, p):
    s = cell * SS
    lw = stroke_width(cell)
    d.rectangle([s*0.15, s*0.25, s*0.85, s*0.75], outline=p.stroke, width=lw)
    filled_circle(d, s*0.5, s*0.5, lw, p.accent)
    d.line([(s*0.15, s*0.5), (s*0.85, s*0.5)], fill=p.accent, width=lw)
    d.line([(s*0.5, s*0.25), (s*0.5, s*0.75)], fill=p.accent, width=lw)

@icon("circle")
def draw_circle(d, cell, p):
    s = cell * SS
    lw = stroke_width(cell) * 2
    circle(d, s/2, s/2, s*0.32, outline=p.stroke, lw=lw)
    filled_circle(d, s/2, s/2, lw*0.7, p.accent)

@icon("arc_3pt")
def draw_arc_3pt(d, cell, p):
    s = cell * SS
    lw = stroke_width(cell) * 2
    arc_segment(d, s/2, s*0.65, s*0.38, 210, 330, p.stroke, lw)
    for x, y in [(s*0.2, s*0.82), (s*0.5, s*0.28), (s*0.8, s*0.82)]:
        filled_circle(d, x, y, lw, p.accent)

@icon("arc_center")
def draw_arc_center(d, cell, p):
    s = cell * SS
    lw = stroke_width(cell) * 2
    cx, cy = s*0.5, s*0.65
    arc_segment(d, cx, cy, s*0.35, 210, 330, p.stroke, lw)
    filled_circle(d, cx, cy, lw, p.accent)  # center point

@icon("arc_tangent")
def draw_arc_tangent(d, cell, p):
    s = cell * SS
    lw = stroke_width(cell) * 2
    # line first, then tangent arc
    d.line([(s*0.15, s*0.65), (s*0.5, s*0.65)], fill=p.stroke, width=lw)
    arc_segment(d, s*0.5, s*0.65, s*0.3, 270, 0, p.accent, lw)

@icon("polygon")
def draw_polygon(d, cell, p):
    s = cell * SS
    lw = stroke_width(cell)
    cx, cy, r, n = s/2, s/2, s*0.35, 6
    pts = [(cx + r*math.cos(math.radians(-90 + i*360/n)),
            cy + r*math.sin(math.radians(-90 + i*360/n))) for i in range(n)]
    d.polygon(pts, fill=None, outline=p.stroke)
    d.line(pts + [pts[0]], fill=p.stroke, width=lw)

@icon("slot")
def draw_slot(d, cell, p):
    """Oblong slot shape."""
    s = cell * SS
    lw = stroke_width(cell)
    d.rounded_rectangle([s*0.15, s*0.35, s*0.85, s*0.65], radius=int(s*0.15),
                         outline=p.stroke, width=lw)

@icon("spline")
def draw_spline(d, cell, p):
    """Smooth S-curve."""
    s = cell * SS
    lw = stroke_width(cell) * 2
    # approximate S-curve with arcs
    d.arc([s*0.15, s*0.1, s*0.65, s*0.6], start=200, end=360, fill=p.stroke, width=lw)
    d.arc([s*0.35, s*0.4, s*0.85, s*0.9], start=20, end=180, fill=p.stroke, width=lw)

@icon("ellipse")
def draw_ellipse(d, cell, p):
    s = cell * SS
    lw = stroke_width(cell) * 2
    d.ellipse([s*0.12, s*0.28, s*0.88, s*0.72], outline=p.stroke, width=lw)
    # axes
    d.line([(s*0.12, s*0.5), (s*0.88, s*0.5)], fill=p.accent, width=lw//2)
    d.line([(s*0.5, s*0.28), (s*0.5, s*0.72)], fill=p.accent, width=lw//2)

@icon("point_ent")
def draw_point_ent(d, cell, p):
    s = cell * SS
    lw = stroke_width(cell)
    cx, cy = s/2, s/2
    filled_circle(d, cx, cy, lw*1.5, p.accent)
    d.line([(cx - s*0.2, cy), (cx + s*0.2, cy)], fill=p.stroke, width=lw)
    d.line([(cx, cy - s*0.2), (cx, cy + s*0.2)], fill=p.stroke, width=lw)

@icon("text_ent")
def draw_text_ent(d, cell, p):
    """'T' glyph."""
    s = cell * SS
    lw = stroke_width(cell) * 2
    d.line([(s*0.2, s*0.22), (s*0.8, s*0.22)], fill=p.stroke, width=lw)
    d.line([(s*0.5, s*0.22), (s*0.5, s*0.80)], fill=p.stroke, width=lw)

@icon("trim")
def draw_trim(d, cell, p):
    """Scissors cutting a line; the trimmed-off segment is dashed (removed)."""
    s = cell * SS
    lw = stroke_width(cell)
    y = s * 0.50
    cut = s * 0.50
    # kept segment (solid) up to the cut point
    d.line([(s*0.08, y), (cut, y)], fill=p.stroke, width=lw * 2)
    # trimmed-off segment (dashed, in 'remove' red) past the cut
    seg = s * 0.09
    x = cut + seg * 0.8
    while x < s * 0.94:
        d.line([(x, y), (min(x + seg, s*0.94), y)], fill=p.err, width=lw)
        x += seg * 1.8
    # scissors at the cut: two blades opening right + two finger loops on the handles
    bx, by = cut, y
    d.line([(bx, by), (bx + s*0.16, by - s*0.18)], fill=p.accent, width=lw)
    d.line([(bx, by), (bx + s*0.16, by + s*0.18)], fill=p.accent, width=lw)
    filled_circle(d, bx, by, lw * 0.7, p.accent)
    circle(d, bx - s*0.12, by - s*0.13, s*0.06, outline=p.accent, lw=lw)
    circle(d, bx - s*0.12, by + s*0.13, s*0.06, outline=p.accent, lw=lw)
    d.line([(bx, by), (bx - s*0.07, by - s*0.07)], fill=p.accent, width=lw)
    d.line([(bx, by), (bx - s*0.07, by + s*0.07)], fill=p.accent, width=lw)

@icon("extend")
def draw_extend(d, cell, p):
    """Solid line extended (accent dashed continuation) reaching a boundary wall with an arrow."""
    s = cell * SS
    lw = stroke_width(cell)
    y = s * 0.50
    wall = s * 0.86
    # existing entity (solid)
    d.line([(s*0.10, y), (s*0.45, y)], fill=p.stroke, width=lw * 2)
    # extension reaching toward the boundary (accent arrow)
    arrow(d, s*0.45, y, wall - lw, y, p.accent, lw)
    # boundary wall (vertical) the entity extends to
    d.line([(wall, s*0.16), (wall, s*0.84)], fill=p.stroke, width=lw * 2)

@icon("offset_ent")
def draw_offset_ent(d, cell, p):
    s = cell * SS
    lw = stroke_width(cell)
    d.rounded_rectangle([s*0.15, s*0.25, s*0.75, s*0.75], radius=int(s*0.08),
                         outline=p.stroke, width=lw)
    d.rounded_rectangle([s*0.25, s*0.35, s*0.85, s*0.85], radius=int(s*0.08),
                         outline=p.accent, width=lw)

@icon("convert_ent")
def draw_convert_ent(d, cell, p):
    """Two stacked rects (model face -> sketch entity) with an arrow showing the conversion."""
    s = cell * SS
    lw = stroke_width(cell)
    # source (model edge/face) — upper-left, solid stroke
    d.rectangle([s*0.12, s*0.14, s*0.50, s*0.46], outline=p.stroke, width=lw)
    # converted result — lower-right, accent (the new sketch entity)
    d.rectangle([s*0.50, s*0.54, s*0.88, s*0.86], outline=p.accent, width=lw)
    # arrow from source to result
    arrow(d, s*0.40, s*0.42, s*0.60, s*0.58, p.accent, lw)

@icon("mirror_ent")
def draw_mirror_ent(d, cell, p):
    """Sketch mirror — two lines either side of centerline."""
    s = cell * SS
    lw = stroke_width(cell)
    d.line([(s*0.5, s*0.1), (s*0.5, s*0.9)], fill=p.accent, width=lw)
    polyline(d, [(s*0.2, s*0.3), (s*0.38, s*0.5), (s*0.2, s*0.7)], p.stroke, lw)
    polyline(d, [(s*0.8, s*0.3), (s*0.62, s*0.5), (s*0.8, s*0.7)], p.stroke, lw)

@icon("pattern_lin_ent")
def draw_pattern_lin_ent(d, cell, p):
    """Three dots in a row."""
    s = cell * SS
    lw = stroke_width(cell)
    r = s * 0.09
    for x in [0.25, 0.50, 0.75]:
        col = p.accent if x == 0.25 else p.stroke
        circle(d, s*x, s*0.5, r, outline=col, lw=lw)
    arrow(d, s*0.2, s*0.72, s*0.8, s*0.72, p.accent, lw)

@icon("pattern_circ_ent")
def draw_pattern_circ_ent(d, cell, p):
    s = cell * SS
    lw = stroke_width(cell)
    cx, cy = s/2, s/2
    ring_r = s * 0.28
    dot_r  = s * 0.07
    circle(d, cx, cy, ring_r, outline=p.stroke, lw=lw)
    for i in range(4):
        angle = math.radians(i * 90)
        dx = cx + ring_r * math.cos(angle)
        dy = cy + ring_r * math.sin(angle)
        col = p.accent if i == 0 else p.stroke
        filled_circle(d, dx, dy, dot_r, col)

@icon("move_ent")
def draw_move_ent(d, cell, p):
    s = cell * SS
    lw = stroke_width(cell)
    cx, cy = s/2, s/2
    r = s * 0.16
    d.rounded_rectangle([cx-r, cy-r, cx+r, cy+r], radius=int(r*0.3),
                         outline=p.stroke, width=lw)
    m = s * 0.32
    for ax, ay, bx, by in [(cx, cy-m, cx, cy-r-2), (cx, cy+m, cx, cy+r+2),
                             (cx-m, cy, cx-r-2, cy), (cx+m, cy, cx+r+2, cy)]:
        arrow(d, bx, by, ax, ay, p.accent, lw)

@icon("construction")
def draw_construction(d, cell, p):
    """Dashed line (construction geometry)."""
    s = cell * SS
    lw = stroke_width(cell) * 2
    segs = 4
    for i in range(segs):
        t0 = (i + 0.1) / segs
        t1 = (i + 0.8) / segs
        d.line([(s*0.15 + (s*0.7)*t0, s*0.75 - (s*0.5)*t0),
                (s*0.15 + (s*0.7)*t1, s*0.75 - (s*0.5)*t1)],
               fill=p.accent, width=lw)

@icon("relations")
def draw_relations(d, cell, p):
    """Two point dots with constraint symbol (=)."""
    s = cell * SS
    lw = stroke_width(cell)
    filled_circle(d, s*0.25, s*0.5, lw, p.stroke)
    filled_circle(d, s*0.75, s*0.5, lw, p.stroke)
    d.line([(s*0.25, s*0.5), (s*0.75, s*0.5)], fill=p.stroke, width=lw)
    # = symbol mid
    d.line([(s*0.42, s*0.43), (s*0.58, s*0.43)], fill=p.accent, width=lw)
    d.line([(s*0.42, s*0.57), (s*0.58, s*0.57)], fill=p.accent, width=lw)

@icon("fix")
def draw_fix(d, cell, p):
    """Lock/anchor symbol."""
    s = cell * SS
    lw = stroke_width(cell)
    # shackle
    arc_segment(d, s*0.5, s*0.42, s*0.2, 180, 360, p.stroke, lw)
    # body
    d.rounded_rectangle([s*0.3, s*0.48, s*0.7, s*0.80], radius=int(s*0.05),
                         outline=p.stroke, width=lw)
    filled_circle(d, s*0.5, s*0.64, lw, p.accent)

# ---- Assembly tab ----

@icon("insert_component")
def draw_insert_component(d, cell, p):
    s = cell * SS
    lw = stroke_width(cell)
    # box with down-arrow (drop in)
    d.rounded_rectangle([s*0.2, s*0.45, s*0.75, s*0.82], radius=int(s*0.05),
                         outline=p.stroke, width=lw)
    arrow(d, s*0.5, s*0.12, s*0.5, s*0.45, p.accent, lw)

@icon("mate")
def draw_mate(d, cell, p):
    """Two faces touching — paperclip-like."""
    s = cell * SS
    lw = stroke_width(cell)
    # two half-circles opening toward each other
    arc_segment(d, s*0.38, s*0.5, s*0.22, 90, 270, p.stroke, lw)
    arc_segment(d, s*0.62, s*0.5, s*0.22, 270, 90, p.accent, lw)
    d.line([(s*0.38, s*0.28), (s*0.62, s*0.28)], fill=p.stroke, width=lw)
    d.line([(s*0.38, s*0.72), (s*0.62, s*0.72)], fill=p.stroke, width=lw)

@icon("move_component")
def draw_move_component(d, cell, p):
    s = cell * SS
    lw = stroke_width(cell)
    cx, cy = s/2, s/2
    d.rounded_rectangle([cx - s*0.2, cy - s*0.2, cx + s*0.2, cy + s*0.2],
                         radius=int(s*0.04), outline=p.stroke, width=lw)
    m = s * 0.35
    arrow(d, cx, cy, cx + m, cy, p.accent, lw)
    arrow(d, cx, cy, cx, cy - m, p.accent, lw)

@icon("rotate_component")
def draw_rotate_component(d, cell, p):
    s = cell * SS
    lw = stroke_width(cell)
    cx, cy = s/2, s/2
    d.rounded_rectangle([cx - s*0.18, cy - s*0.18, cx + s*0.18, cy + s*0.18],
                         radius=int(s*0.04), outline=p.stroke, width=lw)
    r = s*0.36
    arc_segment(d, cx, cy, r, 270, 180, p.accent, lw)
    arrow(d, cx - r, cy, cx - r - s*0.1, cy - s*0.12, p.accent, lw)

@icon("pattern_component")
def draw_pattern_component(d, cell, p):
    s = cell * SS
    lw = stroke_width(cell)
    sz = s * 0.20
    for xi in range(2):
        for yi in range(2):
            x = s*(0.18 + xi*0.35)
            y = s*(0.25 + yi*0.35)
            col = p.accent if (xi == 0 and yi == 0) else p.stroke
            d.rounded_rectangle([x, y, x+sz, y+sz], radius=int(sz*0.15),
                                 outline=col, width=lw)

@icon("smart_fasteners")
def draw_smart_fasteners(d, cell, p):
    """Bolt/nut cross-section."""
    s = cell * SS
    lw = stroke_width(cell)
    cx, cy = s/2, s*0.42
    r = s * 0.20
    # hex
    pts = [(cx + r*math.cos(math.radians(-90 + i*60)),
            cy + r*math.sin(math.radians(-90 + i*60))) for i in range(6)]
    d.polygon(pts, fill=None, outline=p.stroke)
    d.line(pts + [pts[0]], fill=p.stroke, width=lw)
    # thread shank
    d.rectangle([cx-lw*1.5, cy+r, cx+lw*1.5, cy+r+s*0.3], fill=p.accent)

@icon("exploded_view")
def draw_exploded_view(d, cell, p):
    """Three pieces flying outward from a center."""
    s = cell * SS
    lw = stroke_width(cell)
    cx, cy = s/2, s/2
    for angle, r_inner, r_outer in [(315, 0.12, 0.38), (180, 0.12, 0.38), (90, 0.12, 0.38)]:
        a = math.radians(angle)
        x0 = cx + r_inner*s*math.cos(a)
        y0 = cy + r_inner*s*math.sin(a)
        x1 = cx + r_outer*s*math.cos(a)
        y1 = cy + r_outer*s*math.sin(a)
        d.line([(x0, y0), (x1, y1)], fill=p.accent, width=lw)
        sz = s * 0.10
        d.rectangle([x1 - sz, y1 - sz, x1 + sz, y1 + sz], outline=p.stroke, width=lw)

@icon("bom")
def draw_bom(d, cell, p):
    """Table rows (bill of materials)."""
    s = cell * SS
    lw = stroke_width(cell)
    # header
    d.rectangle([s*0.12, s*0.20, s*0.88, s*0.36], outline=p.accent, width=lw)
    # rows
    for i in range(3):
        y = s*(0.38 + i*0.16)
        d.rectangle([s*0.12, y, s*0.88, y + s*0.14], outline=p.stroke, width=lw)
        d.line([(s*0.35, y), (s*0.35, y + s*0.14)], fill=p.stroke, width=lw)
        d.line([(s*0.65, y), (s*0.65, y + s*0.14)], fill=p.stroke, width=lw)

@icon("interference")
def draw_interference(d, cell, p):
    """Two overlapping solid rects, overlap in red."""
    s = cell * SS
    lw = stroke_width(cell)
    d.rectangle([s*0.15, s*0.3, s*0.62, s*0.75], outline=p.stroke, width=lw)
    d.rectangle([s*0.38, s*0.25, s*0.85, s*0.70], outline=p.stroke, width=lw)
    # overlap zone
    d.rectangle([s*0.38, s*0.30, s*0.62, s*0.70], outline=p.err, width=lw*2)

# ---- Evaluate tab ----

@icon("measure")
def draw_measure(d, cell, p):
    """Ruler diagonal with tick marks."""
    s = cell * SS
    lw = stroke_width(cell) * 2
    d.line([(s*0.15, s*0.85), (s*0.85, s*0.15)], fill=p.stroke, width=lw)
    for i in range(4):
        t = (i + 1) / 5
        px = s*0.15 + (s*0.7)*t
        py = s*0.85 - (s*0.7)*t
        perp_dx, perp_dy = 0.07*s, 0.07*s
        d.line([(px - perp_dy, py - perp_dx), (px + perp_dy, py + perp_dx)],
               fill=p.accent, width=lw//2)

@icon("mass_props")
def draw_mass_props(d, cell, p):
    """Scale/balance icon."""
    s = cell * SS
    lw = stroke_width(cell)
    cx = s/2
    # pivot
    d.line([(cx, s*0.25), (cx, s*0.65)], fill=p.stroke, width=lw)
    # beam
    d.line([(s*0.18, s*0.38), (s*0.82, s*0.38)], fill=p.stroke, width=lw)
    # pans
    for x in [s*0.22, s*0.78]:
        d.line([(x, s*0.38), (x, s*0.62)], fill=p.stroke, width=lw)
        d.ellipse([x - s*0.12, s*0.62, x + s*0.12, s*0.72], outline=p.accent, width=lw)

@icon("section_props")
def draw_section_props(d, cell, p):
    """I-beam cross-section."""
    s = cell * SS
    lw = stroke_width(cell)
    # flanges
    d.line([(s*0.2, s*0.22), (s*0.8, s*0.22)], fill=p.stroke, width=lw*2)
    d.line([(s*0.2, s*0.78), (s*0.8, s*0.78)], fill=p.stroke, width=lw*2)
    # web
    d.line([(s*0.5, s*0.22), (s*0.5, s*0.78)], fill=p.accent, width=lw*2)

@icon("interference_eval")
def draw_interference_eval(d, cell, p):
    """Overlap rects + warning badge."""
    s = cell * SS
    lw = stroke_width(cell)
    d.rectangle([s*0.15, s*0.35, s*0.58, s*0.72], outline=p.stroke, width=lw)
    d.rectangle([s*0.42, s*0.30, s*0.82, s*0.68], outline=p.stroke, width=lw)
    d.rectangle([s*0.42, s*0.35, s*0.58, s*0.68], outline=p.err, width=lw*2)
    filled_circle(d, s*0.78, s*0.72, s*0.1, p.warn)

@icon("draft_analysis")
def draw_draft_analysis(d, cell, p):
    """Shaded trapezoid (color map for draft)."""
    s = cell * SS
    lw = stroke_width(cell)
    pts = [(s*0.2, s*0.75), (s*0.8, s*0.75), (s*0.65, s*0.25), (s*0.35, s*0.25)]
    d.polygon(pts, fill=(*p.accent[:3], 60))
    d.line([pts[0], pts[1], pts[2], pts[3], pts[0]], fill=p.stroke, width=lw)
    # gradient bars right side
    for i, col in enumerate([p.ok, p.accent, p.warn, p.err]):
        y = s*(0.28 + i*0.12)
        d.rectangle([s*0.82, y, s*0.90, y + s*0.10], fill=col)

@icon("thickness")
def draw_thickness(d, cell, p):
    """Two parallel lines with arrow between them."""
    s = cell * SS
    lw = stroke_width(cell) * 2
    d.line([(s*0.15, s*0.30), (s*0.85, s*0.30)], fill=p.stroke, width=lw)
    d.line([(s*0.15, s*0.70), (s*0.85, s*0.70)], fill=p.stroke, width=lw)
    arrow(d, s*0.5, s*0.30, s*0.5, s*0.70, p.accent, lw)

@icon("curvature")
def draw_curvature(d, cell, p):
    """Curve with comb-like curvature spines."""
    s = cell * SS
    lw = stroke_width(cell)
    # base curve (arc)
    arc_segment(d, s*0.5, s*1.0, s*0.65, 210, 330, p.stroke, lw*2)
    # combs
    for angle in range(215, 330, 25):
        a = math.radians(angle)
        cx = s*0.5 + s*0.65*math.cos(a)
        cy = s*1.0 + s*0.65*math.sin(a)
        ex = cx + s*0.18*math.cos(a)
        ey = cy + s*0.18*math.sin(a)
        d.line([(cx, cy), (ex, ey)], fill=p.accent, width=lw)

@icon("zebra")
def draw_zebra(d, cell, p):
    """Alternating stripes on a surface."""
    s = cell * SS
    lw = stroke_width(cell)
    r = s * 0.35
    cx, cy = s/2, s/2
    circle(d, cx, cy, r, outline=p.stroke, lw=lw)
    # zebra stripes (horizontal, clipped by circle implicitly)
    for i in range(4):
        y = cy - r + (2*r) * (i + 0.25) / 4
        half = math.sqrt(max(0, r*r - (y-cy)**2))
        col = p.stroke if i % 2 == 0 else p.accent
        d.line([(cx - half, y), (cx + half, y)], fill=col, width=lw*2)

@icon("symmetry_check")
def draw_symmetry_check(d, cell, p):
    s = cell * SS
    lw = stroke_width(cell)
    d.line([(s*0.5, s*0.1), (s*0.5, s*0.9)], fill=p.accent, width=lw)
    d.rounded_rectangle([s*0.55, s*0.3, s*0.82, s*0.7], radius=int(s*0.05),
                         outline=p.stroke, width=lw)
    d.rounded_rectangle([s*0.18, s*0.3, s*0.45, s*0.7], radius=int(s*0.05),
                         outline=p.stroke, width=lw)
    check_mark(d, s*0.68, s*0.5, s*0.1, p.ok, lw)

@icon("check")
def draw_check(d, cell, p):
    s = cell * SS
    lw = stroke_width(cell)
    circle(d, s/2, s/2, s*0.35, outline=p.stroke, lw=lw)
    check_mark(d, s/2, s/2, s*0.22, p.ok, lw*2)

@icon("geometry_analysis")
def draw_geometry_analysis(d, cell, p):
    """Solid shape with dimension/analysis arrows."""
    s = cell * SS
    lw = stroke_width(cell)
    d.rounded_rectangle([s*0.2, s*0.2, s*0.72, s*0.72], radius=int(s*0.08),
                         outline=p.stroke, width=lw)
    arrow(d, s*0.72, s*0.46, s*0.85, s*0.46, p.accent, lw)
    arrow(d, s*0.46, s*0.72, s*0.46, s*0.85, p.accent, lw)

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
    """Floppy disk."""
    s = cell * SS
    lw = stroke_width(cell)
    d.rounded_rectangle([s*0.15, s*0.15, s*0.85, s*0.85], radius=int(s*0.04),
                         outline=p.stroke, width=lw)
    # label area
    d.rectangle([s*0.28, s*0.55, s*0.72, s*0.82], outline=p.accent, width=lw)
    # notch top-right
    d.polygon([(s*0.68, s*0.15), (s*0.85, s*0.15), (s*0.85, s*0.32)],
              fill=None, outline=p.stroke)
    d.line([(s*0.68, s*0.15), (s*0.85, s*0.32)], fill=p.stroke, width=lw)

@icon("open")
def draw_open(d, cell, p):
    """Folder with up-arrow."""
    s = cell * SS
    lw = stroke_width(cell)
    d.rounded_rectangle([s*0.12, s*0.32, s*0.88, s*0.80], radius=int(s*0.05),
                         outline=p.stroke, width=lw)
    d.rectangle([s*0.12, s*0.26, s*0.45, s*0.36], outline=p.stroke, width=lw)
    arrow(d, s*0.5, s*0.62, s*0.5, s*0.42, p.accent, lw)

@icon("new_doc")
def draw_new_doc(d, cell, p):
    """Page with dog-ear + plus."""
    s = cell * SS
    lw = stroke_width(cell)
    fold = s * 0.22
    pts = [(s*0.22, s*0.12), (s*0.78 - fold, s*0.12),
           (s*0.78, s*0.12 + fold), (s*0.78, s*0.88),
           (s*0.22, s*0.88)]
    d.polygon(pts, fill=None, outline=p.stroke)
    d.line(pts + [pts[0]], fill=p.stroke, width=lw)
    d.line([(s*0.78 - fold, s*0.12), (s*0.78 - fold, s*0.12 + fold),
            (s*0.78, s*0.12 + fold)], fill=p.stroke, width=lw)
    plus_sign(d, s*0.5, s*0.56, s*0.16, p.accent, lw)

@icon("undo")
def draw_undo(d, cell, p):
    s = cell * SS
    lw = stroke_width(cell)
    cx, cy = s*0.52, s*0.52
    r = s*0.30
    arc_segment(d, cx, cy, r, 180, 360, p.stroke, lw)
    arc_segment(d, cx, cy, r, 0, 90, p.stroke, lw)
    arrow(d, cx - r, cy, cx - r - s*0.1, cy - s*0.14, p.stroke, lw)

@icon("redo")
def draw_redo(d, cell, p):
    s = cell * SS
    lw = stroke_width(cell)
    cx, cy = s*0.48, s*0.52
    r = s*0.30
    arc_segment(d, cx, cy, r, 180, 360, p.stroke, lw)
    arc_segment(d, cx, cy, r, 90, 180, p.stroke, lw)
    arrow(d, cx + r, cy, cx + r + s*0.1, cy - s*0.14, p.stroke, lw)

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

# ---------------------------------------------------------------------------
# C25: sketch flyout variant icons
# ---------------------------------------------------------------------------

@icon("rect_3pt_corner")
def draw_rect_3pt_corner(d, cell, p):
    """Rectangle with 3-point annotation dots on two corners + midpoint."""
    s = cell * SS
    lw = stroke_width(cell)
    d.rectangle([s*0.15, s*0.25, s*0.85, s*0.75], outline=p.stroke, width=lw)
    for x, y in [(s*0.15, s*0.25), (s*0.85, s*0.25), (s*0.85, s*0.75)]:
        filled_circle(d, x, y, lw, p.accent)
    # small 3 label hint (three dots in triangle)
    for ox, oy in [(-0.06, -0.11), (0, -0.11), (0.06, -0.11)]:
        filled_circle(d, s*0.5 + ox*s, s*0.13 + oy*s, lw*0.6, p.accent)

@icon("rect_3pt_center")
def draw_rect_3pt_center(d, cell, p):
    """Rectangle with center + 3 control points."""
    s = cell * SS
    lw = stroke_width(cell)
    d.rectangle([s*0.15, s*0.25, s*0.85, s*0.75], outline=p.stroke, width=lw)
    filled_circle(d, s*0.5, s*0.5, lw, p.accent)
    d.line([(s*0.15, s*0.5), (s*0.85, s*0.5)], fill=p.accent, width=lw)
    d.line([(s*0.5, s*0.25), (s*0.5, s*0.75)], fill=p.accent, width=lw)
    for ox, oy in [(-0.06, -0.11), (0, -0.11), (0.06, -0.11)]:
        filled_circle(d, s*0.5 + ox*s, s*0.13 + oy*s, lw*0.6, p.accent)

@icon("parallelogram")
def draw_parallelogram(d, cell, p):
    """Parallelogram shape."""
    s = cell * SS
    lw = stroke_width(cell)
    pts = [(s*0.30, s*0.25), (s*0.85, s*0.25), (s*0.70, s*0.75), (s*0.15, s*0.75)]
    d.polygon(pts, fill=None, outline=p.stroke)
    d.line([pts[0], pts[1], pts[2], pts[3], pts[0]], fill=p.stroke, width=lw)
    filled_circle(d, pts[0][0], pts[0][1], lw, p.accent)

@icon("circle_perimeter")
def draw_circle_perimeter(d, cell, p):
    """Circle with perimeter point instead of center point."""
    s = cell * SS
    lw = stroke_width(cell) * 2
    cx, cy = s/2, s/2
    r = s * 0.32
    circle(d, cx, cy, r, outline=p.stroke, lw=lw)
    # accent dot on perimeter (top)
    filled_circle(d, cx, cy - r, lw, p.accent)
    # two additional perimeter dots
    filled_circle(d, cx + r*0.87, cy + r*0.5, lw*0.7, p.accent)
    filled_circle(d, cx - r*0.87, cy + r*0.5, lw*0.7, p.accent)

@icon("slot_ctr_str")
def draw_slot_ctr_str(d, cell, p):
    """Centerpoint straight slot — oblong with center dot."""
    s = cell * SS
    lw = stroke_width(cell)
    d.rounded_rectangle([s*0.15, s*0.35, s*0.85, s*0.65], radius=int(s*0.15),
                         outline=p.stroke, width=lw)
    filled_circle(d, s*0.5, s*0.5, lw, p.accent)
    d.line([(s*0.15, s*0.5), (s*0.85, s*0.5)], fill=p.accent, width=lw)

@icon("slot_arc_3pt")
def draw_slot_arc_3pt(d, cell, p):
    """3-point arc slot — slot following a curved arc."""
    s = cell * SS
    lw = stroke_width(cell)
    # curved arc path
    arc_segment(d, s*0.5, s*0.75, s*0.38, 210, 330, p.stroke, lw)
    arc_segment(d, s*0.5, s*0.75, s*0.52, 210, 330, p.stroke, lw)
    # end caps
    for angle in [210, 330]:
        a = math.radians(angle)
        cx1 = s*0.5 + s*0.38*math.cos(a)
        cy1 = s*0.75 + s*0.38*math.sin(a)
        cx2 = s*0.5 + s*0.52*math.cos(a)
        cy2 = s*0.75 + s*0.52*math.sin(a)
        d.line([(cx1, cy1), (cx2, cy2)], fill=p.stroke, width=lw)
    # 3-pt dots
    for angle in [210, 270, 330]:
        a = math.radians(angle)
        cx = s*0.5 + s*0.45*math.cos(a)
        cy = s*0.75 + s*0.45*math.sin(a)
        filled_circle(d, cx, cy, lw*0.7, p.accent)

@icon("slot_arc_ctr")
def draw_slot_arc_ctr(d, cell, p):
    """Centerpoint arc slot."""
    s = cell * SS
    lw = stroke_width(cell)
    arc_segment(d, s*0.5, s*0.75, s*0.38, 210, 330, p.stroke, lw)
    arc_segment(d, s*0.5, s*0.75, s*0.52, 210, 330, p.stroke, lw)
    for angle in [210, 330]:
        a = math.radians(angle)
        cx1 = s*0.5 + s*0.38*math.cos(a)
        cy1 = s*0.75 + s*0.38*math.sin(a)
        cx2 = s*0.5 + s*0.52*math.cos(a)
        cy2 = s*0.75 + s*0.52*math.sin(a)
        d.line([(cx1, cy1), (cx2, cy2)], fill=p.stroke, width=lw)
    # center dot
    filled_circle(d, s*0.5, s*0.75, lw, p.accent)

@icon("spline_style")
def draw_spline_style(d, cell, p):
    """Style spline — smooth S-curve with control polygon."""
    s = cell * SS
    lw = stroke_width(cell) * 2
    d.arc([s*0.15, s*0.1, s*0.65, s*0.6], start=200, end=360, fill=p.stroke, width=lw)
    d.arc([s*0.35, s*0.4, s*0.85, s*0.9], start=20, end=180, fill=p.stroke, width=lw)
    # control polygon (dashed-style in accent)
    ctrl_pts = [(s*0.18, s*0.55), (s*0.38, s*0.22), (s*0.62, s*0.78), (s*0.82, s*0.45)]
    for i in range(len(ctrl_pts) - 1):
        d.line([ctrl_pts[i], ctrl_pts[i+1]], fill=p.accent, width=lw//2)
    for pt in ctrl_pts:
        filled_circle(d, pt[0], pt[1], lw*0.6, p.accent)

@icon("spline_surface")
def draw_spline_surface(d, cell, p):
    """Spline on surface — S-curve overlaid on a plane parallelogram."""
    s = cell * SS
    lw = stroke_width(cell)
    pts = [(s*0.1, s*0.65), (s*0.45, s*0.28), (s*0.9, s*0.38), (s*0.55, s*0.75)]
    d.polygon(pts, fill=(*p.stroke[:3], 30))
    d.line([pts[0], pts[1], pts[2], pts[3], pts[0]], fill=p.stroke, width=lw)
    # spline over it
    d.arc([s*0.20, s*0.18, s*0.65, s*0.62], start=200, end=360, fill=p.accent, width=lw*2)
    d.arc([s*0.38, s*0.40, s*0.82, s*0.84], start=20, end=180, fill=p.accent, width=lw*2)

@icon("spline_equation")
def draw_spline_equation(d, cell, p):
    """Equation-driven curve — smooth curve with '=' annotation."""
    s = cell * SS
    lw = stroke_width(cell) * 2
    d.arc([s*0.15, s*0.1, s*0.65, s*0.6], start=200, end=360, fill=p.stroke, width=lw)
    d.arc([s*0.35, s*0.4, s*0.85, s*0.9], start=20, end=180, fill=p.stroke, width=lw)
    # '=' sign top-right
    d.line([(s*0.70, s*0.18), (s*0.88, s*0.18)], fill=p.accent, width=lw//2)
    d.line([(s*0.70, s*0.28), (s*0.88, s*0.28)], fill=p.accent, width=lw//2)

@icon("ellipse_partial")
def draw_ellipse_partial(d, cell, p):
    """Partial ellipse arc."""
    s = cell * SS
    lw = stroke_width(cell) * 2
    # draw only part of the ellipse
    d.arc([s*0.12, s*0.28, s*0.88, s*0.72], start=30, end=250, fill=p.stroke, width=lw)
    # endpoints
    for angle in [30, 250]:
        a = math.radians(angle)
        cx = s*0.5 + s*0.38*math.cos(a)
        cy = s*0.5 + s*0.22*math.sin(a)
        filled_circle(d, cx, cy, lw*0.7, p.accent)

@icon("parabola")
def draw_parabola(d, cell, p):
    """Parabolic curve — U-shape open upward."""
    s = cell * SS
    lw = stroke_width(cell) * 2
    pts = []
    steps = 20
    for i in range(steps + 1):
        t = (i / steps) * 2 - 1  # -1 to 1
        x = s*0.5 + t * s*0.35
        y = s*0.75 - t*t * s*0.50
        pts.append((x, y))
    for i in range(len(pts) - 1):
        d.line([pts[i], pts[i+1]], fill=p.stroke, width=lw)
    # focal point
    filled_circle(d, s*0.5, s*0.50, lw*0.7, p.accent)
    # axis
    d.line([(s*0.5, s*0.15), (s*0.5, s*0.78)], fill=p.accent, width=lw//2)

@icon("conic")
def draw_conic(d, cell, p):
    """Conic section — ellipse with rho value indicator."""
    s = cell * SS
    lw = stroke_width(cell) * 2
    # slightly asymmetric ellipse to suggest conic
    d.ellipse([s*0.15, s*0.25, s*0.85, s*0.75], outline=p.stroke, width=lw)
    # diagonal cutting plane line (suggesting conic section)
    d.line([(s*0.15, s*0.22), (s*0.85, s*0.78)], fill=p.accent, width=lw//2)
    filled_circle(d, s*0.5, s*0.5, lw*0.7, p.accent)

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
# MISSING placeholder
# ---------------------------------------------------------------------------

def draw_missing(d: ImageDraw.ImageDraw, cell: int, p: Palette):
    s = cell * SS
    lw = stroke_width(cell)
    diagonal_box(d, s*0.1, s*0.1, s*0.9, s*0.9, p.err, lw)


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
