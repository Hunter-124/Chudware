"""Tests for aicad.simhandoff/1 (contracts/sim_handoff.md + services/mcp/simhandoff.py).

Groups:
  1. Builders — envelope shapes come out structurally valid.
  2. Validator — every structural rule fires on a targeted mutation.
  3. Piping conformance — piping_tools' envelope translators produce valid envelopes
     from mock solve/CFD dicts, and piping_verify_cfd embeds one with ZERO change to
     its pre-existing fields (the regression gate).

Pure Python — no engine kernel, no FastMCP server required.

Run: pytest services/mcp/test_simhandoff.py -q
"""

from __future__ import annotations

import copy
import json

import pytest

from services.mcp import simhandoff as sh


# ── fixtures ──────────────────────────────────────────────────────────────────

def _handoff():
    return sh.build_handoff(
        "piping_to_cfd",
        sh.study_ref("piping", "piping_solve", result_digest={"total_dp_kpa": 12.3}),
        sh.study_ref("cfd", "run_cfd_flow", doc_id="d17"),
        geometry=[sh.geometry_ref("fluid_domain", "step", path="/tmp/x/bore.step",
                                  derived_by="piping_internal_volume",
                                  meta={"volume_mm3": 100.0})],
        conditions=[
            sh.condition("inlet_velocity", "velocity", 1.23, "m/s",
                         "1D solve link r1 velocity_ms", applied_to="inlet"),
            sh.condition("outlet_pressure", "pressure", 287.7, "kPa",
                         "1D solve node n1 pressure_kpa", applied_to="outlet"),
        ],
        expectations=[sh.expectation("pressure_drop", 12.3, "kPa",
                                     "sum of 1D link dp_kpa")],
    )


def _verify():
    return sh.build_verify(
        "piping_to_cfd",
        sh.study_ref("piping", "piping_solve"),
        sh.study_ref("cfd", "run_cfd_flow"),
        [sh.check("pressure_drop", "kPa", 12.3, 13.1, 6.5, 20.0, True)],
        "PASS: within tolerance",
    )


# ── group 1: builders ─────────────────────────────────────────────────────────

def test_build_handoff_valid():
    env = _handoff()
    assert sh.validate_envelope(env) == []
    assert env["schema"] == sh.SCHEMA and env["kind"] == "handoff"
    assert env["id"] and env["created"]
    assert json.loads(json.dumps(env)) == env  # JSON-serializable


def test_build_verify_valid_and_pass_computed():
    env = _verify()
    assert sh.validate_envelope(env) == []
    assert env["pass"] is True
    env2 = sh.build_verify(
        "piping_to_cfd", sh.study_ref("piping", "piping_solve"),
        sh.study_ref("cfd", "run_cfd_flow"),
        [sh.check("pressure_drop", "kPa", 12.3, 30.0, 143.9, 20.0, False),
         sh.check("velocity", "m/s", 1.2, 1.25, 4.2, 10.0, True)],
        "FAIL: dp outside tolerance")
    assert env2["pass"] is False
    assert sh.validate_envelope(env2) == []


def test_condition_vector_value_and_case():
    # motion→FEA shape: vector force with a load-case label
    c = sh.condition("joint_a_reaction", "force", [0.0, -120.5, 33.1], "N",
                     "motion step 14 joint A reaction", applied_to="joint_a_boss",
                     case="pose_14")
    env = sh.build_handoff("motion_to_fea",
                           sh.study_ref("motion", "simulate_motion"),
                           sh.study_ref("fea", "run_fea_static"),
                           conditions=[c])
    assert sh.validate_envelope(env) == []


def test_doc_to_doc_handoff_inline_geometry():
    # hydraulic→piping shape: no derived solid, inline generated doc, empty geometry OK too
    env = sh.build_handoff(
        "hydraulic_to_piping",
        sh.study_ref("hydraulic", "hydraulic_bom"),
        sh.study_ref("piping", "piping_solve"),
        geometry=[sh.geometry_ref("doc", "doc", inline={"schema": "aicad.piping/1"},
                                  derived_by="hydraulic_to_piping_translate")],
        conditions=[sh.condition("supply_pressure", "pressure", 20700, "kPa",
                                 "hydraulic pump rated pressure")])
    assert sh.validate_envelope(env) == []
    env2 = sh.build_handoff("hydraulic_to_piping",
                            sh.study_ref("hydraulic", "hydraulic_bom"),
                            sh.study_ref("piping", "piping_solve"))
    assert sh.validate_envelope(env2) == []  # empty geometry + conditions allowed


