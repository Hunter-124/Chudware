"""INP boundary acceptance cases; no external solver dependency.

Hand conversions use the exact international foot (0.3048 m), US gallon
(0.003785411784 m3), Imperial gallon (0.00454609 m3), and EPANET's
0.4333 psi/ft pressure convention. Section semantics: EPA EPANET 2.2
User Manual (2020), Appendix A/C.2 and section 3.2.
"""

from copy import deepcopy
from pathlib import Path

import pytest

from services.civil import epanet_inp
from services.civil.errors import CivilError


def _simple(units="LPS", headloss="H-W", extra=""):
    return f"""[JUNCTIONS]
J 1 1
[RESERVOIRS]
R 10
[PIPES]
P R J 1 1 1 0 OPEN
[OPTIONS]
UNITS {units}
HEADLOSS {headloss}
{extra}
[END]
"""


def _without_metadata(text):
    return "".join(line for line in text.splitlines(keepends=True) if not line.startswith(";@CHUDWARE_CIVIL/1 "))


@pytest.mark.parametrize("units,flow,length,diameter", [
    ("GPM", 6.30901964e-5, .3048, .0254),
    ("CFS", .028316846592, .3048, .0254),
    ("MGD", .003785411784 * 1_000_000 / 86400, .3048, .0254),
    ("IMGD", .00454609 * 1_000_000 / 86400, .3048, .0254),
    ("AFD", 1233.48183754752 / 86400, .3048, .0254),
    ("LPS", .001, 1, .001),
    ("LPM", 1 / 60000, 1, .001),
    ("MLD", 1000 / 86400, 1, .001),
    ("CMH", 1 / 3600, 1, .001),
    ("CMD", 1 / 86400, 1, .001),
])
def test_all_flow_families_have_independent_hand_conversions(units, flow, length, diameter):
    result = epanet_inp.read(_simple(units))
    net = result["network"]
    assert net["junctions"][0]["demand_m3s"] == pytest.approx(flow, rel=1e-12)
    assert net["junctions"][0]["elevation"] == pytest.approx(length)
    assert net["reservoirs"][0]["head"] == pytest.approx(10 * length)
    assert net["pipes"][0]["length"] == pytest.approx(length)
    assert net["pipes"][0]["diameter"] == pytest.approx(diameter)
    assert result["provenance"]["units_out"] == "SI"
    assert result["provenance"]["conversions"]["flow_to_m3s"] == pytest.approx(flow)
    assert result["dropped"] == []


@pytest.mark.parametrize("units,roughness", [("GPM", .0003048), ("CFS", .0003048), ("LPS", .001), ("CMD", .001)])
def test_darcy_roughness_is_millifeet_or_mm_not_pipe_diameter(units, roughness):
    net = epanet_inp.read(_simple(units, "D-W"))["network"]
    assert net["pipes"][0]["roughness"] == pytest.approx(roughness)


ALL_SECTIONS = """; Survey metadata is intentionally retained, not a CRS inference.
[TITLE]
Interchange acceptance network
Pumps, categories, reservoir pattern and tank geometry
[JUNCTIONS]
J1 101 2 Daily
J2 102 3
J3 103 4
J4 104 5
[RESERVOIRS]
R 150 Daily
[TANKS]
T 110 3 1 6 8 2 TV YES
[PIPES]
L1 J1 J2 120 200 .15 .5 OPEN
L2 J2 J3 80 150 .2 0 CLOSED
L3 J3 J4 90 100 .1 0 CV
[PUMPS]
PU1 R J1 HEAD One SPEED 1.1 PATTERN Speed
PU3 J2 T HEAD Three SPEED .9
PUM J4 T HEAD Many
[VALVES]
VR J1 J3 100 PRV 20 .1
VS J2 J4 100 PSV 15 .2
VF J3 T 100 FCV 2 .3
VT J4 J1 100 TCV 4 .4
[PATTERNS]
Daily 1 1.2 .8
Daily 1.1 .9 1
Speed 1 .8 1.1
[CURVES]
;PUMP: Design point
One 10 30
;PUMP: Power-law fit
Three 0 50
Three 10 40
Three 20 10
;PUMP: Piecewise-linear
Many 0 55
Many 5 50
Many 10 40
Many 15 25
;VOLUME: Non-cylindrical tank
TV 0 0
TV 2 30
TV 6 120
;EFFICIENCY: Efficiency metadata
Eff 0 40
Eff 10 80
Eff 20 60
;HEADLOSS: Unused headloss metadata
Loss 0 0
Loss 10 5
[DEMANDS]
J1 1 Daily ;Domestic
J1 .5 Daily ;Industrial process
J2 2 ;No explicit pattern
[STATUS]
L1 CLOSED
PU1 .75
PU3 CLOSED
VR OPEN
VS 18
VF CLOSED
VT OPEN
[OPTIONS]
UNITS LPS
HEADLOSS D-W
DEMAND MODEL PDA
MINIMUM PRESSURE 3
REQUIRED PRESSURE 25
PRESSURE EXPONENT .6
ACCURACY .0000001
TRIALS 123
VISCOSITY 1.2
PATTERN Daily
DEMAND MULTIPLIER 1.25
[TIMES]
DURATION 2 DAYS
HYDRAULIC TIMESTEP 15 MINUTES
QUALITY TIMESTEP 30 SEC
RULE TIMESTEP 1 MIN
PATTERN TIMESTEP 1:30
PATTERN START .5
REPORT TIMESTEP 1 HOURS
REPORT START 0
START CLOCKTIME 6:30 PM
STATISTIC NONE
[COORDINATES]
R 0 0
J1 10 0
J2 20 0
J3 30 0
J4 40 0
T 20 20
[VERTICES]
L1 12 2
L1 15 3
PU3 20 10
VF 25 10
[END]
"""


