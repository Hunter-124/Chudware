"""DFM (Design-For-Manufacturing) check suite tests.

House style: ``from __future__ import annotations``, plain ``main()`` + asserts,
run as a module.

Run:
    & "<venv>/python.exe" -m engine.ecad.test_dfm

Covers every DFM rule with a seeded violation (fires) and a clean case (silent):
  1. silk_over_pad     -- silk line crossing a pad; move it off -> none.
  2. silk_min_width    -- silk line below 0.15mm width; widen -> none.
  3. silk_min_text     -- silk text below 0.8mm height; increase -> none.
  4. mask_sliver       -- two pads with mask opening closer than 0.1mm; widen -> none.
  5. acid_trap         -- two same-net tracks meeting at ~30deg; obtuse junction -> none.
  6. courtyard_overlap -- two overlapping courtyard boxes; separate -> none.
  7. drill_min         -- via drill below 0.2mm fab minimum; increase -> none.
  8. annular_min       -- via annular ring below 0.15mm DFM minimum; fix -> none.
  9. Non-mutation + determinism.
"""

from __future__ import annotations

import json

from engine.ecad import schema
from engine.ecad.dfm import run_dfm


# ── Shared fixture helpers ────────────────────────────────────────────────────

def _board() -> dict:
    return schema.new_board("dfm-test")


def _add_net(board: dict, name: str) -> None:
    if not any(n.get("name") == name for n in board.get("nets", [])):
        board.setdefault("nets", []).append({"name": name, "class": "default"})


def _fp(fid: str, pos, pads=None, graphics=None, side="top") -> dict:
    return {
        "id": fid, "fpid": "Test:FP", "ref": fid.upper(), "value": "",
        "pos": list(pos), "angle": 0.0, "side": side,
        "pads": pads or [], "graphics": graphics or [],
        "model_3d": None, "attr": "smd", "linked_symbol": None,
    }


def _smd_pad(pid: str, pos, net, size=(1.0, 1.0), shape="rect",
             layers=None) -> dict:
    if layers is None:
        layers = ["F.Cu", "F.Mask", "F.Paste"]
    return {
        "id": pid, "number": "1", "pos": list(pos), "angle": 0.0,
        "shape": shape, "size": list(size), "type": "smd",
        "layers": layers, "drill": None, "net": net, "pin": "1",
    }


def _tht_pad(pid: str, pos, net, size=(1.5, 1.5), drill=0.5) -> dict:
    return {
        "id": pid, "number": "1", "pos": list(pos), "angle": 0.0,
        "shape": "circle", "size": list(size), "type": "tht",
        "layers": ["F.Cu", "B.Cu", "F.Mask", "B.Mask"],
        "drill": {"diameter": drill, "shape": "round", "offset": [0, 0]},
        "net": net, "pin": "1",
    }


def _via(vid: str, at, net, drill=0.4, diameter=0.8) -> dict:
    return {"id": vid, "net": net, "at": list(at), "drill": drill,
            "diameter": diameter, "layers": ["F.Cu", "B.Cu"], "type": "through"}


def _silk_line(gid: str, p1, p2, width=0.15, layer="F.SilkS") -> dict:
    return {"id": gid, "type": "line", "layer": layer,
            "p1": list(p1), "p2": list(p2), "width": width}


def _silk_text(gid: str, pos, height=1.0, layer="F.SilkS") -> dict:
    return {"id": gid, "type": "text", "layer": layer,
            "pos": list(pos), "text": "TEST", "size": [height, height], "width": 0.15}


def _crtyd_line(gid: str, p1, p2, layer="F.CrtYd") -> dict:
    return {"id": gid, "type": "line", "layer": layer,
            "p1": list(p1), "p2": list(p2), "width": 0.05}


def _track(tid: str, p1, p2, net, width=0.25, layer="F.Cu") -> dict:
    return {"id": tid, "net": net, "layer": layer, "width": width,
            "kind": "segment", "p1": list(p1), "p2": list(p2)}


def _rules(viols) -> list[str]:
    return [v["rule"] for v in viols]


def _by_rule(viols, rule) -> list[dict]:
    return [v for v in viols if v["rule"] == rule]


# ── Test 1: silk_over_pad ─────────────────────────────────────────────────────

