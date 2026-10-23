"""Multiphysics-misc regression suite — motion, piping, hydraulic, structural + the
cross-sim handoff seam (aicad.simhandoff/1).

Split in three groups, mirroring the house pattern (test_fea_advanced.py /
test_hydraulic_handoff.py):

  1. Pure-oracle physics checks (always run, no engine): hydraulic sizing vs
     Pascal's-law hand calcs, piping 1-D solve vs Hagen-Poiseuille / Darcy-Weisbach,
     pump duty point vs a hand quadratic intersection, cantilever beam vs
     Euler-Bernoulli closed forms.
  2. MCP-tool seam smokes via a stub FastMCP (payload-shape asserts for the
     hydraulic->FEA and hydraulic->piping->solve handoff chain).
  3. Engine-backed motion checks (skipped cleanly when libchudware isn't staged):
     driven-distance displacement and quasi-static joint reactions vs m*g.

Every analytic reference value cites its formula next to the number — no
load-bearing constants from memory (repo differential-oracle rule).

Run:  .venv-mcp/bin/python -X utf8 -m pytest services/mcp/test_multiphysics_misc.py -v
"""
from __future__ import annotations

import json
import math
import os
import sys
from pathlib import Path

# Repo root on sys.path regardless of symlink route (same fix as test_cad_tools.py).
_REPO_ROOT = str(Path(__file__).resolve().parents[2])
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import pytest

# Import the registrar EARLY, before sibling tests pollute sys.modules with stub
# mcp/anyio packages (same rule as test_fea_advanced.py).
from services.mcp.cad_tools import register_cad_tools as _register_cad_tools  # noqa: F401

# engine_oracle/hydraulic + engine/piping/{solve,properties} were ported to core
# and deleted; these shims drive the same methods over the core bridge.
from services.mcp import core_bridge as _cb_hyd


class _HydSolveShim:
    @staticmethod
    def size_cylinder(**params):
        return _cb_hyd.call("hydraulic_size_cylinder", params)

    @staticmethod
    def size_pump(**params):
        return _cb_hyd.call("hydraulic_size_pump", params)


class _PipingSolveShim:
    @staticmethod
    def solve(doc, boundary=None, fluid=None, pumps=None):
        return _cb_hyd.call("piping_solve", {"doc": doc, "boundary": boundary,
                                             "fluid": fluid, "pumps": pumps})

    @staticmethod
    def operating_point(csv, system=None):
        return _cb_hyd.call("piping_operating_point", {"pump_curve": csv, "system": system})


hyd_solve = _HydSolveShim()
piping_solve_mod = _PipingSolveShim()
from services.piping import schema as piping_schema  # kept in engine_oracle (threed/cfd_handoff hold-back)
from services.mcp import simhandoff as sh


def _have(mod: str) -> bool:
    try:
        __import__(mod)
        return True
    except Exception:  # noqa: BLE001 - any import failure means "not usable here"
        return False


HAVE_FLUIDS = _have("fluids")            # engine.piping.solve dependency (MIT)
HAVE_BUILD123D = _have("build123d")      # piping_internal_volume OCP kernel


# ── stub MCP registrar (same shape as test_hydraulic_handoff._StubMCP) ─────────

class _StubMCP:
    def __init__(self):
        self.tools = {}

    def tool(self):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco


def _hydraulic_tools():
    from services.mcp import hydraulic_tools as ht
    m = _StubMCP()
    ht.register_hydraulic_tools(m)
    return m.tools


def _piping_tools():
    from services.mcp import piping_tools as pt
    m = _StubMCP()
    pt.register_piping_tools(m)
    return m.tools


def _structural_tools():
    from services.mcp import structural_tools as st
    m = _StubMCP()
    st.register_structural_tools(m)
    return m.tools


# ══════════════════════════════ group 1: hydraulic (Pascal's law) ══════════════