def test_supported_sections_parse_with_exact_canonical_meanings():
    net = epanet_inp.read(ALL_SECTIONS)["network"]
    assert net["title"] == ["Interchange acceptance network", "Pumps, categories, reservoir pattern and tank geometry"]
    assert net["reservoirs"][0]["pattern"] == "Daily"
    tank = net["tanks"][0]
    assert [tank[k] for k in ("elevation", "init_level", "min_level", "max_level", "diameter")] == [110, 3, 1, 6, 8]
    assert tank["min_volume_m3"] == 2
    assert tank["volume_curve"] == "TV"
    assert tank["overflow"] is True
    j1, j2 = net["junctions"][:2]
    assert j1["demand_m3s"] == 0  # DEMANDS replaces the original 2 LPS.
    assert j1["pattern"] is None
    assert j1["demands"] == [
        {"demand_m3s": .001, "pattern": "Daily", "category": "Domestic"},
        {"demand_m3s": .0005, "pattern": "Daily", "category": "Industrial process"},
    ]
    assert j2["demands"][0]["pattern"] is None
    assert net["patterns"]["Daily"] == [1, 1.2, .8, 1.1, .9, 1]
    assert net["pumps"][0]["speed"] == .75
    assert net["pumps"][0]["speed_pattern"] == "Speed"
    assert net["pumps"][1]["status"] == "closed"
    assert [len(p["curve"]) for p in net["pumps"]] == [1, 3, 4]
    assert net["pumps"][0]["curve"] == [[.01, 30]]
    assert net["curves"]["TV"]["points"] == [[0, 0], [2, 30], [6, 120]]
    assert net["curves"]["Eff"]["points"][1] == [.01, 80]
    assert net["curves"]["Loss"]["points"][1] == [.01, 5]
    assert net["pipes"][0]["status"] == "closed"
    assert net["pipes"][0]["roughness"] == pytest.approx(.00015)
    assert net["pipes"][2]["status"] == "cv"
    assert net["pipes"][0]["vertices"] == [[12, 2], [15, 3]]
    assert net["pumps"][1]["vertices"] == [[20, 10]]
    assert [v["status"] for v in net["valves"]] == ["open", "active", "closed", "open"]
    assert net["valves"][1]["setting"] == 18
    assert net["valves"][2]["setting"] == .002
    options = net["options"]
    assert options["duration_s"] == 172800
    assert options["hydraulic_step_s"] == 900
    assert options["pattern_step_s"] == 5400
    assert options["pattern_start_s"] == 1800
    assert options["quality_step_s"] == 30
    assert options["rule_step_s"] == 60
    assert options["report_step_s"] == 3600
    assert options["report_start_s"] == 0
    assert options["start_clocktime_s"] == 18.5 * 3600
    assert options["max_iter"] == 123
    assert options["accuracy"] == 1e-7
    assert options["viscosity_m2s"] == pytest.approx(1.2 * 1.0219328e-6)
    assert options["demand_model"] == "PDA"
    assert options["p_min_m"] == 3
    assert options["p_req_m"] == 25
    assert options["pda_exponent"] == .6
    assert options["demand_multiplier"] == 1.25


