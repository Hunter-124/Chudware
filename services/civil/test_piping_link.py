"""Offline interface V&V; no native bridge/daemon is needed.

Oracle: Darcy-Weisbach laminar f=64/Re gives h_f=a*Q with
 a=128*nu*L/(pi*g*D**4), EPA EPANET 2.2 Manual §12.1. The plant law
 Q=k*sqrt(delta_p_kPa) then gives Q**2+k**2*rho*g/1000*a*Q
 -k**2*rho*g/1000*(H_source-H_sink)=0. These are independent hand
 calculations, not values taken from the implementation under test.
"""
from copy import deepcopy
import json
import math

import pytest

from services.civil.errors import CivilError
from services.civil.piping_link import civil_to_fea_handoff, couple, tie_in_boundaries
from services.mcp.simhandoff import validate_envelope

G = 9.80665
RHO = 1000.0
NU = 1.004e-6
DIAMETER = 0.01
LENGTH = 1000.0
K = 5e-7  # m3/s/sqrt(kPa)


def model_and_inputs(direction="supply", base_demand=0.0):
    reservoir_head = 40.0 if direction == "supply" else 20.0
    endpoint = "start" if direction == "supply" else "end"
    model = {"schema": "aicad.civil/1", "name": "Interface oracle",
             "water_networks": [{"id": "w1", "name": "Laminar feeder", "headloss": "D-W",
                                 "junctions": [{"id": "j1", "x": LENGTH, "y": 0.0, "elevation": 10.0,
                                                "demand_m3s": base_demand, "pattern": None}],
                                 "reservoirs": [{"id": "res", "x": 0.0, "y": 0.0, "head": reservoir_head, "pattern": None}],
                                 "tanks": [], "pipes": [{"id": "feeder", "from": "res", "to": "j1", "length": LENGTH,
                                                          "diameter": DIAMETER, "roughness": 0.0, "minor_loss": 0.0,
                                                          "status": "open", "material": None, "vertices": []}],
                                 "pumps": [], "valves": [], "patterns": {},
                                 "options": {"duration_s": 0, "demand_model": "DDA", "viscosity_m2s": NU,
                                             "accuracy": 1e-11, "max_iter": 200}}],
             "gravity_networks": [],
             "tie_ins": [{"id": "t1", "civil_network": "w1", "civil_node": "j1", "piping_doc_id": "plant",
                          "piping_study": "piping_network", "piping_endpoint": {"run_id": "r1", "end": endpoint},
                          "direction": direction, "elevation_offset_m": 0.0}]}
    doc = {"schema": "aicad.piping/1", "id": "ca406a0d-1a28-49da-b57f-43cb2fb8c03d", "name": "Fixed resistance",
           "units": {"system": "MMGS"}, "material": "A106-B",
           "runs": [{"id": "r1", "path": [[0, 0, 10000], [1000, 0, 10000]], "nps": 0.5,
                     "schedule": "40", "suppressed": False}], "fittings": []}
    other_end = "end" if direction == "supply" else "start"
    other_pressure = 0.0 if direction == "supply" else RHO * G * (40.0 - 10.0) / 1000.0
    inputs = {"doc": doc, "boundary": [{"run": "r1", "end": other_end, "pressure": other_pressure}],
              "fluid": {"rho": RHO, "mu": RHO * NU}}
    return model, inputs


def fixed_resistance(doc, boundary, fluid):
    """Independent plant q=k*sqrt(delta_p), returning the native solver shape."""
    pressures = {b["end"]: b["pressure"] for b in boundary if b.get("run") == "r1"}
    dp = pressures["start"] - pressures["end"]
    q = math.copysign(K * math.sqrt(abs(dp)), dp)
    nodes = []
    for i, endpoint in enumerate(("start", "end")):
        xyz = doc["runs"][0]["path"][0 if i == 0 else -1]
        p = pressures[endpoint]
        nodes.append({"id": f"n{i}", "xyz": xyz, "pressure_kpa": p,
                      "head_m": xyz[2] / 1000.0 + p * 1000.0 / (fluid["rho"] * G), "demand_m3h": 0.0})
    return {"ok": True, "converged": True, "nodes": nodes,
            "links": [{"id": "r1", "from": "n0", "to": "n1", "flow_m3h": q * 3600.0}], "warnings": []}


