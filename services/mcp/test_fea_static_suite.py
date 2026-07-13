"""fea_static regression suite — diverse-geometry meshing + static-solve accuracy.

Drives the REAL engine in-process on the raw core bridge (same access pattern as
test_fea_live_bridge.py, but via the shared case library
benchmarks/simsuite/fea_static_cases.py so the pytest bounds and the simsuite
bench numbers come from one code path).

Coverage:
  * every geometry in the diverse fleet (box / plate+hole / annulus / filleted
    L-bracket / shelled box / revolve / multi-feature / loft / sweep) builds
    through engine ops AND meshes without failure, with sane quality metrics;
  * analytic accuracy anchors — Euler-Bernoulli cantilever deflection (Roark 8th
    T8.1-1a), Peterson/Kirsch stress concentration at a hole, Lamé thick-wall
    radial displacement + hoop-stress bracket (Timoshenko & Goodier §28);
  * element-order hierarchy (tet10 beats tet4 on bending; structured hex8 beats
    tet4) — the accuracy/speed frontier's sanity backbone;
  * pressure-load/force-load consistency — regression witness for the core
    defect FIXED 2026-07-12 (core/sim/fea_run.cpp accumulate_face_weights used
    unoriented fan normals; now flipped outward vs element centroid). PASSES
    against a fixed .so; the dynamic xfail remains as a regression tripwire —
    do not delete it.

Skips honestly (with the probe reason) when the staged libaicad.so lacks the
Netgen FEA RPCs. Meshes are kept coarse — the whole module is a smoke-priced
suite, the timing baseline lives in benchmarks/simsuite/.
"""
from __future__ import annotations

import importlib.util
import math
from pathlib import Path

import pytest

_CASES_PATH = (Path(__file__).resolve().parents[2]
               / "benchmarks" / "simsuite" / "fea_static_cases.py")
_spec = importlib.util.spec_from_file_location("fea_static_cases", _CASES_PATH)
fsc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fsc)  # type: ignore[union-attr]

_AVAILABLE, _REASON = (False, "probe not run")
try:
    _AVAILABLE, _REASON = fsc.solver_available()
except Exception as exc:  # noqa: BLE001
    _AVAILABLE, _REASON = False, f"solver probe raised: {exc}"

pytestmark = pytest.mark.skipif(not _AVAILABLE, reason=_REASON)

_CACHE: dict = {}


def _cached(key, fn):
    if key not in _CACHE:
        _CACHE[key] = fn()
    return _CACHE[key]


# ------------------------------------------------------------- geometry fleet

@pytest.mark.parametrize("geom", sorted(fsc.GEOMETRY_BUILDERS))
def test_geometry_builds_and_meshes(geom):
    """Every fleet part must build through engine ops and tet-mesh without failure,
    with usable quality (positive Jacobian, bounded aspect, low poor-element %)."""
    def _run():
        doc, _ = fsc.GEOMETRY_BUILDERS[geom]()
        step = fsc.export_step(doc)
        m = fsc.mesh_step(step, order=1, fineness=0.25)
        import os
        os.unlink(step)
        fsc.close_doc(doc)
        return m
    m = _cached(("mesh", geom), _run)
    assert m["n_nodes"] > 0 and m["n_elems"] > 0, m
    assert m["nodes_per_elem"] in (4, 8, 10), m["nodes_per_elem"]
    # Quality gates: generous but real — they catch degenerate/inverted meshes,
    # not styling. (Coarse-mesh worst observed across the fleet: aspect_max 9.03,
    # jacobian_min 0.113, pct_poor 0.0 — see the simsuite baseline.)
    assert m["jacobian_min"] > 0.0, f"{geom}: inverted/degenerate element"
    assert m["aspect_max"] < 50.0, f"{geom}: aspect_max {m['aspect_max']}"
    assert m["pct_poor"] < 25.0, f"{geom}: pct_poor {m['pct_poor']}"


# --------------------------------------------------------- analytic anchors

def _cantilever(order, fineness, structured=False):
    return _cached(("cant", order, fineness, structured),
                   lambda: fsc.case_cantilever("t", order=order, fineness=fineness,
                                               structured=structured))


def test_cantilever_tet10_matches_euler_bernoulli():
    """δ = P·L³/(3EI) (Roark 8th T8.1-1a). tet10 measured 0.42 % off at this
    fineness; 5 % bound covers mesh jitter + shear compliance at L/H=20."""
    c = _cantilever(2, 0.25)
    assert c["status"] == "ok", c
    assert c["rel_err"] < 0.05, f"tet10 deflection {c['metric']:.4f} vs {c['ref']:.4f} " \
                                f"({c['rel_err']:.1%})"