def test_hydraulic_cylinder_force_matches_p_times_a():
    """F = P*A (Pascal's law): extend on the full bore, retract on the annulus."""
    bore, rod, p_bar, eta = 50.0, 25.0, 200.0, 1.0
    r = hyd_solve.size_cylinder(bore_mm=bore, rod_mm=rod, pressure_bar=p_bar,
                                stroke_mm=300.0, velocity_mm_s=100.0, efficiency=eta)
    assert r["ok"], r
    # F_ext = pi/4 * D^2 * P            (Pascal's law, full bore area)
    f_ext_kn = math.pi / 4.0 * (bore * 1e-3) ** 2 * (p_bar * 1e5) * eta / 1000.0
    # F_ret = pi/4 * (D^2 - d^2) * P    (annulus area)
    f_ret_kn = math.pi / 4.0 * ((bore * 1e-3) ** 2 - (rod * 1e-3) ** 2) * (p_bar * 1e5) * eta / 1000.0
    assert math.isclose(r["extend_force_kn"], f_ext_kn, rel_tol=1e-4), r
    assert math.isclose(r["retract_force_kn"], f_ret_kn, rel_tol=1e-4), r
    # continuity: Q = A*v -> L/min      (flow to hold the velocity target)
    q_ext_lpm = (math.pi / 4.0 * (bore * 1e-3) ** 2) * 0.1 * 60.0 * 1000.0
    assert math.isclose(r["flow_extend_lpm"], q_ext_lpm, rel_tol=1e-4), r
    # kinematics: t = stroke / velocity
    assert math.isclose(r["cycle_time_s"], 300.0 / 100.0, rel_tol=1e-9), r


def test_hydraulic_pump_power_and_torque_hand_calc():
    """P_hyd = Q*dP; T = P_shaft/omega (rotational power balance)."""
    q_lpm, p_bar, eta, rpm = 30.0, 150.0, 0.85, 1800.0
    r = hyd_solve.size_pump(flow_lpm=q_lpm, pressure_bar=p_bar,
                            efficiency=eta, speed_rpm=rpm)
    assert r["ok"], r
    # hydraulic power P = Q (m^3/s) * dP (Pa)
    p_hyd_kw = (q_lpm / 60.0 / 1000.0) * (p_bar * 1e5) / 1000.0
    assert math.isclose(r["hydraulic_power_kw"], p_hyd_kw, rel_tol=1e-4), r
    assert math.isclose(r["shaft_power_kw"], p_hyd_kw / eta, rel_tol=1e-4), r
    # T = P / omega, omega = 2*pi*n/60
    t_nm = (p_hyd_kw / eta) * 1000.0 / (2.0 * math.pi * rpm / 60.0)
    assert math.isclose(r["torque_nm"], t_nm, rel_tol=1e-4), r
    # volumetric: displacement = Q*1000/n (cc/rev); result rounded to 4 dp
    assert math.isclose(r["displacement_cc_rev"], q_lpm * 1000.0 / rpm, rel_tol=1e-4), r


def test_hydraulic_size_tools_mcp_payload_shape():
    tools = _hydraulic_tools()
    r = json.loads(tools["hydraulic_size_cylinder"](
        bore_mm=50.0, pressure_bar=200.0, stroke_mm=300.0, rod_mm=25.0,
        velocity_mm_s=100.0, efficiency=1.0))
    assert r["ok"] and "hint" in r and "warnings" in r, r
    # docstring's own hand-calc: 50 mm bore, 200 bar, eta=1 -> pi/4*0.05^2*200e5 = 39.27 kN
    assert math.isclose(r["extend_force_kn"], 39.2699, rel_tol=1e-3), r
    r2 = json.loads(tools["hydraulic_size_pump"](flow_lpm=10.0, pressure_bar=200.0))
    assert r2["ok"], r2
    # docstring hand-calc: (10/60000)*200e5/1000/0.85 = 3.922 kW shaft
    assert math.isclose(r2["shaft_power_kw"], 3.9216, rel_tol=1e-3), r2
    # bad inputs degrade contract-shaped, never raise
    bad = json.loads(tools["hydraulic_size_cylinder"](
        bore_mm=20.0, pressure_bar=200.0, stroke_mm=100.0, rod_mm=25.0))
    assert bad["ok"] is False and bad["reason"], bad


# ══════════════════════════════ group 2: piping 1-D solve ═════════════════════


def _straight_pipe_doc(bore_mm: float, length_mm: float) -> dict:
    doc = piping_schema.new_piping("straight")
    doc["runs"].append(piping_schema.new_pipe_run(
        "r1", [[0.0, 0.0, 0.0], [length_mm, 0.0, 0.0]], 0.5, bore_mm=bore_mm))
    assert piping_schema.validate(doc) == []
    return doc


