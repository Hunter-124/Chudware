"""Offline equation/continuity/criteria V&V; no SWMM daemon dependency.

SWMM fixture provenance is in data/swmm_steady.inp: pyswmm 2.2, EPA SWMM
5.2.4, CMS KINWAVE, three 0.3 m pipes, 100 m, n=.013, S=.004, 12 h.
Closed forms follow Chow (1959) and HEC-22 4th ed. ch. 6/9.
"""
from copy import deepcopy
import json
import math
from pathlib import Path

import pytest

from . import gravity
from .errors import CivilError
from .swmm_inp import import_inp

CIRCLE = {"shape": "circular", "diameter": 0.3}
RECT = {"shape": "rectangular", "width": 2.0}
TRAP = {"shape": "trapezoidal", "width": 2.0, "side_slope": 2.0}


def _network(q=0.01, slope=0.004, tailwater=None):
    return {"id": "g", "name": "test", "kind": "sanitary",
            "nodes": [{"id": "A", "type": "manhole", "x": 0, "y": 0, "invert": 10 + 100 * slope,
                       "rim": 12, "inflow_m3s": q, "loss_k": 0},
                      {"id": "O", "type": "outfall", "x": 100, "y": 0, "invert": 10,
                       "rim": 11, "inflow_m3s": 0, "outfall_tailwater": tailwater, "loss_k": 0}],
            "conduits": [{"id": "P", "from": "A", "to": "O", **CIRCLE,
                          "length": 100, "n": .013, "vertices": []}]}


def test_circular_half_full_exact_geometry():
    # θ=π: A=πD²/8, P=πD/2, R=D/4, T=D.
    geometry = gravity.partial_flow(CIRCLE, .15)
    assert geometry["area_m2"] == pytest.approx(math.pi * .3 ** 2 / 8, rel=1e-13)
    assert geometry["wetted_perimeter_m"] == pytest.approx(math.pi * .3 / 2, rel=1e-13)
    assert geometry["hydraulic_radius_m"] == pytest.approx(.3 / 4, rel=1e-13)
    assert geometry["top_width_m"] == pytest.approx(.3, rel=1e-13)


def test_full_capacity_closed_form_and_rounded_si_coefficient():
    result = gravity.full_flow_capacity(CIRCLE, .004, .013)
    exact = math.pi * .3 ** 2 / 4 / .013 * (.3 / 4) ** (2 / 3) * math.sqrt(.004)
    rounded = .312 / .013 * .3 ** (8 / 3) * math.sqrt(.004)
    assert result["flow_m3s"] == pytest.approx(exact, rel=1e-13)
    assert result["flow_m3s"] == pytest.approx(rounded, rel=.002)
    assert result["velocity_ms"] == pytest.approx(exact / (math.pi * .3 ** 2 / 4))


def test_circular_maximum_capacity_and_declining_branch():
    capacity = gravity.full_flow_capacity(CIRCLE, .004, .013)
    assert capacity["max_depth_m"] / .3 == pytest.approx(.93818, abs=2e-5)
    assert capacity["max_flow_m3s"] / capacity["flow_m3s"] == pytest.approx(1.0757, abs=2e-4)
    deeper = gravity.manning_flow(CIRCLE, .995 * .3, .004, .013)
    assert deeper["flow_m3s"] < capacity["max_flow_m3s"]
    normal = gravity.normal_depth(CIRCLE, 1.03 * capacity["flow_m3s"], .004, .013)
    assert normal["depth_m"] < capacity["max_depth_m"]


@pytest.mark.parametrize("ratio", [.01, .1, .5, .75, .9])
def test_circular_normal_depth_inverse(ratio):
    depth = .3 * ratio
    flow = gravity.manning_flow(CIRCLE, depth, .004, .013)["flow_m3s"]
    assert gravity.normal_depth(CIRCLE, flow, .004, .013)["depth_m"] == pytest.approx(depth, abs=2e-10)


def test_capacity_exceedance_refusal_reports_both_capacities():
    capacity = gravity.full_flow_capacity(CIRCLE, .004, .013)
    with pytest.raises(CivilError) as caught:
        gravity.normal_depth(CIRCLE, capacity["max_flow_m3s"] * 1.001, .004, .013)
    assert caught.value.reason == "exceeds_capacity"
    assert caught.value.extra["full_flow_m3s"] == capacity["flow_m3s"]
    assert caught.value.extra["max_flow_m3s"] == capacity["max_flow_m3s"]


