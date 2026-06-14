"""Style-spline tests (SolidWorks-parity "Style Spline" sketch entity).

A ``style_spline`` entity is shaped by a CONTROL POLYGON (its ``points`` are control poles), NOT
interpolated through-points like the ``spline`` entity. The defining properties: the curve starts at
``points[0]``, ends at ``points[-1]``, and is built by poles (a clamped cubic B-spline by poles, with
a Bezier fallback). A CLOSED control polygon (first pole == last pole) forms a region that extrudes to
a solid; an OPEN style spline is a valid edge that participates in loop detection.

Run (from repo root, dev venv):
    set PYTHONUTF8=1 && <devpython> -X utf8 -m pytest engine/doc/test_style_spline.py -q
"""

from __future__ import annotations

import math

import engine.doc.test_features1 as T
from engine.doc.compiler import compile_document
from engine.doc.schema import validate_document
from engine.doc.features.sketch import _entity_to_edges


def _ss_sketch(fid: str, points: list, refs: dict | None = None) -> dict:
    return T._feat(fid, "sketch", {"entities": [
        {"id": "e1", "type": "style_spline", "construction": False, "points": points},
    ], "constraints": []}, refs=refs or {"plane": "plane.top"})


def _extrude(fid: str, profile: list, depth: float = 5.0) -> dict:
    return T._feat(fid, "extrude",
                   {"mode": "boss", "end": "blind", "depth": {"value": depth, "expr": None}},
                   refs={"profile": profile})


# --------------------------------------------------------------------------- validation


def test_style_spline_validates() -> None:
    doc = _ss_sketch("f1", [[0, 0], [5, 8], [12, 3], [18, 9]])
    assert validate_document(T._doc([doc])) == [], "style_spline sketch must validate"


def test_style_spline_needs_two_poles() -> None:
    """A single-pole style spline fails schema validation (>=2 [x,y] poles required)."""
    bad = T._feat("f1", "sketch", {"entities": [
        {"id": "e1", "type": "style_spline", "construction": False, "points": [[0, 0]]},
    ], "constraints": []}, refs={"plane": "plane.top"})
    assert validate_document(T._doc([bad])), "1-pole style_spline must fail validation"


def test_style_spline_subhandles_in_range() -> None:
    """Control poles are addressable as p0..pN sub-handles (parallel to spline)."""
    sk = _ss_sketch("f1", [[0, 0], [5, 8], [12, 3], [18, 9]])
    # Reference p2 (valid) and p9 (out of range) via a fix constraint.
    sk["params"]["constraints"] = [
        {"id": "c1", "type": "fix", "refs": ["e1.p2"]},
    ]
    assert validate_document(T._doc([sk])) == [], "e1.p2 must be a valid style_spline sub-handle"
    sk["params"]["constraints"] = [
        {"id": "c1", "type": "fix", "refs": ["e1.p9"]},
    ]
    assert validate_document(T._doc([sk])), "e1.p9 (out of range) must fail validation"


# --------------------------------------------------------------------------- geometry: open


def test_open_style_spline_endpoints_match_first_last_poles() -> None:
    """The DEFINING property: the edge starts at points[0] and ends at points[-1] (the curve is
    shaped by, not interpolated through, the control poles)."""
    poles = [[0.0, 0.0], [5.0, 10.0], [15.0, 2.0], [20.0, 9.0]]
    edges = _entity_to_edges(
        {"id": "e1", "type": "style_spline", "construction": False, "points": poles})
    assert len(edges) == 1, "style_spline must produce exactly one edge"
    e = edges[0]
    start = tuple(e @ 0)
    end = tuple(e @ 1)
    assert math.hypot(start[0] - poles[0][0], start[1] - poles[0][1]) < 1e-6, \
        f"edge start {start[:2]} must match first pole {poles[0]}"
    assert math.hypot(end[0] - poles[-1][0], end[1] - poles[-1][1]) < 1e-6, \
        f"edge end {end[:2]} must match last pole {poles[-1]}"
    assert e.length > 0.0, "style_spline edge must have positive length"


def test_open_style_spline_status_ok_no_region() -> None:
    """An open style spline compiles ok and forms no region (it doesn't close)."""
    doc = T._doc([_ss_sketch("f1", [[0, 0], [5, 8], [12, 3], [18, 9]])])
    assert validate_document(doc) == []
    res = compile_document(doc)
    s = T._status(res, "f1")
    assert s["ok"], f"open style_spline failed: {s.get('error')}"
    assert s["resolved"].get("_region_count", 0) == 0.0, "an open style_spline must not form a region"


def test_two_pole_style_spline_is_a_line() -> None:
    """The minimum (2 poles) degenerates to a degree-1 curve from p0 to p1 — still a valid edge."""
    poles = [[0.0, 0.0], [10.0, 4.0]]
    edges = _entity_to_edges(
        {"id": "e1", "type": "style_spline", "construction": False, "points": poles})
    assert len(edges) == 1
    e = edges[0]
    start = tuple(e @ 0)
    end = tuple(e @ 1)
    assert math.hypot(start[0], start[1]) < 1e-6
    assert math.hypot(end[0] - 10.0, end[1] - 4.0) < 1e-6


# --------------------------------------------------------------------------- geometry: closed


def test_closed_style_spline_extrudes_to_solid() -> None:
    """A CLOSED control polygon (square-ish hull, first pole == last) closes a region and extrudes
    (blind depth 5) to a single solid with positive volume; the document validates clean."""
    # Square-ish control hull, explicitly closed by repeating the first pole.
    poles = [[0, 0], [0, 20], [20, 20], [20, 0], [0, 0]]
    depth = 5.0
    doc = T._doc([
        _ss_sketch("f1", poles),
        _extrude("f2", [{"kind": "region", "sketch": "f1"}], depth=depth),
    ])
    assert validate_document(doc) == [], "closed style_spline doc must validate clean"
    res = compile_document(doc)
    s1 = T._status(res, "f1")
    assert s1["ok"], f"closed style_spline sketch failed: {s1.get('error')}"
    assert s1["resolved"].get("_region_count", 0) >= 1.0, \
        "closed style_spline must form >= 1 region"
    s2 = T._status(res, "f2")
    assert s2["ok"], f"style_spline extrude failed: {s2.get('error')}"
    body = res.bodies[-1].shape
    assert body.volume > 0.0, f"extruded solid must have positive volume (got {body.volume})"
    # Solid sanity: a single connected solid.
    assert len(body.solids()) == 1, "closed style_spline must extrude to a single solid"
