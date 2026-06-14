"""Tests for sketch_fillet / sketch_chamfer ops (engine/doc/ops.py + sketch_geom.py).

Run from repo root with the dev venv:

    AI_CAD_REPO=... AI_CAD_PROVIDER=mock $py -m pytest engine/doc/test_sketch_fillet_chamfer.py -q

A fillet rounds a sketch corner (trim both lines + insert a tangent arc); a chamfer bevels it
(trim both + insert a connecting line). These tests build a constrained rectangle, fillet/chamfer
one corner, and assert:
  - the inserted geometry exists with the right type;
  - the trimmed segments shrank;
  - the corner stays closed (coincident joins solve to shared points);
  - the arc is tangent to both edges (fillet);
  - the sketch still solves (no conflicts);
  - radius/setback too large raises a clean OpError with NO mutation;
  - the filleted profile extrudes to a sane solid.
"""

from __future__ import annotations

import copy
import math

import pytest

from engine.doc import ops as O
from engine.doc import session as S
from engine.doc.sketch_solver import solve_sketch


# --------------------------------------------------------------------------- fixtures


def _rect_session():
    """A fully-constrained corner rectangle A(0,0) B(80,0) C(80,40) D(0,40), dof 0.

    Lines: L0 A->B (bottom), L1 B->C (right), L2 C->D (top), L3 D->A (left). Coincident corners,
    h/v relations, A fixed, and the size driven by an 80 dim on the BOTTOM edge (L0) + a 40 dim on
    the LEFT edge (L3). The top-right corner (L1/L2) carries NO length dim, so a fillet/chamfer
    there does not fight a stored edge-length dimension (the SolidWorks norm — you fillet a corner
    whose edges aren't both pinned to exact lengths). Returns (session, fid, [L0..L3]).
    """
    s = S.new_session(kind="part")
    r = S.apply_ops(s, None, [
        {"op": "add_feature", "feature": {"type": "sketch", "name": "Sketch1",
         "refs": {"plane": "plane.top"}, "params": {"entities": [], "constraints": []}}}])
    fid = r["id_map"]["$0"]
    corners = [(0, 0), (80, 0), (80, 40), (0, 40)]
    L = []
    for i in range(4):
        a, b = corners[i], corners[(i + 1) % 4]
        cs = []
        if L:
            cs.append({"type": "coincident", "refs": [f"{L[-1]}.p2", "$self.p1"]})
        rr = S.apply_ops(s, None, [{"op": "sketch_add_entity", "sketch": fid,
            "entity": {"type": "line", "p1": [a[0], a[1]], "p2": [b[0], b[1]]},
            "constraints": cs}])
        L.append(rr["id_map"]["$0"])
    L0, L1, L2, L3 = L
    S.apply_ops(s, None, [
        {"op": "sketch_add_constraint", "sketch": fid,
         "constraint": {"type": "coincident", "refs": [f"{L3}.p2", f"{L0}.p1"]}},
        {"op": "sketch_add_constraint", "sketch": fid, "constraint": {"type": "horizontal", "refs": [L0]}},
        {"op": "sketch_add_constraint", "sketch": fid, "constraint": {"type": "horizontal", "refs": [L2]}},
        {"op": "sketch_add_constraint", "sketch": fid, "constraint": {"type": "vertical", "refs": [L1]}},
        {"op": "sketch_add_constraint", "sketch": fid, "constraint": {"type": "vertical", "refs": [L3]}},
        {"op": "sketch_add_constraint", "sketch": fid, "constraint": {"type": "fix", "refs": [f"{L0}.p1"]}},
        {"op": "sketch_add_constraint", "sketch": fid, "constraint": {
            "type": "distance", "refs": [f"{L0}.p1", f"{L0}.p2"], "value": {"value": 80.0, "expr": None}}},
        {"op": "sketch_add_constraint", "sketch": fid, "constraint": {
            "type": "distance", "refs": [f"{L3}.p2", f"{L3}.p1"], "value": {"value": 40.0, "expr": None}}}])
    return s, fid, L


def _sketch_feat(s, fid):
    return next(f for f in s.doc["features"] if f["id"] == fid)


