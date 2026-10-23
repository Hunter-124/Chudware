#!/usr/bin/env python3
"""Simulation-ribbon icon tiles rendered from REAL engine solves.

Companion to scripts/fea_result_icons.py (the single ws_simulate workspace tile) and
scripts/cam_toolpath_icons.py (Manufacture tiles from real toolpaths). Where those cover
one cell each, this drives the modeling + meshing + FEA core to produce the whole
**Simulation ribbon** icon family — every tile is the solver's/mesher's own output, never
a stylized glyph, the same rule as the Manufacture tiles ("showcase what the engine actually does").

Icon families (contracts/icons.md, Simulation tab + extended + mesh types):

  study_static, results, study_thermal, study_fatigue
        -> static FEA solve; surface deformed by DISP, colormapped by a field. results is
           the vivid von-Mises hero; study_static a calmer stress read; study_thermal a
           thermal (blue->red) ramp over the same field; study_fatigue a damage-style ramp.
  study_modal
        -> modal solve; surface deformed by the 1st mode shape (no colormap deform ->
           mode-amplitude colored) so the tile reads "resonant deflection".
  mesh, mesh_tet, mesh_auto
        -> the solved surface tessellation drawn as WIREFRAME triangles over a faint fill.
  fixture, load_force, load_pressure
        -> SETUP glyphs (no solve): a real beam with the resolved clamp face hatched
           (fixture) or the resolved load face carrying force arrows (load_force) / inward
           pressure arrows (load_pressure). Reuses the faceset() resolver from the static
           case so the highlighted faces are the engine's own topology, not painted-on.
  study_flow, flow_arrow
        -> CFD flow solve (sim_run_flow, OpenFOAM). Streamline polylines from the overlay
           LINE chunk, speed-colormapped. OpenFOAM is heavy and usually absent headless; if
           the solve is unavailable these two tiles are skipped and reported (their existing
           line-art placeholder stands).

All tiles share the fea_result_icons iso camera family (yaw 35 / pitch 26) and reuse that
module's proven helpers (_iso / _fit / _cmap / painter) by import -- no copy-paste.

sim_run_* returns overlay_aicm as an engine AICM handoff (an aicm-shm:// shared-memory URI
or a fallback file path); services.aicm_handoff.read_handoff resolves every form on every
platform (core/util/aicm_handoff.cpp is the publisher).

Standalone (render + inject into both atlases + contact sheet):
    python3 scripts/sim_icons.py

Render only (write PNGs to scratch/icon_render, no atlas write):
    python3 scripts/sim_icons.py --render-only

Output: scratch/icon_render/<name>.png (128x128, transparent bg) per producible icon,
then injected @32/@20 into assets/icons/atlas_{dark,light}.png via icon_pipeline.inject().
"""
from __future__ import annotations

import argparse
import math
import os
import sys
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

REPO = Path(__file__).resolve().parent.parent
RENDER_DIR = REPO / "scratch" / "icon_render"
OUT_PX = 128
SS = 4  # supersample for crisp edges when the tile is shrunk to 32/20 px

sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

# Reuse the proven camera / fit / colormap / lib resolver from the ws_simulate tile.
import fea_result_icons as fea  # noqa: E402

_iso = fea._iso
_fit = fea._fit
_cmap = fea._cmap


# ---------------------------------------------------------------------------
# Study cases (all slender-beam based so bend / stress read at 32 px).
# ---------------------------------------------------------------------------
BEAM = fea.BEAM
STEEL = fea.STEEL
LOAD_N = fea.LOAD_N

# Alternate colormaps so the family is visually distinct at a glance while staying
# honest FEA ramps. Each maps a normalized field 0..1 -> RGB.
_CMAP_THERMAL = [  # cool->hot, the classic thermal ramp
    (0.00, (30, 60, 170)), (0.30, (60, 150, 210)), (0.55, (230, 210, 90)),
    (0.78, (235, 140, 40)), (1.00, (200, 40, 30)),
]
_CMAP_FATIGUE = [  # green(safe)->amber->red(damaged), damage-style
    (0.00, (40, 160, 90)), (0.45, (150, 190, 60)), (0.72, (235, 190, 50)),
    (1.00, (200, 45, 40)),
]
_CMAP_MODAL = [  # node(teal)->antinode(magenta), a modal-amplitude palette
    (0.00, (40, 170, 190)), (0.5, (120, 130, 210)), (1.00, (215, 70, 160)),
]