def test_silk_over_pad():
    """A silk line crossing a pad fires silk_over_pad; moving it off clears the violation."""
    b = _board()
    _add_net(b, "NA")
    # SMD pad at (5, 5), 2x2mm rect -> half-extents 1x1mm
    pad = _smd_pad("p1", (0.0, 0.0), "NA", size=(2.0, 2.0))
    fp1 = _fp("fp1", (5.0, 5.0), pads=[pad])
    b["footprints"] = [fp1]
    # Silk line that passes directly through the pad center (5,5)
    # crossing from (3,5) to (7,5) -> must overlap the 2x2 pad at (5,5)
    b["graphics"] = [_silk_line("g1", (3.0, 5.0), (7.0, 5.0), width=0.15)]

    viols = run_dfm(b)
    fired = _by_rule(viols, "silk_over_pad")
    assert len(fired) >= 1, f"expected silk_over_pad, got {_rules(viols)}"
    assert "g1" in fired[0]["items"] or "p1" in fired[0]["items"], fired[0]

    # Move the silk line well above the pad -> no overlap
    b["graphics"] = [_silk_line("g1", (3.0, 10.0), (7.0, 10.0), width=0.15)]
    viols2 = run_dfm(b)
    assert _by_rule(viols2, "silk_over_pad") == [], \
        f"silk line far from pad must not fire: {_rules(viols2)}"
    print("  test_silk_over_pad OK")


# ── Test 2: silk_min_width ────────────────────────────────────────────────────

def test_silk_min_width():
    """A silk line narrower than 0.15mm fires silk_min_width; widening clears it."""
    b = _board()
    b["graphics"] = [_silk_line("g1", (0.0, 0.0), (5.0, 0.0), width=0.08)]

    viols = run_dfm(b)
    fired = _by_rule(viols, "silk_min_width")
    assert len(fired) == 1, f"expected silk_min_width, got {_rules(viols)}"
    assert fired[0]["items"] == ["g1"]
    assert "0.08" in fired[0]["message"] or "8e-02" in fired[0]["message"], fired[0]["message"]

    # Widen to exactly the minimum -> no violation
    b["graphics"][0]["width"] = 0.15
    assert _by_rule(run_dfm(b), "silk_min_width") == [], \
        "silk line at minimum width must not fire"

    # Widen beyond minimum
    b["graphics"][0]["width"] = 0.2
    assert _by_rule(run_dfm(b), "silk_min_width") == [], \
        "silk line above minimum width must not fire"
    print("  test_silk_min_width OK")


# ── Test 3: silk_min_text ─────────────────────────────────────────────────────

def test_silk_min_text():
    """Silk text below 0.8mm height fires silk_min_text; increasing height clears it."""
    b = _board()
    b["graphics"] = [_silk_text("g1", (5.0, 5.0), height=0.5)]

    viols = run_dfm(b)
    fired = _by_rule(viols, "silk_min_text")
    assert len(fired) == 1, f"expected silk_min_text, got {_rules(viols)}"
    assert fired[0]["items"] == ["g1"]

    # Raise text height to legal value
    b["graphics"][0]["size"] = [1.0, 1.0]
    assert _by_rule(run_dfm(b), "silk_min_text") == [], \
        "silk text at legal height must not fire"
    print("  test_silk_min_text OK")


# ── Test 4: mask_sliver ───────────────────────────────────────────────────────

def test_mask_sliver():
    """Two pads with a mask opening gap < 0.1mm fires mask_sliver; widening gap clears it."""
    b = _board()
    _add_net(b, "NA")
    _add_net(b, "NB")
    # Two rect pads, each 1x1mm, on F.Cu with F.Mask.
    # Pad centers at x=0 and x=1.05 -> edge gap = 1.05 - 0.5 - 0.5 = 0.05mm < 0.1mm sliver min.
    pad_a = _smd_pad("p1", (0.0, 0.0), "NA", size=(1.0, 1.0))
    pad_b = _smd_pad("p2", (0.0, 0.0), "NB", size=(1.0, 1.0))
    fp_a = _fp("fp1", (0.0, 0.0), pads=[pad_a])
    fp_b = _fp("fp2", (1.05, 0.0), pads=[pad_b])
    b["footprints"] = [fp_a, fp_b]

    viols = run_dfm(b)
    fired = _by_rule(viols, "mask_sliver")
    assert len(fired) >= 1, f"expected mask_sliver, got {_rules(viols)}"
    items = fired[0]["items"]
    assert "p1" in items and "p2" in items, items

    # Move fp_b to 2.0mm spacing -> 1.0mm gap > 0.1mm -> no sliver
    fp_b["pos"] = [2.0, 0.0]
    viols2 = run_dfm(b)
    assert _by_rule(viols2, "mask_sliver") == [], \
        f"well-spaced pads must not fire mask_sliver: {_rules(viols2)}"
    print("  test_mask_sliver OK")