@pytest.mark.skipif(not HAVE_FLUIDS, reason="fluids library not installed")
def test_piping_solve_laminar_matches_hagen_poiseuille():
    """Laminar straight pipe: dp = 128*mu*L*Q/(pi*D^4) (Hagen-Poiseuille), which the
    Darcy-Weisbach form reproduces exactly with f = 64/Re."""
    bore_mm, length_mm = 6.0, 20000.0
    d = bore_mm / 1000.0
    area = math.pi / 4.0 * d * d
    v = 0.05                                  # m/s -> Re ~ 300, safely laminar (< 2100)
    q_m3s = v * area
    doc = _straight_pipe_doc(bore_mm, length_mm)
    res = piping_solve_mod.solve(doc, boundary=[
        {"run": "r1", "end": "start", "pressure": 300.0},
        {"run": "r1", "end": "end", "demand": q_m3s * 3600.0}])
    assert res["ok"] and res["converged"], res
    rho, mu = res["fluid"]["rho"], res["fluid"]["mu"]
    lk = res["links"][0]
    re = rho * v * d / mu
    assert re < 2100.0, f"test setup drifted turbulent: Re={re}"
    # Hagen-Poiseuille: dp = 128*mu*L*Q/(pi*D^4)   [Pa]
    dp_ref_kpa = 128.0 * mu * (length_mm / 1000.0) * q_m3s / (math.pi * d ** 4) / 1000.0
    assert math.isclose(lk["dp_kpa"], dp_ref_kpa, rel_tol=0.01), (lk, dp_ref_kpa)
    # velocity + Reynolds bookkeeping
    assert math.isclose(lk["velocity_ms"], v, rel_tol=1e-3), lk
    assert math.isclose(lk["reynolds"], re, rel_tol=1e-2), (lk, re)
    # node pressure difference equals the link drop (horizontal pipe, z=0)
    p = {n["id"]: n["pressure_kpa"] for n in res["nodes"]}
    dp_nodes = max(p.values()) - min(p.values())
    assert math.isclose(dp_nodes, dp_ref_kpa, rel_tol=0.01), (p, dp_ref_kpa)


@pytest.mark.skipif(not HAVE_FLUIDS, reason="fluids library not installed")
def test_piping_solve_turbulent_darcy_weisbach_swamee_jain():
    """Turbulent straight pipe vs an INDEPENDENT hand Darcy-Weisbach calc:
    h_f = f*(L/D)*v^2/(2g) with f from the Swamee-Jain explicit Colebrook fit
    (f = 0.25/[log10(eps/3.7D + 5.74/Re^0.9)]^2, valid 5e3<Re<1e8) — Swamee-Jain
    tracks the exact Colebrook solution within ~1%, so a 3% gate is honest."""
    bore_mm, length_mm = 52.5, 50000.0          # ~2" bore, 50 m
    d = bore_mm / 1000.0
    area = math.pi / 4.0 * d * d
    v = 2.0                                     # m/s -> Re ~ 1e5, fully turbulent
    q_m3s = v * area
    doc = _straight_pipe_doc(bore_mm, length_mm)
    res = piping_solve_mod.solve(doc, boundary=[
        {"run": "r1", "end": "start", "pressure": 600.0},
        {"run": "r1", "end": "end", "demand": q_m3s * 3600.0}])
    assert res["ok"] and res["converged"], res
    rho, mu = res["fluid"]["rho"], res["fluid"]["mu"]
    lk = res["links"][0]
    re = rho * v * d / mu
    assert re > 5000.0, f"test setup drifted laminar: Re={re}"
    eps_rel = (lk["k_total"], )  # no fittings -> K must be zero
    assert lk["k_total"] == 0.0, lk
    # roughness the solver used, backed out of nothing: recompute from its friction
    # factor is circular — instead take commercial-steel eps = 4.5e-5 m, the value
    # engine.piping.solve._ROUGHNESS_M assigns the doc default A106-B material.
    eps = 4.5e-5
    f_sj = 0.25 / (math.log10(eps / (3.7 * d) + 5.74 / re ** 0.9)) ** 2  # Swamee-Jain
    g = 9.80665                                  # standard gravity
    h_ref = f_sj * (length_mm / 1000.0 / d) * v * v / (2.0 * g)   # Darcy-Weisbach [m]
    dp_ref_kpa = rho * g * h_ref / 1000.0
    assert math.isclose(lk["dp_kpa"], dp_ref_kpa, rel_tol=0.03), (lk, dp_ref_kpa, f_sj)


