"""DRC + ratsnest tests — geometry core of the AI-ECAD PCB module.

House style: ``from __future__ import annotations``, plain ``main()`` + asserts, run as a module.
Fixtures are self-contained — small boards built inline from :func:`schema.new_board`, adding nets,
footprints with pads, tracks, and vias. Coordinates are mm.

Run:  & "<venv>/python.exe" -m engine.ecad.test_drc

Covers (per the task spec):
  1. Two different-net pads closer than clearance -> exactly one `clearance`; apart -> none;
     SAME net that close -> none (the net-aware guarantee).
  2. A track narrower than track_width_min -> `track_width`.
  3. A via with too-small annular ring -> `annular_ring`; too-small drill -> `via_size`.
  4. Two unconnected same-net pads, no track -> ratsnest 1 airwire -> one `unrouted`; add a track
     joining them -> ratsnest empty, no `unrouted`.
  5. Copper touching the board edge < edge_clearance -> `edge_clearance`.
"""

from __future__ import annotations

import json

from engine.ecad import schema
from engine.ecad.drc import run_drc, ratsnest, same_net, seg_seg_dist


# ── fixture builders ───────────────────────────────────────────────────────────────

def _board(name: str = "test") -> dict:
    """A fresh 2-layer board with the default rule set and no outline."""
    return schema.new_board(name)


def _add_net(board: dict, name: str, klass: str = "default") -> None:
    if not any(n.get("name") == name for n in board["nets"]):
        board["nets"].append({"name": name, "class": klass})


def _smd_pad(pid: str, pos, net, size=(0.5, 0.5), shape="circle", layer="F.Cu") -> dict:
    return {"id": pid, "number": "1", "pos": list(pos), "angle": 0.0, "shape": shape,
            "size": list(size), "type": "smd", "layers": [layer], "drill": None, "net": net}


def _tht_pad(pid: str, pos, net, size=(1.0, 1.0), drill=0.5) -> dict:
    return {"id": pid, "number": "1", "pos": list(pos), "angle": 0.0, "shape": "circle",
            "size": list(size), "type": "tht", "layers": ["F.Cu", "B.Cu"],
            "drill": {"diameter": drill, "shape": "round", "offset": [0, 0]}, "net": net}


def _fp(fid: str, pos, pads, side="top") -> dict:
    return {"id": fid, "fpid": "Test:FP", "ref": fid.upper(), "value": "", "pos": list(pos),
            "angle": 0.0, "side": side, "pads": pads, "graphics": [], "model_3d": None,
            "attr": "smd", "linked_symbol": None}


def _track(tid: str, p1, p2, net, width=0.25, layer="F.Cu") -> dict:
    return {"id": tid, "net": net, "layer": layer, "width": width, "kind": "segment",
            "p1": list(p1), "p2": list(p2)}


def _via(vid: str, at, net, drill=0.4, diameter=0.8) -> dict:
    return {"id": vid, "net": net, "at": list(at), "drill": drill, "diameter": diameter,
            "layers": ["F.Cu", "B.Cu"], "type": "through"}


def _rect_outline(board: dict, w: float, h: float, x0: float = 0.0, y0: float = 0.0) -> None:
    """A closed rectangular Edge.Cuts outline from (x0,y0) to (x0+w, y0+h)."""
    x1, y1 = x0 + w, y0 + h
    board["outline"] = {
        "shapes": [
            {"type": "line", "p1": [x0, y0], "p2": [x1, y0]},
            {"type": "line", "p1": [x1, y0], "p2": [x1, y1]},
            {"type": "line", "p1": [x1, y1], "p2": [x0, y1]},
            {"type": "line", "p1": [x0, y1], "p2": [x0, y0]},
        ],
        "closed": True,
    }


def _rules(viols) -> list[str]:
    return [v["rule"] for v in viols]


def _by_rule(viols, rule) -> list[dict]:
    return [v for v in viols if v["rule"] == rule]


