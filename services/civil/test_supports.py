"""Deterministic beam/tributary V&V with hand-selected inputs, no daemon.

Beam sources: Euler–Bernoulli simply-supported uniform-load formulas, Roark's
Formulas for Stress and Strain (8th ed., 2012). Pipe dimensions in routing fixture:
ASME B36.10M NPS 2 Schedule 40 OD=60.3mm, wall=3.91mm (fluids runtime catalog).
"""
import copy
import json
import math

import pytest

from services.civil import supports
from services.civil.errors import CivilError


BEAM_INPUTS = {"elastic_modulus_gpa": 200, "allowable_stress_mpa": 100,
               "pipe_density_kg_m3": 7850, "fluid_density_kg_m3": 1000,
               "allowable_sag_mm": 2.5}


def piping_fixture():
    # Contract centreline polylines: r1=6m+4m=10m; r2=5m; branch at x=3m.
    # Valve/elbow/tee explicit weights sum to 250N; tee counted once, not per leg.
    return {"schema": "aicad.piping/1", "id": "hand-calculation", "name": "two runs",
            "units": {"system": "MMGS"}, "material": "A106-B",
            "runs": [
                {"id": "r1", "path": [[0, 0, 0], [6000, 0, 0], [6000, 4000, 0]],
                 "nps": 2, "schedule": "40"},
                {"id": "r2", "path": [[3000, 0, 0], [3000, -5000, 0]],
                 "nps": 2, "schedule": "40"}],
            "fittings": [
                {"id": "valve", "type": "valve", "run_id": "r1", "origin": [1000, 0, 0],
                 "params": {"valve_type": "gate"}},
                {"id": "elbow", "type": "elbow", "run_id": "r1", "origin": [6000, 0, 0],
                 "params": {"angle": 90}},
                {"id": "tee", "type": "tee", "run_id": "r1", "origin": [3000, 0, 0],
                 "params": {"branch_run": "r2"}}]}


def fixture_result(doc=None):
    return supports.supports_for_piping(piping_fixture() if doc is None else doc,
                                       **BEAM_INPUTS,
                                       fitting_weights_n={"valve": 120, "elbow": 50, "tee": 80})


def test_annular_section_closed_form_and_no_nominal_diameter_substitution():
    # OD=.2m,ID=.1m: metal A=.0075π; bore A=.0025π;
    # I=.0000234375πm⁴; Z=.000234375πm³.
    section = supports.section_properties(0.2, 0.05)
    assert section["id_m"] == pytest.approx(0.1)
    assert section["pipe_area_m2"] == pytest.approx(0.0075*math.pi)
    assert section["contents_area_m2"] == pytest.approx(0.0025*math.pi)
    assert section["moment_of_inertia_m4"] == pytest.approx(0.0000234375*math.pi)
    assert section["section_modulus_m3"] == pytest.approx(0.000234375*math.pi)


def test_stress_governs_and_deflection_governs_switch():
    # Hand-selected rho(pipe)=rho(fluid)=1000kg/m³,OD=.2,wall=.05,g=10:
    # total mass/m=10π kg/m, w=100πN/m, E=200GPa, δallow=.0025m.
    # S=1MPa -> Lstress²=18.75m²; Lsag⁴=9000m⁴. S=100 -> stress²=1875.
    low_stress = supports.support_spacing(0.2, 0.05, 200, 1, pipe_density_kg_m3=1000,
                                         gravity_ms2=10)
    high_stress = supports.support_spacing(0.2, 0.05, 200, 100, pipe_density_kg_m3=1000,
                                          gravity_ms2=10)
    assert low_stress["weight_n_m"] == pytest.approx(100*math.pi)
    assert low_stress["stress_span_m"] == pytest.approx(math.sqrt(18.75))
    assert low_stress["deflection_span_m"] == pytest.approx(9000**0.25)
    assert low_stress["governing"] == "stress"
    assert low_stress["governing_span_m"] == pytest.approx(math.sqrt(18.75))
    assert low_stress["stress_at_span_mpa"] == pytest.approx(1)
    assert low_stress["sag_at_span_mm"] < 2.5
    assert high_stress["governing"] == "deflection"
    assert high_stress["governing_span_m"] == pytest.approx(9000**0.25)
    assert high_stress["sag_at_span_mm"] == pytest.approx(2.5)
    assert high_stress["stress_at_span_mpa"] < 100


def test_continuous_moment_approximation_retains_conservative_sag():
    simply = supports.support_spacing(0.2, 0.05, 200, 1, pipe_density_kg_m3=1000,
                                     gravity_ms2=10)
    continuous = supports.support_spacing(0.2, 0.05, 200, 1, pipe_density_kg_m3=1000,
                                         gravity_ms2=10, condition="continuous")
    assert continuous["stress_span_m"] == pytest.approx(math.sqrt(23.4375))
    assert continuous["deflection_span_m"] == pytest.approx(simply["deflection_span_m"])
    assert any("preliminary" in text for text in continuous["assumptions"])


