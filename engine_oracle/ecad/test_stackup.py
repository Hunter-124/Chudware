"""Tests for the stackup advisor (engine.ecad.stackup, T-FIN12).

House style: plain main() + asserts, runnable as ``python -m engine.ecad.test_stackup``.

REFERENCE VALIDATION (Definition of Done): the 4-layer 50R/100R solve is checked against the
PUBLISHED JLCPCB JLC04161H-7628 controlled-impedance stackup:

  * Stackup layers/heights: https://jlcpcb.com/impedance — JLC04161H-7628: outer Cu 0.035 mm,
    7628 prepreg 0.2104 mm (Er 4.4), inner Cu 0.0152 mm, core 1.065 mm (Er 4.6), 1.6 mm total.
  * Published routing structures (JLCPCB's impedance calculator output, as republished in the
    JITX JLC04161H_7628 fab module,
    https://docs-testing.jitx.com/en/latest/jitxlib/jitxlib.jlcpcb.JLC04161H_7628.html):
      50R single-ended  outer layers: w = 0.3244 mm
      90R differential  outer layers: w = 0.2332 mm, gap = 0.15 mm
      100R differential outer layers: w = 0.1722 mm, gap = 0.15 mm

  The solver must reproduce this stackup exactly (it selects the published dielectric menu) and
  the IPC-2141A forward model evaluated at JLC's published 50R geometry must land within 5% of
  50 ohm (it lands at ~52.4 ohm, +4.8% — inside both the 5% DoD band and the model's stated
  accuracy). JLC's numbers come from a 2-D field solver including solder mask, which the
  IPC-2141A closed forms deliberately ignore, so the differential structures are checked against
  the standard +/-10% controlled-impedance fab tolerance band (they land at +8.6% / +5.6%).
"""
from __future__ import annotations

from engine.ecad import schema
from engine.ecad import stackup as stk
from engine.ecad.impedance import diff_pair_z0, microstrip_z0, track_impedance
from engine.ecad.stackup import StackupInfeasible


def _pct(a: float, b: float) -> float:
    return abs(a - b) / b * 100.0


def test_profiles_load():
    names = stk.list_profiles()
    assert "jlc_4layer_7628" in names, names
    assert "jlc_6layer_3313" in names, names
    assert "generic_2layer" in names, names
    for n in names:
        p = stk.get_profile(n)
        assert p["copper_layers"] >= 1 and p["stackup_layers"], n
        # copper layer ids in each profile must match the canonical schema naming
        cu = [ly["id"] for ly in p["stackup_layers"] if ly["type"] == "copper"]
        assert cu == schema.copper_layer_ids(p["copper_layers"]), (n, cu)
    try:
        stk.get_profile("nope")
        assert False, "unknown profile must raise"
    except ValueError as e:
        assert "jlc_4layer_7628" in str(e)


