"""Copy/paste ops — Wave 4A (features) + 4B (sketch entities).

Run from repo root with the dev venv:

    & $py -X utf8 -m pytest engine/doc/test_copy_paste.py -q

Covers the four append-only ops added to engine/doc/ops.py:
  * copy_feature  — READ-ONLY: package a feature (+ its dependency closure) into a portable
    ``aicad.feature_bundle/1`` bundle returned via the id_map side-channel ($bundle).
  * paste_feature — re-id the bundle and append it; refs resolved against the target doc.
  * paste_feature_as_derived — paste + stamp a ``derived_from`` link on each pasted feature.
  * sketch_paste_entities — deep-copy sketch entities shifted by (dx,dy) with fresh ids, keeping
    internal constraints and dropping ones that reach outside the pasted set.

Assertions (per the Wave 4A/4B card):
  * same-doc copy -> paste round-trip yields an INDEPENDENT feature (fresh ids, identical volume,
    editing the original doesn't move the copy);
  * an internal coincident constraint survives the entity re-id;
  * a cross-doc edge selector is DETACHED (owner_feature=null) + flagged ``external``;
  * a cross-doc datum is reused by NAME in the target;
  * ``derived_from`` is stamped by paste_feature_as_derived;
  * sketch_paste_entities offsets geometry, keeps internal constraints, drops external ones;
  * a garbage bundle -> a clean ``invalid_op`` (atomic rejection, no mutation).
"""

from __future__ import annotations

import copy

import pytest

from engine.doc import ops as O
from engine.doc.compiler import compile_document


# --------------------------------------------------------------------------- fixtures


def _dim(value, expr=None):
    return {"value": value, "expr": expr}


def _doc(features):
    return {
        "schema": "aicad.part/1",
        "id": "doc-A",
        "name": "PartA",
        "units": {"system": "MMGS"},
        "material": None,
        "equations": {"globals": []},
        "features": features,
    }


def _rect_sketch(fid, width=20.0, height=20.0, with_coincident=True):
    """A closed 4-line rectangle sketch. With ``with_coincident``, a coincident relation ties the
    last line's end back to the first line's start (an INTERNAL relation among pasted entities)."""
    ents = [
        {"id": "e1", "type": "line", "construction": False, "p1": [0, 0], "p2": [width, 0]},
        {"id": "e2", "type": "line", "construction": False, "p1": [width, 0], "p2": [width, height]},
        {"id": "e3", "type": "line", "construction": False,
         "p1": [width, height], "p2": [0, height]},
        {"id": "e4", "type": "line", "construction": False, "p1": [0, height], "p2": [0, 0]},
    ]
    cons = []
    if with_coincident:
        cons.append({"id": "c1", "type": "coincident", "refs": ["e4.p2", "e1.p1"]})
    return {"id": fid, "type": "sketch", "name": fid, "suppressed": False,
            "params": {"entities": ents, "constraints": cons}, "refs": {"plane": "plane.top"}}


def _extrude(fid, sketch_fid, depth=10.0):
    # merge=False so a pasted copy at the same coordinates yields a SECOND body (rather than the
    # boolean-union OCCT would otherwise produce), letting the round-trip test count both volumes.
    return {"id": fid, "type": "extrude", "name": fid, "suppressed": False,
            "params": {"mode": "boss", "end": "blind", "depth": _dim(depth), "flip": False,
                       "draft": _dim(0.0), "draft_outward": False, "merge": False},
            "refs": {"profile": [{"kind": "region", "sketch": sketch_fid, "centroid": [10.0, 10.0],
                                  "area_or_len": 400.0, "hint_index": 0}]}}


def _part_sketch_extrude():
    """A part: f1 = rectangle sketch (with an internal coincident), f2 = blind extrude of it."""
    return _doc([_rect_sketch("f1"), _extrude("f2", "f1")])


def _vol(doc):
    res = compile_document(doc)
    return res.bodies[0].shape.volume


def _feat(doc, fid):
    return next(f for f in doc["features"] if f["id"] == fid)


# --------------------------------------------------------------------------- copy bundle shape


def test_copy_feature_is_read_only_and_bundles_closure():
    doc = _part_sketch_extrude()
    before = copy.deepcopy(doc)
    out, id_map = O.apply_ops(doc, [{"op": "copy_feature", "id": "f2"}])
    # READ-ONLY: the document is untouched.
    assert out["features"] == before["features"]
    bundle = id_map["$bundle"]
    assert bundle["kind"] == "aicad.feature_bundle/1"
    # closure = {f1 (sketch), f2 (extrude)} in dependency-first order; root = the copied f2.
    assert bundle["feature_order"] == ["$0", "$1"]
    assert bundle["root"] == "$1"
    assert bundle["features"]["$1"]["type"] == "extrude"
    assert bundle["features"]["$0"]["type"] == "sketch"
    # ids are stripped; the extrude's region ref now points at the sketch PLACEHOLDER.
    assert "id" not in bundle["features"]["$1"]
    assert bundle["features"]["$1"]["refs"]["profile"][0]["sketch"] == "$0"
    # sketch entity/constraint ids re-keyed to the bundle namespace.
    sk = bundle["features"]["$0"]
    assert [e["id"] for e in sk["params"]["entities"]] == ["~e0", "~e1", "~e2", "~e3"]
    assert sk["params"]["constraints"][0]["id"] == "~c0"
    assert sk["params"]["constraints"][0]["refs"] == ["~e3.p2", "~e0.p1"]
    assert bundle["external_refs"] == []
    # also exposed under the per-index key for multi-copy batches.
    assert id_map["$bundle:0"] is bundle