def test_tiny_circular_depth_is_positive_and_finite():
    geometry = gravity.partial_flow(CIRCLE, 1e-12)
    # Near-invert circular segment: A~(4/3)sqrt(D)y^(3/2).
    expected = 4 / 3 * math.sqrt(.3) * (1e-12) ** 1.5
    assert geometry["area_m2"] == pytest.approx(expected, rel=1e-7)
    assert geometry["hydraulic_radius_m"] > 0


def test_rectangular_and_trapezoidal_geometry():
    rect = gravity.partial_flow(RECT, 1)
    assert rect["area_m2"] == 2
    assert rect["wetted_perimeter_m"] == 4
    assert rect["hydraulic_radius_m"] == .5
    trap = gravity.partial_flow(TRAP, 1)
    assert trap["area_m2"] == 4
    assert trap["wetted_perimeter_m"] == pytest.approx(2 + 2 * math.sqrt(5))
    assert trap["top_width_m"] == 6


def test_rectangular_closed_roof_full_geometry():
    shape = {**RECT, "height": 1, "closed": True}
    full = gravity.partial_flow(shape, 1)
    assert full["wetted_perimeter_m"] == 6
    assert full["hydraulic_radius_m"] == pytest.approx(1 / 3)
    assert full["top_width_m"] == 0
    cap = gravity.full_flow_capacity(shape, .01, .02)
    assert cap["flow_m3s"] == pytest.approx(2 * (1 / 3) ** (2 / 3) * .1 / .02)
    assert cap["max_flow_m3s"] > cap["flow_m3s"]


@pytest.mark.parametrize("shape", [RECT, TRAP, {"shape": "trapezoidal", "width": 0, "side_slope": 2}])
def test_non_circular_normal_depth_inverse(shape):
    flow = gravity.manning_flow(shape, .7, .002, .025)["flow_m3s"]
    assert gravity.normal_depth(shape, flow, .002, .025)["depth_m"] == pytest.approx(.7, abs=1e-10)


def test_rectangular_critical_depth_closed_form_and_froude():
    # Q=4, b=2: unit discharge q=2; yc=(4/g)^(1/3).
    expected = (4 / gravity.G) ** (1 / 3)
    result = gravity.critical_depth(RECT, 4)
    assert result["depth_m"] == pytest.approx(expected, rel=1e-13)
    assert gravity.froude(RECT, expected, 4)["froude"] == pytest.approx(1, rel=1e-12)
    assert gravity.froude(RECT, expected * 2, 4)["regime"] == "subcritical"
    assert gravity.froude(RECT, expected / 2, 4)["regime"] == "supercritical"


@pytest.mark.parametrize("shape,flow", [(CIRCLE, .01), (TRAP, 4), ({"shape": "trapezoidal", "width": 0, "side_slope": 1.5}, 2)])
def test_critical_depth_satisfies_specific_energy_condition(shape, flow):
    y = gravity.critical_depth(shape, flow)["depth_m"]
    geometry = gravity.partial_flow(shape, y)
    assert flow ** 2 * geometry["top_width_m"] / (gravity.G * geometry["area_m2"] ** 3) == pytest.approx(1, rel=1e-8)


def test_rectangular_jump_momentum_and_energy_loss():
    # b=2, y1=.2, Q=2; exact rectangular conjugate-depth equation.
    flow, y1 = 2, .2
    fr = flow / (2 * y1 * math.sqrt(gravity.G * y1))
    y2 = y1 * .5 * (math.sqrt(1 + 8 * fr ** 2) - 1)
    result = gravity.hydraulic_jump(RECT, flow, y1)
    assert result["conjugate_depth_m"] == pytest.approx(y2, rel=1e-13)
    assert result["energy_loss_m"] == pytest.approx((y2 - y1) ** 3 / (4 * y1 * y2))
    # Specific force per unit width must be continuous through the jump.
    q = flow / 2
    assert y1 ** 2 / 2 + q ** 2 / (gravity.G * y1) == pytest.approx(y2 ** 2 / 2 + q ** 2 / (gravity.G * y2))
    assert result["froude_downstream"] < 1