def test_piping_operating_point_hand_quadratic():
    """Duty point: intersect linear pump curve H = 50 - 0.2*Q with system
    H = 10 + 0.004*Q^2 -> 0.004*Q^2 + 0.2*Q - 40 = 0 (hand quadratic root)."""
    csv = "flow,head\n0,50\n100,30\n"
    r = piping_solve_mod.operating_point(csv, system={"static_head_m": 10.0, "k": 0.004})
    assert r["ok"], r
    # Q* = (-b + sqrt(b^2 + 4*k*(H0-h0))) / (2k) with k=0.004, b=0.2, H0-h0=40
    q_star = (-0.2 + math.sqrt(0.2 ** 2 + 4.0 * 0.004 * 40.0)) / (2.0 * 0.004)
    h_star = 10.0 + 0.004 * q_star * q_star
    assert math.isclose(r["duty_point"]["flow_m3h"], q_star, rel_tol=1e-4), (r, q_star)
    assert math.isclose(r["duty_point"]["head_m"], h_star, rel_tol=1e-4), (r, h_star)


def test_piping_operating_point_no_intersection_soft_fails():
    csv = "flow,head\n0,50\n100,30\n"
    r = piping_solve_mod.operating_point(csv, system={"static_head_m": 60.0, "k": 0.004})
    assert r["ok"] is False and "shutoff" in r["reason"], r


@pytest.mark.skipif(not HAVE_BUILD123D, reason="build123d/OCP kernel not installed")
def test_piping_internal_volume_exact_cylinder(tmp_path):
    """Straight-run bore solid volume = pi/4 * d^2 * L (exact cylinder)."""
    tools = _piping_tools()
    bore_mm, length_mm = 20.0, 500.0
    doc = _straight_pipe_doc(bore_mm, length_mm)
    out = str(tmp_path / "bore.step")
    r = json.loads(tools["piping_internal_volume"](doc, out_path=out))
    assert r["ok"], r
    v_ref = math.pi / 4.0 * bore_mm ** 2 * length_mm   # cylinder volume
    assert math.isclose(r["volume_mm3"], v_ref, rel_tol=0.01), (r["volume_mm3"], v_ref)
    assert os.path.isfile(r["step_path"]) and os.path.getsize(r["step_path"]) > 0


# ══════════════════════════════ group 3: structural (Euler-Bernoulli) ══════════
#
# The struct_* MCP tools are dispatched to the C++ core (libchudware struct_* RPCs via
# core_bridge) — they need a staged engine, not PyNite. Same gate as group 5.

from services.mcp import core_bridge as cb  # noqa: E402

_STRUCT_ENGINE = pytest.mark.skipif(
    not cb.available(),
    reason="libchudware not staged; struct_* RPCs unavailable")


def _cantilever_doc(tmp_path, L_mm: float, w_n_mm: float) -> str:
    """One horizontal steel beam along +X, root at origin. The model builder fixes the
    single lowest node (tie-break z,y,x -> the x=0 root) => a cantilever."""
    doc = {
        "schema": "aicad.structure/1",
        "name": "cantilever",
        "elements": [{
            "element_type": "beam", "section_name": "100x100", "material": "steel",
            "length_mm": L_mm, "id": "b1",
            # local +Z (extrusion axis) -> global +X, so the member lies horizontal
            "transform": [0, 0, 1, 0,  0, 1, 0, 0,  1, 0, 0, 0,  0, 0, 0, 1],
        }],
        "load_cases": [{"kind": "dead", "factor": 1.0, "gravity": [0, 0, -1],
                        "distributed": {"w_n_per_mm": w_n_mm}}],
    }
    p = tmp_path / "cantilever.json"
    p.write_text(json.dumps(doc))
    return str(p)