# --------------------------------------------------------------------------- round-trip + independence


def test_same_doc_paste_round_trip_independent_and_equal_volume():
    doc = _part_sketch_extrude()
    vol0 = _vol(doc)

    # The realistic host flow is two batches: copy_feature returns the bundle via the id_map
    # side-channel, then paste_feature consumes it (a bundle can't be threaded between ops in one
    # batch — inter-op placeholders are plain id strings, not nested objects).
    _, cm = O.apply_ops(doc, [{"op": "copy_feature", "id": "f2"}])
    bundle = cm["$bundle"]
    out, pm = O.apply_ops(doc, [{"op": "paste_feature", "bundle": bundle}])

    # Two new features appended (sketch + extrude); fresh, non-colliding ids.
    assert len(out["features"]) == 4
    root = pm["$0"]
    pasted_ids = [pm["$0#0"], pm["$0#1"]]
    assert root == pm["$0#1"]  # root (extrude) is last in dependency-first order
    assert set(pasted_ids).isdisjoint({"f1", "f2"})

    # The pasted extrude profiles the PASTED sketch, not the original.
    new_sketch, new_extrude = pasted_ids
    assert _feat(out, new_extrude)["refs"]["profile"][0]["sketch"] == new_sketch

    # Identical geometry: the copy is a second body of the same volume.
    res = compile_document(out)
    vols = sorted(b.shape.volume for b in res.bodies)
    assert len(res.bodies) == 2
    assert abs(vols[0] - vol0) / vol0 < 0.01
    assert abs(vols[1] - vol0) / vol0 < 0.01

    # INDEPENDENCE: edit the ORIGINAL sketch; the pasted sketch must not move.
    orig_e1 = _feat(out, "f1")["params"]["entities"][0]
    pasted_e1 = _feat(out, new_sketch)["params"]["entities"][0]
    before_pasted = copy.deepcopy(pasted_e1)
    out2, _ = O.apply_ops(out, [
        {"op": "sketch_edit_entity", "sketch": "f1", "id": orig_e1["id"],
         "params": {"p2": [50.0, 0.0]}}])
    assert _feat(out2, new_sketch)["params"]["entities"][0] == before_pasted


def test_internal_coincident_survives_reid():
    doc = _part_sketch_extrude()
    _, cm = O.apply_ops(doc, [{"op": "copy_feature", "id": "f1"}])
    out, pm = O.apply_ops(doc, [{"op": "paste_feature", "bundle": cm["$bundle"]}])
    new_sketch = pm["$0"]
    sk = _feat(out, new_sketch)
    cons = sk["params"]["constraints"]
    assert len(cons) == 1
    eids = {e["id"] for e in sk["params"]["entities"]}
    # The coincident's handles were re-pointed to the pasted entity ids (and still internal).
    assert cons[0]["type"] == "coincident"
    assert all(h.split(".")[0] in eids for h in cons[0]["refs"])


# --------------------------------------------------------------------------- cross-doc resolution


def _cross_doc_part():
    """A part whose ONLY feature is a fillet referencing an edge owned by 'f99' (a feature NOT in
    this doc) and a datum named 'MyPlane'. include_dependencies=false bundles just the fillet, so
    both refs are out-of-closure cross-doc references."""
    fillet = {"id": "f1", "type": "fillet", "name": "Fillet1", "suppressed": False,
              "params": {"radius": _dim(2.0)},
              "refs": {"edges": [{"kind": "edge", "surf": "line", "centroid": [1.0, 2.0, 3.0],
                                  "axis": None, "area_or_len": 5.0, "owner_feature": "f99",
                                  "hint_index": 0}],
                       "middle_face": {"kind": "datum", "feature": "f50"}}}
    return _doc([fillet])


def test_cross_doc_edge_selector_detached_and_flagged():
    src = _cross_doc_part()
    _, cm = O.apply_ops(src, [{"op": "copy_feature", "id": "f1", "include_dependencies": False}])
    bundle = cm["$bundle"]
    assert "f99" in bundle["external_refs"]

    # Paste into a DIFFERENT, empty doc.
    tgt = _doc([])
    out, pm = O.apply_ops(tgt, [{"op": "paste_feature", "bundle": bundle}])
    edge = _feat(out, pm["$0"])["refs"]["edges"][0]
    assert edge["owner_feature"] is None          # detached -> compiler dangles for re-pick
    assert edge["external"]["owner_feature"] == "f99"
    assert edge["external"]["doc_id"] == "doc-A"


