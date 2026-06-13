"""C51 — sketched-feature overhaul: even-odd region holes, whole-sketch / indexed profiles,
sweep, loft, and the sketch_regions RPC.

Run:  & $py -m pytest engine/doc/test_feature_overhaul.py   (from repo root)

Guards the user-reported bugs:
  * "! dangling reference" when extruding a sketch with concentric loops (square + circle) —
    the whole-sketch profile must resolve cleanly (no fragile centroid match).
  * a circle inside a square must extrude as a WASHER, not a filled square (even-odd holes).
  * sweep / loft must actually build (they were unimplemented ribbon stubs).
"""

from __future__ import annotations

import math

import engine.doc.test_features1 as T
from engine.doc.compiler import compile_document
from engine.doc.schema import validate_document


# --------------------------------------------------------------------------- helpers


def _square_circle_sketch(fid: str = "f1") -> dict:
    """80 x 50 square with a concentric r10 circle on plane.top."""
    return T._feat(fid, "sketch", {"entities": [
        {"id": "e1", "type": "line", "construction": False, "p1": [0.0, 0.0], "p2": [80.0, 0.0]},
        {"id": "e2", "type": "line", "construction": False, "p1": [80.0, 0.0], "p2": [80.0, 50.0]},
        {"id": "e3", "type": "line", "construction": False, "p1": [80.0, 50.0], "p2": [0.0, 50.0]},
        {"id": "e4", "type": "line", "construction": False, "p1": [0.0, 50.0], "p2": [0.0, 0.0]},
        {"id": "e5", "type": "circle", "construction": False, "center": [40.0, 25.0], "radius": 10.0},
    ], "constraints": []}, refs={"plane": "plane.top"})


def _circle_sketch(fid: str, plane, cx: float, cy: float, r: float) -> dict:
    return T._feat(fid, "sketch", {"entities": [
        {"id": "e1", "type": "circle", "construction": False, "center": [cx, cy], "radius": r},
    ], "constraints": []}, refs={"plane": plane})


def _square_sketch(fid: str, plane, side: float) -> dict:
    h = side / 2.0
    return T._feat(fid, "sketch", {"entities": [
        {"id": "e1", "type": "line", "construction": False, "p1": [-h, -h], "p2": [h, -h]},
        {"id": "e2", "type": "line", "construction": False, "p1": [h, -h], "p2": [h, h]},
        {"id": "e3", "type": "line", "construction": False, "p1": [h, h], "p2": [-h, h]},
        {"id": "e4", "type": "line", "construction": False, "p1": [-h, h], "p2": [-h, -h]},
    ], "constraints": []}, refs={"plane": plane})


def _extrude(fid: str, profile: list, depth: float = 10.0, mode: str = "boss") -> dict:
    return T._feat(fid, "extrude", {"mode": mode, "end": "blind",
                                    "depth": {"value": depth, "expr": None}},
                   refs={"profile": profile})


# --------------------------------------------------------------------------- region / hole model


def test_whole_sketch_extrude_is_washer_not_dangling() -> None:
    """The reported bug: extruding a square+circle sketch as a whole must NOT dangle, and must
    produce a washer (square minus circle), not a filled square."""
    doc = T._doc([_square_circle_sketch("f1"),
                  _extrude("f2", [{"kind": "region", "sketch": "f1"}])])
    assert validate_document(doc) == [], "whole-sketch region selector must validate"
    res = compile_document(doc)
    s = T._status(res, "f2")
    assert s["ok"], f"whole-sketch extrude failed (dangling regression?): {s.get('error')}"
    vol = res.bodies[-1].shape.volume
    expected = (80.0 * 50.0 - math.pi * 100.0) * 10.0
    assert abs(vol - expected) < 1.0, f"washer volume {vol:.1f} != {expected:.1f}"


def test_washer_independent_of_loop_winding() -> None:
    """A clockwise-wound outer square must still subtract the circle (winding-robust holes).

    Regression for the live bug: a CW square made the hole reversal a no-op → the circle was
    ADDED as material (filled square + disc, vol 43141) instead of a washer."""
    for ccw in (True, False):
        pts = ([[0, 0], [80, 0], [80, 50], [0, 50]] if ccw
               else [[0, 0], [0, 50], [80, 50], [80, 0]])
        ents = [{"id": f"e{i+1}", "type": "line", "construction": False,
                 "p1": pts[i], "p2": pts[(i + 1) % 4]} for i in range(4)]
        ents.append({"id": "e5", "type": "circle", "construction": False,
                     "center": [40.0, 25.0], "radius": 10.0})
        sk = T._feat("f1", "sketch", {"entities": ents, "constraints": []},
                     refs={"plane": "plane.top"})
        res = compile_document(T._doc([sk, _extrude("f2", [{"kind": "region", "sketch": "f1"}])]))
        vol = res.bodies[-1].shape.volume
        expected = (80.0 * 50.0 - math.pi * 100.0) * 10.0
        assert abs(vol - expected) < 1.0, f"winding ccw={ccw}: washer {vol:.1f} != {expected:.1f}"


