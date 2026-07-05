"""Unit tests for T-BL04 advanced FEA — fatigue / thermal / buckling / contact.

Split in two: the pure-Python `fea_advanced` math + .inp generators (always run,
no CalculiX binary, no libaicad), and the MCP-tool degrade paths (need FastMCP
registration but assert only the honest ccx/backend-absent behaviour so they run
green in CI without ccx installed).
"""
from __future__ import annotations

import json
import math

import pytest

from services.mcp import fea_advanced as fa

# Import the registrar EARLY, before sibling tests (_ensure_stubs) pollute
# sys.modules with stub mcp/anyio packages — cad_tools.py imports FastMCP at
# module level, so it must be imported while the REAL mcp package is accessible.
# (Same rule as test_mcp_freshness.py; needed for the mcp_server/ mirror suite.)
from services.mcp.cad_tools import register_cad_tools as _register_cad_tools


# --------------------------------------------------------------------- fatigue math

def test_sn_life_finite_matches_shigley():
    # a=(0.9*400)^2/200=648; b=-1/3*log10(1.8)=-0.085085; N=(250/648)^(1/b)=7.263e4
    N = fa.sn_life(250.0, 200.0, 400.0)
    assert math.isclose(N, 7.2627e4, rel_tol=0.01)


def test_sn_life_runout_and_guards():
    assert fa.sn_life(150.0, 200.0, 400.0) == 1e6      # below endurance
    assert fa.sn_life(0.0, 200.0, 400.0) == 1e6        # zero stress
    assert fa.sn_life(300.0, 0.0, 400.0) == 1e6        # no endurance data


def test_marin_factors_ranges():
    assert math.isclose(fa.marin_ka(400.0, 1), 0.9218, abs_tol=0.01)
    assert fa.marin_kb(10.0, 1) == 1.0                  # axial: no size effect
    assert 0.9 < fa.marin_kb(10.0, 0) <= 1.0            # bending size factor
    assert fa.marin_kc(1) == 0.85 and fa.marin_kc(2) == 0.59
    assert fa.marin_kd(20.0) == 1.0
    assert fa.reliability_ke(50.0) == 1.0
    assert fa.reliability_ke(99.0) < 1.0


def test_corrected_endurance_applies_chain():
    corr = fa.corrected_endurance(200.0, 400.0, surface=1, reliability_pct=99.0)
    # Se = 200 * ka * kb(=1 bending default d) * kc * kd * ke  < 200
    assert corr["se_corrected_mpa"] < 200.0
    assert set(corr) >= {"ka", "kb", "kc", "kd", "ke", "se_corrected_mpa"}


def test_fatigue_summarize_regimes():
    theories = {"goodman": {"sa_eq_mpa": 250.0}, "gerber": {"sa_eq_mpa": 150.0},
                "morrow": {"sa_eq_mpa": 500.0}}
    out = fa.fatigue_summarize(theories, 200.0, 400.0, theory="goodman")
    assert out["selected_theory"] == "goodman"
    assert out["theories"]["goodman"]["regime"] == "finite"
    assert out["theories"]["gerber"]["regime"] == "infinite"       # <= Se
    assert out["theories"]["morrow"]["regime"] == "static_overload"  # >= Sut
    assert out["cycles"] == out["theories"]["goodman"]["cycles"]


# --------------------------------------------------------------------- .inp generators

def test_thermal_inp_and_expected_mid_T():
    gen = fa.build_thermal_bar_inp(200.0, 20.0, 20.0, 50.0, 100.0, 0.0, nx=8)
    assert "*HEAT TRANSFER, STEADY STATE" in gen["inp"]
    assert "*CONDUCTIVITY" in gen["inp"]
    assert gen["expected_mid_T"] == 50.0


