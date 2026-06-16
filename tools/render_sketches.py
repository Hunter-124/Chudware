"""Render solved sketches to SVG for visual verification.

Runs the (parity-verified) Python solve_sketch on each corpus fixture and draws
the solved geometry to corpus_render/<name>.svg, plus a self-contained gallery
HTML for a curated set. Because the C++ solver is at 36/36 parity with this Python
oracle, what you see here is what the C++ engine produces.

Run from repo root with the project venv:
    & "$env:APPDATA\AI-CAD\runtime\venv\Scripts\python.exe" tools/render_sketches.py
"""
from __future__ import annotations

import glob
import json
import math
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from engine.doc.sketch_solver import solve_sketch  # noqa: E402

OUT = os.path.join(REPO, "corpus_render")
os.makedirs(OUT, exist_ok=True)

# Status -> stroke color (SolidWorks-ish: black fully defined, blue under, red over).
COL = {"full": "#111827", "under": "#2563eb", "over": "#dc2626"}

# Optional PNG rasterization (lets us eyeball a true image, not just SVG text).
try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    _HAVE_MPL = True
except Exception:
    _HAVE_MPL = False


def _poly_circle(cx, cy, r, a0=0.0, a1=360.0, n=72):
    pts = []
    a0r, a1r = math.radians(a0), math.radians(a1)
    if a1r < a0r:
        a1r += 2 * math.pi
    for i in range(n + 1):
        t = a0r + (a1r - a0r) * i / n
        pts.append((cx + r * math.cos(t), cy + r * math.sin(t)))
    return pts


def _arc_seg(cx, cy, r, d0, d1, n=24):
    """Sample a circular arc from angle d0 to d1 (degrees, SIGNED — no 360 wrap),
    so callers control the sweep direction. d0<d1 sweeps CCW, d0>d1 sweeps CW."""
    pts = []
    for i in range(n + 1):
        a = math.radians(d0 + (d1 - d0) * i / n)
        pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    return pts


def _slot_outline(p1, p2, w):
    (x1, y1), (x2, y2) = p1, p2
    dx, dy = x2 - x1, y2 - y1
    L = math.hypot(dx, dy) or 1.0
    ux, uy = dx / L, dy / L
    nx, ny = -uy, ux
    hr = w / 2.0
    a0 = math.degrees(math.atan2(ny, nx))  # angle of the +normal direction
    # CCW outline: top rail p1->p2, right cap (+n -> +u -> -n), bottom rail p2->p1,
    # left cap (-n -> -u -> +n). Caps bulge OUTWARD (CW arc = decreasing angle), so
    # the half-circles open past the spine ends instead of pinching inward.
    pts = [(x1 + nx * hr, y1 + ny * hr), (x2 + nx * hr, y2 + ny * hr)]
    pts += _arc_seg(x2, y2, hr, a0, a0 - 180, 24)
    pts += [(x1 - nx * hr, y1 - ny * hr)]
    pts += _arc_seg(x1, y1, hr, a0 + 180, a0, 24)
    return pts


def entity_drawables(eid, ent, status):
    """Return (polylines, point_markers) for one solved entity, in world coords."""
    t = ent.get("type")
    polys, pts = [], []
    if t == "point":
        pts.append(tuple(ent["p"]))
    elif t == "line":
        polys.append([tuple(ent["p1"]), tuple(ent["p2"])])
    elif t == "circle":
        c = ent["center"]
        polys.append(_poly_circle(c[0], c[1], ent["radius"]))
        pts.append(tuple(c))
    elif t == "arc":
        c = ent["center"]
        polys.append(_poly_circle(c[0], c[1], ent["radius"], ent["start"], ent["end"]))
        pts.append(tuple(c))
    elif t == "ellipse":
        cx, cy = ent["center"]
        rx, ry, rot = ent["rx"], ent["ry"], math.radians(ent.get("rot", 0.0))
        pl = []
        for i in range(73):
            a = 2 * math.pi * i / 72
            lx, ly = rx * math.cos(a), ry * math.sin(a)
            pl.append((cx + lx * math.cos(rot) - ly * math.sin(rot),
                       cy + lx * math.sin(rot) + ly * math.cos(rot)))
        polys.append(pl)
        pts.append((cx, cy))
    elif t == "slot":
        polys.append(_slot_outline(ent["p1"], ent["p2"], ent["width"]))
    elif t in ("spline", "style_spline"):
        polys.append([tuple(p) for p in ent.get("points", [])])
    else:
        # exotic / unsupported render: mark any handle points we can find
        for k in ("p", "p1", "p2", "center", "vertex", "focus", "apex"):
            if k in ent and isinstance(ent[k], list) and len(ent[k]) == 2:
                pts.append(tuple(ent[k]))
    return polys, pts


