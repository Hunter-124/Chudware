"""Independent HDS-5 equation fixtures, geometry and applicability V&V.

Reference: FHWA-HIF-12-026 (2012), Appendix A Table A.1, eqs. A.1–A.3;
§3.1.4, eqs.3.5–3.6; Table C.2. No HY-8 equivalence is implied in the
empirical inlet transition or in the partly-full extended-HGL approximation.
"""
import math

import pytest

from services.civil.errors import CivilError
from services.civil import culvert as c


G = 9.80665


def pipe(**updates):
    return {"shape": "circular", "diameter_m": 1.2, "length_m": 30, "slope": 0.01,
            "n": 0.013, "inlet": "concrete_pipe_square_edge_headwall", "inlet_invert_m": 100,
            "road_crest_m": 103, **updates}


def refused(reason, function, *args, **kwargs):
    with pytest.raises(CivilError) as error:
        function(*args, **kwargs)
    assert error.value.reason == reason


def test_hds5_published_submerged_equation_fixture_1200mm_rcp():
    # Independent hand substitution in HDS-5 A.3 / A.1 square-edge-headwall:
    # D=1.2m,A=pi D²/4,Q=3m³/s,S=.01,Ku=1.811,c=.0398,Y=.67,Ks=-.5.
    # x=4.385273742304347, HW=1.716454687966524m. This is a published
    # nomograph-equation fixture, not an engine run or graph-reading estimate.
    diameter, q = 1.2, 3
    x = 1.811 * q / ((math.pi * diameter ** 2 / 4) * math.sqrt(diameter))
    expected = diameter * (0.0398 * x ** 2 + 0.67 - 0.5 * 0.01)
    result = c.inlet_control(pipe(), q)
    assert result["inlet_regime"] == "submerged"
    assert result["headwater_m"] == pytest.approx(expected, rel=1e-12)
    assert result["headwater_m"] == pytest.approx(1.716454687966524)


def test_unsubmerged_specific_critical_energy_fixture():
    # Independent circular geometry root of g A(y)^3/T(y)=Q², then eq.A.1.
    # Input D=1.2m,Q=1m³/s,S=.01: dc=.5417357565733179m;
    # A_c=D²(theta-sin theta)/8, theta=2acos(1-2dc/D).
    # Reference root and HW were evaluated from these equations using scipy
    # brentq, not the production module: HW=.7683776745532895m.
    diameter, q, dc = 1.2, 1, 0.5417357565733179
    theta = 2 * math.acos(1 - 2 * dc / diameter)
    ac = diameter ** 2 / 8 * (theta - math.sin(theta))
    top = diameter * math.sin(theta / 2)
    assert G * ac ** 3 / top == pytest.approx(q * q, rel=1e-10)
    critical_energy = dc + (q / ac) ** 2 / (2 * G)
    x = 1.811 * q / (math.pi * diameter ** 2 / 4 * math.sqrt(diameter))
    expected = critical_energy + diameter * (0.0098 * x ** 2 - 0.5 * 0.01)
    result = c.inlet_control(pipe(), q)
    assert result["inlet_regime"] == "unsubmerged_form_1"
    assert result["headwater_m"] == pytest.approx(expected, rel=1e-10)
    assert result["headwater_m"] == pytest.approx(0.7683776745532895)


def test_outlet_control_full_flow_closed_form_si_manning():
    # HDS-5 eq.3.5 in SI, R=D/4, exit K=1, square-edge entrance ke=.5.
    diameter, length, n, slope, q, tw = 1.2, 30, 0.013, 0.01, 2, 2
    velocity = q / (math.pi * diameter ** 2 / 4)
    loss = (1 + 0.5 + 2 * G * n ** 2 * length / (diameter / 4) ** (4 / 3)) * velocity ** 2 / (2 * G)
    expected = loss + tw - length * slope
    result = c.outlet_control(pipe(), q, tw)
    assert result["outlet_regime"] == "full_flow"
    assert result["headloss_m"] == pytest.approx(loss, rel=1e-12)
    assert result["headwater_m"] == pytest.approx(expected, rel=1e-12)
    assert result["velocity_ms"] == pytest.approx(velocity)
    analysis = c.analyze(pipe(), q, tw)
    assert analysis["controlling"] == "outlet"
    assert analysis["headwater_elevation_m"] == pytest.approx(100 + expected)
    assert analysis["overtopping"] is False