def test_insulation_and_contents_contribute_weight_and_shorten_spans():
    empty = supports.support_spacing(0.2, 0.05, 200, 1, pipe_density_kg_m3=1000,
                                    fluid_density_kg_m3=0, insulation_kg_m=0, gravity_ms2=10)
    insulated = supports.support_spacing(0.2, 0.05, 200, 1, pipe_density_kg_m3=1000,
                                        insulation_kg_m=2, gravity_ms2=10)
    assert empty["weight_n_m"] == pytest.approx(75*math.pi)
    assert insulated["weight_n_m"] == pytest.approx(100*math.pi+20)
    assert insulated["governing_span_m"] < empty["governing_span_m"]


def test_two_run_supports_at_bends_valves_branches_and_within_spacing():
    doc = piping_fixture()
    original = copy.deepcopy(doc)
    result = fixture_result(doc)
    assert doc == original  # analysis must not edit the user's design document
    r1 = [record for record in result["supports"] if record["run_id"] == "r1"]
    r2 = [record for record in result["supports"] if record["run_id"] == "r2"]
    for station in (0, 1, 3, 6, 10):
        assert any(record["station_m"] == pytest.approx(station) for record in r1)
    assert any(record["location_mm"] == pytest.approx([6000, 0, 0]) for record in r1)
    assert any(record["station_m"] == pytest.approx(0) and record["location_mm"] == pytest.approx([3000, 0, 0]) for record in r2)
    valve = next(record for record in r1 if record["station_m"] == pytest.approx(1))
    assert valve["fitting_weight_n"] == pytest.approx(120)
    assert "fitting:valve:valve" in valve["reasons"]
    for rid, local in (("r1", r1), ("r2", r2)):
        distances = [b["station_m"]-a["station_m"] for a, b in zip(local, local[1:])]
        assert max(distances) <= result["runs"][rid]["governing_span_m"]*(1+1e-12)
        for record in local:
            assert record["station_mm"] == pytest.approx(record["station_m"]*1000)
            assert record["location_mm"] == pytest.approx([x*1000 for x in record["location_m"]])
    assert result["runs"]["r1"]["od_mm"] == pytest.approx(60.3)
    assert result["runs"]["r1"]["wall_mm"] == pytest.approx(3.91)
    assert result["runs"]["r1"]["length_m"] == pytest.approx(10)
    assert result["runs"]["r2"]["length_m"] == pytest.approx(5)


def test_two_run_reactions_conserve_exact_total_distributed_and_point_weight():
    # Published schedule geometry, hand-computed SI line weight, not catalog mass.
    # w=π/4*[7850*(.0603²-.05248²)+1000*.05248²]*9.80665 N/m.
    expected_w = math.pi/4*(7850*(0.0603**2-0.05248**2)+1000*0.05248**2)*9.80665
    result = fixture_result()
    expected_total = expected_w*15+250
    assert result["total_weight_n"] == pytest.approx(expected_total, rel=1e-12)
    assert result["total_reaction_n"] == pytest.approx(expected_total, rel=1e-12)
    assert sum(record["fitting_weight_n"] for record in result["supports"]) == pytest.approx(250)
    assert sum(record["tributary_length_m"] for record in result["supports"]) == pytest.approx(15)
    for rid, length in (("r1", 10), ("r2", 5)):
        local = [s for s in result["supports"] if s["run_id"] == rid]
        point = 250 if rid == "r1" else 0
        assert sum(record["reaction_n"] for record in local) == pytest.approx(expected_w*length+point)
    json.dumps(result, allow_nan=False)


def test_single_span_endpoint_reactions_equal_half_load():
    doc = {"schema": "aicad.piping/1", "runs": [
        {"id": "r1", "path": [[0, 0, 0], [1000, 0, 0]], "nps": 2, "schedule": "40"}],
        "fittings": []}
    result = supports.supports_for_piping(doc, **BEAM_INPUTS)
    assert len(result["supports"]) == 2
    assert result["supports"][0]["reaction_n"] == pytest.approx(result["total_weight_n"]/2)
    assert result["supports"][1]["reaction_n"] == pytest.approx(result["total_weight_n"]/2)
    assert result["supports"][0]["tributary_length_m"] == pytest.approx(0.5)


def test_collinear_vertices_do_not_force_spurious_bend_supports():
    doc = {"schema": "aicad.piping/1", "runs": [
        {"id": "r1", "path": [[0, 0, 0], [400, 0, 0], [1000, 0, 0]], "nps": 2, "schedule": "40"}],
        "fittings": []}
    result = supports.supports_for_piping(doc, **BEAM_INPUTS)
    assert [record["station_m"] for record in result["supports"]] == pytest.approx([0, 1])
    assert all("bend" not in record["reasons"] for record in result["supports"])