def test_m1_profile_approaches_normal_depth_upstream_and_energy_balances():
    shape = {"shape": "rectangular", "width": 4}
    yn = gravity.normal_depth(shape, 5, .001, .03)["depth_m"]
    # Independent differential-GVF quadrature (Chow 1959):
    # L=∫[(1-Fr²)/(S-Sf)]dy from 1.1yn to 2yn = 2032.992176460 m.
    # The original 2000 m reach cannot meet 10% decay: y=1.508379678920266 m.
    # At 2500 m, independently integrated y=1.419696641022611 m.
    result = gravity.standard_step(shape, 5, .03, .001, 2500, 2 * yn, "upstream", 250)
    points = result["profile"]
    assert result["classification"] == "M1"
    assert all(yn < p["depth_m"] <= 2 * yn for p in points)
    assert all(b["depth_m"] < a["depth_m"] for a, b in zip(points, points[1:]))
    assert points[-1]["depth_m"] - yn < .1 * (points[0]["depth_m"] - yn)
    assert points[-1]["x_m"] == pytest.approx(-2500)
    assert points[-1]["depth_m"] == pytest.approx(1.419696641022611, abs=2e-6)
    for a, b in zip(points, points[1:]):
        slopes = []
        for p in (a, b):
            geometry = gravity.partial_flow(shape, p["depth_m"])
            slopes.append((.03 * 5 / (geometry["area_m2"] * geometry["hydraulic_radius_m"] ** (2 / 3))) ** 2)
        expected_loss = .5 * sum(slopes) * 10
        assert b["egl_m"] - a["egl_m"] == pytest.approx(expected_loss, abs=1e-8)


@pytest.mark.parametrize("slope,factor,classification,direction", [(.001, .9, "M2", "upstream"), (.02, 1.8, "S1", "upstream"), (.02, 1.2, "S2", "downstream")])
def test_standard_step_other_supported_profile_classes(slope, factor, classification, direction):
    shape = {"shape": "rectangular", "width": 3}
    yn = gravity.normal_depth(shape, 3, slope, .02)["depth_m"]
    yc = gravity.critical_depth(shape, 3)["depth_m"]
    control = factor * yn if classification.startswith("M") else factor * yc if classification == "S1" else (yn + yc) / 2
    result = gravity.standard_step(shape, 3, .02, slope, 5, control, direction, 20)
    assert result["classification"] == classification
    assert result["max_energy_residual_m"] < 1e-8


def test_steady_swmm_reference_normal_depths_and_network_continuity():
    fixture = Path(__file__).parent / "data" / "swmm_steady.inp"
    network = import_inp(fixture.read_text())["network"]
    result = gravity.analyze(network)
    # Independently observed SWMM 5.2.4 outputs, not generated by this solver.
    expected = {"C1": (.004, .05190444804532634), "C2": (.006, .06342327783525785), "C3": (.012, .09008185480236247)}
    for key, (flow, depth) in expected.items():
        assert result["conduits"][key]["flow_m3s"] == pytest.approx(flow, abs=1e-12)
        assert result["conduits"][key]["normal_depth_m"] == pytest.approx(depth, rel=.02)
    assert result["total_inflow_m3s"] == pytest.approx(.012)
    assert result["total_outflow_m3s"] == pytest.approx(.012)
    assert result["nodes"]["J3"]["flow_m3s"] == pytest.approx(.004 + .006 + .002)
    json.dumps(result, allow_nan=False)


def test_optional_node_and_conduit_catchment_peaks_add_downstream():
    fixture = Path(__file__).parent / "data" / "swmm_steady.inp"
    network = import_inp(fixture.read_text())["network"]
    result = gravity.analyze(network, flows={"J1": .001, "C2": .002, "J3": .003})
    assert result["conduits"]["C1"]["flow_m3s"] == pytest.approx(.005)
    assert result["conduits"]["C2"]["flow_m3s"] == pytest.approx(.008)
    assert result["conduits"]["C3"]["flow_m3s"] == pytest.approx(.018)
    assert result["total_inflow_m3s"] == pytest.approx(result["total_outflow_m3s"])
    assert any("simultaneous" in a for a in result["assumptions"])