def test_partly_full_outlet_uses_critical_reference_and_warns():
    diameter, q, dc = 1.2, 2, 0.7781296594876317
    velocity = q / (math.pi * diameter ** 2 / 4)
    loss = (1 + 0.5 + 2 * G * 0.013 ** 2 * 30 / (diameter / 4) ** (4 / 3)) * velocity ** 2 / (2 * G)
    result = c.outlet_control(pipe(slope=0), q, 0)
    assert result["critical_depth_m"] == pytest.approx(dc, rel=1e-9)
    assert result["headwater_m"] == pytest.approx(loss + (dc + diameter) / 2)
    assert result["outlet_regime"] == "partly_full_extended_hgl"
    assert "outlet_control_partly_full_extended_hgl_approximation" in result["warnings"]
    refused("partly_full_backwater_required", c.outlet_control, pipe(), 0.1, 0)


def test_controlling_maximum_and_absolute_elevation_overtopping():
    result = c.analyze(pipe(road_crest_m=101.5), 3, 1.2)
    assert result["controlling"] == "inlet"
    assert result["headwater_m"] == pytest.approx(1.716454687966524)
    assert result["overtopping"] is True
    assert "roadway_overtopping_flow_not_partitioned" in result["warnings"]
    crest_equality = c.analyze(pipe(road_crest_m=result["headwater_elevation_m"]), 3, 1.2)
    assert crest_equality["overtopping"] is False
    no_crest = pipe()
    del no_crest["road_crest_m"]
    assert c.analyze(no_crest, 3, 1.2)["overtopping"] is None
    no_datum = pipe()
    del no_datum["inlet_invert_m"]
    refused("culvert_inlet_datum_required", c.analyze, no_datum, 3, 1.2)


def test_transition_is_continuous_tangent_and_monotone():
    # Appendix A's empirical transition is bounded at x=3.5 and x=4.
    # Verify both value and derivative continuity; an abrupt equation switch
    # or interpolation of discharge instead of head fails these assertions.
    flow_scale = math.pi * 1.2 ** 2 / 4 * math.sqrt(1.2) / 1.811
    def hw(x):
        return c.inlet_control(pipe(), x * flow_scale)["headwater_m"]
    previous = hw(3.49)
    for index in range(101):
        current = hw(3.5 + index * 0.005)
        assert current > previous
        previous = current
    for boundary in [3.5, 4]:
        epsilon = 1e-5
        left_slope = (hw(boundary) - hw(boundary - epsilon)) / epsilon
        right_slope = (hw(boundary + epsilon) - hw(boundary)) / epsilon
        assert left_slope == pytest.approx(right_slope, rel=0.002)
    midpoint = c.inlet_control(pipe(), 3.75 * flow_scale)
    assert midpoint["inlet_regime"] == "transition_hermite"
    assert "inlet_transition_is_empirical_interpolation" in midpoint["warnings"]


@pytest.mark.parametrize("inlet,c_sub,y,ks,ke", [
    ("concrete_pipe_square_edge_headwall", 0.0398, 0.67, -0.5, 0.5),
    ("concrete_pipe_groove_end_headwall", 0.0292, 0.74, -0.5, 0.2),
    ("concrete_pipe_groove_end_projecting", 0.0317, 0.69, -0.5, 0.2),
    ("cmp_headwall", 0.0379, 0.69, -0.5, 0.5),
    ("cmp_mitered", 0.0463, 0.75, 0.7, 0.7),
    ("cmp_projecting", 0.0553, 0.54, -0.5, 0.9),
])
def test_transcribed_common_pipe_inlet_and_entrance_coefficients(inlet, c_sub, y, ks, ke):
    # HDS-5 Table A.1, charts1/2 and C.2: independent numeric transcription.
    x = 1.811 * 3 / (math.pi * 1.2 ** 2 / 4 * math.sqrt(1.2))
    model = pipe(inlet=inlet)
    result = c.inlet_control(model, 3)
    assert result["headwater_m"] == pytest.approx(1.2 * (c_sub * x ** 2 + y + ks * 0.01))
    assert c.outlet_control(model, 3, 2)["ke"] == ke