# ── Test 1: net-aware clearance (the headline guarantee) ───────────────────────────

def test_clearance_net_aware():
    rules_clear = 0.2  # default global clearance

    # Two round pads on DIFFERENT nets, centers 0.6mm apart, radius 0.25 each -> edge gap 0.1mm
    # which is < 0.2 clearance -> exactly one clearance violation.
    def board_with_gap(net_a, net_b, dx):
        b = _board()
        _add_net(b, "NA"); _add_net(b, "NB")
        b["footprints"] = [
            _fp("fp1", (0.0, 0.0), [_smd_pad("p1", (0.0, 0.0), net_a, size=(0.5, 0.5))]),
            _fp("fp2", (dx, 0.0), [_smd_pad("p2", (0.0, 0.0), net_b, size=(0.5, 0.5))]),
        ]
        return b

    # 0.6mm center spacing, radii 0.25 -> 0.1mm edge gap < 0.2 -> violation
    close = board_with_gap("NA", "NB", 0.6)
    assert schema.validate_board(close) == [], schema.validate_board(close)
    viols = run_drc(close)
    clr = _by_rule(viols, "clearance")
    assert len(clr) == 1, f"expected exactly one clearance violation, got {len(clr)}: {clr}"
    assert clr[0]["severity"] == "error"
    assert set(clr[0]["items"]) == {"p1", "p2"}, clr[0]["items"]

    # Move them apart: 2.0mm spacing -> 1.5mm edge gap >> 0.2 -> none.
    far = board_with_gap("NA", "NB", 2.0)
    assert _by_rule(run_drc(far), "clearance") == [], "well-separated pads must not violate"

    # SAME net at the close 0.6mm spacing -> NO clearance violation (net-aware).
    same = board_with_gap("NA", "NA", 0.6)
    assert _by_rule(run_drc(same), "clearance") == [], \
        "same-net copper must NEVER raise a clearance violation"

    # purity: run_drc did not mutate the doc
    snap = json.dumps(close, sort_keys=True)
    run_drc(close)
    assert json.dumps(close, sort_keys=True) == snap, "run_drc mutated the board"
    print("  test_clearance_net_aware OK  (close=1 viol, far=0, same-net=0)")


# ── Test 1b: track-to-pad and track-to-track clearance, net-aware ──────────────────

def test_clearance_tracks():
    b = _board()
    _add_net(b, "NA"); _add_net(b, "NB")
    # Two parallel tracks, both width 0.25 (half 0.125), centerlines 0.3mm apart.
    # edge gap = 0.3 - 0.125 - 0.125 = 0.05 < 0.2 -> violation when different nets.
    b["tracks"] = [
        _track("t1", (0.0, 0.0), (5.0, 0.0), "NA", width=0.25),
        _track("t2", (0.0, 0.3), (5.0, 0.3), "NB", width=0.25),
    ]
    assert len(_by_rule(run_drc(b), "clearance")) == 1, "different-net parallel tracks too close"

    # same net -> none
    b["tracks"][1]["net"] = "NA"
    assert _by_rule(run_drc(b), "clearance") == [], "same-net tracks never clear-violate"
    print("  test_clearance_tracks OK")


# ── Test 2: track width below minimum ──────────────────────────────────────────────

def test_track_width():
    b = _board()
    _add_net(b, "NA")
    # default track_width_min = 0.15; make a 0.1mm track -> violation.
    b["tracks"] = [_track("t1", (0.0, 0.0), (5.0, 0.0), "NA", width=0.10)]
    viols = run_drc(b)
    tw = _by_rule(viols, "track_width")
    assert len(tw) == 1, f"expected one track_width violation, got {tw}"
    assert tw[0]["items"] == ["t1"]

    # widen it to a legal 0.25 -> none.
    b["tracks"][0]["width"] = 0.25
    assert _by_rule(run_drc(b), "track_width") == [], "legal-width track must not violate"
    print("  test_track_width OK")


