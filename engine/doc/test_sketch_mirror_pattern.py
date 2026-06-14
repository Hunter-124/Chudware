"""Tests for sketch_mirror_entities / sketch_pattern_entities / sketch_convert (C53).

Run from repo root with the dev venv:

    AI_CAD_REPO=... AI_CAD_PROVIDER=mock $py -m pytest engine/doc/test_sketch_mirror_pattern.py -q

These four reserved sketch tools were stubs (_op_unsupported_m2); this exercises the real ops:
  - MIRROR: reflect a line / circle / arc across a vertical construction line, asserting the
    mirrored copy appears, a symmetric (+ equal-radius) relation is added, and the sketch solves;
  - LINEAR PATTERN x4: 4 instances stepped along a direction at the right spacing;
  - CIRCULAR PATTERN x6: 6 instances equally spaced about a center;
  - CONVERT: project a model edge (and a datum plane) into the sketch as real, pinned entities;
  - empty / unsupported selection -> clean OpError with NO document mutation (atomic rejection).
"""

from __future__ import annotations

import copy
import math

import pytest

from engine.doc import ops as O
from engine.doc import session as S
from engine.doc.sketch_solver import solve_sketch


# --------------------------------------------------------------------------- fixtures


def _sketch_session():
    """A fresh part with one empty top-plane sketch. Returns (session, sketch_fid)."""
    s = S.new_session(kind="part")
    r = S.apply_ops(s, None, [
        {"op": "add_feature", "feature": {"type": "sketch", "name": "Sketch1",
         "refs": {"plane": "plane.top"}, "params": {"entities": [], "constraints": []}}}])
    return s, r["id_map"]["$0"]


def _sketch_feat(s, fid):
    return next(f for f in s.doc["features"] if f["id"] == fid)


def _solve_params(s, fid):
    feat = _sketch_feat(s, fid)
    return {"entities": feat["params"]["entities"], "constraints": feat["params"]["constraints"]}


def _ents(s, fid):
    return {e["id"]: e for e in _sketch_feat(s, fid)["params"]["entities"]}


def _cons(s, fid):
    return _sketch_feat(s, fid)["params"]["constraints"]


def _add(s, fid, entity):
    r = S.apply_ops(s, None, [{"op": "sketch_add_entity", "sketch": fid, "entity": entity}])
    return r["id_map"]["$0"]


# --------------------------------------------------------------------------- MIRROR


def test_mirror_line_about_vertical_axis():
    """Mirror a slanted line across the vertical line u=0: a reflected line appears with u flipped,
    two symmetric relations join the endpoints, and the sketch solves."""
    s, fid = _sketch_session()
    axis = _add(s, fid, {"type": "line", "p1": [0, -50], "p2": [0, 50], "construction": True})
    L = _add(s, fid, {"type": "line", "p1": [10, 0], "p2": [30, 20]})

    ncon_before = len(_cons(s, fid))
    r = S.apply_ops(s, None, [{"op": "sketch_mirror_entities", "sketch": fid,
                               "entities": [L], "line": axis}])
    mid = r["id_map"]["$0"]
    ents = _ents(s, fid)
    m = ents[mid]
    assert m["type"] == "line", m
    # reflection across u=0 negates the u coordinate, keeps v.
    assert abs(m["p1"][0] - (-10)) < 1e-9 and abs(m["p1"][1] - 0) < 1e-9, m
    assert abs(m["p2"][0] - (-30)) < 1e-9 and abs(m["p2"][1] - 20) < 1e-9, m

    # two symmetric relations added (one per endpoint pair), referencing the axis line.
    syms = [c for c in _cons(s, fid) if c["type"] == "symmetric"]
    assert len(syms) == 2, syms
    for c in syms:
        assert c["refs"][2] == axis, c
    assert len(_cons(s, fid)) == ncon_before + 2

    sol = solve_sketch(_solve_params(s, fid), {}, warm=None)
    assert sol.converged and not sol.conflicting, (sol.dof, sol.conflicting)
    print(f"[mirror-line] copy={mid} syms=2 dof={sol.dof}")