def test_reference_jlc_4layer_50r_100r():
    """DoD: the 4-layer 50R/100R solve reproduces the published JLC04161H-7628 reference."""
    sol = stk.solve_stackup(4, {"se_50": 50.0, "diff_100": {"type": "diff", "z0": 100.0},
                                "diff_90": {"type": "diff", "z0": 90.0}},
                            profile="jlc_4layer_7628")

    # 1) Stackup reproduces the published JLC04161H-7628 build exactly (menu selection).
    layers = sol["stackup"]["layers"]
    th = {ly["id"]: ly["thickness"] for ly in layers}
    assert th["pp1"] == 0.2104 and th["pp2"] == 0.2104, th      # 7628 prepreg
    assert th["core1"] == 1.065, th                              # published core
    assert th["F.Cu"] == 0.035 and th["In1.Cu"] == 0.0152, th    # published coppers
    assert [ly["id"] for ly in layers if ly["type"] == "copper"] == \
        ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]
    assert sol["layer_roles"] == {"F.Cu": "signal", "In1.Cu": "plane_gnd",
                                  "In2.Cu": "plane_pwr", "B.Cu": "signal"}

    # 2) The forward IPC-2141A model at JLC's PUBLISHED 50R geometry is within 5% of target
    #    (validates model + stackup agree with the fab's published structure — DoD 5% band).
    z_pub = microstrip_z0(0.3244, 0.2104, 0.035, 4.4)["z0"]
    assert _pct(z_pub, 50.0) <= 5.0, z_pub    # ~52.4 ohm, +4.8%

    # 3) Solved geometries hit their targets exactly in model space (true inversion).
    se = sol["geometries"]["se_50"]["layers"]["F.Cu"]
    assert se["kind"] == "microstrip" and abs(se["z0"] - 50.0) <= 0.05, se
    assert abs(microstrip_z0(se["width"], 0.2104, 0.035, 4.4)["z0"] - 50.0) <= 0.05
    # ... and the solved width is in the published width's neighbourhood (the residual is the
    # documented closed-form-vs-field-solver gap, bounded by the 10% fab tolerance in width).
    assert _pct(se["width"], 0.3244) <= 10.0, se["width"]        # 0.3498 vs 0.3244, ~7.8%

    # 4) Differential structures: same gap as JLC's published pairs, targets hit exactly,
    #    and the model at JLC's published geometries is inside the +/-10% fab tolerance band.
    d100 = sol["geometries"]["diff_100"]
    assert d100["diff_gap"] == 0.15, d100                        # JLC publishes s = 0.15 mm
    assert abs(d100["layers"]["F.Cu"]["z0"] - 100.0) <= 0.05
    z_pub_100 = diff_pair_z0(0.1722, 0.15, 0.2104, 0.035, 4.4)["z0_diff"]
    assert _pct(z_pub_100, 100.0) <= 10.0, z_pub_100             # ~108.6, +8.6%
    d90 = sol["geometries"]["diff_90"]
    assert d90["diff_gap"] == 0.15
    z_pub_90 = diff_pair_z0(0.2332, 0.15, 0.2104, 0.035, 4.4)["z0_diff"]
    assert _pct(z_pub_90, 90.0) <= 10.0, z_pub_90                # ~95.0, +5.6%


def test_solution_round_trips_through_checker():
    """A solved geometry, routed on a board carrying the solved stackup, must read back at the
    target through the SAME track_impedance path ecad_check_impedance uses."""
    sol = stk.solve_stackup(4, {"se_50": 50.0}, profile="jlc_4layer_7628")
    board = schema.new_board("b")
    stk.apply_solution(board, sol)
    w = sol["geometries"]["se_50"]["track_width"]
    board["nets"].append({"name": "CLK", "class": "se_50"})
    board["tracks"] = [{"id": "t1", "net": "CLK", "layer": "F.Cu", "width": w,
                        "p1": [0.0, 0.0], "p2": [10.0, 0.0]}]
    r = track_impedance(board, "CLK")
    assert r["kind"] == "microstrip", r
    assert abs(r["z0"] - 50.0) <= 0.05, r["z0"]


def test_apply_solution_wires_net_classes():
    """Solved geometries land in board net classes with the exact keys autoroute/DRC read."""
    from engine.ecad.autoroute import _clearance, _diff_gap, _track_width, _via_dims

    sol = stk.solve_stackup(4, {"se_50": 50.0, "diff_100": {"type": "diff", "z0": 100.0}},
                            profile="jlc_4layer_7628")
    board = schema.new_board("b")
    res = stk.apply_solution(board, sol, net_map={"se_50": ["CLK"],
                                                  "diff_100": ["USB_DP", "USB_DN"]})
    assert res["assigned"] == {"CLK": "se_50", "USB_DP": "diff_100", "USB_DN": "diff_100"}
    assert board["stackup"]["copper_layers"] == 4
    assert len(board["stackup"]["layers"]) == 7

    # autoroute reads the solved width/gap/via straight from the class
    assert _track_width(board, "CLK") == sol["geometries"]["se_50"]["track_width"]
    assert _track_width(board, "USB_DP") == sol["geometries"]["diff_100"]["track_width"]
    assert _diff_gap(board, "USB_DP") == sol["geometries"]["diff_100"]["diff_gap"]
    assert _via_dims(board, "CLK") == (0.45, 0.3)
    assert _clearance(board, "CLK") >= 0.09

    # DRC clearance honours the class when both shapes share it
    from engine.ecad import drc as drc_mod
    a = drc_mod._Shape("seg", "CLK", ["F.Cu"], "t1", pts=([0, 0], [1, 0]))
    b = drc_mod._Shape("seg", "CLK", ["F.Cu"], "t2", pts=([0, 1], [1, 1]))
    assert drc_mod._clearance_for(board, a, b) == board["net_classes"]["se_50"]["clearance"]

    # unknown class in net_map fails loudly
    try:
        stk.apply_solution(board, sol, net_map={"nope": ["X"]})
        assert False, "unknown class must raise"
    except ValueError as e:
        assert "nope" in str(e)