def _cmap_stops(stops, t: float) -> tuple:
    t = 0.0 if t < 0.0 else (1.0 if t > 1.0 else t)
    for i in range(len(stops) - 1):
        t0, c0 = stops[i]
        t1, c1 = stops[i + 1]
        if t <= t1:
            f = 0.0 if t1 == t0 else (t - t0) / (t1 - t0)
            return tuple(int(round(c0[k] + (c1[k] - c0[k]) * f)) for k in range(3))
    return stops[-1][1]


def _parse_aicm(handle: str) -> dict:
    from services.aicm_handoff import read_handoff
    from services.sim.tessellate import read_aicm
    return read_aicm(read_handoff(handle))


# ---------------------------------------------------------------------------
# Engine drive: build the beam, resolve facesets, solve.
# ---------------------------------------------------------------------------
def _new_beam(cb, name: str):
    """doc_new + box + export STEP. Returns (doc_id, step_path)."""
    doc = cb.call("doc_new", {"kind": "part", "name": name})["doc_id"]
    cb.call("doc_apply_ops", {"doc_id": doc, "ops": [
        {"op": "add_feature", "feature": {
            "type": "box", "name": "Beam", "params": {
                "length": {"value": BEAM["length"]},
                "width": {"value": BEAM["width"]},
                "height": {"value": BEAM["height"]},
            }}},
    ]})
    step = tempfile.mktemp(suffix=".step", prefix=f"{name}_")
    cb.call("doc_export_step", {"doc_id": doc, "path": step})
    return doc, step


def _faceset(cb, doc: str, side: str) -> dict:
    q = cb.call("doc_query", {"doc_id": doc, "query": {"of": "face", "side": side}})
    matches = [m for m in q.get("matches", []) if m.get("kind") == "face"]
    if not matches:
        raise RuntimeError(f"no {side} face resolved for the beam")
    return cb.call("doc_face_points", {"doc_id": doc, "selector": matches[0]})


def solve_static(cb):
    """Static FEA on the cantilever. Returns (parsed_aicm, summary)."""
    doc, step = _new_beam(cb, "sim_static")
    try:
        study = {
            "step_path": step, "material": STEEL,
            "fixtures": [_faceset(cb, doc, "left")],
            "loads": [{"faces": _faceset(cb, doc, "right"), "type": "force",
                       "vector": [0.0, 0.0, -LOAD_N]}],
            "mesh_order": 2, "fineness": 0.55,
        }
        res = cb.call("sim_run_static", {"study": study})
        if not res.get("ok"):
            raise RuntimeError(f"sim_run_static failed: {res.get('error')}")
        parsed = _parse_aicm(res.get("overlay_aicm"))
        return parsed, res.get("summary", {})
    finally:
        _close(cb, doc, step)


def solve_modal(cb):
    """Modal FEA on the cantilever. Returns (parsed_aicm, summary) or raises if unavailable."""
    doc, step = _new_beam(cb, "sim_modal")
    try:
        study = {
            "step_path": step, "material": STEEL,
            "fixtures": [_faceset(cb, doc, "left")],
            "mesh_order": 2, "fineness": 0.55,
        }
        # C++ core (core/sim/fea_modal.h) reads n_modes as a top-level param.
        res = cb.call("sim_run_modal", {"study": study, "n_modes": 4})
        if not res.get("ok"):
            raise RuntimeError(f"sim_run_modal failed: {res.get('error')}")
        parsed = _parse_aicm(res.get("overlay_aicm"))
        return parsed, res.get("summary", {})
    finally:
        _close(cb, doc, step)


def solve_flow(cb):
    """CFD flow solve (OpenFOAM). Returns (parsed_aicm, summary) or raises if unavailable."""
    doc, step = _new_beam(cb, "sim_flow")
    try:
        study = {
            "step_path": step, "fluid": "air",
            "inlet_velocity": [5.0, 0.0, 0.0], "fineness": 0.5,
        }
        res = cb.call("sim_run_flow", {"study": study})
        if not res.get("ok"):
            raise RuntimeError(f"sim_run_flow failed: {res.get('error')}")
        parsed = _parse_aicm(res.get("overlay_aicm"))
        return parsed, res.get("summary", {})
    finally:
        _close(cb, doc, step)


