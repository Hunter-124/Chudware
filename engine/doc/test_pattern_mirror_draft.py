"""Pattern / mirror / draft feature compilers (post-C51 wave).

These features were schema-defined but had NO compiler (ribbon greyed "Coming soon"). They now
replicate upstream seed features with the correct boolean (boss fuses, cut/hole subtracts) or
taper faces. Volumes are checked against closed-form expectations.

Setups mirror engine/doc/test_hole.py: a CENTRED box (top face at z=+H/2) drilled via a top-face
selector with hole positions given in the face frame. Plates are kept thick relative to their
footprint so the top-face selector is unambiguous (a thin square plate ties top vs bottom), and
holes stay well inside and well separated so every instance removes a full cylinder.

Run:  & $py -m pytest engine/doc/test_pattern_mirror_draft.py
"""

from __future__ import annotations

import math

import engine.doc.test_features1 as T
from engine.doc.compiler import compile_document
from engine.doc.schema import validate_document

_D = 8.0  # hole diameter


def _dim(v: float) -> dict:
    return {"value": v, "expr": None}


def _box(fid: str, L: float, W: float, H: float, centered: bool = True) -> dict:
    return T._feat(fid, "box", {"length": _dim(L), "width": _dim(W), "height": _dim(H),
                                "centered": centered}, refs={})


def _top_face_sel(h: float) -> dict:
    return {"kind": "face", "surf": "plane", "centroid": [0.0, 0.0, h / 2.0],
            "axis": [0.0, 0.0, 1.0], "owner_feature": "f1", "hint_index": 0}


def _hole(fid: str, h: float, points: list) -> dict:
    return T._feat(fid, "hole", {"style": "simple", "end": "through_all",
                                 "standard": "custom", "diameter": _dim(_D)},
                   refs={"face": _top_face_sel(h), "points": points})


def _cyl(depth: float) -> float:
    return math.pi * (_D / 2.0) ** 2 * depth


# --------------------------------------------------------------------------- linear pattern


def test_linear_pattern_of_hole_grid() -> None:
    """A through hole patterned 3×2 drills 6 holes total (removes ~6× one hole's material)."""
    H = 20.0
    base = _box("f1", 60.0, 40.0, H)
    hole = _hole("f2", H, [[-15.0, -7.0]])
    pat = T._feat("f3", "linear_pattern", {
        "count1": 3, "spacing1": _dim(15.0),
        "count2": 2, "spacing2": _dim(14.0),
    }, refs={"dir1": "axis.x", "dir2": "axis.y", "features": ["f2"]})
    doc = T._doc([base, hole, pat])
    assert validate_document(doc) == [], "linear pattern doc must validate"
    res = compile_document(doc)
    assert T._status(res, "f2")["ok"], T._status(res, "f2").get("error")
    s = T._status(res, "f3")
    assert s["ok"], f"linear pattern failed: {s.get('error')}"
    vol = res.bodies[-1].shape.volume
    expected = 60.0 * 40.0 * H - 6.0 * _cyl(H)
    assert abs(vol - expected) < 5.0, f"6-hole grid vol {vol:.1f} != {expected:.1f}"


def test_linear_pattern_single_direction() -> None:
    """count1 only (no dir2) → a 1-D row of 4 holes."""
    H = 20.0
    base = _box("f1", 100.0, 40.0, H)
    hole = _hole("f2", H, [[-22.0, 0.0]])
    pat = T._feat("f3", "linear_pattern", {"count1": 4, "spacing1": _dim(15.0)},
                  refs={"dir1": "axis.x", "features": ["f2"]})
    res = compile_document(T._doc([base, hole, pat]))
    s = T._status(res, "f3")
    assert s["ok"], f"1-D pattern failed: {s.get('error')}"
    vol = res.bodies[-1].shape.volume
    expected = 100.0 * 40.0 * H - 4.0 * _cyl(H)
    assert abs(vol - expected) < 5.0, f"4-hole row vol {vol:.1f} != {expected:.1f}"


# --------------------------------------------------------------------------- circular pattern