# ── Test 5: acid_trap / acute_angle ──────────────────────────────────────────

def test_acid_trap():
    """Same-net tracks meeting at ~30 deg fires acid_trap; obtuse meeting does not."""
    b = _board()
    _add_net(b, "NA")
    # Two tracks sharing endpoint at (0,0).
    # Track A goes to (10,0) (angle 0 deg from origin).
    # Track B goes to (10*cos30, 10*sin30) = ~(8.66, 5.0) (angle 30 deg from origin).
    # Angle BETWEEN outgoing vectors = 30 deg < 90 deg threshold -> acid_trap fires.
    import math
    end_b = (10.0 * math.cos(math.radians(30)), 10.0 * math.sin(math.radians(30)))
    b["tracks"] = [
        _track("t1", (0.0, 0.0), (10.0, 0.0), "NA"),
        _track("t2", (0.0, 0.0), end_b, "NA"),
    ]
    viols = run_dfm(b)
    fired = _by_rule(viols, "acid_trap")
    assert len(fired) >= 1, f"expected acid_trap at 30deg, got {_rules(viols)}"
    assert "30" in fired[0]["message"] or "30." in fired[0]["message"], fired[0]["message"]

    # Replace track B with one going to (0, 10) (90 deg from A).
    # Angle between vectors = 90 deg. Default threshold is <= 90 deg, so 90 deg DOES fire.
    # Use 91 deg to be safely above the threshold.
    end_c = (10.0 * math.cos(math.radians(91)), 10.0 * math.sin(math.radians(91)))
    b["tracks"][1] = _track("t2", (0.0, 0.0), end_c, "NA")
    viols2 = run_dfm(b)
    assert _by_rule(viols2, "acid_trap") == [], \
        f"obtuse (91 deg) tracks must not fire acid_trap: {_rules(viols2)}"

    # Different nets at an acute angle -> must NOT fire (acid trap is same-net only)
    _add_net(b, "NB")
    b["tracks"][1] = _track("t2", (0.0, 0.0), end_b, "NB")
    viols3 = run_dfm(b)
    assert _by_rule(viols3, "acid_trap") == [], \
        "different-net tracks must not fire acid_trap"
    print("  test_acid_trap OK")


# ── Test 6: courtyard_overlap ─────────────────────────────────────────────────

def test_courtyard_overlap():
    """Two overlapping courtyard boxes fire courtyard_overlap; separating clears it."""
    b = _board()
    # Footprint fp1 at (0,0) with a 4x4mm courtyard (corners -2,-2 to 2,2)
    # Footprint fp2 at (3,0) with a 4x4mm courtyard (corners 1,-2 to 5,2)
    # They share x from 1 to 2 -> overlap
    cy_a = [
        _crtyd_line("g1", (-2.0, -2.0), (2.0, -2.0)),
        _crtyd_line("g2", (2.0, -2.0), (2.0, 2.0)),
        _crtyd_line("g3", (2.0, 2.0), (-2.0, 2.0)),
        _crtyd_line("g4", (-2.0, 2.0), (-2.0, -2.0)),
    ]
    cy_b = [
        _crtyd_line("g5", (-2.0, -2.0), (2.0, -2.0)),
        _crtyd_line("g6", (2.0, -2.0), (2.0, 2.0)),
        _crtyd_line("g7", (2.0, 2.0), (-2.0, 2.0)),
        _crtyd_line("g8", (-2.0, 2.0), (-2.0, -2.0)),
    ]
    fp_a = _fp("fp1", (0.0, 0.0), graphics=cy_a)
    fp_b = _fp("fp2", (3.0, 0.0), graphics=cy_b)
    b["footprints"] = [fp_a, fp_b]

    viols = run_dfm(b)
    fired = _by_rule(viols, "courtyard_overlap")
    assert len(fired) >= 1, f"expected courtyard_overlap, got {_rules(viols)}"
    items = fired[0]["items"]
    assert "fp1" in items and "fp2" in items, items
    assert fired[0]["severity"] == "error", "courtyard_overlap must be 'error'"

    # Separate fp2 to x=10 -> no overlap
    fp_b["pos"] = [10.0, 0.0]
    viols2 = run_dfm(b)
    assert _by_rule(viols2, "courtyard_overlap") == [], \
        f"separated footprints must not fire courtyard_overlap: {_rules(viols2)}"
    print("  test_courtyard_overlap OK")