@pytest.mark.parametrize("units", ["LPS", "GPM", "CFS", "MGD", "IMGD", "AFD", "LPM", "MLD", "CMH", "CMD"])
def test_all_sections_roundtrip_identical_and_deterministic(tmp_path, units):
    net = epanet_inp.read(ALL_SECTIONS)["network"]
    net["epanet_units"] = units
    net["epanet_pressure_units"] = "PSI" if units in {"GPM", "CFS", "MGD", "IMGD", "AFD"} else "METERS"
    net["crs"] = {"horizontal": "EPSG:26915", "vertical": "EPSG:5703"}
    net["asset_metadata"] = {"owner": "Municipal utility", "notes": "7 civil model", "nested": [1, None, True]}
    net["pipes"][0]["material"] = "ductile_iron"
    original = deepcopy(net)
    path = tmp_path / "network.inp"
    other = tmp_path / "again.inp"
    assert epanet_inp.write(net, path) == {"ok": True, "path": str(path)}
    epanet_inp.write(net, other)
    assert net == original
    assert path.read_bytes() == other.read_bytes()
    imported = epanet_inp.read(path)
    assert imported["network"] == original
    assert imported["provenance"]["metadata_restored"] is True
    # Ordinary foreign-tool records independently carry the SI hydraulics.
    plain = epanet_inp.read(_without_metadata(path.read_text()))["network"]
    assert plain["junctions"][0]["demands"][0]["demand_m3s"] == pytest.approx(.001)
    assert plain["pipes"][0]["diameter"] == pytest.approx(.2)
    assert plain["pipes"][0]["roughness"] == pytest.approx(.00015)
    assert plain["tanks"][0]["diameter"] == pytest.approx(8)
    assert plain["pumps"][0]["curve"][0] == pytest.approx([.01, 30])
    assert plain["valves"][1]["setting"] == pytest.approx(18)


def test_us_pressure_settings_and_tank_geometry_have_distinct_units():
    text = """[JUNCTIONS]
J 100 1
[RESERVOIRS]
R 200
[TANKS]
T 100 10 5 20 50 10
[PIPES]
P R J 100 6 100
[VALVES]
V J T 6 PRV 30 0
[OPTIONS]
UNITS GPM
DEMAND MODEL PDA
MINIMUM PRESSURE 5
REQUIRED PRESSURE 30
[END]
"""
    result = epanet_inp.read(text)
    net = result["network"]
    assert net["valves"][0]["setting"] == pytest.approx(30 / .4333 * .3048)
    assert net["valves"][0]["diameter"] == pytest.approx(6 * .0254)
    assert net["valves"][0]["status"] == "active"
    assert net["tanks"][0]["diameter"] == pytest.approx(50 * .3048)
    assert net["tanks"][0]["init_level"] == pytest.approx(10 * .3048)
    assert net["tanks"][0]["min_volume_m3"] == pytest.approx(10 * .028316846592)
    assert net["options"]["p_min_m"] == pytest.approx(5 / .4333 * .3048)
    assert net["options"]["p_req_m"] == pytest.approx(30 / .4333 * .3048)
    assert result["provenance"]["pressure_units_in"] == "PSI"


def test_scalar_and_category_demands_are_both_exported_without_double_count(tmp_path):
    net = epanet_inp.read(_simple())["network"]
    node = net["junctions"][0]
    node["demands"] = [{"demand_m3s": .002, "pattern": None, "category": "Additional"}]
    path = tmp_path / "categories.inp"
    epanet_inp.write(net, path)
    assert epanet_inp.read(path)["network"] == net
    ordinary = epanet_inp.read(_without_metadata(path.read_text()))["network"]["junctions"][0]
    assert ordinary["demand_m3s"] == 0
    assert sum(d["demand_m3s"] for d in ordinary["demands"]) == pytest.approx(.003)


def test_metadata_snapshot_never_masks_an_external_hydraulic_edit(tmp_path):
    net = epanet_inp.read(_simple())["network"]
    net["custom_asset"] = {"id": 123}
    path = tmp_path / "editable.inp"
    epanet_inp.write(net, path)
    result = epanet_inp.read(path.read_text().replace("R 10\n", "R 11\n"))
    assert result["network"]["reservoirs"][0]["head"] == 11
    assert "custom_asset" not in result["network"]
    assert result["network"]["epanet_unapplied_metadata"]["custom_asset"] == {"id": 123}
    assert result["provenance"]["metadata_restored"] is False
    assert any("records changed" in warning for warning in result["provenance"]["warnings"])