def test_circular_pattern_of_hole_full_circle() -> None:
    """One hole patterned 4× about Z (equal spacing) = 4 holes at 0/90/180/270°."""
    H = 20.0
    base = _box("f1", 60.0, 60.0, H)
    hole = _hole("f2", H, [[20.0, 0.0]])
    pat = T._feat("f3", "circular_pattern", {"count": 4, "angle": _dim(360.0)},
                  refs={"axis": "axis.z", "features": ["f2"]})
    doc = T._doc([base, hole, pat])
    assert validate_document(doc) == [], "circular pattern doc must validate"
    res = compile_document(doc)
    s = T._status(res, "f3")
    assert s["ok"], f"circular pattern failed: {s.get('error')}"
    vol = res.bodies[-1].shape.volume
    expected = 60.0 * 60.0 * H - 4.0 * _cyl(H)
    assert abs(vol - expected) < 5.0, f"4-hole ring vol {vol:.1f} != {expected:.1f}"


# --------------------------------------------------------------------------- mirror


def test_mirror_feature_reflects_cut() -> None:
    """A hole on +X mirrored across the Right (YZ) plane drills a matching -X hole."""
    H = 20.0
    base = _box("f1", 60.0, 40.0, H)
    hole = _hole("f2", H, [[15.0, 0.0]])
    mir = T._feat("f3", "mirror", {}, refs={"plane": "plane.right", "features": ["f2"]})
    doc = T._doc([base, hole, mir])
    assert validate_document(doc) == [], "mirror doc must validate"
    res = compile_document(doc)
    s = T._status(res, "f3")
    assert s["ok"], f"mirror failed: {s.get('error')}"
    vol = res.bodies[-1].shape.volume
    expected = 60.0 * 40.0 * H - 2.0 * _cyl(H)
    assert abs(vol - expected) < 5.0, f"mirrored-hole vol {vol:.1f} != {expected:.1f}"


def test_mirror_whole_body() -> None:
    """No features named → mirror the whole body and union (doubles a one-sided part)."""
    base = _box("f1", 50.0, 30.0, 10.0, centered=False)   # x: 0..50
    mir = T._feat("f2", "mirror", {}, refs={"plane": "plane.right"})  # YZ (x=0)
    res = compile_document(T._doc([base, mir]))
    s = T._status(res, "f2")
    assert s["ok"], f"whole-body mirror failed: {s.get('error')}"
    vol = res.bodies[-1].shape.volume
    assert abs(vol - 2.0 * 50.0 * 30.0 * 10.0) < 5.0, f"mirrored body vol {vol:.1f}"


# --------------------------------------------------------------------------- draft


def test_draft_tapers_side_faces() -> None:
    """Drafting two side faces about the top plane reduces (tapers) the box volume."""
    base = _box("f1", 40.0, 40.0, 20.0)                    # centred, z: -10..10
    toppl = T._feat("f2", "plane", {"method": "offset", "offset": _dim(10.0)},
                    refs={"base": "plane.top"})            # neutral at z=+10 (box top)
    side = lambda cx, cy, ax: {"kind": "face", "surf": "plane",
                               "centroid": [cx, cy, 0.0], "axis": ax,
                               "owner_feature": "f1"}
    draft = T._feat("f3", "draft", {"angle": _dim(5.0)},
                    refs={"neutral": {"kind": "datum", "feature": "f2"},
                          "faces": [side(20.0, 0.0, [1.0, 0.0, 0.0]),
                                    side(-20.0, 0.0, [-1.0, 0.0, 0.0])]})
    doc = T._doc([base, toppl, draft])
    assert validate_document(doc) == [], "draft doc must validate"
    res = compile_document(doc)
    s = T._status(res, "f3")
    assert s["ok"], f"draft failed: {s.get('error')}"
    vol = res.bodies[-1].shape.volume
    full = 40.0 * 40.0 * 20.0
    # Two 40-wide faces tilted 5° over the 20mm height each sweep a wedge of
    # 0.5·tan(5°)·H·H·width ≈ 700 mm³ → ~1400 total. Sign (in/out) is the user's flip;
    # assert the taper happened with the right magnitude, not a specific direction.
    wedge = 2.0 * 0.5 * math.tan(math.radians(5.0)) * 20.0 * 20.0 * 40.0
    assert abs(abs(vol - full) - wedge) < 250.0, \
        f"draft delta {vol - full:.1f} != ±{wedge:.0f} (taper wrong/absent)"


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print(f"[pattern/mirror/draft] {_name} ok")
