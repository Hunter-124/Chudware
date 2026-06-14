"""Hole Wizard parity extensions — ANSI/UNC + UNF + BSP/NPT threads, cosmetic-thread
annotations, and sketch-point hole positions.

Mirrors the harness in ``test_hole.py`` (same _doc / _plate / _top_face_sel / _status / _vol
builders). Covers, on top of the existing ISO-metric / multi-position behaviour:

  * a 1/4-20 UNC TAPPED hole drills the standard tap-drill diameter (0.201 in = 5.105 mm),
    NOT the clearance drill;
  * a 1/4-20 UNC clearance (simple) hole still drills the clearance drill (different size),
    proving the tap-vs-clearance split;
  * the cosmetic-thread record on a tapped hole carries the right designation, MAJOR diameter
    (0.25 in = 6.35 mm), pitch (1.27 mm), depth, and class — both echoed numerically in
    feature_status.resolved AND stashed structured on the ``_cosmetic_threads`` side-channel;
  * a metric M6 tap also emits a cosmetic thread (M6x1.0, major 6.0, tap-drill 5.0);
  * UNF (fine) is distinct from UNC (finer pitch → larger tap drill);
  * BSP/NPT pipe threads resolve and are flagged approximate;
  * sketch-point positions drill exactly N holes at a sketch's N points;
  * existing ISO + explicit multi-position behaviour is unchanged (a regression guard re-runs a
    custom-diameter multi-point hole).

Run:  & $py -X utf8 -m pytest engine/doc/test_hole_ext.py -q
"""

from __future__ import annotations

import math

from engine.doc.compiler import compile_document
from engine.doc.schema import validate_part
from engine.doc.features import hole as hole_mod


_INCH = 25.4


# --------------------------------------------------------------------------- builders
# (mirrors test_hole.py so the two files share the same document shape)

def _dim(value: float, expr: str | None = None) -> dict:
    return {"value": value, "expr": expr}


def _feat(fid: str, ftype: str, params: dict, refs: dict | None = None,
          name: str | None = None) -> dict:
    return {"id": fid, "type": ftype, "name": name or fid,
            "suppressed": False, "params": params, "refs": refs or {}}