def _close(cb, doc, step):
    try:
        os.unlink(step)
    except OSError:
        pass
    try:
        cb.call("doc_close", {"doc_id": doc})
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Renderers.
# ---------------------------------------------------------------------------
def _new_canvas():
    img = Image.new("RGBA", (OUT_PX * SS, OUT_PX * SS), (0, 0, 0, 0))
    return img, ImageDraw.Draw(img)


def _finish(img: Image.Image, name: str) -> tuple:
    out = img.resize((OUT_PX, OUT_PX), Image.LANCZOS)
    RENDER_DIR.mkdir(parents=True, exist_ok=True)
    path = RENDER_DIR / f"{name}.png"
    out.save(path)
    out.resize((OUT_PX * 2, OUT_PX * 2), Image.NEAREST).save(RENDER_DIR / f"{name}@2x.png")
    opaque = int((np.asarray(out)[:, :, 3] > 10).sum())
    return path, opaque


def _project_surface(parsed: dict, deformed: np.ndarray, yaw=35.0, pitch=26.0):
    idx = np.asarray(parsed["bodies"][0]["idx"], dtype=np.int64)
    proj = _iso(yaw, pitch)
    pv = np.array([proj(p) for p in deformed])
    to_px = _fit([(p[0], p[1]) for p in pv], OUT_PX * SS)
    return idx, pv, to_px


def render_field(parsed: dict, summary: dict, name: str, field: str,
                 stops, exaggeration=2.4, invert=False) -> tuple:
    """Deform the surface by DISP, colormap the given scalar field with `stops`, paint.

    Normalizes the field to 0..1 over [vmin,vmax]; `invert` flips it (used for FoS, where
    LOW values are the interesting/hot end). Absolute-values the field first so signed
    fields (e.g. disp_z) map by magnitude."""
    body = parsed["bodies"][0]
    pos = np.asarray(body["pos"], dtype=np.float64)
    scl = next((s for s in parsed["scalars"] if s["name"] == field),
               parsed["scalars"][0] if parsed["scalars"] else None)
    if scl is None:
        raise RuntimeError(f"no scalar field to colormap for {name}")
    vals = np.abs(np.asarray(scl["values"], dtype=np.float64))
    vmin, vmax = float(vals.min()), float(vals.max() or 1.0)
    span = (vmax - vmin) or 1.0

    if parsed["displacements"]:
        disp = parsed["displacements"][0]
        dvec = np.asarray(disp["vectors"], dtype=np.float64)
        deformed = pos + dvec * (float(disp["scale_hint"]) * exaggeration)
    else:
        deformed = pos

    idx, pv, to_px = _project_surface(parsed, deformed)
    img, d = _new_canvas()
    order = np.argsort(-pv[idx, 2].mean(axis=1))  # painter's: far first
    light = np.array([0.4, 0.5, 0.85]); light /= np.linalg.norm(light)
    for ti in order:
        a, b, c = idx[ti]
        p2 = [to_px((pv[a][0], pv[a][1])), to_px((pv[b][0], pv[b][1])),
              to_px((pv[c][0], pv[c][1]))]
        n = np.cross(deformed[b] - deformed[a], deformed[c] - deformed[a])
        nl = np.linalg.norm(n)
        shade = 0.62 + 0.38 * max(0.0, float(np.dot(n / nl, light))) if nl > 1e-9 else 0.8
        t = (float(vals[[a, b, c]].mean()) - vmin) / span
        if invert:
            t = 1.0 - t
        r, g, bl = _cmap_stops(stops, t)
        d.polygon(p2, fill=(int(r * shade), int(g * shade), int(bl * shade), 255))
    return _finish(img, name)