def test_unannotated_branch_endpoint_requires_header_support():
    doc = piping_fixture()
    doc["fittings"] = []
    result = supports.supports_for_piping(doc, **BEAM_INPUTS)
    branch = next(s for s in result["supports"] if s["run_id"] == "r1" and s["station_m"] == pytest.approx(3))
    assert "branch" in branch["reasons"]
    assert result["total_weight_n"] == pytest.approx(result["total_reaction_n"])


def test_run_property_override_and_suppression_affect_spacing_and_weight():
    doc = piping_fixture()
    doc["fittings"] = []
    doc["runs"][1]["suppressed"] = True
    result = supports.supports_for_piping(doc, **BEAM_INPUTS,
                                         run_properties={"r1": {"allowable_stress_mpa": 0.1}})
    assert set(result["runs"]) == {"r1"}
    assert result["runs"]["r1"]["governing"] == "stress"
    assert all(s["run_id"] == "r1" for s in result["supports"])
    assert sum(s["tributary_length_m"] for s in result["supports"]) == pytest.approx(10)


def test_rack_loads_tributary_equilibrium_and_coulomb_envelope():
    # Known reactions 100N,200N on beam A and 400N on B; μ=.3 -> 90N,120N.
    records = [{"id": "s1", "reaction_n": 100}, {"id": "s2", "reaction_n": 200},
               {"id": "s3", "reaction_n": 400}]
    result = supports.rack_loads(records, friction_coefficient=0.3,
                                 beam_assignments={"s1": "A", "s2": "A", "s3": "B"})
    assert result["beams"]["A"]["vertical_load_n"] == pytest.approx(300)
    assert result["beams"]["A"]["thermal_friction_n"] == pytest.approx(90)
    assert result["beams"]["B"]["thermal_friction_n"] == pytest.approx(120)
    assert result["total_vertical_load_n"] == pytest.approx(700)
    assert result["total_thermal_friction_n"] == pytest.approx(210)
    routing = fixture_result()
    assignments = {s["id"]: ("A" if s["run_id"] == "r1" else "B") for s in routing["supports"]}
    racks = supports.rack_loads(routing, friction_coefficient=0.2, beam_assignments=assignments)
    assert racks["total_vertical_load_n"] == pytest.approx(routing["total_weight_n"])
    assert racks["total_thermal_friction_n"] == pytest.approx(0.2*routing["total_weight_n"])


def test_rack_support_embedded_assignment_and_zero_friction():
    result = supports.rack_loads([{"id": "s", "reaction_n": 123, "beam_id": "B"}],
                                 friction_coefficient=0)
    assert result["beams"]["B"]["vertical_load_n"] == pytest.approx(123)
    assert result["beams"]["B"]["thermal_friction_n"] == 0


@pytest.mark.parametrize("call,reason", [
    (lambda: supports.section_properties(0.1, 0.05), "invalid_pipe_section"),
    (lambda: supports.support_spacing(0.1, 0.005, 200, 0, pipe_density_kg_m3=7850), "invalid_input"),
    (lambda: supports.support_spacing(0.1, 0.005, 200, 100, pipe_density_kg_m3=7850, condition="cantilever"), "unsupported_support_condition"),
    (lambda: supports.supports_for_piping({"schema": "other"}, **BEAM_INPUTS), "invalid_piping"),
    (lambda: supports.rack_loads([{"id": "s", "reaction_n": 1}], friction_coefficient=0.3), "rack_assignment_required"),
    (lambda: supports.rack_loads([{"id": "s", "reaction_n": -1, "beam_id": "B"}], friction_coefficient=0.3), "invalid_input"),
    (lambda: supports.rack_loads([], friction_coefficient=0.3, beam_assignments={"missing": "B"}), "unknown_support"),
])
def test_stable_refusals(call, reason):
    with pytest.raises(CivilError) as exc:
        call()
    assert exc.value.reason == reason


def test_invalid_geometry_fitting_attribution_catalog_and_units_refuse():
    doc = piping_fixture()
    doc["runs"][0]["path"][1] = [0, 0, 0]
    with pytest.raises(CivilError) as exc:
        fixture_result(doc)
    assert exc.value.reason == "degenerate_path"
    doc = piping_fixture()
    doc["fittings"][0]["origin"] = [1000, 100, 0]
    with pytest.raises(CivilError) as exc:
        fixture_result(doc)
    assert exc.value.reason == "fitting_not_on_run"
    doc = piping_fixture()
    doc["runs"][0]["schedule"] = "unavailable"
    with pytest.raises(CivilError) as exc:
        fixture_result(doc)
    assert exc.value.reason == "unknown_pipe_size"
    doc = piping_fixture()
    doc["units"] = {"system": "metres"}
    with pytest.raises(CivilError) as exc:
        fixture_result(doc)
    assert exc.value.reason == "unsupported_piping_units"


def test_unknown_point_load_never_silently_dropped():
    with pytest.raises(CivilError) as exc:
        supports.supports_for_piping(piping_fixture(), **BEAM_INPUTS,
                                    fitting_weights_n={"not_in_document": 1000})
    assert exc.value.reason == "unknown_fitting"
