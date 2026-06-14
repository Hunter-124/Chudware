"""Tests for sketch_move (C54 — SolidWorks "Move/Copy Entities").

Run from repo root with the dev venv:

    AI_CAD_REPO=... AI_CAD_PROVIDER=mock $py -X utf8 -m pytest engine/doc/test_sketch_move.py -q

sketch_move translates the selected entities by (dx, dy):
  - copy=false: IN PLACE — each entity's own geometry shifts, entity count unchanged, no new ids;
  - copy=true:  the originals are untouched and a translated copy of each is APPENDED (count doubles);
  - bad input (empty selection / unknown sketch / unsupported entity / non-numeric delta) -> a clean
    OpError with NO document mutation (atomic rejection).
"""

from __future__ import annotations

import copy

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


def _add(s, fid, entity):
    r = S.apply_ops(s, None, [{"op": "sketch_add_entity", "sketch": fid, "entity": entity}])
    return r["id_map"]["$0"]


def _two_lines(s, fid):
    """Add two lines and return their ids."""
    a = _add(s, fid, {"type": "line", "p1": [0, 0], "p2": [10, 0]})
    b = _add(s, fid, {"type": "line", "p1": [10, 0], "p2": [10, 10]})
    return a, b


# --------------------------------------------------------------------------- move in place


def test_move_in_place_shifts_lines():
    """copy=false: both lines shift by (10, 5), the entity count is unchanged, and it solves."""
    s, fid = _sketch_session()
    a, b = _two_lines(s, fid)
    before = _ents(s, fid)
    a0p1, a0p2 = list(before[a]["p1"]), list(before[a]["p2"])
    b0p1, b0p2 = list(before[b]["p1"]), list(before[b]["p2"])
    n_before = len(before)

    r = S.apply_ops(s, None, [{"op": "sketch_move", "sketch": fid,
                               "entities": [a, b], "dx": 10.0, "dy": 5.0, "copy": False}])
    # in-place returns the first moved id under $0 (no new entities).
    assert r["id_map"]["$0"] == a

    after = _ents(s, fid)
    assert len(after) == n_before, "in-place move must not change the entity count"
    assert after[a]["p1"] == [a0p1[0] + 10, a0p1[1] + 5]
    assert after[a]["p2"] == [a0p2[0] + 10, a0p2[1] + 5]
    assert after[b]["p1"] == [b0p1[0] + 10, b0p1[1] + 5]
    assert after[b]["p2"] == [b0p2[0] + 10, b0p2[1] + 5]

    sol = solve_sketch(_solve_params(s, fid), {}, warm=None)
    assert sol.converged, sol.dof
    print(f"[move-inplace] 2 lines shifted by (10,5); n={len(after)} dof={sol.dof}")


def test_move_in_place_circle_and_arc():
    """copy=false on a circle + arc shifts each entity's center by the delta (count unchanged)."""
    s, fid = _sketch_session()
    c = _add(s, fid, {"type": "circle", "center": [5, 5], "radius": 3.0})
    arc = _add(s, fid, {"type": "arc", "center": [20, 0], "radius": 10.0,
                        "start": 0.0, "end": 90.0, "ccw": True})
    n_before = len(_ents(s, fid))

    S.apply_ops(s, None, [{"op": "sketch_move", "sketch": fid,
                           "entities": [c, arc], "dx": -4.0, "dy": 7.0, "copy": False}])
    after = _ents(s, fid)
    assert len(after) == n_before
    assert after[c]["center"] == [1.0, 12.0] and after[c]["radius"] == 3.0
    assert after[arc]["center"] == [16.0, 7.0] and after[arc]["radius"] == 10.0
    print(f"[move-inplace-arc] circle+arc centers shifted by (-4,7)")


# --------------------------------------------------------------------------- copy


