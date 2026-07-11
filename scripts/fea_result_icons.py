#!/usr/bin/env python3
"""Simulation workspace-tab icon rendered from a REAL static-FEA solve.

Companion to scripts/cam_toolpath_icons.py (CAM tiles from real toolpaths): this
renders the Simulate workspace tile from an honest ``sim_run_static`` result — a
slender steel cantilever clamped at one end with a transverse tip load, so it bends
visibly and stress concentrates at the root. NEVER a stylized glyph: the deformed
shape and the von-Mises colormap are the solver's own output.

Pipeline (drives the same libaicad RPC bridge the MCP server / test_fea_live_bridge
use, all raw core-bridge method names):

    doc_new -> doc_apply_ops(add_feature box) -> doc_export_step
            -> doc_query/doc_face_points (resolve clamp + load facesets)
            -> sim_run_static  (Netgen tet10 solve; needs the occt build .so)

``sim_run_static`` returns the full field (per-node von_mises / displacement) AND an
``overlay_aicm`` file: the surface tessellation with per-surface-vertex von_mises
(SCLR) + displacement vectors (DISP) already mapped by engine.sim.overlay. We read
that AICM with engine.doc.tessellate.read_aicm, deform each surface vertex by
(displacement * exaggeration), colormap by von Mises (blue->green->red), and paint
the triangles painter's-algorithm in the same iso camera the other icon tiles use
(yaw 35 / pitch 26). The whole thing doubles as a smoke test of the static solver.

Standalone:  python3 scripts/fea_result_icons.py
Output:      scratch/icon_render/ws_simulate.png  (128x128, transparent bg)
"""
from __future__ import annotations

import math
import os
import struct
import sys
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

REPO = Path(__file__).resolve().parent.parent
RENDER_DIR = REPO / "scratch" / "icon_render"
OUT_PX = 128
SS = 4  # supersample for crisp triangle edges when the tile is shrunk to 32/20 px

sys.path.insert(0, str(REPO))


def _resolve_libaicad() -> None:
    """Point core_bridge at a FEA-capable libaicad.so. The plain build-linux .so
    does NOT carry the Netgen FEA RPCs ("unknown method: sim_run_static"), so we
    prefer build-occt-linux. Worktrees carry no build dir -> fall back to the main
    checkout via the git common dir (same probe as cam_toolpath_icons.py)."""
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
        # occt build FIRST — it is the one with AICAD_WITH_NETGEN / the FEA plugin.
        for rel in ("core/build-occt-linux/bin/libaicad.so",
                    "host/build-occt-linux/bin/libaicad.so"):
            p = root / rel
            if p.exists():
                os.environ["AI_CAD_LIBAICAD"] = str(p)
                return


_resolve_libaicad()


# ---------------------------------------------------------------------------
# Case: slender steel cantilever, clamped -X, transverse -Z tip load.
# Aspect ratio is deliberately slender (L >> h) so tip deflection is large and the
# bend reads unmistakably at 32 px. Load sized so peak stress ~yields => vivid
# blue->green->red colormap span. This is the same study shape as
# services/mcp/test_fea_live_bridge.py / cad_tools.run_fea_static.
# ---------------------------------------------------------------------------
BEAM = {"length": 300.0, "width": 26.0, "height": 16.0}   # mm
LOAD_N = 2500.0                                            # transverse tip force (-Z)
STEEL = {"name": "icon-steel", "E_mpa": 200000.0, "nu": 0.3,
         "density_g_cm3": 7.85, "yield_mpa": 250.0}

# von-Mises colormap stops (blue -> cyan -> green -> yellow -> red), the classic
# FEA stress ramp. RGB 0..255.
_CMAP = [
    (0.00, (40, 70, 200)),    # low stress: blue
    (0.25, (40, 170, 210)),   # cyan
    (0.50, (60, 200, 90)),    # green
    (0.72, (240, 210, 40)),   # yellow
    (1.00, (220, 45, 40)),    # peak: red
]


def _cmap(t: float) -> tuple:
    t = 0.0 if t < 0.0 else (1.0 if t > 1.0 else t)
    for i in range(len(_CMAP) - 1):
        t0, c0 = _CMAP[i]
        t1, c1 = _CMAP[i + 1]
        if t <= t1:
            f = 0.0 if t1 == t0 else (t - t0) / (t1 - t0)
            return tuple(int(round(c0[k] + (c1[k] - c0[k]) * f)) for k in range(3))
    return _CMAP[-1][1]


def _iso(yaw_deg: float = 35.0, pitch_deg: float = 26.0):
    """Orthographic iso projector matching the modeling / CAM icon camera family
    (copied from scripts/cam_toolpath_icons.py so all workspace tiles frame alike)."""
    ya, pa = math.radians(yaw_deg), math.radians(pitch_deg)
    cy, sy, cp, sp = math.cos(ya), math.sin(ya), math.cos(pa), math.sin(pa)

    def proj(p):
        x, y, z = p
        xr = x * cy - y * sy
        yr = x * sy + y * cy
        u = xr
        v = z * cp - yr * sp
        depth = z * sp + yr * cp  # +depth = farther from camera (for painter sort)
        return (u, v, depth)
    return proj