def test_mirror_circle_adds_equal_radius():
    """Mirroring a circle adds a symmetric (center) + equal (radius) relation."""
    s, fid = _sketch_session()
    axis = _add(s, fid, {"type": "line", "p1": [0, -50], "p2": [0, 50], "construction": True})
    C = _add(s, fid, {"type": "circle", "center": [20, 5], "radius": 7.0})

    r = S.apply_ops(s, None, [{"op": "sketch_mirror_entities", "sketch": fid,
                               "entities": [C], "line": axis}])
    mid = r["id_map"]["$0"]
    m = _ents(s, fid)[mid]
    assert m["type"] == "circle" and abs(m["center"][0] + 20) < 1e-9 and abs(m["radius"] - 7) < 1e-9, m
    types = sorted(c["type"] for c in _cons(s, fid) if c["type"] in ("symmetric", "equal"))
    assert types == ["equal", "symmetric"], types
    sol = solve_sketch(_solve_params(s, fid), {}, warm=None)
    assert sol.converged and not sol.conflicting, (sol.dof, sol.conflicting)
    print(f"[mirror-circle] copy={mid} relations={types}")


def test_mirror_arc_reflects_and_solves():
    """Mirroring an arc reflects the center, flips orientation, and the sketch solves."""
    s, fid = _sketch_session()
    axis = _add(s, fid, {"type": "line", "p1": [0, -50], "p2": [0, 50], "construction": True})
    A = _add(s, fid, {"type": "arc", "center": [20, 0], "radius": 10.0,
                      "start": 0.0, "end": 90.0, "ccw": True})
    r = S.apply_ops(s, None, [{"op": "sketch_mirror_entities", "sketch": fid,
                               "entities": [A], "line": axis}])
    mid = r["id_map"]["$0"]
    m = _ents(s, fid)[mid]
    assert m["type"] == "arc", m
    assert abs(m["center"][0] + 20) < 1e-9 and abs(m["radius"] - 10) < 1e-9, m
    assert m["ccw"] is False, "arc reflection must flip orientation"
    sol = solve_sketch(_solve_params(s, fid), {}, warm=None)
    assert sol.converged and not sol.conflicting, (sol.dof, sol.conflicting)
    print(f"[mirror-arc] copy={mid} ccw={m['ccw']} dof={sol.dof}")


def test_mirror_empty_selection_clean_error():
    """An empty selection raises invalid_op with no mutation."""
    s, fid = _sketch_session()
    axis = _add(s, fid, {"type": "line", "p1": [0, -1], "p2": [0, 1], "construction": True})
    doc_before = copy.deepcopy(s.doc)
    rev = s.revision
    with pytest.raises(O.OpError) as ei:
        S.apply_ops(s, None, [{"op": "sketch_mirror_entities", "sketch": fid,
                               "entities": [], "line": axis}])
    assert ei.value.code == "invalid_op", ei.value.code
    assert s.doc == doc_before and s.revision == rev
    print(f"[mirror-err] empty selection -> {ei.value.code} (no mutation)")


def test_mirror_unsupported_entity_clean_error():
    """Mirroring an unsupported entity (a point) raises unsupported_entity, no mutation."""
    s, fid = _sketch_session()
    axis = _add(s, fid, {"type": "line", "p1": [0, -1], "p2": [0, 1], "construction": True})
    P = _add(s, fid, {"type": "point", "p": [5, 5]})
    doc_before = copy.deepcopy(s.doc)
    with pytest.raises(O.OpError) as ei:
        S.apply_ops(s, None, [{"op": "sketch_mirror_entities", "sketch": fid,
                               "entities": [P], "line": axis}])
    assert ei.value.code == "unsupported_entity", ei.value.code
    assert s.doc == doc_before
    print(f"[mirror-err] point -> {ei.value.code} (no mutation)")


# --------------------------------------------------------------------------- LINEAR PATTERN