# ── Test 3: via annular ring + drill ───────────────────────────────────────────────

def test_via_annular_and_drill():
    # default rules: via_diameter_min=0.45, via_drill_min=0.2, annular_ring_min=0.13
    # A via diameter 0.5, drill 0.45 -> ring (0.5-0.45)/2 = 0.025 < 0.13 -> annular_ring.
    # diameter 0.5 >= 0.45 ok; drill 0.45 >= 0.2 ok -> no via_size from this one.
    b = _board()
    _add_net(b, "NA")
    b["vias"] = [_via("v1", (0.0, 0.0), "NA", drill=0.45, diameter=0.5)]
    viols = run_drc(b)
    assert len(_by_rule(viols, "annular_ring")) == 1, f"expected annular_ring: {_rules(viols)}"
    assert _by_rule(viols, "via_size") == [], "this via meets diameter/drill minimums"
    assert _by_rule(viols, "annular_ring")[0]["items"] == ["v1"]

    # A via with too-small drill: drill 0.1 < 0.2 -> via_size. Use a fat diameter so the ring is ok.
    b2 = _board()
    _add_net(b2, "NA")
    b2["vias"] = [_via("v2", (0.0, 0.0), "NA", drill=0.1, diameter=0.8)]
    viols2 = run_drc(b2)
    vs = _by_rule(viols2, "via_size")
    assert len(vs) == 1, f"expected one via_size (drill too small), got {vs}"
    assert "drill" in vs[0]["message"], vs[0]["message"]
    # ring (0.8-0.1)/2 = 0.35 >= 0.13 so no annular_ring here.
    assert _by_rule(viols2, "annular_ring") == [], "fat via has a healthy ring"

    # A healthy via: diameter 0.8, drill 0.4 -> ring 0.2 -> nothing.
    b3 = _board()
    _add_net(b3, "NA")
    b3["vias"] = [_via("v3", (0.0, 0.0), "NA", drill=0.4, diameter=0.8)]
    v3 = run_drc(b3)
    assert _by_rule(v3, "annular_ring") == [] and _by_rule(v3, "via_size") == [], \
        "a healthy via must not raise via rules"
    print("  test_via_annular_and_drill OK")


# ── Test 4: ratsnest -> unrouted, and routing clears it ────────────────────────────

def test_ratsnest_and_unrouted():
    # Two pads on net NA, far apart, no track joining them.
    b = _board()
    _add_net(b, "NA")
    b["footprints"] = [
        _fp("fp1", (0.0, 0.0), [_smd_pad("p1", (0.0, 0.0), "NA")]),
        _fp("fp2", (10.0, 0.0), [_smd_pad("p2", (0.0, 0.0), "NA")]),
    ]
    air = ratsnest(b)
    assert len(air) == 1, f"two unconnected same-net pads -> exactly one airwire, got {air}"
    assert air[0]["net"] == "NA"
    # endpoints are the two pad world centers
    pts = {tuple(air[0]["p1"]), tuple(air[0]["p2"])}
    assert pts == {(0.0, 0.0), (10.0, 0.0)}, pts

    viols = run_drc(b)
    unrouted = _by_rule(viols, "unrouted")
    assert len(unrouted) == 1, f"expected one unrouted violation, got {unrouted}"
    assert unrouted[0]["severity"] == "warning"

    # Now add a track joining the two pad centers -> ratsnest empty, no unrouted.
    b["tracks"] = [_track("t1", (0.0, 0.0), (10.0, 0.0), "NA", width=0.25)]
    assert ratsnest(b) == [], "a track joining both pads must clear the ratsnest"
    assert _by_rule(run_drc(b), "unrouted") == [], "routed net must not be unrouted"

    # A track that only reaches the first pad does NOT connect them -> still one airwire.
    b["tracks"] = [_track("t1", (0.0, 0.0), (4.0, 0.0), "NA", width=0.25)]
    assert len(ratsnest(b)) == 1, "a track touching only one pad leaves the net unrouted"

    # determinism
    assert json.dumps(ratsnest(b), sort_keys=True) == json.dumps(ratsnest(b), sort_keys=True)
    print("  test_ratsnest_and_unrouted OK")


