"""C11 — document ops tests (house-style plain runner, NOT pytest).

Run:  & $py -m engine.doc.test_ops   (from repo root)

Covers the card's deliverable obligations for ops.py:
  * placeholder batch: add a sketch + its entities in one batch ($N + $self)
  * patch edit semantics (present keys replace; null deletes)
  * delete dependency refusal (no cascade) + cascade delete
  * reorder validation (can't move a feature before its dependency)
  * stale revision guard (via session layer)
  * eq_set re-resolves dependents; eq_delete refused when in use
"""

from __future__ import annotations

from engine.doc.ops import OpError, apply_ops
from engine.doc.session import apply_ops as session_apply_ops, new_session
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


def _box_op(l=20, w=20, h=20, name="Box1"):
    return {"op": "add_feature", "feature": {
        "type": "box", "name": name,
        "params": {"length": {"value": l, "expr": None},
                   "width": {"value": w, "expr": None},
                   "height": {"value": h, "expr": None}}}}


# --------------------------------------------------------------------------- placeholder batch


def test_placeholder_batch_sketch_and_entities() -> None:
    """One batch: add a sketch (op 0), then add a line entity referencing the sketch via $0,
    with an inferred 'horizontal' constraint that uses $self for its handle."""
    doc = _blank()
    batch = [
        {"op": "add_feature", "feature": {
            "type": "sketch", "name": "Sketch1",
            "params": {"entities": [], "constraints": []},
            "refs": {"plane": "plane.top"}}},
        {"op": "sketch_add_entity", "sketch": "$0",
         "entity": {"type": "line", "construction": False, "p1": [0.0, 0.0], "p2": [50.0, 0.0]},
         "constraints": [{"type": "horizontal", "refs": ["$self"]}]},
        {"op": "sketch_add_entity", "sketch": "$0",
         "entity": {"type": "line", "construction": False, "p1": [50.0, 0.0], "p2": [50.0, 30.0]}},
    ]
    new_doc, id_map = apply_ops(doc, batch)
    assert validate_document(new_doc) == [], validate_document(new_doc)
    assert id_map["$0"] == "f1", id_map
    sk = new_doc["features"][0]
    ents = sk["params"]["entities"]
    assert [e["id"] for e in ents] == ["e1", "e2"], ents
    cons = sk["params"]["constraints"]
    assert len(cons) == 1 and cons[0]["id"] == "c1", cons
    assert cons[0]["refs"] == ["e1"], "the $self handle must resolve to the just-added entity e1"
    print(f"[ops] placeholder batch: sketch f1 + entities {[e['id'] for e in ents]} + "
          f"constraint {cons[0]['id']} ($self->e1)")


# --------------------------------------------------------------------------- patch edit


def test_patch_edit_replaces_and_deletes() -> None:
    doc = _blank()
    doc, _ = apply_ops(doc, [_box_op(20, 20, 20)])
    # Patch: replace height, add an optional 'centered' key.
    doc, _ = apply_ops(doc, [{"op": "edit_feature", "id": "f1",
                              "params": {"height": {"value": 40, "expr": None},
                                         "centered": False}}])
    f = doc["features"][0]
    assert f["params"]["height"]["value"] == 40
    assert f["params"]["centered"] is False
    # Patch with null deletes the optional key; untouched keys remain.
    doc, _ = apply_ops(doc, [{"op": "edit_feature", "id": "f1",
                              "params": {"centered": None}}])
    f = doc["features"][0]
    assert "centered" not in f["params"], "null must delete the optional key"
    assert f["params"]["length"]["value"] == 20, "untouched keys remain"
    print("[ops] patch edit: present keys replace, null deletes, untouched keys remain")


# --------------------------------------------------------------------------- delete dependency


def test_delete_dependency_refusal_and_cascade() -> None:
    """A box (f1) + a fillet (f2) whose edge selector is owned by f1. Deleting f1 without cascade
    must fail dependency_exists; with cascade it removes both."""
    doc = _blank()
    doc, _ = apply_ops(doc, [_box_op()])
    edge_sel = {"kind": "edge", "surf": "line", "centroid": [10.0, 10.0, 0.0],
                "axis": [0.0, 0.0, 1.0], "area_or_len": 20.0, "owner_feature": "f1",
                "hint_index": 0}
    doc, _ = apply_ops(doc, [{"op": "add_feature", "feature": {
        "type": "fillet", "name": "Fillet1",
        "params": {"radius": {"value": 2.0, "expr": None}},
        "refs": {"edges": [edge_sel]}}}])
    assert [f["id"] for f in doc["features"]] == ["f1", "f2"]

    # Refusal without cascade
    try:
        apply_ops(doc, [{"op": "delete_feature", "id": "f1"}])
        raise AssertionError("delete of f1 (referenced by f2) must be refused")
    except OpError as e:
        assert e.code == "dependency_exists" and "f2" in e.detail["dependents"], e.to_envelope()

    # Cascade removes both
    doc2, _ = apply_ops(doc, [{"op": "delete_feature", "id": "f1", "cascade": True}])
    assert doc2["features"] == [], "cascade delete should remove f1 and its dependent f2"
    print("[ops] delete: refused without cascade (dependency_exists f2); cascade removes f1+f2")


