"""fea_static simsuite — shared geometry + study case library.

Builds parts through the real engine, meshes them through Polyhedral-FEA, and solves
linear statics with the returned inline tet4/tet10 mesh.

Used by two consumers:
  * benchmarks/simsuite/fea_static_bench.py  (the simsuite bench module)
  * services/mcp/test_fea_static_suite.py    (the pytest regression suite)

Analytic references (each carries its formula citation at the point of use):
  * Cantilever tip deflection      δ = P·L³/(3·E·I)   — Roark's Formulas for
    Stress & Strain, 8th ed., Table 8.1 case 1a (Euler-Bernoulli).
  * Plate with central hole        σ_peak = Ktn·σ_net, Ktn = 2 + (1 − d/W)³
    — Peterson's Stress Concentration Factors (Heywood fit for a finite-width
    strip with a central circular hole in tension); Kirsch Kt→3 as d/W→0.
  * Thick-wall cylinder (Lamé)     σ_θ(Ri) = p(Ro²+Ri²)/(Ro²−Ri²), σ_r(Ri) = −p,
    u_r(Ri) = (p·Ri/E)·((Ro²+Ri²)/(Ro²−Ri²) + ν)  (open ends / plane stress)
    — Timoshenko & Goodier, Theory of Elasticity, 3rd ed., §28.

FIXED CORE BUG (2026-07-12, regression-witnessed by case_pressure_native_bar):
the engine's native ``type=="pressure"`` load path used to deposit per-face
normals with ARBITRARY fan orientation (core/sim/fea_run.cpp,
accumulate_face_weights: kTetFaces was not consistently outward-oriented).
Measured on a 20×20 mm bar end face: sign-mixed resultant (−1000, 0, 0) N for
p=10 MPa instead of ±4000 N (axial displacement 3.5× tet4 / 6.9× tet10 the
force-equivalent case). Fixed by flipping each fan normal outward against its
element centroid; the native path now reads <0.5 % on the axial bar. Pressure
studies below still use ``consistent_pressure_loads`` — it independently
exercises the oracle-side traction math and keeps the two paths cross-checked:
the textbook work-equivalent surface traction f_i = p·(∫N_i dA)·n̂_out computed
in Python with outward orientation enforced, applied through single-node
"force" loads (the force path is verified correct to <0.5 % on the cantilever).
"""
from __future__ import annotations

import math
import os
import sys
import tempfile
import time

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)
os.environ.setdefault("CHUDWARE_ENGINE_DAEMON", "0")

# Explicit material dict (never a named preset) so the analytic reference and the
# solver always agree on the exact properties. density is g/cm³ (steel 7.85).
STEEL = {"name": "fs-steel", "E_mpa": 200000.0, "nu": 0.3,
         "density_g_cm3": 7.85, "yield_mpa": 250.0}


# --------------------------------------------------------------------- bridge

def _cb():
    from services.mcp import core_bridge as cb  # never bare `import core_bridge`
    return cb


def solver_available() -> tuple[bool, str]:
    """True when the staged .so answers sim_run_static."""
    try:
        cb = _cb()
        if not cb.available():
            return False, "core_bridge reports libchudware not loadable"
        r = cb.call("sim_run_static", {"study": {}})
    except Exception as exc:  # noqa: BLE001 — CoreError('unknown method') or load fail
        if "unknown method" in str(exc):
            return False, f"staged libchudware.so lacks sim_run_static (no Polyhedral-FEA): {exc}"
        return False, f"engine probe failed: {exc}"
    # A reachable solver rejects the empty study IN-BAND (ok:false), never rc!=0.
    if isinstance(r, dict) and r.get("ok") is False:
        return True, ""
    return True, ""


# ----------------------------------------------------------------- op helpers

def _dim(v):
    return _cb().dim(v)


def _feat(ftype, name, params=None, refs=None):
    return _cb().feat(ftype, name, params, refs)


def _region(sketch_fid, cx=0.0, cy=0.0):
    return {"kind": "region", "sketch": sketch_fid, "centroid": [cx, cy], "hint_index": 0}


def _datum(axis):
    return {"kind": "datum", "feature": axis}


def _sketch_ops(entities, plane, name, fid, offset=None):
    f = {"type": "sketch", "name": name,
         "params": {"entities": [], "constraints": []}, "refs": {"plane": plane}}
    if offset is not None:
        f["offset"] = offset
    steps = [{"op": "add_feature", "feature": f}]
    steps += [{"op": "sketch_add_entity", "sketch": fid, "entity": e} for e in entities]
    return steps