# ── Test 4b: ratsnest MST over 3 pads, partial routing ─────────────────────────────

def test_ratsnest_three_pads_mst():
    b = _board()
    _add_net(b, "NA")
    # three pads in a row at x=0,10,20 on net NA -> 2 airwires (a tree, not a clique)
    b["footprints"] = [
        _fp("fp1", (0.0, 0.0), [_smd_pad("p1", (0.0, 0.0), "NA")]),
        _fp("fp2", (10.0, 0.0), [_smd_pad("p2", (0.0, 0.0), "NA")]),
        _fp("fp3", (20.0, 0.0), [_smd_pad("p3", (0.0, 0.0), "NA")]),
    ]
    assert len(ratsnest(b)) == 2, "3 pads -> MST has 2 edges, not 3"

    # route p1-p2 with a track -> only 1 airwire (p2/p1 component to p3) remains
    b["tracks"] = [_track("t1", (0.0, 0.0), (10.0, 0.0), "NA")]
    air = ratsnest(b)
    assert len(air) == 1, f"one route leaves one airwire, got {air}"
    print("  test_ratsnest_three_pads_mst OK")


# ── Test 5: edge clearance ─────────────────────────────────────────────────────────

def test_edge_clearance():
    # 20x20 board outline. default edge_clearance = 0.3.
    b = _board()
    _add_net(b, "NA")
    _rect_outline(b, 20.0, 20.0)
    # A pad whose copper touches the left edge (x=0): center at x=0.1, radius 0.25 -> copper edge at
    # x=-0.15, i.e. distance to edge is effectively 0 (< 0.3) -> edge_clearance violation.
    b["footprints"] = [_fp("fp1", (0.1, 10.0), [_smd_pad("p1", (0.0, 0.0), "NA", size=(0.5, 0.5))])]
    viols = run_drc(b)
    ec = _by_rule(viols, "edge_clearance")
    assert len(ec) == 1, f"expected one edge_clearance violation, got {ec}"
    assert ec[0]["items"] == ["p1"]

    # Move the pad well inside (center at 10,10) -> no edge violation.
    b["footprints"][0]["pos"] = [10.0, 10.0]
    assert _by_rule(run_drc(b), "edge_clearance") == [], "interior copper must clear the edge"
    print("  test_edge_clearance OK")


# ── Test 6: hole-to-hole ───────────────────────────────────────────────────────────

def test_hole_to_hole():
    # default hole_to_hole_min = 0.25. Two tht pads, drills 0.5 each, centers 0.6 apart.
    # wall gap = 0.6 - 0.25 - 0.25 = 0.1 < 0.25 -> hole_to_hole.
    b = _board()
    _add_net(b, "NA"); _add_net(b, "NB")
    b["footprints"] = [
        _fp("fp1", (0.0, 0.0), [_tht_pad("p1", (0.0, 0.0), "NA", size=(1.5, 1.5), drill=0.5)]),
        _fp("fp2", (0.6, 0.0), [_tht_pad("p2", (0.0, 0.0), "NB", size=(1.5, 1.5), drill=0.5)]),
    ]
    viols = run_drc(b)
    h2h = _by_rule(viols, "hole_to_hole")
    assert len(h2h) == 1, f"expected one hole_to_hole, got {h2h}"
    assert set(h2h[0]["items"]) == {"p1", "p2"}

    # spread the holes to 3mm apart -> none.
    b["footprints"][1]["pos"] = [3.0, 0.0]
    assert _by_rule(run_drc(b), "hole_to_hole") == [], "well-spaced holes must not violate"
    print("  test_hole_to_hole OK")


# ── Test 7: net-class clearance overrides the global ───────────────────────────────

