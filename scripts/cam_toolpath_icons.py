#!/usr/bin/env python3
"""T-BL34: Manufacture ribbon icons rendered from REAL generated toolpaths.

Hunter directive (2026-07-04): the Manufacture icons must showcase the GENERATED
TOOLPATHS the tools produce, not screenshots of the shape types they're used on.

Each tile is produced by driving the actual CAM engine chain through the same
libaicad RPC bridge the MCP server uses:

    cam_new -> cam_apply_ops(add_tool/add_setup/add_operation) -> cam_generate
            -> cam_simulate (smoke check)

and then rendering the returned Move list (rapid/lead/cut/plunge/arc, classified
exactly like the in-app backplot, ui_theme.cpp camRapid/camLead/camCut/camPlunge/
camArc dark palette) over the stock silhouette in the same iso-ish camera the
modeling tiles use (yaw 35 / pitch 26). NEVER a stylized glyph: if cam_generate
yields no renderable moves for an op, that is a CAM bug to FILE, and the tile is
reported as failed (the atlas keeps the SDF fallback glyph for that cell).

The render doubles as a smoke test of the CAM op chain: a failing op is returned
in the `failures` list so the caller (icon_pipeline.py) can surface it loudly.

Standalone: python3 scripts/cam_toolpath_icons.py  (writes scratch/icon_render/cam_*.png)
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

from PIL import Image, ImageDraw

REPO = Path(__file__).resolve().parent.parent
RENDER_DIR = REPO / "scratch" / "icon_render"
OUT_PX = 128        # matches modeling-tile render size
SS = 4              # supersample factor for crisp polylines at 32/20 px

sys.path.insert(0, str(REPO / "services" / "mcp"))


def _resolve_libaicad() -> None:
    """Point core_bridge at a built libaicad.so. Worktrees usually have no build
    dir, so fall back to the main checkout (git common dir) transparently."""
    import os
    import subprocess
    if os.environ.get("AI_CAD_LIBAICAD"):
        return
    roots = [REPO]
    try:
        common = subprocess.run(
            ["git", "-C", str(REPO), "rev-parse", "--path-format=absolute",
             "--git-common-dir"], capture_output=True, text=True, check=True
        ).stdout.strip()
        roots.append(Path(common).parent)
    except Exception:
        pass
    for root in roots:
        for rel in ("core/build-linux/bin/libaicad.so",
                    "host/build-linux/bin/libaicad.so",
                    "core/build-occt-linux/bin/libaicad.so"):
            p = root / rel
            if p.exists():
                os.environ["AI_CAD_LIBAICAD"] = str(p)
                return


_resolve_libaicad()

# ---------------------------------------------------------------------------
# Backplot palette — dark-theme values from host/src/ui_theme.cpp L90-94 so the
# icon IS the in-app backplot look (one asset serves both themes; the dark
# palette stays readable on the light ribbon glass).
# ---------------------------------------------------------------------------
COL = {
    "rapid":  (199, 61, 51, 110),
    "lead":   (79, 199, 120, 240),
    "plunge": (79, 181, 219, 240),
    "cut":    (89, 150, 240, 255),
    "arc":    (150, 130, 240, 255),
}
LW = {"rapid": 4, "lead": 8, "plunge": 9, "cut": 11, "arc": 11}  # at SS*OUT_PX canvas
STOCK_EDGE = (150, 155, 163, 235)
STOCK_TOP = (196, 200, 207, 78)
STOCK_SIDE = (128, 132, 140, 64)


def _iso(yaw_deg: float = 35.0, pitch_deg: float = 26.0):
    """Orthographic iso-ish projector matching the modeling-tile camera family."""
    ya, pa = math.radians(yaw_deg), math.radians(pitch_deg)
    cy, sy, cp, sp = math.cos(ya), math.sin(ya), math.cos(pa), math.sin(pa)

    def proj(p):
        x, y, z = p
        # yaw about Z
        xr = x * cy - y * sy
        yr = x * sy + y * cy
        # pitch about screen-X: screen up mixes world z and yr
        u = xr
        v = z * cp - yr * sp
        return (u, v)
    return proj


def _fit_points(pts2, px: int, margin: float = 0.10):
    xs = [p[0] for p in pts2]
    ys = [p[1] for p in pts2]
    w = max(xs) - min(xs) or 1.0
    h = max(ys) - min(ys) or 1.0
    s = (1.0 - 2 * margin) * px / max(w, h)
    cx, cy = (max(xs) + min(xs)) / 2, (max(ys) + min(ys)) / 2

    def to_px(p):
        return (px / 2 + (p[0] - cx) * s, px / 2 - (p[1] - cy) * s)
    return to_px


def _sample_arc(p0, p1, center, ccw, seg_len=1.0):
    """Sample a G2/G3 XY-plane arc from p0 to p1 about center into short segments."""
    cx, cy = center[0], center[1]
    a0 = math.atan2(p0[1] - cy, p0[0] - cx)
    a1 = math.atan2(p1[1] - cy, p1[0] - cx)
    r = math.hypot(p0[0] - cx, p0[1] - cy)
    if r < 1e-9:
        return [p0, p1]
    if ccw:
        while a1 <= a0 + 1e-12:
            a1 += 2 * math.pi
    else:
        while a1 >= a0 - 1e-12:
            a1 -= 2 * math.pi
    sweep = a1 - a0
    n = max(2, int(abs(sweep) * r / seg_len))
    out = []
    for i in range(n + 1):
        t = i / n
        a = a0 + sweep * t
        z = p0[2] + (p1[2] - p0[2]) * t
        out.append((cx + r * math.cos(a), cy + r * math.sin(a), z))
    return out


def _classify_polylines(moves):
    """Turn the engine Move list into per-kind 3D polylines (world mm)."""
    polys = {k: [] for k in COL}
    cur = None
    prev = None
    prev_kind = None
    for mv in moves:
        kind = mv.get("kind", "cut")
        if kind in ("dwell", "tool_change"):
            continue
        to = tuple(mv["to"])
        if prev is None:
            prev, prev_kind = to, kind
            continue
        if kind == "arc":
            pts = _sample_arc(prev, to, mv.get("center", to), mv.get("ccw", False))
        else:
            pts = [prev, to]
        key = kind if kind in polys else "cut"
        if prev_kind == kind and cur is not None and polys[key] and polys[key][-1] is cur \
                and cur[-1] == prev:
            cur.extend(pts[1:])
        else:
            cur = list(pts)
            polys[key].append(cur)
        prev, prev_kind = to, kind
    return polys


def _stock_wire(stock):
    """3D edge segments + translucent face polys for the stock silhouette."""
    faces, edges = [], []
    if stock["type"].startswith("box"):
        lo, hi = stock["min"], stock["max"]
        x0, y0, z0 = lo
        x1, y1, z1 = hi
        top = [(x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1)]
        front = [(x0, y0, z0), (x1, y0, z0), (x1, y0, z1), (x0, y0, z1)]
        right = [(x1, y0, z0), (x1, y1, z0), (x1, y1, z1), (x1, y0, z1)]
        faces = [(top, STOCK_TOP), (front, STOCK_SIDE), (right, STOCK_SIDE)]
        c = [(x, y, z) for z in (z0, z1) for y in (y0, y1) for x in (x0, x1)]
        idx = [(0, 1), (1, 3), (3, 2), (2, 0), (4, 5), (5, 7), (7, 6), (6, 4),
               (0, 4), (1, 5), (2, 6), (3, 7)]
        edges = [(c[a], c[b]) for a, b in idx]
    elif stock["type"].startswith("cylinder"):
        # lathe stock: axis = +Z through `base`, fields radius/height (model.h L77-80)
        r = stock["radius"]
        z0 = stock["base"][2]
        z1 = z0 + stock["height"]
        n = 40
        ring0 = [(r * math.cos(2 * math.pi * i / n), r * math.sin(2 * math.pi * i / n), z0)
                 for i in range(n + 1)]
        ring1 = [(p[0], p[1], z1) for p in ring0]
        faces = [(ring1, STOCK_TOP)]
        edges = list(zip(ring0[:-1], ring0[1:])) + list(zip(ring1[:-1], ring1[1:]))
        # silhouette sides at the yaw-facing tangents
        for a in (math.radians(125), math.radians(305)):
            x, y = r * math.cos(a), r * math.sin(a)
            edges.append(((x, y, z0), (x, y, z1)))
    return faces, edges


def render_toolpath_tile(name: str, moves, stock, out_dir: Path,
                         yaw: float = 35.0, pitch: float = 26.0,
                         lw_scale: float = 1.0) -> Path:
    """Render classified toolpath moves over the stock silhouette to <name>.png."""
    px = OUT_PX * SS
    proj = _iso(yaw, pitch)
    polys = _classify_polylines(moves)
    faces, edges = _stock_wire(stock)

    # fit view on stock + cutting moves (rapids often tower above; exclude them
    # from the fit so the cut pattern fills the tile, but still draw them)
    fit_pts = [proj(p) for f, _ in faces for p in f] + [proj(p) for e in edges for p in e]
    for k in ("cut", "arc", "lead", "plunge"):
        for pl in polys[k]:
            fit_pts += [proj(p) for p in pl]
    if not fit_pts:
        raise RuntimeError("nothing to render")
    to_px = _fit_points(fit_pts, px)

    img = Image.new("RGBA", (px, px), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    for poly, col in faces:
        d.polygon([to_px(proj(p)) for p in poly], fill=col)
    for a, b in edges:
        d.line([to_px(proj(a)), to_px(proj(b))], fill=STOCK_EDGE, width=2)
    # rapids under the cutting art
    for kind in ("rapid", "lead", "plunge", "cut", "arc"):
        for pl in polys[kind]:
            pts = [to_px(proj(p)) for p in pl]
            if len(pts) >= 2:
                d.line(pts, fill=COL[kind], width=max(2, round(LW[kind] * lw_scale)),
                       joint="curve")

    out = img.resize((OUT_PX, OUT_PX), Image.LANCZOS)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{name}.png"
    out.save(path)
    return path


# ---------------------------------------------------------------------------
# Demo-part meshes for the 3D group (pure python; small grids keep gen fast)
# ---------------------------------------------------------------------------

def _grid_mesh(fz, x0=-18, x1=18, y0=-18, y1=18, n=24):
    verts, tris = [], []
    for j in range(n + 1):
        for i in range(n + 1):
            x = x0 + (x1 - x0) * i / n
            y = y0 + (y1 - y0) * j / n
            verts += [x, y, fz(x, y)]
    for j in range(n):
        for i in range(n):
            a = j * (n + 1) + i
            b = a + 1
            c = a + n + 1
            e = c + 1
            tris += [a, b, e, a, e, c]
    return {"verts": verts, "tris": tris}


def _dome(h=14.0, s=110.0):
    return _grid_mesh(lambda x, y: h * math.exp(-(x * x + y * y) / s) - h)


def _bowl(h=12.0, s=130.0):
    return _grid_mesh(lambda x, y: -h * math.exp(-(x * x + y * y) / s))


def _wave():
    return _grid_mesh(lambda x, y: 5.0 * math.sin(x * 0.25) * math.cos(y * 0.25) - 6.0)


# ---------------------------------------------------------------------------
# The job spec per Manufacture ribbon op button. cell = new atlas cell name.
# Each spec is one complete, honest CAM program for that op type.
# ---------------------------------------------------------------------------
MILL_STOCK = {"type": "box", "min": [-20, -20, -14], "max": [20, 20, 0],
              "material": "aluminum"}
MESH_STOCK = {"type": "box", "min": [-18, -18, -15], "max": [18, 18, 0],
              "material": "aluminum"}
LATHE_STOCK = {"type": "cylinder", "base": [0.0, 0.0, -24.0], "radius": 14.0,
               "height": 25.0, "material": "aluminum"}
EM6 = {"number": 1, "type": "endmill_flat", "diameter": 6.0, "flutes": 3}
BALL4 = {"number": 1, "type": "endmill_ball", "diameter": 4.0, "corner_radius": 2.0,
         "flutes": 2}
FEEDS = {"rpm": 8000, "feed": 900.0, "plunge": 300.0}
H = {"clearance": 12.0, "retract": 4.0, "top": 0.0, "bottom": -6.0}
SQUARE = [[-14, -14], [14, -14], [14, 14], [-14, 14]]

CAM_SPECS: list[dict] = [
    # ---- 2.5D ------------------------------------------------------------
    {"cell": "cam_face", "machine": "mill3", "stock": MILL_STOCK, "tool": EM6,
     "op": {"type": "face", "tool_number": 1, "stepover": 4.5, "stepdown": 2.0,
            "pattern": "zigzag", "heights": {**H, "bottom": -2.0},
            "geometry": {"regions": [{"outer": SQUARE}]}, **FEEDS}},
    {"cell": "cam_contour2d", "machine": "mill3", "stock": MILL_STOCK, "tool": EM6,
     "op": {"type": "contour2d", "tool_number": 1, "stepdown": 3.0,
            "comp_side": "left", "heights": H,
            "geometry": {"contours": [SQUARE + [[-14, -14]]]}, **FEEDS}},
    {"cell": "cam_pocket2d", "machine": "mill3", "stock": MILL_STOCK, "tool": EM6,
     "op": {"type": "pocket2d", "tool_number": 1, "stepover": 4.5, "stepdown": 6.0,
            "pattern": "concentric", "heights": H,
            "geometry": {"regions": [{"outer": SQUARE}]}, **FEEDS}},
    {"cell": "cam_adaptive2d", "machine": "mill3", "stock": MILL_STOCK, "tool": EM6,
     "op": {"type": "adaptive2d", "tool_number": 1, "stepover": 3.0, "stepdown": 6.0,
            "heights": H, "geometry": {"regions": [{"outer": SQUARE}]}, **FEEDS}},
    {"cell": "cam_slot", "machine": "mill3", "stock": MILL_STOCK, "tool": EM6,
     "op": {"type": "slot", "tool_number": 1, "stepdown": 2.5, "heights": {**H, "bottom": -5.0},
            "geometry": {"contours": [[[-12, -12], [12, 12]]]}, **FEEDS}},
    {"cell": "cam_chamfer2d", "machine": "mill3", "stock": MILL_STOCK,
     "tool": {"number": 1, "type": "chamfer_mill", "diameter": 8.0, "tip_angle": 90.0,
              "flutes": 2},
     "op": {"type": "chamfer2d", "tool_number": 1, "heights": {**H, "bottom": -2.0},
            "geometry": {"contours": [SQUARE + [[-14, -14]]]}, **FEEDS}},
    {"cell": "cam_engrave", "machine": "mill3", "stock": MILL_STOCK,
     "tool": {"number": 1, "type": "vbit", "diameter": 6.0, "tip_angle": 60.0, "flutes": 1},
     "op": {"type": "engrave", "tool_number": 1, "heights": {**H, "bottom": -1.2},
            "geometry": {"contours": [
                [[-12, -8], [-12, 8], [-4, -8], [-4, 8]],            # N
                [[2, 8], [2, -8], [12, -8]],                          # L
            ]}, **FEEDS}},
    # ---- Holes -----------------------------------------------------------
    {"cell": "cam_drill", "machine": "mill3", "stock": MILL_STOCK,
     "tool": {"number": 1, "type": "drill", "diameter": 5.0, "tip_angle": 118.0,
              "flutes": 2},
     "op": {"type": "drill", "tool_number": 1, "cycle": "peck", "peck": 6.0,
            "heights": {**H, "bottom": -12.0},
            "geometry": {"points": [[-10, -10, 0], [10, -10, 0], [10, 10, 0],
                                    [-10, 10, 0]]}, **FEEDS}},
    {"cell": "cam_thread_mill", "machine": "mill3",
     "stock": {"type": "box", "min": [-8, -8, -12], "max": [8, 8, 0],
               "material": "aluminum"},
     "tool": {"number": 1, "type": "thread_mill", "diameter": 6.0, "thread_pitch": 1.5,
              "flutes": 3},
     "op": {"type": "thread_mill", "tool_number": 1, "thread_pitch": 1.5,
            "heights": {**H, "bottom": -10.0},
            "geometry": {"points": [[0, 0, 0]], "regions": [
                {"outer": [[-5, -5], [5, -5], [5, 5], [-5, 5]]}]}, **FEEDS}},
    # ---- 3D ----------------------------------------------------------------
    {"cell": "cam_parallel3d", "machine": "mill3", "stock": MESH_STOCK, "tool": BALL4,
     "op": {"type": "parallel3d", "tool_number": 1, "stepover": 3.2,
            "heights": {"clearance": 20, "retract": 6, "top": 0, "bottom": -15},
            "geometry": {"mesh": _dome()}, **FEEDS}},
    {"cell": "cam_waterline3d", "machine": "mill3", "stock": MESH_STOCK, "tool": BALL4,
     "op": {"type": "contour3d", "tool_number": 1, "stepdown": 2.4,
            "heights": {"clearance": 20, "retract": 6, "top": 0, "bottom": -14},
            "geometry": {"mesh": _dome()}, **FEEDS}},
    {"cell": "cam_scallop3d", "machine": "mill3", "stock": MESH_STOCK, "tool": BALL4,
     "op": {"type": "scallop3d", "tool_number": 1, "stepover": 3.0,
            "heights": {"clearance": 20, "retract": 6, "top": 0, "bottom": -15},
            "geometry": {"mesh": _wave()}, **FEEDS}},
    {"cell": "cam_pocket3d", "machine": "mill3", "stock": MESH_STOCK, "tool": BALL4,
     "op": {"type": "pocket3d", "tool_number": 1, "stepover": 3.5, "stepdown": 3.5,
            "heights": {"clearance": 20, "retract": 6, "top": 0, "bottom": -12},
            "geometry": {"mesh": _bowl()}, **FEEDS}},
    # ---- Turning (profile is [z,x]; lathe axis = Z) ------------------------
    {"cell": "cam_turn_rough", "machine": "lathe", "stock": LATHE_STOCK,
     "tool": {"number": 1, "type": "turning_general", "diameter": 0.0},
     "op": {"type": "turn_rough", "tool_number": 1, "stepdown": 2.4,
            "turn_clearance": 1.0, "heights": {"clearance": 12, "retract": 3,
                                               "top": 1, "bottom": -22},
            "geometry": {"profile": [[0, 5], [-7, 5], [-10, 9], [-16, 9],
                                     [-18, 12], [-22, 12]]},
            "rpm": 2500, "feed": 500.0, "plunge": 200.0}},
    {"cell": "cam_turn_finish", "machine": "lathe", "stock": LATHE_STOCK,
     "tool": {"number": 1, "type": "turning_general", "diameter": 0.0},
     "op": {"type": "turn_finish", "tool_number": 1,
            "turn_clearance": 1.0, "heights": {"clearance": 12, "retract": 3,
                                               "top": 1, "bottom": -22},
            "geometry": {"profile": [[0, 5], [-7, 5], [-10, 9], [-16, 9],
                                     [-18, 12], [-22, 12]]},
            "rpm": 3000, "feed": 350.0, "plunge": 200.0}},
    {"cell": "cam_turn_face", "machine": "lathe", "stock": LATHE_STOCK,
     "tool": {"number": 1, "type": "turning_general", "diameter": 0.0},
     "op": {"type": "turn_face", "tool_number": 1, "stepdown": 1.0,
            "turn_clearance": 1.0, "heights": {"clearance": 12, "retract": 3,
                                               "top": 1, "bottom": -3},
            "geometry": {"profile": [[-3, 0], [-3, 13]]},
            "rpm": 2500, "feed": 400.0, "plunge": 200.0}},
    {"cell": "cam_turn_part", "machine": "lathe", "stock": LATHE_STOCK,
     "tool": {"number": 1, "type": "parting_tool", "diameter": 0.0},
     "op": {"type": "turn_part", "tool_number": 1, "infeed": 1.2,
            "turn_clearance": 1.5, "heights": {"clearance": 12, "retract": 3,
                                               "top": 1, "bottom": -22},
            "geometry": {"profile": [[-16, 0], [-16, 14]]},
            "rpm": 1600, "feed": 220.0, "plunge": 150.0}},
    # ---- Cutting -----------------------------------------------------------
    {"cell": "cam_cut2d", "machine": "laser",
     "stock": {"type": "box", "min": [-20, -16, -2], "max": [20, 16, 0],
               "material": "steel_mild"},
     "tool": {"number": 1, "type": "laser_head", "diameter": 0.2},
     "op": {"type": "cut2d", "tool_number": 1, "passes": 1, "power": 0.8,
            "pierce_delay": 0.2, "cut_height": 1.0, "pierce_height": 3.0,
            "heights": {"clearance": 8, "retract": 3, "top": 0, "bottom": -2},
            "geometry": {"contours": [
                [[-16, -12], [16, -12], [16, 12], [-16, 12], [-16, -12]],
                [[-6, -4], [6, -4], [6, 4], [-6, 4], [-6, -4]],
            ]}, "feed_rate": 1800.0, "feed": 1800.0}},
    {"cell": "cam_probe", "machine": "mill3", "stock": MILL_STOCK,
     "tool": {"number": 1, "type": "probe", "diameter": 6.0},
     "op": {"type": "probe", "tool_number": 1,
            "heights": {"clearance": 12, "retract": 4, "top": 0, "bottom": -4},
            "geometry": {"points": [[-14, 0, -3], [14, 0, -3], [0, -14, -3],
                                    [0, 14, -3]]},
            "feed": 200.0, "plunge": 100.0}},
    # ---- Workspace-tab exemplar (title-bar Manufacture tab, T-BL-wsicons) --------
    # A concentric 2D pocket over rectangular stock -- the ringed cutter passes read
    # unmistakably as "CNC toolpath" at 32px, the clearest small-size Manufacture cue.
    # Clone of cam_pocket2d with a dedicated cell so the title bar can blit it directly.
    {"cell": "ws_manufacture", "machine": "mill3", "stock": MILL_STOCK, "tool": EM6,
     "op": {"type": "pocket2d", "tool_number": 1, "stepover": 3.2, "stepdown": 6.0,
            "pattern": "concentric", "heights": H,
            "geometry": {"regions": [{"outer": SQUARE}]}, **FEEDS}},
]

CAM_CELLS = [s["cell"] for s in CAM_SPECS]


def _run_spec(cb, spec) -> tuple[list, dict]:
    """Run one op's full CAM chain. Returns (moves, sim_result). Raises on failure."""
    job = cb.call("cam_new", {"name": f"icon_{spec['cell']}", "post": "grbl"})
    doc_id, rev = job["doc_id"], job["revision"]
    try:
        r1 = cb.call("cam_apply_ops", {"doc_id": doc_id, "base_revision": rev, "ops": [
            {"op": "add_tool", "tool": spec["tool"]},
            {"op": "add_setup", "ref": "S", "setup": {
                "name": "S1", "machine": {"kind": spec["machine"]},
                "stock": spec["stock"]}},
        ]})
        setup_id = r1["id_map"]["S"]
        r2 = cb.call("cam_apply_ops", {"doc_id": doc_id, "base_revision": r1["revision"],
                                       "ops": [{"op": "add_operation",
                                                "setup_id": setup_id,
                                                "operation": spec["op"]}]})
        gen = cb.call("cam_generate", {"doc_id": doc_id})
        for w in gen.get("warnings", []):
            if "error" in w:
                raise RuntimeError(f"cam_generate error: {w['error']}")
        tps = gen.get("toolpaths", [])
        moves = [m for tp in tps for m in tp.get("moves", [])]
        feed_moves = [m for m in moves if m.get("kind") in ("cut", "arc", "lead", "plunge")]
        if not feed_moves:
            raise RuntimeError(f"no cutting moves generated ({len(moves)} total moves)")
        sim = cb.call("cam_simulate", {"doc_id": doc_id})
        res = (sim.get("results") or [{}])[0]
        if not res.get("ok", False):
            raise RuntimeError(f"cam_simulate not ok: {res.get('warnings')}")
        return moves, res
    finally:
        try:
            cb.call("cam_close", {"doc_id": doc_id})
        except Exception:
            pass


