"""Tests for the multi-body ``boolean`` and curve ``helix`` feature compilers.

Construction style mirrors test_features1.py: the ``_doc`` / ``_feat`` / ``_dim`` helpers feed
``compile_document``; ``_status`` reads a feature's status record.

Coverage
--------
boolean:
  * combine two overlapping boxes → one body, volume == union (less the overlap).
  * subtract → target minus tool; intersect → common volume.
  * keep_tools true vs false → body-count difference.
  * dangling body selector → contained FeatureError (compile does not crash).
helix:
  * builds a wire (pitch 5, height 50, ~10 turns); turns=null path; taper; ccw both ways.
  * a sweep of a small circle along the helix feature produces a spring solid (sweep consumes
    the helix as its path).
"""

from __future__ import annotations

import math

from build123d import Wire

from engine.doc.compiler import compile_document
from engine.doc.schema import validate_part


# --------------------------------------------------------------------------- helpers


def _dim(value: float, expr: str | None = None) -> dict:
    return {"value": value, "expr": expr}


def _feat(fid: str, ftype: str, params: dict, refs: dict | None = None,
          name: str | None = None, suppressed: bool = False) -> dict:
    return {"id": fid, "type": ftype, "name": name or fid,
            "suppressed": suppressed, "params": params, "refs": refs or {}}


def _doc(features: list[dict], globals_list: list[dict] | None = None) -> dict:
    return {
        "schema": "aicad.part/1",
        "id": "test-doc",
        "name": "test",
        "units": {"system": "MMGS"},
        "material": None,
        "equations": {"globals": globals_list or []},
        "features": features,
    }


def _assert_valid(doc: dict) -> None:
    errs = validate_part(doc)
    assert errs == [], "doc must validate before compile:\n" + "\n".join(errs)


def _status(res, fid: str) -> dict:
    for s in res.feature_status:
        if s["id"] == fid:
            return s
    raise AssertionError(f"no status for {fid}")


def _body_sel(owner: str) -> dict:
    return {"kind": "body", "owner_feature": owner}


# --------------------------------------------------------------------------- boolean tests


def _two_boxes_doc(extra: list[dict]) -> dict:
    """f1 box centred at origin; f3 box centred at (10,0,0) as a SEPARATE body; + extra feats.

    Primitives place via an 'at' datum (no authorable point feature without geometry selectors),
    so f2 = plane.right (YZ, normal +X) offset +10 gives an origin at (10,0,0); the box 'at'
    uses that plane's origin. f1 spans x∈[-10,10], f3 spans x∈[0,20] → 10×20×20 = 4000 mm³ overlap;
    union = 12000 mm³, intersect = 4000 mm³. The boolean feature is added as f4 by each test.
    """
    feats = [
        _feat("f1", "box", {"length": _dim(20), "width": _dim(20), "height": _dim(20)}),
        _feat("f2", "plane", {"method": "offset", "offset": _dim(10.0)},
              refs={"base": "plane.right"}),
        _feat("f3", "box", {"length": _dim(20), "width": _dim(20), "height": _dim(20),
                            "merge": False}, refs={"at": "f2"}),
    ]
    return _doc(feats + extra)


def test_boolean_combine_union_volume() -> None:
    """Combine two overlapping boxes → one body whose volume equals the union (less overlap)."""
    doc = _two_boxes_doc([
        _feat("f4", "boolean", {"op": "combine"},
              refs={"targets": [_body_sel("f1")], "tools": [_body_sel("f3")]}),
    ])
    _assert_valid(doc)
    res = compile_document(doc)
    assert _status(res, "f1")["ok"], _status(res, "f1")["error"]
    assert _status(res, "f3")["ok"], _status(res, "f3")["error"]
    s = _status(res, "f4")
    assert s["ok"], f"boolean combine failed: {s['error']}"
    assert len(res.bodies) == 1, f"combine should leave 1 body; got {len(res.bodies)}"
    vol = res.bodies[0].shape.volume
    expected = 8000.0 + 8000.0 - 4000.0  # union less the 10×20×20 overlap
    assert abs(vol - expected) / expected < 0.01, f"combine vol {vol} vs union {expected}"
    print(f"[bool-combine] union vol={vol:.1f} (expected {expected})")


def test_boolean_subtract_volume() -> None:
    """Subtract tool from target → target minus the overlap."""
    doc = _two_boxes_doc([
        _feat("f4", "boolean", {"op": "subtract"},
              refs={"targets": [_body_sel("f1")], "tools": [_body_sel("f3")]}),
    ])
    _assert_valid(doc)
    res = compile_document(doc)
    s = _status(res, "f4")
    assert s["ok"], f"boolean subtract failed: {s['error']}"
    assert len(res.bodies) == 1, f"subtract (keep_tools false) should leave 1 body; got {len(res.bodies)}"
    vol = res.bodies[0].shape.volume
    expected = 8000.0 - 4000.0  # f1 minus the overlap
    assert abs(vol - expected) / expected < 0.01, f"subtract vol {vol} vs {expected}"
    print(f"[bool-subtract] vol={vol:.1f} (expected {expected})")


