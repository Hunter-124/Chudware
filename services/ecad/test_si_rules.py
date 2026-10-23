"""Tests for services.ecad.si_rules — the high-speed/SI rule pack.

Fixture boards (built programmatically, mirroring test_drc.py style):
  * DDR-ish match-group board — 4 byte-lane nets, one routed short → si_match_length fires
    on exactly that net with a per-net delta.
  * Unterminated clock board — net class demands series termination, no R → si_termination.
  * Clean board — same checks declared, geometry compliant → zero violations.
"""

from __future__ import annotations

import math

from services.ecad import schema
from services.ecad import si_rules


# ── board builders ────────────────────────────────────────────────────────────


def _add_net(board, name, cls=None):
    board["nets"].append({"name": name, "class": cls})


def _route(board, net, length, y, layer="F.Cu", width=0.2):
    tid = schema.next_id(board, "t")
    board["tracks"].append({"id": tid, "kind": "segment", "net": net, "layer": layer,
                            "width": width, "p1": [0.0, y], "p2": [length, y]})
    return tid


def _resistor(board, ref, net_a, net_b, at):
    fid = schema.next_id(board, "fp")
    board["footprints"].append({
        "id": fid, "ref": ref, "fpid": "Resistor_SMD:R_0402", "pos": list(at),
        "side": "top", "angle": 0.0,
        "pads": [
            {"id": "1", "type": "smd", "shape": "rect", "pos": [-0.5, 0.0],
             "size": [0.5, 0.5], "layers": ["F.Cu"], "net": net_a},
            {"id": "2", "type": "smd", "shape": "rect", "pos": [0.5, 0.0],
             "size": [0.5, 0.5], "layers": ["F.Cu"], "net": net_b},
        ]})
    return fid


def ddr_board(*, bad_member=True):
    """DDR-ish byte lane: 4 data nets in match group 'ddr_byte0', tol 0.5 mm."""
    b = schema.new_board("ddrish")
    for i in range(4):
        _add_net(b, f"DQ{i}")
    si_rules.set_net_class(b, "ddr_byte0", {"match_tolerance_mm": 0.5},
                           nets=[f"DQ{i}" for i in range(4)])
    # three nets at ~30 mm; the fourth 2 mm short (or matched, for the clean variant)
    _route(b, "DQ0", 30.0, 1.0)
    _route(b, "DQ1", 30.2, 2.0)
    _route(b, "DQ2", 29.9, 3.0)
    _route(b, "DQ3", 28.0 if bad_member else 30.1, 4.0)
    return b


def clock_board(*, terminated=False):
    """Single clock net whose class demands a series termination at the driver."""
    b = schema.new_board("clkboard")
    _add_net(b, "CLK")
    _add_net(b, "CLK_SRC")
    si_rules.set_net_class(
        b, "clk50",
        {"termination": {"scheme": "series", "value_ohm": 33.0,
                         "max_dist_mm": 10.0, "driver_ref": "U1"},
         "rise_time_ps": 500.0},
        nets=["CLK"])
    _route(b, "CLK", 40.0, 0.0)
    # the driver footprint
    b["footprints"].append({
        "id": schema.next_id(b, "fp"), "ref": "U1", "fpid": "SOIC-8", "pos": [0.0, 0.0],
        "side": "top", "angle": 0.0,
        "pads": [{"id": "1", "type": "smd", "shape": "rect", "pos": [0.0, 0.0],
                  "size": [0.6, 1.0], "layers": ["F.Cu"], "net": "CLK_SRC"}]})
    if terminated:
        _resistor(b, "R1", "CLK_SRC", "CLK", (2.0, 0.0))
    return b


def clean_board():
    """Everything declared, everything compliant — must produce zero violations."""
    b = ddr_board(bad_member=False)
    _add_net(b, "CLK")
    _add_net(b, "CLK_SRC")
    si_rules.set_net_class(
        b, "clk50",
        {"termination": {"scheme": "series", "max_dist_mm": 10.0, "driver_ref": "U1"}},
        nets=["CLK"])
    _route(b, "CLK", 20.0, 6.0)
    b["footprints"].append({
        "id": schema.next_id(b, "fp"), "ref": "U1", "fpid": "SOIC-8", "pos": [0.0, 6.0],
        "side": "top", "angle": 0.0,
        "pads": [{"id": "1", "type": "smd", "shape": "rect", "pos": [0.0, 0.0],
                  "size": [0.6, 1.0], "layers": ["F.Cu"], "net": "CLK_SRC"}]})
    _resistor(b, "R1", "CLK_SRC", "CLK", (2.0, 6.0))
    return b


def _rules(res, rule):
    return [v for v in res["violations"] if v["rule"] == rule]


# ── match groups ─────────────────────────────────────────────────────────────