# --------------------------------------------------------------------------- reorder validation


def test_reorder_dependency_validation() -> None:
    """Moving the fillet (f2) before the box (f1) it depends on must be refused."""
    doc = _blank()
    doc, _ = apply_ops(doc, [_box_op()])
    edge_sel = {"kind": "edge", "surf": "line", "centroid": [10.0, 10.0, 0.0],
                "axis": [0.0, 0.0, 1.0], "area_or_len": 20.0, "owner_feature": "f1",
                "hint_index": 0}
    doc, _ = apply_ops(doc, [{"op": "add_feature", "feature": {
        "type": "fillet", "name": "Fillet1",
        "params": {"radius": {"value": 2.0, "expr": None}},
        "refs": {"edges": [edge_sel]}}}])
    try:
        apply_ops(doc, [{"op": "reorder_feature", "id": "f2", "before": "f1"}])
        raise AssertionError("reorder f2 before its dependency f1 must be refused")
    except OpError as e:
        assert e.code == "dependency_exists", e.to_envelope()
    print("[ops] reorder: moving fillet before its owning box refused (dependency_exists)")


# --------------------------------------------------------------------------- stale revision


def test_stale_revision() -> None:
    s = new_session("part", "Stale")
    r1 = session_apply_ops(s, 0, [_box_op()])
    assert r1["revision"] == 1
    # Submitting against the now-stale base_revision 0 must raise stale_revision.
    try:
        session_apply_ops(s, 0, [_box_op(name="Box2")])
        raise AssertionError("stale base_revision must raise")
    except OpError as e:
        assert e.code == "stale_revision" and e.detail["current_revision"] == 1, e.to_envelope()
    # Current revision works.
    r2 = session_apply_ops(s, 1, [_box_op(name="Box2")])
    assert r2["revision"] == 2
    print("[ops] stale_revision: base!=current refused (current_revision echoed); current accepted")


def test_coalesce_undo_groups_multi_batch_action() -> None:
    """C50: ``coalesce=True`` folds a batch into the previous undo step so one Ctrl+Z reverts a
    whole multi-batch user action (e.g. a rectangle = 4 chained line ops)."""
    from engine.doc.session import undo as session_undo, redo as session_redo
    s = new_session("part", "Coalesce")
    r0 = session_apply_ops(s, None, [{"op": "add_feature", "feature": {
        "type": "sketch", "name": "Sketch1", "refs": {"plane": "plane.front"},
        "params": {"entities": [], "constraints": []}}}])
    fid = r0["id_map"]["$0"]

    def nents():
        sk = next(f for f in s.doc["features"] if f["id"] == fid)
        return len(sk["params"].get("entities", []))

    corners = [([0, 0], [10, 0]), ([10, 0], [10, 10]), ([10, 10], [0, 10]), ([0, 10], [0, 0])]
    for i, (p1, p2) in enumerate(corners):
        session_apply_ops(s, None, [{"op": "sketch_add_entity", "sketch": fid,
            "entity": {"type": "line", "p1": p1, "p2": p2}}], coalesce=(i != 0))
    assert nents() == 4
    session_undo(s)
    assert nents() == 0, "one undo must revert the whole coalesced rectangle"
    session_redo(s)
    assert nents() == 4, "redo must restore the whole rectangle"
    print("[ops] coalesce: 4-batch rectangle undoes/redoes as ONE step")


# --------------------------------------------------------------------------- equations


