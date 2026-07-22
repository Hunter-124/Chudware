"""Integration tests for the run_fea_frequency_response MCP tool (T-BL17).

Engine-free: the C++ bridge (cb.call) is monkeypatched with a canned modal result,
so these run in CI without libaicad.so. The physics itself is validated separately
in engine_oracle/sim/test_vibration.py against textbook references.

Covers the DoD behaviours:
  * harmonic response spectrum comes back with peak + citation + limitations
  * honest degrade: no psd_breakpoints -> random={ok:false, reason:"no_psd_provided"}
    (never a default profile), verdict=None when no spec given
  * vibration_spec -> pass/fail verdict (the DAG verify-node end condition)
  * DAG dispatch table carries the "vibration" check_type, fail-closed predicate
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

_REPO_ROOT = str(Path(__file__).resolve().parents[2])
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import pytest

from mcp_server import cad_tools
from mcp_server import cad_sim_tools  # T-RF8: run_fea_* tools live here now


# ---------------------------------------------------------------------------
# Fixtures — a fake bridge with one 100 Hz mode on a 2-node "mesh"
# ---------------------------------------------------------------------------

def _fake_cb_call(method: str, params: dict | None = None) -> dict:
    if method == "doc_export_step":
        return {"ok": True}
    if method == "sim_run_modal":
        # 1 mode * 2 nodes * 3 dof, both nodes displacing along +Z with unit
        # amplitude -> gamma/m == 1 exactly (SDOF-equivalent, see engine tests).
        return {"ok": True, "freqs_hz": [100.0],
                "mode_shapes": [0.0, 0.0, 1.0, 0.0, 0.0, 1.0],
                "n_modes": 1, "n_nodes": 2, "n_dof": 6}
    if method == "eval_mass":
        return {"volume_mm3": 127388.5, "mass_g": 1000.0}
    raise AssertionError(f"unexpected bridge method {method!r}")


@pytest.fixture()
def freq_tool(monkeypatch):
    """The registered tool fn with every engine touchpoint mocked out."""
    from mcp.server.fastmcp import FastMCP

    monkeypatch.setattr(cad_tools.cb, "call", _fake_cb_call)
    monkeypatch.setattr(cad_sim_tools, "_doc", lambda doc_id: "doc-test")
    monkeypatch.setattr(cad_sim_tools, "_resolve_face_points",
                        lambda d, q: [{"points": [[0.0, 0.0, 0.0]], "tol_mm": 1.0}])
    monkeypatch.setattr(cad_sim_tools, "_autosave_all_open", lambda: None)
    monkeypatch.setattr(cad_sim_tools, "_persist_study", lambda d, rec: "study1")
    monkeypatch.setattr(cad_sim_tools, "_solve_sync_or_handoff",
                        lambda kind, fn, *a, **kw: fn())

    mcp = FastMCP("test-vibration")
    cad_tools.register_cad_tools(mcp)
    tool = mcp._tool_manager._tools.get("run_fea_frequency_response")
    assert tool is not None, "run_fea_frequency_response not registered"
    return tool.fn


# ---------------------------------------------------------------------------
# Harmonic path
# ---------------------------------------------------------------------------

def test_harmonic_response_spectrum(freq_tool):
    out = json.loads(freq_tool(fixtures=["bottom"], sweep_hz=[50.0, 100.0, 200.0],
                               zeta=0.02))
    assert out["ok"] is True
    harm = out["harmonic"]
    assert harm["peak_freq_hz"] == pytest.approx(100.0)
    # SDOF-equivalent mode at resonance: |T| = sqrt(1+4z^2)/(2z) = 25.02 for z=0.02
    assert harm["peak_accel_g"] == pytest.approx(25.02, rel=1e-2)
    assert len(harm["accel_g"]) == 3
    assert "citation" in harm and "limitations" in harm
    assert out["freqs_hz"] == [100.0]
    assert "zeta=0.02" in out["damping_assumption"]
    assert out["saved_to_study"] is True


def test_default_sweep_is_200_points(freq_tool):
    out = json.loads(freq_tool(fixtures=["bottom"]))
    assert out["ok"] is True
    assert len(out["harmonic"]["sweep_hz"]) == 200
    assert out["harmonic"]["sweep_hz"][0] == pytest.approx(10.0)
    assert out["harmonic"]["sweep_hz"][-1] == pytest.approx(2000.0)


# ---------------------------------------------------------------------------
# Honest degrade — no PSD, no default profile; no spec, no verdict
# ---------------------------------------------------------------------------

def test_no_psd_degrades_contract_shaped(freq_tool):
    out = json.loads(freq_tool(fixtures=["bottom"], sweep_hz=[100.0]))
    assert out["ok"] is True                       # harmonic still runs
    rand = out["random"]
    assert rand["ok"] is False
    assert rand["reason"] == "no_psd_provided"
    assert "psd_breakpoints" in rand["hint"]
    assert "total_grms" not in rand                # nothing fabricated
    assert out["verdict"] is None                  # no spec -> no verdict


def test_random_with_psd(freq_tool):
    # Flat 0.04 g^2/Hz at the 100 Hz mode, Q=25: GRMS = sqrt(pi/2*100*25*0.04)
    # = 12.533 g (Steinberg Eq. 6.6 — literal, see engine tests).
    out = json.loads(freq_tool(fixtures=["bottom"], sweep_hz=[100.0],
                               psd_breakpoints=[[20.0, 0.04], [2000.0, 0.04]]))
    rand = out["random"]
    assert rand["ok"] is True
    assert rand["total_grms"] == pytest.approx(12.533, rel=1e-2)
    assert "Steinberg" in rand["citation"]


# ---------------------------------------------------------------------------
# Spec verdict — DAG verify-node end condition
# ---------------------------------------------------------------------------

def test_verdict_pass_and_fail(freq_tool):
    args = dict(fixtures=["bottom"], sweep_hz=[100.0],
                psd_breakpoints=[[20.0, 0.04], [2000.0, 0.04]])
    out = json.loads(freq_tool(vibration_spec={"max_grms": 15.0,
                                               "max_peak_accel_g": 30.0}, **args))
    assert out["verdict"]["passed"] is True
    out = json.loads(freq_tool(vibration_spec={"max_grms": 5.0}, **args))
    assert out["verdict"]["passed"] is False


def test_verdict_grms_limit_without_psd_fails_closed(freq_tool):
    out = json.loads(freq_tool(fixtures=["bottom"], sweep_hz=[100.0],
                               vibration_spec={"max_grms": 15.0}))
    assert out["verdict"]["passed"] is False
    reasons = [c.get("reason", "") for c in out["verdict"]["checks"]]
    assert any("psd" in r.lower() for r in reasons)


# ---------------------------------------------------------------------------
# Fixture resolution guard
# ---------------------------------------------------------------------------

def test_unresolved_fixtures_error(freq_tool, monkeypatch):
    monkeypatch.setattr(cad_sim_tools, "_resolve_face_points", lambda d, q: [])
    out = json.loads(freq_tool(fixtures=["bogus-face"], sweep_hz=[100.0]))
    assert out["ok"] is False
    assert "no fixture faces resolved" in out["error"]


# ---------------------------------------------------------------------------
# DAG dispatch wiring
# ---------------------------------------------------------------------------

def test_dag_dispatch_table_has_vibration():
    from services.dag.verify import DISPATCH_TABLE, _check_vibration_default
    module_key, tool_name, predicate = DISPATCH_TABLE["vibration"]
    assert module_key == "cad"
    assert tool_name == "run_fea_frequency_response"
    # Fail-closed behaviour:
    assert predicate({"ok": False}) is False
    assert predicate({"ok": True, "verdict": None}) is False       # no spec -> not a pass
    assert predicate({"ok": True, "verdict": {"passed": False}}) is False
    assert predicate({"ok": True, "verdict": {"passed": True}}) is True
    assert predicate is _check_vibration_default