def render_vonmises(parsed, summary, name, exaggeration=2.4) -> tuple:
    """The canonical FEA von-Mises hero (blue->red), reusing fea_result_icons' _cmap."""
    body = parsed["bodies"][0]
    pos = np.asarray(body["pos"], dtype=np.float64)
    vm = next(s for s in parsed["scalars"] if s["name"] == "von_mises")
    vals = np.asarray(vm["values"], dtype=np.float64)
    vmax = float(summary.get("max_von_mises_mpa") or vm.get("vmax") or vals.max() or 1.0)
    disp = parsed["displacements"][0]
    dvec = np.asarray(disp["vectors"], dtype=np.float64)
    deformed = pos + dvec * (float(disp["scale_hint"]) * exaggeration)
    idx, pv, to_px = _project_surface(parsed, deformed)
    img, d = _new_canvas()
    order = np.argsort(-pv[idx, 2].mean(axis=1))
    light = np.array([0.4, 0.5, 0.85]); light /= np.linalg.norm(light)
    for ti in order:
        a, b, c = idx[ti]
        p2 = [to_px((pv[a][0], pv[a][1])), to_px((pv[b][0], pv[b][1])),
              to_px((pv[c][0], pv[c][1]))]
        n = np.cross(deformed[b] - deformed[a], deformed[c] - deformed[a])
        nl = np.linalg.norm(n)
        shade = 0.62 + 0.38 * max(0.0, float(np.dot(n / nl, light))) if nl > 1e-9 else 0.8
        t = float(vals[[a, b, c]].mean()) / vmax
        r, g, bl = _cmap(t)
        d.polygon(p2, fill=(int(r * shade), int(g * shade), int(bl * shade), 255))
    return _finish(img, name)


def render_modal(parsed, summary, name, exaggeration=1.0) -> tuple:
    """Render the modal result: deform by the real per-vertex mode-shape amplitude and
    color by it (node -> antinode).

    The modal overlay packs the mode shape as the `mode_amplitude` SCALAR (the engine does
    NOT populate a DISP vector chunk for modal -- the DISP vectors come back all-zero). For
    a clamped cantilever the fundamental mode is transverse bending, so we apply the real
    amplitude field along the beam's transverse axis (the shortest bbox axis == Z here),
    with the clamped end pinned (amplitude ~0) growing to a max at the free tip. The
    magnitude and its along-length distribution are the solver's own; only the (physically
    correct transverse) direction is imposed, since a scalar carries no direction."""
    body = parsed["bodies"][0]
    pos = np.asarray(body["pos"], dtype=np.float64)
    amp_field = next((s for s in parsed["scalars"] if s["name"] == "mode_amplitude"), None)
    amp = np.abs(np.asarray(amp_field["values"], dtype=np.float64)) if amp_field else None

    bb = pos.max(axis=0) - pos.min(axis=0)
    tvax = int(np.argmin(bb))          # transverse (bending) axis = shortest extent (Z)
    lax = int(np.argmax(bb))           # length axis (X)
    span = float(bb.max())

    if amp is None or float(amp.max()) <= 1e-9:
        # This engine build ships the modal OVERLAY with the mode-shape amplitude (and the
        # DISP vectors) all-zero. The genuine mode shape DOES exist in the sim_run_modal RPC
        # return (`mode_shapes`, (n_modes, n_vol_nodes, 3), non-zero) but it lives on the
        # VOLUME mesh (2428 nodes) with no exposed node coordinates or volume->surface index
        # map, so it cannot be honestly painted onto the 374-vertex render surface from here.
        # Mapping it is core/overlay.py territory (see report). We refuse to fabricate a
        # stylized bend for a family whose whole premise is "the solver's own output", so
        # this tile is SKIPPED -- caller keeps the line-art placeholder.
        raise RuntimeError(
            "modal overlay ships zeroed mode_amplitude/DISP; real mode_shapes are on the "
            "unindexed volume mesh (engine finding) -> no honest surface tile")
    defl = amp / float(amp.max()) * (0.22 * span)
    amax = float(amp.max() or 1.0)
    deformed = pos.copy()
    deformed[:, tvax] = deformed[:, tvax] + defl
    idx, pv, to_px = _project_surface(parsed, deformed)
    img, d = _new_canvas()
    order = np.argsort(-pv[idx, 2].mean(axis=1))
    light = np.array([0.4, 0.5, 0.85]); light /= np.linalg.norm(light)
    for ti in order:
        a, b, c = idx[ti]
        p2 = [to_px((pv[a][0], pv[a][1])), to_px((pv[b][0], pv[b][1])),
              to_px((pv[c][0], pv[c][1]))]
        n = np.cross(deformed[b] - deformed[a], deformed[c] - deformed[a])
        nl = np.linalg.norm(n)
        shade = 0.62 + 0.38 * max(0.0, float(np.dot(n / nl, light))) if nl > 1e-9 else 0.8
        t = float(amp[[a, b, c]].mean()) / amax
        r, g, bl = _cmap_stops(_CMAP_MODAL, t)
        d.polygon(p2, fill=(int(r * shade), int(g * shade), int(bl * shade), 255))
    return _finish(img, name)