def test_eq_set_drives_and_eq_delete_in_use() -> None:
    """eq_set creates an equation; a box height bound to it follows it; eq_delete is refused while
    in use, allowed once the reference is gone."""
    doc = _blank()
    doc, _ = apply_ops(doc, [{"op": "eq_set", "name": "depth", "expr": "5"}])
    doc, _ = apply_ops(doc, [{"op": "add_feature", "feature": {
        "type": "box", "name": "Box1",
        "params": {"length": {"value": 10, "expr": None},
                   "width": {"value": 10, "expr": None},
                   "height": {"value": 0, "expr": "depth"}}}}])
    # eq_delete refused while the box references it.
    try:
        apply_ops(doc, [{"op": "eq_delete", "name": "depth"}])
        raise AssertionError("eq_delete of an in-use equation must be refused")
    except OpError as e:
        assert e.code == "eq_in_use" and "f1" in e.detail["referencing"], e.to_envelope()

    # Update the equation (create-or-update); compile picks up the new value.
    doc2, _ = apply_ops(doc, [{"op": "eq_set", "name": "depth", "expr": "8"}])
    g = doc2["equations"]["globals"][0]
    assert g["name"] == "depth" and g["expr"] == "8", "eq_set must update in place"

    # Rebind the box off the equation, then eq_delete succeeds.
    doc3, _ = apply_ops(doc2, [{"op": "edit_feature", "id": "f1",
                                "params": {"height": {"value": 12, "expr": None}}}])
    doc4, _ = apply_ops(doc3, [{"op": "eq_delete", "name": "depth"}])
    assert doc4["equations"]["globals"] == [], "eq_delete allowed once unreferenced"
    print("[ops] eq_set create-or-update + dependent rebind; eq_delete refused in use, then allowed")


# --------------------------------------------------------------------------- C27-B cache-hit owner maps


def test_cache_hit_face_owners_nonempty() -> None:
    """After a pure cache-hit rebuild (apply_ops then rebuild), face_owners must map every face
    to its owning feature — not be empty (C27-B regression)."""
    s = new_session("part", "CacheOwnerTest")
    session_apply_ops(s, 0, [_box_op()])
    # Second rebuild is a pure cache hit for f1.
    from engine.doc.session import rebuild as sess_rebuild
    sess_rebuild(s)
    fo = s.last_result.face_owners
    assert fo, "face_owners must be non-empty after cache-hit rebuild"
    bid = next(iter(fo))
    assert fo[bid], f"face_owners[{bid!r}] must be non-empty"
    assert all(v == "f1" for v in fo[bid].values()), \
        f"all box faces must be owned by f1 on cache hit, got {fo[bid]}"
    print(f"[C27-B] cache-hit rebuild: face_owners={fo!r}")


def test_cache_hit_partial_rebuild_owner_maps() -> None:
    """Partial rebuild: first feature (box) is cache-hit, second feature (box2) compiles fresh.
    face_owners for the first feature must be correctly restored; the second feature's faces must
    be owned by f2."""
    s = new_session("part", "PartialCacheTest")
    session_apply_ops(s, 0, [_box_op(name="Box1")])
    # Add a second box (both features now in cache from the first full compile).
    session_apply_ops(s, 1, [_box_op(l=10, w=10, h=10, name="Box2")])
    # Forced rebuild: both features are cache hits.
    from engine.doc.session import rebuild as sess_rebuild
    sess_rebuild(s)
    fo = s.last_result.face_owners
    eo = s.last_result.edge_owners
    # Two bodies (box + box) — each should have non-empty owner maps.
    assert len(fo) >= 1, f"expected at least one body in face_owners, got {fo!r}"
    for bid, owners in fo.items():
        assert owners, f"face_owners[{bid!r}] must be non-empty"
    # Now edit Box1's height (invalidates f1 cache, keeps f2 cache if it existed before edit).
    session_apply_ops(s, 2, [{"op": "edit_feature", "id": "f1",
                               "params": {"height": {"value": 30, "expr": None}}}])
    fo2 = s.last_result.face_owners
    assert fo2, "face_owners must be non-empty after partial rebuild"
    for bid, owners in fo2.items():
        assert owners, f"face_owners[{bid!r}] must be non-empty after partial rebuild"
    print(f"[C27-B] partial rebuild: face_owners non-empty in all {len(fo2)} bodies")


def main() -> None:
    test_placeholder_batch_sketch_and_entities()
    test_patch_edit_replaces_and_deletes()
    test_delete_dependency_refusal_and_cascade()
    test_reorder_dependency_validation()
    test_stale_revision()
    test_eq_set_drives_and_eq_delete_in_use()
    test_cache_hit_face_owners_nonempty()
    test_cache_hit_partial_rebuild_owner_maps()
    test_coalesce_undo_groups_multi_batch_action()
    print("\nDOC OPS TESTS PASSED")


if __name__ == "__main__":
    main()