def test_net_class_clearance_override():
    b = _board()
    # a "wide" class with a big clearance; both nets in it.
    b["net_classes"]["wide"] = {"clearance": 0.5, "track_width": 0.3,
                                "via_diameter": 1.0, "via_drill": 0.5}
    _add_net(b, "NA", klass="wide")
    _add_net(b, "NB", klass="wide")
    # pads at 0.6mm spacing, radius 0.25 -> 0.1mm gap. Global clearance 0.2 would NOT fire on a
    # 0.1 gap? 0.1 < 0.2 actually fires. Use a gap that passes global but fails the class:
    # spacing 1.0 -> gap 0.5 ; class clearance 0.5 -> 0.5 < 0.5 is false. Use spacing 0.9 ->
    # gap 0.4 < 0.5 class but >= 0.2 global -> fires only because of the class override.
    b["footprints"] = [
        _fp("fp1", (0.0, 0.0), [_smd_pad("p1", (0.0, 0.0), "NA", size=(0.5, 0.5))]),
        _fp("fp2", (0.9, 0.0), [_smd_pad("p2", (0.0, 0.0), "NB", size=(0.5, 0.5))]),
    ]
    # gap = 0.9 - 0.5 = 0.4. global(0.2): pass. class(0.5): fail.
    viols = run_drc(b)
    clr = _by_rule(viols, "clearance")
    assert len(clr) == 1, f"net-class clearance (0.5) should fire on a 0.4mm gap: {clr}"
    assert "0.5" in clr[0]["message"], clr[0]["message"]

    # demote NB to default class -> classes differ -> global (0.2) applies -> 0.4 gap passes.
    for n in b["nets"]:
        if n["name"] == "NB":
            n["class"] = "default"
    assert _by_rule(run_drc(b), "clearance") == [], \
        "mismatched classes fall back to the global clearance (0.2), which 0.4mm passes"
    print("  test_net_class_clearance_override OK")


# ── Test 8: same_net helper + segment distance util ────────────────────────────────

def test_helpers():
    assert same_net("GND", "GND") is True
    assert same_net("GND", "VCC") is False
    assert same_net(None, None) is False, "two unassigned nets are NOT the same net"
    assert same_net("", "") is False
    assert same_net("GND", None) is False

    # segment distance: parallel segments 1mm apart
    d = seg_seg_dist((0.0, 0.0), (5.0, 0.0), (0.0, 1.0), (5.0, 1.0))
    assert abs(d - 1.0) < 1e-9, d
    # crossing segments -> 0
    d2 = seg_seg_dist((-1.0, 0.0), (1.0, 0.0), (0.0, -1.0), (0.0, 1.0))
    assert d2 == 0.0, d2
    # endpoint-to-endpoint
    d3 = seg_seg_dist((0.0, 0.0), (1.0, 0.0), (4.0, 0.0), (5.0, 0.0))
    assert abs(d3 - 3.0) < 1e-9, d3
    print("  test_helpers OK")


# ── Test 9: determinism + non-board input ──────────────────────────────────────────

def test_determinism_and_guard():
    b = _board()
    _add_net(b, "NA"); _add_net(b, "NB")
    b["footprints"] = [
        _fp("fp1", (0.0, 0.0), [_smd_pad("p1", (0.0, 0.0), "NA", size=(0.5, 0.5))]),
        _fp("fp2", (0.6, 0.0), [_smd_pad("p2", (0.0, 0.0), "NB", size=(0.5, 0.5))]),
    ]
    a = json.dumps(run_drc(b), sort_keys=True)
    c = json.dumps(run_drc(b), sort_keys=True)
    assert a == c, "run_drc is not deterministic"

    # non-board input never raises, returns []
    assert run_drc({"schema": "aicad.sch/1"}) == []
    assert run_drc({}) == []
    print("  test_determinism_and_guard OK")


# ── Test M1: edge_distance reports true signed gap (no 0-floor) ───────────────────────

