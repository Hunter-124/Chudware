"""Thermal live regression suite — engine steady/transient conduction vs analytic
truths, including the public tool on a meshed 6061 bar.

Layers covered:
  * steady conduction (fea_thermal_solve via sim_run_thermal): slab linear profile,
    pin fin vs 1-D fin theory, singular pure-Neumann rejection;
  * transient conduction (fea_thermal_transient): slab quench vs the 1-D
    Fourier-series decay, lumped-capacitance cooling, duty-schedule differential
    parity against engine_oracle/sim/fea_thermal_transient.py, malformed-schedule
    rejection;
  * public steady tool: real CAD bar, VTU linear temperature field and Fourier flux.

Kernel studies use deterministic inline Kuhn box meshes; the public-tool study
uses the actual part mesher. All engine calls run in-process.

Run:  CHUDWARE_ENGINE_DAEMON=0 .venv-mcp/bin/python -X utf8 -m pytest \
          services/mcp/test_fea_thermal_live.py -q
"""

from __future__ import annotations

import math
import os
import sys

import numpy as np
import pytest

os.environ.setdefault("CHUDWARE_ENGINE_DAEMON", "0")

_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from services.sim.fea_thermal_transient import (  # noqa: E402
    FluxLoad, Schedule, box_mesh, solve_transient)

# ---------------------------------------------------------------------------
# Engine availability probe mirrors test_fea_live_bridge's honest-skip convention.
# ---------------------------------------------------------------------------
_SKIP_REASON = ""
try:
    from services.mcp import core_bridge as cb
    from services.mcp.core_bridge import CoreError

    try:
        cb.call("sim_run_thermal", {"study": {}})
    except CoreError as exc:
        if "unknown method" in str(exc):
            _SKIP_REASON = f"loaded libchudware lacks sim_run_thermal: {exc}"
        # any other CoreError means the RPC exists (it rejected the empty study)
except Exception as exc:  # noqa: BLE001 — no engine at all
    cb = None
    _SKIP_REASON = f"core_bridge unavailable: {exc}"

needs_engine = pytest.mark.skipif(bool(_SKIP_REASON), reason=_SKIP_REASON)


def _faceset(pts: np.ndarray, ids) -> dict:
    """Inline-mesh faceset: the exact node coordinates + a tight tolerance."""
    return {"points": [[float(v) for v in pts[i]] for i in ids], "tol_mm": 1e-6}


def _plane_ids(pts: np.ndarray, axis: int, value: float) -> list:
    return [i for i in range(len(pts)) if abs(pts[i, axis] - value) < 1e-9]


def _study(pts, tets, material, fixtures, loads, transient=None) -> dict:
    st = {"points": pts.flatten().tolist(), "tets": tets.flatten().tolist(),
          "nodes_per_elem": 4, "material": material,
          "fixtures": fixtures, "loads": loads}
    if transient is not None:
        st["transient"] = transient
    return st


# ---------------------------------------------------------------------------
# Steady conduction
# ---------------------------------------------------------------------------

@needs_engine
def test_steady_slab_linear_profile_and_exact_flux():
    # ∇·(k∇T)=0 slab, ends fixed: T(x) = Th + (Tc-Th)·x/L (1-D Fourier conduction).
    # The linear field lies in the tet4 shape space and the BCs are exact, so the
    # FEM solution is exact to round-off — mid-plane at (Th+Tc)/2, profile linear.
    L, Th, Tc = 20.0, 100.0, 0.0
    pts, tets, x0, xL = box_mesh(L, 8.0, 8.0, 8, 2, 2)
    res = cb.call("sim_run_thermal", {"study": _study(
        pts, tets, {"k_w_mk": 50.0},
        [dict(_faceset(pts, x0), temp_c=Th), dict(_faceset(pts, xL), temp_c=Tc)], [])})
    assert res.get("ok"), res.get("error")
    T = np.array(res["temperature"])
    exact = Th + (Tc - Th) * pts[:, 0] / L
    assert float(np.max(np.abs(T - exact))) < 1e-6 * abs(Th - Tc)
    assert abs(res["summary"]["max_temp_c"] - Th) < 1e-6
    assert abs(res["summary"]["min_temp_c"] - Tc) < 1e-6
    # Exact flux: linear profile ⇒ q = k·(Th-Tc)/L (Fourier's law) — implied by the
    # profile check above; the slope itself is pinned via a least-squares fit.
    slope = float(np.polyfit(pts[:, 0], T, 1)[0])
    assert abs(slope - (Tc - Th) / L) < 1e-9 * abs(Th - Tc) / L * 1e3


