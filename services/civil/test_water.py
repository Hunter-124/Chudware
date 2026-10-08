"""Deterministic independent V&V for civil hydraulics; no EPANET runtime needed.

Recorded fixture source: WNTR 1.5.0 EpanetSimulator, compiled EPANET 2.2.0
(ENgetversion 20200), input/engine hashes and options in data/*_reference.json.
Hand formulas: EPA EPANET 2.2 User Manual §§3.1, 12.1 and Appendix D.
"""
from copy import deepcopy
import json
import math
from pathlib import Path

import pytest

from .errors import CivilError
from . import water, epanet_inp

_DATA = Path(__file__).with_name("data")
_HW_EPANET = 10.666829488930048


def _pipe(ident, start, end, *, length=1000.0, diameter=0.2, roughness=130.0, **extra):
    return {"id": ident, "from": start, "to": end, "length": length,
            "diameter": diameter, "roughness": roughness, "minor_loss": 0,
            "status": "open", **extra}


def _single(*, flow=0.01, formula="H-W", roughness=130.0):
    return {"id": "single", "name": "Single pipe", "headloss": formula,
            "junctions": [{"id": "J", "elevation": 2.0, "demand_m3s": flow}],
            "reservoirs": [{"id": "R", "head": 60.0}], "tanks": [],
            "pipes": [_pipe("P", "R", "J", roughness=roughness)],
            "pumps": [], "valves": [], "patterns": {},
            "options": {"accuracy": 1e-9, "max_iter": 200}}


def _first(network):
    result = water.solve(network)
    assert result["converged"], result
    return result["timesteps"][0]


def _continuity(network, timestep, tolerance=1e-8):
    # Independent incidence balance: supply minus withdrawal at EACH junction.
    links = network.get("pipes", [])+network.get("pumps", [])+network.get("valves", [])
    for node in network["junctions"]:
        ident = node["id"]
        incoming = sum(timestep["links"][link["id"]]["flow_m3s"]*((link["to"] == ident)-(link["from"] == ident)) for link in links)
        assert incoming == pytest.approx(timestep["nodes"][ident]["demand_delivered_m3s"], abs=tolerance)


def _fixture(name):
    network = epanet_inp.read(_DATA/f"epanet_{name}.inp")["network"]
    reference = json.loads((_DATA/f"epanet_{name}_reference.json").read_text())
    return network, reference


def test_single_pipe_hazen_williams_hand_calculation():
    net = _single()
    ts = _first(net)
    # 10.667 L Q^1.852 / (C^1.852 D^4.871) = 0.6511815658908212 m.
    # EPANET's exact converted US coefficient is 10.666829488930048;
    # 10.667 is rounded to five significant figures (relative difference 1.6e-5).
    hf = 10.667*1000*0.01**1.852/(130**1.852*0.2**4.871)
    assert hf == pytest.approx(0.6511815658908212, abs=1e-14)
    assert ts["links"]["P"]["headloss_m"] == pytest.approx(hf, rel=2e-5)
    assert ts["nodes"]["J"]["head_m"] == pytest.approx(60-hf, abs=2e-5)
    assert ts["nodes"]["J"]["pressure_m"] == pytest.approx(58-hf, abs=2e-5)
    _continuity(net, ts)


def test_single_pipe_darcy_weisbach_swamee_jain_hand_calculation():
    net = _single(formula="D-W", roughness=0.0001)
    ts = _first(net)
    nu, q, diameter, length = 1.004e-6, 0.01, 0.2, 1000
    re = 4*q/(math.pi*diameter*nu)
    f = 0.25/math.log10(0.0001/diameter/3.7+5.74/re**0.9)**2
    hf = f*length/diameter*(4*q/(math.pi*diameter**2))**2/(2*9.80665)
    assert re == pytest.approx(63408.34386131289)
    assert hf == pytest.approx(0.5623496294725638, abs=1e-14)
    assert ts["links"]["P"]["headloss_m"] == pytest.approx(hf, rel=1e-7)
    _continuity(net, ts)


