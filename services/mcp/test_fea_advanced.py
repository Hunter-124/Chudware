"""Unit tests for advanced FEA — fatigue / thermal / buckling / contact.

Split in two: the pure-Python `fea_advanced` math + .inp generators (always run,
no CalculiX binary, no libchudware), and the MCP-tool degrade paths (need FastMCP
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
# (Same rule as test_mcp_freshness.py; needed for the services/mcp/ mirror suite.)
from services.mcp.cad_tools import register_cad_tools as _register_cad_tools


# --------------------------------------------------------------------- fatigue math

def test_sn_life_finite_matches_shigley():
    # a=(0.9*400)^2/200=648; b=-1/3*log10(1.8)=-0.085085; N=(250/648)^(1/b)=7.263e4
    N = fa.sn_life(250.0, 200.0, 400.0)
    assert math.isclose(N, 7.2627e4, rel_tol=0.01)


def test_sn_life_runout_and_guards():
    assert fa.sn_life(150.0, 200.0, 400.0) == 1e6      # below endurance
    assert fa.sn_life(0.0, 200.0, 400.0) == 1e6        # zero applied stress
    with pytest.raises(ValueError, match="both be > 0"):
        fa.sn_life(300.0, 0.0, 400.0)                  # missing material data
    with pytest.raises(ValueError, match=r"0.9\*Sut"):
        fa.sn_life(390.0, 360.0, 400.0)                # singular/nonphysical S-N line


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


@pytest.mark.parametrize(
    "call,args",
    [(fa.marin_ka, (400.0, 9)),
     (fa.marin_kb, (0.0, 0)),
     (fa.marin_kc, (9,)),
     (fa.marin_kd, (float("nan"),)),
     (fa.reliability_ke, (100.0,)),
     (fa.corrected_endurance, (0.0, 400.0))])
def test_fatigue_corrections_reject_values_they_used_to_clamp_or_default(call, args):
    with pytest.raises(ValueError):
        call(*args)


def test_fatigue_summary_refuses_missing_or_zero_solver_theory_fields():
    with pytest.raises(ValueError, match="absent"):
        fa.fatigue_summarize(
            {"goodman": {"sa_eq_mpa": 100.0}}, 200.0, 400.0, theory="gerber")
    with pytest.raises(ValueError, match="non-positive"):
        fa.fatigue_summarize(
            {"goodman": {"sa_eq_mpa": 0.0}}, 200.0, 400.0, theory="goodman")


# --------------------------------------------------------------------- .inp generators



def test_contact_inp_two_disjoint_blocks():
    # The box-proxy deck the frozen `sim_run_contact` worker RPC still generates; the
    # MCP run_fea_contact tool solves real contact in the engine instead.
    gen = fa.build_contact_block_inp(20.0, 20.0, 10.0, 200000.0, 0.29, 5.0,
                                     nx=3, ny=3, nz=2)
    t = gen["inp"]
    assert "NLGEOM" in t and "*CONTACT PAIR" in t
    assert "TYPE=ELEMENT" in t and "TYPE=NODE" in t
    assert gen["applied_load_n"] == 5.0 * 20.0 * 20.0
    assert min(gen["top_nodes"]) > max(gen["base_nodes"])   # no id collision


def test_generators_reject_degenerate_or_non_finite_inputs_before_meshing():
    with pytest.raises(ValueError, match="must all be > 0"):
        fa.build_contact_block_inp(0.0, 20.0, 20.0, 200000.0, 0.29, 5.0)
    with pytest.raises(ValueError, match="pressure"):
        fa.build_contact_block_inp(20.0, 20.0, 10.0, 200000.0, 0.29, 0.0)


# --------------------------------------------------------------------- ccx runner

def test_ccx_absent_degrades_honestly(monkeypatch):
    monkeypatch.setattr(fa, "ccx_path", lambda: None)
    r = fa.run_ccx("*NODE\n1, 0, 0, 0\n", job="x")
    assert r["ok"] is False and r["skipped"] is True and "reason" in r


def test_ccx_absent_result_shape():
    r = fa._ccx_absent_result("thermal")
    assert r["ok"] is False and r["skipped"] is True
    assert "GPL-2.0" in r["reason"] and r["backend"].startswith("calculix")


def _fake_ccx_run(monkeypatch, tmp_path, outcome: str):
    created: list = []

    def fake_mkdtemp(prefix: str):
        path = tmp_path / f"{prefix}{len(created)}"
        path.mkdir()
        created.append(path)
        return str(path)

    def fake_run(cmd, *, cwd, capture_output, text, timeout):
        if outcome == "timeout":
            raise fa.subprocess.TimeoutExpired(cmd, timeout)
        if outcome == "launch":
            raise OSError("cannot execute ccx")
        dat_path = fa.Path(cwd) / f"{cmd[-1]}.dat"
        dat_path.write_text(
            " temperatures for set NMID and time  0.100000E+01\n\n"
            " 5  50.0\n"
            " 6  50.0\n\n")
        return fa.subprocess.CompletedProcess(
            cmd, 0 if outcome == "success" else 2,
            stdout="Job finished" if outcome == "success" else "",
            stderr="" if outcome == "success" else "solver failed")

    monkeypatch.setattr(fa, "ccx_path", lambda: "/fake/ccx")
    monkeypatch.setattr(fa.tempfile, "mkdtemp", fake_mkdtemp)
    monkeypatch.setattr(fa.subprocess, "run", fake_run)
    return created


def test_run_ccx_default_keeps_success_outputs_through_parse_then_cleans(
        monkeypatch, tmp_path):
    created = _fake_ccx_run(monkeypatch, tmp_path, "success")
    run = fa.run_ccx("*NODE\n5, 0, 0, 0\n6, 1, 0, 0\n", job="owned")
    dat_path = run["dat_path"]
    workdir = created[0]
    assert run["ok"] is True and run["retained"] is False
    assert run["workdir"] is None and dat_path
    assert workdir.is_dir()
    assert fa.parse_dat_nodal(dat_path, "NT", [5, 6]) == 50.0
    assert workdir.is_dir(), "result path must remain valid for all caller parsing"

    # The unretained result/path objects own the directory. Releasing the final
    # output path is the end of the parsing lifetime and must remove it.
    del run
    del dat_path
    import gc
    gc.collect()
    assert not workdir.exists()


@pytest.mark.parametrize("outcome", ["timeout", "launch", "solver"])
def test_run_ccx_default_cleans_every_error_workdir(
        monkeypatch, tmp_path, outcome):
    created = _fake_ccx_run(monkeypatch, tmp_path, outcome)
    run = fa.run_ccx("*NODE\n1, 0, 0, 0\n", job="failed")
    assert run["ok"] is False and run["workdir"] is None
    assert run["retained"] is False
    assert created and not created[0].exists()
    if outcome == "solver":
        assert run["dat_path"] is None


@pytest.mark.parametrize("outcome", ["success", "timeout", "launch", "solver"])
def test_run_ccx_keep_dir_retains_and_reports_every_outcome(
        monkeypatch, tmp_path, outcome):
    created = _fake_ccx_run(monkeypatch, tmp_path, outcome)
    run = fa.run_ccx("*NODE\n1, 0, 0, 0\n", job="kept", keep_dir=True)
    assert run["workdir"] == str(created[0])
    assert run["retained"] is True
    assert created[0].is_dir()


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


def test_dat_nodal_parser_refuses_an_incomplete_final_increment(tmp_path):
    text = (
        " temperatures for set NBASE and time  0.500000E+00\n\n"
        " 5  10.0\n"
        " 6  10.0\n\n"
        " temperatures for set NBASE and time  0.100000E+01\n\n"
        " 5  20.0\n\n"
    )
    path = tmp_path / "incomplete.dat"
    path.write_text(text)
    assert fa.parse_dat_nodal(str(path), "NT", [5, 6], component=1) is None


def test_dat_nodal_parser_refuses_duplicate_requested_nodes(tmp_path):
    path = tmp_path / "unused.dat"
    path.write_text("")
    with pytest.raises(ValueError, match="duplicate"):
        fa.parse_dat_nodal(str(path), "RF", [5, 5], component=3)


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




@pytest.mark.parametrize(
    "tool,kwargs,needle",
    [("run_fea_modal", {"fixtures": [], "n_modes": 0, "doc_id": "nope"},
      "positive integer"),
     ("run_fea_frequency_response",
      {"fixtures": ["bottom"], "base_accel_g": 0.0, "doc_id": "nope"},
      "base_accel_g"),
     ("run_fea_thermal",
      {"hot_faces": [{"face": "+x", "temperature": 100}],
       "cold_faces": [{"face": "-x", "temperature": 0}],
       "conductivity_w_mk": 0.0, "doc_id": "nope"}, "conductivity_w_mk"),
     ("run_fea_buckling",
      {"fixtures": ["bottom"], "load": [{"face": "top", "force": [0, 0, -1]}],
       "n_modes": 0, "doc_id": "nope"}, "positive integer"),
     ("run_fea_contact",
      {"fixtures": ["bottom"], "load": {"face": "top", "force": [0, 0, -1]},
       "max_gap_mm": 0.0, "doc_id": "nope"}, "max_gap_mm"),
     ("run_cfd_flow", {"velocity": 0.0, "doc_id": "nope"}, "velocity"),
     ("run_cfd_flow", {"fluid": "steam", "doc_id": "nope"}, "fluid"),
     ("run_cfd_accurate", {"fluid": "steam", "doc_id": "nope"}, "fluid"),
     ("run_cfd_accurate", {"preset": "ultra", "doc_id": "nope"}, "preset")])
def test_public_setup_guards_fail_before_document_or_backend_work(tool, kwargs, needle):
    out = json.loads(_cad_tools()[tool](**kwargs))
    assert out.get("ok") is False, out
    assert needle in out.get("error", ""), out
