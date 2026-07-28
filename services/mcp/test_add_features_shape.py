"""add_features ARGUMENT-SHAPE + validation-miss normalizer tests.

Every case here is a real call shape mined from the recorded agent-run ledgers under
``tools/flywheel/bench/gym/reports/runs/**`` — the ways frontier models actually phrase a
build call, each of which used to burn a whole turn on a rejection. The normalizers are
pure functions, so most of this runs without an engine; the end-to-end cases go through
``services.agent.agent_turn._bind_tool`` (the BUILT-IN agent's dispatch path, which calls
the tool function directly and bypasses FastMCP) and through FastMCP itself.

Run:  python3 -m pytest services/mcp/test_add_features_shape.py -q -p no:randomly
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

_REPO_ROOT = str(Path(__file__).resolve().parents[2])
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from services.mcp import cad_doc_tools as M  # noqa: E402

_CANON_BOX = {"op": "add_feature", "feature": {
    "type": "box", "name": "Block",
    "params": {"length": 40, "width": 30, "height": 20}}}


def _args(ops=None, doc_id=None, **extra):
    """Run the argument-shape normalizer -> (ops, doc_id, background, notes)."""
    notes: list = []
    got_ops, got_doc, bg = M._normalize_add_features_args(ops, doc_id, extra, notes)
    return got_ops, got_doc, bg, notes


# --------------------------------------------------------------------- argument shapes

def test_canonical_ops_pass_through_untouched():
    ops, doc, bg, notes = _args(ops=[_CANON_BOX], doc_id="doc-1")
    assert ops == [_CANON_BOX]
    assert (doc, bg, notes) == ("doc-1", None, [])


def test_single_dict_ops_becomes_one_element_batch():
    ops, _doc, _bg, notes = _args(ops=_CANON_BOX)
    assert ops == [_CANON_BOX]
    assert notes  # the rewrite is echoed, never silent


def test_json_string_ops_is_parsed():
    ops, _doc, _bg, notes = _args(ops=json.dumps([_CANON_BOX]))
    assert ops == [_CANON_BOX]
    assert notes


def test_python_repr_ops_string_is_read_as_a_literal():
    ops, _doc, _bg, _notes = _args(ops="[{'op': 'add_feature', 'feature': {'type': 'box'}}]")
    assert ops == [{"op": "add_feature", "feature": {"type": "box"}}]


def test_unparseable_ops_string_teaches_the_canonical_array():
    with pytest.raises(ValueError) as ei:
        _args(ops="[['extrude', '50mm'], ['cut', '0mm'")
    msg = str(ei.value)
    assert "neither JSON nor a Python literal" in msg
    assert '"op":"add_feature"' in msg


def test_non_container_ops_names_the_accepted_forms():
    with pytest.raises(ValueError) as ei:
        _args(ops=42)
    assert "ops must be a list of ops" in str(ei.value)
    assert "add_features accepts" in str(ei.value)


@pytest.mark.parametrize("alias", ["feature", "features", "op", "ops_json", "operations",
                                   "actions"])
def test_every_batch_alias_is_accepted(alias):
    ops, _doc, _bg, notes = _args(**{alias: [_CANON_BOX]})
    assert ops == [_CANON_BOX]
    assert any(alias in n for n in notes)


@pytest.mark.parametrize("value", [_CANON_BOX, json.dumps([_CANON_BOX])])
def test_batch_alias_accepts_dict_and_json_string(value):
    ops, _doc, _bg, _notes = _args(features=value)
    assert ops == [_CANON_BOX]


def test_conflicting_batches_are_a_teaching_error():
    other = {"op": "add_feature", "feature": {"type": "sphere", "name": "Ball",
                                              "params": {"radius": 5}}}
    with pytest.raises(ValueError) as ei:
        _args(ops=[_CANON_BOX], features=[other])
    msg = str(ei.value)
    assert "DIFFERENT op batches" in msg and "ops" in msg and "features" in msg


def test_identical_duplicate_batches_are_accepted_once():
    ops, _doc, _bg, notes = _args(ops=[_CANON_BOX], features=[_CANON_BOX])
    assert ops == [_CANON_BOX]
    assert any("duplicate" in n for n in notes)


def test_bare_feature_spec_is_wrapped_as_one_add_feature():
    ops, _doc, _bg, notes = _args(type="box", name="Plate",
                                  params={"length": 40, "width": 30, "height": 10})
    assert ops == [{"op": "add_feature", "feature": {
        "type": "box", "name": "Plate",
        "params": {"length": 40, "width": 30, "height": 10}}}]
    assert any("bare feature spec" in n for n in notes)


def test_bare_feature_spec_folds_loose_geometry_kwargs_into_params():
    # The real ledger call: one feature spread flat, mixing geometry with design intent.
    ops, _doc, _bg, notes = _args(feature="bore", type="cylindrical", diameter=20.042,
                                  location="center", purpose="sliding fit h7",
                                  clearance=0.042, basis="acceptance criteria AC1")
    feat = ops[0]["feature"]
    assert ops[0]["op"] == "add_feature"
    assert feat["name"] == "bore"                      # feature= was the NAME, not a batch
    assert feat["params"]["diameter"] == 20.042        # geometry survives
    assert feat["params"]["location"] == "center"
    text = " ".join(notes)
    assert "purpose" in text and "basis" in text and "clearance" in text
    assert "commentary" in text                        # intent accepted + ignored, not fatal


@pytest.mark.parametrize("key", ["name", "identifier"])
def test_a_name_beside_a_type_is_the_feature_name(key):
    ops, doc, _bg, _notes = _args(**{key: "bore", "type": "cylinder",
                                     "params": {"radius": 10, "height": 40}})
    assert ops[0]["feature"]["name"] == "bore"
    assert doc is None                                  # not a document handle


def test_bare_op_dict_is_wrapped():
    ops, _doc, _bg, notes = _args(op="delete_feature", id="f2")
    assert ops == [{"op": "delete_feature", "id": "f2"}]
    assert notes


def test_feature_type_used_as_the_op_name_becomes_add_feature():
    ops, _doc, _bg, _notes = _args(operation="box3D",
                                   dimension=[{"value": 40, "expr": None},
                                              {"value": 30, "expr": None},
                                              {"value": 20, "expr": None}])
    assert ops[0]["op"] == "add_feature"
    assert ops[0]["feature"]["type"] == "box"


@pytest.mark.parametrize("key", ["identifier", "part_name", "part", "document", "name"])
def test_a_lone_document_word_is_routed_to_doc_id(key):
    ops, doc, _bg, notes = _args(ops=[_CANON_BOX], **{key: "flanged_bushing"})
    assert ops == [_CANON_BOX]
    assert doc == "flanged_bushing"
    assert any("doc_id" in n for n in notes)


def test_a_document_word_that_contradicts_doc_id_is_an_error():
    with pytest.raises(ValueError) as ei:
        _args(ops=[_CANON_BOX], doc_id="doc-cpp1", part_name="other_part")
    assert "different documents" in str(ei.value)


def test_unknown_leftover_kwarg_is_a_teaching_error_naming_the_parameters():
    with pytest.raises(ValueError) as ei:
        _args(parameters={"diameter": 80, "length": 22})
    msg = str(ei.value)
    assert "cannot place: parameters" in msg
    for token in ("ops=", "doc_id=", "background=", "features=", "ops_json="):
        assert token.rstrip("=") in msg


def test_no_arguments_at_all_is_a_teaching_error():
    with pytest.raises(ValueError) as ei:
        _args()
    assert "called with no ops" in str(ei.value)
    assert "add_features accepts" in str(ei.value)


# --------------------------------------------------------------------- op elements

def test_bare_feature_dicts_in_ops_get_an_op_once_per_batch():
    notes: list = []
    ops = M._normalize_op_elements(
        [{"type": "box", "name": "Plate", "params": {"length": 1}},
         {"type": "hole", "name": "H", "params": {"diameter": 3}}], notes)
    assert [o["op"] for o in ops] == ["add_feature", "add_feature"]
    assert ops[0]["feature"]["name"] == "Plate"
    assert len(M._interpreted_notes(notes)) == 1  # one note for the whole batch


def test_an_element_with_neither_op_nor_type_teaches_both_shapes():
    with pytest.raises(ValueError) as ei:
        M._normalize_op_elements([{"name": "Plate"}], [])
    msg = str(ei.value)
    assert 'ops[0] has no "op"' in msg and "bare feature" in msg


def test_a_positional_pair_element_is_named_as_such():
    with pytest.raises(ValueError) as ei:
        M._normalize_op_elements([["extrude", "50mm"]], [])
    assert "pair is not an op" in str(ei.value)


def test_delete_feature_accepts_the_feature_id_spelling():
    notes: list = []
    ops = M._normalize_op_elements([{"op": "delete_feature", "feature_id": "f2"}], notes)
    assert ops == [{"op": "delete_feature", "id": "f2"}]
    assert notes


# --------------------------------------------------------------------- feature types

def test_feature_type_aliases_map_onto_the_engine_vocabulary():
    vocab = frozenset({"box", "cylinder", "sphere", "hole", "extrude"})
    for word, want in (("cube", "box"), ("cuboid", "box"), ("cylindrical", "cylinder"),
                       ("ball", "sphere"), ("box3D", "box")):
        notes: list = []
        out = M._retype_feature({"type": word, "name": "X"}, vocab, True, notes)
        assert out["type"] == want, word
        assert notes


def test_cut_prefixed_type_becomes_the_shape_with_cut_mode():
    notes: list = []
    out = M._retype_feature({"type": "cut_cylinder", "name": "Bore"},
                            frozenset({"cylinder"}), True, notes)
    assert out["type"] == "cylinder" and out["mode"] == "cut"
    assert 'mode:"cut"' in " ".join(notes)


def test_a_boolean_word_used_as_a_type_points_at_mode_cut():
    with pytest.raises(ValueError) as ei:
        M._retype_feature({"type": "subtract"}, frozenset({"box"}), True, [])
    msg = str(ei.value)
    assert "boolean OPERATION, not a feature type" in msg and 'mode:"cut"' in msg


def test_unknown_type_error_lists_closest_matches_first():
    vocab = frozenset({"box", "cylinder", "sphere", "cone", "shell"})
    msg = M._feature_type_error("cylnder", vocab)
    assert "is not a feature type" in msg
    assert msg.index("Closest:") < msg.index("Feature types:")
    assert "cylinder" in msg.split("Feature types:")[0]


def test_an_offline_vocabulary_never_rejects_an_unknown_type():
    # The fallback list is a SUBSET of the engine's, so only a LIVE vocabulary may reject.
    feat = {"type": "weld_gusset", "name": "G"}
    assert M._retype_feature(feat, frozenset({"box"}), False, []) is feat


# --------------------------------------------------------------------- hole schema

def test_hole_style_and_end_are_defaulted_and_echoed():
    notes: list = []
    out = M._normalize_hole_feature(
        {"type": "hole", "name": "Bore", "params": {"diameter": 8}}, notes)
    assert out["params"]["style"] == "simple"
    assert out["params"]["end"] == "through_all"
    text = " ".join(notes)
    assert "params.style is required" in text and "params.end is required" in text


def test_hole_with_a_depth_defaults_to_a_blind_end():
    out = M._normalize_hole_feature(
        {"type": "hole", "name": "B", "params": {"diameter": 5, "depth": 10}}, [])
    assert out["params"]["end"] == "blind"


@pytest.mark.parametrize("word,want", [("counterbore", "cbore"), ("c-bore", "cbore"),
                                       ("cbore", "cbore"), ("countersink", "csink"),
                                       ("csk", "csink"), ("tapped", "tap")])
def test_hole_style_synonyms_map_to_the_engine_enum(word, want):
    out = M._normalize_hole_feature(
        {"type": "hole", "name": "B", "params": {"diameter": 5, "style": word,
                                                 "end": "through"}}, [])
    assert out["params"]["style"] == want


def test_a_hole_with_no_face_ref_is_given_the_top_face():
    notes: list = []
    ops = M._supply_missing_refs([{"op": "add_feature", "feature": {
        "type": "hole", "name": "Bore",
        "params": {"diameter": 8, "style": "simple", "end": "through_all"}}}], notes)
    assert ops[0]["feature"]["refs"]["face"] == {"of": "face", "side": "top"}
    assert any("no refs.face given" in n for n in notes)


def test_supplying_a_face_never_overrides_one_the_model_named():
    op = {"op": "add_feature", "feature": {
        "type": "hole", "name": "Bore", "params": {"diameter": 8},
        "refs": {"face": {"of": "face", "side": "bottom"}}}}
    notes: list = []
    assert M._supply_missing_refs([op], notes) == [op]
    assert notes == []


def test_an_end_condition_written_into_the_depth_slot_is_moved():
    notes: list = []
    out = M._normalize_hole_feature(
        {"type": "hole", "name": "B", "params": {"diameter": 20, "depth": "through"}}, notes)
    assert out["params"]["end"] == "through_all"
    assert "depth" not in out["params"]
    assert any("end condition, not a distance" in n for n in notes)


def test_a_hole_direction_picks_the_entry_face():
    out = M._normalize_hole_feature(
        {"type": "hole", "name": "B",
         "params": {"diameter": 5, "style": "simple", "end": "through_all",
                    "direction": [1, 0, 0]}}, [])
    assert out["params"]["end"] == "through_all"
    assert out["refs"]["face"] == {"of": "face", "side": "right"}
    assert "direction" not in out["params"]


def test_a_blind_hole_without_a_depth_teaches_the_fix():
    with pytest.raises(ValueError) as ei:
        M._normalize_hole_feature(
            {"type": "hole", "name": "B", "params": {"diameter": 5, "end": "blind"}}, [])
    assert 'end:"blind" needs a numeric params.depth' in str(ei.value)


def test_a_canonical_hole_is_left_byte_identical():
    feat = {"type": "hole", "name": "B",
            "params": {"diameter": 5, "style": "simple", "end": "through_all"},
            "refs": {"face": {"of": "face", "side": "top"}}}
    notes: list = []
    assert M._normalize_hole_feature(feat, notes) is feat
    assert notes == []


# --------------------------------------------------------------------- sketch schema

def test_sketch_entity_ids_are_renumbered_and_every_reference_follows():
    feat = {"type": "sketch", "name": "S", "params": {
        "plane": "XZ",
        "entities": [{"id": "L1", "type": "line", "start": [0, 0], "end": [10, 0]},
                     {"id": "L2", "type": "line", "start": [10, 0], "end": [10, 5]}],
        "constraints": [{"type": "coincident", "entities": ["L1.p2", "L2.p1"]}],
        "dimensions": [{"type": "distance", "entities": ["L1.p1", "L1.p2"], "value": 10}]}}
    out = M._normalize_sketch_feature(feat)
    ents = out["params"]["entities"]
    assert [e["id"] for e in ents] == ["e1", "e2"]
    assert all(e["construction"] is False for e in ents)
    assert ents[0]["p1"] == [0, 0] and ents[0]["p2"] == [10, 0]
    cons = out["params"]["constraints"]
    assert cons[0]["refs"] == ["e1.p2", "e2.p1"]        # constraint handles remapped
    assert cons[1]["refs"] == ["e1.p1", "e1.p2"]        # and so are dimension handles
    assert cons[1]["value"] == {"value": 10, "expr": None}
    assert out["refs"]["plane"] == "plane.front"        # "XZ" -> the reserved datum


def test_a_named_plane_on_an_otherwise_canonical_sketch_is_normalized():
    # The gap: every entity was already canonical, so the sketch pass never ran and the
    # batch died on the dangling-ref guardrail ("Missing: XZ").
    feat = {"type": "sketch", "name": "S", "refs": {"plane": "XZ"}, "params": {
        "entities": [{"id": "e0", "type": "circle", "construction": False,
                      "center": [0, 0], "radius": 22}]}}
    assert M._sketch_needs_norm(feat, feat["params"]) is True
    out = M._normalize_sketch_feature(feat)
    assert out["refs"]["plane"] == "plane.front"
    assert out["params"]["constraints"] == []           # required for every sketch


def test_a_fully_canonical_sketch_is_left_alone():
    feat = {"type": "sketch", "name": "S", "refs": {"plane": "plane.top"}, "params": {
        "entities": [{"id": "e1", "type": "circle", "construction": False,
                      "center": [0, 0], "radius": 5}],
        "constraints": []}}
    assert M._normalize_sketch_feature(feat) is feat


class TestSketchEntityShorthand:
    """Flat entity shorthand a model plausibly writes instead of the engine's canonical
    fields — the live 2026-07-28 payload ({"type":"circle","cx":-23,"cy":0,"r":2.5}, a
    29-circle keychain-hole lattice) used to SIGSEGV the worker; with the crash fixed it
    just became a clean "center: required for circle" rejection instead of a build.
    """

    def _norm(self, entities, notes=None):
        sink = notes if notes is not None else []
        out = M._normalize_sketch_feature(
            {"type": "sketch", "name": "S", "params": {"entities": entities}}, sink)
        return out["params"]["entities"], sink

    def test_circle_cx_cy_r_becomes_center_and_radius(self):
        ents, notes = self._norm([{"type": "circle", "cx": -23, "cy": 0, "r": 2.5}])
        assert ents[0]["center"] == [-23, 0]
        assert ents[0]["radius"] == 2.5
        assert "cx" not in ents[0] and "cy" not in ents[0] and "r" not in ents[0]
        assert any("cx/cy -> center" in n and "r -> radius" in n for n in notes)

    def test_a_diameter_key_is_halved_into_radius(self):
        for key in ("d", "dia", "diameter"):
            ents, notes = self._norm([{"type": "circle", "cx": 0, "cy": 0, key: 10}])
            assert ents[0]["radius"] == 5
            assert any("halved" in n for n in notes), key

    def test_agreeing_radius_and_diameter_resolve(self):
        ents, _ = self._norm([{"type": "circle", "cx": 0, "cy": 0, "r": 5, "d": 10}])
        assert ents[0]["radius"] == 5

    def test_disagreeing_radius_and_diameter_is_left_for_the_engine(self):
        ents, _ = self._norm([{"type": "circle", "cx": 0, "cy": 0, "r": 5, "d": 999}])
        assert "radius" not in ents[0]
        assert ents[0]["r"] == 5 and ents[0]["d"] == 999

    def test_a_lone_cx_with_no_cy_is_not_guessed(self):
        ents, _ = self._norm([{"type": "circle", "cx": 0, "r": 5}])
        assert "center" not in ents[0]
        assert ents[0]["cx"] == 0

    def test_arc_gets_the_same_center_radius_shorthand(self):
        ents, notes = self._norm([{"type": "arc", "cx": 1, "cy": 2, "r": 3,
                                   "start": 0, "end": 90}])
        assert ents[0]["center"] == [1, 2] and ents[0]["radius"] == 3
        assert ents[0]["ccw"] is True

    def test_ellipse_center_shorthand_leaves_rx_ry_alone(self):
        ents, _ = self._norm([{"type": "ellipse", "cx": 1, "cy": 1, "rx": 4, "ry": 2,
                               "rot": 0}])
        assert ents[0]["center"] == [1, 1]
        assert ents[0]["rx"] == 4 and ents[0]["ry"] == 2

    def test_line_x1_y1_x2_y2_becomes_p1_p2(self):
        ents, notes = self._norm([{"type": "line", "x1": 0, "y1": 0, "x2": 10, "y2": 0}])
        assert ents[0]["p1"] == [0, 0] and ents[0]["p2"] == [10, 0]
        assert any("x1/y1/x2/y2 -> p1/p2" in n for n in notes)

    def test_point_x_y_becomes_p(self):
        ents, notes = self._norm([{"type": "point", "x": 3, "y": 4}])
        assert ents[0]["p"] == [3, 4]
        assert any("x/y -> p" in n for n in notes)

    def test_29_circles_emit_one_note_not_29(self):
        entities = [{"type": "circle", "cx": float(i), "cy": 0, "r": 1.0}
                    for i in range(29)]
        ents, notes = self._norm(entities)
        assert len(ents) == 29
        assert all(e["radius"] == 1.0 and "center" in e for e in ents)
        shorthand_notes = [n for n in notes if "expanded flat entity shorthand" in n]
        assert len(shorthand_notes) == 1

    def test_an_already_canonical_circle_is_untouched(self):
        ents, notes = self._norm([{"type": "circle", "id": "e1", "construction": False,
                                   "center": [0, 0], "radius": 5}])
        assert ents == [{"type": "circle", "id": "e1", "construction": False,
                         "center": [0, 0], "radius": 5}]
        assert notes == []


# --------------------------------------------------------------------- other schema

def test_a_value_wrapper_around_a_coordinate_is_unwrapped():
    notes: list = []
    out = M._unwrap_vector_dims(
        {"type": "cylinder", "name": "F",
         "params": {"radius": 22, "origin": {"value": [0, 0, -7.5], "expr": None}}},
        "cylinder", notes)
    assert out["params"]["origin"] == [0, 0, -7.5]
    assert notes


def test_a_kindless_face_ref_becomes_a_geometry_query():
    notes: list = []
    out = M._normalize_feature_refs(
        {"type": "hole", "name": "B", "refs": {"face": {"side": "top"}}}, "hole", notes)
    assert out["refs"]["face"] == {"of": "face", "side": "top"}
    assert notes


def test_kindless_body_targets_are_completed():
    out = M._normalize_feature_refs(
        {"type": "boolean", "name": "U",
         "refs": {"targets": [{"feature": "Barrel"}, "Flange"]}}, "boolean", [])
    assert out["refs"]["targets"] == [{"kind": "body", "owner_feature": "Barrel"},
                                      {"kind": "body", "owner_feature": "Flange"}]


def test_boolean_operation_and_targets_become_op_a_b():
    notes: list = []
    out = M._normalize_boolean_feature(
        {"type": "boolean", "name": "UnionBody",
         "params": {"operation": "union", "targets": ["Barrel", "Flange"]}}, notes)
    assert out["params"]["op"] == "fuse"
    assert (out["params"]["a"], out["params"]["b"]) == ("Barrel", "Flange")
    assert "operation" not in out["params"] and "targets" not in out["params"]
    assert notes


def test_revolve_gets_its_mode_profile_and_axis_refs():
    notes: list = []
    out = M._normalize_profile_feature(
        {"type": "revolve", "name": "Body",
         "params": {"sketch": "ProfileSketch", "axis": "Z", "angle": 360}}, "revolve", notes)
    assert out["params"]["mode"] == "boss"
    assert out["refs"]["profile"] == [{"kind": "region", "sketch": "ProfileSketch",
                                       "hint_index": 0}]
    assert out["refs"]["axis"] == "axis.z"
    assert "sketch" not in out["params"] and "axis" not in out["params"]


def test_an_inline_polyline_profile_teaches_the_sketch_first_workflow():
    with pytest.raises(ValueError) as ei:
        M._normalize_profile_feature(
            {"type": "revolve", "name": "Body",
             "params": {"profile": {"type": "polyline", "points": [[0, 0], [1, 0]]}}},
            "revolve", [])
    msg = str(ei.value)
    assert "INLINE geometry" in msg and '"kind":"region"' in msg


def test_a_same_batch_feature_name_resolves_to_its_placeholder():
    notes: list = []
    ops = M._resolve_batch_name_refs([
        {"op": "add_feature", "feature": {"type": "cylinder", "name": "Flange",
                                          "params": {"radius": 25, "height": 6}}},
        {"op": "add_feature", "feature": {"type": "boolean", "name": "UnionBody",
                                          "params": {"op": "fuse", "a": "Flange",
                                                     "b": "f1"}}}], notes)
    assert ops[1]["feature"]["params"]["a"] == "$0"
    assert ops[1]["feature"]["params"]["b"] == "f1"     # a real id is never touched
    assert notes


def test_a_placement_word_becomes_a_coordinate_or_a_face_query():
    notes: list = []
    out = M._normalize_placement_word(
        {"type": "cylinder", "name": "C", "at": "center"}, "cylinder", notes)
    assert out["at"] == [0, 0, 0]
    out = M._normalize_placement_word(
        {"type": "cylinder", "name": "C", "at": "top"}, "cylinder", [])
    assert out["at"] == {"of": "face", "side": "top"}
    assert notes


# --------------------------------------------------------------------- end to end

try:
    from services.mcp import core_bridge as _cb
    _ENGINE = _cb.available()
except Exception:  # noqa: BLE001
    _ENGINE = False

enginetest = pytest.mark.skipif(not _ENGINE, reason="libchudware not built/staged")


@pytest.fixture(scope="module")
def live():
    """(add_features callable, a fresh doc_id with a real body) via the BUILT-IN agent path."""
    from services.agent.agent_turn import _bind_tool
    new_part, add_features = _bind_tool("new_part"), _bind_tool("add_features")
    assert new_part is not None and add_features is not None
    doc = json.loads(new_part(name="add_features_shape_test"))["doc_id"]
    r = json.loads(add_features(doc_id=doc, ops=[{"op": "add_feature", "feature": {
        "type": "box", "name": "Plate",
        "params": {"length": 80, "width": 60, "height": 20}}}]))
    assert r["ok"], r
    return add_features, doc


@enginetest
def test_live_canonical_call_reports_no_interpretation(live):
    add_features, doc = live
    r = json.loads(add_features(doc_id=doc, ops=[{"op": "add_feature", "feature": {
        "type": "cylinder", "name": "CanonPost",
        "params": {"radius": 4, "height": 30}, "at": [30, 20, 0]}}]))
    assert r["ok"], r
    assert "interpreted" not in r


@enginetest
def test_live_bare_feature_dicts_in_ops_build(live):
    add_features, doc = live
    r = json.loads(add_features(doc_id=doc, ops=[
        {"type": "cylinder", "name": "BarePost", "params": {"radius": 4, "height": 30},
         "at": [-30, 20, 0]}]))
    assert r["ok"], r
    assert any("bare feature dict" in n for n in r["interpreted"])


@enginetest
def test_live_bare_new_part_head_op_mints_the_doc():
    """A leading bare {"type":"new_part",…} — the exact batch shape a live agent run
    was rejected over ("new_part is not a feature type", 2026-07-27) — mints the
    document and the rest of the batch builds into it."""
    from services.agent.agent_turn import _bind_tool
    add_features = _bind_tool("add_features")
    r = json.loads(add_features(ops=[
        {"type": "new_part", "name": "BareHeadMint"},
        {"type": "box", "name": "Base",
         "params": {"length": 40, "width": 30, "height": 20}}]))
    assert r["ok"], r
    assert r["doc_id"]
    assert any("new_part" in n for n in r["interpreted"])
    assert r["changes"]["volume_delta_mm3"] > 0


@enginetest
def test_live_bare_new_part_head_op_with_doc_id_is_a_noop(live):
    """With a target doc already given, the bare new_part element is dropped, not
    mistaken for a feature type."""
    add_features, doc = live
    r = json.loads(add_features(doc_id=doc, ops=[
        {"type": "new_part", "name": "ShouldNotMint"},
        {"type": "cylinder", "name": "PostAfterBareHead",
         "params": {"radius": 3, "height": 10}, "at": [30, 20, 10],
         "centered": False}]))
    assert r["ok"], r
    assert r["doc_id"] == doc


@enginetest
def test_live_field_batch_bare_new_part_ab_sketch_revolve_builds():
    """The exact dialect of a real rejected copilot batch (2026-07-27, "build me a
    mud bucket"): bare {"type":"new_part"} head, a sketch whose lines use a/b
    endpoints with an id-labeled profile, and a revolve that omits params.angle.
    All three used to be independent batch-killing rejections."""
    from services.agent.agent_turn import _bind_tool
    add_features = _bind_tool("add_features")
    r = json.loads(add_features(ops=[
        {"type": "new_part", "name": "Mud_Bucket"},
        {"type": "sketch", "id": "profile", "plane": "XZ", "entities": [
            {"type": "line", "a": [25, 0], "b": [25, 100]},
            {"type": "line", "a": [25, 100], "b": [147, 180]},
            {"type": "line", "a": [147, 180], "b": [150, 180]},
            {"type": "line", "a": [150, 180], "b": [28, 0]},
            {"type": "line", "a": [28, 0], "b": [25, 0]}]},
        {"type": "revolve", "name": "Bucket", "profile": "profile", "axis": "Z"}]))
    assert r["ok"], r
    assert r["changes"]["volume_delta_mm3"] > 0
    notes = " ".join(r["interpreted"])
    assert "new_part" in notes and "angle" in notes


@enginetest
def test_live_hole_without_style_end_or_face_builds(live):
    add_features, doc = live
    r = json.loads(add_features(doc_id=doc, ops=[{"op": "add_feature", "feature": {
        "type": "hole", "name": "LiveBore",
        "params": {"diameter": 12.0, "depth": "through", "origin": [0, 0, 0],
                   "direction": [0, 0, 1]}}}]))
    assert r["ok"], r
    assert r["changes"]["volume_delta_mm3"] < 0        # it really removed material
    text = " ".join(r["interpreted"])
    assert "params.style is required" in text and "drill direction as the top face" in text


@enginetest
def test_live_flat_feature_kwargs_build(live):
    add_features, doc = live
    r = json.loads(add_features(doc_id=doc, type="cylindrical", name="FlatPost",
                                params={"radius": 4, "height": 25}, at=[30, -20, 0],
                                purpose="a mounting boss", basis="AC2"))
    assert r["ok"], r
    text = " ".join(r["interpreted"])
    assert "cylindrical" in text and "commentary" in text


@enginetest
def test_live_rejected_batch_still_echoes_the_interpretation(live):
    add_features, doc = live
    r = json.loads(add_features(doc_id=doc, type="cylindrical", diameter=20.042,
                                location="center", purpose="sliding fit"))
    assert r["ok"] is False
    assert "height" in r["error"]                       # genuinely underspecified
    assert r["interpreted"]                             # …but the model sees how we read it


@enginetest
def test_live_unknown_feature_type_names_the_closest_real_types(live):
    add_features, doc = live
    r = json.loads(add_features(doc_id=doc, ops=[{"op": "add_feature", "feature": {
        "type": "cylnder", "name": "Typo", "params": {"radius": 3, "height": 5}}}]))
    assert r["ok"] is False
    assert "is not a feature type" in r["error"] and "cylinder" in r["error"]


@enginetest
def test_fastmcp_path_still_serves_the_canonical_call(live):
    """The var-keyword parameter must not turn into a REQUIRED FastMCP field.

    A bare ``**kwargs`` becomes a required pydantic field named after the parameter, so
    EVERY FastMCP call — including the documented one — fails with "Field required" while
    the built-in agent path (which bypasses FastMCP) keeps working.
    """
    from mcp_server import server
    _add_features, doc = live
    tools = asyncio.run(server.mcp.list_tools())
    schema = next(t for t in tools if t.name == "add_features").inputSchema
    assert "extra" in schema["properties"]
    assert "extra" not in (schema.get("required") or [])

    _content, structured = asyncio.run(server.mcp.call_tool("add_features", {
        "doc_id": doc,
        "ops": [{"op": "add_feature", "feature": {
            "type": "cylinder", "name": "FastMcpPost",
            "params": {"radius": 3, "height": 30}, "at": [-30, -20, 0]}}]}))
    r = json.loads(structured["result"])
    assert r["ok"], r
    assert "interpreted" not in r


@enginetest
def test_bound_tool_reports_var_kw_so_the_reconciler_passes_args_through():
    from services.agent.agent_turn import _bind_tool, _signature_spec
    assert _signature_spec(_bind_tool("add_features"))["var_kw"] is True


def test_extrude_param_name_dialect_maps_onto_the_keys_the_compiler_reads():
    notes: list = []
    out = M._normalize_extrude_params(
        {"type": "extrude", "name": "E",
         "params": {"distance": 4, "symmetric": True}}, "extrude", notes)
    assert out["params"]["depth"] == 4
    assert out["params"]["mid_plane"] is True
    assert "distance" not in out["params"] and "symmetric" not in out["params"]
    text = " ".join(notes)
    assert "params.depth" in text and "params.mid_plane" in text


def test_a_thin_feature_thickness_is_not_stolen_as_the_extrude_depth():
    feat = {"type": "extrude", "name": "E",
            "params": {"thin": True, "thickness": 2, "depth": 10}}
    assert M._normalize_extrude_params(feat, "extrude", []) is feat


@enginetest
def test_live_guide_shaped_sketch_extrude_batch_builds(live):
    """The VERBATIM batch a live Opus run wrote after reading cad_guide('sketch').

    Op-level feature keys (no "feature" wrapper), the documented reserved datum
    "plane.top", a SCALAR profile ref, no params.mode, and the param-name dialect
    distance/symmetric. It used to die on a raw `key 'depth' not found` kernel throw.
    """
    from services.agent.agent_turn import _bind_tool
    new_part = _bind_tool("new_part")
    add_features, _doc = live
    doc = json.loads(new_part(name="guide_shaped_batch"))["doc_id"]
    r = json.loads(add_features(doc_id=doc, ops=[
        {"op": "add_feature", "type": "sketch", "name": "Prof",
         "refs": {"plane": "plane.top"},
         "params": {"entities": [
             {"id": "e1", "type": "line", "construction": False,
              "p1": [0, 0], "p2": [20, 0]},
             {"id": "e2", "type": "line", "construction": False,
              "p1": [20, 0], "p2": [20, 12]},
             {"id": "e3", "type": "line", "construction": False,
              "p1": [20, 12], "p2": [0, 12]},
             {"id": "e4", "type": "line", "construction": False,
              "p1": [0, 12], "p2": [0, 0]}],
             "constraints": []}},
        {"op": "add_feature", "type": "extrude", "name": "Body",
         "refs": {"profile": "$0"},
         "params": {"distance": 4, "symmetric": True}},
    ]))
    assert r["ok"], r
    assert r["body_count"] >= 1 and r["volume_mm3"] > 0
    text = " ".join(r["interpreted"])
    assert "params.depth" in text          # distance -> depth
    assert "params.mid_plane" in text      # symmetric -> mid_plane


def test_bare_sid_detection_matches_the_stable_sid_grammar():
    assert M._bare_sid("Ffa9a6e") == "Ffa9a6e"
    assert M._bare_sid("E1c4a90") == "E1c4a90"
    assert M._bare_sid("f1") is None            # a feature id, not a sid
    assert M._bare_sid("$0") is None            # a same-batch placeholder
    assert M._bare_sid("plane.top") is None     # a reserved datum
    assert M._bare_sid({"kind": "face"}) is None


def test_cylinder_without_height_points_at_the_hole_feature():
    with pytest.raises(ValueError) as ei:
        M._check_cylinder_height(
            {"type": "cylinder", "name": "bore", "params": {"radius": 10.021}},
            "cylinder", [])
    msg = str(ei.value)
    assert "params.height is required" in msg
    assert '"type":"hole"' in msg and '"end":"through_all"' in msg


def test_a_plain_cylinder_without_height_still_says_what_is_missing():
    with pytest.raises(ValueError) as ei:
        M._check_cylinder_height(
            {"type": "cylinder", "name": "Post", "params": {"radius": 4}}, "cylinder", [])
    msg = str(ei.value)
    assert "params.height is required" in msg
    assert "hole" not in msg          # no bore intent -> no misleading suggestion


def test_a_cylinder_with_a_height_is_left_alone():
    feat = {"type": "cylinder", "name": "Bore", "params": {"radius": 4, "height": 10}}
    assert M._check_cylinder_height(feat, "cylinder", []) is feat


@enginetest
def test_live_a_bare_face_sid_works_as_a_sketch_plane(live):
    """The verbatim recorded call: a sketch whose refs.plane is the bare sid describe_part
    handed the model. Sketching on a face is core CAD; the engine wants a selector."""
    from services.mcp import core_bridge as cb
    add_features, doc = live
    faces = cb.call("doc_query", {"doc_id": doc, "query": {"of": "face", "side": "top"}})
    sid = faces["matches"][0]["sid"]
    r = json.loads(add_features(doc_id=doc, ops=[{"op": "add_feature", "feature": {
        "type": "sketch", "name": "FlangeSketch", "refs": {"plane": sid},
        "params": {"entities": [{"id": "e0", "type": "circle", "construction": False,
                                 "center": [0, 0], "radius": 22}]}}}]))
    assert r["ok"], r
    assert any("bare face/edge sid" in n and sid in n for n in r["interpreted"])


@enginetest
def test_live_a_bare_sid_is_wrapped_in_a_non_plane_slot_too(live):
    from services.mcp import core_bridge as cb
    add_features, doc = live
    faces = cb.call("doc_query", {"doc_id": doc, "query": {"of": "face", "side": "top"}})
    sid = faces["matches"][0]["sid"]
    r = json.loads(add_features(doc_id=doc, ops=[{"op": "add_feature", "feature": {
        "type": "hole", "name": "SidBore",
        "params": {"diameter": 6, "style": "simple", "end": "through_all"},
        "refs": {"face": sid}}}]))
    assert r["ok"], r
    assert r["changes"]["volume_delta_mm3"] < 0
    assert any("bare face/edge sid" in n for n in r["interpreted"])


@enginetest
def test_live_an_unknown_sid_teaches_with_the_real_roster(live):
    add_features, doc = live
    r = json.loads(add_features(doc_id=doc, ops=[{"op": "add_feature", "feature": {
        "type": "sketch", "name": "Ghost", "refs": {"plane": "Fdeadbe"},
        "params": {"entities": [], "constraints": []}}}]))
    assert r["ok"] is False
    msg = r["error"]
    assert 'no face with sid "Fdeadbe"' in msg
    assert "Live face sids:" in msg and "F" in msg.split("Live face sids:")[1]
    assert "describe_part" in msg


@enginetest
def test_live_bore_shaped_cylinder_error_names_the_hole_feature(live):
    add_features, doc = live
    r = json.loads(add_features(doc_id=doc, feature="bore", type="cylindrical",
                                diameter=20.042, location="center",
                                purpose="sliding fit h7", clearance=0.042,
                                basis="acceptance criteria AC1, AC2"))
    assert r["ok"] is False
    assert '"type":"hole"' in r["error"]
    assert r["interpreted"]          # how the flat kwargs were read is still echoed


# --------------------------------------------------- polyline / boolean / selector shapes
# The live "mudbucket" run (kimi-k3, 2026-07-27) fumbled add_features twice on shapes that
# say exactly one thing: a polyline profile, a flat boolean with target/tool/kind, and a
# single selector wrapped in a list. Each is normalized; each ambiguous cousin still errors.

_MUDBUCKET_POLYLINE = {"type": "polyline", "closed": True,
                       "points": [[0, 0], [256, 0], [306, 800], [0, 800]]}


def _sketch_ops(feature, notes=None):
    """Run one add_feature through the op-element + sketch passes -> the feature dict."""
    sink = notes if isinstance(notes, list) else []
    ops = M._normalize_op_elements([feature], sink)
    return M._normalize_sketch_ops(ops, sink)[0]["feature"]


def test_a_flat_polyline_sketch_op_expands_into_the_line_entities_it_names():
    notes: list = []
    feat = _sketch_ops({"op": "sketch", "name": "shell_profile", "plane": "XZ",
                        "entities": [_MUDBUCKET_POLYLINE]}, notes)
    assert feat == {
        "type": "sketch", "name": "shell_profile",
        "refs": {"plane": "plane.front"},
        "params": {"constraints": [], "entities": [
            {"id": "e1", "type": "line", "construction": False,
             "p1": [0, 0], "p2": [256, 0]},
            {"id": "e2", "type": "line", "construction": False,
             "p1": [256, 0], "p2": [306, 800]},
            {"id": "e3", "type": "line", "construction": False,
             "p1": [306, 800], "p2": [0, 800]},
            {"id": "e4", "type": "line", "construction": False,
             "p1": [0, 800], "p2": [0, 0]},        # closed:true -> back to the first point
        ]}}
    assert any("expanded the polyline into 4 line entities" in n for n in notes)


def test_a_repeated_closing_point_is_not_a_second_closing_segment():
    feat = _sketch_ops({"op": "add_feature", "feature": {
        "type": "sketch", "name": "S", "params": {"plane": "XY"},
        "entities": [{"type": "polyline", "closed": True,
                      "points": [[0, 0], [10, 0], [10, 20], [0, 0]]}]}})
    ents = feat["params"]["entities"]
    assert [e["id"] for e in ents] == ["e1", "e2", "e3"]
    assert ents[-1]["p1"] == [10, 20] and ents[-1]["p2"] == [0, 0]


def test_an_open_polyline_is_left_open():
    feat = _sketch_ops({"op": "sketch", "name": "S", "entities": [
        {"type": "polyline", "closed": False, "points": [[0, 0], [5, 0], [5, 5]]}]})
    ents = feat["params"]["entities"]
    assert len(ents) == 2 and ents[-1]["p2"] == [5, 5]


def test_a_polyline_keeps_numbering_beside_the_entities_around_it():
    feat = _sketch_ops({"op": "sketch", "name": "S", "entities": [
        {"type": "circle", "center": [0, 0], "radius": 3},
        {"type": "polyline", "closed": False, "points": [[0, 0], [1, 0], [2, 0]]},
        {"type": "line", "start": [2, 0], "end": [2, 9]}]})
    ents = feat["params"]["entities"]
    assert [e["id"] for e in ents] == ["e1", "e2", "e3", "e4"]
    assert [e["type"] for e in ents] == ["circle", "line", "line", "line"]


def test_a_construction_polyline_expands_into_construction_lines():
    feat = _sketch_ops({"op": "sketch", "name": "S", "entities": [
        {"type": "polyline", "construction": True, "points": [[0, 0], [1, 1]]}]})
    assert feat["params"]["entities"][0]["construction"] is True


def test_a_one_point_polyline_is_left_for_the_engine_to_reject():
    # Nothing unambiguous to expand -> the type stays "polyline" and validate.cpp still
    # answers "not a sketch entity type" instead of this inventing a profile.
    feat = _sketch_ops({"op": "sketch", "name": "S", "entities": [
        {"type": "polyline", "closed": True, "points": [[0, 0]]}]})
    assert feat["params"]["entities"][0]["type"] == "polyline"


def test_a_polyline_whose_points_are_not_xy_pairs_is_left_alone():
    feat = _sketch_ops({"op": "sketch", "name": "S", "entities": [
        {"type": "polyline", "points": [[0, 0], "midpoint", [4, 4]]}]})
    assert feat["params"]["entities"][0]["type"] == "polyline"


def test_entities_at_the_feature_level_are_re_homed_under_params():
    # The wrapper-op dialect: params holds the plane, entities sit beside it. Canonical is
    # params.entities (validate.cpp FEATURE_TYPES["sketch"] requires it there).
    feat = _sketch_ops({"op": "add_feature", "feature": {
        "type": "sketch", "name": "S", "params": {"plane": "XZ"},
        "entities": [{"type": "line", "start": [0, 0], "end": [1, 0]}]}})
    assert "entities" not in feat and "plane" not in feat["params"]
    assert feat["params"]["entities"][0] == {"id": "e1", "type": "line",
                                             "construction": False,
                                             "p1": [0, 0], "p2": [1, 0]}
    assert feat["refs"]["plane"] == "plane.front"


def test_a_boolean_target_tool_pair_becomes_params_a_and_b():
    notes: list = []
    out = M._normalize_boolean_feature(
        {"type": "boolean", "name": "drain_union", "target": "body",
         "tool": "drain_boss", "kind": "union"}, notes)
    assert out == {"type": "boolean", "name": "drain_union",
                   "params": {"op": "fuse", "a": "body", "b": "drain_boss"}}
    text = " ".join(notes)
    assert 'target="body"' in text and 'tool="drain_boss"' in text


@pytest.mark.parametrize("word,want", [("union", "fuse"), ("fuse", "fuse"),
                                       ("cut", "cut"), ("subtract", "cut"),
                                       ("difference", "cut"),
                                       ("intersect", "common"), ("common", "common")])
def test_boolean_kind_words_map_onto_the_engine_operations(word, want):
    out = M._normalize_boolean_feature(
        {"type": "boolean", "name": "B", "kind": word, "target": "A", "tool": "T"}, [])
    assert out["params"] == {"op": want, "a": "A", "b": "T"}


def test_a_single_element_operand_list_is_read_as_that_operand():
    out = M._normalize_boolean_feature(
        {"type": "boolean", "name": "B", "params": {"op": "cut"},
         "targets": ["plate"], "tools": ["pocket"]}, [])
    assert out["params"] == {"op": "cut", "a": "plate", "b": "pocket"}


def test_a_boolean_with_only_a_target_still_teaches_the_two_operands():
    with pytest.raises(ValueError) as ei:
        M._normalize_boolean_feature(
            {"type": "boolean", "name": "B", "kind": "union", "target": "body"}, [])
    assert "needs its two operands" in str(ei.value)


def test_an_unknown_boolean_kind_is_still_a_teaching_error():
    with pytest.raises(ValueError) as ei:
        M._normalize_boolean_feature(
            {"type": "boolean", "name": "B", "kind": "imprint",
             "target": "body", "tool": "t"}, [])
    assert "is not a boolean operation" in str(ei.value)


def test_a_one_element_selector_list_in_a_single_slot_is_unwrapped():
    notes: list = []
    out = M._normalize_feature_refs(
        {"type": "revolve", "name": "body",
         "refs": {"profile": [{"kind": "region", "sketch": "P"}],
                  "axis": [{"kind": "datum", "id": "Z"}]}}, "revolve", notes)
    assert out["refs"] == {"profile": [{"kind": "region", "sketch": "P"}],  # a LIST slot
                           "axis": {"kind": "datum", "feature": "axis.z"}}
    text = " ".join(notes)
    assert "unwrapped the one-element list in refs.axis" in text


def test_a_multi_element_list_in_a_single_slot_keeps_the_engine_error():
    feat = {"type": "revolve", "name": "body", "refs": {
        "axis": [{"kind": "datum", "feature": "axis.z"},
                 {"kind": "datum", "feature": "axis.x"}]}}
    # Two selectors in a one-selector slot is a real disagreement, not a spelling:
    # left untouched so validate.cpp still says "selector must be an object".
    assert M._normalize_feature_refs(feat, "revolve", []) is feat


def test_a_datum_selector_gets_its_datum_under_feature():
    out = M._normalize_feature_refs(
        {"type": "circular_pattern", "name": "P",
         "refs": {"axis": {"kind": "datum", "name": "Y axis"}}}, "circular_pattern", [])
    assert out["refs"]["axis"] == {"kind": "datum", "feature": "axis.y"}


def test_a_datum_selector_naming_no_known_datum_is_left_alone():
    feat = {"type": "revolve", "name": "R",
            "refs": {"axis": {"kind": "datum", "id": "spindle"}}}
    assert M._normalize_feature_refs(feat, "revolve", []) is feat


def test_a_canonical_datum_selector_round_trips():
    feat = {"type": "revolve", "name": "R",
            "refs": {"axis": {"kind": "datum", "feature": "axis.z"}}}
    assert M._normalize_feature_refs(feat, "revolve", []) is feat


def test_the_live_mudbucket_batch_normalizes_end_to_end():
    """Both rejected calls from run-13918288087065, through the unit pipeline."""
    notes: list = []
    ops = [{"op": "sketch", "name": "shell_profile", "plane": "XZ",
            "entities": [_MUDBUCKET_POLYLINE]},
           {"op": "add_feature", "feature": {
               "type": "revolve", "name": "body",
               "refs": {"profile": [{"kind": "region", "sketch": "shell_profile"}],
                        "axis": [{"kind": "datum", "id": "Z"}]},
               "params": {"mode": "boss", "angle": 360}}},
           {"op": "boolean", "name": "drain_union", "tool": "drain_boss",
            "target": "body", "kind": "union"}]
    ops = M._normalize_op_elements(ops, notes)
    ops = M._normalize_feature_types(ops, notes)
    ops = M._normalize_sketch_ops(ops, notes)
    ops = M._normalize_feature_ops(ops, notes)
    sketch, revolve, boolean = (o["feature"] for o in ops)
    assert len(sketch["params"]["entities"]) == 4
    assert all(e["type"] == "line" for e in sketch["params"]["entities"])
    assert sketch["refs"]["plane"] == "plane.front"
    assert revolve["refs"]["axis"] == {"kind": "datum", "feature": "axis.z"}
    assert boolean["params"] == {"op": "fuse", "a": "body", "b": "drain_boss"}


@enginetest
def test_live_mudbucket_polyline_revolve_boolean_batch_builds():
    """The VERBATIM shapes of both rejected calls in run-13918288087065 (kimi-k3, "make me
    a mudbucket for oil drilling and rigging"), merged into the batch they were reaching
    for: a flat polyline sketch op, a revolve whose axis is a one-element datum-selector
    list, and a flat boolean spelled target/tool/kind. It must BUILD, not just normalize."""
    from services.agent.agent_turn import _bind_tool
    new_part, add_features = _bind_tool("new_part"), _bind_tool("add_features")
    doc = json.loads(new_part(name="mudbucket_dialect"))["doc_id"]
    r = json.loads(add_features(doc_id=doc, ops=[
        {"op": "sketch", "name": "shell_profile", "plane": "XZ", "entities": [
            {"type": "polyline", "closed": True,
             "points": [[0, 0], [256, 0], [306, 800], [0, 800]]}]},
        {"op": "add_feature", "feature": {
            "type": "revolve", "name": "body",
            "refs": {"profile": [{"kind": "region", "sketch": "shell_profile"}],
                     "axis": [{"kind": "datum", "id": "Z"}]},
            "params": {"mode": "boss", "angle": 360}}},
        {"op": "add_feature", "feature": {
            "type": "cylinder", "name": "drain_boss", "merge": False,
            "params": {"radius": 40, "height": 120, "at": [0, 0, 0], "axis": [1, 0, 0]}}},
        {"op": "boolean", "name": "drain_union", "tool": "drain_boss",
         "target": "body", "kind": "union"}]))
    assert r["ok"], r
    assert r["changes"]["volume_delta_mm3"] > 0
    text = " ".join(r["interpreted"])
    assert "expanded the polyline into 4 line entities" in text
    assert "unwrapped the one-element list in refs.axis" in text
    assert 'target="body"' in text and 'tool="drain_boss"' in text


# --------------------------------------------------- loft sections / revolve zero-volume
# The live "bucket" run (kimi-k3, 2026-07-27) died on «refs.sections: required for loft»
# after writing the profiles under a different key, and on «revolve produced a
# zero-volume solid» with no hint of WHERE the profile sat. The loft aliases normalize;
# the revolve error now names the exact offset — geometry is NEVER auto-repaired.

def _loft_feat(refs=None, **params):
    f = {"type": "loft", "name": "Bucket",
         "params": {"mode": "boss", **params}}
    if refs is not None:
        f["refs"] = refs
    return f


def _sections_of(feat, notes=None):
    """Run one loft through the schema pipeline -> its refs.sections value."""
    sink = notes if notes is not None else []
    out = M._normalize_feature_schema(feat, "loft", sink)
    return (out.get("refs") or {}).get("sections")


def test_loft_refs_profiles_alias_becomes_sections():
    notes: list = []
    secs = _sections_of(_loft_feat(refs={"profiles": [
        {"kind": "region", "sketch": "base"}, {"kind": "region", "sketch": "rim"}]}), notes)
    assert secs == [{"kind": "region", "sketch": "base"},
                    {"kind": "region", "sketch": "rim"}]
    assert any("refs.profiles" in n and "refs.sections" in n for n in notes)


def test_loft_refs_sketches_alias_becomes_sections():
    secs = _sections_of(_loft_feat(refs={"sketches": [{"kind": "region", "sketch": "s1"}]}))
    assert secs == [{"kind": "region", "sketch": "s1"}]


def test_loft_sections_nested_one_level_too_deep_are_unwrapped():
    secs = _sections_of(_loft_feat(refs={"sections": {"sections": [
        {"kind": "region", "sketch": "a"}, {"kind": "region", "sketch": "b"}]}}))
    assert [s["sketch"] for s in secs] == ["a", "b"]


def test_loft_a_single_bare_selector_becomes_a_one_element_list():
    secs = _sections_of(_loft_feat(refs={"sections": {"kind": "region", "sketch": "only"}}))
    assert secs == [{"kind": "region", "sketch": "only"}]


def test_loft_a_bare_sketch_name_string_becomes_a_region_selector():
    secs = _sections_of(_loft_feat(refs={"sections": "base"}))
    assert secs == [{"kind": "region", "sketch": "base", "hint_index": 0}]


def test_loft_string_members_become_region_selectors():
    secs = _sections_of(_loft_feat(refs={"profiles": ["base", "rim"]}))
    assert secs == [{"kind": "region", "sketch": "base", "hint_index": 0},
                    {"kind": "region", "sketch": "rim", "hint_index": 0}]


@pytest.mark.parametrize("key", ["profiles", "sketches"])
def test_loft_params_level_plural_keys_become_sections(key):
    notes: list = []
    secs = _sections_of(_loft_feat(**{key: ["base", "rim"]}), notes)
    assert [s["sketch"] for s in secs] == ["base", "rim"]
    assert any(f"params.{key}" in n for n in notes)


def test_loft_params_sections_list_becomes_sections():
    secs = _sections_of(_loft_feat(sections=[{"kind": "region", "sketch": "a"},
                                             {"kind": "region", "sketch": "b"}]))
    assert [s["sketch"] for s in secs] == ["a", "b"]


def test_a_canonical_loft_is_left_byte_identical():
    feat = _loft_feat(refs={"sections": [{"kind": "region", "sketch": "a", "hint_index": 0},
                                         {"kind": "region", "sketch": "b", "hint_index": 0}]})
    notes: list = []
    assert M._normalize_feature_schema(feat, "loft", notes) == feat
    assert notes == []


@pytest.mark.parametrize("bad", [{"sections": []},                    # empty: no intent
                                 {"sections": {"width": 4}},          # neither wrapper nor selector
                                 {"sections": [["a", "b"]]},          # non-selector member
                                 {"sections": {"sections": "deep", "extra": 1}}])
def test_an_ambiguous_sections_shape_keeps_the_engine_error(bad):
    feat = _loft_feat(refs=bad)
    notes: list = []
    out = M._normalize_feature_schema(feat, "loft", notes)
    assert (out.get("refs") or {}).get("sections") == bad["sections"]
    assert not any("refs.sections" in n for n in notes)


def test_the_loft_alias_error_now_matches_the_schema_the_engine_validates():
    # The engine requires refs.sections as a SelList (validate.cpp FEATURE_TYPES["loft"]) —
    # every accepted alias above lands in exactly that slot as a list of selector objects.
    for refs in ({"profiles": [{"kind": "region", "sketch": "a"}]},
                 {"sketches": [{"kind": "region", "sketch": "a"}]},
                 {"sections": {"sections": [{"kind": "region", "sketch": "a"}]}},
                 {"sections": {"kind": "region", "sketch": "a"}}):
        secs = _sections_of(_loft_feat(refs=refs))
        assert isinstance(secs, list) and all(isinstance(s, dict) for s in secs)


# --------------------------------------------------------------------- revolve zero-volume

_ZERO_VOL = ("revolve produced a zero-volume solid — the profile must sit to ONE side of "
             "the axis (not cross or touch it) and the angle must be > 0 (got 360 deg)")


def _revolve_batch(x0, x1):
    """A front-plane profile sketch + the revolve of it, as a post-normalization batch."""
    return [{"op": "add_feature", "feature": {
                "type": "sketch", "name": "profile", "refs": {"plane": "plane.front"},
                "params": {"plane": "plane.front", "constraints": [], "entities": [
                    {"type": "line", "p1": [x0, 0], "p2": [x1, 0]},
                    {"type": "line", "p1": [x1, 0], "p2": [x1, 100]},
                    {"type": "line", "p1": [x1, 100], "p2": [x0, 100]},
                    {"type": "line", "p1": [x0, 100], "p2": [x0, 0]}]}}},
            {"op": "add_feature", "feature": {
                "type": "revolve", "name": "bucket",
                "refs": {"profile": [{"kind": "region", "sketch": "profile",
                                      "hint_index": 0}],
                         "axis": "axis.z"},
                "params": {"mode": "boss", "angle": 360}}}]


def _enriched(x0, x1):
    fs = [{"id": "f1", "ok": True, "error": None},
          {"id": "f2", "ok": False, "error": _ZERO_VOL}]
    M._enrich_zero_volume_errors(fs, _revolve_batch(x0, x1), {"$0": "f1", "$1": "f2"})
    assert fs[0]["error"] is None                      # untouched: only failures enriched
    return fs[1]["error"]


def test_a_straddling_profile_error_names_the_offset_that_clears_the_axis():
    msg = _enriched(-10, 40)
    assert _ZERO_VOL in msg                            # the engine's words are kept
    assert "x in [-10, 40] mm" in msg                  # the profile's extent on the plane
    assert "axis.z" in msg
    assert "minimum x is > 0" in msg                   # the concrete, actionable fix
    assert "add 11 mm" in msg and "[1, 51]" in msg     # a worked example of that fix


def test_a_profile_touching_the_axis_is_told_to_move_off_it():
    msg = _enriched(0, 40)
    assert "minimum x is > 0" in msg and "add 1 mm" in msg


def test_a_profile_already_clear_of_the_axis_is_pointed_at_the_angle():
    msg = _enriched(10, 40)
    assert "already clears the axis" in msg and "angle" in msg
    assert "offset" not in msg


def test_an_unknown_profile_keeps_a_generic_but_actionable_fix():
    fs = [{"id": "f2", "ok": False, "error": _ZERO_VOL}]
    ops = [{"op": "add_feature", "feature": {
        "type": "revolve", "name": "cup",
        "refs": {"profile": [{"kind": "region", "sketch": "f9", "hint_index": 0}],
                 "axis": "axis.y"},
        "params": {"mode": "boss", "angle": 360}}}]
    M._enrich_zero_volume_errors(fs, ops, {"$0": "f2"})
    assert "strictly to ONE side of the axis" in fs[0]["error"]


def test_other_failures_are_never_reworded():
    fs = [{"id": "f1", "ok": False, "error": "loft produced a zero-volume solid — …"},
          {"id": "f2", "ok": False, "error": "extrude produced a zero-volume solid — …"}]
    M._enrich_zero_volume_errors(fs, [], {})
    assert fs[0]["error"].endswith("— …") and fs[1]["error"].endswith("— …")


# --------------------------------------------------------------------------- shakeout dialects
# Found by running claude-haiku-4-5 across modeling scenarios (2026-07-27) and
# bucketing failures by shape. Both are cases where the model had the RIGHT
# content in the WRONG container.

class TestRefSlotMigration:
    def test_fillet_edges_under_params_move_to_refs(self):
        """The live failure: correct edge sids, wrong bag, rejected 3x with
        '.refs.edges: required for fillet'."""
        from services.mcp.cad_doc_tools import _migrate_ref_slots
        ops = [{"op": "add_feature", "feature": {
            "type": "fillet", "name": "Corner_Fillets",
            "params": {"radius": {"value": 10}, "edges": [{"sid": "Ed7ec5d"}]}}}]
        feat = _migrate_ref_slots(ops, [])[0]["feature"]
        assert feat["refs"]["edges"] == [{"sid": "Ed7ec5d"}]
        assert "edges" not in feat["params"]
        assert feat["params"]["radius"] == {"value": 10}

    def test_explicit_refs_win_and_stale_params_copy_is_dropped(self):
        """A duplicate left in params would trip the unknown-param check."""
        from services.mcp.cad_doc_tools import _migrate_ref_slots
        ops = [{"op": "add_feature", "feature": {
            "type": "fillet", "refs": {"edges": [{"sid": "KEEP"}]},
            "params": {"edges": [{"sid": "DROP"}], "radius": 2}}}]
        feat = _migrate_ref_slots(ops, [])[0]["feature"]
        assert feat["refs"]["edges"] == [{"sid": "KEEP"}]
        assert "edges" not in feat["params"]

    def test_non_ref_params_are_untouched(self):
        from services.mcp.cad_doc_tools import _migrate_ref_slots
        ops = [{"op": "add_feature", "feature": {
            "type": "box", "params": {"length": 10, "width": 5, "height": 2}}}]
        feat = _migrate_ref_slots(ops, [])[0]["feature"]
        assert feat["params"] == {"length": 10, "width": 5, "height": 2}
        assert not feat.get("refs")


class TestEnumDimUnwrap:
    def test_mode_wrapped_as_a_dimension_is_unwrapped(self):
        """validate.cpp dim-validates any {value|expr} params entry, so an enum
        wrapped like a length fails as 'non-numeric' — a units error for
        something that was never a number."""
        from services.mcp.cad_doc_tools import _unwrap_enum_dims
        ops = [{"op": "add_feature", "feature": {
            "type": "extrude",
            "params": {"mode": {"value": "blind", "expr": None},
                       "distance": {"value": 5, "expr": None}}}}]
        feat = _unwrap_enum_dims(ops, [])[0]["feature"]
        assert feat["params"]["mode"] == "blind"
        assert feat["params"]["distance"] == {"value": 5, "expr": None}, "real dims must survive"

    def test_numeric_valued_text_key_is_left_as_a_dim(self):
        """'size' is in _NON_DIM_KEYS but a numeric one is a genuine dimension."""
        from services.mcp.cad_doc_tools import _unwrap_enum_dims
        ops = [{"op": "add_feature", "feature": {
            "type": "box", "params": {"size": {"value": 10, "expr": None}}}}]
        feat = _unwrap_enum_dims(ops, [])[0]["feature"]
        assert feat["params"]["size"] == {"value": 10, "expr": None}


class TestMaterialDesignations:
    def test_engineering_designations_resolve_to_an_appearance(self):
        from services.mcp.cad_common import _resolve_appearance
        for name in ("A36 steel", "6061-T6 Al", "304 stainless", "PLA filament"):
            assert _resolve_appearance(name).get("color"), name

    def test_stainless_steel_does_not_degrade_to_steel(self):
        """Longest-first matching, or the more specific preset is lost."""
        from services.mcp.cad_common import _resolve_appearance
        assert _resolve_appearance("stainless steel") == _resolve_appearance("stainless")
        assert _resolve_appearance("A36 steel") == _resolve_appearance("steel")

    def test_genuine_nonsense_is_still_rejected(self):
        import pytest as _pytest
        from services.mcp.cad_common import _resolve_appearance
        with _pytest.raises(ValueError):
            _resolve_appearance("mystery unobtainium")


class TestProseFaceAsSketchPlane:
    """refs.plane="the top face" (live copilot run, 2026-07-28).

    _norm_datum returns an unrecognized string UNCHANGED, so the phrase reached the kernel
    and died on "'the top face' is neither a reserved datum nor a feature id" — which also
    stranded the extrude that referenced the sketch by name, making one root cause look
    like two failures.
    """

    @pytest.mark.parametrize("phrase,side", [
        ("the top face", "top"),
        ("top face", "top"),
        ("the upper surface", "top"),
        ("bottom face", "bottom"),
        ("the underside face", "bottom"),
        ("front face", "front"),
        ("the rear surface", "back"),
        ("left face", "left"),
    ])
    def test_a_face_phrase_becomes_a_geometry_query(self, phrase, side):
        assert M._face_phrase_query(phrase) == {"of": "face", "side": side}

    @pytest.mark.parametrize("phrase", [
        "plane.top",        # already canonical
        "top plane",        # a DATUM, not a face — must stay on the datum path
        "the xy plane",
        "top",              # bare side word: datum
        "the angled face",  # a face, but no side we can resolve — do not guess
        "the top and bottom faces",  # two sides — ambiguous, do not guess
        "PocketSk",         # a feature name
        42,
        None,
    ])
    def test_everything_else_is_left_alone(self, phrase):
        assert M._face_phrase_query(phrase) is None

    def test_a_face_phrase_is_not_silently_swapped_for_the_origin_datum(self):
        """plane.top is z=0; the top face of a 20mm boss is z=20. Mapping a face phrase to
        the like-named datum would be wrong geometry, not a near miss."""
        notes: list = []
        out = M._normalize_sketch_feature({
            "type": "sketch", "name": "PocketSk", "refs": {"plane": "the top face"},
            "params": {"entities": [{"type": "rectangle", "cx": 0, "cy": 0,
                                     "width": 58.6, "height": 29.5}]}}, notes)
        assert out["refs"]["plane"] == {"of": "face", "side": "top"}
        assert any("prose FACE reference" in n for n in notes)

    def test_a_datum_plane_still_normalizes_to_the_datum(self):
        out = M._normalize_sketch_feature({
            "type": "sketch", "name": "S", "refs": {"plane": "top"},
            "params": {"entities": [{"type": "circle", "cx": 0, "cy": 0, "r": 5}]}}, [])
        assert out["refs"]["plane"] == "plane.top"


class TestNegativeExtrudeDepth:
    """params {"distance": -8, "mode": "cut"} (same live run).

    The kernel refuses a negative depth with "extrude depth must be > 0" and never says how
    to reverse the run, so the batch died. The sign is DIRECTION, not magnitude.
    """

    def _norm(self, params):
        notes: list = []
        out = M._normalize_extrude_params(
            {"type": "extrude", "name": "DevicePocket", "params": dict(params)},
            "extrude", notes)
        return out.get("params", {}), notes

    def test_a_negative_distance_becomes_depth_plus_flip(self):
        params, notes = self._norm({"distance": -8, "mode": "cut"})
        assert params["depth"] == 8
        assert params["flip"] is True
        assert any("DIRECTION" in n for n in notes)

    def test_a_negative_depth_on_top_of_an_explicit_flip_flips_back(self):
        """Two reversals are not a reversal — otherwise we silently double-negate."""
        params, _ = self._norm({"depth": -5, "flip": True})
        assert params["depth"] == 5
        assert params["flip"] is False

    def test_a_negative_dimension_object_keeps_its_wrapper(self):
        params, _ = self._norm({"depth": {"value": -12, "expr": None}})
        assert params["depth"] == {"value": 12, "expr": None}
        assert params["flip"] is True

    def test_a_positive_depth_is_untouched(self):
        params, notes = self._norm({"depth": 8, "mode": "cut"})
        assert params["depth"] == 8
        assert "flip" not in params
        assert not any("DIRECTION" in n for n in notes)


class TestFlatFacePocket:
    """{"op":"pocket","face":"top","shape":"rect","width":…,"depth":…} (live copilot run,
    2026-07-28, "open the part and pocket it").

    `pocket` aliased straight onto the `extrude` feature type and then died on
    «refs.profile: required for extrude» — the flat spec names a profile in words but never
    builds one — while the second element, op:"cut", was rejected outright as an unknown op.
    Both are fully determined, so both expand into the canonical sketch + extrude-cut pair.
    """

    LIVE = [
        {"op": "pocket", "name": "DevicePocket", "face": "top", "shape": "rect",
         "width": 51.6, "height": 22.5, "depth": 72, "center": [0, 0], "corner_radius": 4},
        {"op": "cut", "name": "MouthpieceSlot", "face": "top", "shape": "rect",
         "width": 14, "height": 10, "depth": 8, "center": [0, 0]},
    ]

    @staticmethod
    def _expand(ops):
        notes: list = []
        return M._expand_face_pockets(ops, notes), notes

    def test_the_live_batch_becomes_two_sketch_extrude_cut_pairs(self):
        ops, notes = self._expand(self.LIVE)
        assert len(ops) == 4
        sk1, ex1, sk2, ex2 = (o["feature"] for o in ops)
        assert all(o["op"] == "add_feature" for o in ops)
        assert sk1["type"] == "sketch" and ex1["type"] == "extrude"
        assert sk1["refs"]["plane"] == {"of": "face", "side": "top"}
        assert sk1["params"]["entities"] == [
            {"type": "rectangle", "center": [0.0, 0.0], "width": 51.6, "height": 22.5}]
        assert sk1["params"]["constraints"] == []
        assert ex1["params"] == {"mode": "cut", "depth": 72.0}
        assert ex1["refs"]["profile"] == [
            {"kind": "region", "sketch": "DevicePocketProfile", "hint_index": 0}]
        assert sk2["refs"]["plane"] == {"of": "face", "side": "top"}
        assert ex2["refs"]["profile"][0]["sketch"] == "MouthpieceSlotProfile"
        text = " ".join(notes)
        assert "expanded the flat pocket spec" in text
        assert "expanded the flat cut spec" in text

    def test_a_cut_carrying_the_flat_spec_is_the_same_rewrite_as_a_pocket(self):
        spec = {"name": "P", "face": "front", "shape": "rect",
                "width": 20, "height": 10, "depth": 5}
        as_pocket, _ = self._expand([{"op": "pocket", **spec}])
        as_cut, notes = self._expand([{"op": "cut", **spec}])
        assert as_pocket == as_cut
        assert any('read op:"cut"' in n for n in notes)

    def test_a_boolean_style_cut_is_not_hijacked(self):
        """{"op":"cut","a":"f1","b":"f2"} is a BOOLEAN — no face, no depth, no profile."""
        ops = [{"op": "cut", "a": "Body", "b": "Tool"}]
        out, notes = self._expand(ops)
        assert out is ops and notes == []

    def test_a_cut_naming_a_face_but_no_shape_is_left_alone(self):
        """Half a spec is not the spec: a face word alone must not conjure a profile."""
        ops = [{"op": "cut", "face": "top", "target": "Body"}]
        out, _notes = self._expand(ops)
        assert out is ops

    def test_a_circle_pocket_becomes_a_circle_entity_with_a_radius(self):
        ops, notes = self._expand([{"op": "pocket", "name": "Bore", "face": "top",
                                    "shape": "circle", "diameter": 12, "depth": 6,
                                    "center": [10, -5]}])
        assert ops[0]["feature"]["params"]["entities"] == [
            {"type": "circle", "center": [10.0, -5.0], "radius": 6.0}]
        assert any("read diameter=12.0 as radius=6.0" in n for n in notes)

    def test_the_center_defaults_to_the_face_centre(self):
        ops, _notes = self._expand([{"op": "pocket", "face": "bottom", "shape": "rect",
                                     "width": 8, "height": 8, "depth": 2}])
        assert ops[0]["feature"]["params"]["entities"][0]["center"] == [0.0, 0.0]
        assert ops[1]["feature"]["name"] == "Pocket"

    def test_a_corner_radius_is_dropped_loudly_not_silently(self):
        """There is no rounded-rectangle sketch entity, so the radius cannot be honoured —
        but a silently sharp pocket is a wrong part nobody was told about."""
        _ops, notes = self._expand([{"op": "pocket", "name": "P", "face": "top",
                                     "shape": "rect", "width": 20, "height": 10,
                                     "depth": 4, "corner_radius": 3}])
        note = next(n for n in notes if "corner_radius" in n)
        assert "dropped corner_radius=3.0" in note
        assert '"type":"fillet"' in note

    def test_an_unrecognised_shape_teaches_the_accepted_ones(self):
        with pytest.raises(ValueError) as e:
            self._expand([{"op": "pocket", "name": "P", "face": "top", "shape": "hexagon",
                           "width": 20, "height": 10, "depth": 4}])
        msg = str(e.value)
        assert 'shape "hexagon" is not a pocket profile' in msg
        assert 'shape:"rect"' in msg and 'shape:"circle"' in msg

    def test_a_deeper_step_inside_an_earlier_pocket_measures_from_its_floor(self):
        """The live batch's second cut sits inside the first pocket's footprint: swept only
        8 mm from the outer top face it would pass through pure void and the engine would
        reject the batch with "extrude cut removed no material"."""
        ops, notes = self._expand(self.LIVE)
        assert ops[3]["feature"]["params"]["depth"] == 80.0  # 72 floor + 8
        assert any('sits inside "DevicePocket"' in n for n in notes)

    def test_a_pocket_beside_an_earlier_one_keeps_its_own_depth(self):
        ops, notes = self._expand([
            {"op": "pocket", "name": "A", "face": "top", "shape": "rect",
             "width": 20, "height": 20, "depth": 10},
            {"op": "pocket", "name": "B", "face": "top", "shape": "rect",
             "width": 5, "height": 5, "depth": 3, "center": [40, 0]}])
        assert ops[3]["feature"]["params"]["depth"] == 3.0
        assert not any("sits inside" in n for n in notes)

    def test_a_pocket_on_another_face_keeps_its_own_depth(self):
        ops, _notes = self._expand([
            {"op": "pocket", "name": "A", "face": "top", "shape": "rect",
             "width": 20, "height": 20, "depth": 10},
            {"op": "pocket", "name": "B", "face": "front", "shape": "rect",
             "width": 5, "height": 5, "depth": 3}])
        assert ops[3]["feature"]["params"]["depth"] == 3.0

    def test_a_same_batch_placeholder_after_an_expansion_is_renumbered(self):
        """Each expansion inserts an op, so a "$N" written against the ORIGINAL indices
        would silently point one feature too early."""
        ops, _notes = self._expand([
            {"op": "pocket", "name": "P", "face": "top", "shape": "rect",
             "width": 20, "height": 10, "depth": 4},
            {"op": "add_feature", "feature": {
                "type": "fillet", "name": "R", "refs": {"edges": ["$0"]},
                "params": {"radius": 1}}}])
        assert ops[2]["feature"]["refs"]["edges"] == ["$1"]  # the extrude, not its sketch

    def test_a_batch_with_no_pocket_is_the_very_same_list(self):
        ops = [_CANON_BOX]
        assert M._expand_face_pockets(ops, []) is ops


class TestUnknownOpSuggestions:
    """op:"cut" reported «closest: eq_set, rename, set_units, eq_delete, set_folder» (live
    copilot run, 2026-07-28) — five unrelated ops, because the levenshtein fallback scored
    every short name. A misleading suggestion list is worse than none."""

    def test_an_unrelated_query_suggests_nothing(self):
        from services.mcp.op_registry import suggest_doc_ops
        for q in ("cut", "pocket", "zzzzz"):
            assert suggest_doc_ops(q) == [], q

    def test_a_genuine_typo_still_suggests(self):
        from services.mcp.op_registry import suggest_doc_ops
        assert suggest_doc_ops("add_featur")[0] == "add_feature"
        assert suggest_doc_ops("set_materal")[0] == "set_material"
        assert suggest_doc_ops("renmae") == ["rename"]
        assert suggest_doc_ops("delete_fature") == ["delete_feature"]
        assert "eq_set" in suggest_doc_ops("eq_st")

    def test_a_feature_type_still_pins_add_feature(self):
        from services.mcp.op_registry import suggest_doc_ops
        assert suggest_doc_ops("box") == ["add_feature"]

    def test_the_unknown_op_message_teaches_the_shape_instead_of_noise(self):
        from services.mcp.op_registry import unknown_doc_op_message
        msg = unknown_doc_op_message(1, "cut")
        # No suggestion list at all — the vocabulary names still appear, but only inside
        # the teaching sentence, never as "closest: …" near misses to go chase.
        assert "closest" not in msg
        assert 'params.mode:"cut"' in msg
        assert '"op":"add_feature"' in msg

    def test_a_feature_type_op_keeps_its_own_teaching_branch(self):
        from services.mcp.op_registry import unknown_doc_op_message
        msg = unknown_doc_op_message(0, "box")
        assert '"type":"box"' in msg
        assert "closest: add_feature" in msg


@enginetest
def test_live_flat_face_pocket_batch_builds_on_a_stock_block():
    """The VERBATIM two-op batch from the live run, on the 100x60x80 stock it was aimed at.
    Both elements used to be batch-killing rejections; the pair must BUILD and remove
    exactly 51.6*22.5*72 (the pocket) + 14*10*8 (the slot through its floor)."""
    from services.agent.agent_turn import _bind_tool
    new_part, add_features = _bind_tool("new_part"), _bind_tool("add_features")
    doc = json.loads(new_part(name="flat_face_pocket"))["doc_id"]
    assert json.loads(add_features(doc_id=doc, ops=[{"op": "add_feature", "feature": {
        "type": "box", "name": "Stock",
        "params": {"length": 100, "width": 60, "height": 80}}}]))["ok"]
    r = json.loads(add_features(doc_id=doc, ops=TestFlatFacePocket.LIVE))
    assert r["ok"], r
    assert r["changes"]["volume_delta_mm3"] == -(51.6 * 22.5 * 72 + 14 * 10 * 8)
    text = " ".join(r["interpreted"])
    assert "expanded the flat pocket spec" in text
    assert 'read op:"cut"' in text


@enginetest
def test_live_a_pocket_deeper_than_the_stock_is_not_clamped():
    """A cut deeper than the block is legitimate — it just breaks through the bottom."""
    from services.agent.agent_turn import _bind_tool
    new_part, add_features = _bind_tool("new_part"), _bind_tool("add_features")
    doc = json.loads(new_part(name="deep_pocket"))["doc_id"]
    add_features(doc_id=doc, ops=[{"op": "add_feature", "feature": {
        "type": "box", "name": "Stock",
        "params": {"length": 40, "width": 40, "height": 20}}}])
    r = json.loads(add_features(doc_id=doc, ops=[
        {"op": "pocket", "name": "ThroughSlot", "face": "top", "shape": "rect",
         "width": 10, "height": 10, "depth": 200}]))
    assert r["ok"], r
    assert r["changes"]["volume_delta_mm3"] == -(10 * 10 * 20)  # only the stock is there


# ------------------------------------------------------- prose EDGE refs (live 2026-07-28)

class TestProseEdgeRefs:
    """The four-rung rejection ladder a live copilot climbed asking for a fillet on
    "the vertical edges" of a keychain body — each rejection taught exactly one more
    validation layer, and the turn died before it reached the top. The engine already
    resolves the geometry (picks.cpp kQueryGrammar of/side/parallel; parse_axis_vec reads
    "vertical" as z), so every rung is dialect, not a missing capability."""

    #: The VERBATIM op from the live run.
    LIVE = {"op": "add_feature", "feature": {
        "type": "fillet", "name": "CornerRound",
        "params": {"radius": {"value": 12, "expr": None}},
        "refs": {"edges": {"query": "the vertical edges"}}}}

    @staticmethod
    def _refs(refs, ftype="fillet"):
        notes: list = []
        out = M._normalize_feature_refs(
            {"type": ftype, "name": "CornerRound", "refs": refs}, ftype, notes)
        return out["refs"], notes

    def test_the_live_query_wrapper_becomes_a_vertical_edge_query(self):
        refs, notes = self._refs(dict(self.LIVE["feature"]["refs"]))
        assert refs["edges"] == [{"of": "edge", "parallel": "z"}]
        text = " ".join(notes)
        assert "the vertical edges" in text and '{"of":"edge","parallel":"z"}' in text

    def test_rung1_a_bare_dict_in_a_list_slot_is_wrapped(self):
        refs, notes = self._refs({"edges": {"of": "edge", "parallel": "z"}})
        assert refs["edges"] == [{"of": "edge", "parallel": "z"}]
        assert any("takes a LIST" in n for n in notes)

    def test_rung2_a_bare_prose_string_becomes_the_query(self):
        refs, _ = self._refs({"edges": ["the vertical edges"]})
        assert refs["edges"] == [{"of": "edge", "parallel": "z"}]

    def test_rung3_a_query_wrapper_inside_the_list_becomes_the_query(self):
        refs, _ = self._refs({"edges": [{"query": "the vertical edges"}]})
        assert refs["edges"] == [{"of": "edge", "parallel": "z"}]

    def test_rung4_a_query_wrapper_around_a_query_dict_is_unwrapped(self):
        refs, _ = self._refs({"edges": [{"query": {"of": "edge", "parallel": "z"}}]})
        assert refs["edges"] == [{"of": "edge", "parallel": "z"}]

    def test_a_side_word_reuses_the_face_side_vocabulary(self):
        refs, _ = self._refs({"edges": ["the top edges"]})
        assert refs["edges"] == [{"of": "edge", "side": "top"}]

    def test_all_edges_is_the_unfiltered_query(self):
        for phrase in ("all edges", "every edge"):
            refs, _ = self._refs({"edges": [phrase]})
            assert refs["edges"] == [{"of": "edge"}], phrase

    def test_horizontal_is_two_axes_so_it_is_left_for_the_engine(self):
        feat = {"type": "fillet", "name": "R", "refs": {"edges": ["the horizontal edges"]}}
        assert M._normalize_feature_refs(feat, "fillet", []) is feat

    def test_an_unmappable_phrase_is_left_for_the_engine(self):
        for phrase in ("the weird edges", "the vertical bits"):
            feat = {"type": "fillet", "name": "R", "refs": {"edges": [phrase]}}
            assert M._normalize_feature_refs(feat, "fillet", []) is feat, phrase

    def test_an_unmappable_query_wrapper_is_not_silently_dropped(self):
        refs, _ = self._refs({"edges": [{"query": "the weird edges"}]})
        assert refs["edges"] == [{"query": "the weird edges"}]

    def test_a_canonical_query_list_passes_through_byte_identical(self):
        feat = {"type": "fillet", "name": "R",
                "refs": {"edges": [{"of": "edge", "parallel": "z"}]}}
        notes: list = []
        assert M._normalize_feature_refs(feat, "fillet", notes) is feat
        assert notes == []

    def test_a_face_phrase_works_the_same_way_in_a_face_slot(self):
        refs, _ = self._refs({"faces": ["the top face"]}, ftype="draft")
        assert refs["faces"] == [{"of": "face", "side": "top"}]


@enginetest
def test_live_the_vertical_edges_fillet_builds_on_a_real_box():
    """The verbatim live payload, through the real resolver, against the real kernel."""
    from services.agent.agent_turn import _bind_tool
    new_part, add_features = _bind_tool("new_part"), _bind_tool("add_features")
    doc = json.loads(new_part(name="keychain_body"))["doc_id"]
    r0 = json.loads(add_features(doc_id=doc, ops=[{"op": "add_feature", "feature": {
        "type": "box", "name": "Body",
        "params": {"length": 60, "width": 40, "height": 8}}}]))
    assert r0["ok"], r0
    r = json.loads(add_features(doc_id=doc, ops=[TestProseEdgeRefs.LIVE]))
    assert r["ok"], r
    # A 12mm fillet on the four vertical edges of a 60x40 body removes real material.
    assert r["changes"]["volume_delta_mm3"] < 0, r["changes"]
    assert any("the vertical edges" in n for n in r["interpreted"]), r["interpreted"]


# ------------------------------------------------------------- ref-resolution geometry hint
#
# The live failure this defends against: a fillet on "the vertical edges" was rejected
# four times and the model never once could SEE what edges the part actually had. A
# ref-RESOLUTION failure (no such edge/face, a query matching nothing, a prose phrase the
# normalizer declined to guess at) now carries a real, capped inventory of the document's
# actual geometry — never a syntax failure, which the C++ validator already teaches.

class TestRefResolutionGeometryHint:
    def test_ref_failure_kind_reads_edge_resolution_markers(self):
        assert M._ref_failure_kind("f2 (Round): fillet: edge query matched no edges") == "edge"
        assert M._ref_failure_kind("hole: query matched no face") == "face"
        assert M._ref_failure_kind("edges[0]: face selector dangling — unresolved") == "edge"

    def test_ref_failure_kind_is_none_for_a_syntax_rejection(self):
        # "unknown ref slot" / "must be a non-empty list" are SYNTAX errors the C++
        # validator already teaches in full — never re-taught here.
        assert M._ref_failure_kind(
            "features[0].refs.remove: unknown ref slot for shell") is None
        assert M._ref_failure_kind(
            "features[0](f1).refs.edges: must be a non-empty list") is None

    def test_declined_geom_phrases_finds_a_leftover_edge_phrase(self):
        ops = [{"op": "add_feature", "feature": {
            "type": "fillet", "name": "R", "refs": {"edges": "horizontal edges"}}}]
        found = M._declined_geom_phrases(ops)
        assert found == [("edge", "edges", "horizontal edges")]

    def test_declined_geom_phrases_ignores_already_canonical_refs(self):
        ops = [{"op": "add_feature", "feature": {
            "type": "fillet", "name": "R",
            "refs": {"edges": [{"of": "edge", "parallel": "z"}]}}}]
        assert M._declined_geom_phrases(ops) == []

    def test_inventory_line_summarizes_and_caps_many_buckets(self, monkeypatch):
        # 9 distinct surf buckets (well past the cap) + a large axis-aligned bucket, so
        # both the "roll the tail into N other" AND the "biggest buckets first" behaviour
        # are exercised — a lattice part's hundreds of edges must summarize, never dump.
        matches = [{"surf": "line", "axis": [0, 0, 1]} for _ in range(40)]
        for i, surf in enumerate(["circle", "ellipse", "bspline", "bezier", "offset",
                                   "hyperbola", "parabola", "other1", "other2"]):
            matches.append({"surf": surf, "axis": None})
        monkeypatch.setattr(M.cb, "call",
                             lambda op, args: {"matches": matches} if op == "doc_query"
                             else (_ for _ in ()).throw(AssertionError(op)))
        line = M._geometry_inventory_line("doc-x", "edge")
        assert line is not None
        assert "49 edges" in line
        assert "40 parallel to z (vertical)" in line
        assert "other edge(s)" in line  # the tail past the cap is rolled up, not enumerated
        # Bounded — never a per-edge dump.
        bucket_clause = line.split(" — ")[1]  # "40 parallel to z…, 1 bezier…, 4 other edge(s)"
        assert len(bucket_clause.split(", ")) <= M._INVENTORY_BUCKET_CAP + 1  # +1 = the roll-up

    def test_inventory_line_reports_empty_body_honestly(self, monkeypatch):
        monkeypatch.setattr(M.cb, "call", lambda op, args: {"matches": []})
        line = M._geometry_inventory_line("doc-x", "face")
        assert "no faces at all" in line

    def test_enrich_only_fires_on_a_failed_dict_result(self):
        ok_result = {"ok": True}
        assert M._enrich_ref_resolution_error(ok_result, "doc-x", []) is ok_result
        assert M._enrich_ref_resolution_error("not a dict", "doc-x", []) == "not a dict"

    def test_enrich_leaves_a_syntax_error_alone_with_no_declined_phrase(self):
        result = {"ok": False, "error": "features[0].refs.edges: must be a non-empty list"}
        out = M._enrich_ref_resolution_error(result, "doc-x", [{"op": "add_feature",
            "feature": {"type": "fillet", "name": "R", "params": {}}}])
        assert "geometry_hint" not in out
        assert out == result


@enginetest
def test_live_a_zero_match_edge_query_names_the_real_inventory():
    """A structured query that matches NOTHING useful (no circular edges on a plain box)
    now teaches the real edge roster instead of a flat rejection."""
    from services.agent.agent_turn import _bind_tool
    new_part, add_features = _bind_tool("new_part"), _bind_tool("add_features")
    doc = json.loads(new_part(name="zero_match_edge_query"))["doc_id"]
    r0 = json.loads(add_features(doc_id=doc, ops=[{"op": "add_feature", "feature": {
        "type": "box", "name": "Box1",
        "params": {"length": 40, "width": 30, "height": 20}}}]))
    assert r0["ok"], r0
    r = json.loads(add_features(doc_id=doc, ops=[{"op": "add_feature", "feature": {
        "type": "fillet", "name": "CornerRound", "params": {"radius": {"value": 12, "expr": None}},
        "refs": {"edges": [{"of": "edge", "surf": "circle", "parallel": "z"}]}}}]))
    assert not r["ok"], r
    assert "matched no edges" in r["error"]
    assert "geometry_hint" in r, r
    assert "12 edges" in r["geometry_hint"]
    assert "parallel to z" in r["geometry_hint"]
    print("LIVE zero-match edge query error:", r["error"])


@enginetest
def test_live_horizontal_edges_names_the_candidate_orientations():
    """"horizontal edges" is genuinely ambiguous (x or y) and the normalizer declines to
    guess — the resulting rejection now lists the actual candidate orientations so the
    model can pick one, instead of guessing blind."""
    from services.agent.agent_turn import _bind_tool
    new_part, add_features = _bind_tool("new_part"), _bind_tool("add_features")
    doc = json.loads(new_part(name="horizontal_edges_ambiguous"))["doc_id"]
    r0 = json.loads(add_features(doc_id=doc, ops=[{"op": "add_feature", "feature": {
        "type": "box", "name": "Box1",
        "params": {"length": 40, "width": 30, "height": 20}}}]))
    assert r0["ok"], r0
    r = json.loads(add_features(doc_id=doc, ops=[{"op": "add_feature", "feature": {
        "type": "fillet", "name": "HFillet", "params": {"radius": {"value": 3, "expr": None}},
        "refs": {"edges": "horizontal edges"}}}]))
    assert not r["ok"], r
    assert "geometry_hint" in r, r
    assert "parallel to x" in r["geometry_hint"]
    assert "parallel to y" in r["geometry_hint"]
    print("LIVE horizontal-edges ambiguous error:", r["error"])
    print("LIVE horizontal-edges geometry_hint:", r["geometry_hint"])


@enginetest
def test_live_the_29_circle_pattern_sketch_builds():
    """The VERBATIM live payload (2026-07-28): a keychain hole-lattice sketch written
    entirely in cx/cy/r shorthand. The engine SIGSEGV on this shape is fixed separately;
    this proves the shape itself now normalizes into a real, buildable sketch."""
    from services.agent.agent_turn import _bind_tool
    new_part, add_features = _bind_tool("new_part"), _bind_tool("add_features")
    doc = json.loads(new_part(name="keychain_pattern"))["doc_id"]
    entities = [{"type": "circle", "cx": -23 + i * 1.5, "cy": 0, "r": 2.5 - i * 0.01}
                for i in range(29)]
    r = json.loads(add_features(doc_id=doc, ops=[{"op": "add_feature", "feature": {
        "type": "sketch", "name": "PatternSk",
        "params": {"plane": "XY", "entities": entities}}}]))
    assert r["ok"], r
    assert any("expanded flat entity shorthand" in n for n in r["interpreted"]), r["interpreted"]
    tree = json.loads(_bind_tool("get_part")(doc_id=doc))
    sk_ents = tree["tree"]["features"][0]["params"]["entities"]
    assert len(sk_ents) == 29
    assert all("center" in e and "radius" in e for e in sk_ents)
    print("LIVE 29-circle PatternSk interpreted:", r["interpreted"])
    print("LIVE 29-circle PatternSk changes:", r.get("changes"))