def test_buckling_inp_and_euler_reference():
    gen = fa.build_buckling_column_inp(1000.0, 20.0, 20.0, 200000.0, 0.29, 5000.0)
    assert "*BUCKLE" in gen["inp"]
    I = 20.0 * 20.0 ** 3 / 12.0
    Pcr = math.pi ** 2 * 200000.0 * I / (2000.0 ** 2)
    assert math.isclose(gen["expected_Pcr"], Pcr, rel_tol=1e-9)


def test_contact_inp_two_disjoint_blocks():
    gen = fa.build_contact_block_inp(20.0, 20.0, 10.0, 200000.0, 0.29, 5.0,
                                     nx=3, ny=3, nz=2)
    t = gen["inp"]
    assert "NLGEOM" in t and "*CONTACT PAIR" in t
    assert "TYPE=ELEMENT" in t and "TYPE=NODE" in t
    assert gen["applied_load_n"] == 5.0 * 20.0 * 20.0
    assert min(gen["top_nodes"]) > max(gen["base_nodes"])   # no id collision


# --------------------------------------------------------------------- ccx runner

def test_ccx_absent_degrades_honestly(monkeypatch):
    monkeypatch.setattr(fa, "ccx_path", lambda: None)
    r = fa.run_ccx("*NODE\n1, 0, 0, 0\n", job="x")
    assert r["ok"] is False and r["skipped"] is True and "reason" in r


def test_ccx_absent_result_shape():
    r = fa._ccx_absent_result("thermal")
    assert r["ok"] is False and r["skipped"] is True
    assert "GPL-2.0" in r["reason"] and r["backend"].startswith("calculix")


def test_dat_nodal_parser_averages():
    import tempfile
    import os
    # Minimal ccx-like *NODE PRINT NT block.
    text = (
        "\n number of nodes\n\n"
        " temperatures (modal) for set NMID and time  0.100000E+01\n"
        "\n"
        "         5   0.500000E+02\n"
        "         6   0.500000E+02\n"
        "\n"
    )
    p = os.path.join(tempfile.mkdtemp(), "j.dat")
    with open(p, "w") as fh:
        fh.write(text)
    val = fa.parse_dat_nodal(p, "NT", [5, 6], component=1)
    assert val is not None and math.isclose(val, 50.0, abs_tol=1e-6)


# --------------------------------------------------------------------- MCP tool degrade

class _CaptureMCP:
    """Minimal FastMCP stand-in capturing @mcp.tool() registrations — collects
    cleanly whether or not the real ``mcp`` package is importable at call time
    (register_cad_tools never touches FastMCP internals)."""

    def __init__(self):
        self.tools: dict = {}

    def tool(self, *args, **kwargs):
        def dec(fn):
            self.tools[kwargs.get("name", fn.__name__)] = fn
            return fn
        return dec


def _cad_tools():
    m = _CaptureMCP()
    _register_cad_tools(m)
    return m.tools


def test_all_four_tools_registered():
    fns = _cad_tools()
    for name in ("run_fea_fatigue", "run_fea_thermal", "run_fea_buckling",
                 "run_fea_contact"):
        assert name in fns, f"{name} not registered"


def test_ccx_tools_skip_when_absent(monkeypatch):
    """When ccx is absent, the ccx-backed tools must return skipped:true — never a
    fabricated result. Uses the module's ccx_path so no binary is required."""
    monkeypatch.setattr(fa, "ccx_path", lambda: None)
    fns = _cad_tools()
    # These early-return on the ccx check before touching any doc, so doc_id="x"
    # never resolves geometry.
    r_th = json.loads(fns["run_fea_thermal"]([{"face": "+x", "temperature": 100}],
                                             [{"face": "-x", "temperature": 0}],
                                             doc_id="nope"))
    assert r_th.get("skipped") is True and r_th.get("ok") is False
    r_bk = json.loads(fns["run_fea_buckling"](["bottom"],
                                              [{"face": "top", "force": [0, 0, -1]}],
                                              doc_id="nope"))
    assert r_bk.get("skipped") is True
    r_ct = json.loads(fns["run_fea_contact"](1.0, doc_id="nope"))
    assert r_ct.get("skipped") is True