def render_svg(name, sol, W=360, H=320, pad=26):
    ents = sol.entities
    statuses = sol.statuses
    draw = []  # (polys, pts, status, construction)
    allpts = []
    for eid, ent in ents.items():
        polys, pts = entity_drawables(eid, ent, statuses.get(eid, "full"))
        cons = bool(ent.get("construction"))
        draw.append((polys, pts, statuses.get(eid, "full"), cons))
        for pl in polys:
            allpts += pl
        allpts += pts
    allpts.append((0.0, 0.0))  # always show the origin
    if not allpts:
        allpts = [(0, 0), (1, 1)]
    xs = [p[0] for p in allpts]
    ys = [p[1] for p in allpts]
    minx, maxx, miny, maxy = min(xs), max(xs), min(ys), max(ys)
    spanx, spany = max(maxx - minx, 1e-6), max(maxy - miny, 1e-6)
    scale = min((W - 2 * pad) / spanx, (H - 2 * pad) / spany)

    def sx(x):
        return pad + (x - minx) * scale

    def sy(y):
        return H - pad - (y - miny) * scale  # Y-up -> screen Y-down

    out = [f'<svg viewBox="0 0 {W} {H}" xmlns="http://www.w3.org/2000/svg" '
           f'font-family="ui-sans-serif,system-ui" style="background:#fff">']
    # axes through origin
    ox, oy = sx(0), sy(0)
    out.append(f'<line x1="{pad}" y1="{oy:.1f}" x2="{W-pad}" y2="{oy:.1f}" '
               f'stroke="#e5e7eb" stroke-width="1"/>')
    out.append(f'<line x1="{ox:.1f}" y1="{pad}" x2="{ox:.1f}" y2="{H-pad}" '
               f'stroke="#e5e7eb" stroke-width="1"/>')
    out.append(f'<circle cx="{ox:.1f}" cy="{oy:.1f}" r="2.5" fill="#9ca3af"/>')
    for polys, pts, status, cons in draw:
        col = COL.get(status, "#111827")
        dash = ' stroke-dasharray="4 3"' if cons else ''
        for pl in polys:
            if len(pl) < 2:
                continue
            d = "M" + " L".join(f"{sx(x):.2f} {sy(y):.2f}" for x, y in pl)
            out.append(f'<path d="{d}" fill="none" stroke="{col}" '
                       f'stroke-width="1.8"{dash}/>')
        for (x, y) in pts:
            out.append(f'<circle cx="{sx(x):.2f}" cy="{sy(y):.2f}" r="2.6" '
                       f'fill="{col}"/>')
    out.append('</svg>')
    return "\n".join(out)


def render_png(name, sol):
    """Rasterize a solved sketch to corpus_render/png/<name>.png via matplotlib."""
    if not _HAVE_MPL:
        return
    fig, ax = plt.subplots(figsize=(3.4, 3.0), dpi=120)
    ax.axhline(0, color="#e5e7eb", lw=0.8, zorder=0)
    ax.axvline(0, color="#e5e7eb", lw=0.8, zorder=0)
    for eid, ent in sol.entities.items():
        status = sol.statuses.get(eid, "full")
        col = COL.get(status, "#111827")
        cons = bool(ent.get("construction"))
        polys, pts = entity_drawables(eid, ent, status)
        for pl in polys:
            if len(pl) >= 2:
                ax.plot([p[0] for p in pl], [p[1] for p in pl], color=col,
                        lw=1.5, ls="--" if cons else "-")
        if pts:
            ax.scatter([p[0] for p in pts], [p[1] for p in pts], color=col, s=9, zorder=3)
    ax.set_aspect("equal")
    ax.margins(0.12)
    ax.set_title(name, fontsize=8)
    ax.tick_params(labelsize=6)
    pdir = os.path.join(OUT, "png")
    os.makedirs(pdir, exist_ok=True)
    fig.tight_layout()
    fig.savefig(os.path.join(pdir, name + ".png"))
    plt.close(fig)


def main():
    files = sorted(glob.glob(os.path.join(REPO, "corpus", "*.json")))
    files += sorted(glob.glob(os.path.join(REPO, "corpus_sw_parity", "*.json")))
    cards = {}
    for fp in files:
        try:
            fx = json.load(open(fp, encoding="utf-8"))
        except Exception:
            continue
        if "sketch" not in fx:
            continue
        name = fx.get("name") or os.path.splitext(os.path.basename(fp))[0]
        try:
            sol = solve_sketch(fx["sketch"], fx.get("env", {}))
        except Exception as exc:
            print(f"  SKIP {name}: {exc}")
            continue
        svg = render_svg(name, sol)
        with open(os.path.join(OUT, name + ".svg"), "w", encoding="utf-8") as f:
            f.write(svg)
        render_png(name, sol)
        cards[name] = (svg, sol.dof, sol.converged,
                       len(sol.conflicting) > 0)
    print(f"rendered {len(cards)} sketches -> corpus_render/")

    # Curated, self-contained gallery for inline viewing.
    curated = [
        "complex_bolt_circle_plate", "feature_mirror_holes",
        "feature_linear_pattern_grid", "feature_circular_pattern_bolt",
        "feature_circular_pattern_tri", "feature_chain_dim_staircase",
        "feature_expr_dim_driven", "complex_hexagon",
    ]
    blocks = []
    for name in curated:
        if name not in cards:
            continue
        svg, dof, conv, conflict = cards[name]
        tag = "conflict" if conflict else (f"dof {dof}" if dof else "fully defined")
        col = "#dc2626" if conflict else ("#2563eb" if dof else "#059669")
        blocks.append(
            f'<figure style="margin:0;border:1px solid #e5e7eb;border-radius:10px;'
            f'overflow:hidden;background:#fff">{svg}'
            f'<figcaption style="padding:6px 10px;font:600 12px ui-sans-serif;'
            f'display:flex;justify-content:space-between;border-top:1px solid #f1f5f9">'
            f'<span style="color:#374151">{name}</span>'
            f'<span style="color:{col}">{tag}{"" if conv else " · !conv"}</span>'
            f'</figcaption></figure>')
    gallery = (
        '<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));'
        'gap:12px;padding:4px">' + "".join(blocks) + '</div>')
    with open(os.path.join(OUT, "gallery_inline.html"), "w", encoding="utf-8") as f:
        f.write(gallery)
    print("wrote corpus_render/gallery_inline.html")


if __name__ == "__main__":
    main()