def analytic_flow(head_difference, base_demand=0.0):
    a = 128.0 * NU * LENGTH / (math.pi * G * DIAMETER**4)
    c = RHO * G / 1000.0
    beta = K**2 * c * a
    gamma = K**2 * c * (head_difference - a * base_demand)
    return (-beta + math.sqrt(beta**2 + 4.0 * gamma)) / 2.0, a


def test_hydrostatic_boundary_timestep_offset_and_native_units():
    model, _ = model_and_inputs()
    model["water_networks"][0]["junctions"][0]["elevation"] = 12.5
    model["tie_ins"][0]["elevation_offset_m"] = 2.0
    result = {"ok": True, "converged": True,
              "timesteps": [{"t_s": 0, "nodes": {"j1": {"head_m": 80.0}}},
                            {"t_s": 3600, "nodes": {"j1": {"head_m": 90.0}}}]}
    mapped = tie_in_boundaries(model, result, timestep=1)
    row = mapped["tie_ins"]["t1"]
    # rho=1000, g=9.80665, pressure head=90-12.5-2=75.5 m.
    assert row["pressure_kpa"] == pytest.approx(740.402075, abs=1e-9)
    assert row["head_m"] == 90.0
    assert row["elevation_mm"] == 14500.0
    assert mapped["boundary"] == [{"run": "r1", "end": "start", "pressure": pytest.approx(740.402075)}]
    assert mapped["assumptions"]  # disclosed freshwater idealization, not native default
    result["density_kgm3"] = 850.0
    assert tie_in_boundaries(model, result)["tie_ins"]["t1"]["pressure_kpa"] == pytest.approx(545.98523875)


@pytest.mark.parametrize("direction,head_difference", [("supply", 30.0), ("discharge", 20.0)])
def test_coupled_intersection_matches_closed_form_and_conserves_flow(direction, head_difference):
    model, inputs = model_and_inputs(direction)
    before_model, before_inputs = deepcopy(model), deepcopy(inputs)
    expected_q, a = analytic_flow(head_difference)
    # Re below 2000 validates use of the independent laminar resistance oracle.
    assert 4.0 * expected_q / (math.pi * DIAMETER * NU) < 2000.0
    result = couple(model, "w1", fixed_resistance, piping_inputs=inputs, tol_rel=1e-7, max_iter=100)
    assert result["ok"] and result["converged"]
    row = result["tie_ins"]["t1"]
    assert row["flow_m3s"] == pytest.approx(expected_q, rel=3e-7)
    sign = 1 if direction == "supply" else -1
    expected_head = model["water_networks"][0]["reservoirs"][0]["head"] - sign * a * expected_q
    assert row["head_m"] == pytest.approx(expected_head, abs=1e-5)
    civil_step = result["civil_result"]["timesteps"][0]
    assert civil_step["links"]["feeder"]["flow_m3s"] == pytest.approx(sign * expected_q, rel=3e-7)
    assert civil_step["nodes"]["j1"]["demand_m3s"] == pytest.approx(sign * expected_q, rel=3e-7)
    assert row["civil_flow_m3s"] == pytest.approx(row["flow_m3s"], rel=1e-7)
    assert model == before_model and inputs == before_inputs
    assert result["history"][-1]["flow_exchange_residual_rel"] < 1e-7
    for envelope in result["simhandoff"].values():
        assert validate_envelope(envelope) == []
    outgoing = result["simhandoff"]["handoff_civil_to_piping"]
    incoming = result["simhandoff"]["handoff_piping_to_civil"]
    assert outgoing["conditions"][0]["units"] == "kPa"
    assert outgoing["conditions"][0]["applied_to"] == f"plant:r1:{'start' if direction == 'supply' else 'end'}"
    assert outgoing["geometry"][0]["units"] == "mm"
    assert len(outgoing["meta"]["boundary"]) == 2
    assert incoming["conditions"][0]["units"] == "m3/s"
    assert incoming["conditions"][0]["value"] == pytest.approx(sign * expected_q, rel=3e-7)
    assert incoming["source"]["handoff_id"] == outgoing["id"]
    verify = result["simhandoff"]["verify"]
    assert verify["pass"]
    assert {c["quantity"] for c in verify["checks"]} == {"head:t1", "flow:t1"}
    assert all(c["tolerance_pct"] == pytest.approx(1e-5) for c in verify["checks"])
    json.dumps(result, allow_nan=False)