def test_darcy_laminar_hagen_poiseuille_and_zero_flow():
    net = _single(flow=1e-6, formula="D-W", roughness=0.0001)
    ts = _first(net)
    # 128 nu L Q / (pi g D^4), Re=6.340834386, independent laminar limit.
    hf = 128*1.004e-6*1000*1e-6/(math.pi*9.80665*0.2**4)
    assert hf == pytest.approx(2.6070727575963317e-6, abs=1e-18)
    assert ts["links"]["P"]["headloss_m"] == pytest.approx(hf, abs=1e-8)
    net["junctions"][0]["demand_m3s"] = 0
    zero = _first(net)
    assert zero["links"]["P"]["flow_m3s"] == pytest.approx(0, abs=1e-12)
    assert zero["nodes"]["J"]["head_m"] == pytest.approx(60, abs=1e-8)


def test_darcy_transition_endpoint_continuity():
    # Check the EPANET Dunlop transition boundaries with independent endpoint
    # formulas. These directly guard the consumer pipe-head results around Re.
    for re in (1999.999, 2000, 2000.001, 3999.999, 4000, 4000.001):
        q = re*math.pi*0.2*1.004e-6/4
        ts = _first(_single(flow=q, formula="D-W", roughness=0.0001))
        target_re = 2000 if re < 3000 else 4000
        f = 64/2000 if target_re == 2000 else 0.25/math.log10(0.0005/3.7+5.74/4000**0.9)**2
        endpoint_q = target_re*math.pi*0.2*1.004e-6/4
        endpoint_hf = f*1000/0.2*(4*endpoint_q/(math.pi*0.2**2))**2/(2*9.80665)
        assert ts["links"]["P"]["headloss_m"] == pytest.approx(endpoint_hf, rel=2e-5)


def test_minor_losses_add_velocity_head_and_signed_injection():
    net = _single(flow=-0.01)
    net["pipes"][0]["minor_loss"] = 3.0
    ts = _first(net)
    velocity = 0.01/(math.pi*0.2**2/4)
    friction = _HW_EPANET*1000*0.01**1.852/(130**1.852*0.2**4.871)
    assert ts["links"]["P"]["headloss_m"] == pytest.approx(-friction-3*velocity**2/(2*9.80665), abs=1e-7)
    assert ts["nodes"]["J"]["head_m"] > 60
    _continuity(net, ts)


def test_epanet_net1_steady_heads_flows_and_continuity():
    net, fixture = _fixture("net1")
    net["options"]["duration_s"] = 0
    ts = _first(net)
    ref = fixture["timesteps"][0]
    for ident, node in ref["nodes"].items():
        assert ts["nodes"][ident]["head_m"] == pytest.approx(node["head_m"], abs=0.01), ident
    for ident, link in ref["links"].items():
        assert ts["links"][ident]["flow_m3s"] == pytest.approx(link["flow_m3s"], rel=0.001, abs=1e-8), ident
    _continuity(net, ts)


def test_epanet_net1_24_hour_tank_levels_and_controls():
    net, fixture = _fixture("net1")
    result = water.solve(net)
    assert result["converged"]
    for ref in fixture["timesteps"]:
        # Hydraulic grid remains hour-aligned after fractional-second events.
        ts = next(s for s in result["timesteps"] if abs(s["t_s"]-ref["time_s"]) < 1e-6)
        assert ts["tanks"]["2"]["level_m"] == pytest.approx(ref["tanks"]["2"]["level_m"], abs=0.05)
        assert ts["links"]["9"]["status"] == ref["links"]["9"]["status"]
        _continuity(net, ts)
    assert any(abs(s["t_s"]/3600-round(s["t_s"]/3600)) > 1e-5 for s in result["timesteps"])


def test_symmetric_loop_reference_and_hand_invariant():
    net, fixture = _fixture("loop")
    ts = _first(net)
    ref = fixture["timesteps"][0]
    for ident in ("A", "B"):
        assert ts["nodes"][ident]["head_m"] == pytest.approx(ref["nodes"][ident]["head_m"], abs=1e-5)
    assert ts["nodes"]["A"]["head_m"] == pytest.approx(ts["nodes"]["B"]["head_m"], abs=1e-9)
    assert ts["links"]["AB"]["flow_m3s"] == pytest.approx(0, abs=1e-9)
    for ident in ("RA", "RB"):
        assert ts["links"][ident]["flow_m3s"] == pytest.approx(0.005, abs=1e-9)
    _continuity(net, ts)