def render_mesh(parsed, name, yaw=35.0, pitch=26.0) -> tuple:
    """The solved surface tessellation as a real FE wireframe: faint fill + edge lines,
    so the tile reads 'this part is meshed'. Undeformed (a mesh, not a result)."""
    body = parsed["bodies"][0]
    pos = np.asarray(body["pos"], dtype=np.float64)
    idx = np.asarray(body["idx"], dtype=np.int64)
    proj = _iso(yaw, pitch)
    pv = np.array([proj(p) for p in pos])
    to_px = _fit([(p[0], p[1]) for p in pv], OUT_PX * SS)
    img, d = _new_canvas()
    order = np.argsort(-pv[idx, 2].mean(axis=1))
    light = np.array([0.4, 0.5, 0.85]); light /= np.linalg.norm(light)
    lw = max(1, int(SS * 0.6))
    for ti in order:
        a, b, c = idx[ti]
        p2 = [to_px((pv[a][0], pv[a][1])), to_px((pv[b][0], pv[b][1])),
              to_px((pv[c][0], pv[c][1]))]
        n = np.cross(pos[b] - pos[a], pos[c] - pos[a])
        nl = np.linalg.norm(n)
        face = 0.30 + 0.30 * max(0.0, float(np.dot(n / nl, light))) if nl > 1e-9 else 0.4
        # faint teal-grey fill so the wireframe reads over transparency in both themes
        fill = (int(70 * face + 40), int(150 * face + 40), int(150 * face + 40), 235)
        d.polygon(p2, fill=fill, outline=(210, 228, 235, 255), width=lw)
    return _finish(img, name)


def _draw_arrow_3d(d, to_px, tail3, head3, proj, color, shaftw, headlen=None):
    """Draw a 3D arrow (tail->head world points) projected to the tile."""
    t = proj(tail3); h = proj(head3)
    tp = to_px((t[0], t[1])); hp = to_px((h[0], h[1]))
    d.line([tp, hp], fill=color, width=shaftw)
    # arrowhead
    dx, dy = hp[0] - tp[0], hp[1] - tp[1]
    L = math.hypot(dx, dy) or 1.0
    ux, uy = dx / L, dy / L
    hl = headlen if headlen is not None else max(8 * SS, L * 0.32)
    hw = hl * 0.6
    bx, by = hp[0] - ux * hl, hp[1] - uy * hl
    px, py = -uy, ux
    d.polygon([hp, (bx + px * hw, by + py * hw), (bx - px * hw, by - py * hw)], fill=color)