def test_linear_pattern_x4():
    """Linear pattern of a circle, count 4, spacing 25 along +u: 3 new circles at u=25,50,75."""
    s, fid = _sketch_session()
    C = _add(s, fid, {"type": "circle", "center": [0, 0], "radius": 3.0})
    r = S.apply_ops(s, None, [{"op": "sketch_pattern_entities", "sketch": fid, "mode": "linear",
                               "entities": [C], "dir": [1, 0], "count": 4, "spacing": 25.0}])
    # 3 copies (count-1): $0, $0#1, $0#2.
    ids = [r["id_map"]["$0"], r["id_map"]["$0#1"], r["id_map"]["$0#2"]]
    ents = _ents(s, fid)
    centers = sorted(ents[i]["center"][0] for i in ids)
    assert centers == [25.0, 50.0, 75.0], centers
    for i in ids:
        assert abs(ents[i]["radius"] - 3.0) < 1e-9
    # total circles now 4 (original + 3 copies).
    circ = [e for e in ents.values() if e["type"] == "circle"]
    assert len(circ) == 4, len(circ)
    sol = solve_sketch(_solve_params(s, fid), {}, warm=None)
    assert sol.converged, sol.dof
    print(f"[pattern-lin] 4 instances at u={[0.0] + centers}")


def test_linear_pattern_diagonal_spacing():
    """Spacing is along the (normalized) dir vector — a diagonal dir steps by `spacing` exactly."""
    s, fid = _sketch_session()
    L = _add(s, fid, {"type": "line", "p1": [0, 0], "p2": [4, 0]})
    r = S.apply_ops(s, None, [{"op": "sketch_pattern_entities", "sketch": fid, "mode": "linear",
                               "entities": [L], "dir": [3, 4], "count": 2, "spacing": 10.0}])
    m = _ents(s, fid)[r["id_map"]["$0"]]
    # dir [3,4] normalized is [0.6,0.8]; step 10 -> (+6,+8).
    assert abs(m["p1"][0] - 6) < 1e-9 and abs(m["p1"][1] - 8) < 1e-9, m
    assert abs(m["p2"][0] - 10) < 1e-9 and abs(m["p2"][1] - 8) < 1e-9, m
    print(f"[pattern-lin-diag] copy p1={m['p1']}")


# --------------------------------------------------------------------------- CIRCULAR PATTERN


def test_circular_pattern_x6_equal_spacing():
    """Circular pattern of a circle, count 6, 360 deg equal spacing about the origin: copies sit on
    a 6-spoke ring at 60-deg increments, all at radius 30 from the center."""
    s, fid = _sketch_session()
    C = _add(s, fid, {"type": "circle", "center": [30, 0], "radius": 4.0})
    r = S.apply_ops(s, None, [{"op": "sketch_pattern_entities", "sketch": fid, "mode": "circular",
                               "entities": [C], "center": [0, 0], "count": 6, "angle": 360.0,
                               "equal_spacing": True}])
    ents = _ents(s, fid)
    circ = [e for e in ents.values() if e["type"] == "circle"]
    assert len(circ) == 6, len(circ)
    # every center is 30 from origin; angles are multiples of 60 deg.
    angs = sorted(round(math.degrees(math.atan2(e["center"][1], e["center"][0])) % 360, 4)
                  for e in circ)
    for e in circ:
        assert abs(math.hypot(*e["center"]) - 30.0) < 1e-6, e["center"]
    assert angs == [0.0, 60.0, 120.0, 180.0, 240.0, 300.0], angs
    sol = solve_sketch(_solve_params(s, fid), {}, warm=None)
    assert sol.converged, sol.dof
    print(f"[pattern-circ] 6 instances at angles {angs}")


def test_circular_pattern_step_angle():
    """Without equal_spacing, 'angle' is the step BETWEEN consecutive instances."""
    s, fid = _sketch_session()
    C = _add(s, fid, {"type": "circle", "center": [10, 0], "radius": 2.0})
    r = S.apply_ops(s, None, [{"op": "sketch_pattern_entities", "sketch": fid, "mode": "circular",
                               "entities": [C], "center": [0, 0], "count": 3, "angle": 30.0}])
    ents = _ents(s, fid)
    angs = sorted(round(math.degrees(math.atan2(e["center"][1], e["center"][0])) % 360, 4)
                  for e in ents.values() if e["type"] == "circle")
    assert angs == [0.0, 30.0, 60.0], angs
    print(f"[pattern-circ-step] angles {angs}")