def test_base_civil_demand_is_additive_not_replaced():
    base = 1e-6
    model, inputs = model_and_inputs(base_demand=base)
    expected_q, _ = analytic_flow(30.0, base)
    result = couple(model, "w1", fixed_resistance, piping_inputs=inputs, tol_rel=1e-7, max_iter=100)
    assert result["converged"]
    assert result["tie_ins"]["t1"]["flow_m3s"] == pytest.approx(expected_q, rel=3e-7)
    assert result["civil_result"]["timesteps"][0]["links"]["feeder"]["flow_m3s"] == pytest.approx(base + expected_q, rel=3e-7)


def test_default_tolerance_is_one_percent_and_max_iteration_is_not_convergence():
    model, inputs = model_and_inputs()
    result = couple(model, "w1", fixed_resistance, piping_inputs=inputs)
    assert result["converged"]
    assert result["simhandoff"]["verify"]["checks"][0]["tolerance_pct"] == 1.0
    limited = couple(model, "w1", fixed_resistance, piping_inputs=inputs, max_iter=1)
    assert limited["ok"] and not limited["converged"]
    assert limited["iterations"] == 1 and len(limited["history"]) == 1
    assert limited["tie_ins"]["t1"]["civil_flow_m3s"] == 0
    assert limited["tie_ins"]["t1"]["flow_m3s"] > 0
    assert not limited["simhandoff"]["verify"]["pass"]
    flow_check = next(c for c in limited["simhandoff"]["verify"]["checks"] if c["quantity"] == "flow:t1")
    assert flow_check["deviation_pct"] == 100.0
    assert limited["warnings"]
    assert all(validate_envelope(env) == [] for env in limited["simhandoff"].values())
    json.dumps(limited, allow_nan=False)


def test_stable_flow_cannot_hide_inconsistent_piping_head():
    model, inputs = model_and_inputs()

    def wrong_head(doc, boundary, fluid):
        result = fixed_resistance(doc, boundary, fluid)
        result["nodes"][0]["pressure_kpa"] += 100.0
        return result

    result = couple(model, "w1", wrong_head, piping_inputs=inputs, max_iter=30)
    assert not result["converged"]
    assert not next(c for c in result["simhandoff"]["verify"]["checks"] if c["quantity"] == "head:t1")["pass"]


def test_exchange_sums_all_incident_run_flows_at_shared_endpoint():
    model, inputs = model_and_inputs()

    def branched_solver(doc, boundary, fluid):
        result = fixed_resistance(doc, boundary, fluid)
        result["nodes"].append({"id": "n2", "xyz": [0, 1000, 10000], "pressure_kpa": 0.0, "demand_m3h": 0.0})
        result["links"].append({"id": "r2", "from": "n0", "to": "n2", "flow_m3h": 0.0036})
        return result

    result = couple(model, "w1", branched_solver, piping_inputs=inputs, max_iter=1)
    first_run_q = result["piping_result"]["links"][0]["flow_m3h"] / 3600.0
    assert result["tie_ins"]["t1"]["flow_m3s"] == pytest.approx(first_run_q + 1e-6)


@pytest.mark.parametrize("change,reason", [
    ("no_ties", "tie_in_not_found"),
    ("missing_node", "tie_in_not_found"),
    ("wrong_network", "tie_in_network_mismatch"),
    ("missing_run", "tie_in_not_found"),
    ("select_missing", "tie_in_not_found"),
    ("select_wrong_network", "tie_in_network_mismatch"),
    ("conflicting_bc", "conflicting_tie_in_boundary"),
    ("eps", "coupling_requires_steady_state"),
    ("pda", "unsupported_demand_model"),
    ("pattern", "unsupported_tie_in_pattern"),
])
def test_named_model_refusals(change, reason):
    model, inputs = model_and_inputs()
    if change == "no_ties":
        model["tie_ins"] = []
    elif change == "missing_node":
        model["tie_ins"][0]["civil_node"] = "absent"
    elif change == "wrong_network":
        model["tie_ins"][0]["civil_network"] = "absent"
    elif change == "missing_run":
        model["tie_ins"][0]["piping_endpoint"]["run_id"] = "absent"
    elif change == "select_missing":
        inputs["tie_in_ids"] = ["absent"]
    elif change == "select_wrong_network":
        model["tie_ins"][0]["civil_network"] = "w2"
        inputs["tie_in_ids"] = ["t1"]
    elif change == "conflicting_bc":
        inputs["boundary"].append({"run": "r1", "end": "start", "pressure": 300.0})
    elif change == "eps":
        model["water_networks"][0]["options"]["duration_s"] = 3600
    elif change == "pda":
        model["water_networks"][0]["options"]["demand_model"] = "PDA"
    elif change == "pattern":
        model["water_networks"][0]["junctions"][0]["pattern"] = "weekday"
    with pytest.raises(CivilError) as exc:
        couple(model, "w1", fixed_resistance, piping_inputs=inputs)
    assert exc.value.reason == reason