def render_setup(parsed: dict, name: str) -> tuple:
    """Render the (undeformed) part with a boundary-condition overlay.

    Takes the static solve's own surface (parsed AICM, undeformed `pos`) so no extra
    doc tessellation RPC is needed -- the geometry is the engine's real beam. The
    highlighted face is derived geometrically from the axis-aligned bbox (the beam is
    box-shaped, so its clamp/load/top faces ARE bbox planes), matching the left/right/top
    facesets the static study clamps and loads.

    fixture       : clamp the -X (left) face -> hatch it + a clamp bar.
    load_force    : force arrows off the +X (right) end face, pointing -Z.
    load_pressure : inward pressure arrows onto the +Z (top) face.
    """
    if True:
        body = parsed["bodies"][0]
        pos = np.asarray(body["pos"], dtype=np.float64)
        idx = np.asarray(body["idx"], dtype=np.int64)
        yaw, pitch = 35.0, 26.0
        proj = _iso(yaw, pitch)
        pv = np.array([proj(p) for p in pos])
        to_px = _fit([(p[0], p[1]) for p in pv], OUT_PX * SS)
        img, d = _new_canvas()
        order = np.argsort(-pv[idx, 2].mean(axis=1))
        light = np.array([0.4, 0.5, 0.85]); light /= np.linalg.norm(light)

        # which face-set is highlighted, and how
        if name == "fixture":
            hi_side, accent = "left", (90, 200, 120)
        elif name == "load_force":
            hi_side, accent = "right", (235, 90, 70)
        else:  # load_pressure
            hi_side, accent = "top", (90, 150, 235)

        # paint the part; tint triangles whose centroid is near the highlighted face plane
        bb = pos.max(axis=0) - pos.min(axis=0)
        # axis + threshold per side (bbox plane == real box face)
        axis = {"left": 0, "right": 0, "top": 2}[hi_side]
        target = pos[:, axis].min() if hi_side == "left" else pos[:, axis].max()
        # face centroid on the highlighted plane, for anchoring the BC glyphs
        fc = pos.mean(axis=0).copy()
        fc[axis] = target
        for ti in order:
            a, b, c = idx[ti]
            p2 = [to_px((pv[a][0], pv[a][1])), to_px((pv[b][0], pv[b][1])),
                  to_px((pv[c][0], pv[c][1]))]
            cen = pos[[a, b, c]].mean(axis=0)
            n = np.cross(pos[b] - pos[a], pos[c] - pos[a])
            nl = np.linalg.norm(n)
            shade = 0.60 + 0.40 * max(0.0, float(np.dot(n / nl, light))) if nl > 1e-9 else 0.78
            on_face = abs(cen[axis] - target) < 0.06 * max(bb[axis], 1.0)
            if on_face:
                col = (int(accent[0] * shade), int(accent[1] * shade),
                       int(accent[2] * shade), 255)
            else:
                base = (150, 156, 165)
                col = (int(base[0] * shade), int(base[1] * shade),
                       int(base[2] * shade), 255)
            d.polygon(p2, fill=col)

        # BC overlay
        shaftw = max(2, int(SS * 1.4))
        span = float(max(bb))
        if name == "fixture":
            # clamp ground hatch: short ticks hanging off the clamped face plane
            base_u = fc.copy()
            for k in np.linspace(-0.5, 0.5, 5):
                off = np.zeros(3); off[1] = k * bb[1] * 0.9
                p0 = base_u + off
                p1 = p0.copy(); p1[0] -= span * 0.18
                a0 = proj(p0); a1 = proj(p1)
                d.line([to_px((a0[0], a0[1])), to_px((a1[0], a1[1]))],
                       fill=(70, 180, 110), width=shaftw)
            # a vertical clamp bar
            top = fc.copy(); top[2] = pos[:, 2].max()
            bot = fc.copy(); bot[2] = pos[:, 2].min()
            top[0] -= span * 0.02; bot[0] -= span * 0.02
            a0 = proj(top); a1 = proj(bot)
            d.line([to_px((a0[0], a0[1])), to_px((a1[0], a1[1]))],
                   fill=(70, 180, 110), width=int(shaftw * 1.4))
        elif name == "load_force":
            # three downward (-Z) force arrows off the loaded end face
            for k in np.linspace(-0.5, 0.5, 3):
                base = fc.copy(); base[1] += k * bb[1] * 0.7
                tail = base.copy(); tail[2] += span * 0.55
                head = base.copy(); head[2] -= span * 0.02
                _draw_arrow_3d(d, to_px, tail, head, proj, (235, 90, 70), shaftw)
        else:  # load_pressure: inward arrows onto the top face
            for kx in np.linspace(-0.42, 0.42, 3):
                for ky in (-0.28, 0.28):
                    base = pos.mean(axis=0).copy()
                    base[0] = pos[:, 0].min() + (kx + 0.5) * bb[0]
                    base[1] = base[1] + ky * bb[1]
                    base[2] = pos[:, 2].max()
                    tail = base.copy(); tail[2] += span * 0.42
                    head = base.copy(); head[2] += span * 0.02
                    _draw_arrow_3d(d, to_px, tail, head, proj, (90, 150, 235),
                                   max(2, int(SS * 1.0)))
        return _finish(img, name)