def render_all(out_dir: Path = RENDER_DIR, only: list[str] | None = None):
    """Render every Manufacture op tile. Returns (done_names, failures dict)."""
    # core_bridge does `from .daemon_bridge import DaemonError` inside call(), which only
    # resolves when core_bridge is imported as the `services.mcp` package member. Prefer the
    # package import (add REPO to sys.path); fall back to the bare top-level import for envs
    # where services/mcp is already the sole path entry.
    try:
        sys.path.insert(0, str(REPO))
        from services.mcp import core_bridge as cb  # type: ignore
    except Exception:
        import core_bridge as cb  # type: ignore
    done, failures = [], {}
    for spec in CAM_SPECS:
        name = spec["cell"]
        if only and name not in only:
            continue
        try:
            moves, res = _run_spec(cb, spec)
            lw = 1.5 if spec["machine"] == "lathe" else 1.0
            render_toolpath_tile(name, moves, spec["stock"], out_dir, lw_scale=lw)
            print(f"[cam_icons] {name}: {len(moves)} moves, "
                  f"removed {res.get('removed_volume_mm3', 0):.0f} mm3, "
                  f"cycle {res.get('cycle_time_s', 0):.0f} s")
            done.append(name)
        except Exception as e:  # noqa: BLE001 — every op failure must be reported, not raised
            failures[name] = str(e)
            print(f"[cam_icons] {name}: FAILED — {e}")
    return done, failures


if __name__ == "__main__":
    only = sys.argv[1:] or None
    done, failures = render_all(only=only)
    print(f"[cam_icons] {len(done)} rendered, {len(failures)} failed")
    for n, e in failures.items():
        print(f"  FAIL {n}: {e}")
    sys.exit(1 if failures else 0)