@pytest.mark.parametrize("section,rows,hydraulic", [
    ("RULES", "RULE 1\nIF NODE J PRESSURE BELOW 10\nTHEN LINK P STATUS IS CLOSED\n", True),
    ("CONTROLS", "LINK P CLOSED AT CLOCKTIME 10 AM\n", True),
    ("QUALITY", "J 1.0\n", False),
    ("REACTIONS", "GLOBAL BULK -.5\n", False),
    ("EMITTERS", "J 1\n", True),
    ("VENDOR_EXTENSION", "a b c\n", True),
])
def test_unsupported_sections_preserved_verbatim_reported_and_roundtripped(tmp_path, section, rows, hydraulic):
    verbatim = f"[{section}]\n; Keep this exact section comment.\n{rows}"
    source = _simple().replace("[END]\n", verbatim + "[END]\n")
    result = epanet_inp.read(source)
    record = next(r for r in result["dropped"] if r["section"] == section)
    assert record["text"] == verbatim
    assert record["active"] is True
    assert record["hydraulic"] is hydraulic
    assert any(r["text"] == verbatim for r in result["network"]["epanet_passthrough"])
    assert result["provenance"]["warnings"]
    path = tmp_path / "unsupported.inp"
    epanet_inp.write(result["network"], path)
    assert verbatim in path.read_text()
    assert epanet_inp.read(path)["network"] == result["network"]
    if hydraulic:
        from services.civil.water import solve
        with pytest.raises(CivilError) as error:
            solve(result["network"])
        assert error.value.reason == "unsupported_epanet_hydraulics"
        assert error.value.extra["section"] == section


def test_simple_tank_and_elapsed_controls_are_interpreted_and_not_flagged_unsupported(tmp_path):
    source = """[JUNCTIONS]
J 100 1
[RESERVOIRS]
R 200
[TANKS]
T 100 10 5 20 50 0
[PIPES]
P R J 100 6 100
P2 J T 100 6 100
[OPTIONS]
UNITS GPM
[CONTROLS]
LINK P OPEN IF NODE T BELOW 6
LINK P CLOSED IF NODE T ABOVE 18
LINK P2 CLOSED AT TIME 1:30
[END]
"""
    result = epanet_inp.read(source)
    assert result["dropped"] == []
    assert result["network"]["controls"] == [
        {"link_id": "P", "status": "open", "node_id": "T", "relation": "below", "level_m": 6 * .3048},
        {"link_id": "P", "status": "closed", "node_id": "T", "relation": "above", "level_m": 18 * .3048},
        {"link_id": "P2", "status": "closed", "time_s": 5400},
    ]
    assert all(not r["active"] and not r["hydraulic"] for r in result["network"]["epanet_passthrough"])
    path = tmp_path / "controls.inp"
    epanet_inp.write(result["network"], path)
    assert epanet_inp.read(path)["network"] == result["network"]
    ordinary = epanet_inp.read(_without_metadata(path.read_text()))["network"]
    assert ordinary["controls"] == result["network"]["controls"]


def test_mixed_controls_keep_only_uninterpreted_controls_active(tmp_path):
    source = _simple().replace("[END]\n", "[CONTROLS]\nLINK P CLOSED AT TIME 2\nLINK P OPEN IF NODE J BELOW 10\n[END]\n")
    result = epanet_inp.read(source)
    assert result["network"]["controls"] == [{"link_id": "P", "status": "closed", "time_s": 7200}]
    active = [r for r in result["network"]["epanet_passthrough"] if r["active"] and r["hydraulic"]]
    assert len(active) == 1
    assert "IF NODE J BELOW 10" in active[0]["text"]
    assert "AT TIME 2" not in active[0]["text"]
    path = tmp_path / "mixed.inp"
    epanet_inp.write(result["network"], path)
    assert epanet_inp.read(path)["network"] == result["network"]