def test_cross_doc_datum_reused_by_name():
    # Source fillet's middle_face datum is feature f50 named 'MyPlane'.
    src = _cross_doc_part()
    _feat(src, "f1")  # ensure structure
    # Add a datum f50 named 'MyPlane' so external_datums records the name.
    src["features"].insert(0, {"id": "f50", "type": "plane", "name": "MyPlane",
                               "suppressed": False, "params": {"method": "offset"},
                               "refs": {"base": "plane.top"}})
    _, cm = O.apply_ops(src, [{"op": "copy_feature", "id": "f1", "include_dependencies": False}])
    bundle = cm["$bundle"]
    assert bundle["external_datums"].get("f50") == "MyPlane"

    # Target has its OWN datum, different id, SAME name -> the paste must reuse it by name.
    tgt = _doc([{"id": "f7", "type": "plane", "name": "MyPlane", "suppressed": False,
                 "params": {"method": "offset"}, "refs": {"base": "plane.top"}}])
    out, pm = O.apply_ops(tgt, [{"op": "paste_feature", "bundle": bundle}])
    mf = _feat(out, pm["$0"])["refs"]["middle_face"]
    assert mf == {"kind": "datum", "feature": "f7"}

    # No same-named datum in target -> schema-valid downgrade flagged unresolved.
    tgt2 = _doc([])
    out2, pm2 = O.apply_ops(tgt2, [{"op": "paste_feature", "bundle": bundle}])
    mf2 = _feat(out2, pm2["$0"])["refs"]["middle_face"]
    assert mf2["feature"] == "plane.front"
    assert mf2["external"]["unresolved"] is True


# --------------------------------------------------------------------------- derived link


def test_paste_as_derived_stamps_link():
    doc = _part_sketch_extrude()
    _, cm = O.apply_ops(doc, [{"op": "copy_feature", "id": "f2"}])
    bundle = cm["$bundle"]
    out, pm = O.apply_ops(doc, [{"op": "paste_feature_as_derived", "bundle": bundle}])
    for k in ("$0#0", "$0#1"):
        feat = _feat(out, pm[k])
        assert feat["derived_from"]["doc_id"] == "doc-A"
        assert feat["derived_from"]["doc_name"] == "PartA"
        assert feat["derived_from"]["feature"] in bundle["feature_order"]


# --------------------------------------------------------------------------- sketch entity paste


def test_sketch_paste_entities_offsets_and_keeps_internal_drops_external():
    # A sketch with two lines + one extra "outside" line. The pasted bundle = the two lines and
    # TWO constraints: one internal (between the two pasted lines) and one external (to the third).
    doc = _doc([{"id": "f1", "type": "sketch", "name": "Sketch1", "suppressed": False,
                 "refs": {"plane": "plane.top"},
                 "params": {"entities": [
                     {"id": "e9", "type": "line", "construction": False, "p1": [0, 0], "p2": [5, 0]}],
                     "constraints": []}}])
    eb = {
        "entities": [
            {"id": "s1", "type": "line", "construction": False, "p1": [0, 0], "p2": [10, 0]},
            {"id": "s2", "type": "line", "construction": False, "p1": [10, 0], "p2": [10, 10]},
        ],
        "constraints": [
            {"id": "k1", "type": "coincident", "refs": ["s1.p2", "s2.p1"]},   # internal -> kept
            {"id": "k2", "type": "coincident", "refs": ["s1.p1", "sOUT.p1"]},  # external -> dropped
        ],
    }
    out, pm = O.apply_ops(doc, [
        {"op": "sketch_paste_entities", "sketch": "f1", "entities_bundle": eb,
         "offset_x": 100.0, "offset_y": 50.0}])
    sk = _feat(out, "f1")
    ents = {e["id"]: e for e in sk["params"]["entities"]}
    # original e9 untouched; two fresh entities appended.
    assert "e9" in ents and len(ents) == 3
    new_ids = [pm["$0"], pm["$0#1"]]
    a, b = ents[new_ids[0]], ents[new_ids[1]]
    # geometry shifted by (100, 50).
    assert a["p1"] == [100.0, 50.0] and a["p2"] == [110.0, 50.0]
    assert b["p1"] == [110.0, 50.0] and b["p2"] == [110.0, 60.0]
    # exactly the internal constraint survives, re-pointed to the new ids.
    cons = sk["params"]["constraints"]
    assert len(cons) == 1
    assert cons[0]["type"] == "coincident"
    assert {h.split(".")[0] for h in cons[0]["refs"]} == set(new_ids)


# --------------------------------------------------------------------------- error handling


def test_garbage_bundle_is_invalid_op():
    doc = _part_sketch_extrude()
    before = copy.deepcopy(doc)
    with pytest.raises(O.OpError) as ei:
        O.apply_ops(doc, [{"op": "paste_feature", "bundle": {"kind": "nope"}}])
    assert ei.value.code == "invalid_op"
    # atomic: the document is unchanged.
    assert doc["features"] == before["features"]

    for bad in ({}, {"bundle": 123}, {"bundle": {"kind": "aicad.feature_bundle/1"}}):
        with pytest.raises(O.OpError) as e2:
            O.apply_ops(doc, [{"op": "paste_feature", **bad}])
        assert e2.value.code == "invalid_op"