def test_ecad_thermal_per_component_heat_sources():
    conds = [sh.condition(f"heat_{ref}", "heat_source", w, "W",
                          f"boardphys component {ref} power_w", applied_to=ref)
             for ref, w in [("U1", 2.5), ("U2", 0.8), ("Q3", 1.1)]]
    env = sh.build_handoff("ecad_to_thermal_fea",
                           sh.study_ref("ecad", "ecad_thermal_handoff"),
                           sh.study_ref("thermal", "run_fea_thermal"),
                           geometry=[sh.geometry_ref("board", "step", path="/tmp/b.step"),
                                     sh.geometry_ref("component_bodies", "step",
                                                     path="/tmp/c.step")],
                           conditions=conds)
    assert sh.validate_envelope(env) == []


# ── group 2: validator rules ──────────────────────────────────────────────────

def _assert_invalid(env, needle):
    errs = sh.validate_envelope(env)
    assert errs and any(needle in e for e in errs), errs


def test_validator_rejects_non_object():
    assert sh.validate_envelope("nope") == ["$: envelope must be an object"]


def test_validator_wrong_schema_and_kind():
    env = _handoff()
    env["schema"] = "aicad.other/1"
    _assert_invalid(env, "$.schema")
    env2 = _handoff()
    env2["kind"] = "banana"
    _assert_invalid(env2, "$.kind")


def test_validator_missing_study_ref_fields():
    env = _handoff()
    del env["source"]["tool"]
    _assert_invalid(env, "$.source.tool")
    env2 = _handoff()
    env2["target"] = "run_cfd_flow"
    _assert_invalid(env2, "$.target")


def test_validator_condition_rules():
    env = _handoff()
    del env["conditions"][0]["units"]
    _assert_invalid(env, "$.conditions[0].units")
    env2 = _handoff()
    del env2["conditions"][1]["provenance"]
    _assert_invalid(env2, "$.conditions[1].provenance")
    env3 = _handoff()
    env3["conditions"][1]["name"] = env3["conditions"][0]["name"]
    _assert_invalid(env3, "duplicate name")
    env4 = _handoff()
    del env4["conditions"][0]["value"]
    _assert_invalid(env4, "$.conditions[0].value")


def test_validator_geometry_needs_payload():
    env = _handoff()
    del env["geometry"][0]["path"]
    _assert_invalid(env, "path/doc_id/inline")


def test_validator_expectation_rules():
    env = _handoff()
    del env["expectations"][0]["units"]
    _assert_invalid(env, "$.expectations[0].units")


def test_validator_verify_rules():
    env = _verify()
    env["checks"] = []
    _assert_invalid(env, "$.checks")
    env2 = _verify()
    del env2["checks"][0]["deviation_pct"]
    _assert_invalid(env2, "deviation_pct")
    env3 = _verify()
    env3["pass"] = False  # contradicts the all-passing checks
    _assert_invalid(env3, "AND of all check passes")
    env4 = _verify()
    env4["verdict"] = ""
    _assert_invalid(env4, "$.verdict")


def test_handoff_rejects_non_finite_physical_values():
    env = _handoff()
    env["conditions"][0]["value"] = float("nan")
    _assert_invalid(env, "$.conditions[0].value")
    env2 = _handoff()
    env2["expectations"][0]["value"] = float("inf")
    _assert_invalid(env2, "$.expectations[0].value")
    env3 = _verify()
    env3["checks"][0]["source_value"] = float("nan")
    _assert_invalid(env3, "$.checks[0].source_value")


def test_check_builder_refuses_boolean_coercion_and_inconsistent_pass():
    with pytest.raises(ValueError, match="boolean"):
        sh.check("stress", "MPa", 1.0, 1.1, 10.0, 20.0, "false")
    with pytest.raises(ValueError, match="contradicts"):
        sh.check("stress", "MPa", 1.0, 2.0, 100.0, 20.0, True)


def test_validator_checks_individual_pass_math_and_meaningful_geometry_refs():
    env = _verify()
    env["checks"][0]["pass"] = False
    _assert_invalid(env, "contradicts deviation_pct")
    handoff = _handoff()
    handoff["geometry"][0]["path"] = ""
    _assert_invalid(handoff, "non-empty path/doc_id/inline")


def test_builders_refuse_empty_provenance_and_geometry_payloads():
    with pytest.raises(ValueError, match="at least one"):
        sh.geometry_ref("domain", "stl")
    with pytest.raises(ValueError, match="non-empty"):
        sh.study_ref(" ", "run")
    with pytest.raises(ValueError, match="condition.value"):
        sh.condition("fixture", "fixed", True, "1", "caller")



