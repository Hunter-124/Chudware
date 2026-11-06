"""Behavioral checks for harness conductor sizing and voltage-drop calculations."""
import pytest

from services.harness import cable_models, electrical, wire_data


def _wire(wid, net, cls):
    return {"id": wid, "net": net, "signal_class": cls, "kind": "wire",
            "from": {"connector": "source", "pin": 1},
            "to": {"connector": "load", "pin": 1}, "gauge_awg": 18,
            "insulation_od_mm": 2.0, "length_mm": 3000,
            "service_loop_mm": 0, "segments": []}


def test_awg18_round_trip_resistance_and_load_voltage():
    harness = {"id": "h", "wires": [_wire("feed", "V5", "power"),
                                    _wire("return", "GND", "ground")], "segments": []}
    result = electrical.analyze(harness, ambient_c=20, find_fixes=False,
                                loads=[{"wire": "feed", "return_wire": "return", "current_a": 5,
                                        "voltage_v": 5, "peak_a": 8, "min_load_voltage_v": 4}])
    feed = next(w for w in result["wires"] if w["id"] == "feed")
    drop = feed["checks"]["voltage_drop"]
    assert drop["drop_20c_v"] == pytest.approx(5 * 2 * 3 * 20.948 / 1000, rel=0.001)
    assert drop["load_end_v_at_peak"] < drop["load_end_v"] < 5


def test_bundle_derating_reduces_table_ampacity():
    base = wire_data.nec_table_ampacity(14, 90)
    assert base * wire_data.nec_adjustment_factor(8) < base * wire_data.nec_adjustment_factor(2)


def test_unknown_return_never_proves_drop_pass():
    harness = {"id": "h", "wires": [_wire("feed", "V5", "power")], "segments": []}
    result = electrical.analyze(harness, ambient_c=20, find_fixes=False,
                                loads=[{"wire": "feed", "current_a": 1, "voltage_v": 5}])
    assert result["wires"][0]["checks"]["voltage_drop"]["status"] == "unknown"
    assert result["verdict"] != "pass"


def test_missing_or_invalid_load_is_not_silently_zero():
    harness = {"id": "h", "wires": [_wire("feed", "V5", "power")], "segments": []}
    with pytest.raises(ValueError):
        electrical.analyze(harness, loads=[{"wire": "feed", "current_a": -1}], find_fixes=False)
    with pytest.raises(ValueError):
        electrical.analyze(harness, loads=[{"wire": "feed", "current_a": 1, "duty": 2}], find_fixes=False)


def test_metric_missing_standard_row_requires_datasheet():
    with pytest.raises(wire_data.NotTabulated):
        wire_data.resolve_conductor(csa_mm2=1.2)
    assert wire_data.resolve_conductor(csa_mm2=1.2, r20_ohm_per_km=16)["r20_ohm_per_km"] == 16


def test_cable_capacitance_alone_does_not_invent_impedance():
    result = cable_models.evaluate_cable(
        {"datasheet": {"capacitance_pf_per_m": 50, "resistance_ohm_per_km": 100}})
    assert result["c_pf_per_m"]["value"] == pytest.approx(50)
    assert result["r_dc_ohm_per_m"] == pytest.approx(0.1)
    assert result["z0_ohm"]["value"] is None
    assert result["td_ns_per_m"]["value"] is None


def test_coax_shield_resistance_is_not_equal_to_center_by_assumption():
    conductor = wire_data.resolve_conductor(gauge_awg=18)
    base = {"kind": "coax", "dielectric_od_mm": 4, "er": 2}
    result = cable_models.evaluate_cable(base, conductor=conductor)
    assert result["loop_r_dc_ohm_per_m"] is None
    result = cable_models.evaluate_cable(
        {**base, "shield_r_ohm_per_m": 0.01}, conductor=conductor)
    assert result["loop_r_dc_ohm_per_m"] == pytest.approx(result["r_dc_ohm_per_m"] + 0.01)