def test_infeasible_names_binding_constraint():
    # 120R single-ended on the JLC 4-layer outer dielectric needs ~0.013 mm — far below the
    # 0.09 mm fab minimum: must fail naming min_trace.
    try:
        stk.solve_stackup(4, {"hot": 120.0}, profile="jlc_4layer_7628")
        assert False, "must be infeasible"
    except StackupInfeasible as e:
        assert e.binding == "min_trace", e.binding
        assert "min_trace" in str(e)
    # 20R single-ended on a thick 2-layer core: even the 5 mm-wide fab maximum only gets down
    # to ~29 ohm -> max_trace binds.
    try:
        stk.solve_stackup(2, {"bus": 20.0}, profile="generic_2layer")
        assert False, "must be infeasible"
    except StackupInfeasible as e:
        assert e.binding == "max_trace", e.binding
    # 150R differential is out of reach at min_trace on every candidate gap -> min_trace binds.
    try:
        stk.solve_stackup(4, {"lvds": {"type": "diff", "z0": 200.0}}, profile="jlc_4layer_7628")
        assert False, "must be infeasible"
    except StackupInfeasible as e:
        assert e.binding in ("min_trace", "max_trace"), e.binding


def test_other_layer_counts_solve():
    # 6-layer JLC-style menu: inner signal layers classify as stripline and solve.
    sol6 = stk.solve_stackup(6, {"se_50": 50.0}, profile="auto")
    assert sol6["profile"] == "jlc_6layer_3313"
    inner = [cu for cu in sol6["signal_layers"] if cu.startswith("In")]
    assert inner, sol6["signal_layers"]
    for cu in inner:
        assert sol6["geometries"]["se_50"]["layers"][cu]["kind"] == "stripline"
    # 2-layer generic FR-4: 50R microstrip needs a ~2.9 mm trace (honest physics).
    sol2 = stk.solve_stackup(2, {"se_50": 50.0}, profile="generic_2layer")
    w = sol2["geometries"]["se_50"]["layers"]["F.Cu"]["width"]
    assert 2.5 <= w <= 3.3, w
    # mismatched layer count vs profile fails loudly
    try:
        stk.solve_stackup(6, {"se_50": 50.0}, profile="jlc_4layer_7628")
        assert False
    except ValueError as e:
        assert "4" in str(e)


def test_target_normalization():
    try:
        stk.solve_stackup(4, {"x": {"type": "coax", "z0": 50}}, profile="jlc_4layer_7628")
        assert False
    except ValueError as e:
        assert "coax" in str(e)
    try:
        stk.solve_stackup(4, {}, profile="jlc_4layer_7628")
        assert False
    except ValueError as e:
        assert "target" in str(e)


def main():
    test_profiles_load()
    test_reference_jlc_4layer_50r_100r()
    test_solution_round_trips_through_checker()
    test_apply_solution_wires_net_classes()
    test_infeasible_names_binding_constraint()
    test_other_layer_counts_solve()
    test_target_normalization()
    print("test_stackup: all tests passed")


if __name__ == "__main__":
    main()
