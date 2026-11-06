"""Steady-state wire thermal model and scoped ampacity checks."""
import pytest

from services.harness import electrical, wire_data


def _harness(awg=24, od=1.5):
    return {"id": "h", "wires": [{"id": "w", "net": "P", "signal_class": "power",
            "gauge_awg": awg, "insulation_od_mm": od, "length_mm": 1000,
            "segments": []}], "segments": []}


def _analyze(current=2, *, harness=None, spec=None, ambient=30):
    return electrical.analyze(harness or _harness(), loads=[{"wire": "w", "current_a": current}],
                              ambient_c=ambient, wire_specs={"w": spec or {}}, emissivity=0.9,
                              find_fixes=False)


def test_radial_conduction_and_joule_surface_balance():
    current, r20, radial, diameter, ambient = 2.0, 0.084, 1.1, 0.0015, 30.0
    surface, temps = electrical._coupled_temperature({"w": (current, r20, radial)}, diameter, ambient, 0.9)
    conductor = temps["w"]
    heat = current**2 * r20 * (1 + wire_data.ALPHA_CU_20_PER_C * (conductor - 20))
    assert conductor > surface > ambient
    assert (conductor - surface) / radial == pytest.approx(heat, rel=1e-10)
    assert electrical._surface_loss_w_per_m(diameter, surface, ambient, 0.9) == pytest.approx(heat, rel=1e-10)


def test_zero_current_and_insulation_conductivity_effect():
    spec = {"temp_rating_c": 105, "insulation_thermal_conductivity_w_mk": 0.2}
    zero = _analyze(0, spec=spec)["wires"][0]
    low = _analyze(2, spec=spec)["wires"][0]
    high = _analyze(2, spec={**spec, "insulation_thermal_conductivity_w_mk": 0.4})["wires"][0]
    assert zero["conductor_temp_c"] == 30
    assert low["conductor_temp_c"] > high["conductor_temp_c"] > 30
    assert low["checks"]["ampacity_thermal"]["limit_a"] < high["checks"]["ampacity_thermal"]["limit_a"]
    assert low["checks"]["ampacity_thermal"]["confidence"] == "low"


def test_thermal_limit_satisfies_temperature_rating():
    spec = {"temp_rating_c": 105, "insulation_thermal_conductivity_w_mk": 0.2}
    limit = _analyze(1, spec=spec)["wires"][0]["checks"]["ampacity_thermal"]["limit_a"]
    row = _analyze(limit, spec=spec)["wires"][0]
    assert row["conductor_temp_c"] == pytest.approx(105, abs=0.05)


def test_missing_and_invalid_material_inputs():
    assert _analyze(2, spec={"temp_rating_c": 105})["wires"][0]["conductor_temp_c"] is None
    assert _analyze(2, harness=_harness(24, 0.1), spec={"insulation_thermal_conductivity_w_mk": 0.2})["wires"][0]["conductor_temp_c"] is None
    for value in (0, -1, float("nan"), float("inf")):
        with pytest.raises(ValueError):
            _analyze(spec={"insulation_thermal_conductivity_w_mk": value})
    with pytest.raises(ValueError):
        _analyze(ambient=-250)


def test_bundle_couples_members_and_missing_member_blocks_thermal():
    h = _harness()
    h["wires"][0]["segments"] = ["s"]
    h["wires"].append({**h["wires"][0], "id": "other", "net": "Q"})
    h["segments"] = [{"id": "s", "od_mm": 3, "wires": ["w", "other"]}]
    spec = {"temp_rating_c": 105, "insulation_thermal_conductivity_w_mk": 0.2}
    result = electrical.analyze(h, ambient_c=30, emissivity=0.9, find_fixes=False,
                                loads=[{"wire": "w", "current_a": 2}, {"wire": "other", "current_a": 1}],
                                wire_specs={"*": spec})
    assert result["wires"][0]["conductor_temp_c"] > result["segments"][0]["bundle_temp_c"]
    missing = electrical.analyze(h, ambient_c=30, find_fixes=False,
                                 loads=[{"wire": "w", "current_a": 2}], wire_specs={"w": spec})
    assert missing["wires"][0]["conductor_temp_c"] is None