def _solve_params(s, fid):
    """Raw entities/constraints dict for solve_sketch."""
    feat = _sketch_feat(s, fid)
    return {"entities": feat["params"]["entities"],
            "constraints": feat["params"]["constraints"]}


def _len_line(e):
    return math.hypot(e["p2"][0] - e["p1"][0], e["p2"][1] - e["p1"][1])


def _dist_center_to_line(center, line):
    """Perpendicular distance from a point to the infinite line through line.p1/p2."""
    a = line["p1"]; b = line["p2"]
    dx, dy = b[0] - a[0], b[1] - a[1]
    L = math.hypot(dx, dy)
    cross = dx * (a[1] - center[1]) - dy * (a[0] - center[0])
    return abs(cross) / L


# --------------------------------------------------------------------------- fillet


def test_fillet_rounds_corner():
    """Fillet R6 at the top-right corner C (L1.p2 / L2.p1): an arc appears tangent to both edges,
    endpoints coincident with the trimmed line ends, segments shortened, loop closed, the radius
    is preserved (the corner's edges are not length-dimensioned), and the sketch solves."""
    s, fid, (L0, L1, L2, L3) = _rect_session()
    assert solve_sketch(_solve_params(s, fid), {}, warm=None).dof == 0, "fixture must start fully defined"

    before = {e["id"]: copy.deepcopy(e) for e in _sketch_feat(s, fid)["params"]["entities"]}
    r = S.apply_ops(s, None, [{"op": "sketch_fillet", "sketch": fid,
                               "e1": L1, "e2": L2, "radius": 6.0}])
    arc_id = r["id_map"]["$0"]

    feat = _sketch_feat(s, fid)
    ents = {e["id"]: e for e in feat["params"]["entities"]}

    # 1. the arc exists with the right shape.
    arc = ents[arc_id]
    assert arc["type"] == "arc", arc
    assert abs(arc["radius"] - 6.0) < 1e-6, arc

    # 2. the two segments were trimmed back (shorter than before).
    assert _len_line(ents[L1]) < _len_line(before[L1]) - 1e-6
    assert _len_line(ents[L2]) < _len_line(before[L2]) - 1e-6
    # right angle -> setback == radius == 6 along each edge.
    assert abs(_len_line(ents[L1]) - (40.0 - 6.0)) < 1e-4, _len_line(ents[L1])
    assert abs(_len_line(ents[L2]) - (80.0 - 6.0)) < 1e-4, _len_line(ents[L2])

    # 3. solve the mutated sketch: converges, no conflicts, radius preserved.
    sol = solve_sketch(_solve_params(s, fid), {}, warm=None)
    assert sol.converged, f"filleted sketch did not converge (dof={sol.dof})"
    assert not sol.conflicting, f"unexpected conflicts: {sol.conflicting}"

    se = sol.entities
    sarc = se[arc_id]
    assert abs(sarc["radius"] - 6.0) < 1e-4, f"fillet radius not preserved through solve: {sarc['radius']}"
    c = sarc["center"]
    # 4. tangent to both edges: perpendicular center-distance == radius.
    da = _dist_center_to_line(c, se[L1])
    db = _dist_center_to_line(c, se[L2])
    assert abs(da - sarc["radius"]) < 1e-4, f"arc not tangent to L1: dist={da}, r={sarc['radius']}"
    assert abs(db - sarc["radius"]) < 1e-4, f"arc not tangent to L2: dist={db}, r={sarc['radius']}"

    # 5. endpoints coincident: the arc start == L1's trimmed corner end, arc end == L2's.
    a_start = (c[0] + sarc["radius"] * math.cos(math.radians(sarc["start"])),
               c[1] + sarc["radius"] * math.sin(math.radians(sarc["start"])))
    a_end = (c[0] + sarc["radius"] * math.cos(math.radians(sarc["end"])),
             c[1] + sarc["radius"] * math.sin(math.radians(sarc["end"])))
    l1_end = se[L1]["p2"]   # corner at L1.p2
    l2_end = se[L2]["p1"]   # corner at L2.p1
    assert math.hypot(a_start[0] - l1_end[0], a_start[1] - l1_end[1]) < 1e-4, (a_start, l1_end)
    assert math.hypot(a_end[0] - l2_end[0], a_end[1] - l2_end[1]) < 1e-4, (a_end, l2_end)

    print(f"[fillet] arc r={sarc['radius']:.3f} tangent (d={da:.4f},{db:.4f}); "
          f"L1={_len_line(ents[L1]):.2f} L2={_len_line(ents[L2]):.2f}; dof={sol.dof}")


