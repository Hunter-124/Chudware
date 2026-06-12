"""C33-A — multi-position Hole Wizard compiler tests.

Covers:
  * single hole (back-compat default: no positions -> one hole at face centre)
  * explicit refs.points -> N bores, volume == single delta * N within tolerance
  * rectangular grid (nx,ny,dx,dy) -> nx*ny bores
  * vertex-selector positions projected into the face frame
  * cbore / csink remove MORE material than a simple hole of the same diameter
  * position cap (>64) raises a clean contained error
  * schema accepts the new refs.points / grid params

Run:  & $py -m pytest engine/doc/test_hole.py -q
"""

from __future__ import annotations

import math

from engine.doc.compiler import compile_document
from engine.doc.schema import validate_part


# --------------------------------------------------------------------------- builders

def _dim(value: float, expr: str | None = None) -> dict:
    return {"value": value, "expr": expr}


def _feat(fid: str, ftype: str, params: dict, refs: dict | None = None,
          name: str | None = None) -> dict:
    return {"id": fid, "type": ftype, "name": name or fid,
            "suppressed": False, "params": params, "refs": refs or {}}


def _doc(features: list[dict]) -> dict:
    return {
        "schema": "aicad.part/1",
        "id": "test-hole",
        "name": "test",
        "units": {"system": "MMGS"},
        "material": None,
        "equations": {"globals": []},
        "features": features,
    }


# A 40x40x10 plate (centred at origin). Top face is at z=+5, normal +Z.
def _plate(l: float = 40.0, w: float = 40.0, h: float = 10.0) -> dict:
    return _feat("f1", "box",
                 {"length": _dim(l), "width": _dim(w), "height": _dim(h)})


def _top_face_sel() -> dict:
    return {"kind": "face", "surf": "plane",
            "centroid": [0.0, 0.0, 5.0], "axis": [0.0, 0.0, 1.0],
            "owner_feature": "f1", "hint_index": 0}


def _status(res, fid: str) -> dict:
    for s in res.feature_status:
        if s["id"] == fid:
            return s
    raise AssertionError(f"no status for {fid}")


def _vol(res) -> float:
    return res.bodies[0].shape.volume


# --------------------------------------------------------------------------- tests

def test_schema_accepts_points_and_grid() -> None:
    doc = _doc([
        _plate(),
        _feat("f2", "hole",
              {"style": "simple", "end": "through_all", "standard": "custom",
               "diameter": _dim(5.0), "nx": 2, "ny": 1, "dx": _dim(10.0)},
              refs={"face": _top_face_sel(),
                    "points": [[5.0, 5.0], [-5.0, -5.0]]}),
    ])
    assert validate_part(doc) == [], validate_part(doc)


def test_single_hole_backcompat() -> None:
    # No positions -> one hole at the face centre (back-compatible default).
    d, plate_h = 6.0, 10.0
    doc = _doc([
        _plate(h=plate_h),
        _feat("f2", "hole",
              {"style": "simple", "end": "through_all", "standard": "custom",
               "diameter": _dim(d)},
              refs={"face": _top_face_sel()}),
    ])
    assert validate_part(doc) == []
    res = compile_document(doc)
    st = _status(res, "f2")
    assert st["ok"], st["error"]
    assert abs(st["resolved"]["_hole_count"] - 1.0) < 1e-9
    delta = 40 * 40 * plate_h - _vol(res)
    expected = math.pi * (d / 2) ** 2 * plate_h
    assert abs(delta - expected) / expected < 0.02, (delta, expected)


def test_multi_position_points_n_bores() -> None:
    d, plate_h = 4.0, 10.0
    single = math.pi * (d / 2) ** 2 * plate_h

    base = [_plate(h=plate_h)]

    # single-hole reference
    res1 = compile_document(_doc(base + [
        _feat("f2", "hole",
              {"style": "simple", "end": "through_all", "standard": "custom",
               "diameter": _dim(d)},
              refs={"face": _top_face_sel()}),
    ]))
    delta1 = 40 * 40 * plate_h - _vol(res1)

    # four explicit positions, well separated so they don't overlap
    pts = [[10.0, 10.0], [-10.0, 10.0], [10.0, -10.0], [-10.0, -10.0]]
    doc4 = _doc(base + [
        _feat("f2", "hole",
              {"style": "simple", "end": "through_all", "standard": "custom",
               "diameter": _dim(d)},
              refs={"face": _top_face_sel(), "points": pts}),
    ])
    assert validate_part(doc4) == []
    res4 = compile_document(doc4)
    st = _status(res4, "f2")
    assert st["ok"], st["error"]
    assert abs(st["resolved"]["_hole_count"] - 4.0) < 1e-9
    delta4 = 40 * 40 * plate_h - _vol(res4)

    # 4 holes remove ~4x a single hole's volume
    assert abs(delta4 - 4 * delta1) / (4 * delta1) < 0.02, (delta4, delta1)
    assert abs(delta4 - 4 * single) / (4 * single) < 0.02, (delta4, single)