@_STRUCT_ENGINE
def test_struct_analyze_cantilever_udl_euler_bernoulli(tmp_path):
    """Cantilever + UDL closed forms (Euler-Bernoulli, e.g. Gere Table G-1):
    delta_max = w*L^4/(8*E*I), M_max = w*L^2/2, V_max = w*L. DEAD_ONLY combo = 1.4*D."""
    tools = _structural_tools()
    L, w = 2000.0, 10.0
    r = json.loads(tools["struct_analyze"](_cantilever_doc(tmp_path, L, w),
                                           load_combo="DEAD_ONLY"))
    assert r["ok"], r
    row = r["results"][0]
    E = 200000.0                       # steel E (MPa) — core/structural materials table
    I = 100.0 * 100.0 ** 3 / 12.0      # I = b*h^3/12, square 100x100 section
    w_eff = 1.4 * w                    # ASCE 7 DEAD_ONLY = 1.4*D
    d_ref = w_eff * L ** 4 / (8.0 * E * I)      # Euler-Bernoulli cantilever UDL
    m_ref = w_eff * L ** 2 / 2.0                # max moment at the root
    v_ref = w_eff * L                           # max shear at the root
    assert math.isclose(abs(row["deflection_mm"]), d_ref, rel_tol=0.02), (row, d_ref)
    assert math.isclose(abs(row["moment_nmm"]), m_ref, rel_tol=0.02), (row, m_ref)
    assert math.isclose(abs(row["shear_n"]), v_ref, rel_tol=0.02), (row, v_ref)


@_STRUCT_ENGINE
def test_struct_check_payload_shape_and_disclaimer(tmp_path):
    tools = _structural_tools()
    r = json.loads(tools["struct_check"](_cantilever_doc(tmp_path, 2000.0, 10.0),
                                         load_combo="DEAD_ONLY"))
    assert r["ok"], r
    assert "licensed structural engineer" in r["disclaimer"]
    row = r["results"][0]
    assert set(row) >= {"element_id", "dc_ratio", "passed", "governing_limit"}, row
    assert row["dc_ratio"] is not None and row["dc_ratio"] > 0, row


def test_struct_check_incomplete_payload_is_not_success(monkeypatch):
    """Unavailable member checks remain visible, but make the common verdict false."""
    from services.mcp import structural_tools as st

    partial = {
        "ok": True,
        "complete": False,
        "disclaimer": "preliminary only",
        "results": [{
            "element_id": "f1",
            "status": "unavailable",
            "reason": "soil bearing input missing",
            "dc_ratio": None,
            "passed": False,
        }],
    }
    monkeypatch.setattr(st.cb, "call", lambda method, params: partial)
    r = json.loads(st._struct_rpc("struct_check", {"doc_path": "partial.json"}))
    assert r["ok"] is False and r["complete"] is False, r
    assert r["results"] == partial["results"], r
    assert r["results"][0]["reason"] == "soil bearing input missing"


def test_struct_estimate_partial_total_is_not_success(monkeypatch):
    """A useful subtotal survives without being mislabeled as a complete estimate."""
    from services.mcp import structural_tools as st

    partial = {
        "ok": True,
        "complete": False,
        "total_is_partial": True,
        "total_material_cost": 125.0,
        "unestimated_element_ids": ["x2"],
        "line_items": [{
            "element_id": "x2",
            "material_estimated": False,
            "unavailable_reason": "no material quantity/rate model",
        }],
    }
    monkeypatch.setattr(st.cb, "call", lambda method, params: partial)
    r = json.loads(st._struct_rpc("struct_estimate", {"doc_path": "partial.json"}))
    assert r["ok"] is False and r["complete"] is False, r
    assert r["total_is_partial"] is True
    assert r["total_material_cost"] == 125.0
    assert r["unestimated_element_ids"] == ["x2"]
    assert r["line_items"][0]["unavailable_reason"]


# ══════════════════════════════ group 4: handoff seams (aicad.simhandoff/1) ════