def test_ten_states_minimum_slope_violation_flagged():
    network = _network(slope=.0001)
    network["conduits"][0]["diameter"] = .2
    result = gravity.analyze(network, criteria={"profile": "ten_states_2014", "overrides": {}})
    slope = next(r for r in result["checks"] if r["check"] == "minimum_slope")
    assert not slope["pass"]
    assert slope["value"] == pytest.approx(.0001)
    assert slope["limit"] > .0001
    assert "reference" in slope and slope["reference"]


def test_tailwater_surcharge_flooding_and_full_section_friction():
    network = _network(tailwater=2)
    result = gravity.analyze(network)
    pipe = result["conduits"]["P"]
    assert pipe["surcharged"]
    assert result["nodes"]["O"]["flooding"]
    assert result["nodes"]["A"]["flooding"]
    area = math.pi * .3 ** 2 / 4
    expected_sf = (.013 * .01 / (area * (.3 / 4) ** (2 / 3))) ** 2
    assert pipe["friction_slope"] == pytest.approx(expected_sf, rel=1e-12)
    assert pipe["headloss_m"] == pytest.approx(expected_sf * 100)
    assert pipe["egl_upstream_m"] - pipe["egl_downstream_m"] == pytest.approx(expected_sf * 100)


def test_overcapacity_network_is_surcharged_not_false_normal_depth():
    network = _network(q=.2)
    result = gravity.analyze(network)
    assert result["conduits"]["P"]["normal_depth_m"] is None
    assert result["conduits"]["P"]["exceeds_capacity"]
    assert result["conduits"]["P"]["surcharged"]


def test_explicit_junction_k_uses_outlet_velocity_head():
    network = _network(tailwater=2)
    network["nodes"][1]["loss_k"] = 2.5
    result = gravity.analyze(network)
    pipe = result["conduits"]["P"]
    velocity = .01 / (math.pi * .3 ** 2 / 4)
    expected = 2.5 * velocity ** 2 / (2 * gravity.G)
    assert pipe["junction_loss_m"] == pytest.approx(expected)
    assert pipe["egl_upstream_m"] - pipe["egl_downstream_m"] == pytest.approx(pipe["headloss_m"] + expected)


def test_terrain_cover_checks_midspan_not_only_rims():
    network = _network()
    result = gravity.analyze(network, z_at=lambda x, y: 10.8 if 40 <= x <= 60 else 13)
    cover = next(r for r in result["checks"] if r["check"] == "minimum_cover")
    assert cover["value"] == pytest.approx(.26)
    assert not cover["pass"]


def test_plan_polyline_length_and_explicit_inverts_are_honored():
    network = _network()
    pipe = network["conduits"][0]
    pipe.update(length=None, vertices=[[50, 30]], inlet_invert=11, outlet_invert=10)
    result = gravity.analyze(network)
    length = 2 * math.hypot(50, 30)
    assert result["conduits"]["P"]["length_m"] == pytest.approx(length)
    assert result["conduits"]["P"]["slope"] == pytest.approx(1 / length)


def test_design_picks_smallest_feasible_standard_and_does_not_mutate():
    network = _network(q=.025, slope=.004)
    original = deepcopy(network)
    profile = {"profile": "ten_states_2014", "overrides": {"gravity_max_depth_ratio": .5}}
    result = gravity.design_sizes(network, {}, profile, [.4, .2, .3, .25])
    # Half-full circular capacity is exactly .5 Qfull. Qfull for .3 m is
    # about .06116, so .3 passes; .25 m Qhalf is only about .0188 and fails.
    assert result["sizes"]["P"]["diameter_m"] == .3
    assert result["sizes"]["P"]["depth_ratio"] < .5
    assert network == original


