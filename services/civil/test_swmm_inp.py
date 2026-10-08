"""SWMM 5.2 Appendix D grammar/unit and consumer-visible roundtrip V&V."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from .errors import CivilError
from .swmm_inp import export_inp, import_inp

DATA = Path(__file__).parent / "data"


def test_us_fixture_import_converts_every_dimension():
    result = import_inp((DATA / "swmm_roundtrip.inp").read_text())
    node = result["network"]["nodes"][0]
    pipe = result["network"]["conduits"][0]
    catch = result["catchments"][0]
    # International foot=.3048m; acre=43560ft²; cfs=.3048³ m³/s.
    assert node["invert"] == pytest.approx(30.48)
    assert node["rim"] == pytest.approx(33.528)
    assert node["swmm_initial_depth_m"] == pytest.approx(.1524)
    assert node["swmm_surcharge_depth_m"] == pytest.approx(.6096)
    assert node["swmm_ponded_area_m2"] == pytest.approx(4 * .3048 ** 2)
    assert node["inflow_m3s"] == pytest.approx(.25 * .3048 ** 3)
    assert pipe["diameter"] == pytest.approx(.3048)
    assert pipe["length"] == pytest.approx(30.48)
    assert pipe["inlet_invert"] == pytest.approx(100.2 * .3048)
    assert pipe["outlet_invert"] == pytest.approx(99.1 * .3048)
    assert pipe["vertices"] == [[50 * .3048, 3 * .3048]]
    assert catch["area_m2"] == pytest.approx(2.5 * 43560 * .3048 ** 2)
    assert catch["swmm_width_m"] == pytest.approx(200 * .3048)
    assert catch["swmm_curb_length_m"] == pytest.approx(12 * .3048)
    assert catch["swmm_subareas"]["dstore_perv_mm"] == pytest.approx(.2 * 25.4)
    assert catch["swmm_conductivity_mmh"] == pytest.approx(.5 * 25.4)
    assert catch["swmm_drying_time_s"] == 7 * 86400
    assert catch["cn"] == 78
    assert catch["polygon"][2] == [20 * .3048, 20 * .3048]
    assert result["network"]["nodes"][-1]["outfall_tailwater"] == pytest.approx(.3048)
    assert result["dropped"]["LOSSES"] == ["C1 0.1 0.2 0.0 NO 0"]
    assert result["dropped"]["DWF"] == ["J1 TSS 20"]
    json.dumps(result, allow_nan=False)


def test_four_shape_roundtrip_and_unsupported_section_replay():
    first = import_inp((DATA / "swmm_roundtrip.inp").read_text())
    output = export_inp(first["network"], first["catchments"], flow_units="CFS", dropped=first["dropped"])
    second = import_inp(output["inp"])
    for a, b in zip(first["network"]["nodes"], second["network"]["nodes"]):
        for field in ("invert", "rim", "x", "y", "inflow_m3s", "outfall_tailwater"):
            if a.get(field) is None:
                assert b.get(field) is None
            else:
                assert b[field] == pytest.approx(a[field], rel=1e-12)
    for a, b in zip(first["network"]["conduits"], second["network"]["conduits"]):
        for field in ("diameter", "width", "height", "side_slope", "length", "n", "inlet_invert", "outlet_invert"):
            if a.get(field) is None:
                assert b.get(field) is None
            else:
                assert b[field] == pytest.approx(a[field], rel=1e-12)
        assert a["shape"] == b["shape"]
        assert a.get("closed", False) == b.get("closed", False)
        assert a["vertices"] == b["vertices"]
    shapes = first["network"]["conduits"]
    assert [p["shape"] for p in shapes] == ["circular", "rectangular", "rectangular", "trapezoidal"]
    assert shapes[1]["closed"] is True and shapes[2]["closed"] is False
    assert second["catchments"][0]["cn"] == 78
    assert second["catchments"][0]["area_m2"] == pytest.approx(first["catchments"][0]["area_m2"])
    assert second["dropped"] == first["dropped"]


@pytest.mark.parametrize("units,qf,lf", [
    ("CFS", .028316846592, .3048),
    ("GPM", .003785411784 / 60, .3048),
    ("MGD", 3785.411784 / 86400, .3048),
    ("CMS", 1, 1), ("LPS", .001, 1), ("MLD", 1000 / 86400, 1),
])
def test_all_six_flow_unit_systems_roundtrip(units, qf, lf):
    text = f"""[OPTIONS]