@pytest.mark.parametrize("inlet,k,m,c_sub,y,ke", [
    ("concrete_box_wingwall_30_75", 0.026, 1.0, 0.0347, 0.81, 0.4),
    ("concrete_box_wingwall_90_15", 0.061, 0.75, 0.0400, 0.80, 0.5),
])
def test_box_flared_wingwall_form1_coefficients(inlet, k, m, c_sub, y, ke):
    model = {"shape": "rectangular", "height_m": 1.2, "width_m": 2, "length_m": 20,
             "n": 0.012, "slope": 0.01, "inlet": inlet}
    x = 1.5
    q = x * 2 * 1.2 * math.sqrt(1.2) / 1.811
    critical = (q ** 2 / (G * 2 ** 2)) ** (1 / 3)
    result = c.inlet_control(model, q)
    assert result["headwater_m"] == pytest.approx(1.5 * critical + 1.2 * (k * x ** m - 0.5 * 0.01))
    q_high = 5 * 2 * 1.2 * math.sqrt(1.2) / 1.811
    assert c.inlet_control(model, q_high)["headwater_m"] == pytest.approx(1.2 * (c_sub * 25 + y - 0.5 * 0.01))
    assert c.outlet_control(model, q_high, 3)["ke"] == ke


def test_form2_has_no_unsubmerged_slope_term():
    # Table A.1 chart10: chamfered headwall K=.515,M=.667 (A.2).
    model = {"shape": "rectangular", "height_m": 1.2, "width_m": 2, "length_m": 20,
             "n": 0.012, "slope": 0.01, "inlet": "concrete_box_headwall_chamfer_19mm"}
    x = 1.5
    q = x * 2 * 1.2 * math.sqrt(1.2) / 1.811
    expected = 1.2 * 0.515 * x ** 0.667
    assert c.inlet_control(model, q)["headwater_m"] == pytest.approx(expected)
    assert c.inlet_control({**model, "slope": 0.1}, q)["headwater_m"] == pytest.approx(expected)


def test_parallel_barrels_have_same_hw_as_one_at_half_total_flow():
    one = c.analyze(pipe(), 3, 2)
    two = c.analyze(pipe(barrels=2), 6, 2)
    assert two["headwater_m"] == pytest.approx(one["headwater_m"])
    assert two["outlet_velocity_ms"] == pytest.approx(one["outlet_velocity_ms"])
    refused("invalid_culvert_parameter", c.analyze, pipe(barrels=1.5), 3, 2)


def test_zero_flow_hydrostatic_head_and_performance_curve():
    result = c.analyze(pipe(), 0, 2)
    assert result["headwater_m"] == pytest.approx(2 - 30 * 0.01)
    assert result["headloss_m"] == 0
    assert result["outlet_velocity_ms"] == 0
    curve = c.performance_curve(pipe(tailwater_m=2), [0, 1, 2, 3, 4])
    heads = [point["headwater_m"] for point in curve["points"]]
    assert all(a < b for a, b in zip(heads, heads[1:]))
    assert heads[2] == pytest.approx(c.analyze(pipe(), 2, 2)["headwater_m"])
    refused("invalid_performance_flows", c.performance_curve, pipe(tailwater_m=2), [2, 1])
    refused("culvert_tailwater_required", c.performance_curve, pipe(), [1, 2])


def test_invalid_and_unsupported_cases_have_named_refusals():
    refused("unsupported_culvert_inlet", c.analyze, pipe(inlet="unlisted"), 3, 2)
    refused("culvert_inlet_shape_mismatch", c.analyze, pipe(inlet="concrete_box_wingwall_30_75"), 3, 2)
    refused("invalid_culvert_parameter", c.analyze, pipe(n=0), 3, 2)
    refused("invalid_culvert_parameter", c.analyze, pipe(), -1, 2)
    refused("invalid_culvert_parameter", c.analyze, pipe(), 3, -1)
    refused("invalid_culvert_parameter", c.analyze, pipe(diameter_m=float("nan")), 3, 2)
    refused("inlet_equation_outside_domain", c.inlet_control, pipe(slope=0.5), 0.01)
