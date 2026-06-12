r"""C29-A — sketch trim / extend / offset op tests (pytest-collectable; also a house-style runner).

Run:  & $py -m pytest engine\doc\test_sketch_tools.py -q     (from repo root)
  or: & $py -m engine.doc.test_sketch_tools

Exercises the three sketch geometry tool ops wired in ops.py against the pure-math engine in
sketch_geom.py, through the same validate-then-apply path the GUI/AI use (apply_ops):

  * trim the middle of a double-crossed line (3 spans -> middle dropped, 2 survive, id mapping)
  * trim with no intersections deletes the whole entity (power-trim)
  * extend a line to a crossing line (endpoint lands exactly on it)
  * extend with nothing ahead -> extend_no_target error
  * offset an L-chain both sides (distance + joint verified)
  * offset a closed rectangle inward (smaller closed loop, area check)
  * unsupported entity involved -> unsupported_entity error
  * constraints referencing trimmed geometry are dropped
"""

from __future__ import annotations

import math

from engine.doc.ops import OpError, apply_ops
from engine.doc.schema import validate_document


def _blank() -> dict:
    return {
        "schema": "aicad.part/1",
        "id": "doc-test",
        "name": "test",
        "units": {"system": "MMGS"},
        "material": None,
        "equations": {"globals": []},
        "features": [],
    }


def _sketch_with(entities, constraints=None) -> dict:
    """A document with one sketch on plane.top carrying the given entities/constraints."""
    doc = _blank()
    doc, _ = apply_ops(doc, [{"op": "add_feature", "feature": {
        "type": "sketch", "name": "Sketch1", "refs": {"plane": "plane.top"},
        "params": {"entities": entities, "constraints": constraints or []}}}])
    return doc


def _ents(doc):
    return doc["features"][0]["params"]["entities"]


def _cons(doc):
    return doc["features"][0]["params"]["constraints"]


def _by_id(doc):
    return {e["id"]: e for e in _ents(doc)}


def _line(eid, p1, p2, construction=False):
    return {"id": eid, "type": "line", "construction": construction, "p1": p1, "p2": p2}


# --------------------------------------------------------------------------- trim


def test_trim_middle_of_double_crossed_line() -> None:
    """A horizontal line crossed by two verticals = 3 spans; trimming the middle drops it and the
    two end spans survive (one keeps e1, the other gets a fresh id; id_map records the mapping)."""
    doc = _sketch_with([
        _line("e1", [0.0, 0.0], [100.0, 0.0]),     # target
        _line("e2", [30.0, -10.0], [30.0, 10.0]),  # cuts at x=30
        _line("e3", [70.0, -10.0], [70.0, 10.0]),  # cuts at x=70
    ])
    new_doc, id_map = apply_ops(doc, [
        {"op": "sketch_trim", "sketch": "f1", "entity": "e1", "point": [50.0, 0.0]}])
    assert validate_document(new_doc) == [], validate_document(new_doc)

    by = _by_id(new_doc)
    # e1 survives (the first span 0..30), and exactly one new line span survives (70..100).
    assert "e1" in by, "trim must keep the original id on a surviving span"
    line_ids = [e["id"] for e in _ents(new_doc) if e["type"] == "line"]
    # original 3 lines, e1 split into 2 (one keeps e1, one new) => total 4 lines now.
    assert len(line_ids) == 4, line_ids
    survivors = [e for e in _ents(new_doc) if e["id"] in ("e1",) or e["id"] == id_map.get("$0#1")]
    xs = sorted(round(c, 3) for e in survivors for c in (e["p1"][0], e["p2"][0]))
    assert xs == [0.0, 30.0, 70.0, 100.0], f"middle span (30..70) must be gone, got {xs}"
    assert id_map["$0"] == "e1", id_map
    assert id_map["$0#1"] == "e4", f"second survivor gets a fresh id, got {id_map}"
    print(f"[trim] double-cross: survivors e1(0..30)+{id_map['$0#1']}(70..100); middle dropped; "
          f"id_map={ {k: v for k, v in id_map.items() if k.startswith('$0')} }")


def test_trim_no_intersections_deletes_whole() -> None:
    """A line with no crossings is fully removed by trim (SolidWorks power-trim)."""
    doc = _sketch_with([
        _line("e1", [0.0, 0.0], [100.0, 0.0]),
        _line("e2", [0.0, 50.0], [100.0, 50.0]),   # parallel, never crosses e1
    ])
    new_doc, id_map = apply_ops(doc, [
        {"op": "sketch_trim", "sketch": "f1", "entity": "e1", "point": [50.0, 0.0]}])
    assert validate_document(new_doc) == [], validate_document(new_doc)
    ids = [e["id"] for e in _ents(new_doc)]
    assert ids == ["e2"], f"e1 should be entirely deleted, remaining: {ids}"
    assert id_map["$0"] == "", f"no surviving span -> empty primary id, got {id_map}"
    print("[trim] no-intersection power-trim: whole entity e1 deleted")