def test_diagnostic_tuning_options_do_not_hide_active_physical_features():
    text = _simple(extra="SPECIFIC GRAVITY 1\nCHECKFREQ 2\nMAXCHECK 10\nDAMPLIMIT 0\nUNBALANCED CONTINUE 10\nQUALITY AGE")
    result = epanet_inp.read(text)
    assert result["dropped"]
    assert all(not r["hydraulic"] for r in result["network"]["epanet_passthrough"])
    with pytest.raises(CivilError) as error:
        epanet_inp.read(_simple(extra="SPECIFIC GRAVITY 1.2"))
    assert error.value.reason == "epanet_unsupported_feature"
    assert error.value.extra["feature"] == "specific_gravity"


def test_unscaled_diagram_metadata_does_not_claim_physical_coordinates(tmp_path):
    text = _simple("GPM").replace("[END]\n", "[COORDINATES]\nR 1 2\nJ 3 4\n[VERTICES]\nP 2 3\n[BACKDROP]\nUNITS None\n[END]\n")
    result = epanet_inp.read(text)
    net = result["network"]
    assert net["junctions"][0]["x"] is None
    assert net["pipes"][0]["vertices"] == []
    assert net["epanet_map"] == {"units": "none", "coordinates": {"R": [1, 2], "J": [3, 4]}, "vertices": {"P": [[2, 3]]}}
    assert result["provenance"]["conversions"]["map_to_m"] is None
    assert result["provenance"]["assumptions"] == []
    path = tmp_path / "diagram.inp"
    epanet_inp.write(net, path)
    assert epanet_inp.read(path)["network"] == net
    ordinary = epanet_inp.read(_without_metadata(path.read_text()))["network"]
    assert ordinary["epanet_map"] == net["epanet_map"]


def test_explicit_map_units_override_hydraulic_units():
    text = _simple("GPM").replace("[END]\n", "[COORDINATES]\nJ 10 20\n[BACKDROP]\nUNITS Meters\n[END]\n")
    result = epanet_inp.read(text)
    assert result["network"]["junctions"][0]["x"] == 10
    assert result["provenance"]["conversions"]["map_to_m"] == 1
    assert result["provenance"]["assumptions"] == []


def test_untyped_unused_curve_retains_foreign_values_without_guessing_units(tmp_path):
    text = _simple("GPM").replace("[END]\n", "[CURVES]\nUnused 1 2\nUnused 3 4\n[END]\n")
    result = epanet_inp.read(text)
    curve = result["network"]["curves"]["Unused"]
    assert curve["type"] == "unknown"
    assert curve["points_inp"] == [[1, 2], [3, 4]]
    assert curve["units_in"] == "GPM"
    assert any(r["section"] == "CURVES" for r in result["dropped"])
    path = tmp_path / "untyped.inp"
    epanet_inp.write(result["network"], path)
    assert epanet_inp.read(path)["network"] == result["network"]
    result["network"]["epanet_units"] = "LPS"
    with pytest.raises(CivilError) as error:
        epanet_inp.write(result["network"], path)
    assert error.value.reason == "epanet_unsupported_feature"
    assert error.value.extra["feature"] == "untyped_curve_unit_conversion"


@pytest.mark.parametrize("text,reason", [
    ("[JUNCTIONS\nJ 0", "epanet_bad_syntax"),
    (_simple().replace("J 1 1", "J nan 1"), "epanet_bad_syntax"),
    (_simple().replace("J 1 1", "J 1 1\nJ 2 3"), "epanet_bad_syntax"),
    (_simple().replace("P R J", "P Missing J"), "epanet_bad_syntax"),
    (_simple().replace("P R J 1 1 1 0 OPEN", "P R J 1 1 1 0 BROKEN"), "epanet_bad_syntax"),
    (_simple().replace("[END]", "[STATUS]\nP banana\n[END]"), "epanet_bad_syntax"),
    (_simple().replace("[END]", "[PATTERNS]\nBad -1\n[END]"), "epanet_bad_syntax"),
    (_simple().replace("[END]", "[TIMES]\nHYDRAULIC TIMESTEP 0\n[END]"), "epanet_bad_syntax"),
    (_simple("BOGUS"), "epanet_unsupported_feature"),
    (_simple(headloss="C-M"), "epanet_unsupported_feature"),
    (_simple().replace("[END]", "[PUMPS]\nPU R J POWER 10\n[END]"), "epanet_unsupported_feature"),
    (_simple().replace("[END]", "[VALVES]\nV R J 10 GPV C1 0\n[END]"), "epanet_unsupported_feature"),
    (_simple().replace("[END]", "[PUMPS]\nPU R J HEAD C1\n[CURVES]\nC1 1 20\nC1 2 10\n[END]"), "epanet_unsupported_feature"),
])
def test_invalid_and_unrepresentable_records_have_stable_civil_refusals(text, reason):
    with pytest.raises(CivilError) as error:
        epanet_inp.read(text)
    assert error.value.reason == reason
    assert error.value.to_dict()["ok"] is False