def test_boolean_intersect_volume() -> None:
    """Intersect target and tool → the common (overlap) volume only."""
    doc = _two_boxes_doc([
        _feat("f4", "boolean", {"op": "intersect"},
              refs={"targets": [_body_sel("f1")], "tools": [_body_sel("f3")]}),
    ])
    _assert_valid(doc)
    res = compile_document(doc)
    s = _status(res, "f4")
    assert s["ok"], f"boolean intersect failed: {s['error']}"
    assert len(res.bodies) == 1, f"intersect should leave 1 body; got {len(res.bodies)}"
    vol = res.bodies[0].shape.volume
    expected = 4000.0  # the 10×20×20 common volume
    assert abs(vol - expected) / expected < 0.01, f"intersect vol {vol} vs {expected}"
    print(f"[bool-intersect] vol={vol:.1f} (expected {expected})")


def test_boolean_keep_tools_body_count() -> None:
    """keep_tools controls whether the subtracted tool body survives in the rolling list."""
    # keep_tools=False → 1 body (default; tested above). keep_tools=True on subtract → 2 bodies.
    doc_keep = _two_boxes_doc([
        _feat("f4", "boolean", {"op": "subtract", "keep_tools": True},
              refs={"targets": [_body_sel("f1")], "tools": [_body_sel("f3")]}),
    ])
    _assert_valid(doc_keep)
    res_keep = compile_document(doc_keep)
    s = _status(res_keep, "f4")
    assert s["ok"], f"boolean subtract keep_tools failed: {s['error']}"
    assert len(res_keep.bodies) == 2, (
        f"subtract keep_tools=True should leave target-result + tool = 2 bodies; "
        f"got {len(res_keep.bodies)}")

    doc_drop = _two_boxes_doc([
        _feat("f4", "boolean", {"op": "subtract", "keep_tools": False},
              refs={"targets": [_body_sel("f1")], "tools": [_body_sel("f3")]}),
    ])
    _assert_valid(doc_drop)
    res_drop = compile_document(doc_drop)
    assert len(res_drop.bodies) == 1, (
        f"subtract keep_tools=False should leave 1 body; got {len(res_drop.bodies)}")
    print(f"[bool-keep] keep_tools True -> {len(res_keep.bodies)} bodies, "
          f"False -> {len(res_drop.bodies)} body")


def test_boolean_dangling_selector_contained() -> None:
    """A body selector naming a non-existent feature → contained FeatureError, no crash."""
    doc = _two_boxes_doc([
        _feat("f4", "boolean", {"op": "subtract"},
              refs={"targets": [_body_sel("f1")], "tools": [_body_sel("f999")]}),
    ])
    _assert_valid(doc)
    res = compile_document(doc)  # must not raise
    s = _status(res, "f4")
    assert not s["ok"], "boolean with a dangling tool selector should fail"
    assert s["dangling_refs"], f"dangling_refs should be populated; got {s['dangling_refs']}"
    # Compile continued: the two source bodies remain (last good state).
    assert len(res.bodies) == 2, f"failed boolean should leave the source bodies; got {len(res.bodies)}"
    print(f"[bool-dangling] error='{s['error'][:50]}' dangling={s['dangling_refs']}")


# --------------------------------------------------------------------------- helix tests


def _helix_feat(fid: str, *, pitch: float = 5.0, height: float = 50.0,
                turns: float | None = None, taper: float = 0.0,
                ccw: bool = True, radius: float = 8.0,
                base: str = "plane.top") -> dict:
    params: dict = {
        "pitch": _dim(pitch), "height": _dim(height),
        "taper": _dim(taper), "start_angle": _dim(0.0),
        "ccw": ccw, "radius": _dim(radius),
    }
    if turns is not None:
        params["turns"] = _dim(turns)
    return _feat(fid, "helix", params, refs={"base": base})


def test_helix_builds_wire() -> None:
    """A pitch-5 height-50 helix → a wire with ~10 turns and the expected curve length."""
    doc = _doc([_helix_feat("f1", pitch=5.0, height=50.0, radius=8.0)])
    _assert_valid(doc)
    res = compile_document(doc)
    s = _status(res, "f1")
    assert s["ok"], f"helix failed: {s['error']}"
    # helix is a CURVE feature: it must NOT add a solid body.
    assert len(res.bodies) == 0, f"helix must not add a solid body; got {len(res.bodies)}"
    assert abs(s["resolved"]["turns"] - 10.0) < 1e-6, f"turns should be 10; got {s['resolved']}"
    # The stored curve is a wire whose length ≈ turns·√((2πr)² + pitch²).
    wire = res.tool_bodies["f1"]
    assert isinstance(wire, Wire), f"helix tool_body should be a Wire; got {type(wire)}"
    per_turn = math.sqrt((2 * math.pi * 8.0) ** 2 + 5.0 ** 2)
    expected_len = 10.0 * per_turn
    assert abs(wire.length - expected_len) / expected_len < 0.02, \
        f"helix length {wire.length:.2f} vs expected {expected_len:.2f}"
    print(f"[helix] pitch5 h50 r8 -> turns={s['resolved']['turns']:.1f}, len={wire.length:.1f}")