def test_nasa_opt_in_scope_and_vacuum():
    spec = {"temp_rating_c": 200, "nasa_pressure_psia": 14.7,
            "insulation_thermal_conductivity_w_mk": 0.2}
    row = _analyze(2, spec=spec, ambient=22)["wires"][0]
    assert row["checks"]["ampacity_nasa"]["derated_a"] == pytest.approx(5.4)
    assert "spaceflight" in row["checks"]["ampacity_nasa"]["scope"]
    assert _analyze(2, spec=spec, ambient=30)["wires"][0]["checks"]["ampacity_nasa"]["status"] == "not_evaluated"
    vacuum = _analyze(2, spec={**spec, "nasa_pressure_psia": 0}, ambient=94)["wires"][0]
    assert vacuum["checks"]["ampacity_nasa"]["derated_a"] == pytest.approx(4.7)
    assert vacuum["conductor_temp_c"] is None


def test_thermal_feedback_runaway_is_refused():
    with pytest.raises(electrical.ThermalRange):
        electrical._coupled_temperature({"w": (100, 1, 10)}, 0.001, 30, 0.9)



def test_datasheet_ampacity_scoped_to_supplied_conditions():
    spec = {"temp_rating_c": 105, "ampacity": {"current_a": 3, "ambient_c": 30,
                                                "max_conductors": 1, "source": "manufacturer"}}
    row = _analyze(4, spec=spec)["wires"][0]
    assert row["checks"]["ampacity_datasheet"]["status"] == "fail"
    assert row["derated_ampacity_a"] == 3
    hotter = _analyze(4, spec=spec, ambient=31)["wires"][0]
    assert hotter["checks"]["ampacity_datasheet"]["status"] == "not_evaluated"
    assert hotter["derated_ampacity_a"] is None


def test_datasheet_overrides_generic_table():
    spec = {"temp_rating_c": 90, "ampacity": {"current_a": 30, "ambient_c": 30,
                                               "max_conductors": 1, "source": "manufacturer"}}
    row = _analyze(26, harness=_harness(14, 3), spec=spec)["wires"][0]
    assert row["checks"]["ampacity_table"]["status"] == "fail"
    assert row["ampacity_status"] == "warn"
    assert row["derated_ampacity_a"] == 30


def test_insulation_selection_and_property_overrides():
    h = _harness()
    h["wires"][0]["insulation"] = "PVC"
    values = wire_data.resolve_insulation("PVC")
    result = electrical.analyze(h, loads=[{"wire": "w", "current_a": 2}], find_fixes=False)
    row = result["wires"][0]
    assert row["insulation"]["material"] == values["material"]
    assert row["insulation"]["sources"] == values["sources"]
    assert row["insulation"]["assumptions"] == values["assumptions"]
    assert row["checks"]["thermal"]["insulation_thermal_conductivity_w_mk"] == values["thermal_conductivity_w_mk"]
    overridden = electrical.analyze(h, loads=[{"wire": "w", "current_a": 2}],
        wire_specs={"w": {"insulation": "PTFE", "insulation_thermal_conductivity_w_mk": 0.31,
                          "emissivity": 0.75, "temp_rating_c": 125}}, find_fixes=False)["wires"][0]
    assert overridden["insulation"]["material"] == wire_data.resolve_insulation("PTFE")["material"]
    assert overridden["checks"]["thermal"]["insulation_thermal_conductivity_w_mk"] == 0.31
    assert overridden["checks"]["thermal"]["emissivity"] == 0.75
    assert overridden["checks"]["thermal"]["rating_c"] == 125
    assert all(overridden["insulation"]["sources"][key] == "caller-supplied wire specification"
               for key in ("thermal_conductivity_w_mk", "emissivity", "max_temperature_c"))
    assert overridden["conductor_temp_c"] is not None