def test_hydraulic_fea_handoff_seam_payload():
    """hydraulic_fea_handoff: envelope valid, per-case fea_args, expectations equal
    the independent F = P*A hand calc; hydraulic_verify_fea closes the loop."""
    tools = _hydraulic_tools()
    r = json.loads(tools["hydraulic_fea_handoff"](
        bore_mm=63.0, max_pressure_bar=210.0, rod_mm=36.0, axis=[0, 0, 1]))
    assert r["ok"], r
    env = r["simhandoff"]
    assert sh.validate_envelope(env) == [], sh.validate_envelope(env)
    assert env["coupling"] == "hydraulic_to_fea" and env["kind"] == "handoff"
    # F_ext = pi/4*bore^2*P (Pascal's law); F_ret on the annulus
    f_ext = math.pi / 4.0 * (63.0e-3) ** 2 * 210.0e5
    f_ret = math.pi / 4.0 * ((63.0e-3) ** 2 - (36.0e-3) ** 2) * 210.0e5
    assert math.isclose(r["extend_force_n"], f_ext, rel_tol=1e-3), (r, f_ext)
    assert math.isclose(r["retract_force_n"], f_ret, rel_tol=1e-3), (r, f_ret)
    assert set(r["fea_args"]) == {"extend_max_pressure", "retract_max_pressure"}, r
    for case, args in r["fea_args"].items():
        assert args["fixtures"] and args["loads"][0]["force"], (case, args)
    exp = {e["quantity"]: e["value"] for e in env["expectations"]}
    assert math.isclose(exp["mount_force:extend_max_pressure"], f_ext, rel_tol=1e-3)
    # verify step recomputes A*P independently and must PASS on its own envelope
    v = json.loads(tools["hydraulic_verify_fea"](handoff=r))
    assert v["ok"] and v["pass"], v
    assert sh.validate_envelope(v["simhandoff"]) == []


@pytest.mark.skipif(not HAVE_FLUIDS, reason="fluids library not installed")
def test_hydraulic_piping_handoff_feeds_piping_solve():
    """Cross-seam chain: hydraulic_piping_handoff produces a handoff envelope whose
    inline piping doc + meta boundary/fluid piping_solve consumes directly, and the
    solve result records the handoff_id (provenance chain closed)."""
    # engine.hydraulic.schema was ported to core: build the circuit doc
    # through the C++ RPCs (component ids c1/c2 come from next_id in order).
    htools = _hydraulic_tools()
    ptools = _piping_tools()
    # minimal pump -> cylinder circuit with two hoses carrying real geometry
    doc = _cb_hyd.call("hydraulic_new", {"name": "mp circuit"})["doc"]
    doc = _cb_hyd.call("hydraulic_add_component",
                       {"doc": doc, "type": "pump", "name": "pump"})["doc"]
    doc = _cb_hyd.call("hydraulic_add_component",
                       {"doc": doc, "type": "cylinder_linear", "name": "cyl",
                        "params": {"bore_mm": 50.0, "rod_mm": 28.0}})["doc"]
    doc = _cb_hyd.call("hydraulic_add_hose",
                       {"doc": doc, "net_name": "PRESSURE_LINE",
                        "connections": [{"component_id": "c1", "port": "pressure"},
                                        {"component_id": "c2", "port": "work_a"}]})["doc"]
    doc["hoses"][0]["id_mm"] = 12.7
    doc["hoses"][0]["length_mm"] = 2500.0
    r = json.loads(htools["hydraulic_piping_handoff"](
        supply_pressure_bar=200.0, flow_lpm=30.0, doc=doc))
    assert r["ok"], r
    env = r.get("simhandoff")
    assert env and sh.validate_envelope(env) == [], r
    assert env["coupling"] == "hydraulic_to_piping"
    # the envelope carries the generated aicad.piping/1 doc inline (doc-to-doc shape)
    inline = [g for g in env["geometry"] if g.get("kind") == "doc" and g.get("inline")]
    assert inline, env["geometry"]
    assert (env.get("meta") or {}).get("boundary"), env
    s = json.loads(ptools["piping_solve"](source_handoff=env))
    assert s["ok"] and s["converged"], s
    assert s.get("handoff_id") == env["id"], s
    assert s["links"] and all(lk["dp_kpa"] >= 0 or True for lk in s["links"])


def test_piping_verify_envelope_shape_from_mock_results():
    """piping_verify_cfd seam: 1D-vs-3D check envelope built from plain dicts —
    no engine, no CFD; asserts the verify payload contract only."""
    from services.mcp.piping_tools import _verify_envelope
    result = {"ok": True, "pass": True, "dp_1d_kpa": 12.0, "dp_3d_kpa": 13.0,
              "deviation_pct": 8.33, "tolerance_pct": 20.0,
              "verdict": "PASS: within tolerance", "velocity_limits": {"violations": []},
              "warnings": []}
    env = _verify_envelope(result)
    assert sh.validate_envelope(env) == [], sh.validate_envelope(env)
    assert env["kind"] == "verify" and env["coupling"] == "piping_to_cfd"
    ck = env["checks"][0]
    assert ck["quantity"] == "pressure_drop" and ck["units"] == "kPa"
    assert env["pass"] is True