def test_helix_turns_drives_height() -> None:
    """turns given → height = turns·pitch (turns=null path is the default-height case)."""
    doc = _doc([_helix_feat("f1", pitch=5.0, height=999.0, turns=4.0, radius=8.0)])
    _assert_valid(doc)
    res = compile_document(doc)
    s = _status(res, "f1")
    assert s["ok"], f"helix turns failed: {s['error']}"
    assert abs(s["resolved"]["height"] - 20.0) < 1e-6, \
        f"turns=4·pitch5 should set height 20; got {s['resolved']['height']}"
    assert abs(s["resolved"]["turns"] - 4.0) < 1e-6, f"turns should be 4; got {s['resolved']}"
    print(f"[helix-turns] turns=4 -> height={s['resolved']['height']:.1f}")


def test_helix_taper_changes_length() -> None:
    """A tapered (conical) helix has a different curve length than a straight one."""
    res_straight = compile_document(_doc([_helix_feat("f1", taper=0.0, radius=8.0)]))
    res_taper = compile_document(_doc([_helix_feat("f1", taper=15.0, radius=8.0)]))
    assert _status(res_straight, "f1")["ok"] and _status(res_taper, "f1")["ok"]
    l0 = res_straight.tool_bodies["f1"].length
    lt = res_taper.tool_bodies["f1"].length
    assert abs(l0 - lt) > 1.0, f"taper should change helix length; straight={l0:.2f} taper={lt:.2f}"
    print(f"[helix-taper] straight len={l0:.1f}  taper15 len={lt:.1f}")


def test_helix_ccw_both_ways_build() -> None:
    """Both right-handed (ccw=True) and left-handed (ccw=False) helices build."""
    for ccw in (True, False):
        res = compile_document(_doc([_helix_feat("f1", ccw=ccw, radius=8.0)]))
        s = _status(res, "f1")
        assert s["ok"], f"helix ccw={ccw} failed: {s['error']}"
        assert res.tool_bodies["f1"].length > 0
    print("[helix-ccw] ccw True and False both build")


def test_sweep_along_helix_makes_spring() -> None:
    """A sweep of a small circle profile along the helix feature produces a spring solid.

    This exercises the contract that a helix is consumable as a sweep ``path`` via
    ``{"kind":"region","sketch":<helix_fid>}``.
    """
    radius = 8.0
    # Profile: a small circle on plane.front (XZ, normal -Y ≈ helix tangent at the start),
    # centred at local (u,v)=(radius, 0) → world (radius, 0, 0) = the helix start point.
    prof_r = 1.2
    profile_sketch = _feat("f2", "sketch", {
        "entities": [
            {"id": "e1", "type": "circle", "construction": False,
             "center": [radius, 0.0], "radius": prof_r},
        ],
        "constraints": [],
    }, refs={"plane": "plane.front"})

    doc = _doc([
        _helix_feat("f1", pitch=5.0, height=50.0, radius=radius, base="plane.top"),
        profile_sketch,
        _feat("f3", "sweep", {"mode": "boss", "merge": True},
              refs={
                  "profile": [{"kind": "region", "sketch": "f2",
                               "centroid": [radius, 0.0],
                               "area_or_len": math.pi * prof_r ** 2, "hint_index": 0}],
                  "path": {"kind": "region", "sketch": "f1"},  # the helix feature as the path
              }),
    ])
    _assert_valid(doc)
    res = compile_document(doc)
    assert _status(res, "f1")["ok"], _status(res, "f1")["error"]
    assert _status(res, "f2")["ok"], _status(res, "f2")["error"]
    s = _status(res, "f3")
    assert s["ok"], f"sweep along helix failed: {s['error']}"
    assert len(res.bodies) == 1, f"spring should be one body; got {len(res.bodies)}"
    vol = res.bodies[0].shape.volume
    # Rough sanity: a tube of cross-section π·prof_r² swept along the helix length.
    per_turn = math.sqrt((2 * math.pi * radius) ** 2 + 5.0 ** 2)
    approx = (math.pi * prof_r ** 2) * (10.0 * per_turn)
    assert vol > 0.0, "spring should have positive volume"
    assert abs(vol - approx) / approx < 0.25, f"spring vol {vol:.1f} vs ~{approx:.1f}"
    print(f"[sweep-helix] spring vol={vol:.1f} (approx {approx:.1f}) - sweep consumed the helix path")


# --------------------------------------------------------------------------- main


def main() -> None:
    test_boolean_combine_union_volume()
    test_boolean_subtract_volume()
    test_boolean_intersect_volume()
    test_boolean_keep_tools_body_count()
    test_boolean_dangling_selector_contained()
    test_helix_builds_wire()
    test_helix_turns_drives_height()
    test_helix_taper_changes_length()
    test_helix_ccw_both_ways_build()
    test_sweep_along_helix_makes_spring()
    print("\nBOOLEAN+HELIX TESTS PASSED")


if __name__ == "__main__":
    main()