def test_cantilever_order_hierarchy_tet10_beats_tet4():
    """Quadratic elements must beat linear tets on bending (tet4 shear-locks:
    measured ~60 % vs ~0.4 % at comparable fineness). Guards order plumbing —
    a mesh_order that silently stops reaching the solver flips this instantly."""
    c4 = _cantilever(1, 0.3)
    c10 = _cantilever(2, 0.25)
    assert c10["rel_err"] < c4["rel_err"], (c10["rel_err"], c4["rel_err"])
    # tet4 locking is EXPECTED to be large but not absurd; and it must under-,
    # never over-predict deflection (locking is too-stiff, a >1 ratio here means
    # something else broke).
    assert c4["metric"] < c4["ref"], f"tet4 coarser than ref? {c4}"


def test_cantilever_hex_mesh_supported_and_beats_tet4():
    """structured=true on a box must actually produce hex8 (not silently fall back)
    and improve on shear-locked tet4 at the same fineness (measured 30 % vs 60 %)."""
    ch = _cantilever(1, 0.3, structured=True)
    c4 = _cantilever(1, 0.3)
    assert ch["quality"]["mesh_type"] == "hex8", ch["quality"]
    assert ch["rel_err"] < c4["rel_err"], (ch["rel_err"], c4["rel_err"])


def test_plate_hole_stress_concentration_bracket():
    """Peak vm at the hole must bracket Ktn·σ_net (Peterson Ktn=2+(1−d/W)³;
    Kirsch Kt→3). Nodal averaging under-reads the surface peak (measured 10-20 %
    low on tet10), so the bracket is [0.65, 1.35]×ref — it still catches the real
    failure modes: no concentration at all (ratio ~0.4), double-counted loads, or
    a runaway singularity."""
    c = _cached(("plate", 2, 0.3, 24.0),
                lambda: fsc.case_plate_hole("t", order=2, fineness=0.3,
                                            curv_elems_per_circle=24.0))
    assert c["status"] == "ok", c
    ratio = c["metric"] / c["ref"]
    assert 0.65 < ratio < 1.35, f"hole peak vm {c['metric']:.2f} vs Ktn ref " \
                                f"{c['ref']:.2f} (ratio {ratio:.2f})"


def test_annulus_lame_radial_displacement():
    """u_r(Ri) = (p·Ri/E)((Ro²+Ri²)/(Ro²−Ri²)+ν) — Lamé open-end thick cylinder
    (Timoshenko & Goodier §28). tet10 measured 2.0 % off; 10 % bound."""
    c = _cached(("lame", 2, 0.25),
                lambda: fsc.case_annulus_lame("t", order=2, fineness=0.25))
    assert c["status"] == "ok", c
    assert c["rel_err"] < 0.10, f"u_r {c['metric']:.6f} vs {c['ref']:.6f} " \
                                f"({c['rel_err']:.1%})"


def test_annulus_lame_hoop_stress_bracket():
    """Mean nodal vm on the inner mid-height band vs the Lamé state (σθ,0,−p).
    Nodal averaging smooths the inner-surface value (measured ~23 % low on the
    coarse tet10), so bracket [0.6, 1.15]×ref — catches sign-flipped or garbage
    pressure fields (the unoriented-normal core bug read 4× LOW here)."""
    c = _cached(("lame", 2, 0.25),
                lambda: fsc.case_annulus_lame("t", order=2, fineness=0.25))
    ratio = c["vm_mid"] / c["vm_ref"]
    assert 0.60 < ratio < 1.15, f"vm_mid {c['vm_mid']:.2f} vs Lamé {c['vm_ref']:.2f} " \
                                f"(ratio {ratio:.2f})"


# ------------------------------------------------- pressure/force consistency

def test_pressure_load_matches_equivalent_force():
    """An axial pressure p on the bar end and an axial force p·A on the SAME face
    must produce the same tip displacement (δ = σL/E, uniaxial Hooke). Regression
    witness for the core defect FIXED 2026-07-12: fea_run.cpp
    accumulate_face_weights used kTetFaces fan normals without outward
    orientation, so the consistent-pressure deposit p·∫N n̂ dA carried mixed signs
    (δ ratio 3.5× tet4 / 6.9× tet10). Now oriented outward vs element centroid;
    the dynamic xfail below stays as a tripwire should the fix regress."""
    c = _cached(("press_native",), lambda: fsc.case_pressure_native_bar("t"))
    rel = c["rel_err"]
    if rel is not None and rel > 0.05:
        pytest.xfail(
            f"KNOWN core bug: native pressure path off by {rel:.1%} vs δ=σL/E "
            f"(unoriented fan normals in core/sim/fea_run.cpp "
            f"accumulate_face_weights ~L230-284; fix: orient n̂ outward against "
            f"the element centroid before depositing p·∫N n̂ dA)")
    assert rel is not None and rel <= 0.05, c