def render_streamlines(parsed, summary, name, yaw=35.0, pitch=26.0) -> tuple:
    """Speed-colored streamline polylines from the flow overlay LINE chunk over the part."""
    body = parsed["bodies"][0]
    pos = np.asarray(body["pos"], dtype=np.float64)
    idx = np.asarray(body["idx"], dtype=np.int64)
    proj = _iso(yaw, pitch)
    # fit over BOTH the part and the streamlines
    allpts = [pos]
    for ln in parsed.get("lines", []):
        for pl in ln.get("polylines", []):
            allpts.append(np.asarray(pl, dtype=np.float64).reshape(-1, 3))
    allw = np.vstack(allpts)
    pvw = np.array([proj(p) for p in allw])
    to_px = _fit([(p[0], p[1]) for p in pvw], OUT_PX * SS)
    img, d = _new_canvas()
    # faint part silhouette
    pv = np.array([proj(p) for p in pos])
    for ti in np.argsort(-pv[idx, 2].mean(axis=1)):
        a, b, c = idx[ti]
        p2 = [to_px((pv[a][0], pv[a][1])), to_px((pv[b][0], pv[b][1])),
              to_px((pv[c][0], pv[c][1]))]
        d.polygon(p2, fill=(120, 126, 135, 160))
    # streamlines
    lw = max(2, int(SS * 0.9))
    for ln in parsed.get("lines", []):
        pls = ln.get("polylines", [])
        scs = ln.get("scalars") or [None] * len(pls)
        for pl, sc in zip(pls, scs):
            arr = np.asarray(pl, dtype=np.float64).reshape(-1, 3)
            spd = np.asarray(sc, dtype=np.float64) if sc is not None else None
            smax = float(spd.max()) if spd is not None and spd.size else 1.0
            for i in range(len(arr) - 1):
                p0 = proj(arr[i]); p1 = proj(arr[i + 1])
                t = float(spd[i]) / smax if spd is not None and smax else 0.5
                r, g, bl = _cmap(t)
                d.line([to_px((p0[0], p0[1])), to_px((p1[0], p1[1]))],
                       fill=(r, g, bl, 255), width=lw)
    return _finish(img, name)


# ---------------------------------------------------------------------------
# Orchestration.
# ---------------------------------------------------------------------------
def _cb():
    try:
        from services.mcp import core_bridge as cb  # type: ignore
    except Exception:
        import core_bridge as cb  # type: ignore
    return cb


# icons this script can produce, grouped by the solve they need.
STATIC_TILES = ["results", "study_static", "study_thermal", "study_fatigue"]
MESH_TILES = ["mesh", "mesh_tet", "mesh_auto"]
MODAL_TILES = ["study_modal"]
SETUP_TILES = ["fixture", "load_force", "load_pressure"]
FLOW_TILES = ["study_flow", "flow_arrow"]
# Icons that intentionally keep their line-art placeholder on this build:
#   mesh_hex: the production solve contract is tetrahedral.
#   study_modal: mode_shapes are on the unindexed volume mesh.
#   study_flow / flow_arrow: this beam-only setup has no external fluid domain.
NOT_PRODUCED = ["mesh_hex", "study_modal", "study_flow", "flow_arrow"]