def _fit(pts_uv, px: int, margin: float = 0.06):
    xs = [p[0] for p in pts_uv]
    ys = [p[1] for p in pts_uv]
    w = max(xs) - min(xs) or 1.0
    h = max(ys) - min(ys) or 1.0
    s = (1.0 - 2 * margin) * px / max(w, h)
    cx, cy = (max(xs) + min(xs)) / 2, (max(ys) + min(ys)) / 2

    def to_px(p):
        return (px / 2 + (p[0] - cx) * s, px / 2 - (p[1] - cy) * s)
    return to_px


# ---------------------------------------------------------------------------
# Overlay resolution: file path OR aicm-shm:// URI -> AICM bytes.
# ---------------------------------------------------------------------------
def _read_overlay(handle: str) -> bytes:
    """Resolve a sim_run_* ``overlay_aicm`` handle to raw AICM bytes.

    The engine emits two forms (matches scripts/sim_icons.py._read_overlay):
      * a plain filesystem path (older engine / file fallback), and
      * ``aicm-shm:///aicad_overlay_N`` -> POSIX shm object ``/aicad_overlay_N``,
        i.e. ``/dev/shm/aicad_overlay_N``, whose layout is an 8-byte little-endian
        size header followed by exactly that many AICM payload bytes
        (core/util/aicm_handoff.cpp publish_shm: memcpy(p,&sz,8); memcpy(p+8,data,n)).
    """
    if not handle:
        raise RuntimeError("empty overlay handle")
    prefix = "aicm-shm://"
    if handle.startswith(prefix):
        name = handle[len(prefix):]          # "/aicad_overlay_0"
        name = "/" + name.lstrip("/")        # normalize to a single leading slash
        shm_path = Path("/dev/shm") / name.lstrip("/")
        if not shm_path.exists():
            raise RuntimeError(f"shm overlay object missing: {shm_path}")
        raw = shm_path.read_bytes()
        if len(raw) < 8:
            raise RuntimeError(f"shm overlay too small: {len(raw)} bytes")
        (sz,) = struct.unpack_from("<Q", raw, 0)
        if sz <= 0 or 8 + sz > len(raw):
            raise RuntimeError(f"shm overlay bad size header: sz={sz} raw={len(raw)}")
        return raw[8:8 + sz]
    p = Path(handle)
    if not p.exists():
        raise RuntimeError(f"overlay file missing: {handle}")
    return p.read_bytes()


# ---------------------------------------------------------------------------
# Engine drive
# ---------------------------------------------------------------------------

def solve_cantilever(cb):
    """Build the beam, solve static FEA, return the parsed overlay AICM + stats.

    Returns (aicm_dict, summary, stats) where aicm_dict is engine.doc.tessellate
    .read_aicm output (surface pos/idx + von_mises SCLR + displacement DISP).
    """
    try:  # deployed engine aliases `engine`; source tree uses `engine_oracle`
        from engine.doc.tessellate import read_aicm
    except ImportError:
        from engine_oracle.doc.tessellate import read_aicm

    doc = cb.call("doc_new", {"kind": "part", "name": "ws_simulate_cantilever"})["doc_id"]
    try:
        cb.call("doc_apply_ops", {"doc_id": doc, "ops": [
            {"op": "add_feature", "feature": {
                "type": "box", "name": "Beam", "params": {
                    "length": {"value": BEAM["length"]},
                    "width": {"value": BEAM["width"]},
                    "height": {"value": BEAM["height"]},
                }}},
        ]})
        step = tempfile.mktemp(suffix=".step", prefix="ws_simulate_")
        cb.call("doc_export_step", {"doc_id": doc, "path": step})

        def faceset(side: str) -> dict:
            q = cb.call("doc_query", {"doc_id": doc,
                                      "query": {"of": "face", "side": side}})
            matches = [m for m in q.get("matches", []) if m.get("kind") == "face"]
            if not matches:
                raise RuntimeError(f"no {side} face resolved for the beam")
            return cb.call("doc_face_points", {"doc_id": doc, "selector": matches[0]})

        study = {
            "step_path": step, "material": STEEL,
            "fixtures": [faceset("left")],
            "loads": [{"faces": faceset("right"), "type": "force",
                       "vector": [0.0, 0.0, -LOAD_N]}],
            "mesh_order": 2, "fineness": 0.55,
        }
        res = cb.call("sim_run_static", {"study": study})
        if not res.get("ok"):
            raise RuntimeError(f"sim_run_static failed: {res.get('error')}")
        overlay = res.get("overlay_aicm")
        if not overlay:
            raise RuntimeError(f"no overlay_aicm produced: {overlay!r}")
        # overlay_aicm is either a plain file path OR an "aicm-shm:///..." POSIX
        # shared-memory URI (current engine); _read_overlay resolves both.
        parsed = read_aicm(_read_overlay(overlay))
        stats = {"n_nodes": res.get("n_nodes"), "n_elems": res.get("n_elems"),
                 "n_dof": res.get("n_dof"), "mesh": res.get("mesh_type_used")}
        try:
            os.unlink(step)
        except OSError:
            pass
        return parsed, res.get("summary", {}), stats
    finally:
        try:
            cb.call("doc_close", {"doc_id": doc})
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Render: deform the surface by displacement, colormap by von Mises, paint.
# ---------------------------------------------------------------------------