def test_io_errors_and_malformed_custom_comments_are_named(tmp_path):
    with pytest.raises(CivilError) as error:
        epanet_inp.read(tmp_path / "missing.inp")
    assert error.value.reason == "epanet_io_error"
    with pytest.raises(CivilError) as error:
        epanet_inp.read(";@CHUDWARE_CIVIL/1 not-base64!\n" + _simple())
    assert error.value.reason == "epanet_bad_syntax"
    with pytest.raises(CivilError) as error:
        epanet_inp.write(epanet_inp.read(_simple())["network"], tmp_path / "absent" / "network.inp")
    assert error.value.reason == "epanet_io_error"


def test_net1_import_preserves_quality_and_simple_controls_without_hydraulic_refusal():
    path = Path(__file__).with_name("data") / "epanet_net1.inp"
    result = epanet_inp.read(path)
    net = result["network"]
    assert len(net["junctions"]) == 9
    assert len(net["pipes"]) == 12
    assert net["options"]["duration_s"] == 86400
    assert net["options"]["pattern_step_s"] == 7200
    assert net["options"]["default_pattern"] == "1"
    assert net["controls"] == [
        {"link_id": "9", "status": "open", "node_id": "2", "relation": "below", "level_m": 110 * .3048},
        {"link_id": "9", "status": "closed", "node_id": "2", "relation": "above", "level_m": 140 * .3048},
    ]
    assert net["pumps"][0]["curve"][0] == pytest.approx([1500 * 6.30901964e-5, 250 * .3048])
    assert not any(r["active"] and r["hydraulic"] for r in net["epanet_passthrough"])
    assert any(r["section"] == "QUALITY" and r["active"] for r in result["dropped"])
    assert net["epanet_map"]["units"] == "none"


def test_foreign_crlf_sections_and_civil_snapshot_survive_filesystem_roundtrip(tmp_path):
    verbatim = "[QUALITY]\r\n; Verbatim CRLF comment\r\nJ 1\r\n"
    text = _simple().replace("[END]\n", verbatim + "[END]\n")
    source = tmp_path / "foreign.inp"
    source.write_bytes(text.encode("utf-8"))
    result = epanet_inp.read(source)
    assert next(r for r in result["dropped"] if r["section"] == "QUALITY")["text"] == verbatim
    exported = tmp_path / "roundtrip.inp"
    epanet_inp.write(result["network"], exported)
    assert verbatim.encode("utf-8") in exported.read_bytes()
    assert epanet_inp.read(exported)["network"] == result["network"]
    assert epanet_inp.read(exported)["provenance"]["metadata_restored"] is True


def test_contract_inline_pump_curves_get_deterministic_foreign_ids(tmp_path):
    net = epanet_inp.read(_simple())["network"]
    del net["curves"]
    net["pumps"] = [
        {"id": "PU", "from": "R", "to": "J", "curve": [[.01, 30]], "speed": 1, "status": "open"},
        {"id": "PU2", "from": "R", "to": "J", "curve": [[0, 50], [.01, 40], [.02, 10]], "speed": 1, "status": "closed"},
    ]
    path = tmp_path / "inline.inp"
    original = deepcopy(net)
    epanet_inp.write(net, path)
    assert net == original
    assert epanet_inp.read(path)["network"] == original
    ordinary = epanet_inp.read(_without_metadata(path.read_text()))["network"]
    assert ordinary["pumps"][0]["curve_id"] == "CIVIL_PUMP_PU"
    assert ordinary["pumps"][1]["curve_id"] == "CIVIL_PUMP_PU2"
    assert ordinary["pumps"][0]["curve"] == [[.01, 30]]
    assert ordinary["pumps"][1]["status"] == "closed"


def test_uninterpreted_hydraulic_records_refuse_unit_changes_instead_of_reinterpreting(tmp_path):
    source = _simple("GPM").replace("[END]\n", "[EMITTERS]\nJ 2\n[END]\n")
    net = epanet_inp.read(source)["network"]
    net["epanet_units"] = "LPS"
    with pytest.raises(CivilError) as error:
        epanet_inp.write(net, tmp_path / "converted.inp")
    assert error.value.reason == "epanet_unsupported_feature"
    assert error.value.extra["feature"] == "hydraulic_passthrough_unit_conversion"