def test_digest_and_metadata_allow_json_flags_but_reject_non_finite_numbers():
    ref = sh.study_ref("cfd", "run", result_digest={"converged": True, "cd": 0.2})
    assert ref["result_digest"]["converged"] is True
    with pytest.raises(ValueError, match="finite"):
        sh.study_ref("cfd", "run", result_digest={"cd": float("nan")})
    with pytest.raises(ValueError, match="finite"):
        sh.geometry_ref("domain", "inline", inline={"bbox": [0, float("inf")]})

def test_attach_valid_and_invalid():
    res = {"ok": True, "x": 1}
    out = sh.attach(res, _verify())
    assert out is res and res["simhandoff"]["kind"] == "verify"
    assert res["x"] == 1  # pre-existing fields untouched
    bad = _verify()
    bad["checks"] = []
    with pytest.raises(ValueError):
        sh.attach({"ok": True}, bad)


# ── group 3: piping conformance (mock dicts, engine-less) ────────────────────

def _mock_solve_result():
    return {
        "ok": True, "converged": True,
        "fluid": {"name": "water", "rho": 998.2, "mu": 1.002e-3},
        "nodes": [
            {"id": "n0", "xyz": [0, 0, 0], "fixed": True, "pressure_kpa": 300.0},
            {"id": "n1", "xyz": [800, 0, 0], "fixed": False, "pressure_kpa": 287.7},
        ],
        "links": [
            {"id": "r1", "from": "n0", "to": "n1", "flow_m3h": 9.5,
             "velocity_ms": 1.23, "dp_kpa": 12.3, "id_mm": 52.5},
        ],
    }


def _mock_cfd_result():
    return {"ok": True, "drag_n": 28.4, "frontal_area_mm2": 2165.0,
            "cd": 0.02, "velocity_ms": 1.23, "converged": True}


def test_piping_handoff_envelope_conforms():
    from services.mcp.piping_tools import _handoff_envelope
    from services.piping import cfd_handoff as ch
    solve = _mock_solve_result()
    bc = ch.cfd_boundary_from_solve(solve)
    assert bc["ok"], bc
    env = _handoff_envelope(solve, bc, "/tmp/x/bore.step", 100.0, "d17")
    assert sh.validate_envelope(env) == []
    assert env["coupling"] == "piping_to_cfd"
    names = {c["name"] for c in env["conditions"]}
    assert names == {"inlet_velocity", "inlet_pressure", "outlet_pressure"}
    assert all(c["units"] and c["provenance"] for c in env["conditions"])
    assert env["expectations"][0]["quantity"] == "pressure_drop"
    assert env["geometry"][0]["role"] == "fluid_domain"
    assert env["geometry"][0]["doc_id"] == "d17"


def test_piping_handoff_envelope_no_bore_doc():
    # pending-manual-import path: no engine doc yet — still valid
    from services.mcp.piping_tools import _handoff_envelope
    from services.piping import cfd_handoff as ch
    solve = _mock_solve_result()
    bc = ch.cfd_boundary_from_solve(solve)
    env = _handoff_envelope(solve, bc, "/tmp/x/bore.step", None, None)
    assert sh.validate_envelope(env) == []


def test_piping_verify_tool_embeds_envelope_without_changing_fields():
    """The regression gate: piping_verify_cfd's pre-existing fields are IDENTICAL to
    the raw engine verify result; the only addition is the `simhandoff` key."""
    from services.mcp.piping_tools import register_piping_tools
    from services.piping import cfd_handoff as ch

    class _MCP:
        def __init__(self):
            self.tools = {}

        def tool(self):
            def _deco(f):
                self.tools[f.__name__] = f
                return f
            return _deco

    mcp = _MCP()
    register_piping_tools(mcp)
    sr, cr = _mock_solve_result(), _mock_cfd_result()
    baseline = ch.verify_pressure_drop(copy.deepcopy(sr), copy.deepcopy(cr),
                                       tolerance_pct=20.0)
    res = json.loads(mcp.tools["piping_verify_cfd"](
        solve_result=json.dumps(sr), cfd_result=json.dumps(cr)))
    assert res.get("ok"), res
    env = res.pop("simhandoff")
    assert res == baseline  # ZERO behavior change on existing fields
    assert sh.validate_envelope(env) == []
    assert env["kind"] == "verify"
    assert env["checks"][0]["source_value"] == baseline["dp_1d_kpa"]
    assert env["checks"][0]["target_value"] == baseline["dp_3d_kpa"]
    assert env["pass"] == baseline["pass"]
    assert env["verdict"] == baseline["verdict"]