def render_tile(parsed: dict, summary: dict, out_dir: Path,
                exaggeration: float = 2.4, yaw: float = 35.0,
                pitch: float = 26.0) -> tuple:
    """Deform + colormap-render the FEA surface to ws_simulate.png. Returns (path, opaque_px)."""
    body = parsed["bodies"][0]
    pos = np.asarray(body["pos"], dtype=np.float64)          # (n,3) undeformed surface
    idx = np.asarray(body["idx"], dtype=np.int64)            # (t,3) triangles

    vm = next(s for s in parsed["scalars"] if s["name"] == "von_mises")
    vm_vals = np.asarray(vm["values"], dtype=np.float64)     # per-vertex MPa
    vmax = float(summary.get("max_von_mises_mpa") or vm["vmax"] or vm_vals.max() or 1.0)

    disp = parsed["displacements"][0]
    dvec = np.asarray(disp["vectors"], dtype=np.float64)     # (n,3) mm
    # scale_hint already makes peak deflection ~10% of bbox; exaggerate a bit more so
    # the bend is unmistakable at icon size.
    deformed = pos + dvec * (float(disp["scale_hint"]) * exaggeration)

    proj = _iso(yaw, pitch)
    pv = np.array([proj(p) for p in deformed])               # (n,3): u,v,depth
    to_px = _fit([(p[0], p[1]) for p in pv], OUT_PX * SS)

    img = Image.new("RGBA", (OUT_PX * SS, OUT_PX * SS), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # painter's algorithm: farthest triangles first. depth = pv[:,2]
    tri_depth = pv[idx, 2].mean(axis=1)
    order = np.argsort(-tri_depth)

    # simple lambert shade from tri normal (in deformed space) toward an iso key light
    light = np.array([0.4, 0.5, 0.85]); light /= np.linalg.norm(light)
    for ti in order:
        a, b, c = idx[ti]
        p2 = [to_px((pv[a][0], pv[a][1])), to_px((pv[b][0], pv[b][1])),
              to_px((pv[c][0], pv[c][1]))]
        n = np.cross(deformed[b] - deformed[a], deformed[c] - deformed[a])
        nl = np.linalg.norm(n)
        shade = 0.62 + 0.38 * max(0.0, float(np.dot(n / nl, light))) if nl > 1e-9 else 0.8
        t = float(vm_vals[[a, b, c]].mean()) / vmax
        r, g, bl = _cmap(t)
        col = (int(r * shade), int(g * shade), int(bl * shade), 255)
        d.polygon(p2, fill=col, outline=None)

    out = img.resize((OUT_PX, OUT_PX), Image.LANCZOS)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "ws_simulate.png"
    out.save(path)

    opaque = int((np.asarray(out)[:, :, 3] > 10).sum())
    # 2x preview for human review
    out.resize((OUT_PX * 2, OUT_PX * 2), Image.NEAREST).save(out_dir / "ws_simulate@2x.png")
    return path, opaque


def main() -> int:
    try:
        from services.mcp import core_bridge as cb  # type: ignore
    except Exception:
        import core_bridge as cb  # type: ignore

    if not os.environ.get("AI_CAD_LIBAICAD"):
        print("[fea_icons] FAILED: no FEA-capable libaicad.so found "
              "(need core/build-occt-linux/bin/libaicad.so with AICAD_WITH_NETGEN)")
        return 1

    parsed, summary, stats = solve_cantilever(cb)
    path, opaque = render_tile(parsed, summary, RENDER_DIR)

    body = parsed["bodies"][0]
    print(f"[fea_icons] solved: {stats['n_nodes']} nodes / {stats['n_elems']} elems "
          f"({stats['mesh']}), {stats['n_dof']} dof")
    print(f"[fea_icons] peak von Mises {summary.get('max_von_mises_mpa'):.1f} MPa, "
          f"tip disp {summary.get('max_disp_mm'):.2f} mm, "
          f"min FoS {summary.get('min_fos'):.2f}")
    print(f"[fea_icons] surface {body['n_verts']} verts / {body['n_tris']} tris")
    print(f"[fea_icons] wrote {path}  ({opaque} opaque px)")
    if opaque < 300:
        print("[fea_icons] FAILED: render is blank/near-blank")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