def test_pressure_driven_deficient_case_epanet_reference_and_wagner():
    net, fixture = _fixture("pda")
    ts = _first(net)
    ref = fixture["timesteps"][0]["nodes"]["J"]
    node = ts["nodes"]["J"]
    assert node["head_m"] == pytest.approx(ref["head_m"], abs=1e-4)
    # The recorded input accuracy is 1e-6, not the engine default .001.
    # EPANET also rounds LPSperCFS to 28.317 (v2.2 src/types.h), whereas
    # civil uses exact 1000*.3048³. That 5.418e-6 input-flow conversion
    # discrepancy bounds the reference comparison; do not weaken the exact
    # Wagner law or continuity assertions against our own state below.
    conversion_rel = abs(28.317/(1000*0.3048**3)-1)
    reference_rel = conversion_rel+fixture["input_options"]["hydraulic"]["accuracy"]
    assert node["demand_delivered_m3s"] == pytest.approx(ref["delivered_demand_m3s"], rel=reference_rel)
    assert node["demand_delivered_m3s"] == pytest.approx(0.01*math.sqrt(node["pressure_m"]/20), abs=1e-10)
    delivered = node["demand_delivered_m3s"]
    exact_si_loss = _HW_EPANET*1000*delivered**1.852/(130**1.852*0.1**4.871)
    assert node["head_m"] == pytest.approx(10-exact_si_loss, abs=1e-6)
    _continuity(net, ts)


@pytest.mark.parametrize("reservoir_head,expected", [(-1, 0), (60, 0.01)])
def test_pressure_driven_clipped_limits(reservoir_head, expected):
    net = _single()
    net["reservoirs"][0]["head"] = reservoir_head
    net["options"]["demand_model"] = "PDA"
    ts = _first(net)
    assert ts["nodes"]["J"]["demand_delivered_m3s"] == pytest.approx(expected, abs=1e-10)
    _continuity(net, ts)


def test_prv_holds_downstream_and_opens_when_source_deficient():
    net, fixture = _fixture("prv")
    ts = _first(net)
    assert ts["nodes"]["B"]["pressure_m"] == pytest.approx(20, abs=1e-8)
    assert ts["nodes"]["A"]["head_m"] == pytest.approx(fixture["timesteps"][0]["nodes"]["A"]["head_m"], abs=1e-5)
    assert ts["links"]["V"]["status"] == "active"
    _continuity(net, ts)
    net["reservoirs"][0]["head"] = 10
    deficient = _first(net)
    assert deficient["links"]["V"]["status"] == "open"
    assert deficient["nodes"]["B"]["pressure_m"] < 10
    _continuity(net, deficient)


def test_psv_sustains_upstream_pressure():
    net = {"headloss": "H-W", "junctions": [{"id": "A", "elevation": 0, "demand_m3s": 0},
                                               {"id": "B", "elevation": 0, "demand_m3s": 0.005}],
           "reservoirs": [{"id": "R", "head": 60}, {"id": "S", "head": 0}],
           "pipes": [_pipe("RA", "R", "A"), _pipe("BS", "B", "S")],
           "valves": [{"id": "V", "from": "A", "to": "B", "type": "PSV", "diameter": 0.2, "setting": 40}],
           "options": {"accuracy": 1e-9}}
    ts = _first(net)
    assert ts["nodes"]["A"]["pressure_m"] == pytest.approx(40, abs=1e-7)
    assert ts["links"]["V"]["status"] == "active"
    assert ts["nodes"]["B"]["head_m"] < 40
    _continuity(net, ts)