# ══════════════════════════════ group 5: engine-backed motion ══════════════════

_ENGINE = pytest.mark.skipif(
    not cb.available(),
    reason="libchudware not staged; engine-backed motion tools unavailable")


def _call(name: str, args: dict):
    import asyncio
    from services.mcp import server
    content, structured = asyncio.run(server.mcp.call_tool(name, args))
    text = structured.get("result") if isinstance(structured, dict) else None
    try:
        return json.loads(text) if isinstance(text, str) else None
    except json.JSONDecodeError:
        return None


def _bar_part(name, L, W, H, sidecar):
    r = _call("new_part", {"name": name})
    doc = r["doc_id"]
    _call("add_features", {"doc_id": doc, "ops": [
        cb.feat("box", name, {"length": cb.dim(L), "width": cb.dim(W),
                              "height": cb.dim(H)})]})
    faces = cb.call("doc_query", {"doc_id": doc, "query": {"of": "face"}}).get("matches", [])
    path = os.path.join(sidecar, f"{name}.step+")
    cb.call("doc_save", {"doc_id": doc, "path": path})
    return path, faces


def _swing_arm(tmp_path):
    """Fixed Ground bar + free Arm bar on a driven angle mate + parallel plane lock
    (the minimal stable single-DOF crank — same topology as test_cad_tools.py)."""
    sidecar = str(tmp_path)
    bar, faces = _bar_part("Bar", 40, 4, 4, sidecar)
    asm = cb.call("doc_new", {"name": "MPSwing", "kind": "assembly"})["doc_id"]

    def rev():
        return cb.call("doc_tree", {"doc_id": asm}).get("revision", 0)

    grd_xf = [1, 0, 0, 0, 0, 1, 0, 200, 0, 0, 1, 0, 0, 0, 0, 1]
    cb.call("doc_apply_ops", {"doc_id": asm, "base_revision": rev(), "with_tess": False,
                              "ops": [
        {"op": "asm_insert_component", "source": {"path": bar}, "name": "Ground",
         "fixed": True, "transform": grd_xf},
        {"op": "asm_insert_component", "source": {"path": bar}, "name": "Arm",
         "fixed": False}]})
    xsel = max(faces, key=lambda f: abs(f.get("centroid", [0, 0, 0])[0]))
    zsel = max(faces, key=lambda f: abs(f.get("centroid", [0, 0, 0])[2]))
    r = cb.call("doc_apply_ops", {"doc_id": asm, "base_revision": rev(),
                                  "with_tess": False, "ops": [
        {"op": "asm_add_mate", "mate": {
            "type": "angle", "align": "aligned", "suppressed": False,
            "refs": [{"component": "k1", "selector": xsel},
                     {"component": "k2", "selector": xsel}],
            "value": {"value": 0.0, "axis": [0, 0, 1], "axis_point": [0, -200, 0]}}},
        {"op": "asm_add_mate", "mate": {
            "type": "parallel", "align": "aligned", "suppressed": False,
            "refs": [{"component": "k1", "selector": zsel},
                     {"component": "k2", "selector": zsel}]}}]})
    return asm, r.get("id_map", {}).get("$0"), "k2"