def _poly(pts, pref="e"):
    out = []
    for i in range(len(pts)):
        p1, p2 = pts[i], pts[(i + 1) % len(pts)]
        out.append({"type": "line", "p1": list(p1), "p2": list(p2), "id": f"{pref}{i}"})
    return out


def build_part(name: str, ops: list) -> str:
    """doc_new + doc_apply_ops; asserts every feature rebuilt ok. Returns doc_id."""
    cb = _cb()
    doc = cb.call("doc_new", {"kind": "part", "name": name})["doc_id"]
    r = cb.call("doc_apply_ops", {"doc_id": doc, "ops": ops})
    fs = (r.get("rebuild") or {}).get("feature_status", [])
    if not fs or not all(f.get("ok") for f in fs):
        raise RuntimeError(f"{name}: feature rebuild failed: {fs}")
    return doc


def export_step(doc: str) -> str:
    path = tempfile.mktemp(suffix=".step", prefix="fea_static_")
    _cb().call("doc_export_step", {"doc_id": doc, "path": path})
    return path


def close_doc(doc: str) -> None:
    try:
        _cb().call("doc_close", {"doc_id": doc})
    except Exception:  # noqa: BLE001 — teardown best-effort
        pass


def mesh_step(step_path: str, order: int = 1, fineness: float = 0.25) -> dict:
    m = _cb().call("sim_mesh", {"step_path": step_path, "fineness": fineness,
                                "order": order})
    if not m.get("ok"):
        raise RuntimeError(f"sim_mesh failed: {m.get('error')}")
    return m


def static_solve(mesh: dict, fixtures: list, loads: list, material: dict = STEEL) -> dict:
    """sim_run_static on the INLINE mesh (fields align with mesh['points'])."""
    r = _cb().call("sim_run_static", {"study": {
        "points": mesh["points"], "tets": mesh["tets"],
        "nodes_per_elem": mesh["nodes_per_elem"],
        "material": material, "fixtures": fixtures, "loads": loads}})
    if not r.get("ok"):
        raise RuntimeError(f"sim_run_static failed: {r.get('error')}")
    return r


# ------------------------------------------------------------------ facesets

def plane_faceset(point, normal, tol_mm=80.0, perp_tol_mm=0.01) -> dict:
    """Manual faceset for a known flat face: one on-plane sample + the face normal.
    The split proximity test (perp tight, in-plane wide) grabs the whole face."""
    return {"points": [list(point)], "tol_mm": tol_mm,
            "face_normal": list(normal), "perp_tol_mm": perp_tol_mm}


def side_faceset(doc: str, side: str) -> dict:
    """doc_query {'of':'face','side':...} + doc_face_points (the host's own path)."""
    cb = _cb()
    q = cb.call("doc_query", {"doc_id": doc, "query": {"of": "face", "side": side}})
    faces = [m for m in q.get("matches", []) if m.get("kind") == "face"]
    if not faces:
        raise RuntimeError(f"no face for side={side!r}")
    return cb.call("doc_face_points", {"doc_id": doc, "selector": faces[0]})


def query_faces(doc: str) -> list:
    return _cb().call("doc_query", {"doc_id": doc, "query": {"of": "face"}})["matches"]


# ---------------------------------------------- consistent pressure (oracle side)

# tet faces + tet10 mid-edge nodes per face (mesher edge order
# {0,1}=4 {1,2}=5 {2,0}=6 {3,0}=7 {3,2}=8 {3,1}=9 — core/sim/fea_run.cpp kTetFaces).
_KTET_FACES = [((0, 1, 2), (4, 5, 6)), ((0, 1, 3), (4, 9, 7)),
               ((0, 2, 3), (6, 8, 7)), ((1, 2, 3), (5, 8, 9))]
_EDGE_ENDS = [(4, 0, 1), (5, 1, 2), (6, 2, 0), (7, 3, 0), (8, 3, 2), (9, 3, 1)]