def test_fcv_flow_constraint_and_tcv_loss():
    net = {"headloss": "H-W", "junctions": [{"id": "A", "elevation": 0, "demand_m3s": 0}],
           "reservoirs": [{"id": "R", "head": 60}, {"id": "S", "head": 0}],
           "pipes": [_pipe("AS", "A", "S")],
           "valves": [{"id": "V", "from": "R", "to": "A", "type": "FCV", "diameter": 0.2, "setting": 0.005}]}
    ts = _first(net)
    assert ts["links"]["V"]["flow_m3s"] == pytest.approx(0.005, abs=1e-10)
    assert ts["links"]["V"]["status"] == "active"
    assert ts["nodes"]["A"]["head_m"] == pytest.approx(_HW_EPANET*1000*0.005**1.852/(130**1.852*0.2**4.871), abs=1e-7)
    _continuity(net, ts)
    tcv = _single()
    tcv["pipes"] = []
    tcv["valves"] = [{"id": "V", "from": "R", "to": "J", "type": "TCV", "diameter": 0.2, "setting": 8, "minor_loss": 2}]
    valve = _first(tcv)
    assert valve["links"]["V"]["headloss_m"] == pytest.approx(10*(0.01/(math.pi*0.2**2/4))**2/(2*9.80665), abs=1e-7)
    _continuity(tcv, valve)
    tcv["valves"][0]["status"] = "open"
    opened = _first(tcv)
    assert opened["links"]["V"]["headloss_m"] == pytest.approx(2*(0.01/(math.pi*0.2**2/4))**2/(2*9.80665), abs=1e-7)
    _continuity(tcv, opened)


@pytest.mark.parametrize("curve,speed,flow,gain", [
    ([[0, 40], [0.01, 35], [0.02, 25], [0.03, 10]], 1, 0.015, 30),
    ([[0, 40], [0.01, 35], [0.02, 25], [0.03, 10]], 1.5, 0.015, 78.75),
    ([[0, 40], [0.01, 30], [0.02, 0]], 1, 0.01, 30),
    ([[0.01, 30]], 1, 0.01, 30),
])
def test_pump_curves_and_affinity_laws(curve, speed, flow, gain):
    net = _single(flow=flow)
    net["pipes"] = []
    net["pumps"] = [{"id": "P", "from": "R", "to": "J", "curve": curve, "speed": speed}]
    ts = _first(net)
    assert ts["nodes"]["J"]["head_m"] == pytest.approx(60+gain, abs=1e-7)
    assert ts["links"]["P"]["headloss_m"] == pytest.approx(-gain, abs=1e-7)
    _continuity(net, ts)


def test_pump_shutoff_and_check_valve_block_reverse_flow():
    net = {"headloss": "H-W", "reservoirs": [{"id": "L", "head": 0}, {"id": "H", "head": 100}],
           "junctions": [], "pipes": [],
           "pumps": [{"id": "P", "from": "L", "to": "H", "curve": [[0.01, 30]]}]}
    ts = _first(net)
    assert ts["links"]["P"]["flow_m3s"] == 0
    assert ts["links"]["P"]["status"] == "head_closed"
    net["pumps"] = []
    net["pipes"] = [_pipe("CV", "L", "H", status="cv")]
    cv = _first(net)
    assert cv["links"]["CV"]["flow_m3s"] == 0
    assert cv["links"]["CV"]["status"] == "closed"


def test_eps_patterns_reservoir_head_and_additive_demands():
    net = _single()
    net["junctions"][0]["demand_m3s"] = 0
    net["junctions"][0]["demands"] = [{"demand_m3s": 0.005, "pattern": "d"}, {"demand_m3s": 0.005, "pattern": None}]
    net["patterns"] = {"d": [1, 3], "r": [1, 2]}
    net["reservoirs"][0]["pattern"] = "r"
    net["options"].update(duration_s=3600, hydraulic_step_s=3600, pattern_step_s=3600)
    result = water.solve(net)
    assert result["converged"]
    assert result["timesteps"][0]["nodes"]["J"]["demand_m3s"] == pytest.approx(0.01)
    assert result["timesteps"][1]["nodes"]["J"]["demand_m3s"] == pytest.approx(0.02)
    assert result["timesteps"][1]["nodes"]["R"]["head_m"] == 120
    for ts in result["timesteps"]:
        _continuity(net, ts)