@needs_engine
def test_steady_fin_matches_1d_fin_theory():
    # Pin fin: base at Tb, four lateral films; adiabatic tip. 1-D fin theory
    # (Incropera Table 3.4 case B): θ(L)/θ_b = 1/cosh(mL), m = √(hP/(kA)).
    # Transverse Biot = h(b/2)/k = 2.5e-3 << 0.1, so the 1-D model is the truth.
    Lf, bf, Tb, Tamb = 100.0, 10.0, 100.0, 25.0
    pts, tets, x0, xL = box_mesh(Lf, bf, bf, 25, 2, 2)
    loads = []
    for ax in (1, 2):
        for v in (0.0, bf):
            loads.append({"faces": _faceset(pts, _plane_ids(pts, ax, v)),
                          "type": "convection", "h": 100.0, "t_amb_c": Tamb})
    res = cb.call("sim_run_thermal", {"study": _study(
        pts, tets, {"k_w_mk": 200.0},
        [dict(_faceset(pts, x0), temp_c=Tb)], loads)})
    assert res.get("ok"), res.get("error")
    T = np.array(res["temperature"])
    m = math.sqrt((1e-4) * 4.0 * bf / (0.2 * bf * bf))   # mm units: h/1e6·P/(k/1e3·A)
    tip_ref = Tamb + (Tb - Tamb) / math.cosh(m * Lf)
    tip_got = float(np.mean(T[xL]))
    assert abs(tip_got - tip_ref) / abs(tip_ref - Tamb) < 0.01, \
        f"fin tip {tip_got} vs 1D theory {tip_ref}"


@needs_engine
def test_steady_pure_neumann_is_rejected_not_garbage():
    # No Dirichlet node and no film ⇒ no temperature datum: the solver must refuse
    # (singularity guard), not return an arbitrary field.
    pts, tets, _, _ = box_mesh(20.0, 8.0, 8.0, 4, 2, 2)
    res = cb.call("sim_run_thermal", {"study": _study(
        pts, tets, {"k_w_mk": 50.0}, [], [])})
    assert res.get("ok") is False
    assert "singular" in res.get("error", "").lower()


# ---------------------------------------------------------------------------
# Transient conduction
# ---------------------------------------------------------------------------

@needs_engine
def test_transient_slab_quench_matches_fourier_series():
    # Slab at T0, both faces quenched to 0 at t=0; mid-plane at Fo = αt/L² = 0.3 vs
    # the odd-term Fourier series  T = Σ 4T0/(kπ)·sin(kπx/L)·exp(-k²π²·Fo)
    # (1-D heat equation separation of variables; same anchor as
    # core/tests/test_thermal.cpp and the oracle suite).
    L, T0 = 20.0, 100.0
    k_w_mk, cp, rho_g_cm3 = 50.0, 500.0, 8.0        # α = k/(ρcp) = 12.5 mm²/s
    alpha = (k_w_mk / 1000.0) / (rho_g_cm3 * 1000.0 * cp / 1e9)
    t_star = 0.3 * L * L / alpha
    pts, tets, x0, xL = box_mesh(L, 8.0, 8.0, 20, 2, 2)
    res = cb.call("sim_run_thermal", {"study": _study(
        pts, tets, {"k_w_mk": k_w_mk, "cp_j_kgk": cp, "density_g_cm3": rho_g_cm3},
        [dict(_faceset(pts, x0), temp_c=0.0), dict(_faceset(pts, xL), temp_c=0.0)],
        [], transient={"t_end_s": t_star, "dt_s": t_star / 400.0, "t_init_c": T0})})
    assert res.get("ok"), res.get("error")
    T = np.array(res["frames"][-1]["temperature"])
    mid = _plane_ids(pts, 0, L / 2.0)
    got = float(np.mean(T[mid]))
    ref = sum(4.0 * T0 / (n * math.pi) * math.sin(n * math.pi / 2.0)
              * math.exp(-alpha * n * n * math.pi * math.pi * t_star / (L * L))
              for n in range(1, 200, 2))
    assert abs(got - ref) / ref < 0.03, f"mid-plane {got} vs series {ref}"
    # Monotone decay ⇒ the run peak is the initial condition at t=0.
    assert abs(res["summary"]["max_temp_c"] - T0) < 1e-6
    assert res["summary"]["max_temp_time_s"] == 0.0


