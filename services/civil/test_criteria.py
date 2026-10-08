"""Table transcription: GLUMRB Wastewater Facilities 2014 §§33.1,33.41,34.1;
Water Works 2018 §8.2.1. Exact international-foot/inch conversion.
"""
import pytest
from .criteria import PROFILES, resolve
from .errors import CivilError


def test_published_slope_table_and_spacing_values():
    criteria = resolve()
    assert criteria["gravity_min_velocity_full_ms"] == pytest.approx(.6096)
    assert criteria["gravity_max_velocity_ms"] == pytest.approx(3.048)  # §33.45, 10 ft/s.
    assert criteria["gravity_min_diameter_sanitary_m"] == pytest.approx(.2032)
    table = criteria["gravity_min_slope_by_diameter"]
    assert table[0] == pytest.approx([.2032, .004])
    assert table[10] == pytest.approx([.9144, .00046])
    assert table[-1] == pytest.approx([1.0668, .00037])
    assert criteria["manhole_max_spacing_m"][0] == pytest.approx([.381, 121.92])
    assert criteria["manhole_max_spacing_m"][1] == pytest.approx([.762, 152.4])


def test_pressure_head_conversion_and_practice_attribution():
    # 1 psi = 6894.757293168 Pa, head p/(rho*g), rho=1000, g=9.80665.
    values = resolve()
    assert values["water_min_pressure_m"] == pytest.approx(14.0613915927829)
    assert values["water_normal_min_pressure_m"] == pytest.approx(24.6074352873701)
    assert values["water_max_pressure_m"] == pytest.approx(70.3069579639145)
    for key in ("gravity_max_depth_ratio", "min_cover_m", "water_max_velocity_ms"):
        assert values["references"][key].startswith("practice:")
    assert set(values["references"]) == set(values)-{"references"}


def test_override_sources_and_nested_copy_isolation():
    result = resolve({"profile": "custom", "overrides": {"min_cover_m": 2,
                      "references": {"min_cover_m": "Municipal standard ABC 2026 §4"}}})
    assert result["min_cover_m"] == 2
    assert result["references"]["min_cover_m"] == "Municipal standard ABC 2026 §4"
    result["gravity_min_slope_by_diameter"][0][1] = 99
    assert resolve()["gravity_min_slope_by_diameter"][0][1] == .004
    assert PROFILES["custom"]["gravity_min_slope_by_diameter"][0][1] == .004
    unreferenced = resolve({"overrides": {"min_cover_m": 3}})
    assert unreferenced["references"]["min_cover_m"].startswith("user override:")


@pytest.mark.parametrize("criteria,reason", [
    ({"profile": "missing"}, "unknown_criteria_profile"),
    ({"overrides": {"mystery": 1}}, "unknown_criteria_key"),
    ({"mystery": 1}, "unknown_criteria_key"),
    ({"overrides": {"min_cover_m": -1}}, "invalid_criteria"),
    ({"overrides": {"gravity_max_depth_ratio": 1.1}}, "invalid_criteria"),
    ({"overrides": {"gravity_min_slope_by_diameter": [[.3, .001], [.2, .002]]}}, "invalid_criteria"),
    ({"overrides": {"references": {"wrong": "source"}}}, "unknown_criteria_key"),
])
def test_exact_criteria_refusals(criteria, reason):
    with pytest.raises(CivilError) as exc:
        resolve(criteria)
    assert exc.value.reason == reason
