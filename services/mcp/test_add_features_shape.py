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