@pytest.mark.parametrize("failure,wrapped", [("refusal", "no_pressure_boundary"), ("exception", "plant_offline"), ("bad_reply", "invalid_result"), ("nonconverged", "not_converged")])
def test_solver_failure_preserves_upstream_reason(failure, wrapped):
    model, inputs = model_and_inputs()

    def failed_solver(doc, boundary, fluid):
        if failure == "exception":
            raise CivilError("plant_offline", "No engine available")
        if failure == "bad_reply":
            return []
        if failure == "nonconverged":
            return {"ok": True, "converged": False}
        return {"ok": False, "reason": "no_pressure_boundary"}

    with pytest.raises(CivilError) as exc:
        couple(model, "w1", failed_solver, piping_inputs=inputs)
    assert exc.value.reason == "piping_solver_failed"
    assert exc.value.extra["solver_reason"] == wrapped


@pytest.mark.parametrize("kwargs", [{"relax": 0.0}, {"relax": 1.01}, {"tol_rel": 0.0}, {"tol_rel": float("nan")}, {"max_iter": 0}, {"max_iter": True}])
def test_invalid_iteration_controls_refuse(kwargs):
    model, inputs = model_and_inputs()
    with pytest.raises(CivilError) as exc:
        couple(model, "w1", fixed_resistance, piping_inputs=inputs, **kwargs)
    assert exc.value.reason == "invalid_input"


def test_boundary_missing_timestep_and_network_mismatch_refuse():
    model, _ = model_and_inputs()
    result = {"ok": True, "converged": True, "timesteps": [{"nodes": {"j1": {"head_m": 40.0}}}]}
    with pytest.raises(CivilError) as exc:
        tie_in_boundaries(model, result, timestep=2)
    assert exc.value.reason == "timestep_not_found"
    result["network_id"] = "another_network"
    with pytest.raises(CivilError) as exc:
        tie_in_boundaries(model, result)
    assert exc.value.reason == "tie_in_network_mismatch"


def test_fea_force_and_pressure_units_and_provenance():
    # A 100 kPa pressure on 0.2 m2 gives 20,000 N; vector direction is explicit.
    result = {"ok": True, "force_vector_n": [20000.0, -1000.0, 0.0], "pressure_kpa": 35.0,
              "warnings": [], "assumptions": ["prism earth load"], "method": {"name": "prism", "reference": "gamma*H"}}
    envelope = civil_to_fea_handoff(result, target_doc_id="block", face_selector="face:4")
    assert validate_envelope(envelope) == []
    assert envelope["coupling"] == "civil_to_fea"
    assert envelope["target"]["doc_id"] == "block"
    force, pressure = envelope["conditions"]
    assert force["value"] == [20000.0, -1000.0, 0.0] and force["units"] == "N"
    assert pressure["value"] == 35000.0 and pressure["units"] == "Pa"
    assert force["applied_to"] == pressure["applied_to"] == "face:4"
    assert "force_vector_n" in force["provenance"] and "pressure_kpa" in pressure["provenance"]
    assert envelope["meta"]["source_method"] == result["method"]
    json.dumps(envelope, allow_nan=False)


@pytest.mark.parametrize("result,reason", [
    ({"ok": True, "thrust_kn": 20.0}, "force_direction_required"),
    ({"ok": True, "rigid_pressure_kpa": 30.0, "flexible_pressure_kpa": 20.0}, "load_selection_required"),
    ({"ok": True, "pressure_kpa": -1.0}, "invalid_input"),
    ({"ok": True, "force_vector_n": [1.0, float("inf"), 0.0]}, "invalid_input"),
])
def test_fea_never_guesses_direction_or_buried_load_selection(result, reason):
    with pytest.raises(CivilError) as exc:
        civil_to_fea_handoff(result)
    assert exc.value.reason == reason


def test_fea_unbound_pressure_handoff_discloses_missing_face():
    envelope = civil_to_fea_handoff({"ok": True, "pressure_kpa": 50.0})
    assert envelope["conditions"][0]["value"] == 50000.0
    assert envelope["conditions"][0]["units"] == "Pa"
    assert "applied_to" not in envelope["conditions"][0]
    assert any("unbound" in warning for warning in envelope["warnings"])
    assert validate_envelope(envelope) == []


