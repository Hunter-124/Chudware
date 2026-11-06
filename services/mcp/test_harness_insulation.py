"""Cited insulation references remain distinct from actual wire specifications."""
import pytest

from services.harness.wire_data import resolve_insulation


@pytest.mark.parametrize("name,canonical,rating", [
    ("PVC", "PVC", 80), ("PTFE", "PTFE", 260), ("FEP", "FEP", 200),
    ("Tefzel", "ETFE", 150), ("ETFE", "ETFE", 150),
    ("irradiated PE", "XLPE", 125), ("cross-linked polyethylene", "XLPE", 125),
    ("silicone rubber", "silicone", 150), ("TPE", "TPE", 90),
])
def test_material_reference_and_provenance(name, canonical, rating):
    result = resolve_insulation(name)
    assert result["material"] == canonical
    assert result["max_temperature_c"] == rating
    assert result["assumptions"]
    for key in ("thermal_conductivity_w_mk", "emissivity", "max_temperature_c"):
        assert bool(result["sources"][key]) == (result[key] is not None)


def test_unknowns_are_not_fabricated():
    assert resolve_insulation("PTFE")["emissivity"] == 0.92
    assert resolve_insulation("XLPE")["thermal_conductivity_w_mk"] == pytest.approx(1 / 3.5)
    assert resolve_insulation("silicone")["emissivity"] == 0.9
    assert resolve_insulation("TPE")["thermal_conductivity_w_mk"] == 0.19
    assert "TPU example" in " ".join(resolve_insulation("TPE")["assumptions"])
    assert "generic cable-surface" in " ".join(resolve_insulation("FEP")["assumptions"])


@pytest.mark.parametrize("name", ["Teflon", "PE", "", "unobtainium", None])
def test_unknown_or_ambiguous_material_is_rejected(name):
    with pytest.raises(ValueError):
        resolve_insulation(name)


def test_results_do_not_share_mutable_provenance():
    result = resolve_insulation("PVC")
    result["sources"].clear()
    result["assumptions"].clear()
    assert resolve_insulation("PVC")["sources"]["emissivity"]
    assert resolve_insulation("PVC")["assumptions"]