# ── Test 7: drill_min ─────────────────────────────────────────────────────────

def test_drill_min():
    """A via drill below 0.2mm fires drill_min; increasing it clears the violation."""
    b = _board()
    _add_net(b, "NA")
    # Via with drill 0.1mm < 0.2mm minimum
    b["vias"] = [_via("v1", (5.0, 5.0), "NA", drill=0.1, diameter=0.5)]

    viols = run_dfm(b)
    fired = _by_rule(viols, "drill_min")
    assert len(fired) >= 1, f"expected drill_min, got {_rules(viols)}"
    assert fired[0]["items"] == ["v1"]

    # Fix the drill to exactly the minimum (0.2mm)
    b["vias"][0]["drill"] = 0.2
    viols2 = run_dfm(b)
    assert _by_rule(viols2, "drill_min") == [], \
        f"via at minimum drill must not fire drill_min: {_rules(viols2)}"

    # Sub-micro drill (< 0.1mm error threshold) fires as "error"
    b["vias"][0]["drill"] = 0.05
    viols3 = run_dfm(b)
    fired3 = _by_rule(viols3, "drill_min")
    assert len(fired3) >= 1, "sub-micro drill must fire drill_min"
    assert fired3[0]["severity"] == "error", \
        f"sub-micro drill must be error severity, got {fired3[0]['severity']}"
    print("  test_drill_min OK")


# ── Test 8: annular_min ───────────────────────────────────────────────────────

def test_annular_min():
    """Via with annular ring below DFM 0.15mm threshold fires annular_min."""
    b = _board()
    _add_net(b, "NA")
    # Via: diameter 0.5mm, drill 0.3mm -> ring (0.5-0.3)/2 = 0.1mm < 0.15mm DFM min
    b["vias"] = [_via("v1", (5.0, 5.0), "NA", drill=0.3, diameter=0.5)]

    viols = run_dfm(b)
    fired = _by_rule(viols, "annular_min")
    assert len(fired) >= 1, f"expected annular_min, got {_rules(viols)}"
    assert fired[0]["items"] == ["v1"]

    # Fix: diameter 0.8, drill 0.4 -> ring = 0.2mm >= 0.15mm
    b["vias"][0]["diameter"] = 0.8
    b["vias"][0]["drill"] = 0.4
    viols2 = run_dfm(b)
    assert _by_rule(viols2, "annular_min") == [], \
        f"via with healthy ring must not fire annular_min: {_rules(viols2)}"
    print("  test_annular_min OK")


# ── Test 9: non-mutation + determinism ───────────────────────────────────────

def test_non_mutation_and_determinism():
    """run_dfm must not mutate the board and must return byte-stable output."""
    b = _board()
    _add_net(b, "NA")
    _add_net(b, "NB")
    # A board with a few features
    pad_a = _smd_pad("p1", (0.0, 0.0), "NA", size=(1.0, 1.0))
    pad_b = _smd_pad("p2", (0.0, 0.0), "NB", size=(1.0, 1.0))
    b["footprints"] = [
        _fp("fp1", (0.0, 0.0), pads=[pad_a]),
        _fp("fp2", (1.05, 0.0), pads=[pad_b]),
    ]
    b["vias"] = [_via("v1", (10.0, 10.0), "NA", drill=0.1, diameter=0.5)]
    b["graphics"] = [_silk_line("g1", (3.0, 0.0), (7.0, 0.0), width=0.08)]

    snap_before = json.dumps(b, sort_keys=True)
    v1 = run_dfm(b)
    snap_after = json.dumps(b, sort_keys=True)
    assert snap_before == snap_after, "run_dfm mutated the board"

    # Determinism: two runs must return identical JSON
    v2 = run_dfm(b)
    assert json.dumps(v1, sort_keys=True) == json.dumps(v2, sort_keys=True), \
        "run_dfm is not deterministic"

    # Non-board input returns []
    assert run_dfm({"schema": "aicad.sch/1"}) == []
    assert run_dfm({}) == []

    print("  test_non_mutation_and_determinism OK")


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    print("=" * 60)
    print("AI-ECAD DFM TESTS")
    print("=" * 60)
    test_silk_over_pad()
    test_silk_min_width()
    test_silk_min_text()
    test_mask_sliver()
    test_acid_trap()
    test_courtyard_overlap()
    test_drill_min()
    test_annular_min()
    test_non_mutation_and_determinism()
    print("ALL DFM TESTS PASSED")


if __name__ == "__main__":
    main()