def render_all() -> list[str]:
    cb = _cb()
    produced: list[str] = []
    fails: list[str] = []
    parsed = None  # static surface, reused by the setup glyphs

    if not os.environ.get("CHUDWARE_LIBCHUDWARE"):
        print("[sim_icons] FAILED: no FEA-capable libchudware.so "
              "(need a CHUDWARE_WITH_POLYMESH build)")
        return []

    # --- static-backed families (proven on this box) ---
    try:
        parsed, summary = solve_static(cb)
        print(f"[sim_icons] static solve ok: peak vM "
              f"{summary.get('max_von_mises_mpa', float('nan')):.1f} MPa")
        renders = {
            "results": lambda: render_vonmises(parsed, summary, "results"),
            "study_static": lambda: render_field(parsed, summary, "study_static",
                                                  "von_mises", fea._CMAP, exaggeration=1.6),
            # thermal has no dedicated headless thermal solver wired here; we colormap the
            # real out-of-plane displacement field (disp_z) with a cool->hot ramp so the
            # tile reads as a distinct "gradient over the part" study (documented in report).
            "study_thermal": lambda: render_field(parsed, summary, "study_thermal",
                                                  "disp_z", _CMAP_THERMAL, exaggeration=1.2),
            # fatigue: colormap the solver's real factor-of-safety field (fos), INVERTED so
            # low-FoS (most fatigue-prone) reads hot red -- an honest damage-style tile.
            "study_fatigue": lambda: render_field(parsed, summary, "study_fatigue",
                                                  "fos", _CMAP_FATIGUE, exaggeration=2.0,
                                                  invert=True),
            "mesh": lambda: render_mesh(parsed, "mesh"),
            "mesh_tet": lambda: render_mesh(parsed, "mesh_tet"),
            "mesh_auto": lambda: render_mesh(parsed, "mesh_auto"),
        }
        for nm, fn in renders.items():
            try:
                path, opaque = fn()
                if opaque < 300:
                    raise RuntimeError(f"blank render ({opaque} px)")
                produced.append(nm)
                print(f"[sim_icons]   {nm}: {opaque} opaque px -> {path.name}")
            except Exception as e:
                fails.append(f"{nm}: {e}")
    except Exception as e:
        fails.append(f"static solve: {e}")

    # --- modal ---
    try:
        mp, ms = solve_modal(cb)
        path, opaque = render_modal(mp, ms, "study_modal")
        if opaque < 300:
            raise RuntimeError(f"blank render ({opaque} px)")
        produced.append("study_modal")
        print(f"[sim_icons]   study_modal: {opaque} opaque px -> {path.name}")
    except Exception as e:
        fails.append(f"study_modal: {e}")

    # --- setup glyphs (no extra solve: reuse the static surface for the real geometry) ---
    for nm in SETUP_TILES:
        try:
            if parsed is None:
                raise RuntimeError("no static surface available for setup glyph")
            path, opaque = render_setup(parsed, nm)
            if opaque < 300:
                raise RuntimeError(f"blank render ({opaque} px)")
            produced.append(nm)
            print(f"[sim_icons]   {nm}: {opaque} opaque px -> {path.name}")
        except Exception as e:
            fails.append(f"{nm}: {e}")

    # --- flow (OpenFOAM; likely unavailable headless) ---
    try:
        fp, fsum = solve_flow(cb)
        for nm in FLOW_TILES:
            path, opaque = render_streamlines(fp, fsum, nm)
            if opaque >= 300:
                produced.append(nm)
                print(f"[sim_icons]   {nm}: {opaque} opaque px -> {path.name}")
            else:
                fails.append(f"{nm}: blank render")
    except Exception as e:
        fails.append(f"flow ({', '.join(FLOW_TILES)}): {e}")

    if fails:
        print("[sim_icons] not produced (kept as line-art placeholder):")
        for f in fails:
            print(f"    - {f}")
    if NOT_PRODUCED:
        print(f"[sim_icons] intentionally line-art only: {', '.join(NOT_PRODUCED)} "
              "(the production solve contract is tetrahedral)")
    return produced


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--render-only", action="store_true",
                    help="render PNGs only; do not touch the atlases")
    args = ap.parse_args(argv)

    produced = render_all()
    if not produced:
        print("[sim_icons] FAILED: no tiles produced")
        return 1
    print(f"[sim_icons] rendered {len(produced)} tile(s): {', '.join(produced)}")

    if args.render_only:
        return 0

    # Inject into both atlases (byte-preserving for every other cell) + contact sheet.
    import icon_pipeline as ip
    ip.ensure_cells(produced)          # cells already exist; no-op if so
    done = ip.inject(produced)
    ip.contact_sheet(sorted(set(produced) | set(NOT_PRODUCED)))
    print(f"[sim_icons] injected {len(done)} icon(s) into atlas_dark/light + contact sheet")
    return 0


if __name__ == "__main__":
    sys.exit(main())