@needs_engine
def test_transient_lumped_capacitance_cooling():
    # 10 mm aluminium-grade cube, film on all six faces, Bi = h(V/A)/k ≈ 1.7e-3
    # << 0.1 ⇒ lumped capacitance is the truth:  θ/θ0 = exp(-t/τ), τ = ρcpV/(hA)
    # (Incropera §5.1). At t = τ the body sits at Tamb + θ0/e.
    Lb, h, Tamb, T0 = 10.0, 200.0, 20.0, 100.0
    rho, cp = 2700.0, 900.0
    pts, tets, _, _ = box_mesh(Lb, Lb, Lb, 3, 3, 3)
    loads = [{"faces": _faceset(pts, _plane_ids(pts, ax, v)),
              "type": "convection", "h": h, "t_amb_c": Tamb}
             for ax in (0, 1, 2) for v in (0.0, Lb)]
    V, A = Lb ** 3 * 1e-9, 6.0 * Lb * Lb * 1e-6      # m³, m²
    tau = rho * cp * V / (h * A)
    res = cb.call("sim_run_thermal", {"study": _study(
        pts, tets, {"k_w_mk": 200.0, "cp_j_kgk": cp, "density_g_cm3": rho / 1000.0},
        [], loads, transient={"t_end_s": tau, "dt_s": tau / 400.0, "t_init_c": T0})})
    assert res.get("ok"), res.get("error")
    got = max(res["frames"][-1]["temperature"])
    ref = Tamb + (T0 - Tamb) * math.exp(-1.0)
    assert abs(got - ref) / abs(ref - Tamb) < 0.02, f"lumped {got} vs {ref}"


@needs_engine
def test_transient_duty_schedule_parity_with_python_oracle():
    # Differential parity: identical mesh/BCs/schedule through BOTH stacks — the
    # duty shorthand {duty, period_s} on the engine side must reproduce the oracle's
    # Schedule([0, d·p], [1, 0], period=p) field to round-off (same backward Euler).
    L, Tc, q_mm2, duty = 30.0, 40.0, 0.01, 0.5
    k_w_mk, cp, rho_g_cm3 = 20.0, 500.0, 8.0
    k_mm, rho_cp = k_w_mk / 1000.0, rho_g_cm3 * 1000.0 * cp / 1e9
    pts, tets, x0, xL = box_mesh(L, 8.0, 8.0, 12, 2, 2)
    tau = rho_cp * L * L / k_mm
    period, t_end, dt = tau / 5.0, 3.0 * tau, tau / 200.0
    r_o = solve_transient(
        pts, tets, k_mm, xL, [Tc] * len(xL),
        [FluxLoad(nodes=x0, q=q_mm2,
                  schedule=Schedule(times=[0.0, duty * period], scales=[1.0, 0.0],
                                    period=period))],
        [], rho_cp, t_end=t_end, dt=dt, t_init=Tc)
    assert r_o.ok, r_o.error
    res = cb.call("sim_run_thermal", {"study": _study(
        pts, tets, {"k_w_mk": k_w_mk, "cp_j_kgk": cp, "density_g_cm3": rho_g_cm3},
        [dict(_faceset(pts, xL), temp_c=Tc)],
        [{"faces": _faceset(pts, x0), "type": "flux", "magnitude": q_mm2 * 1e6,
          "schedule": {"duty": duty, "period_s": period}}],
        transient={"t_end_s": t_end, "dt_s": dt, "t_init_c": Tc})})
    assert res.get("ok"), res.get("error")
    T_engine = np.array(res["frames"][-1]["temperature"])
    T_oracle = r_o.frames[-1][1]
    assert float(np.max(np.abs(T_engine - T_oracle))) < 1e-8
    assert abs(res["summary"]["max_temp_c"] - r_o.max_temp) < 1e-3  # rounded to 4dp


