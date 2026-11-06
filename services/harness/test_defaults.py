"""Behavioral coverage for harness screening defaults and overrides."""
import math

import pytest

from services.harness import model as M


def harness_with_wires(specs, defaults=None):
    h = M.new_harness("defaults", defaults=defaults)
    for ref, x in (("J1", 0), ("J2", 200)):
        M.add_connector(h, ref=ref, family="jst_ph", pins=len(specs),
                        frame=M.make_frame([x, 0, 0], [1, 0, 0]))
    M.add_segment(h, "J1", "J2", [[0, 0, 0], [200, 0, 0]])
    for i, spec in enumerate(specs, 1):
        args = {"gauge_awg": 20, "insulation_od_mm": 2}
        args.update(spec)
        M.add_wire(h, from_=f"J1:{i}", to=f"J2:{i}", **args)
    return h


def test_sourced_wire_bend_assumptions_and_centerline_conversion():
    h = harness_with_wires([{"kind": "coax"}, {"kind": "wire", "insulation": "Kapton"},
                            {"kind": "twisted_pair"}, {"kind": "shielded"}])
    coax, poly, pair, shield = h["wires"]
    assert coax["min_bend_radius_mm"] == 13  # (6 + 0.5) * OD
    assert poly["min_bend_radius_mm"] == 21  # (10 + 0.5) * OD
    assert pair["min_bend_radius_mm"] == shield["min_bend_radius_mm"] == 13
    assert "extrapolation" in pair["bend_radius_basis"]
    assert all(w["bend_radius_confidence"] == "low" for w in h["wires"])
    assert M.NASA_BEND_SOURCE in coax["bend_radius_basis"]
    assert "inside" in coax["bend_radius_basis"]


def test_hyphenated_shielded_drain_is_canonical():
    h = harness_with_wires([{"signal_class": "shielded-drain", "shield_ground_end": "from"}])
    assert h["wires"][0]["signal_class"] == "shielded_drain"
    assert h["wires"][0]["shield_ground_end"] == "from"


def test_explicit_wire_radii_take_precedence_and_mean_centerline():
    h = harness_with_wires([{"min_bend_radius_mm": 7}, {"bend_radius_x_od": 4}],
                           defaults={"bend_radius_x_od": 9})
    assert [w["min_bend_radius_mm"] for w in h["wires"]] == [7, 8]
    assert all(w["bend_radius_confidence"] == "explicit" for w in h["wires"])
    h2 = harness_with_wires([{}], defaults={"bend_radius_x_od": 9})
    assert h2["wires"][0]["min_bend_radius_mm"] == 18


def test_packing_estimate_and_override():
    assert M.bundle_od_mm([2]) == 2
    assert M.bundle_od_mm([2, 3]) == 5
    assert M.bundle_od_mm([2, 2, 2]) == pytest.approx(1.2 * math.sqrt(12))
    assert M.bundle_od_mm([10, 1, 1]) >= 11
    assert M.bundle_od_mm([2, 2, 2], 0.5) == pytest.approx(math.sqrt(24))
    h = harness_with_wires([{}, {}, {}])
    M.recompute_derived(h)
    segment = h["segments"][0]
    assert segment["od_mm"] == pytest.approx(1.2 * math.sqrt(12), abs=0.0001)
    # Bundle radius uses the bundle OD, not the individual wire diameter.
    assert segment["min_bend_radius_mm"] == pytest.approx(3.5 * segment["od_mm"], abs=0.0001)
    assert M.assumptions(h)["packing_factor"] == pytest.approx(1 / 1.2**2)
    assert M.PACKING_SOURCES[1] in M.assumptions(h)["packing_sources"]
    scope = M.assumptions(h)["packing_scope"]
    assert "each wire record" in scope and "twisted assembly OD is not inferred" in scope


def test_twisted_group_records_are_counted_individually():
    h = harness_with_wires([{"kind": "twisted_pair", "group": "pair"},
                            {"kind": "twisted_pair", "group": "pair"}])
    M.recompute_derived(h)
    assert h["segments"][0]["od_mm"] == 4


def test_bundle_categories_and_member_at_inner_edge():
    h = harness_with_wires([{"min_bend_radius_mm": 100}, {"kind": "coax"}])
    M.recompute_derived(h)
    s = h["segments"][0]
    assert s["od_mm"] == 4
    assert s["min_bend_radius_mm"] == 101
    assert "harness with coax" in s["bend_radius_basis"]
    h2 = harness_with_wires([{"insulation": "polyimide"}, {}])
    M.recompute_derived(h2)
    assert h2["segments"][0]["min_bend_radius_mm"] == 42


def test_segment_overrides_and_clear_restore_estimate():
    h = harness_with_wires([{}, {}, {}])
    s = h["segments"][0]
    s.update(override_packing_factor=0.5, override_od_mm=12,
             override_min_bend_radius_mm=25)
    M.recompute_derived(h)
    assert (s["od_mm"], s["min_bend_radius_mm"]) == (12, 25)
    assert "estimated radius without override" in s["bend_radius_basis"]
    del s["override_od_mm"], s["override_min_bend_radius_mm"]
    M.recompute_derived(h)
    assert s["od_mm"] == pytest.approx(math.sqrt(24), abs=0.0001)
    assert s["bend_radius_confidence"] == "low"


def test_legacy_unknowns_and_insulation_passthrough():
    h = harness_with_wires([{"extra": {"insulation": "PTFE", "emissivity": 0.9,
                                        "insulation_thermal_conductivity_w_mk": 0.2}}])
    w = h["wires"][0]
    assert (w["insulation"], w["emissivity"], w["insulation_thermal_conductivity_w_mk"]) == ("PTFE", 0.9, 0.2)
    w["min_bend_radius_mm"] = None
    h["defaults"]["packing_factor"] = None
    M.recompute_derived(h)
    assert w["min_bend_radius_mm"] == 13
    assert h["defaults"]["packing_factor"] is None
    with pytest.raises(M.HarnessError):
        M.validate_insulation("  ")


def test_ribbon_envelope_and_static_bend_are_explicitly_approximate():
    h = harness_with_wires([{"kind": "ribbon", "insulation_od_mm": None,
                             "ribbon_width_mm": 8, "ribbon_thickness_mm": 1}])
    w = h["wires"][0]
    assert w["insulation_od_mm"] == pytest.approx(math.sqrt(65))
    assert w["min_bend_radius_mm"] == pytest.approx(2.5 * math.sqrt(65), abs=0.0001)
    assert "orientation" in w["bend_radius_basis"]
    assert w["ribbon_width_mm"] == 8
    with pytest.raises(M.HarnessError) as exc:
        harness_with_wires([{"kind": "ribbon", "insulation_od_mm": None}])
    assert exc.value.code == "ribbon_dimensions_required"


def test_explicit_unknown_packing_remains_unknown():
    h = harness_with_wires([{}, {}, {}])
    h["defaults"]["packing_factor"] = None
    M.recompute_derived(h)
    assert h["segments"][0]["od_mm"] is None
    assert h["segments"][0]["min_bend_radius_mm"] is None
    h["segments"][0]["override_packing_factor"] = 0.5
    M.recompute_derived(h)
    assert h["segments"][0]["od_mm"] == pytest.approx(math.sqrt(24), abs=0.0001)