def test_rectangular_grid() -> None:
    d, plate_h = 3.0, 10.0
    single = math.pi * (d / 2) ** 2 * plate_h
    doc = _doc([
        _plate(h=plate_h),
        _feat("f2", "hole",
              {"style": "simple", "end": "through_all", "standard": "custom",
               "diameter": _dim(d), "nx": 3, "ny": 2,
               "dx": _dim(10.0), "dy": _dim(10.0)},
              refs={"face": _top_face_sel()}),
    ])
    assert validate_part(doc) == []
    res = compile_document(doc)
    st = _status(res, "f2")
    assert st["ok"], st["error"]
    assert abs(st["resolved"]["_hole_count"] - 6.0) < 1e-9
    delta = 40 * 40 * plate_h - _vol(res)
    assert abs(delta - 6 * single) / (6 * single) < 0.02, (delta, single)


def test_vertex_positions_projected() -> None:
    # Two box corner vertices on the top face become hole positions.
    d, plate_h = 4.0, 10.0
    single = math.pi * (d / 2) ** 2 * plate_h
    # Use interior-ish points expressed as vertex selectors (world coords on the top face).
    v1 = {"kind": "vertex", "centroid": [12.0, 12.0, 5.0],
          "owner_feature": "f1", "hint_index": 0}
    v2 = {"kind": "vertex", "centroid": [-12.0, -12.0, 5.0],
          "owner_feature": "f1", "hint_index": 1}
    doc = _doc([
        _plate(h=plate_h),
        _feat("f2", "hole",
              {"style": "simple", "end": "through_all", "standard": "custom",
               "diameter": _dim(d)},
              refs={"face": _top_face_sel(), "positions": [v1, v2]}),
    ])
    assert validate_part(doc) == []
    res = compile_document(doc)
    st = _status(res, "f2")
    assert st["ok"], st["error"]
    assert abs(st["resolved"]["_hole_count"] - 2.0) < 1e-9
    delta = 40 * 40 * plate_h - _vol(res)
    assert abs(delta - 2 * single) / (2 * single) < 0.02, (delta, single)


def test_cbore_removes_more_than_simple() -> None:
    plate_h = 12.0
    base = [_plate(h=plate_h)]
    simple = compile_document(_doc(base + [
        _feat("f2", "hole",
              {"style": "simple", "end": "through_all", "standard": "M6"},
              refs={"face": _top_face_sel()}),
    ]))
    cbore = compile_document(_doc(base + [
        _feat("f2", "hole",
              {"style": "cbore", "end": "through_all", "standard": "M6"},
              refs={"face": _top_face_sel()}),
    ]))
    assert _status(simple, "f2")["ok"]
    assert _status(cbore, "f2")["ok"]
    assert _vol(cbore) < _vol(simple), "cbore must remove more than a simple hole"


def test_csink_removes_more_than_simple() -> None:
    plate_h = 12.0
    base = [_plate(h=plate_h)]
    simple = compile_document(_doc(base + [
        _feat("f2", "hole",
              {"style": "simple", "end": "through_all", "standard": "M6"},
              refs={"face": _top_face_sel()}),
    ]))
    csink = compile_document(_doc(base + [
        _feat("f2", "hole",
              {"style": "csink", "end": "through_all", "standard": "M6"},
              refs={"face": _top_face_sel()}),
    ]))
    assert _status(csink, "f2")["ok"], _status(csink, "f2")["error"]
    assert _vol(csink) < _vol(simple), "csink must remove more than a simple hole"


def test_position_cap_errors_cleanly() -> None:
    # 9x9 = 81 positions > 64 cap -> contained error (f2 not ok), plate (f1) still builds.
    doc = _doc([
        _plate(),
        _feat("f2", "hole",
              {"style": "simple", "end": "through_all", "standard": "custom",
               "diameter": _dim(1.0), "nx": 9, "ny": 9,
               "dx": _dim(3.0), "dy": _dim(3.0)},
              refs={"face": _top_face_sel()}),
    ])
    assert validate_part(doc) == []
    res = compile_document(doc)
    assert _status(res, "f1")["ok"]
    st = _status(res, "f2")
    assert not st["ok"]
    assert "exceeds" in st["error"] or "cap" in st["error"], st["error"]


def test_blind_depth() -> None:
    d, plate_h, depth = 5.0, 20.0, 8.0
    doc = _doc([
        _plate(h=plate_h),
        _feat("f2", "hole",
              {"style": "simple", "end": "blind", "standard": "custom",
               "diameter": _dim(d), "depth": _dim(depth)},
              refs={"face": _top_face_sel()}),
    ])
    assert validate_part(doc) == []
    res = compile_document(doc)
    st = _status(res, "f2")
    assert st["ok"], st["error"]
    delta = 40 * 40 * plate_h - _vol(res)
    expected = math.pi * (d / 2) ** 2 * depth
    assert abs(delta - expected) / expected < 0.02, (delta, expected)