def test_fillet_radius_too_large_clean_error():
    """A radius larger than the corner can hold raises a clean OpError and mutates nothing."""
    s, fid, (L0, L1, L2, L3) = _rect_session()
    doc_before = copy.deepcopy(s.doc)
    rev_before = s.revision

    # The right edge is 40 and the top edge 80; a radius of 1000 cannot fit either setback.
    with pytest.raises(O.OpError) as ei:
        S.apply_ops(s, None, [{"op": "sketch_fillet", "sketch": fid,
                               "e1": L1, "e2": L2, "radius": 1000.0}])
    assert ei.value.code in ("fillet_failed", "invalid_op"), ei.value.code

    # No mutation: doc + revision unchanged (atomic op-batch rejection).
    assert s.doc == doc_before, "failed fillet must not mutate the document"
    assert s.revision == rev_before, "failed fillet must not bump the revision"
    print(f"[fillet-err] radius too large -> {ei.value.code} (no mutation)")


def test_fillet_unsupported_entity_rejected():
    """Filleting against a non-line entity (a circle) raises unsupported_entity cleanly."""
    s, fid, (L0, L1, L2, L3) = _rect_session()
    rr = S.apply_ops(s, None, [{"op": "sketch_add_entity", "sketch": fid,
        "entity": {"type": "circle", "center": [40, 20], "radius": 5.0}}])
    cid = rr["id_map"]["$0"]
    doc_before = copy.deepcopy(s.doc)
    with pytest.raises(O.OpError) as ei:
        S.apply_ops(s, None, [{"op": "sketch_fillet", "sketch": fid,
                               "e1": L0, "e2": cid, "radius": 3.0}])
    assert ei.value.code == "unsupported_entity", ei.value.code
    assert s.doc == doc_before
    print(f"[fillet-err] line-circle -> {ei.value.code} (no mutation)")


# --------------------------------------------------------------------------- chamfer


def test_chamfer_bevels_corner():
    """Chamfer (equal setback 5) at the top-right corner C: a line appears connecting the two
    trimmed ends, endpoints coincident, segments shortened, loop closed, sketch solves."""
    s, fid, (L0, L1, L2, L3) = _rect_session()
    before = {e["id"]: copy.deepcopy(e) for e in _sketch_feat(s, fid)["params"]["entities"]}
    r = S.apply_ops(s, None, [{"op": "sketch_chamfer", "sketch": fid,
                               "e1": L1, "e2": L2, "distance": 5.0}])
    line_id = r["id_map"]["$0"]

    feat = _sketch_feat(s, fid)
    ents = {e["id"]: e for e in feat["params"]["entities"]}
    bevel = ents[line_id]
    assert bevel["type"] == "line", bevel

    # both edges trimmed by 5.
    assert abs(_len_line(ents[L1]) - (40.0 - 5.0)) < 1e-4, _len_line(ents[L1])
    assert abs(_len_line(ents[L2]) - (80.0 - 5.0)) < 1e-4, _len_line(ents[L2])
    # bevel of a right angle with equal setback 5 has length 5*sqrt(2).
    assert abs(_len_line(bevel) - 5.0 * math.sqrt(2)) < 1e-4, _len_line(bevel)

    sol = solve_sketch(_solve_params(s, fid), {}, warm=None)
    assert sol.converged, f"chamfered sketch did not converge (dof={sol.dof})"
    assert not sol.conflicting, f"unexpected conflicts: {sol.conflicting}"

    se = sol.entities
    # coincident joins: bevel.p1 == L1.p2, bevel.p2 == L2.p1.
    assert math.hypot(se[line_id]["p1"][0] - se[L1]["p2"][0],
                      se[line_id]["p1"][1] - se[L1]["p2"][1]) < 1e-4
    assert math.hypot(se[line_id]["p2"][0] - se[L2]["p1"][0],
                      se[line_id]["p2"][1] - se[L2]["p1"][1]) < 1e-4
    print(f"[chamfer] bevel len={_len_line(bevel):.3f}; "
          f"L1={_len_line(ents[L1]):.2f} L2={_len_line(ents[L2]):.2f}; dof={sol.dof}")