def test_pattern_bad_count_clean_error():
    """count < 2 raises invalid_op with no mutation."""
    s, fid = _sketch_session()
    C = _add(s, fid, {"type": "circle", "center": [0, 0], "radius": 3.0})
    doc_before = copy.deepcopy(s.doc)
    with pytest.raises(O.OpError) as ei:
        S.apply_ops(s, None, [{"op": "sketch_pattern_entities", "sketch": fid, "mode": "linear",
                               "entities": [C], "dir": [1, 0], "count": 1, "spacing": 10.0}])
    assert ei.value.code == "invalid_op", ei.value.code
    assert s.doc == doc_before
    print(f"[pattern-err] count<2 -> {ei.value.code} (no mutation)")


# --------------------------------------------------------------------------- CONVERT


def _box_with_top_sketch():
    """A 80x40x10 box centered at the origin + an empty sketch on plane.top. Returns (s, fid)."""
    s = S.new_session(kind="part")
    S.apply_ops(s, None, [{"op": "add_feature", "feature": {
        "type": "box", "name": "Box1",
        "params": {"length": {"value": 80.0, "expr": None},
                   "width": {"value": 40.0, "expr": None},
                   "height": {"value": 10.0, "expr": None}}}}])
    r = S.apply_ops(s, None, [{"op": "add_feature", "feature": {
        "type": "sketch", "name": "Sketch1", "refs": {"plane": "plane.top"},
        "params": {"entities": [], "constraints": []}}}])
    return s, r["id_map"]["$0"]


def test_convert_box_edge_into_sketch():
    """Convert Entities: project a horizontal top-rim box edge into the top sketch -> a real line
    entity pinned with a fix relation. The projected line lies at the edge's v and is 80 long."""
    s, fid = _box_with_top_sketch()

    # find a linear box edge running along x (the top rim) via resolve_pick.
    S._ensure_built(s)
    esel = None
    topo = 1
    while True:
        try:
            rr = S.resolve_pick(s, None, 1, "edge", topo)
        except O.OpError:
            break
        sel = rr["selector"]
        if sel.get("surf") == "line" and sel.get("axis") and abs(abs(sel["axis"][0]) - 1.0) < 1e-6:
            esel = sel
            break
        topo += 1
    assert esel is not None, "no horizontal box edge found"

    ncon_before = len(_cons(s, fid))
    r = S.apply_ops(s, None, [{"op": "sketch_convert", "sketch": fid,
                               "refs": [{"kind": "edge", "selector": esel}]}])
    nid = r["id_map"]["$0"]
    e = _ents(s, fid)[nid]
    assert e["type"] == "line", e
    elen = math.hypot(e["p2"][0] - e["p1"][0], e["p2"][1] - e["p1"][1])
    assert abs(elen - 80.0) < 1e-4, elen
    # a fix relation pins the projected geometry whole.
    fixes = [c for c in _cons(s, fid) if c["type"] == "fix" and c["refs"] == [nid]]
    assert len(fixes) == 1, fixes
    assert len(_cons(s, fid)) == ncon_before + 1
    sol = solve_sketch(_solve_params(s, fid), {}, warm=None)
    assert sol.converged and not sol.conflicting, (sol.dof, sol.conflicting)
    print(f"[convert] edge -> line len={elen:.1f}, fixed; dof={sol.dof}")


def test_convert_datum_plane_into_sketch():
    """Convert a datum plane (front) into the top sketch -> its intersection line, pinned."""
    s, fid = _box_with_top_sketch()
    r = S.apply_ops(s, None, [{"op": "sketch_convert", "sketch": fid,
                               "refs": [{"datum": "front"}]}])
    nid = r["id_map"]["$0"]
    e = _ents(s, fid)[nid]
    assert e["type"] == "line", e
    # plane.front intersects plane.top in the u axis (v == 0).
    assert abs(e["p1"][1]) < 1e-6 and abs(e["p2"][1]) < 1e-6, e
    assert any(c["type"] == "fix" and c["refs"] == [nid] for c in _cons(s, fid))
    print(f"[convert-datum] front -> line v=0")


def test_convert_empty_refs_clean_error():
    """An empty refs list raises invalid_op with no mutation."""
    s, fid = _box_with_top_sketch()
    doc_before = copy.deepcopy(s.doc)
    with pytest.raises(O.OpError) as ei:
        S.apply_ops(s, None, [{"op": "sketch_convert", "sketch": fid, "refs": []}])
    assert ei.value.code == "invalid_op", ei.value.code
    assert s.doc == doc_before
    print(f"[convert-err] empty refs -> {ei.value.code} (no mutation)")