def test_match_group_flags_only_the_short_net():
    res = si_rules.run_sicheck(ddr_board(bad_member=True))
    v = _rules(res, "si_match_length")
    assert len(v) == 1
    assert v[0]["items"] == ["DQ3"]
    assert v[0]["severity"] == "warning"
    assert "ddr_byte0" in v[0]["message"]
    # per-net delta table in the report
    rep = res["match_report"]["ddr_byte0"]["nets"]
    assert set(rep) == {"DQ0", "DQ1", "DQ2", "DQ3"}
    assert rep["DQ3"]["ok"] is False
    assert rep["DQ0"]["ok"] is True
    assert math.isclose(rep["DQ3"]["length_mm"], 28.0, abs_tol=1e-6)
    assert rep["DQ3"]["delta_mm"] < -1.0  # ~ -2.05 vs the median


def test_match_group_clean_passes():
    res = si_rules.run_sicheck(ddr_board(bad_member=False))
    assert _rules(res, "si_match_length") == []


def test_pair_skew():
    b = schema.new_board("pairs")
    for n in ("LVDS_P", "LVDS_N"):
        _add_net(b, n)
    si_rules.set_net_class(b, "lvds", {"pair_skew_mm": 0.1}, nets=["LVDS_P", "LVDS_N"])
    _route(b, "LVDS_P", 25.0, 0.0)
    _route(b, "LVDS_N", 24.5, 0.2)  # 0.5 mm skew > 0.1 tol
    res = si_rules.run_sicheck(b)
    v = _rules(res, "si_pair_skew")
    assert len(v) == 1 and v[0]["items"] == ["LVDS_N", "LVDS_P"]
    # fix the skew -> passes
    _route(b, "LVDS_N", 0.5, 0.4)
    assert _rules(si_rules.run_sicheck(b), "si_pair_skew") == []


def test_infer_pairs_suffix_conventions():
    assert si_rules.infer_pairs(["A_P", "A_N", "B+", "B-", "X"]) == \
        [("A_N", "A_P")] or True  # order depends on suffix table; check membership instead
    pairs = si_rules.infer_pairs(["A_P", "A_N", "B+", "B-", "X"])
    flat = {frozenset(p) for p in pairs}
    assert frozenset({"A_P", "A_N"}) in flat
    assert frozenset({"B+", "B-"}) in flat


# ── termination ──────────────────────────────────────────────────────────────


def test_unterminated_clock_flagged():
    res = si_rules.run_sicheck(clock_board(terminated=False))
    v = _rules(res, "si_termination")
    assert len(v) == 1 and v[0]["items"] == ["CLK"]
    assert "SERIES" in v[0]["message"]


def test_terminated_clock_passes():
    res = si_rules.run_sicheck(clock_board(terminated=True))
    assert _rules(res, "si_termination") == []


def test_series_termination_too_far_from_driver():
    b = clock_board(terminated=True)
    # move the R 30 mm away from U1 (max_dist 10 mm)
    for fp in b["footprints"]:
        if fp.get("ref") == "R1":
            fp["pos"] = [30.0, 0.0]
    v = _rules(si_rules.run_sicheck(b), "si_termination")
    assert len(v) == 1 and "from driver U1" in v[0]["message"]


def test_parallel_and_thevenin_termination():
    b = schema.new_board("term")
    for n in ("SIG_A", "SIG_B", "VTT", "VDD"):
        _add_net(b, n)
    si_rules.set_net_class(b, "par", {"termination": {"scheme": "parallel"}}, nets=["SIG_A"])
    si_rules.set_net_class(b, "thev", {"termination": {"scheme": "thevenin"}}, nets=["SIG_B"])
    _route(b, "SIG_A", 10.0, 0.0)
    _route(b, "SIG_B", 10.0, 1.0)
    res = si_rules.run_sicheck(b)
    assert len(_rules(res, "si_termination")) == 2  # both missing
    _resistor(b, "R1", "SIG_A", "GND", (10.0, 0.0))    # parallel to GND
    _resistor(b, "R2", "SIG_B", "VDD", (10.0, 1.0))    # thevenin upper
    _resistor(b, "R3", "SIG_B", "GND", (10.0, 1.5))    # thevenin lower
    assert _rules(si_rules.run_sicheck(b), "si_termination") == []


# ── via stubs ────────────────────────────────────────────────────────────────


def _fourlayer_stackup(b):
    b["stackup"] = {
        "copper_layers": 4, "thickness": 1.6,
        "layers": [
            {"id": "F.Cu", "type": "copper", "thickness": 0.035},
            {"id": "d1", "type": "prepreg", "thickness": 0.2, "er": 4.4},
            {"id": "In1.Cu", "type": "copper", "thickness": 0.035},
            {"id": "d2", "type": "core", "thickness": 1.0, "er": 4.5},
            {"id": "In2.Cu", "type": "copper", "thickness": 0.035},
            {"id": "d3", "type": "prepreg", "thickness": 0.2, "er": 4.4},
            {"id": "B.Cu", "type": "copper", "thickness": 0.035},
        ]}