@pytest.mark.parametrize("filling", [True, False])
def test_eps_tank_limit_event_and_volume_conservation(filling):
    tank = {"id": "T", "elevation": 0, "init_level": 1 if filling else 3,
            "min_level": 1, "max_level": 3, "diameter": 2}
    net = {"headloss": "H-W", "junctions": [], "reservoirs": [{"id": "R", "head": 10 if filling else 0}],
           "tanks": [tank], "pipes": [_pipe("P", "R" if filling else "T", "T" if filling else "R", length=100, diameter=0.1)],
           "options": {"duration_s": 3600, "hydraulic_step_s": 3600}}
    result = water.solve(net)
    assert result["converged"]
    expected_level = 3 if filling else 1
    assert result["timesteps"][-1]["tanks"]["T"]["level_m"] == pytest.approx(expected_level, abs=1e-9)
    assert result["timesteps"][-1]["links"]["P"]["flow_m3s"] == 0
    transported = sum(a["links"]["P"]["flow_m3s"]*(b["t_s"]-a["t_s"]) for a, b in zip(result["timesteps"], result["timesteps"][1:]))
    assert transported == pytest.approx(math.pi*2**2/4*2, abs=1e-8)
    assert len(result["timesteps"]) == 3  # initial, fractional tank-limit event, final


def test_fire_flow_bisection_system_limiting_node_not_withdrawal_node():
    net = {"headloss": "H-W", "reservoirs": [{"id": "R", "head": 60}],
           "junctions": [{"id": "A", "elevation": 0, "demand_m3s": 0},
                         {"id": "T", "elevation": 0, "demand_m3s": 0.005},
                         {"id": "K", "elevation": 35, "demand_m3s": 0.005}],
           "pipes": [_pipe("RA", "R", "A"), _pipe("AT", "A", "T", length=100), _pipe("AK", "A", "K", length=100)]}
    untouched = deepcopy(net)
    result = water.fire_flow(net, "T", residual_min_m=20)
    branch_loss = _HW_EPANET*100*0.005**1.852/(130**1.852*0.2**4.871)
    # Closed-form limiting-node energy balance: reservoir60 minus trunk loss
    # minus the fixed K-branch loss minus K elevation35 equals residual20.
    expected = ((60-35-20-branch_loss)*130**1.852*0.2**4.871/(_HW_EPANET*1000))**(1/1.852)-0.01
    assert result["flow_m3s"] == pytest.approx(expected, abs=1e-8)
    assert result["minimum_pressure_m"] == pytest.approx(20, abs=1e-5)
    assert result["limiting_node_id"] == "K"
    assert result["residual_pressures_m"]["T"] > 20
    assert result["pass"]
    assert net == untouched
    _continuity(net, result["hydraulics"]["timesteps"][0])
    fixed = water.fire_flow(net, "T", expected*1.1, residual_min_m=20)
    assert not fixed["pass"]
    assert fixed["minimum_pressure_m"] < 20


def test_elapsed_controls_are_chronological_one_shot_events():
    net = {"headloss": "H-W", "junctions": [], "reservoirs": [{"id": "R", "head": 60}],
           "tanks": [{"id": "T", "elevation": 0, "init_level": 10, "min_level": 0, "max_level": 100, "diameter": 100}],
           "pipes": [_pipe("P", "R", "T")],
           "controls": [{"link_id": "P", "status": "open", "time_s": 240},
                        {"link_id": "P", "status": "closed", "time_s": 120}],
           "options": {"duration_s": 300}}
    result = water.solve(net)
    assert result["converged"]
    assert [s["t_s"] for s in result["timesteps"]] == [0, 120, 240, 300]
    assert result["timesteps"][0]["links"]["P"]["flow_m3s"] > 0
    assert result["timesteps"][1]["links"]["P"]["flow_m3s"] == 0
    assert result["timesteps"][2]["links"]["P"]["flow_m3s"] > 0


def test_pump_open_control_resets_speed_per_epanet():
    net = _single()
    net["pipes"] = []
    net["pumps"] = [{"id": "P", "from": "R", "to": "J", "curve": [[0, 40], [0.02, 30], [0.04, 0]], "speed": 0.5}]
    net["controls"] = [{"link_id": "P", "status": "open", "time_s": 120}]
    net["options"]["duration_s"] = 120
    result = water.solve(net)
    assert result["converged"]
    # h=40-25000q². Initial affinity speed .5: .25*h(.02)=7.5m.
    assert result["timesteps"][0]["nodes"]["J"]["head_m"] == pytest.approx(67.5, abs=1e-7)
    assert result["timesteps"][1]["nodes"]["J"]["head_m"] == pytest.approx(97.5, abs=1e-7)
    assert net["pumps"][0]["speed"] == 0.5