def test_missing_emissivity_is_not_guessed_and_global_override_is_used():
    h = _harness()
    spec = {"insulation_thermal_conductivity_w_mk": 0.2, "temp_rating_c": 105}
    args = {"loads": [{"wire": "w", "current_a": 2}], "wire_specs": {"w": spec}, "find_fixes": False}
    missing = electrical.analyze(h, **args)["wires"][0]
    assert missing["conductor_temp_c"] is None
    assert "emissivity" in missing["thermal_note"]
    supplied = electrical.analyze(h, emissivity=0.7, **args)["wires"][0]
    assert supplied["conductor_temp_c"] is not None
    assert supplied["checks"]["thermal"]["emissivity"] == 0.7


def test_unknown_insulation_and_invalid_property_overrides_refused():
    h = _harness()
    h["wires"][0]["insulation"] = "not a real insulation"
    with pytest.raises(ValueError):
        electrical.analyze(h, find_fixes=False)
    for spec in ({"emissivity": -0.1}, {"emissivity": float("nan")},
                 {"insulation_thermal_conductivity_w_mk": 0}, {"temp_rating_c": 900}):
        with pytest.raises(ValueError):
            _analyze(spec=spec)


def test_mixed_bundle_requires_outer_surface_emissivity():
    h = _harness()
    h["wires"].append(dict(h["wires"][0], id="w2"))
    h["wires"][0]["segments"] = ["s"]
    h["wires"][1]["segments"] = ["s"]
    h["segments"] = [{"id": "s", "od_mm": 3, "wires": ["w", "w2"]}]
    specs = {"*": {"insulation_thermal_conductivity_w_mk": 0.2, "temp_rating_c": 105},
             "w": {"emissivity": 0.8}, "w2": {"emissivity": 0.9}}
    args = {"loads": [{"wire": "w", "current_a": 2}], "wire_specs": specs, "find_fixes": False}
    missing = electrical.analyze(h, **args)
    assert missing["wires"][0]["conductor_temp_c"] is None
    assert "emissivity" in missing["segments"][0]["note"]
    supplied = electrical.analyze(h, emissivity=0.85, **args)
    assert supplied["wires"][0]["conductor_temp_c"] is not None
    assert supplied["wires"][0]["checks"]["thermal"]["emissivity"] == 0.85


def test_wire_spec_material_and_no_connector_family_inference():
    h = _harness()
    h["connectors"] = [{"id": "c", "family": "Deutsch"}]
    args = {"loads": [{"wire": "w", "current_a": 2}], "find_fixes": False}
    unspecified = electrical.analyze(h, **args)["wires"][0]
    assert unspecified["insulation"]["material"] is None
    assert unspecified["conductor_temp_c"] is None
    specified = electrical.analyze(h, wire_specs={"*": {"insulation": "ETFE"}}, **args)["wires"][0]
    assert specified["insulation"]["material"] == wire_data.resolve_insulation("ETFE")["material"]


def test_ribbon_does_not_use_circular_thermal_model():
    h = _harness()
    h["wires"][0]["kind"] = "ribbon"
    spec = {"temp_rating_c": 105, "insulation_thermal_conductivity_w_mk": 0.2}
    row = _analyze(harness=h, spec=spec)["wires"][0]
    assert row["resistance_20c_ohm"] is not None
    assert row["conductor_temp_c"] is None
    assert row["checks"]["thermal"]["status"] == "not_evaluated"
    assert "ribbon" in row["thermal_note"]
    assert "ampacity_thermal" not in row["checks"]
    h["wires"][0]["segments"] = ["s"]
    h["segments"] = [{"id": "s", "od_mm": 3, "wires": ["w"]}]
    result = _analyze(harness=h, spec=spec)
    assert result["wires"][0]["conductor_temp_c"] is None
    assert "ribbon" in result["segments"][0]["note"]