def test_via_stub_flagged_for_fast_edge_and_ok_for_slow():
    b = schema.new_board("stub")
    _fourlayer_stackup(b)
    _add_net(b, "FAST")
    si_rules.set_net_class(b, "fast", {"rise_time_ps": 20.0}, nets=["FAST"])
    # signal routed F.Cu -> In1.Cu through a full-board via: ~1.3 mm stub below In1.Cu
    _route(b, "FAST", 10.0, 0.0, layer="F.Cu")
    _route(b, "FAST", 10.0, 0.0, layer="In1.Cu")
    b["vias"].append({"id": "v1", "type": "through", "net": "FAST", "at": [10.0, 0.0],
                      "diameter": 0.6, "drill": 0.3, "layers": ["F.Cu", "B.Cu"]})
    v = _rules(si_rules.run_sicheck(b), "si_via_stub")
    assert len(v) == 1 and "back-drill" in v[0]["message"]
    # a slow edge (10 ns) tolerates the same stub
    b["net_classes"]["fast"]["rise_time_ps"] = 10_000.0
    assert _rules(si_rules.run_sicheck(b), "si_via_stub") == []
    # ...and a full-depth signal (F.Cu -> B.Cu) has no stub even at 20 ps
    b["net_classes"]["fast"]["rise_time_ps"] = 20.0
    _route(b, "FAST", 5.0, 0.0, layer="B.Cu")
    assert _rules(si_rules.run_sicheck(b), "si_via_stub") == []


# ── BGA fanout ───────────────────────────────────────────────────────────────


def _bga(b, *, pitch=0.8, n=6, pad=0.4, ref="U9"):
    pads = []
    for r in range(n):
        for c in range(n):
            pads.append({"id": f"{chr(65 + r)}{c + 1}", "type": "smd", "shape": "circle",
                         "pos": [c * pitch, r * pitch], "size": [pad, pad],
                         "layers": ["F.Cu"], "net": "GND"})
    fid = schema.next_id(b, "fp")
    b["footprints"].append({"id": fid, "ref": ref, "fpid": "BGA-36", "pos": [0.0, 0.0],
                            "side": "top", "angle": 0.0, "pads": pads})
    return fid


def test_bga_via_in_pad_flagged():
    b = schema.new_board("bga")
    _bga(b)
    b["vias"].append({"id": "v1", "type": "through", "net": "GND", "at": [0.8, 0.8],
                      "diameter": 0.45, "drill": 0.2, "layers": ["F.Cu", "B.Cu"]})
    v = _rules(si_rules.run_sicheck(b), "si_bga_via_in_pad")
    assert len(v) == 1 and "via-in-pad" in v[0]["message"]


def test_bga_escape_infeasible_flagged():
    b = schema.new_board("bga2")
    # 0.5 mm pitch, 0.3 mm balls -> 0.2 mm channel < 0.15 + 2*0.2 = 0.55 needed
    _bga(b, pitch=0.5, pad=0.3, n=8)
    v = _rules(si_rules.run_sicheck(b), "si_bga_escape")
    assert len(v) == 1 and "escape channel" in v[0]["message"]


def test_bga_congestion_flagged():
    b = schema.new_board("bga3")
    _bga(b, pitch=0.8, pad=0.4, n=6)
    # carpet the field with fat vias -> occupancy > 50%
    for r in range(5):
        for c in range(5):
            b["vias"].append({"id": f"v{r * 5 + c + 1}", "type": "through", "net": "GND",
                              "at": [0.4 + c * 0.8, 0.4 + r * 0.8],
                              "diameter": 0.7, "drill": 0.3, "layers": ["F.Cu", "B.Cu"]})
    v = _rules(si_rules.run_sicheck(b), "si_bga_congestion")
    assert len(v) == 1 and "congestion" in v[0]["message"]


def test_bga_clean_no_flags():
    b = schema.new_board("bga4")
    _bga(b, pitch=1.0, pad=0.45, n=6)  # roomy 1.0 mm pitch, no vias
    res = si_rules.run_sicheck(b)
    assert all(not v["rule"].startswith("si_bga") for v in res["violations"])


# ── clean board + envelope ───────────────────────────────────────────────────


def test_clean_board_all_checks_pass():
    res = si_rules.run_sicheck(clean_board())
    assert res["violations"] == []
    assert res["match_report"]["ddr_byte0"]["nets"]["DQ0"]["ok"] is True


def test_limitations_always_stated_and_output_deterministic():
    b = ddr_board(bad_member=True)
    res1 = si_rules.run_sicheck(b)
    res2 = si_rules.run_sicheck(b)
    assert res1 == res2
    assert res1["limitations"] == si_rules.LIMITATIONS
    assert any("field solver" in s for s in res1["limitations"])
    assert any("IBIS" in s for s in res1["limitations"])


def test_non_board_doc_is_harmless():
    res = si_rules.run_sicheck({"schema": "nope"})
    assert res["violations"] == [] and res["limitations"]


def test_set_net_class_merges_additively():
    b = schema.new_board("m")
    si_rules.set_net_class(b, "hs", {"track_width": 0.2}, nets=["A"])
    si_rules.set_net_class(b, "hs", {"match_tolerance_mm": 0.3}, nets=["B"])
    nc = b["net_classes"]["hs"]
    assert nc["track_width"] == 0.2 and nc["match_tolerance_mm"] == 0.3
    assigned = {n["name"]: n["class"] for n in b["nets"] if n.get("class") == "hs"}
    assert set(assigned) == {"A", "B"}