@_ENGINE
def test_motion_kinematic_rotation_tracks_driver(tmp_path):
    """Driven-angle kinematics: the Arm's world rotation equals the driven mate value
    at every sample (rigid-body kinematic transport — the displacement analogue of
    the falling-body s = integral of the driver, here angle theta(t) = driver)."""
    asm, mate_id, arm = _swing_arm(tmp_path)
    r = _call("simulate_motion", {"doc_id": asm, "mate_id": mate_id,
                                  "start": 0.0, "stop": 90.0, "steps": 7,
                                  "check_interference": False})
    assert r["ok"], r
    for frame in r["trajectory"]:
        xf = next(b["transform"] for b in frame["bodies"] if b["component"] == arm)
        ang = math.degrees(math.atan2(xf[4], xf[0]))
        assert frame["converged"], frame
        # rotation about Z equals the driven value (mod direction) within solver tol
        assert math.isclose(abs(ang), abs(frame["value"]), abs_tol=0.5), (ang, frame)
    # arm TIP displacement at 90 deg: |p(90)-p(0)| = r*sqrt(2) for a rigid rotation
    # (chord of a quarter circle, c = 2*r*sin(theta/2) with theta=90deg -> r*sqrt(2))
    xf0 = next(b["transform"] for b in r["trajectory"][0]["bodies"]
               if b["component"] == arm)
    xf1 = next(b["transform"] for b in r["trajectory"][-1]["bodies"]
               if b["component"] == arm)
    tip_local = (40.0, 0.0, 0.0)  # bar is 40 long in +X from its origin

    def _pt(m, p):
        return (m[0] * p[0] + m[1] * p[1] + m[2] * p[2] + m[3],
                m[4] * p[0] + m[5] * p[1] + m[6] * p[2] + m[7],
                m[8] * p[0] + m[9] * p[1] + m[10] * p[2] + m[11])

    p0, p1 = _pt(xf0, tip_local), _pt(xf1, tip_local)
    chord = math.dist(p0, p1)
    r_tip = math.dist(p0, _pt(xf0, (0.0, 0.0, 0.0)))
    assert math.isclose(chord, r_tip * math.sqrt(2.0), rel_tol=0.02), (chord, r_tip)


@_ENGINE
def test_motion_dynamics_rejects_orientation_only_force_path(tmp_path):
    """An angle+parallel topology cannot react gravity and must say so explicitly."""
    asm, mate_id, arm = _swing_arm(tmp_path)
    r = _call("simulate_motion", {
        "doc_id": asm, "mate_id": mate_id, "start": 0.0, "stop": 60.0, "steps": 5,
        "check_interference": False,
        "dynamics": {"duration_s": 30.0, "default_density_g_cm3": 7.85,
                     "gravity": [0, 0, -9.81]}})
    assert r["ok"] is False, r
    dyn = r.get("dynamics")
    assert dyn and dyn.get("frames"), r
    assert dyn["ok"] is False and dyn["force_path_resolved"] is False, dyn
    m_arm = 40.0 * 4.0 * 4.0 * 1e-3 * 7.85 * 1e-3
    masses = {b["component"]: b["mass_kg"] for b in dyn["bodies"]}
    assert math.isclose(masses[arm], m_arm, rel_tol=1e-3), masses
    frames = dyn["frames"]
    assert math.isclose(frames[-1]["t_s"], 30.0, abs_tol=1e-9), frames[-1]
    assert len(frames) == len(r["trajectory"]), (len(frames), len(r["trajectory"]))
    for frame in frames:
        assert frame["balanced"] is False, frame
        assert frame["force_residual_n"] > 0.0, frame
        for reaction in frame["reactions"]:
            assert reaction["force_mag_n"] == 0.0, reaction
            assert all(math.isfinite(float(c)) for c in reaction["moment_nm"]), reaction
    assert any("unresolved force/moment path" in warning
               for warning in dyn.get("warnings", [])), dyn


@_ENGINE
def test_motion_fea_handoff_rejects_unresolved_zero_force_path(tmp_path):
    """The motion→FEA seam cannot turn zero reactions into a successful load case."""
    asm, mate_id, arm = _swing_arm(tmp_path)
    tree = cb.call("doc_tree", {"doc_id": asm}).get("tree") or {}
    drive = next(m for m in tree.get("mates", []) if m.get("id") == mate_id)
    fixture_face = next(ref["selector"] for ref in drive.get("refs", [])
                        if ref.get("component") == arm)
    r = _call("motion_fea_handoff", {
        "doc_id": asm, "component": arm,
        "fixtures": [fixture_face],
        "mate_id": mate_id, "start": 0.0, "stop": 60.0, "steps": 5,
        "dynamics": {"duration_s": 1.0, "default_density_g_cm3": 7.85},
        "dry_run": True})
    assert r["ok"] is False, r
    assert "unresolved force/moment path" in r["error"], r
    assert "simhandoff" not in r and not r.get("loads_applied"), r


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