def consistent_pressure_loads(mesh: dict, on_surface, p_mpa: float) -> list:
    """Work-equivalent nodal forces for a uniform pressure on the surface selected by
    ``on_surface(x, y, z) -> bool``, returned as single-node 'force' loads.

    Textbook consistent surface traction (e.g. Zienkiewicz & Taylor, The Finite
    Element Method, 7th ed., §2 boundary integrals): f_i = p · (∫ N_i dA) · n̂,
    with n̂ the OUTWARD surface normal (positive p pushes INTO the material).
    tri3: each corner carries A/3. tri6 (tet10): corners 0, each mid-side A/3.
    Outward orientation is enforced per face against the owning tet's centroid —
    the missing step in the engine's native pressure path (see module docstring).
    """
    pts, tets, npe = mesh["points"], mesh["tets"], mesh["nodes_per_elem"]
    if npe not in (4, 10):
        raise ValueError(f"consistent_pressure_loads supports tet4/tet10, got npe={npe}")
    n = len(pts) // 3
    node_set = {i for i in range(n) if on_surface(pts[3 * i], pts[3 * i + 1], pts[3 * i + 2])}
    if npe == 10:  # complete mid-edge nodes whose both corner endpoints are selected
        for e in range(len(tets) // 10):
            el = tets[e * 10:(e + 1) * 10]
            for (mid, a, b) in _EDGE_ENDS:
                if el[a] in node_set and el[b] in node_set:
                    node_set.add(el[mid])
    fvec: dict[int, list] = {}
    stride = npe
    for e in range(len(tets) // stride):
        el = tets[e * stride:(e + 1) * stride]
        corners = el[:4]
        cex = sum(pts[3 * c] for c in corners) / 4.0
        cey = sum(pts[3 * c + 1] for c in corners) / 4.0
        cez = sum(pts[3 * c + 2] for c in corners) / 4.0
        for (corner, mid) in _KTET_FACES:
            cn = [el[c] for c in corner]
            if not all(c in node_set for c in cn):
                continue
            p0 = pts[3 * cn[0]:3 * cn[0] + 3]
            pa = pts[3 * cn[1]:3 * cn[1] + 3]
            pb = pts[3 * cn[2]:3 * cn[2] + 3]
            ax, ay, az = pa[0] - p0[0], pa[1] - p0[1], pa[2] - p0[2]
            bx, by, bz = pb[0] - p0[0], pb[1] - p0[1], pb[2] - p0[2]
            nvx = 0.5 * (ay * bz - az * by)
            nvy = 0.5 * (az * bx - ax * bz)
            nvz = 0.5 * (ax * by - ay * bx)
            area = math.sqrt(nvx * nvx + nvy * nvy + nvz * nvz)
            if area < 1e-12:
                continue
            nx, ny, nz = nvx / area, nvy / area, nvz / area
            cfx = (p0[0] + pa[0] + pb[0]) / 3.0
            cfy = (p0[1] + pa[1] + pb[1]) / 3.0
            cfz = (p0[2] + pa[2] + pb[2]) / 3.0
            if nx * (cfx - cex) + ny * (cfy - cey) + nz * (cfz - cez) < 0:
                nx, ny, nz = -nx, -ny, -nz  # enforce OUTWARD
            targets = [el[i] for i in mid] if npe == 10 else cn
            w = area / 3.0
            for t in targets:
                f = fvec.setdefault(t, [0.0, 0.0, 0.0])
                f[0] -= p_mpa * w * nx  # positive pressure pushes INTO the surface
                f[1] -= p_mpa * w * ny
                f[2] -= p_mpa * w * nz
    return [{"faces": {"points": [[pts[3 * i], pts[3 * i + 1], pts[3 * i + 2]]],
                       "tol_mm": 1e-4},
             "type": "force", "vector": f} for i, f in fvec.items()]


# ------------------------------------------------------------ geometry builders
# Each returns (doc_id, info-dict). Dimensions in mm.

def build_cantilever(L=1000.0, B=50.0, H=50.0):
    doc = build_part("fs_cantilever", [
        _feat("box", "Beam", {"length": _dim(L), "width": _dim(B), "height": _dim(H)})])
    return doc, {"L": L, "B": B, "H": H}


def build_plate_with_hole(L=120.0, W=60.0, T=3.0, d=12.0):
    doc = build_part("fs_plate_hole", [
        _feat("box", "Plate", {"length": _dim(L), "width": _dim(W), "height": _dim(T)}),
        _feat("hole", "Hole", {"style": "simple", "end": "through_all",
                               "diameter": _dim(d), "positions": [[0, 0]]},
              {"face": {"of": "face", "side": "top"}}),
    ])
    return doc, {"L": L, "W": W, "T": T, "d": d}


def build_annulus(Ro=40.0, Ri=20.0, H=120.0):
    doc = build_part("fs_annulus", [
        _feat("cylinder", "Outer", {"radius": _dim(Ro), "height": _dim(H)}),
        _feat("cylinder", "Bore", {"radius": _dim(Ri), "height": _dim(H + 10),
                                   "mode": "cut"}),
    ])
    return doc, {"Ro": Ro, "Ri": Ri, "H": H}


def build_lbracket(fillet_r=6.0):
    """L-profile on plane.right, extruded 60, filleted on the inner corner edge."""
    ops = _sketch_ops(_poly([(0, 0), (45, 0), (45, 5), (5, 5), (5, 65), (0, 65)]),
                      "plane.right", "LSk", "f1")
    ops.append({"op": "add_feature", "feature": {
        "type": "extrude", "name": "Body",
        "params": {"mode": "boss", "end": "blind", "depth": _dim(60)},
        "refs": {"profile": [_region("f1", 10, 3)]}}})
    doc = build_part("fs_lbracket", ops)
    cb = _cb()
    edges = cb.call("doc_query", {"doc_id": doc, "query": {"of": "edge"}})["matches"]
    inner = [e for e in edges
             if e.get("surf") == "line"
             and abs(e.get("centroid", [0, 0, 0])[1] - 5) < 0.5
             and abs(e.get("centroid", [0, 0, 0])[2] - 5) < 0.5]
    if not inner:
        raise RuntimeError("L-bracket inner corner edge not found")
    r = cb.call("doc_apply_ops", {"doc_id": doc, "ops": [
        _feat("fillet", "InnerFillet", {"radius": _dim(fillet_r)}, {"edges": [inner[0]]})]})
    fs = (r.get("rebuild") or {}).get("feature_status", [])
    if not all(f.get("ok") for f in fs):
        raise RuntimeError(f"L-bracket fillet failed: {fs}")
    return doc, {}


def build_shelled_box():
    doc = build_part("fs_shellbox", [
        _feat("box", "B", {"length": _dim(60), "width": _dim(60), "height": _dim(40)}),
        _feat("shell", "Sh", {"thickness": _dim(3), "outward": False},
              {"remove": {"of": "face", "side": "top"}}),
    ])
    return doc, {}


def build_revolved_shaft():
    """Stepped shaft (Ø40 lower half, Ø20 upper half) — sketch profile revolved 360°."""
    ops = _sketch_ops(_poly([(0, 0), (20, 0), (20, 40), (10, 40), (10, 80), (0, 80)], "r"),
                      "plane.front", "RevSk", "f1")
    ops.append({"op": "add_feature", "feature": {
        "type": "revolve", "name": "Shaft",
        "params": {"mode": "boss", "angle": _dim(360)},
        "refs": {"profile": [_region("f1", 10, 20)], "axis": _datum("axis.z")}}})
    doc = build_part("fs_shaft", ops)
    return doc, {}


def build_multi_feature_plate():
    """Box + 4 through-holes + pocket cut + chamfered top edges."""
    ops = [
        _feat("box", "Base", {"length": _dim(80), "width": _dim(50), "height": _dim(10)}),
        _feat("hole", "Holes", {"style": "simple", "end": "through_all",
                                "diameter": _dim(8),
                                "positions": [[-30, -15], [-30, 15], [30, -15], [30, 15]]},
              {"face": {"of": "face", "side": "top"}}),
    ]
    ops += _sketch_ops(_poly([(-15, -10), (15, -10), (15, 10), (-15, 10)], "p"),
                       "plane.top", "PocketSk", "f3", offset=5)
    ops.append({"op": "add_feature", "feature": {
        "type": "extrude", "name": "Pocket",
        "params": {"mode": "cut", "end": "blind", "depth": _dim(5)},
        "refs": {"profile": [_region("f3", 0, 0)]}}})
    ops.append(_feat("chamfer", "Cham", {"distance": _dim(1.5)},
                     {"edges": {"of": "edge", "parallel": "x", "side": "top"}}))
    doc = build_part("fs_multi", ops)
    return doc, {}


def build_loft_tower():
    """Loft: Ø36 circle at z=0 → 24×24 square at z=50 (stresses blended surfaces)."""
    ops = _sketch_ops([{"type": "circle", "center": [0, 0], "radius": 18, "id": "c1"}],
                      "plane.top", "LoftBot", "f1")
    ops += _sketch_ops(_poly([(-12, -12), (12, -12), (12, 12), (-12, 12)], "s"),
                       "plane.top", "LoftTop", "f2", offset=50)
    ops.append({"op": "add_feature", "feature": {
        "type": "loft", "name": "Tower", "params": {"mode": "boss"},
        "refs": {"sections": [_region("f1", 0, 0), _region("f2", 0, 0)]}}})
    doc = build_part("fs_loft", ops)
    return doc, {}


def build_swept_rod():
    """Ø12 circular profile swept along an L-path (40 mm + 30 mm legs) — curved sweep
    surfaces + a bend, the classic mesher stressor."""
    ops = _sketch_ops([{"type": "circle", "center": [0, 0], "radius": 6, "id": "c1"}],
                      "plane.right", "SwProf", "f1")
    ops += _sketch_ops([{"type": "line", "p1": [0, 0], "p2": [40, 0], "id": "p1"},
                        {"type": "line", "p1": [40, 0], "p2": [40, 30], "id": "p2"}],
                       "plane.top", "SwPath", "f2")
    ops.append({"op": "add_feature", "feature": {
        "type": "sweep", "name": "Rod", "params": {"mode": "boss"},
        "refs": {"profile": [_region("f1", 0, 0)], "path": _region("f2", 0, 0)}}})
    doc = build_part("fs_sweep", ops)
    return doc, {}


#: name -> builder; the regression suite asserts every one of these meshes.
GEOMETRY_BUILDERS = {
    "cantilever": build_cantilever,
    "plate_with_hole": build_plate_with_hole,
    "annulus": build_annulus,
    "lbracket_fillet": build_lbracket,
    "shelled_box": build_shelled_box,
    "revolved_shaft": build_revolved_shaft,
    "multi_feature_plate": build_multi_feature_plate,
    "loft_tower": build_loft_tower,
    "swept_rod": build_swept_rod,
}


# ------------------------------------------------------------------ case runners
# Every runner returns the simsuite case dict (README contract) — the pytest suite
# reads metric/ref/rel_err/quality off the same dicts.

def _case(case_id, setup_s, mesh_s, solve_s, metric, ref, detail="", extra=None):
    rel = None
    if metric is not None and ref not in (None, 0):
        rel = abs(metric - ref) / abs(ref)
    out = {"domain": "fea_static", "case": case_id,
           "setup_s": round(setup_s, 3), "mesh_s": round(mesh_s, 3),
           "solve_s": round(solve_s, 3),
           "total_s": round(setup_s + mesh_s + solve_s, 3),
           "metric": metric, "ref": ref, "rel_err": rel,
           "status": "ok", "detail": detail}
    if extra:
        out.update(extra)
    return out


def case_cantilever(case_id, order=2, fineness=0.25):
    """Cantilever tip deflection δ = P·L³/(3·E·I) — Roark 8th, Table 8.1 case 1a
    (Euler-Bernoulli; ±shear compliance at L/H=20 is well under the bounds used)."""
    L, B, H, P = 1000.0, 50.0, 50.0, 1000.0
    I = B * H ** 3 / 12.0
    ref = P * L ** 3 / (3.0 * STEEL["E_mpa"] * I)   # = 3.2000 mm
    t0 = time.time()
    doc, _ = build_cantilever(L, B, H)
    step = export_step(doc)
    fix = plane_faceset([-L / 2, 0, 0], [-1, 0, 0])
    load = plane_faceset([L / 2, 0, 0], [1, 0, 0])
    setup_s = time.time() - t0
    t0 = time.time()
    m = mesh_step(step, order=order, fineness=fineness)
    mesh_s = time.time() - t0
    t0 = time.time()
    r = static_solve(m, [fix], [{"faces": load, "type": "force", "vector": [0, 0, -P]}])
    solve_s = time.time() - t0
    os.unlink(step)
    close_doc(doc)
    got = r["summary"]["max_disp_mm"]
    return _case(case_id, setup_s, mesh_s, solve_s, got, ref,
                 detail=f"tip deflection mm; {m['mesh_type_used']} n={m['n_nodes']} "
                        f"(Roark 8th T8.1-1a δ=PL³/3EI)",
                 extra={"quality": _quality(m)})


def case_plate_hole(case_id, order=2, fineness=0.3):
    """Peak stress at a central hole in a finite-width strip under tension.
    σ_peak = Ktn·σ_net with Ktn = 2 + (1 − d/W)³ (Peterson/Heywood finite-width
    fit; → Kirsch Kt=3 as d/W→0), σ_net = σ_gross·W/(W−d). Metric = peak nodal
    von Mises ON the hole boundary (uniaxial σ_θ there, so vm ≡ |σ_θ|)."""
    L, W, T, d, sigma = 120.0, 60.0, 3.0, 12.0, 10.0
    ktn = 2.0 + (1.0 - d / W) ** 3
    ref = ktn * sigma * W / (W - d)
    t0 = time.time()
    doc, _ = build_plate_with_hole(L, W, T, d)
    step = export_step(doc)
    fix = plane_faceset([-L / 2, 0, 0], [-1, 0, 0])
    load = plane_faceset([L / 2, 0, 0], [1, 0, 0])
    setup_s = time.time() - t0
    t0 = time.time()
    m = mesh_step(step, order=order, fineness=fineness)
    mesh_s = time.time() - t0
    F = sigma * W * T
    t0 = time.time()
    r = static_solve(m, [fix], [{"faces": load, "type": "force", "vector": [F, 0, 0]}])
    solve_s = time.time() - t0
    os.unlink(step)
    close_doc(doc)
    pts, vm = m["points"], r["von_mises"]
    n = len(pts) // 3
    peak = max((vm[i] for i in range(n)
                if math.hypot(pts[3 * i], pts[3 * i + 1]) < d / 2 + 0.7), default=None)
    return _case(case_id, setup_s, mesh_s, solve_s, peak, ref,
                 detail=f"peak vm MPa at hole; Ktn={ktn:.3f} {m['mesh_type_used']} "
                        f"n={m['n_nodes']} (Peterson Ktn=2+(1-d/W)³, Kirsch limit 3)",
                 extra={"quality": _quality(m)})


def case_annulus_lame(case_id, order=2, fineness=0.25):
    """Thick-wall cylinder under internal pressure (open ends) — Lamé, Timoshenko
    & Goodier 3rd ed. §28. Metric = mean radial displacement of the inner surface
    at mid-height, u_r(Ri) = (p·Ri/E)·((Ro²+Ri²)/(Ro²−Ri²) + ν). The mid-height
    band sits ≳ 2·√(R·t) above the clamped base, past the edge-effect decay.
    Secondary (recorded in detail): mean nodal von Mises on the same band vs the
    Lamé principal state (σ_θ, 0, −p) — nodal averaging smooths the surface peak,
    so it is only bracketed by the tests, not tightly asserted.
    Pressure is applied via consistent_pressure_loads (see module docstring)."""
    Ro, Ri, H, p = 40.0, 20.0, 120.0, 10.0
    u_ref = (p * Ri / STEEL["E_mpa"]) * ((Ro ** 2 + Ri ** 2) / (Ro ** 2 - Ri ** 2)
                                         + STEEL["nu"])
    s_t = p * (Ro ** 2 + Ri ** 2) / (Ro ** 2 - Ri ** 2)   # Lamé hoop at Ri
    vm_ref = math.sqrt(0.5 * ((s_t + p) ** 2 + p ** 2 + s_t ** 2))  # (σθ,0,−p) vM
    t0 = time.time()
    doc, _ = build_annulus(Ro, Ri, H)
    step = export_step(doc)
    bot = plane_faceset([0, 0, -H / 2], [0, 0, -1], tol_mm=Ro + 1)
    setup_s = time.time() - t0
    t0 = time.time()
    m = mesh_step(step, order=order, fineness=fineness)
    mesh_s = time.time() - t0
    loads = consistent_pressure_loads(
        m, lambda x, y, z: abs(math.hypot(x, y) - Ri) < 0.05, p)
    t0 = time.time()
    r = static_solve(m, [bot], loads)
    solve_s = time.time() - t0
    os.unlink(step)
    close_doc(doc)
    pts, vm, dm = m["points"], r["von_mises"], r["disp_magnitude"]
    n = len(pts) // 3
    band = [i for i in range(n)
            if abs(math.hypot(pts[3 * i], pts[3 * i + 1]) - Ri) < 0.5
            and abs(pts[3 * i + 2]) < H / 6]
    u = sum(dm[i] for i in band) / len(band) if band else None
    vm_mid = sum(vm[i] for i in band) / len(band) if band else None
    vm_txt = f"{vm_mid:.3f}" if vm_mid is not None else "n/a"
    return _case(case_id, setup_s, mesh_s, solve_s, u, u_ref,
                 detail=f"u_r(Ri) mm (Lamé T&G §28); vm_mid={vm_txt} vs Lamé "
                        f"{vm_ref:.3f} MPa; {m['mesh_type_used']} n={m['n_nodes']} "
                        f"loads={len(loads)}",
                 extra={"quality": _quality(m), "vm_mid": vm_mid, "vm_ref": vm_ref})


def case_annulus_lame_native(case_id, order=2, fineness=0.25):
    """Lamé thick-wall cylinder driven by the ENGINE-NATIVE curved-face pressure path —
    the combined witness for the two 2026-07-12 core fixes:
      * C1  outward-oriented pressure fan normals (accumulate_face_weights), and
      * C2  analytic curved-face facesets (doc_face_points emits surface:{cylinder,...};
        nodes_in_faceset_surface keeps a node iff |dist_to_axis − R| < perp_tol).
    The inner-bore faceset comes from doc_face_points on the live doc (no oracle-side
    consistent_pressure_loads), so a regression in either fix reads several-x here.
    Same analytic reference as case_annulus_lame: u_r(Ri) mid-height band."""
    Ro, Ri, H, p = 40.0, 20.0, 120.0, 10.0
    u_ref = (p * Ri / STEEL["E_mpa"]) * ((Ro ** 2 + Ri ** 2) / (Ro ** 2 - Ri ** 2)
                                         + STEEL["nu"])
    t0 = time.time()
    doc, _ = build_annulus(Ro, Ri, H)
    # Inner bore = the ONE concave cylindrical face; `pick:"inner"` is what tells the
    # two coaxial walls apart (Ro=40 outer vs Ri=20 bore). This used to ask for
    # {"of":"face","curved":True}: `curved` is not a query key, so resolve_geom_query
    # dropped it silently and the query matched all 4 faces of the annulus — measured
    # identical to a bare {"of":"face"}. The radius test below happened to land on the
    # bore regardless, which is exactly why the dead key survived review. The engine
    # now rejects an unknown query key outright (doc/picks.cpp::unknown_query_keys).
    q = _cb().call("doc_query", {"doc_id": doc,
                                 "query": {"of": "face", "surf": "cylinder",
                                           "pick": "inner"}})
    faces = [m for m in q.get("matches", []) if m.get("kind") == "face"]
    if len(faces) != 1:
        raise RuntimeError(
            f"annulus bore: expected exactly 1 inner cylindrical face, got {len(faces)}")
    bore = _cb().call("doc_face_points", {"doc_id": doc, "selector": faces[0]})
    surf = bore.get("surface") or {}
    # C2 witness, kept as an assertion now that the query does the narrowing: the
    # faceset MUST carry the analytic cylinder descriptor at radius Ri, else
    # nodes_in_faceset_surface has no |dist_to_axis - R| test to apply and the
    # pressure spreads off the bore.
    if surf.get("type") != "cylinder" or abs(surf.get("radius", 0.0) - Ri) > 1e-6:
        raise RuntimeError(
            f"annulus bore face is not the analytic r={Ri} cylinder: {surf!r}")
    step = export_step(doc)
    bot = plane_faceset([0, 0, -H / 2], [0, 0, -1], tol_mm=Ro + 1)
    setup_s = time.time() - t0
    t0 = time.time()
    m = mesh_step(step, order=order, fineness=fineness)
    mesh_s = time.time() - t0
    t0 = time.time()
    r = static_solve(m, [bot], [{"faces": bore, "type": "pressure", "magnitude": p}])
    solve_s = time.time() - t0
    os.unlink(step)
    close_doc(doc)
    pts, dm = m["points"], r["disp_magnitude"]
    n = len(pts) // 3
    band = [i for i in range(n)
            if abs(math.hypot(pts[3 * i], pts[3 * i + 1]) - Ri) < 0.5
            and abs(pts[3 * i + 2]) < H / 6]
    u = sum(dm[i] for i in band) / len(band) if band else None
    c = _case(case_id, setup_s, mesh_s, solve_s, u, u_ref,
              detail=f"u_r(Ri) mm (Lamé) via NATIVE curved-face pressure "
                     f"(surface faceset + oriented fan normals); "
                     f"{m['mesh_type_used']} n={m['n_nodes']}",
              extra={"quality": _quality(m)})
    if c["rel_err"] is not None and c["rel_err"] > 0.08:
        c["status"] = "fail"  # regression witness for C1+C2 — do NOT loosen
    return c


def case_pressure_native_bar(case_id, order=2, fineness=0.3):
    """Regression witness: engine-native axial 'pressure' vs analytic δ = σ·L/E
    (uniaxial Hooke). Guards the unoriented-fan-normal defect fixed 2026-07-12
    (core/sim/fea_run.cpp accumulate_face_weights — normals now flipped outward
    vs element centroid; was several-x off before). Emits status 'fail' when out
    of tolerance so any regression stays visible — do NOT loosen."""
    Lb, Bb, p = 200.0, 20.0, 10.0
    ref = p * Lb / STEEL["E_mpa"]                        # δ = σL/E = 0.0100 mm
    t0 = time.time()
    doc, _ = build_cantilever(Lb, Bb, Bb)
    step = export_step(doc)
    fix = plane_faceset([-Lb / 2, 0, 0], [-1, 0, 0])
    load = plane_faceset([Lb / 2, 0, 0], [1, 0, 0])
    setup_s = time.time() - t0
    t0 = time.time()
    m = mesh_step(step, order=order, fineness=fineness)
    mesh_s = time.time() - t0
    t0 = time.time()
    r = static_solve(m, [fix], [{"faces": load, "type": "pressure", "magnitude": p}])
    solve_s = time.time() - t0
    os.unlink(step)
    close_doc(doc)
    got = r["summary"]["max_disp_mm"]
    c = _case(case_id, setup_s, mesh_s, solve_s, got, ref,
              detail="native pressure axial bar, δ=σL/E (Hooke); KNOWN core bug: "
                     "unoriented pressure fan normals (core/sim/fea_run.cpp "
                     "accumulate_face_weights)")
    if c["rel_err"] is not None and c["rel_err"] > 0.05:
        c["status"] = "fail"
    return c


#: regression parts: geometry name -> (fix_side, load_side, force_vector)
REGRESSION_LOADCASES = {
    "lbracket_fillet": ("bottom", "top", [0.0, 200.0, 0.0]),
    "shelled_box": ("bottom", "front", [0.0, 100.0, 0.0]),
    "revolved_shaft": ("bottom", "top", [200.0, 0.0, 0.0]),
    "multi_feature_plate": ("left", "right", [0.0, 0.0, -200.0]),
    "loft_tower": ("bottom", "top", [200.0, 0.0, 0.0]),
    "swept_rod": ("left", "front", [0.0, 0.0, -100.0]),
}


def case_regression(case_id, geom_name, order=1, fineness=0.25, ref_disp=None):
    """Mesh + solve a non-analytic part; metric = max displacement. ``ref_disp``
    (optional) is a finer-mesh self-convergence reference for the same load case."""
    fix_side, load_side, fvec = REGRESSION_LOADCASES[geom_name]
    t0 = time.time()
    doc, _ = GEOMETRY_BUILDERS[geom_name]()
    step = export_step(doc)
    fix = side_faceset(doc, fix_side)
    load = side_faceset(doc, load_side)
    setup_s = time.time() - t0
    t0 = time.time()
    m = mesh_step(step, order=order, fineness=fineness)
    mesh_s = time.time() - t0
    t0 = time.time()
    r = static_solve(m, [fix], [{"faces": load, "type": "force", "vector": fvec}])
    solve_s = time.time() - t0
    os.unlink(step)
    close_doc(doc)
    got = r["summary"]["max_disp_mm"]
    vm = r["summary"]["max_von_mises_mpa"]
    return _case(case_id, setup_s, mesh_s, solve_s, got, ref_disp,
                 detail=f"max disp mm ({'self-convergence ref' if ref_disp else 'regression-only'}); "
                        f"max vm={vm:.2f} MPa; {m['mesh_type_used']} n={m['n_nodes']}",
                 extra={"quality": _quality(m)})


def _quality(m: dict) -> dict:
    return {"n_nodes": m["n_nodes"], "n_elems": m["n_elems"],
            "mesh_type": m["mesh_type_used"], "aspect_max": m["aspect_max"],
            "aspect_avg": m["aspect_avg"], "jacobian_min": m["jacobian_min"],
            "pct_poor": m["pct_poor"]}