def test_chamfer_unequal_setbacks():
    """Unequal chamfer (distance 6 on e1=L1, distance2 4 on e2=L2) trims each edge by its setback."""
    s, fid, (L0, L1, L2, L3) = _rect_session()
    r = S.apply_ops(s, None, [{"op": "sketch_chamfer", "sketch": fid,
                               "e1": L1, "e2": L2, "distance": 6.0, "distance2": 4.0}])
    line_id = r["id_map"]["$0"]
    ents = {e["id"]: e for e in _sketch_feat(s, fid)["params"]["entities"]}
    assert abs(_len_line(ents[L1]) - (40.0 - 6.0)) < 1e-4, _len_line(ents[L1])
    assert abs(_len_line(ents[L2]) - (80.0 - 4.0)) < 1e-4, _len_line(ents[L2])
    # bevel of a right angle with legs 6 and 4 -> hypotenuse sqrt(52).
    assert abs(_len_line(ents[line_id]) - math.hypot(6.0, 4.0)) < 1e-4, _len_line(ents[line_id])

    sol = solve_sketch(_solve_params(s, fid), {}, warm=None)
    assert sol.converged and not sol.conflicting, (sol.dof, sol.conflicting)
    print(f"[chamfer-uneq] L1={_len_line(ents[L1]):.2f} L2={_len_line(ents[L2]):.2f} "
          f"bevel={_len_line(ents[line_id]):.3f}")


def test_chamfer_setback_too_large_clean_error():
    """Setback larger than an edge raises a clean OpError and mutates nothing."""
    s, fid, (L0, L1, L2, L3) = _rect_session()
    doc_before = copy.deepcopy(s.doc)
    with pytest.raises(O.OpError) as ei:
        S.apply_ops(s, None, [{"op": "sketch_chamfer", "sketch": fid,
                               "e1": L1, "e2": L2, "distance": 500.0}])
    assert ei.value.code in ("chamfer_failed", "invalid_op"), ei.value.code
    assert s.doc == doc_before, "failed chamfer must not mutate the document"
    print(f"[chamfer-err] setback too large -> {ei.value.code} (no mutation)")


# --------------------------------------------------------------------------- downstream extrude


def test_filleted_profile_extrudes():
    """A filleted rectangle region extrudes to a sane solid (mirrors test_worker_sketch's region
    extrude). Volume ~= rect area minus the corner the fillet rounds off, times depth."""
    s, fid, (L0, L1, L2, L3) = _rect_session()
    S.apply_ops(s, None, [{"op": "sketch_fillet", "sketch": fid,
                           "e1": L1, "e2": L2, "radius": 3.0}])

    # whole-sketch region profile (extrude default) — depth 10.
    region_sel = {"kind": "region", "sketch": fid}
    r = S.apply_ops(s, None, [{"op": "add_feature", "feature": {
        "type": "extrude", "name": "Boss-Extrude1",
        "params": {"depth": {"value": 10.0, "expr": None}, "end": "blind", "mode": "boss"},
        "refs": {"profile": [region_sel]}}}])
    fstat = r["rebuild"]["feature_status"][-1]
    assert fstat["ok"], f"extrude of filleted profile failed: {fstat}"

    # rect area 80*40 = 3200; the R3 fillet removes a tiny corner notch (9 - 9pi/4 ~= 1.93 mm^2).
    # Volume ~ 3200*10 = 32000, slightly less. Bound it tightly enough to catch a broken profile.
    import engine.doc.evaluate as evaluate
    result = S._ensure_built(s)
    bodies = list(result.bodies)
    assert bodies, "filleted extrude produced no body"
    mp = evaluate.mass_props(bodies[-1].shape, 7.85)
    vol = mp["volume_mm3"]
    assert 31800.0 < vol < 32000.0, f"filleted extrude volume {vol} out of range"
    print(f"[fillet-extrude] volume={vol:.1f} (rect 32000 minus corner notch)")