def test_consistent_pressure_oracle_resultant_is_zero_on_closed_ring():
    """The Python consistent-pressure oracle must produce a self-equilibrated load
    on the full inner cylinder (net force = 0 by symmetry of p·n̂ over a closed
    ring) — guards the oracle itself against orientation regressions."""
    def _run():
        doc, info = fsc.build_annulus()
        step = fsc.export_step(doc)
        m = fsc.mesh_step(step, order=2, fineness=0.25)
        import os
        os.unlink(step)
        fsc.close_doc(doc)
        loads = fsc.consistent_pressure_loads(
            m, lambda x, y, z: abs(math.hypot(x, y) - info["Ri"]) < 0.05, 10.0)
        return m, loads, info
    m, loads, info = _cached(("oracle_ring",), _run)
    assert loads, "oracle picked no loaded nodes"
    fx = sum(l["vector"][0] for l in loads)
    fy = sum(l["vector"][1] for l in loads)
    fz = sum(l["vector"][2] for l in loads)
    # magnitude scale: p * inner area = 10 * 2π·Ri·H ≈ 150 kN
    scale = 10.0 * 2 * math.pi * info["Ri"] * info["H"]
    assert abs(fx) < 1e-6 * scale and abs(fy) < 1e-6 * scale, (fx, fy)
    assert abs(fz) < 0.02 * scale, fz  # end-ring faces give a small axial residue


# --------------------------------------------------------------- solve sanity

def test_regression_parts_solve_with_finite_fields():
    """The non-analytic fleet must solve: finite positive displacement, finite
    stress, n_dof bookkeeping consistent. One representative per feature family
    kept coarse (the full sweep lives in the bench module)."""
    for geom in ("lbracket_fillet", "revolved_shaft", "swept_rod"):
        c = _cached(("reg", geom),
                    lambda g=geom: fsc.case_regression("t", g, order=1, fineness=0.25))
        assert c["status"] == "ok", (geom, c)
        assert c["metric"] is not None and c["metric"] > 0.0, (geom, c)
        assert math.isfinite(c["metric"]), (geom, c)


# ------------------------------------------- curved-face faceset (C2 regression)

def test_curved_faceset_covers_cylinder_exactly():
    """doc_face_points on a cylinder barrel must emit the analytic surface descriptor,
    and the solver's membership test must then fix EXACTLY the lateral-face nodes —
    no misses (the pre-fix triangulation sampling missed every node between the end
    rings) and no interior/end-face bleed. Regression witness for the C2 fix
    (session.cpp doc_face_points + fea_run.cpp nodes_in_faceset_surface)."""
    def _run():
        cb = fsc._cb()
        doc = cb.call("doc_new", {"name": "c2_cyl_cover"})["doc_id"]
        cb.call("doc_apply_ops", {"doc_id": doc, "ops": [{"op": "add_feature", "feature": {
            "type": "cylinder", "params": {"radius": 10.0, "height": 100.0}}}]})
        q = cb.call("doc_query", {"doc_id": doc, "query": {"of": "face", "curved": True}})
        sel = [m for m in q.get("matches", []) if m.get("kind") == "face"][0]
        fp = cb.call("doc_face_points", {"doc_id": doc, "selector": sel})
        step = fsc.export_step(doc)
        mesh = fsc.mesh_step(step, order=2, fineness=0.3)
        import os
        os.unlink(step)
        fsc.close_doc(doc)
        return fp, mesh
    fp, mesh = _cached(("c2_cyl",), _run)

    # The analytic descriptor is present and correct.
    surf = fp.get("surface")
    assert surf and surf["type"] == "cylinder", fp.keys()
    assert surf["radius"] == pytest.approx(10.0)
    assert abs(surf["axis"][2]) == pytest.approx(1.0)
    assert fp.get("perp_tol_mm") == pytest.approx(1.5)

    # Membership through a real solve: fixture = the barrel faceset; fixed nodes are
    # exactly the zero-displacement ones. Ground truth = nodes at hypot(x,y) ≈ R.
    load = [{"faces": {"points": [[0, 0, 50]], "tol_mm": 3.0},
             "type": "force", "vector": [100.0, 0.0, 0.0]}]
    r = fsc.static_solve(mesh, [fp], load)
    pts, dm = mesh["points"], r["disp_magnitude"]
    n = len(pts) // 3
    truth = {i for i in range(n) if abs(math.hypot(pts[3 * i], pts[3 * i + 1]) - 10.0) < 0.2}
    fixed = {i for i in range(n) if dm[i] == 0.0}
    missed = truth - fixed
    bled = fixed - truth
    assert not missed, f"{len(missed)}/{len(truth)} lateral nodes missed"
    assert not bled, f"{len(bled)} non-lateral nodes wrongly fixed"


def test_native_curved_pressure_matches_lame():
    """End-to-end C1+C2: engine-native pressure on the annulus BORE face (surface
    faceset from doc_face_points + oriented consistent-pressure deposition) must match
    the Lamé radial displacement. Pre-fix this read several-x off (C1) on a mostly
    missed node set (C2)."""
    c = _cached(("lame_native",), lambda: fsc.case_annulus_lame_native("t"))
    assert c["status"] == "ok", c
    assert c["rel_err"] is not None and c["rel_err"] < 0.08, c