def test_trim_drops_dangling_constraints() -> None:
    """Constraints whose handles reference the trimmed entity are dropped (mirrors delete_entity)."""
    doc = _sketch_with(
        [
            _line("e1", [0.0, 0.0], [100.0, 0.0]),
            _line("e2", [50.0, -10.0], [50.0, 10.0]),
            _line("e3", [0.0, 20.0], [100.0, 20.0]),
        ],
        constraints=[
            {"id": "c1", "type": "horizontal", "refs": ["e1"]},        # touches e1 -> drop
            {"id": "c2", "type": "parallel", "refs": ["e1", "e3"]},    # touches e1 -> drop
            {"id": "c3", "type": "horizontal", "refs": ["e3"]},        # untouched -> keep
        ],
    )
    new_doc, _ = apply_ops(doc, [
        {"op": "sketch_trim", "sketch": "f1", "entity": "e1", "point": [25.0, 0.0]}])
    assert validate_document(new_doc) == [], validate_document(new_doc)
    cids = [c["id"] for c in _cons(new_doc)]
    assert cids == ["c3"], f"constraints touching trimmed e1 must be dropped, kept: {cids}"
    print(f"[trim] dangling constraints on trimmed e1 dropped (c1,c2); kept {cids}")


# --------------------------------------------------------------------------- extend


def test_extend_line_to_crossing_line() -> None:
    """Extending a line's end lands its endpoint exactly on the first crossing line."""
    doc = _sketch_with([
        _line("e1", [0.0, 0.0], [40.0, 0.0]),       # extend this end (p2)
        _line("e2", [80.0, -10.0], [80.0, 10.0]),   # vertical wall at x=80
    ])
    new_doc, id_map = apply_ops(doc, [
        {"op": "sketch_extend", "sketch": "f1", "entity": "e1", "end": "end"}])
    assert validate_document(new_doc) == [], validate_document(new_doc)
    e1 = _by_id(new_doc)["e1"]
    assert abs(e1["p2"][0] - 80.0) < 1e-9 and abs(e1["p2"][1] - 0.0) < 1e-9, e1
    assert e1["p1"] == [0.0, 0.0], "the anchored end must not move"
    assert id_map["$0"] == "e1", id_map
    print(f"[extend] line e1 end extended to ({e1['p2'][0]},{e1['p2'][1]}) on the crossing wall")


def test_extend_no_target_error() -> None:
    """Extending toward empty space raises extend_no_target with no document change."""
    doc = _sketch_with([
        _line("e1", [0.0, 0.0], [40.0, 0.0]),
        _line("e2", [80.0, -10.0], [80.0, 10.0]),   # only ahead of the END, not the START
    ])
    try:
        apply_ops(doc, [{"op": "sketch_extend", "sketch": "f1", "entity": "e1", "end": "start"}])
        raise AssertionError("extending toward nothing must raise extend_no_target")
    except OpError as e:
        assert e.code == "extend_no_target", e.to_envelope()
    print("[extend] extend_no_target raised when nothing lies ahead of the chosen end")


# --------------------------------------------------------------------------- offset


def test_offset_L_chain_both_sides() -> None:
    """Offsetting an L (two lines) by 10 picks a consistent side from the hint; the two offset
    segments join at the offset corner. Inside hint -> corner at (90,10); outside -> (110,-10)."""
    base = [
        _line("e1", [0.0, 0.0], [100.0, 0.0]),
        _line("e2", [100.0, 0.0], [100.0, 100.0]),
    ]
    # Inside the L corner.
    doc_in = _sketch_with([dict(e) for e in base])
    new_in, id_in = apply_ops(doc_in, [{"op": "sketch_offset", "sketch": "f1",
                                        "entities": ["e1", "e2"], "distance": 10.0,
                                        "side": [50.0, 50.0]}])
    assert validate_document(new_in) == [], validate_document(new_in)
    new_ids = [id_in["$0"], id_in["$0#1"]]
    a, b = (_by_id(new_in)[i] for i in new_ids)
    assert abs(a["p1"][1] - 10.0) < 1e-9, a              # first segment offset up to y=10
    corner = a["p2"]
    assert abs(corner[0] - 90.0) < 1e-9 and abs(corner[1] - 10.0) < 1e-9, corner
    assert b["p1"] == corner, "the two offset segments share the offset corner"
    # A coincident constraint joins the new corner (and only that).
    cids = [c for c in _cons(new_in) if c["type"] == "coincident"]
    assert len(cids) == 1 and set(cids[0]["refs"]) == {f"{new_ids[0]}.p2", f"{new_ids[1]}.p1"}, cids

    # Outside the L corner.
    doc_out = _sketch_with([dict(e) for e in base])
    new_out, id_out = apply_ops(doc_out, [{"op": "sketch_offset", "sketch": "f1",
                                           "entities": ["e1", "e2"], "distance": 10.0,
                                           "side": [-50.0, -50.0]}])
    assert validate_document(new_out) == [], validate_document(new_out)
    a2 = _by_id(new_out)[id_out["$0"]]
    assert abs(a2["p1"][1] + 10.0) < 1e-9, a2            # offset down to y=-10
    corner2 = a2["p2"]
    assert abs(corner2[0] - 110.0) < 1e-9 and abs(corner2[1] + 10.0) < 1e-9, corner2
    print(f"[offset] L inside corner={corner}, outside corner={corner2}; one coincident joint each")