@needs_engine
def test_transient_malformed_schedule_is_rejected():
    # Kernel contract: schedule times must be ascending AND start at 0 — a piecewise
    # schedule starting at t=1 must be refused, not silently shifted.
    pts, tets, x0, xL = box_mesh(20.0, 8.0, 8.0, 4, 2, 2)
    res = cb.call("sim_run_thermal", {"study": _study(
        pts, tets, {"k_w_mk": 50.0, "cp_j_kgk": 500.0, "density_g_cm3": 8.0},
        [dict(_faceset(pts, xL), temp_c=20.0)],
        [{"faces": _faceset(pts, x0), "type": "flux", "magnitude": 1e4,
          "schedule": {"points": [[1.0, 1.0]], "period_s": 2.0}}],
        transient={"t_end_s": 2.0, "dt_s": 0.1, "t_init_c": 20.0})})
    assert res.get("ok") is False
    assert "schedule" in res.get("error", "").lower()


@needs_engine
def test_public_steady_6061_bar_temperature_and_flux(tmp_path):
    import json
    import xml.etree.ElementTree as ET
    from services.agent.agent_turn import _bind_tool

    created = json.loads(_bind_tool("new_part")(name="thermal_6061_bar", fresh=True))
    assert created["ok"], created
    doc = created["doc_id"]
    added = json.loads(_bind_tool("add_features")(doc_id=doc, ops=[
        {"op": "add_feature", "feature": {"type": "box", "name": "Bar",
         "params": {"length": 20, "width": 8, "height": 8}}}]))
    assert added["ok"], added
    path = tmp_path / "steady.vtu"
    from services.mcp.test_fea_advanced import _cad_tools
    result = json.loads(_cad_tools()["run_fea_thermal"](
        hot_faces=[{"face": "-x", "temperature": 100}],
        cold_faces=[{"face": "+x", "temperature": 0}],
        material={"name": "6061-T6", "conductivity_w_mk": 167.0},
        mesh_fineness="coarse", doc_id=doc,
        vtu_path=str(path)))
    assert result["ok"], result
    assert result["provenance"]["solver"] == "engine"
    assert result["mesh_quality"]["n_nodes"] > 0
    assert result["temperature_max_c"] == pytest.approx(100, abs=1e-4)
    assert result["temperature_min_c"] == pytest.approx(0, abs=1e-4)
    assert result["flux_max_w_m2"] == pytest.approx(
        result["conductivity_w_mk"] * 100 / 0.020, rel=0.03)
    root = ET.parse(path).getroot()
    points = np.fromstring(root.find(".//Points/DataArray").text, sep=" ").reshape(-1, 3)
    field = np.fromstring(root.find(".//PointData/DataArray[@Name='temperature']").text, sep=" ")
    x = points[:, 0]
    expected = 100 * (x.max() - x) / (x.max() - x.min())
    assert np.max(np.abs(field - expected)) < 1.0
    mid = (x.max() + x.min()) / 2
    assert np.polyval(np.polyfit(x, field, 1), mid) == pytest.approx(50, abs=1)
    assert "frames" not in result


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