def test_throttle_and_regulating_valves_default_active():
    text = _simple().replace("[END]\n", "[VALVES]\nVT R J 100 TCV 2\nVR R J 100 PRV 10\n[END]\n")
    net = epanet_inp.read(text)["network"]
    assert [v["status"] for v in net["valves"]] == ["active", "active"]


def test_writer_invalid_canonical_fields_have_named_refusals(tmp_path):
    net = epanet_inp.read(_simple())["network"]
    del net["pipes"][0]["length"]
    with pytest.raises(CivilError) as error:
        epanet_inp.write(net, tmp_path / "invalid.inp")
    assert error.value.reason == "epanet_bad_syntax"
    net = epanet_inp.read(_simple())["network"]
    net["custom_metadata"] = {1: "Non-JSON object key"}
    with pytest.raises(CivilError) as error:
        epanet_inp.write(net, tmp_path / "invalid.inp")
    assert error.value.reason == "epanet_bad_syntax"


@pytest.mark.parametrize("units,pressure_factor", [("GPM", .3048 / .4333), ("LPS", 1)])
def test_defaults_follow_archived_epanet_engine_not_manual_typo(units, pressure_factor):
    # EPA archive input1.c setdefaults: Preq=MINPDIFF, MaxIter=MAXITER.
    # types.h MINPDIFF=.1; input1.c convertunits divides by Ucf[PRESSURE].
    result = epanet_inp.read(_simple(units))
    options = result["network"]["options"]
    assert options["p_min_m"] == 0
    assert options["p_req_m"] == pytest.approx(.1 * pressure_factor)
    assert options["max_iter"] == 200
    assert options["accuracy"] == .001
    assert options["viscosity_m2s"] == pytest.approx(1.0219328e-6)
    assert options["report_step_s"] == 3600
    assert options["default_pattern"] is None
    assert result["provenance"]["default_pattern"] == {"requested": "1", "explicit": False, "resolved": None}
    assert "input1.c" in result["provenance"]["defaults_reference"]


@pytest.mark.parametrize("units,inp_value,expected", [
    ("LPS", "1.2", 1.2 * 1.0219328e-6),
    ("GPM", "1.2", 1.2 * 1.0219328e-6),
    ("LPS", "0.000001", 1e-6),
    ("GPM", "0.00001", 1e-5 * .3048 ** 2),
])
def test_relative_and_absolute_viscosity_follow_engine_input_branch(units, inp_value, expected):
    result = epanet_inp.read(_simple(units, extra=f"VISCOSITY {inp_value}"))
    assert result["network"]["options"]["viscosity_m2s"] == pytest.approx(expected)


@pytest.mark.parametrize("units", ["GPM", "LPS"])
def test_tiny_absolute_viscosity_roundtrips_without_becoming_relative(tmp_path, units):
    net = epanet_inp.read(_simple(units))["network"]
    net["options"]["viscosity_m2s"] = 1e-12
    path = tmp_path / "viscosity.inp"
    epanet_inp.write(net, path)
    ordinary = epanet_inp.read(_without_metadata(path.read_text()))["network"]
    assert ordinary["options"]["viscosity_m2s"] == pytest.approx(1e-12, rel=1e-12, abs=0)


@pytest.mark.parametrize("status_lines,expected_speed,expected_status", [
    ("PU OPEN", 1, "open"),
    ("PU CLOSED\nPU .75", .75, "open"),
    ("PU 0", 0, "closed"),
    ("PU CLOSED", .5, "closed"),
])
def test_pump_status_matches_epanet_changestatus(status_lines, expected_speed, expected_status):
    text = _simple().replace("[END]\n", "[PUMPS]\nPU R J HEAD C SPEED .5\n[CURVES]\nC 10 30\n[STATUS]\n" + status_lines + "\n[END]\n")
    pump = epanet_inp.read(text)["network"]["pumps"][0]
    assert pump["speed"] == expected_speed
    assert pump["status"] == expected_status


def test_export_never_resets_an_open_pumps_nonunit_speed(tmp_path):
    text = _simple().replace("[END]\n", "[PUMPS]\nPU R J HEAD C SPEED .5\n[CURVES]\nC 10 30\n[END]\n")
    net = epanet_inp.read(text)["network"]
    path = tmp_path / "speed.inp"
    epanet_inp.write(net, path)
    ordinary_text = _without_metadata(path.read_text())
    assert "PU OPEN" not in ordinary_text
    assert epanet_inp.read(ordinary_text)["network"]["pumps"][0]["speed"] == .5