def test_two_plant_documents_share_civil_node_without_overwriting_demands():
    model, entry = model_and_inputs()
    second = deepcopy(model["tie_ins"][0])
    second.update({"id": "t2", "piping_doc_id": "plant2"})
    model["tie_ins"].append(second)
    inputs = {"plant": deepcopy(entry), "plant2": deepcopy(entry)}
    # Two identical resistances in parallel have total plant coefficient 2*K.
    a = 128.0 * NU * LENGTH / (math.pi * G * DIAMETER**4)
    coefficient = (2.0 * K)**2 * RHO * G / 1000.0
    beta = coefficient * a
    expected_total = (-beta + math.sqrt(beta**2 + 4.0 * coefficient * 30.0)) / 2.0
    result = couple(model, "w1", fixed_resistance, piping_inputs=inputs, tol_rel=1e-7, max_iter=100)
    assert result["converged"]
    assert set(result["piping_result"]) == {"plant", "plant2"}
    for row in result["tie_ins"].values():
        assert row["flow_m3s"] == pytest.approx(expected_total / 2.0, rel=3e-7)
    assert result["civil_result"]["timesteps"][0]["links"]["feeder"]["flow_m3s"] == pytest.approx(expected_total, rel=3e-7)
    assert len(result["simhandoff"]["handoff_civil_to_piping"]["geometry"]) == 2
    assert len(result["simhandoff"]["handoff_piping_to_civil"]["conditions"]) == 2
    assert all(validate_envelope(env) == [] for env in result["simhandoff"].values())


def test_reversed_supply_flow_is_signed_not_absolute():
    model, inputs = model_and_inputs()
    inputs["boundary"][0]["pressure"] = 1000.0
    result = couple(model, "w1", fixed_resistance, piping_inputs=inputs, max_iter=2)
    assert result["tie_ins"]["t1"]["flow_m3s"] < 0
    assert result["tie_ins"]["t1"]["civil_flow_m3s"] < 0
    assert result["civil_result"]["timesteps"][0]["nodes"]["j1"]["demand_m3s"] < 0
    assert any("opposite" in warning for warning in result["warnings"])


def test_zero_flow_has_finite_agreement_and_requires_stability_iteration():
    model, inputs = model_and_inputs()

    def isolated_plant(doc, boundary, fluid):
        result = fixed_resistance(doc, boundary, fluid)
        result["links"][0]["flow_m3h"] = 0.0
        return result

    result = couple(model, "w1", isolated_plant, piping_inputs=inputs)
    assert result["converged"] and result["iterations"] == 2
    assert result["tie_ins"]["t1"]["flow_m3s"] == 0.0
    flow = next(c for c in result["simhandoff"]["verify"]["checks"] if c["quantity"] == "flow:t1")
    assert flow["source_value"] == flow["target_value"] == flow["deviation_pct"] == 0
    assert flow["pass"]
    json.dumps(result, allow_nan=False)


def test_tiny_relaxation_never_hides_exchange_residual():
    model, inputs = model_and_inputs()
    result = couple(model, "w1", fixed_resistance, piping_inputs=inputs, relax=1e-5, max_iter=3)
    assert not result["converged"]
    assert result["history"][-1]["flow_exchange_residual_rel"] > 0.99


def test_omitted_fluid_is_explicitly_passed_and_disclosed():
    model, inputs = model_and_inputs()
    del inputs["fluid"]
    received = []

    def capture(doc, boundary, fluid):
        received.append(fluid)
        return fixed_resistance(doc, boundary, fluid)

    result = couple(model, "w1", capture, piping_inputs=inputs, max_iter=1)
    assert received == [{"name": "water", "rho": 1000.0, "mu": 0.001004, "phase": "liquid"}]
    assert result["civil_result"]["density_kgm3"] == 1000.0
    assert any("Freshwater" in assumption for assumption in result["assumptions"])


def test_nonfinite_native_result_refuses_instead_of_serializing_nan():
    model, inputs = model_and_inputs()

    def nonfinite(doc, boundary, fluid):
        result = fixed_resistance(doc, boundary, fluid)
        result["links"][0]["flow_m3h"] = float("nan")
        return result

    with pytest.raises(CivilError) as exc:
        couple(model, "w1", nonfinite, piping_inputs=inputs)
    assert exc.value.reason == "piping_solver_failed"
    assert exc.value.extra["solver_reason"] == "invalid_result"