def test_m1_true_clearance_distance():
    """M1: DRC clearance message must report the REAL edge-to-edge gap, not '0mm'.

    With adjacent SOIC-style rect pads (pitch 1.27mm, pad height 1.55mm) the pads
    GEOMETRICALLY OVERLAP by 0.28mm.  The old code clamped edge_distance to 0 so the
    violation message read "clearance 0mm".  After the fix, the signed gap (-0.28mm) is
    surfaced so the message shows the real measurement.

    For clearly-separated pads (gap 0.1mm, < required 0.2mm), the reported distance
    must still be exactly 0.1mm (positive, no clamping needed there either).
    """
    from engine.ecad.drc import edge_distance, _pad_shape

    # ── sub-test A: separated pads (gap=0.1mm > 0) stays correct ──
    b = _board()
    _add_net(b, "NA"); _add_net(b, "NB")
    # Two circle pads, centers 0.6mm apart, radius 0.25 → gap 0.1mm
    b["footprints"] = [
        _fp("fp1", (0.0, 0.0), [_smd_pad("p1", (0.0, 0.0), "NA", size=(0.5, 0.5))]),
        _fp("fp2", (0.6, 0.0), [_smd_pad("p2", (0.0, 0.0), "NB", size=(0.5, 0.5))]),
    ]
    viols = run_drc(b)
    clr = [v for v in viols if v["rule"] == "clearance"]
    assert len(clr) == 1, clr
    msg = clr[0]["message"]
    assert "0.1" in msg, f"expected 0.1mm gap in message, got: {msg!r}"
    assert "0mm" not in msg.split()[1], f"gap must not be rounded to '0mm': {msg!r}"

    # ── sub-test B: overlapping rect pads (SOIC-style) must report negative gap ──
    # pitch 1.27mm, pad height 1.55mm → overlap -0.28mm on Y axis
    b2 = _board()
    _add_net(b2, "NA"); _add_net(b2, "NB")
    b2["footprints"] = [
        _fp("fp3", (0.0, 0.0), [
            _smd_pad("p3", (0.0, 1.27 / 2), "NA", size=(0.6, 1.55), shape="rect"),
            _smd_pad("p4", (0.0, -1.27 / 2), "NB", size=(0.6, 1.55), shape="rect"),
        ]),
    ]
    viols2 = run_drc(b2)
    clr2 = [v for v in viols2 if v["rule"] == "clearance"]
    # Must fire: gap is -0.28mm which is < required 0.2mm
    assert len(clr2) == 1, f"overlapping SOIC pads must raise clearance, got: {clr2}"
    msg2 = clr2[0]["message"]
    # The gap must NOT say "0.0mm" — it should show a negative value
    assert "0.0mm" not in msg2 or "-" in msg2, \
        f"overlapping-pad violation must not report 0.0mm gap: {msg2!r}"
    # Explicitly verify the signed edge_distance is negative for these shapes
    n_cu = 2
    from engine.ecad.drc import _pad_shape as _ps
    sh3 = _ps(b2["footprints"][0], b2["footprints"][0]["pads"][0], n_cu)
    sh4 = _ps(b2["footprints"][0], b2["footprints"][0]["pads"][1], n_cu)
    d = edge_distance(sh3, sh4)
    assert d < 0, f"overlapping SOIC pads must have negative edge_distance, got {d}"
    print("  test_m1_true_clearance_distance OK")


def main() -> None:
    print("=" * 60)
    print("AI-ECAD DRC + RATSNEST TESTS")
    print("=" * 60)
    test_clearance_net_aware()
    test_clearance_tracks()
    test_track_width()
    test_via_annular_and_drill()
    test_ratsnest_and_unrouted()
    test_ratsnest_three_pads_mst()
    test_edge_clearance()
    test_hole_to_hole()
    test_net_class_clearance_override()
    test_helpers()
    test_determinism_and_guard()
    test_m1_true_clearance_distance()
    print("ALL DRC + RATSNEST TESTS PASSED")


if __name__ == "__main__":
    main()