def test_fire_flow_below_residual_baseline_has_no_available_increment():
    net = _single()
    result = water.fire_flow(net, "J", residual_min_m=59)
    assert result["available_flow_m3s"] == 0
    assert not result["pass"]
    assert result["limiting_node_id"] == "J"
    assert result["minimum_pressure_m"] < 59
    assert result["warnings"]


def test_fire_flow_pda_preserves_fixed_fire_withdrawal():
    net, _ = _fixture("pda")
    result = water.fire_flow(net, "J", 0.001, residual_min_m=1)
    ts = result["hydraulics"]["timesteps"][0]
    delivered = ts["nodes"]["J"]["demand_delivered_m3s"]
    assert delivered == pytest.approx(0.001+0.01*math.sqrt(ts["nodes"]["J"]["pressure_m"]/20), abs=1e-10)
    assert ts["links"]["RJ"]["flow_m3s"] == pytest.approx(delivered, abs=1e-9)


def test_criteria_pressure_and_velocity_checks_are_profile_driven():
    net = _single()
    result = water.solve(net, criteria={"profile": "custom", "overrides": {"water_min_pressure_m": 59,
                         "water_max_pressure_m": 100, "water_max_velocity_ms": 0.1}})
    checks = {(r["check"], r["element_id"]): r for r in result["checks"]}
    assert checks["minimum_pressure", "J"]["limit"] == 59
    assert not checks["minimum_pressure", "J"]["pass"]
    assert not checks["maximum_velocity", "P"]["pass"]
    assert checks["maximum_pressure", "J"]["pass"]


def test_nonconvergence_is_reported_not_claimed_or_eps_advanced():
    # A DDA single pipe starts at its exact prescribed flow; its linear nodal
    # head update legitimately converges in one iteration. Deficient PDA instead
    # couples the nonlinear Wagner demand and pipe resistance, needing iteration.
    net, _ = _fixture("pda")
    net["options"].update(max_iter=1, duration_s=7200)
    result = water.solve(net)
    assert result["ok"] and not result["converged"]
    assert result["iterations"] == 1
    assert len(result["timesteps"]) == 1
    assert result["timesteps"][0]["t_s"] == 0
    assert result["warnings"]


@pytest.mark.parametrize("mutation,reason", [
    (lambda n: n["pipes"][0].update(status="closed"), "disconnected_nodes"),
    (lambda n: n["pipes"][0].update(diameter=0), "invalid_pipe"),
    (lambda n: n["pipes"][0].update(roughness=0), "invalid_roughness"),
    (lambda n: n["pipes"][0].update(to="missing"), "node_not_found"),
    (lambda n: n["options"].update(demand_model="PDA", p_req_m=0), "invalid_pda_pressure_range"),
    (lambda n: n["options"].update(hydraulic_step_s=0), "invalid_options"),
    (lambda n: n["options"].update(max_iter=1.5), "invalid_options"),
    (lambda n: n["junctions"][0].update(pattern="missing"), "pattern_not_found"),
    (lambda n: n.update(headloss="C-M"), "unsupported_headloss"),
    (lambda n: n.update(epanet_passthrough=[{"section": "RULES", "active": True, "hydraulic": True}]), "unsupported_epanet_hydraulics"),
])
def test_named_input_refusals(mutation, reason):
    net = _single()
    mutation(net)
    with pytest.raises(CivilError) as caught:
        water.solve(net)
    assert caught.value.reason == reason
    if reason == "disconnected_nodes":
        assert caught.value.extra["ids"] == ["J"]


def test_no_head_boundary_and_invalid_curve_are_refused():
    net = _single()
    net["reservoirs"] = []
    with pytest.raises(CivilError) as caught:
        water.solve(net)
    assert caught.value.reason == "no_head_boundary"
    net = _single()
    net["pipes"] = []
    net["pumps"] = [{"id": "P", "from": "R", "to": "J", "curve": [[0, 20], [0.01, 30]]}]
    with pytest.raises(CivilError) as caught:
        water.solve(net)
    assert caught.value.reason == "invalid_pump_curve"