def test_crown_matching_warning_and_simplified_straight_manhole_coefficient():
    network = _network()
    network["nodes"][0]["invert"] = 10.8
    network["nodes"].insert(1, {"id": "M", "type": "manhole", "x": 100, "y": 0,
                               "invert": 10.4, "rim": 12, "inflow_m3s": 0})
    network["nodes"][-1]["x"] = 200
    first = network["conduits"][0]
    first.update(to="M", diameter=.2)
    network["conduits"].append({**first, "id": "P2", "from": "M", "to": "O", "diameter": .3})
    result = gravity.analyze(network)
    crown = next(row for row in result["checks"] if row["check"] == "manhole_crown_drop")
    assert crown["value"] == pytest.approx(-.1)
    assert not crown["pass"]
    assert any("outgoing crown is above incoming crown" in warning for warning in result["warnings"])
    # HEC-22 Table 9.4 straight-run access hole: K=.15, alpha=1.
    assert result["conduits"]["P"]["junction_loss_k"] == .15
    outlet_v = result["conduits"]["P2"]["hydraulic_velocity_ms"]
    assert result["conduits"]["P"]["junction_loss_m"] == pytest.approx(.15 * outlet_v ** 2 / (2 * gravity.G))


@pytest.mark.parametrize("call,reason", [
    (lambda: gravity.partial_flow(CIRCLE, -.1), "invalid_input"),
    (lambda: gravity.partial_flow(CIRCLE, .31), "depth_exceeds_section"),
    (lambda: gravity.partial_flow({"shape": "egg"}, .1), "unsupported_shape"),
    (lambda: gravity.normal_depth(CIRCLE, -.1, .004, .013), "invalid_input"),
    (lambda: gravity.manning_flow(CIRCLE, .1, 0, .013), "invalid_input"),
    (lambda: gravity.manning_flow(CIRCLE, .1, .01, 0), "invalid_input"),
    (lambda: gravity.manning_flow(CIRCLE, .1, None, .013), "invalid_input"),
    (lambda: gravity.manning_flow(CIRCLE, .1, .004, None), "invalid_input"),
    (lambda: gravity.analyze(_network(), flows=[]), "invalid_flows"),
    (lambda: gravity.critical_depth({**RECT, "height": .1}, 4), "critical_depth_exceeds_section"),
    (lambda: gravity.hydraulic_jump(RECT, .1, 1), "jump_requires_supercritical_flow"),
    (lambda: gravity.standard_step(RECT, 2, .02, .001, 100, 2, "downstream", 20), "unstable_profile_direction"),
    (lambda: gravity.analyze(_network(slope=-.001)), "adverse_slope"),
    (lambda: gravity.analyze(_network(), flows={"missing": .1}), "not_found"),
    (lambda: gravity.analyze(_network(), z_at=lambda x, y: None), "terrain_outside_surface"),
    (lambda: gravity.design_sizes(_network(q=.2), {}, None, [.2]), "design_no_feasible_size"),
])
def test_named_refusals(call, reason):
    with pytest.raises(CivilError) as caught:
        call()
    assert caught.value.reason == reason


@pytest.mark.parametrize("flow,tail,reason", [(10, None, "open_channel_exceeds_capacity"), (.01, 2, "open_channel_overtopping")])
def test_open_channels_are_not_falsely_replaced_by_pressurized_pipes(flow, tail, reason):
    network = _network(q=flow, tailwater=tail)
    network["conduits"][0].update(shape="rectangular", width=.2, height=.3, closed=False)
    with pytest.raises(CivilError) as caught:
        gravity.analyze(network)
    assert caught.value.reason == reason


def test_cycle_and_split_are_refused_without_duplicating_flow():
    network = _network()
    network["conduits"].append({**network["conduits"][0], "id": "BACK", "from": "O", "to": "A"})
    with pytest.raises(CivilError) as caught:
        gravity.analyze(network)
    assert caught.value.reason == "cycle_in_gravity_network"
    network = _network()
    network["conduits"].append({**network["conduits"][0], "id": "SECOND"})
    with pytest.raises(CivilError) as caught:
        gravity.analyze(network)
    assert caught.value.reason == "unsupported_flow_split"


def test_explicit_adverse_slope_permission_does_not_invent_uniform_depth():
    network = _network(slope=-.001, tailwater=2)
    network["conduits"][0]["allow_adverse_slope"] = True
    result = gravity.analyze(network)
    assert result["conduits"]["P"]["normal_depth_m"] is None
    assert result["conduits"]["P"]["capacity_m3s"] is None
    assert any("no uniform normal depth" in w for w in result["warnings"])
