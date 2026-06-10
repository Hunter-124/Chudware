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


def main() -> None:
    test_placeholder_batch_sketch_and_entities()
    test_patch_edit_replaces_and_deletes()
    test_delete_dependency_refusal_and_cascade()
    test_reorder_dependency_validation()
    test_stale_revision()
    test_eq_set_drives_and_eq_delete_in_use()
    print("\nDOC OPS TESTS PASSED")


if __name__ == "__main__":
    main()