def test_offset_closed_rectangle_inward() -> None:
    """Offsetting a closed rectangle inward yields a smaller closed loop (area 80x40 from 100x60)."""
    rect = [
        _line("e1", [0.0, 0.0], [100.0, 0.0]),
        _line("e2", [100.0, 0.0], [100.0, 60.0]),
        _line("e3", [100.0, 60.0], [0.0, 60.0]),
        _line("e4", [0.0, 60.0], [0.0, 0.0]),
    ]
    doc = _sketch_with(rect)
    new_doc, id_map = apply_ops(doc, [{"op": "sketch_offset", "sketch": "f1",
                                       "entities": ["e1", "e2", "e3", "e4"], "distance": 10.0,
                                       "side": [50.0, 30.0]}])
    assert validate_document(new_doc) == [], validate_document(new_doc)
    new_ids = [id_map["$0"]] + [id_map[f"$0#{k}"] for k in (1, 2, 3)]
    segs = [_by_id(new_doc)[i] for i in new_ids]
    pts = [tuple(s["p1"]) for s in segs]
    area = abs(sum(pts[i][0] * pts[(i + 1) % 4][1] - pts[(i + 1) % 4][0] * pts[i][1]
                   for i in range(4))) / 2.0
    assert abs(area - 80.0 * 40.0) < 1e-6, f"inward offset area should be 80x40=3200, got {area}"
    # Closed: 4 coincident joints (including last->first).
    coincs = [c for c in _cons(new_doc) if c["type"] == "coincident"]
    assert len(coincs) == 4, f"closed offset loop needs 4 coincident joints, got {len(coincs)}"
    assert segs[-1]["p2"] == segs[0]["p1"], "the loop must close back on itself"
    print(f"[offset] closed rect inward: area={area} (=3200), {len(coincs)} coincident joints, "
          f"loop closed")


# --------------------------------------------------------------------------- unsupported


def test_unsupported_entity_error() -> None:
    """A tool touching a spline (unsupported by the geometry engine) raises unsupported_entity."""
    doc = _sketch_with([
        {"id": "e1", "type": "spline", "construction": False,
         "points": [[0.0, 0.0], [50.0, 10.0], [100.0, 0.0]]},
        _line("e2", [50.0, -10.0], [50.0, 10.0]),
    ])
    try:
        apply_ops(doc, [{"op": "sketch_trim", "sketch": "f1", "entity": "e1", "point": [25.0, 0.0]}])
        raise AssertionError("trimming a spline must raise unsupported_entity")
    except OpError as e:
        assert e.code == "unsupported_entity", e.to_envelope()
    print("[unsupported] trimming a spline raised unsupported_entity")


# --------------------------------------------------------------------------- construction toggle


def test_toggle_construction_roundtrip() -> None:
    """C42: sketch_toggle_construction flips the flag both ways; unknown id raises invalid_op."""
    doc = _sketch_with([_line("e1", [0.0, 0.0], [100.0, 0.0])])
    on_doc, _ = apply_ops(doc, [
        {"op": "sketch_toggle_construction", "sketch": "f1", "id": "e1"}])
    assert validate_document(on_doc) == [], validate_document(on_doc)
    assert _by_id(on_doc)["e1"]["construction"] is True, _by_id(on_doc)["e1"]
    off_doc, _ = apply_ops(on_doc, [
        {"op": "sketch_toggle_construction", "sketch": "f1", "id": "e1"}])
    assert validate_document(off_doc) == [], validate_document(off_doc)
    assert _by_id(off_doc)["e1"]["construction"] is False, _by_id(off_doc)["e1"]
    try:
        apply_ops(doc, [{"op": "sketch_toggle_construction", "sketch": "f1", "id": "nope"}])
        raise AssertionError("toggling an unknown entity must raise invalid_op")
    except OpError as e:
        assert e.code == "invalid_op", e.to_envelope()
    print("[construction] e1 toggled on -> off; unknown id raised invalid_op")


def main() -> None:
    test_trim_middle_of_double_crossed_line()
    test_trim_no_intersections_deletes_whole()
    test_trim_drops_dangling_constraints()
    test_extend_line_to_crossing_line()
    test_extend_no_target_error()
    test_offset_L_chain_both_sides()
    test_offset_closed_rectangle_inward()
    test_unsupported_entity_error()
    test_toggle_construction_roundtrip()
    print("\nSKETCH TOOLS TESTS PASSED")


if __name__ == "__main__":
    main()