FLOW_UNITS {units}
[JUNCTIONS]
A 100 10
[OUTFALLS]
O 99 FREE NO
[CONDUITS]
P A O 100 .013 0 0
[XSECTIONS]
P CIRCULAR 1 0 0 0 1
[DWF]
A FLOW 2
[COORDINATES]
A 0 0
O 100 0
"""
    first = import_inp(text)
    assert first["network"]["nodes"][0]["inflow_m3s"] == pytest.approx(2 * qf)
    assert first["network"]["nodes"][0]["invert"] == pytest.approx(100 * lf)
    assert first["network"]["conduits"][0]["diameter"] == pytest.approx(lf)
    encoded = export_inp(first["network"], flow_units=units)["inp"]
    assert "A FLOW 2" in encoded
    second = import_inp(encoded)
    assert second["network"]["nodes"][0]["inflow_m3s"] == pytest.approx(2 * qf)


def test_si_to_us_to_si_replaces_options_and_preserves_physical_geometry():
    first = import_inp((DATA / "swmm_steady.inp").read_text())
    output = export_inp(first["network"], flow_units="GPM")
    second = import_inp(output["inp"])
    assert second["network"]["swmm_options"]["FLOW_UNITS"] == "GPM"
    assert second["network"]["nodes"][0]["inflow_m3s"] == pytest.approx(.004, rel=1e-12)
    assert second["network"]["nodes"][0]["invert"] == pytest.approx(10.8, rel=1e-12)
    assert second["network"]["conduits"][0]["diameter"] == pytest.approx(.3, rel=1e-12)
    assert second["network"]["conduits"][0]["length"] == pytest.approx(100, rel=1e-12)


def test_elevation_link_offsets_do_not_add_node_invert_twice():
    text = (DATA / "swmm_steady.inp").read_text().replace("FLOW_UNITS CMS", "FLOW_UNITS CMS\nLINK_OFFSETS ELEVATION")
    text = text.replace("C1 J1 J3 100 0.013 0 0 0 0", "C1 J1 J3 100 0.013 10.9 10.5 0 0")
    first = import_inp(text)
    pipe = first["network"]["conduits"][0]
    assert pipe["inlet_invert"] == 10.9
    assert pipe["outlet_invert"] == 10.5
    second = import_inp(export_inp(first["network"])["inp"])
    assert second["network"]["conduits"][0]["inlet_invert"] == 10.9
    assert second["network"]["conduits"][0]["outlet_invert"] == 10.5


def test_unsupported_horton_infiltration_is_preserved_not_claimed_cn():
    text = (DATA / "swmm_roundtrip.inp").read_text().replace("INFILTRATION CURVE_NUMBER", "INFILTRATION HORTON").replace("S1 78 0.5 7", "S1 3 0.5 4 7 0")
    first = import_inp(text)
    assert first["catchments"][0]["cn"] is None
    assert first["dropped"]["INFILTRATION"] == ["S1 3 0.5 4 7 0"]
    encoded = export_inp(first["network"], first["catchments"], flow_units="CFS", dropped=first["dropped"])
    assert "INFILTRATION HORTON" in encoded["inp"]
    assert "S1 3 0.5 4 7 0" in encoded["inp"]


def test_dimensional_swmm_options_convert_with_flow_unit_family():
    text = (DATA / "swmm_steady.inp").read_text().replace("FLOW_UNITS CMS", "FLOW_UNITS CMS\nHEAD_TOLERANCE 0.003048\nMIN_SURFAREA 0.9290304")
    first = import_inp(text)
    exported = export_inp(first["network"], flow_units="CFS")["inp"]
    # .003048m=.01ft; .9290304m²=10ft² (EPA SWMM 5.2 Appendix D).
    assert "HEAD_TOLERANCE 0.01" in exported
    assert "MIN_SURFAREA 10" in exported
    second = import_inp(exported)
    back = export_inp(second["network"], flow_units="CMS")["inp"]
    assert "HEAD_TOLERANCE 0.003048" in back
    assert "MIN_SURFAREA 0.9290304" in back


def test_patterned_dwf_preserved_with_explicit_base_flow_warning():
    text = (DATA / "swmm_steady.inp").read_text().replace("J1 FLOW 0.004", "J1 FLOW 0.004 MONTH DAILY") + "\n[PATTERNS]\nMONTH MONTHLY 1 1 1 1 1 1 1 1 1 1 1 1\nDAILY DAILY 1 1 1 1 1 1 1\n"
    first = import_inp(text)
    assert first["network"]["nodes"][0]["swmm_dwf_patterns"] == ["MONTH", "DAILY"]
    assert first["network"]["nodes"][0]["inflow_m3s"] == .004
    assert any("base flow only" in warning for warning in first["warnings"])
    encoded = export_inp(first["network"], dropped=first["dropped"])["inp"]
    assert "J1 FLOW 0.004 MONTH DAILY" in encoded
    assert "[PATTERNS]" in encoded


def test_unavailable_plan_coordinates_are_disclosed_not_unnoticed():
    text = (DATA / "swmm_steady.inp").read_text().replace("J1 0 0\n", "")
    result = import_inp(text)
    assert any("J1: no SWMM coordinate" in warning for warning in result["warnings"])


def test_zero_swmm_maximum_depth_keeps_unknown_rim():
    text = (DATA / "swmm_steady.inp").read_text().replace("J1 10.8 2 0 0 0", "J1 10.8 0 0 0 0")
    result = import_inp(text)
    assert result["network"]["nodes"][0]["rim"] is None
    assert any("rim remains unknown" in warning for warning in result["warnings"])


@pytest.mark.parametrize("old,new,reason", [
    ("FLOW_UNITS CMS", "FLOW_UNITS XYZ", "unsupported_swmm_flow_units"),
    ("FLOW_UNITS CMS\n", "", "swmm_flow_units_missing"),
    ("C1 CIRCULAR 0.3", "C1 EGG 0.3", "unsupported_swmm_shape"),
    ("C1 CIRCULAR 0.3 0 0 0 1", "C1 CIRCULAR 0.3 0 0 0 2", "unsupported_swmm_barrels"),
    ("O 10 FREE NO", "O 10 TIDAL TIDE NO", "unsupported_swmm_outfall"),
    ("O 10 FREE NO", "O 10 FIXED 9 NO", "swmm_tailwater_below_invert"),
    ("J1 FLOW 0.004", "J1 FLOW -0.004", "negative_swmm_inflow"),
    ("C1 J1 J3 100 0.013", "C1 J1 MISSING 100 0.013", "not_found"),
    ("C1 CIRCULAR 0.3 0 0 0 1\n", "", "swmm_xsection_missing"),
    ("J1 10.8 2 0 0 0", "J1 nan 2 0 0 0", "invalid_swmm_number"),
])
def test_named_import_refusals(old, new, reason):
    text = (DATA / "swmm_steady.inp").read_text().replace(old, new)
    with pytest.raises(CivilError) as caught:
        import_inp(text)
    assert caught.value.reason == reason


def test_asymmetric_trapezoid_refused_without_losing_geometry():
    text = (DATA / "swmm_roundtrip.inp").read_text().replace("C4 TRAPEZOIDAL 2 3 2 2 1", "C4 TRAPEZOIDAL 2 3 2 3 1")
    with pytest.raises(CivilError) as caught:
        import_inp(text)
    assert caught.value.reason == "unsupported_asymmetric_trapezoid"


def test_unsupported_dimensional_sections_cannot_silently_change_unit_system():
    first = import_inp((DATA / "swmm_roundtrip.inp").read_text())
    with pytest.raises(CivilError) as caught:
        export_inp(first["network"], first["catchments"], flow_units="CMS", dropped=first["dropped"])
    assert caught.value.reason == "dropped_sections_unit_conversion"


def test_new_catchment_does_not_invent_swmm_runoff_parameters():
    first = import_inp((DATA / "swmm_steady.inp").read_text())
    catchment = {"id": "new", "outlet": "J1", "area_m2": 1000, "cn": 75, "imperv_pct": 50}
    with pytest.raises(CivilError) as caught:
        export_inp(first["network"], [catchment])
    assert caught.value.reason == "swmm_catchment_fields_missing"
    assert "swmm_subareas" in caught.value.extra["fields"]


def test_export_does_not_mutate_the_imported_model():
    first = import_inp((DATA / "swmm_steady.inp").read_text())
    snapshot = deepcopy(first)
    output = export_inp(first["network"], flow_units="LPS")
    assert "J1 FLOW 4" in output["inp"]
    assert first == snapshot