def _doc(features: list[dict]) -> dict:
    return {
        "schema": "aicad.part/1",
        "id": "test-hole-ext",
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


def _cosmetic_records(res, fid: str) -> list[dict]:
    """Pull the structured cosmetic-thread records the compiler stamps on the result body.

    The compiler stashes them both on a ``_cosmetic_threads`` sentinel datum (for the host/BOM,
    keyed by feature id) and directly on the result solid as ``.cosmetic_threads`` for in-process
    consumers. Tests read the body attribute (the compiler result does not surface the sentinel).
    """
    body = res.bodies[0].shape
    return list(getattr(body, "cosmetic_threads", []) or [])


# --------------------------------------------------------------------------- table sanity

def test_unc_table_values_are_standard() -> None:
    # 1/4-20 UNC: tap drill #7 = 0.201", major 0.250", pitch 25.4/20.
    e = hole_mod._STD_TABLE["1/4-20"]
    assert abs(e["tap_drill"] - 0.201 * _INCH) < 1e-6
    assert abs(e["major"] - 0.250 * _INCH) < 1e-6
    assert abs(e["pitch"] - _INCH / 20.0) < 1e-6
    # clearance ("close fit") drill is larger than the major diameter
    assert e["diameter"] > e["major"] > e["tap_drill"]


def test_unf_finer_than_unc() -> None:
    # 1/4-28 UNF has a finer pitch (smaller) and a LARGER tap drill than 1/4-20 UNC.
    unc = hole_mod._STD_TABLE["1/4-20"]
    unf = hole_mod._STD_TABLE["1/4-28"]
    assert unf["pitch"] < unc["pitch"]
    assert unf["tap_drill"] > unc["tap_drill"]
    assert abs(unc["major"] - unf["major"]) < 1e-9  # same nominal major


def test_numbered_sizes_present() -> None:
    for k in ("#4-40", "#6-32", "#8-32", "#10-24", "#12-24"):
        assert k in hole_mod._STD_TABLE, k
    # numbered major diameter law d = 0.060 + n*0.013 in
    assert abs(hole_mod._STD_TABLE["#4-40"]["major"] - (0.060 + 4 * 0.013) * _INCH) < 1e-3


def test_pipe_threads_present_and_flagged_approx() -> None:
    for k in ("G1/2", "NPT1/2"):
        assert k in hole_mod._STD_TABLE, k
        assert k in hole_mod._APPROX_THREAD_KEYS


# --------------------------------------------------------------------------- UNC tap drill geometry

def test_quarter_20_unc_tap_drills_tap_drill_diameter() -> None:
    plate_h = 10.0
    tap_drill = 0.201 * _INCH  # 5.105 mm
    doc = _doc([
        _plate(h=plate_h),
        _feat("f2", "hole",
              {"style": "tap", "end": "through_all", "standard": "1/4-20"},
              refs={"face": _top_face_sel()}),
    ])
    assert validate_part(doc) == [], validate_part(doc)
    res = compile_document(doc)
    st = _status(res, "f2")
    assert st["ok"], st["error"]
    # The drilled bore == tap-drill diameter (volume of one through cylinder).
    assert abs(st["resolved"]["diameter"] - tap_drill) < 1e-6, st["resolved"]
    delta = 40 * 40 * plate_h - _vol(res)
    expected = math.pi * (tap_drill / 2) ** 2 * plate_h
    assert abs(delta - expected) / expected < 0.02, (delta, expected)


def test_quarter_20_unc_simple_drills_clearance_not_tap() -> None:
    # A simple (clearance) hole of the SAME standard drills the CLEARANCE drill — bigger than the
    # tap drill. This proves the tap-vs-clearance bore split, not just a single table lookup.
    plate_h = 10.0
    clearance = hole_mod._STD_TABLE["1/4-20"]["diameter"]
    tap_drill = hole_mod._STD_TABLE["1/4-20"]["tap_drill"]
    assert clearance > tap_drill
    doc = _doc([
        _plate(h=plate_h),
        _feat("f2", "hole",
              {"style": "simple", "end": "through_all", "standard": "1/4-20"},
              refs={"face": _top_face_sel()}),
    ])
    assert validate_part(doc) == []
    res = compile_document(doc)
    st = _status(res, "f2")
    assert st["ok"], st["error"]
    assert abs(st["resolved"]["diameter"] - clearance) < 1e-6, st["resolved"]
    delta = 40 * 40 * plate_h - _vol(res)
    expected = math.pi * (clearance / 2) ** 2 * plate_h
    assert abs(delta - expected) / expected < 0.02, (delta, expected)


# --------------------------------------------------------------------------- cosmetic thread

def test_unc_tap_emits_cosmetic_thread_record() -> None:
    plate_h = 10.0
    doc = _doc([
        _plate(h=plate_h),
        _feat("f2", "hole",
              {"style": "tap", "end": "through_all", "standard": "1/4-20"},
              refs={"face": _top_face_sel()}),
    ])
    assert validate_part(doc) == []
    res = compile_document(doc)
    st = _status(res, "f2")
    assert st["ok"], st["error"]

    # 1) numeric echo in feature_status.resolved
    r = st["resolved"]
    assert r.get("cosmetic_thread") == 1.0
    assert abs(r["thread_major_dia"] - 0.250 * _INCH) < 1e-6   # 6.35 mm major
    assert abs(r["thread_pitch"] - _INCH / 20.0) < 1e-6        # 1.27 mm pitch
    assert abs(r["thread_minor_dia"] - 0.201 * _INCH) < 1e-6   # tap-drill bore
    assert abs(r["thread_depth"] - plate_h) < 0.05             # through ≈ wall thickness

    # 2) structured record on the body side-channel
    recs = _cosmetic_records(res, "f2")
    assert len(recs) == 1, recs
    rec = recs[0]
    assert rec["designation"] == "1/4-20 UNC", rec["designation"]
    assert rec["class"] == "unified"
    assert rec["through"] is True
    assert abs(rec["major_dia"] - 0.250 * _INCH) < 1e-6
    assert abs(rec["pitch"] - _INCH / 20.0) < 1e-6
    assert rec["approx"] is False


def test_metric_tap_emits_cosmetic_thread() -> None:
    plate_h = 10.0
    doc = _doc([
        _plate(h=plate_h),
        _feat("f2", "hole",
              {"style": "tap", "end": "through_all", "standard": "M6"},
              refs={"face": _top_face_sel()}),
    ])
    assert validate_part(doc) == []
    res = compile_document(doc)
    st = _status(res, "f2")
    assert st["ok"], st["error"]
    r = st["resolved"]
    # M6 tap drills the 5.0 mm tap-drill, threads call out major 6.0 / pitch 1.0.
    assert abs(r["diameter"] - 5.0) < 1e-6
    assert abs(r["thread_major_dia"] - 6.0) < 1e-6
    assert abs(r["thread_pitch"] - 1.0) < 1e-6
    recs = _cosmetic_records(res, "f2")
    assert len(recs) == 1
    assert recs[0]["designation"] == "M6x1", recs[0]["designation"]
    assert recs[0]["class"] == "metric"


def test_simple_hole_has_no_cosmetic_thread() -> None:
    # A plain (non-tapped) clearance hole gets NO cosmetic thread.
    doc = _doc([
        _plate(),
        _feat("f2", "hole",
              {"style": "simple", "end": "through_all", "standard": "M6"},
              refs={"face": _top_face_sel()}),
    ])
    res = compile_document(doc)
    st = _status(res, "f2")
    assert st["ok"], st["error"]
    assert "cosmetic_thread" not in st["resolved"]
    assert _cosmetic_records(res, "f2") == []


def test_cosmetic_flag_threads_a_clearance_hole() -> None:
    # params.cosmetic on a simple hole emits a cosmetic thread without changing the bore.
    doc = _doc([
        _plate(),
        _feat("f2", "hole",
              {"style": "simple", "end": "through_all", "standard": "M6",
               "cosmetic": True},
              refs={"face": _top_face_sel()}),
    ])
    res = compile_document(doc)
    st = _status(res, "f2")
    assert st["ok"], st["error"]
    assert st["resolved"].get("cosmetic_thread") == 1.0
    # bore is still the clearance drill (simple style), not the tap drill
    assert abs(st["resolved"]["diameter"] - hole_mod._STD_TABLE["M6"]["diameter"]) < 1e-6


def test_pipe_thread_record_is_approx() -> None:
    doc = _doc([
        _plate(),
        _feat("f2", "hole",
              {"style": "tap", "end": "through_all", "standard": "NPT1/4"},
              refs={"face": _top_face_sel()}),
    ])
    assert validate_part(doc) == []
    res = compile_document(doc)
    st = _status(res, "f2")
    assert st["ok"], st["error"]
    recs = _cosmetic_records(res, "f2")
    assert len(recs) == 1
    assert recs[0]["class"] == "pipe"
    assert recs[0]["approx"] is True
    assert recs[0]["designation"] == "NPT1/4"


def test_cosmetic_thread_per_position() -> None:
    # Multi-position tap → one cosmetic-thread record per drilled bore.
    pts = [[10.0, 10.0], [-10.0, -10.0], [10.0, -10.0]]
    doc = _doc([
        _plate(),
        _feat("f2", "hole",
              {"style": "tap", "end": "through_all", "standard": "M8"},
              refs={"face": _top_face_sel(), "points": pts}),
    ])
    assert validate_part(doc) == []
    res = compile_document(doc)
    st = _status(res, "f2")
    assert st["ok"], st["error"]
    recs = _cosmetic_records(res, "f2")
    assert len(recs) == 3, recs
    # each record stamped with its own face-frame (u,v)
    uvs = sorted(tuple(r["at_uv"]) for r in recs)
    assert uvs == sorted(tuple(p) for p in pts), uvs


# --------------------------------------------------------------------------- sketch-point positions

def _sketch_with_points(fid: str, pts: list[tuple[float, float]],
                        plane: str = "plane.top") -> dict:
    """A sketch feature on *plane* holding one POINT entity per (u, v) in *pts*."""
    entities = [
        {"id": f"e{i}", "type": "point", "construction": False, "p": [u, v]}
        for i, (u, v) in enumerate(pts)
    ]
    return _feat(fid, "sketch", {"entities": entities, "constraints": []},
                 refs={"plane": plane})


def test_sketch_point_positions_drill_n_holes() -> None:
    # plane.top is the XY plane (z=0). The plate top face is at z=+5; sketch points are projected
    # into the face frame, so a point at sketch (u,v) lands at face-local (u,v).
    d, plate_h = 4.0, 10.0
    single = math.pi * (d / 2) ** 2 * plate_h
    pts = [(8.0, 8.0), (-8.0, 8.0), (8.0, -8.0), (-8.0, -8.0), (0.0, 0.0)]
    doc = _doc([
        _plate(h=plate_h),
        _sketch_with_points("f2", pts, plane="plane.top"),
        _feat("f3", "hole",
              {"style": "simple", "end": "through_all", "standard": "custom",
               "diameter": _dim(d)},
              refs={"face": _top_face_sel(), "positions_sketch": "f2"}),
    ])
    assert validate_part(doc) == [], validate_part(doc)
    res = compile_document(doc)
    st = _status(res, "f3")
    assert st["ok"], st["error"]
    assert abs(st["resolved"]["_hole_count"] - float(len(pts))) < 1e-9, st["resolved"]
    delta = 40 * 40 * plate_h - _vol(res)
    n = len(pts)
    assert abs(delta - n * single) / (n * single) < 0.02, (delta, n * single)


def test_sketch_point_selector_form() -> None:
    # positions_sketch as a selector dict carrying the id under 'feature'.
    d, plate_h = 4.0, 10.0
    pts = [(10.0, 10.0), (-10.0, -10.0)]
    doc = _doc([
        _plate(h=plate_h),
        _sketch_with_points("f2", pts, plane="plane.top"),
        _feat("f3", "hole",
              {"style": "simple", "end": "through_all", "standard": "custom",
               "diameter": _dim(d)},
              refs={"face": _top_face_sel(),
                    "positions_sketch": {"kind": "datum", "feature": "f2"}}),
    ])
    assert validate_part(doc) == [], validate_part(doc)
    res = compile_document(doc)
    st = _status(res, "f3")
    assert st["ok"], st["error"]
    assert abs(st["resolved"]["_hole_count"] - 2.0) < 1e-9


def test_sketch_points_combine_with_explicit_points() -> None:
    # Explicit points AND sketch points both contribute (append behaviour).
    d, plate_h = 3.0, 10.0
    single = math.pi * (d / 2) ** 2 * plate_h
    sketch_pts = [(12.0, 12.0), (-12.0, 12.0)]
    explicit = [[12.0, -12.0], [-12.0, -12.0]]
    doc = _doc([
        _plate(h=plate_h),
        _sketch_with_points("f2", sketch_pts, plane="plane.top"),
        _feat("f3", "hole",
              {"style": "simple", "end": "through_all", "standard": "custom",
               "diameter": _dim(d)},
              refs={"face": _top_face_sel(), "positions_sketch": "f2",
                    "points": explicit}),
    ])
    assert validate_part(doc) == []
    res = compile_document(doc)
    st = _status(res, "f3")
    assert st["ok"], st["error"]
    assert abs(st["resolved"]["_hole_count"] - 4.0) < 1e-9, st["resolved"]
    delta = 40 * 40 * plate_h - _vol(res)
    assert abs(delta - 4 * single) / (4 * single) < 0.02, (delta, single)


def test_sketch_construction_points_skipped() -> None:
    # Construction points are reference geometry — not drilled.
    d, plate_h = 4.0, 10.0
    entities = [
        {"id": "e0", "type": "point", "construction": False, "p": [10.0, 10.0]},
        {"id": "e1", "type": "point", "construction": True, "p": [-10.0, -10.0]},
    ]
    doc = _doc([
        _plate(h=plate_h),
        _feat("f2", "sketch", {"entities": entities, "constraints": []},
              refs={"plane": "plane.top"}),
        _feat("f3", "hole",
              {"style": "simple", "end": "through_all", "standard": "custom",
               "diameter": _dim(d)},
              refs={"face": _top_face_sel(), "positions_sketch": "f2"}),
    ])
    assert validate_part(doc) == []
    res = compile_document(doc)
    st = _status(res, "f3")
    assert st["ok"], st["error"]
    assert abs(st["resolved"]["_hole_count"] - 1.0) < 1e-9, st["resolved"]


def test_sketch_with_no_points_errors_cleanly() -> None:
    # A sketch with only a circle (no point entities) → contained error on the hole feature.
    doc = _doc([
        _plate(),
        _feat("f2", "sketch",
              {"entities": [{"id": "e0", "type": "circle", "construction": False,
                             "center": [0.0, 0.0], "radius": 5.0}],
               "constraints": []},
              refs={"plane": "plane.top"}),
        _feat("f3", "hole",
              {"style": "simple", "end": "through_all", "standard": "custom",
               "diameter": _dim(4.0)},
              refs={"face": _top_face_sel(), "positions_sketch": "f2"}),
    ])
    assert validate_part(doc) == []
    res = compile_document(doc)
    assert _status(res, "f1")["ok"]
    st = _status(res, "f3")
    assert not st["ok"]
    assert "point" in st["error"].lower(), st["error"]


# --------------------------------------------------------------------------- regression guard

def test_iso_and_multipoint_unchanged() -> None:
    # Custom-diameter multi-point hole still behaves exactly as before (no thread, right count).
    d, plate_h = 4.0, 10.0
    single = math.pi * (d / 2) ** 2 * plate_h
    pts = [[10.0, 10.0], [-10.0, 10.0], [10.0, -10.0], [-10.0, -10.0]]
    doc = _doc([
        _plate(h=plate_h),
        _feat("f2", "hole",
              {"style": "simple", "end": "through_all", "standard": "custom",
               "diameter": _dim(d)},
              refs={"face": _top_face_sel(), "points": pts}),
    ])
    res = compile_document(doc)
    st = _status(res, "f2")
    assert st["ok"], st["error"]
    assert abs(st["resolved"]["_hole_count"] - 4.0) < 1e-9
    assert "cosmetic_thread" not in st["resolved"]
    delta = 40 * 40 * plate_h - _vol(res)
    assert abs(delta - 4 * single) / (4 * single) < 0.02, (delta, single)