def test_region_count_square_circle() -> None:
    """square + concentric circle -> 2 selectable regions (washer + disc)."""
    res = compile_document(T._doc([_square_circle_sketch("f1")]))
    assert T._status(res, "f1")["resolved"].get("_region_count") == 2.0


def test_indexed_region_selection() -> None:
    """index 0 = material washer region; index 1 = the inner disc."""
    sk = _square_circle_sketch("f1")
    r0 = compile_document(T._doc([sk, _extrude("f2", [{"kind": "region", "sketch": "f1", "index": 0}])]))
    r1 = compile_document(T._doc([sk, _extrude("f2", [{"kind": "region", "sketch": "f1", "index": 1}])]))
    v0 = r0.bodies[-1].shape.volume
    v1 = r1.bodies[-1].shape.volume
    assert abs(v0 - (80 * 50 - math.pi * 100) * 10) < 1.0, f"region0 should be washer, got {v0:.1f}"
    assert abs(v1 - math.pi * 100 * 10) < 1.0, f"region1 should be disc, got {v1:.1f}"


def test_region_index_out_of_range_is_clean_error() -> None:
    sk = _square_circle_sketch("f1")
    res = compile_document(T._doc([sk, _extrude("f2", [{"kind": "region", "sketch": "f1", "index": 9}])]))
    s = T._status(res, "f2")
    detail = (s.get("error") or "") + " ".join(s.get("dangling_refs") or [])
    assert not s["ok"] and "out of range" in detail, f"expected out-of-range; got {detail!r}"


# --------------------------------------------------------------------------- sweep


def test_sweep_circle_along_line_is_cylinder() -> None:
    prof = _circle_sketch("f1", "plane.front", 0.0, 0.0, 5.0)
    path = T._feat("f2", "sketch", {"entities": [
        {"id": "e1", "type": "line", "construction": False, "p1": [0.0, 0.0], "p2": [0.0, 50.0]},
    ], "constraints": []}, refs={"plane": "plane.top"})
    sw = T._feat("f3", "sweep", {"mode": "boss", "merge": True},
                 refs={"profile": [{"kind": "region", "sketch": "f1"}],
                       "path": {"kind": "region", "sketch": "f2"}})
    doc = T._doc([prof, path, sw])
    assert validate_document(doc) == [], "sweep doc must validate"
    res = compile_document(doc)
    s = T._status(res, "f3")
    assert s["ok"], f"sweep failed: {s.get('error')}"
    vol = res.bodies[-1].shape.volume
    assert abs(vol - math.pi * 25.0 * 50.0) < 5.0, f"swept cylinder volume {vol:.1f}"


# --------------------------------------------------------------------------- loft


def test_loft_square_to_square_is_prismatoid() -> None:
    pl2 = T._feat("f1", "plane", {"method": "offset", "offset": {"value": 40.0, "expr": None}},
                  refs={"base": "plane.top"})
    s1 = _square_sketch("f2", "plane.top", 40.0)
    s2 = _square_sketch("f3", {"kind": "datum", "feature": "f1"}, 20.0)
    lo = T._feat("f4", "loft", {"mode": "boss", "ruled": False, "merge": True},
                 refs={"sections": [{"kind": "region", "sketch": "f2"},
                                    {"kind": "region", "sketch": "f3"}]})
    doc = T._doc([pl2, s1, s2, lo])
    assert validate_document(doc) == [], "loft doc must validate"
    res = compile_document(doc)
    s = T._status(res, "f4")
    assert s["ok"], f"loft failed: {s.get('error')}"
    vol = res.bodies[-1].shape.volume
    # Prismatoid: h/6 (A_bot + 4 A_mid + A_top) = 40/6 (1600 + 4*900 + 400)
    expected = 40.0 / 6.0 * (1600.0 + 4 * 900.0 + 400.0)
    assert abs(vol - expected) < 5.0, f"loft volume {vol:.1f} != {expected:.1f}"


def test_loft_needs_two_sections() -> None:
    s1 = _square_sketch("f1", "plane.top", 40.0)
    lo = T._feat("f2", "loft", {"mode": "boss"},
                 refs={"sections": [{"kind": "region", "sketch": "f1"}]})
    res = compile_document(T._doc([s1, lo]))
    s = T._status(res, "f2")
    assert not s["ok"] and "at least 2" in (s.get("error") or "")


if __name__ == "__main__":
    test_whole_sketch_extrude_is_washer_not_dangling()
    test_region_count_square_circle()
    test_indexed_region_selection()
    test_region_index_out_of_range_is_clean_error()
    test_sweep_circle_along_line_is_cylinder()
    test_loft_square_to_square_is_prismatoid()
    test_loft_needs_two_sections()
    print("[overhaul] all feature-overhaul tests passed")