def test_move_copy_doubles_and_keeps_originals():
    """copy=true: the entity count doubles, originals are untouched, copies are shifted by (10,5)."""
    s, fid = _sketch_session()
    a, b = _two_lines(s, fid)
    before = _ents(s, fid)
    a0p1, a0p2 = list(before[a]["p1"]), list(before[a]["p2"])
    b0p1, b0p2 = list(before[b]["p1"]), list(before[b]["p2"])
    n_before = len(before)

    r = S.apply_ops(s, None, [{"op": "sketch_move", "sketch": fid,
                               "entities": [a, b], "dx": 10.0, "dy": 5.0, "copy": True}])
    after = _ents(s, fid)
    assert len(after) == 2 * n_before, "copy move must double the entity count"

    # Originals untouched.
    assert after[a]["p1"] == a0p1 and after[a]["p2"] == a0p2
    assert after[b]["p1"] == b0p1 and after[b]["p2"] == b0p2

    # Copies shifted (one per source, in order: $0, $0#1).
    ca, cb = r["id_map"]["$0"], r["id_map"]["$0#1"]
    assert ca not in (a, b) and cb not in (a, b)
    assert after[ca]["p1"] == [a0p1[0] + 10, a0p1[1] + 5]
    assert after[ca]["p2"] == [a0p2[0] + 10, a0p2[1] + 5]
    assert after[cb]["p1"] == [b0p1[0] + 10, b0p1[1] + 5]
    assert after[cb]["p2"] == [b0p2[0] + 10, b0p2[1] + 5]

    sol = solve_sketch(_solve_params(s, fid), {}, warm=None)
    assert sol.converged, sol.dof
    print(f"[move-copy] count {n_before} -> {len(after)}; copies={ca},{cb}")


def test_move_copy_defaults_to_false():
    """Omitting 'copy' moves in place (default false) — no new entities."""
    s, fid = _sketch_session()
    a = _add(s, fid, {"type": "line", "p1": [0, 0], "p2": [10, 0]})
    n_before = len(_ents(s, fid))
    S.apply_ops(s, None, [{"op": "sketch_move", "sketch": fid,
                           "entities": [a], "dx": 1.0, "dy": 2.0}])
    after = _ents(s, fid)
    assert len(after) == n_before
    assert after[a]["p1"] == [1.0, 2.0] and after[a]["p2"] == [11.0, 2.0]
    print("[move-default] omitted copy -> in place")


# --------------------------------------------------------------------------- error cases


def test_move_empty_selection_clean_error():
    """An empty selection raises invalid_op with no mutation."""
    s, fid = _sketch_session()
    _two_lines(s, fid)
    doc_before = copy.deepcopy(s.doc)
    rev = s.revision
    with pytest.raises(O.OpError) as ei:
        S.apply_ops(s, None, [{"op": "sketch_move", "sketch": fid,
                               "entities": [], "dx": 1.0, "dy": 1.0}])
    assert ei.value.code == "invalid_op", ei.value.code
    assert s.doc == doc_before and s.revision == rev
    print(f"[move-err] empty selection -> {ei.value.code} (no mutation)")


def test_move_bad_sketch_clean_error():
    """An unknown sketch id raises invalid_op with no mutation."""
    s, fid = _sketch_session()
    a = _add(s, fid, {"type": "line", "p1": [0, 0], "p2": [10, 0]})
    doc_before = copy.deepcopy(s.doc)
    with pytest.raises(O.OpError) as ei:
        S.apply_ops(s, None, [{"op": "sketch_move", "sketch": "f999",
                               "entities": [a], "dx": 1.0, "dy": 1.0}])
    assert ei.value.code == "invalid_op", ei.value.code
    assert s.doc == doc_before
    print(f"[move-err] bad sketch -> {ei.value.code} (no mutation)")


def test_move_bad_delta_clean_error():
    """A non-numeric delta raises invalid_op with no mutation."""
    s, fid = _sketch_session()
    a = _add(s, fid, {"type": "line", "p1": [0, 0], "p2": [10, 0]})
    doc_before = copy.deepcopy(s.doc)
    with pytest.raises(O.OpError) as ei:
        S.apply_ops(s, None, [{"op": "sketch_move", "sketch": fid,
                               "entities": [a], "dx": "nope", "dy": 1.0}])
    assert ei.value.code == "invalid_op", ei.value.code
    assert s.doc == doc_before
    print(f"[move-err] bad delta -> {ei.value.code} (no mutation)")


def test_move_unsupported_entity_clean_error():
    """Moving an unsupported entity (a point) raises unsupported_entity with no mutation."""
    s, fid = _sketch_session()
    p = _add(s, fid, {"type": "point", "p": [5, 5]})
    doc_before = copy.deepcopy(s.doc)
    with pytest.raises(O.OpError) as ei:
        S.apply_ops(s, None, [{"op": "sketch_move", "sketch": fid,
                               "entities": [p], "dx": 1.0, "dy": 1.0}])
    assert ei.value.code == "unsupported_entity", ei.value.code
    assert s.doc == doc_before
    print(f"[move-err] point -> {ei.value.code} (no mutation)")