def test_inactive_dda_pressure_range_is_retained_but_active_pda_range_refuses(tmp_path):
    text = _simple(extra="MINIMUM PRESSURE 0\nREQUIRED PRESSURE 0\nDEMAND MULTIPLIER 0")
    net = epanet_inp.read(text)["network"]
    assert net["options"]["p_req_m"] == 0
    assert net["options"]["demand_multiplier"] == 0
    path = tmp_path / "dda.inp"
    epanet_inp.write(net, path)
    assert epanet_inp.read(path)["network"] == net
    with pytest.raises(CivilError) as error:
        epanet_inp.read(text.replace("UNITS LPS", "UNITS LPS\nDEMAND MODEL PDA"))
    assert error.value.reason == "epanet_bad_syntax"


def test_pressure_family_adjustment_is_explicit_and_kpa_uses_engine_factor():
    metric = epanet_inp.read(_simple(extra="PRESSURE PSI"))
    assert metric["provenance"]["pressure_units_requested"] == "PSI"
    assert metric["provenance"]["pressure_units_in"] == "METERS"
    assert metric["provenance"]["warnings"]
    us = epanet_inp.read(_simple("GPM", extra="PRESSURE METERS"))
    assert us["provenance"]["pressure_units_in"] == "PSI"
    assert us["provenance"]["warnings"]
    kpa = epanet_inp.read(_simple(extra="PRESSURE KPA\nDEMAND MODEL PDA\nREQUIRED PRESSURE 100"))
    assert kpa["network"]["options"]["p_req_m"] == pytest.approx(100 * .3048 / .4333 / 6.895)


@pytest.mark.parametrize("directive,requested,explicit", [
    ("", "1", False),
    ("PATTERN 1", "1", True),
    ("PATTERN Missing", "Missing", True),
])
def test_missing_default_patterns_mean_constant_demands_per_epanet_adjustdata(tmp_path, directive, requested, explicit):
    # input1.c adjustdata applies the default only when findpattern returns >0.
    # Unlike a junction/pump reference, an absent OPTIONS PATTERN is not an error.
    result = epanet_inp.read(_simple(extra=directive))
    net = result["network"]
    assert net["options"]["default_pattern"] is None
    assert net["epanet_default_pattern"] == {"requested": requested, "explicit": explicit}
    assert result["provenance"]["default_pattern"] == {"requested": requested, "explicit": explicit, "resolved": None}
    assert any("constant multiplier 1" in warning for warning in result["provenance"]["warnings"])
    path = tmp_path / "constant.inp"
    epanet_inp.write(net, path)
    assert epanet_inp.read(path)["network"] == net
    assert epanet_inp.read(_without_metadata(path.read_text()))["network"]["options"]["default_pattern"] is None


@pytest.mark.parametrize("directive,pattern_id,explicit", [
    ("", "1", False),
    ("PATTERN Daily", "Daily", True),
])
def test_existing_implicit_and_explicit_default_patterns_remain_references(directive, pattern_id, explicit):
    text = _simple(extra=directive).replace("[END]\n", f"[PATTERNS]\n{pattern_id} 1 .8 1.2\n[END]\n")
    result = epanet_inp.read(text)
    assert result["network"]["options"]["default_pattern"] == pattern_id
    assert result["provenance"]["default_pattern"] == {"requested": pattern_id, "explicit": explicit, "resolved": pattern_id}
    assert "epanet_default_pattern" not in result["network"]


def test_explicit_node_pattern_remains_invalid_when_default_pattern_is_missing():
    text = _simple(extra="PATTERN Missing").replace("J 1 1", "J 1 1 Missing")
    with pytest.raises(CivilError) as error:
        epanet_inp.read(text)
    assert error.value.reason == "epanet_bad_syntax"


@pytest.mark.parametrize("filename", ["epanet_loop.inp", "epanet_pda.inp", "epanet_prv.inp"])
def test_reference_networks_without_patterns_do_not_keep_dangling_default_pattern(filename):
    path = Path(__file__).with_name("data") / filename
    net = epanet_inp.read(path)["network"]
    assert "1" not in net["patterns"]
    assert net["options"]["default_pattern"] is None
