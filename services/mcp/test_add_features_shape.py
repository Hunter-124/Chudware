"""add_features ARGUMENT-SHAPE + validation-miss normalizer tests.

Every case here is a real call shape mined from recorded agent-run ledgers — the ways frontier models actually phrase a
build call, each of which used to burn a whole turn on a rejection. The normalizers are
pure functions, so most of this runs without an engine; the end-to-end cases go through
``services.agent.agent_turn._bind_tool`` (the BUILT-IN agent's dispatch path, which calls
the tool function directly and bypasses FastMCP) and through FastMCP itself.

Run:  python3 -m pytest services/mcp/test_add_features_shape.py -q -p no:randomly
"""
from __future__ import annotations

import asyncio
import json
import math
import sys
import uuid
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


@pytest.mark.parametrize("alias", ["id", "feature_id", "feature", "target"])
def test_set_rollback_reads_a_target_spelling_as_after(alias):
    """c259: these reached the engine as ignored keys; the bar never moved, ok:true."""
    notes: list = []
    ops = M._normalize_op_elements([{"op": "set_rollback", alias: "f30"}], notes)
    assert ops == [{"op": "set_rollback", "after": "f30"}]
    assert notes


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"op": "suppress_feature", "id": "f7"},
         {"op": "set_suppressed", "id": "f7", "value": True}),
        ({"op": "unsuppress_feature", "id": "f7"},
         {"op": "set_suppressed", "id": "f7", "value": False}),
        ({"op": "set_suppressed", "id": "f7", "suppressed": True},
         {"op": "set_suppressed", "id": "f7", "value": True}),
    ],
)
def test_suppression_dialect_rewrites_to_canonical_value(payload, expected):
    notes: list = []
    assert M._normalize_op_elements([payload], notes) == [expected]
    assert notes


@pytest.mark.parametrize(
    "payload",
    [
        {"op": "suppress_feature", "id": "f7", "value": False},
        {"op": "unsuppress_feature", "id": "f7", "suppressed": True},
        {"op": "set_suppressed", "id": "f7", "value": False, "suppressed": True},
    ],
)
def test_conflicting_suppression_spellings_are_rejected(payload):
    with pytest.raises(ValueError, match="conflicting"):
        M._normalize_op_elements([payload], [])


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


_CANON_HOLE = {"diameter": 5, "style": "simple", "end": "through_all"}


@pytest.mark.parametrize("key", ["at", "origin", "position", "center"])
def test_a_world_hole_point_becomes_world_points_not_face_local_positions(key):
    """A world [x,y,z] is NOT [u,v] (those are measured from the face centre): it must reach
    the engine as world_points, never as positions=[[x,y]] with z dropped."""
    notes: list = []
    out = M._normalize_hole_feature(
        {"type": "hole", "name": "B", "params": {**_CANON_HOLE, key: [30, 20, 0]},
         "refs": {"face": {"of": "face", "side": "bottom"}}}, notes)
    assert out["params"]["world_points"] == [[30.0, 20.0, 0.0]]
    assert "positions" not in out["params"] and key not in out["params"]
    assert any("world_points" in n and "bore axis" in n for n in notes), notes


def test_a_world_origin_hole_point_is_kept_not_dropped_as_the_face_centre():
    out = M._normalize_hole_feature(
        {"type": "hole", "name": "B", "params": {**_CANON_HOLE, "origin": [0, 0, 0]}}, [])
    assert out["params"]["world_points"] == [[0.0, 0.0, 0.0]]


def test_xyz_triples_under_positions_become_world_points():
    out = M._normalize_hole_feature(
        {"type": "hole", "name": "B",
         "params": {**_CANON_HOLE, "positions": [[30, 20, 0], [50, 20, 0]]}}, [])
    assert out["params"]["world_points"] == [[30.0, 20.0, 0.0], [50.0, 20.0, 0.0]]
    assert "positions" not in out["params"]


def test_face_local_uv_positions_and_explicit_world_points_are_left_alone():
    for params in ({**_CANON_HOLE, "positions": [[3, 4], [5, 6]]},
                   {**_CANON_HOLE, "world_points": [[1, 2, 3]]}):
        feat = {"type": "hole", "name": "B", "params": params,
                "refs": {"face": {"of": "face", "side": "top"}}}
        notes: list = []
        assert M._normalize_hole_feature(feat, notes) is feat
        assert notes == []


def test_a_world_bolt_circle_centre_becomes_world_center():
    notes: list = []
    out = M._normalize_bolt_circle_feature(
        {"type": "bolt_circle", "name": "R",
         "params": {"diameter": 3, "count": 4, "pcd": 20, "center": [40, 20, 0]}}, notes)
    assert out["params"]["world_center"] == [40.0, 20.0, 0.0]
    assert "center" not in out["params"]
    assert any("world_center" in n for n in notes), notes


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

    def test_rect_x1_y1_x2_y2_becomes_corner1_corner2(self):
        """Live run 2026-07-29 (lexus_f_keychain): all three regions of an embossed "F"
        were written {"kind":"rect","x1":10,"y1":-6,"x2":16,"y2":12}. The `line` branch
        already understood this flat dialect; one slot over it was a batch-killing
        rejection ("rectangle: expected center-form … or two-corner form"), and the
        model had to guess the canonical spelling on a second call."""
        ents, notes = self._norm([{"kind": "rect", "x1": 10, "y1": -6,
                                   "x2": 16, "y2": 12}])
        assert ents[0]["corner1"] == [10, -6] and ents[0]["corner2"] == [16, 12]
        assert not any(k in ents[0] for k in ("x1", "y1", "x2", "y2"))
        assert any("x1/y1/x2/y2 -> corner1/corner2" in n for n in notes)

    def test_rect_with_width_and_height_keeps_its_center_form(self):
        """width+height already satisfies the engine's center-form, so a stray x1/y1
        must NOT be promoted into corners — that would move a working rectangle."""
        ents, _ = self._norm([{"type": "rectangle", "center": [0, 0], "width": 4,
                               "height": 2, "x1": 99, "y1": 99, "x2": 1, "y2": 1}])
        assert "corner1" not in ents[0]
        assert ents[0]["width"] == 4 and ents[0]["height"] == 2

    def test_rect_with_existing_corners_is_not_rewritten(self):
        ents, _ = self._norm([{"type": "rect", "corner1": [0, 0], "corner2": [5, 5],
                               "x1": 9, "y1": 9, "x2": 8, "y2": 8}])
        assert ents[0]["corner1"] == [0, 0] and ents[0]["corner2"] == [5, 5]

    def test_a_half_written_flat_rect_is_not_guessed(self):
        """Only x1/y1 present: no second corner to infer, so leave it for the engine."""
        ents, _ = self._norm([{"type": "rect", "x1": 10, "y1": -6}])
        assert "corner1" not in ents[0]
        assert ents[0]["x1"] == 10

    def test_a_polygon_is_not_given_rectangle_corners(self):
        """_TWO_CORNER_TYPES is exactly what the ENGINE can build from two corners;
        a polygon carrying x1/y1/x2/y2 would gain fields it never reads."""
        ents, _ = self._norm([{"type": "polygon", "sides": 6, "x1": 0, "y1": 0,
                               "x2": 5, "y2": 5}])
        assert "corner1" not in ents[0]

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
@pytest.mark.parametrize("policy", [{"mode": "new"}, {"merge": False},
                                  {"mode": "add", "merge": False}])
def test_live_new_body_preserves_touching_and_overlapping_bodies(policy):
    from services.agent.agent_turn import _bind_tool
    add = _bind_tool("add_features")
    doc = _cb.call("doc_new", {"name": "independent_body"})["doc_id"]
    def box(x, **options):
        return {"type": "box", "params": {"length": 10, "width": 10, "height": 10},
                "at": [x, 0, 0], **options}
    try:
        first = json.loads(add(doc_id=doc, ops=[box(0, merge=False), box(10, merge=False)]))
        assert first["ok"] and first["body_count"] == 2, first
        before = [_cb.call("eval_mass", {"doc_id": doc, "body": i})["volume_mm3"]
                  for i in (1, 2)]
        added = json.loads(add(doc_id=doc, ops=[box(15, **policy)]))
        assert added["ok"] and added["body_count"] == 3, added
        after = [_cb.call("eval_mass", {"doc_id": doc, "body": i})["volume_mm3"]
                 for i in (1, 2)]
        assert before == pytest.approx([1000, 1000])
        assert after == pytest.approx(before)
        assert added["volume_mm3"] == pytest.approx(3000)
    finally:
        _cb.call("doc_close", {"doc_id": doc})


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
    from services.mcp import server
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
    assert any("resolved face/edge sid" in n and sid in n for n in r["interpreted"])


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
    assert any("resolved face/edge sid" in n and sid in n for n in r["interpreted"])


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


# Live c245 (GPT-6.1-Sol): one closed polyline "e1" pinned by {"type":"fix","refs":["e1"]}.
# The expansion consumed "e1" and the dangling-ref check then refused the model's own valid
# reference, so the fan-out meant for exactly this case never ran.
_C245_POINTS = [[-16, 0], [16, 0], [16, 2.4], [8.5, 2.4], [8.5, 6], [4, 6], [4, 2.4],
                [-4, 2.4], [-4, 6], [-8.5, 6], [-8.5, 2.4], [-16, 2.4]]


def _c245_sketch(constraints):
    return {"op": "add_feature", "feature": {
        "type": "sketch", "name": "cassette_profile", "refs": {"plane": "plane.front"},
        "params": {"entities": [{"id": "e1", "closed": True, "construction": False,
                                 "type": "polyline", "points": _C245_POINTS}],
                   "constraints": constraints}}}


def test_live_c245_fix_on_a_polyline_pins_every_expanded_segment():
    notes: list = []
    ops = [_c245_sketch([{"id": "c1", "refs": ["e1"], "type": "fix"}]),
           {"op": "add_feature", "feature": {
               "type": "extrude", "name": "cassette_body",
               "refs": {"profile": {"kind": "region", "sketch": "$0", "centroid": [0, 1]}},
               "params": {"mode": "boss", "distance": 10}}}]
    ops = M._normalize_op_elements(ops, notes)
    ops = M._normalize_feature_types(ops, notes)
    ops = M._normalize_sketch_ops(ops, notes)
    ops = M._normalize_feature_ops(ops, notes)
    sketch, extrude = (o["feature"] for o in ops)
    ents = sketch["params"]["entities"]
    seg_ids = [e["id"] for e in ents]
    assert len(ents) == 12 and all(e["type"] == "line" for e in ents)
    cons = sketch["params"]["constraints"]
    assert [c["type"] for c in cons] == ["fix"] * 12
    assert [c["refs"] for c in cons] == [[i] for i in seg_ids]
    assert len({c["id"] for c in cons}) == 12
    assert [p["sketch"] for p in extrude["refs"]["profile"]] == ["$0"]
    assert any('"fix" on polyline "e1"' in n for n in notes)


def test_a_non_fix_relation_on_a_whole_polyline_teaches_the_segment_ids():
    with pytest.raises(ValueError) as ei:
        _sketch_ops(_c245_sketch([{"id": "c1", "refs": ["e1"], "type": "horizontal"}]))
    msg = str(ei.value)
    assert "e2 [-16, 0]->[16, 0]" in msg and "e13 [-16, 2.4]->[-16, 0]" in msg


def test_a_dangling_ref_lists_entity_ids_in_numeric_order():
    with pytest.raises(ValueError) as ei:
        _sketch_ops(_c245_sketch([{"id": "c1", "refs": ["e99"], "type": "fix"}]))
    msg = str(ei.value)
    assert "e2, e3, e4, e5, e6, e7, e8, e9, e10, e11, e12, e13" in msg
    assert 'polyline "e1" became lines e2..e13' in msg


def test_rename_without_a_feature_id_is_refused_before_the_engine():
    with pytest.raises(ValueError) as ei:
        M._normalize_op_elements([{"op": "rename", "name": "aa_battery_cassette"}], [])
    assert "new_part" in str(ei.value)
    op = {"op": "rename", "id": "f1", "name": "Base"}
    assert M._normalize_op_elements([op], []) == [op]


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

# --------------------------------------------------------- the kimi-keychain batch (2026-07-28)
# Ledger: ~/.local/share/Chudware/cache/runs/run-93714991526393/events.jsonl. TWO independent
# defects made the same four-feature batch unbuildable, twice in a row, on a basic part:
#   * params.shapes=[…] was never read as the entity list, so the sketch was created EMPTY and
#     the extrudes referencing it died on "profile sketch has no closed face";
#   * refs.profile="HexPattern" (a bare same-batch NAME, not a list) was left verbatim because
#     `profile` was missing from the refs key allowlist -> "is neither a reserved datum…".
# On top of both, every polygon center was an OBJECT ({"x":20,"y":8}), which the engine reads
# only as an array — silently building all seven hexagons at the origin.

_HEXES = [{"type": "polygon", "sides": 6, "center": {"x": 20, "y": 8}, "radius": 4},
          {"type": "polygon", "sides": 6, "center": {"x": 27, "y": 15}, "radius": 4},
          {"type": "polygon", "sides": 6, "center": {"x": 13, "y": 15}, "radius": 3}]
_EMBLEM = [{"type": "polyline", "closed": True,
            "points": [[46, 6], [56, 6], [56, 9], [50, 9], [50, 13], [55, 13],
                       [55, 16], [50, 16], [50, 24], [46, 24]]}]


def test_params_shapes_is_read_as_the_entity_list():
    feat = M._normalize_sketch_feature(
        {"type": "sketch", "name": "HexPattern",
         "params": {"plane": "top", "shapes": list(_HEXES)}}, notes := [])
    ents = feat["params"]["entities"]
    assert len(ents) == len(_HEXES)
    assert [e["type"] for e in ents] == ["polygon"] * 3
    assert [e["center"] for e in ents] == [[20, 8], [27, 15], [13, 15]]
    assert all(isinstance(e["id"], str) and e["construction"] is False for e in ents)
    assert "shapes" not in feat["params"]          # consumed, never left to confuse the engine
    assert any("read params.shapes" in n for n in notes), notes


def test_object_form_points_become_arrays():
    """{"x":…,"y":…} on a polygon center reached the engine as "no array" and silently
    placed the shape at the ORIGIN — wrong geometry, not an error."""
    assert M._xy({"x": 20, "y": 8}) == [20, 8]
    assert M._xy({"u": 3, "v": 4}) == [3, 4]
    assert M._xy([1, 2]) == [1, 2]
    assert M._xy("plane.top") == "plane.top"       # untouched
    feat = M._normalize_sketch_feature(
        {"type": "sketch", "name": "Hex",
         "params": {"plane": "top", "shapes": [dict(_HEXES[0])]}}, [])
    assert feat["params"]["entities"][0]["center"] == [20, 8]


def test_bare_name_in_refs_profile_resolves_to_its_placeholder():
    notes: list = []
    ops = M._resolve_batch_name_refs([
        {"op": "add_feature", "feature": {"type": "sketch", "name": "HexPattern",
                                          "params": {"entities": []}}},
        {"op": "add_feature", "feature": {"type": "extrude", "name": "HexCuts",
                                          "params": {"mode": "cut"},
                                          "refs": {"profile": "HexPattern"}}},
    ], notes)
    assert ops[1]["feature"]["refs"]["profile"] == "$0"
    assert any("resolved same-batch feature name" in n for n in notes), notes


def test_refs_geometry_query_is_not_mistaken_for_a_name():
    """A query object's enum words ("top", "face") must survive even when a feature in the
    batch happens to carry that display name."""
    ops = M._resolve_batch_name_refs([
        {"op": "add_feature", "feature": {"type": "box", "name": "top",
                                          "params": {"length": 1, "width": 1, "height": 1}}},
        {"op": "add_feature", "feature": {"type": "hole", "name": "H",
                                          "refs": {"face": {"of": "face", "side": "top"}}}},
    ], [])
    assert ops[1]["feature"]["refs"]["face"] == {"of": "face", "side": "top"}


@enginetest
@pytest.mark.parametrize("profile_ref", ["HexPattern", ["$0"]])
def test_live_kimi_keychain_hex_batch_builds(profile_ref):
    """The VERBATIM live batch, in both ref dialects the model tried. Both used to fail."""
    from services.agent.agent_turn import _bind_tool
    new_part, add_features = _bind_tool("new_part"), _bind_tool("add_features")
    # fresh=True per parametrization: new_part otherwise hands back the CURRENT document,
    # and the second dialect would re-cut hexes the first already removed.
    doc = json.loads(new_part(name="kimi_keychain", fresh=True))["doc_id"]
    add_features(doc_id=doc, ops=[{"op": "add_feature", "feature": {
        "type": "box", "name": "Plate",
        "params": {"length": 60, "width": 30, "height": 4}}}])
    emblem_ref = "FEmblem" if isinstance(profile_ref, str) else ["$2"]
    r = json.loads(add_features(doc_id=doc, ops=[
        {"op": "add_feature", "feature": {
            "type": "sketch", "name": "HexPattern",
            "params": {"plane": "top face", "shapes": list(_HEXES)}}},
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": "HexCuts",
            "params": {"depth": {"value": 4}, "mode": "cut"},
            "refs": {"profile": profile_ref}}},
        {"op": "add_feature", "feature": {
            "type": "sketch", "name": "FEmblem",
            "params": {"plane": "top face", "shapes": list(_EMBLEM)}}},
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": "FBoss",
            "params": {"depth": {"value": 2}, "mode": "boss"},
            "refs": {"profile": emblem_ref}}},
    ]))
    assert r["ok"], r
    tree = json.loads(_bind_tool("get_part")(doc_id=doc))
    hexes = next(f for f in tree["tree"]["features"] if f.get("name") == "HexPattern")
    ents = hexes["params"]["entities"]
    assert len(ents) == 18, ents          # 3 hexagons x 6 line segments
    xs = [p[0] for e in ents for p in (e["p1"], e["p2"])]
    # Centers honoured: the lattice spans the plate, it is not collapsed on the origin.
    assert min(xs) > 5 and max(xs) > 25, (min(xs), max(xs))


# ------------------------------------------------------------------ live IWB holster (2026-08-04)
# Ledger: ~/.local/share/Chudware/cache/runs/run-135571474839343/events.jsonl. The model's
# VERBATIM sketch op used three coherent dialect spellings together: a singular `profile`
# entity-list alias, `kind` plus `from`/`to` lines, and a bulge-defined arc.
_HOLSTER_OUTLINE_OPS = [{"op": "add_feature", "feature": {
    "type": "sketch", "name": "HolsterOutline", "plane": "XY", "params": {"profile": [
        {"kind": "line", "from": [-6, 0], "to": [132, 0]},
        {"kind": "line", "from": [132, 0], "to": [132, 58]},
        {"kind": "line", "from": [132, 58], "to": [72, 58]},
        {"kind": "line", "from": [72, 58], "to": [72, 95]},
        {"kind": "arc", "from": [72, 95], "to": [30, 90], "bulge": -0.35},
        {"kind": "line", "from": [30, 90], "to": [-6, 80]},
        {"kind": "line", "from": [-6, 80], "to": [-6, 0]},
    ]}}}]


def test_live_holster_profile_from_to_and_bulge_normalize_without_losing_a_segment():
    feat = M._normalize_sketch_ops(_HOLSTER_OUTLINE_OPS, [])[0]["feature"]
    ents = feat["params"]["entities"]
    assert [e["type"] for e in ents] == [
        "line", "line", "line", "line", "arc", "line", "line"]
    assert [e["id"] for e in ents] == [f"e{i}" for i in range(1, 8)]
    assert all(e["construction"] is False for e in ents)
    assert ents[:4] == [
        {"type": "line", "p1": [-6, 0], "p2": [132, 0],
         "id": "e1", "construction": False},
        {"type": "line", "p1": [132, 0], "p2": [132, 58],
         "id": "e2", "construction": False},
        {"type": "line", "p1": [132, 58], "p2": [72, 58],
         "id": "e3", "construction": False},
        {"type": "line", "p1": [72, 58], "p2": [72, 95],
         "id": "e4", "construction": False},
    ]
    assert ents[5:] == [
        {"type": "line", "p1": [30, 90], "p2": [-6, 80],
         "id": "e6", "construction": False},
        {"type": "line", "p1": [-6, 80], "p2": [-6, 0],
         "id": "e7", "construction": False},
    ]
    arc = ents[4]
    assert set(arc) == {"type", "id", "construction", "center", "radius",
                        "start", "end", "ccw"}
    assert arc["center"] == pytest.approx([47.86607142857143, 118.825])
    assert arc["radius"] == pytest.approx(33.91278716488541)
    assert arc["start"] == pytest.approx(315.36906701281623)
    assert arc["end"] == pytest.approx(238.20888213606133)
    assert arc["ccw"] is False

    # Recover the canonical arc's endpoints and prove every segment meets the next,
    # including the last-to-first closure. This catches a silently dropped/reordered arc.
    def endpoints(entity):
        if entity["type"] == "line":
            return entity["p1"], entity["p2"]
        center, radius = entity["center"], entity["radius"]
        point = lambda angle: [
            center[0] + radius * math.cos(math.radians(angle)),
            center[1] + radius * math.sin(math.radians(angle))]
        return point(entity["start"]), point(entity["end"])

    pairs = [endpoints(e) for e in ents]
    for (_, end), (start, _) in zip(pairs, pairs[1:] + pairs[:1]):
        assert end == pytest.approx(start)


@pytest.mark.parametrize("alias", [
    "shape", "shapes", "profile", "profiles", "geometry",
    "curve", "curves", "element", "elements", "entity", "segment", "segments",
])
def test_unambiguous_entity_list_aliases_are_consumed(alias):
    feat = M._normalize_sketch_feature({
        "type": "sketch", "name": "AliasSk", "params": {
            alias: [{"kind": "line", "from": [0, 0], "to": [5, 0]}]}}, [])
    assert feat["params"]["entities"] == [{
        "type": "line", "p1": [0, 0], "p2": [5, 0],
        "id": "e1", "construction": False}]
    assert alias not in feat["params"]


def test_geometry_under_an_unreadable_key_is_rejected_not_silently_dropped():
    """A sketch created EMPTY reports success and then poisons the extrude that
    references it with "profile sketch has no closed face" — an error naming neither
    the cause nor a fix. `from`/`to` still make these unmistakably line entities."""
    with pytest.raises(ValueError) as ei:
        M._normalize_sketch_ops([{"op": "add_feature", "feature": {
            "type": "sketch", "name": "Sk",
            "params": {"plane": "top",
                       "loops": [{"kind": "line", "from": [0, 0], "to": [5, 0]}]}}}], [])
    assert "params.loops" in str(ei.value)
    assert "params.entities" in str(ei.value)


def test_a_deliberately_empty_sketch_still_normalizes():
    """Empty is legal — only geometry we could not READ is a rejection."""
    feat = M._normalize_sketch_feature(
        {"type": "sketch", "name": "Empty", "params": {"plane": "top", "entities": []}}, [])
    assert feat["params"]["entities"] == []
    assert feat["params"]["constraints"] == []


# ---------------------------------------------------------------------------
# Live run run-96954661041418 (2026-07-28). Three add_features calls, three distinct
# rejections, one abandoned part. Every payload below is verbatim from that ledger.

def test_entity_kind_is_read_as_the_shape_word():
    """Six valid lines saying "kind" instead of "type" were rejected one by one as
    "not a sketch entity type" — a whole hexagon lost to a synonym."""
    feat = M._normalize_sketch_feature({"type": "sketch", "name": "Hex1", "params": {
        "plane": "XY", "entities": [
            {"kind": "line", "p1": [30, 0], "p2": [15, 25.98]},
            {"kind": "line", "p1": [15, 25.98], "p2": [-15, 25.98]}]}}, [])
    ents = feat["params"]["entities"]
    assert [e["type"] for e in ents] == ["line", "line"]
    assert not any("kind" in e for e in ents)
    assert ents[0]["p1"] == [30, 0] and ents[0]["p2"] == [15, 25.98]


@pytest.mark.parametrize("alias", ["kind", "shape", "entity", "entity_type", "etype"])
def test_every_entity_type_synonym_is_read(alias):
    feat = M._normalize_sketch_feature({"type": "sketch", "name": "S", "params": {
        "plane": "top", "entities": [{alias: "circle", "center": [0, 0], "radius": 4}]}}, [])
    assert feat["params"]["entities"][0]["type"] == "circle"


def test_an_explicit_type_beats_a_synonym():
    """`kind` is also a legitimate word inside selectors — it must never override a
    `type` the model actually wrote."""
    feat = M._normalize_sketch_feature({"type": "sketch", "name": "S", "params": {
        "plane": "top", "entities": [
            {"type": "circle", "kind": "region", "center": [0, 0], "radius": 4}]}}, [])
    assert feat["params"]["entities"][0]["type"] == "circle"


def test_polygon_extrude_becomes_a_polygon_sketch_and_an_extrude():
    """There is no prism primitive, so a model asks for the compound it wishes existed.
    Sides, size and height are all present — that is a complete spec, not a guess."""
    notes = []
    out = M._expand_prisms([{"op": "add_feature", "feature": {
        "type": "polygon_extrude", "name": "Level1",
        "params": {"sides": 6, "radius": {"value": 30, "expr": None},
                   "height": {"value": 20, "expr": None}}}}], notes)
    assert [o["feature"]["type"] for o in out] == ["sketch", "extrude"]
    ent = out[0]["feature"]["params"]["entities"][0]
    assert ent == {"type": "polygon", "sides": 6, "center": [0, 0], "radius": 30}
    assert out[1]["feature"]["params"] == {"mode": "boss", "depth": 20}
    assert out[1]["feature"]["refs"]["profile"][0]["sketch"] == "Level1Profile"
    assert any("polygon_extrude" in n for n in notes)


def test_a_prism_word_implies_its_own_side_count():
    out = M._expand_prisms([{"op": "add_feature", "feature": {
        "type": "hexagonal_prism", "name": "Nut",
        "params": {"across_flats": 17, "height": 6}}}], [])
    ent = out[0]["feature"]["params"]["entities"][0]
    assert ent["sides"] == 6
    # across_flats is the INSCRIBED diameter: circumradius = (17/2) / cos(pi/6).
    assert ent["radius"] == pytest.approx((17 / 2) / math.cos(math.pi / 6))


def test_an_incomplete_prism_is_a_teaching_error_not_a_guess():
    with pytest.raises(ValueError) as ei:
        M._expand_prisms([{"op": "add_feature", "feature": {
            "type": "polygon_extrude", "name": "X", "params": {"sides": 6}}}], [])
    assert "radius" in str(ei.value) and "height" in str(ei.value)


def test_a_plain_extrude_is_not_mistaken_for_a_prism():
    """`extrude` carries the token but is the canonical feature — expanding it would
    invent a profile the model never asked for."""
    ops = [{"op": "add_feature", "feature": {"type": "extrude", "name": "E",
                                             "params": {"depth": 5}}}]
    assert M._expand_prisms(ops, []) is ops


def test_profile_last_resolves_to_the_sketch_just_created():
    """Five stacked sketch/extrude levels, every extrude saying params.sketch:"last" —
    the whole batch was rejected with «profile 'last' -> 'last' not found» x4."""
    notes = []
    out = M._resolve_relative_profile_refs([
        {"op": "add_feature", "feature": {"type": "sketch", "name": "Hex2",
                                          "params": {"plane": "top", "entities": []}}},
        {"op": "add_feature", "feature": {"type": "extrude", "name": "Level2",
                                          "params": {"sketch": "last", "depth": 20}}}], notes)
    assert out[1]["feature"]["params"]["sketch"] == "$0"
    assert any("relative profile word" in n for n in notes)


def test_relative_profile_picks_the_NEAREST_preceding_sketch():
    out = M._resolve_relative_profile_refs([
        {"op": "add_feature", "feature": {"type": "sketch", "name": "A", "params": {}}},
        {"op": "add_feature", "feature": {"type": "sketch", "name": "B", "params": {}}},
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": "E",
            "refs": {"profile": [{"kind": "region", "sketch": "previous"}]}}}], [])
    assert out[2]["feature"]["refs"]["profile"][0]["sketch"] == "$1"


def test_relative_words_are_not_applied_to_face_or_edge_slots():
    """"last" in a topology slot is not this sketch, and guessing there would silently
    select the wrong geometry."""
    ops = [{"op": "add_feature", "feature": {"type": "sketch", "name": "A", "params": {}}},
           {"op": "add_feature", "feature": {"type": "fillet", "name": "F",
                                             "refs": {"edges": ["last"]}}}]
    assert M._resolve_relative_profile_refs(ops, [])[1]["feature"]["refs"]["edges"] == ["last"]


def test_a_relative_word_with_no_preceding_sketch_is_left_alone():
    ops = [{"op": "add_feature", "feature": {"type": "extrude", "name": "E",
                                             "params": {"sketch": "last", "depth": 5}}}]
    assert M._resolve_relative_profile_refs(ops, []) is ops


@enginetest
@pytest.mark.parametrize("case", ["prism", "kind_entities", "profile_last"])
def test_the_live_run_96954661041418_batches_build(case):
    """Each of the three rejected calls, verbatim, must now produce real geometry."""
    from services.agent.agent_turn import _bind_tool
    from services.mcp import cad_common as cc
    doc = json.loads(_bind_tool("new_part")(name=f"live_{case}"))["doc_id"]
    ops = {
        "prism": [{"op": "add_feature", "feature": {
            "type": "polygon_extrude", "name": "Level1",
            "params": {"sides": 6, "radius": {"value": 30, "expr": None},
                       "height": {"value": 20, "expr": None}}}}],
        "kind_entities": [{"op": "add_feature", "feature": {
            "type": "sketch", "name": "Hex1", "params": {"plane": "XY", "entities": [
                {"kind": "line", "p1": [30, 0], "p2": [15, 25.98]},
                {"kind": "line", "p1": [15, 25.98], "p2": [-15, 25.98]},
                {"kind": "line", "p1": [-15, 25.98], "p2": [-30, 0]},
                {"kind": "line", "p1": [-30, 0], "p2": [-15, -25.98]},
                {"kind": "line", "p1": [-15, -25.98], "p2": [15, -25.98]},
                {"kind": "line", "p1": [15, -25.98], "p2": [30, 0]}]}}}],
        "profile_last": [
            {"op": "add_feature", "feature": {"type": "sketch", "name": "Hex2", "params": {
                "plane": "XY", "entities": [
                    {"type": "line", "start": [25.11, 6.73], "end": [6.73, 25.11]},
                    {"type": "line", "start": [6.73, 25.11], "end": [-18.39, 18.39]},
                    {"type": "line", "start": [-18.39, 18.39], "end": [-25.11, -6.73]},
                    {"type": "line", "start": [-25.11, -6.73], "end": [-6.73, -25.11]},
                    {"type": "line", "start": [-6.73, -25.11], "end": [18.39, -18.39]},
                    {"type": "line", "start": [18.39, -18.39], "end": [25.11, 6.73]}]}}},
            {"op": "add_feature", "feature": {"type": "extrude", "name": "Level2", "params": {
                "sketch": "last", "depth": {"value": 20, "expr": None}}}}],
    }[case]
    r = json.loads(_bind_tool("add_features")(doc_id=doc, ops=ops))
    assert r["ok"] is True, r.get("error")
    if case == "kind_entities":
        tree = json.loads(_bind_tool("get_part")(doc_id=doc))["tree"]
        ents = tree["features"][0]["params"]["entities"]
        assert len(ents) == 6 and {e["type"] for e in ents} == {"line"}
        return
    # Both solid cases must be a real hexagonal prism 20 tall, not a sliver or a point.
    size = cc.cb.call("doc_bbox", {"doc_id": doc})["size"]
    assert size[2] == pytest.approx(20, abs=1e-3)
    expect_x = 60.0 if case == "prism" else 50.22
    assert size[0] == pytest.approx(expect_x, abs=1e-2)


@enginetest
def test_live_new_part_resumes_an_unbuilt_same_named_doc_instead_of_forking():
    """Live bug (doc_list on the running daemon showed four docs named
    CD-XXX_TwistedLatticeOrb, ...Orb2, ...Orb3, ...Orb4 for ONE requested part): a general
    agent that retries a brief, or a model3d worker that respawns per turn, calls
    new_part(name=...) again with no memory of the first doc_id. doc_new uniquifies the
    collision and mints a fresh document each time, and every mint fans out doc.opened, so
    the GUI opens another mirror tab. An UNBUILT part of that name has no geometry to
    inherit, so resuming it is unambiguous — and it is exactly the respawn case."""
    from services.agent.agent_turn import _bind_tool
    from services.mcp import cad_common as cc
    new_part = _bind_tool("new_part")
    name = f"WidgetUnbuilt_{uuid.uuid4().hex[:8]}"

    first = json.loads(new_part(name=name))
    assert first["ok"] and "reused" not in first
    doc = first["doc_id"]

    second = json.loads(new_part(name=name))
    assert second["ok"] is True
    assert second["doc_id"] == doc, "an unbuilt part of the same name must be RESUMED"
    assert second.get("reused") is True

    docs = cc.cb.call("doc_list", {}).get("docs", [])
    assert len([d for d in docs if d.get("name") == name]) == 1


@enginetest
def test_live_new_part_resumes_a_built_part_and_says_what_it_inherited():
    """The other half. Forking was the reported bug — the user's tab strip filled with
    "AI: SwingArm4/5/6/7" for ONE part — so a same-named part is resumed even when it
    already has geometry. The hazard that buys is a caller building blind on top of a body
    it does not know is there ("boss added no material — it is buried inside the body"), so
    the response must state the feature count, not just that something was reused."""
    from services.agent.agent_turn import _bind_tool
    new_part = _bind_tool("new_part")
    add_features = _bind_tool("add_features")
    name = f"WidgetBuilt_{uuid.uuid4().hex[:8]}"

    doc = json.loads(new_part(name=name))["doc_id"]
    built = json.loads(add_features(doc_id=doc, ops=[{"op": "add_feature", "feature": {
        "type": "box", "name": "Block",
        "params": {"length": 40, "width": 30, "height": 20}}}]))
    assert built["ok"], built

    second = json.loads(new_part(name=name))
    assert second["ok"] is True
    assert second["doc_id"] == doc, "a same-named part must be RESUMED, never forked"
    assert second.get("reused") is True
    assert second.get("feature_count", 0) >= 1
    assert "ALREADY CARRIES" in second["note"]

    # And the resume must not have zeroed the write-diff baseline.
    again = json.loads(add_features(doc_id=doc, ops=[{"op": "add_feature", "feature": {
        "type": "cylinder", "name": "Post",
        "params": {"radius": 3, "height": 10}, "at": {"of": "face", "side": "top"}}}]))
    assert again["ok"], again
    assert again["changes"]["volume_delta_mm3"] < built["changes"]["volume_delta_mm3"]


@enginetest
def test_live_new_part_fresh_true_mints_a_separate_document():
    from services.agent.agent_turn import _bind_tool
    new_part = _bind_tool("new_part")

    first = json.loads(new_part(name="ForkOnPurpose"))
    second = json.loads(new_part(name="ForkOnPurpose", fresh=True))
    assert first["ok"] and second["ok"]
    assert second.get("reused") is not True
    assert second["doc_id"] != first["doc_id"], (
        "fresh=True must mint a genuinely separate document")


# ---------------------------------------------------------------------------
# Live run run-97813015264134 (2026-07-28): a hollow orb, ten failed calls, no geometry.
# Six distinct classes, one of them a HARNESS CRASH.

def test_a_stringified_feature_is_decoded_not_crashed_on():
    """The model sent a PYTHON REPR where an object belonged and the turn died on
    «'str' object has no attribute 'get'» — our crash, and a message that taught
    nothing. json.loads cannot read it: single quotes and a bare None."""
    notes = []
    ops = M._decode_embedded_structures([{"op": "add_feature", "feature":
        "{'type': 'cylinder', 'name': 'PoleBore', 'params': "
        "{'height': {'value': 90, 'expr': None}}}"}], notes)
    feat = ops[0]["feature"]
    assert isinstance(feat, dict)
    assert feat["type"] == "cylinder" and feat["params"]["height"]["expr"] is None
    assert any("decoded" in n for n in notes)


def test_real_json_in_a_structure_slot_is_decoded_too():
    ops = M._decode_embedded_structures(
        [{"op": "add_feature", "feature": '{"type": "box", "name": "B"}'}], [])
    assert ops[0]["feature"] == {"type": "box", "name": "B"}


@pytest.mark.parametrize("val", ["HexPattern", "top", "cut", "", "not a dict", "{oops"])
def test_a_plain_string_is_never_mangled_by_the_decoder(val):
    """`profile` is a structure slot AND a legitimate sketch-name slot — a name must
    survive untouched, and an unparseable brace-string is data, not our business."""
    ops = [{"op": "add_feature", "feature": {"type": "extrude",
                                             "params": {"profile": val}}}]
    assert M._decode_embedded_structures(ops, [])[0]["feature"]["params"]["profile"] == val


@pytest.mark.parametrize("empty", ["none", "None", "n/a", "", [], [None], "null"])
def test_an_emptied_ref_slot_is_dropped_not_rejected(empty):
    """A shell saying "remove no faces" is a closed hollow. Both spellings of that were
    hard rejections: «'none' is neither a reserved datum…» and «must be a non-empty list»."""
    notes = []
    out = M._drop_empty_ref_slots([{"op": "add_feature", "feature": {
        "type": "shell", "name": "S", "params": {"thickness": 2.5},
        "refs": {"remove": empty}}}], notes)
    assert "refs" not in out[0]["feature"]
    assert any("no selection" in n for n in notes)


def test_a_real_selector_is_never_dropped_as_empty():
    ops = [{"op": "add_feature", "feature": {
        "type": "shell", "name": "S",
        "refs": {"remove": [{"of": "face", "side": "top"}], "faces": "none"}}}]
    out = M._drop_empty_ref_slots(ops, [])
    assert out[0]["feature"]["refs"] == {"remove": [{"of": "face", "side": "top"}]}


def test_boolean_subtract_op_becomes_a_boolean_feature():
    """«unknown op 'boolean_subtract'» — the name was invented but it carried the
    operation AND both operands, which is a complete boolean."""
    notes = []
    out = M._expand_compound_boolean_ops(
        [{"op": "boolean_subtract", "target": "f1", "tool": "f2", "name": "Hollow"}], notes)
    feat = out[0]["feature"]
    assert out[0]["op"] == "add_feature" and feat["type"] == "boolean"
    assert feat["name"] == "Hollow"
    assert feat["params"]["op"] == "subtract"   # canonicalized to "cut" downstream
    assert feat["params"]["target"] == "f1" and feat["params"]["tool"] == "f2"
    assert any("boolean is a FEATURE" in n for n in notes)


def test_a_real_add_feature_is_not_touched_by_the_compound_op_pass():
    ops = [{"op": "add_feature", "feature": {"type": "boolean",
                                             "params": {"op": "cut", "a": "f1", "b": "f2"}}}]
    assert M._expand_compound_boolean_ops(ops, []) is ops


def test_boolean_operands_are_read_from_refs_base_and_tool():
    """refs.base/refs.tool is exactly the two operands; only params was consulted, so
    the batch died on «needs its two operands» with both of them right there."""
    notes = []
    feat = M._normalize_boolean_feature({
        "type": "boolean", "name": "HollowShell", "params": {"operation": "subtract"},
        "refs": {"base": [{"kind": "feature", "id": "f1"}],
                 "tool": [{"kind": "feature", "id": "f2"}]}}, notes)
    assert feat["params"]["op"] == "cut"
    assert feat["params"]["a"] == "f1" and feat["params"]["b"] == "f2"
    assert not feat.get("refs")


def test_a_same_batch_boolean_operand_is_built_unmerged():
    """The sphere meant as a cutting tool was merged into the box on creation, so it
    "added no material" and the boolean's operand no longer existed."""
    notes = []
    out = M._unmerge_boolean_operands([
        {"op": "add_feature", "feature": {"type": "box", "name": "Base", "params": {}}},
        {"op": "add_feature", "feature": {"type": "sphere", "name": "Inner", "params": {}}},
        {"op": "add_feature", "feature": {"type": "boolean", "name": "H",
                                          "params": {"op": "cut", "a": "f1", "b": "$1"}}}],
        notes)
    assert out[1]["feature"]["params"]["merge"] is False
    assert "merge" not in out[0]["feature"]["params"]   # f1 is the KEPT body
    assert any("separate solid" in n for n in notes)


def test_an_operand_named_by_display_name_is_also_unmerged():
    out = M._unmerge_boolean_operands([
        {"op": "add_feature", "feature": {"type": "sphere", "name": "Inner", "params": {}}},
        {"op": "add_feature", "feature": {"type": "boolean", "name": "H",
                                          "params": {"op": "cut", "a": "f1", "b": "Inner"}}}],
        [])
    assert out[0]["feature"]["params"]["merge"] is False


def test_a_batch_with_no_boolean_is_left_alone():
    ops = [{"op": "add_feature", "feature": {"type": "box", "name": "B", "params": {}}}]
    assert M._unmerge_boolean_operands(ops, []) is ops


@pytest.mark.parametrize("word,mode", [("subtract", "cut"), ("cut", "cut"),
                                       ("union", "boss"), ("add", "boss")])
def test_a_boolean_word_on_a_primitive_becomes_its_mode(word, mode):
    """{"type":"cylinder",…,"refs":{"boolean":"subtract","target":"f2"}} — the engine
    already removes material with params.mode; a second feature would add a body the
    part never asked for."""
    notes = []
    out = M._normalize_inline_boolean([{"op": "add_feature", "feature": {
        "type": "cylinder", "name": "Bore", "params": {"radius": 5, "height": 90},
        "refs": {"boolean": word, "target": "f2"}}}], notes)
    feat = out[0]["feature"]
    assert feat["params"]["mode"] == mode
    assert "refs" not in feat
    assert any("params.mode" in n for n in notes)


def test_an_explicit_mode_is_not_overridden_by_a_boolean_word():
    out = M._normalize_inline_boolean([{"op": "add_feature", "feature": {
        "type": "cylinder", "name": "C", "params": {"mode": "boss"},
        "refs": {"boolean": "subtract"}}}], [])
    assert out[0]["feature"]["params"]["mode"] == "boss"


@enginetest
@pytest.mark.parametrize("case", ["faces_none", "remove_empty", "boolean_subtract_op",
                                  "refs_base_tool", "stringified_feature"])
def test_the_live_run_97813015264134_batches_build(case):
    from services.agent.agent_turn import _bind_tool
    box = {"op": "add_feature", "feature": {"type": "box", "name": "Base", "params": {
        "length": {"value": 65, "expr": None}, "width": {"value": 65, "expr": None},
        "height": {"value": 65, "expr": None}}}}
    sphere = {"op": "add_feature", "feature": {"type": "sphere", "name": "InnerSphere",
                                               "params": {"radius": {"value": 25,
                                                                     "expr": None}}}}
    ops = {
        "faces_none": [box, {"op": "add_feature", "feature": {
            "type": "shell", "name": "ShellWall", "refs": {"faces": "none"},
            "params": {"thickness": {"value": 2.5, "expr": None}, "shell_all": True}}}],
        "remove_empty": [box, {"op": "add_feature", "feature": {
            "type": "shell", "name": "ShellWall", "refs": {"remove": []},
            "params": {"thickness": {"value": 2.5, "expr": None}}}}],
        "boolean_subtract_op": [box, sphere, {"op": "boolean_subtract", "target": "f1",
                                              "tool": "$1", "name": "HollowShell"}],
        "refs_base_tool": [box, sphere, {"op": "add_feature", "feature": {
            "type": "boolean", "name": "HollowShell", "params": {"operation": "subtract"},
            "refs": {"base": [{"kind": "feature", "id": "f1"}],
                     "tool": [{"kind": "feature", "id": "$1"}]}}}],
        "stringified_feature": [box, {"op": "add_feature", "feature":
            "{'type': 'cylinder', 'name': 'PoleBore', 'params': {'diameter': "
            "{'value': 10, 'expr': None}, 'height': {'value': 90, 'expr': None}}, "
            "'refs': {'boolean': 'subtract', 'target': 'f1'}}"}],
    }[case]
    doc = json.loads(_bind_tool("new_part")(name=f"live97_{case}"))["doc_id"]
    r = json.loads(_bind_tool("add_features")(doc_id=doc, ops=ops))
    assert r["ok"] is True, r.get("error")
    # Every case must leave ONE solid that is materially smaller than the raw 65mm block
    # (274625 mm3) — proof the shell/cut actually happened rather than being dropped.
    assert r["body_count"] == 1
    assert r["volume_mm3"] < 274625.0 * 0.995, r["volume_mm3"]

# --------------------------------------------------------------- stringified feature dicts

def test_the_verbatim_run_97813015264134_stringified_feature_decodes():
    """The op that killed run-97813015264134: feature is a Python-repr STRING (single
    quotes, bare None), not JSON. Must decode to a real dict, never crash with
    "'str' object has no attribute 'get'"."""
    ops = [{"op": "add_feature", "feature":
            "{'type': 'cylinder', 'name': 'PoleBore', 'params': {'diameter': "
            "{'value': 10, 'expr': None}, 'height': {'value': 90, 'expr': None}}, "
            "'refs': {'boolean': 'subtract', 'target': 'f2'}}"}]
    notes: list = []
    decoded = M._decode_embedded_structures(ops, notes)
    normalized = M._normalize_op_elements(decoded, notes)
    feat = normalized[0]["feature"]
    assert isinstance(feat, dict)
    assert feat["type"] == "cylinder"
    assert feat["params"]["diameter"]["value"] == 10
    assert notes  # the coercion is reported so the model learns the canonical spelling


def test_a_real_json_string_feature_also_decodes():
    ops = [{"op": "add_feature", "feature":
            '{"type":"box","name":"B","params":{"length":10,"width":10,"height":10}}'}]
    notes: list = []
    decoded = M._decode_embedded_structures(ops, notes)
    normalized = M._normalize_op_elements(decoded, notes)
    feat = normalized[0]["feature"]
    assert isinstance(feat, dict)
    assert feat["type"] == "box"


def test_an_unparseable_string_feature_is_a_teaching_valueerror_not_an_attributeerror():
    ops = [{"op": "add_feature", "feature": "not json and not a python literal either"}]
    notes: list = []
    decoded = M._decode_embedded_structures(ops, notes)
    with pytest.raises(ValueError) as exc:
        M._normalize_op_elements(decoded, notes)
    assert not isinstance(exc.value, AttributeError)
    msg = str(exc.value)
    assert "not json and not a python literal either" in msg
    assert "JSON" in msg


def test_a_bare_string_op_element_is_a_teaching_valueerror():
    with pytest.raises(ValueError) as exc:
        M._normalize_op_elements(["oops, not an op"], [])
    assert not isinstance(exc.value, AttributeError)
    assert "oops, not an op" in str(exc.value)


# ---------------------------------------------------------------------------
# Live run run-120283478269835 (2026-07-28): a twisted lattice orb. Six failed calls,
# the model looping on two sentences it could not act on.

@pytest.mark.parametrize("mode,kind", [
    ("circular", "circular_pattern"), ("polar", "circular_pattern"),
    ("radial", "circular_pattern"), ("linear", "linear_pattern"),
    ("rectangular", "linear_pattern"), ("grid", "linear_pattern")])
def test_a_generic_pattern_reads_its_kind_from_mode(mode, kind):
    """`_FEATURE_TYPE_ALIASES` mapped the bare word "pattern" to linear_pattern REGARDLESS
    of mode, so a circular pattern was rejected for the four linear fields it never had."""
    feat = M._normalize_pattern_feature({
        "type": "pattern", "name": "P",
        "params": {"source": "$0", "mode": mode, "axis": "z", "count": 12,
                   "angle": 360, "spacing": 10, "direction": "x"}}, [])
    assert feat["type"] == kind


def test_a_circular_pattern_lands_on_the_circular_schema():
    notes = []
    feat = M._normalize_pattern_feature({
        "type": "pattern", "name": "CutPatternZ",
        "params": {"source": "$0", "mode": "circular", "axis": "z",
                   "count": 12, "angle": 360}}, notes)
    assert feat["params"] == {"count": 12, "angle": 360}
    assert feat["refs"] == {"axis": "z", "features": ["$0"]}
    assert any("circular_pattern" in n for n in notes)


def test_a_linear_pattern_lands_on_the_numbered_schema():
    feat = M._normalize_pattern_feature({
        "type": "array", "name": "Row",
        "params": {"source": "f2", "mode": "linear", "direction": "x",
                   "count": 5, "spacing": 20}}, [])
    assert feat["type"] == "linear_pattern"
    assert feat["params"] == {"count1": 5, "spacing1": 20}
    assert feat["refs"] == {"dir1": "x", "features": ["f2"]}


def test_the_destination_key_is_not_erased_by_its_own_alias_cleanup():
    """`axis` is both an accepted alias AND the canonical ref name, so stripping aliases
    AFTER writing the answer deleted it and the batch died on «refs.axis: required»."""
    feat = M._normalize_pattern_feature({
        "type": "circular_pattern", "name": "P",
        "params": {"count": 6, "angle": 360}, "refs": {"axis": "axis.z",
                                                       "features": ["f1"]}}, [])
    assert feat["refs"]["axis"] == "axis.z"
    assert feat["refs"]["features"] == ["f1"]


def test_a_circular_pattern_with_no_angle_defaults_to_a_full_revolution():
    feat = M._normalize_pattern_feature({
        "type": "polar_pattern", "name": "P",
        "params": {"count": 8}, "refs": {"axis": "axis.z", "features": ["f1"]}}, [])
    assert feat["params"]["angle"] == 360


def test_a_non_pattern_feature_is_untouched():
    feat = {"type": "extrude", "name": "E", "params": {"depth": 5, "count": 3}}
    assert M._normalize_pattern_feature(feat, []) is feat


@enginetest
def test_a_boolean_on_operands_merged_by_an_earlier_batch_is_repaired():
    """«operand 'f1' was already merged into another body — build that body with
    "merge":false» is not actionable: f1 is already built. Four consecutive live batches
    died on that one sentence. edit_feature CAN apply it, so the harness does."""
    from services.agent.agent_turn import _bind_tool
    np, add = _bind_tool("new_part"), _bind_tool("add_features")
    doc = json.loads(np(name="merged_operand", fresh=True))["doc_id"]
    add(doc_id=doc, ops=[{"op": "add_feature", "feature": {
        "type": "box", "name": "Base",
        "params": {"length": 60, "width": 60, "height": 40}}}])
    # A default primitive FUSES, so after this call f1 and f2 are one solid.
    add(doc_id=doc, ops=[{"op": "add_feature", "feature": {
        "type": "box", "name": "CutTool",
        "params": {"length": 3, "width": 10, "height": 60}}}])
    r = json.loads(add(doc_id=doc, ops=[{"op": "boolean", "feature": {
        "type": "boolean", "name": "LatticeCut1",
        "params": {"operation": "cut", "a": "f1", "b": "f2"}}}]))
    assert r["ok"] is True, r.get("error")
    assert any("merge:false" in n for n in r.get("interpreted") or [])
    # The slot really was cut: 60*60*40 minus the 3x10x40 the tool overlaps.
    assert r["volume_mm3"] == pytest.approx(60 * 60 * 40 - 3 * 10 * 40, rel=1e-4)


@enginetest
@pytest.mark.parametrize("mode", ["circular", "linear"])
def test_the_live_run_120283478269835_pattern_builds(mode):
    from services.agent.agent_turn import _bind_tool
    np, add = _bind_tool("new_part"), _bind_tool("add_features")
    doc = json.loads(np(name=f"pat_{mode}", fresh=True))["doc_id"]
    add(doc_id=doc, ops=[{"op": "add_feature", "feature": {
        "type": "box", "name": "Plate",
        "params": {"length": 120, "width": 80, "height": 8}}}])
    extra = ({"mode": "circular", "axis": "z", "count": 6, "angle": 360} if mode == "circular"
             else {"mode": "linear", "direction": "x", "count": 5, "spacing": 20})
    r = json.loads(add(doc_id=doc, ops=[
        {"op": "add_feature", "feature": {
            "type": "hole", "name": "Seed",
            "params": {"diameter": 6, "style": "simple", "end": "through_all"},
            "refs": {"face": {"of": "face", "side": "top"}}, "at": [-40, 0, 0]}},
        {"op": "pattern", "feature": {"type": "pattern", "name": "Pat",
                                      "params": {"source": "$0", **extra}}}]))
    assert r["ok"] is True, r.get("error")
    # Every instance really was cut: N holes of d6 through 8mm of plate.
    n = 6 if mode == "circular" else 5
    assert r["volume_mm3"] == pytest.approx(120 * 80 * 8 - n * math.pi * 9 * 8, rel=1e-3)

# --------------------------------------------------------------------- flat top-level keys
#
# core/doc/session.cpp:960-991 permissively hoists every non-structural top-level feature
# key into params — so a flat model payload like {"type":"fillet","edges":[...],
# "params":{"radius":2}} is legal from the ENGINE's point of view. The Python normalizers
# that decide the final params/refs split (_migrate_ref_slots, _param_or_ref via
# _normalize_pattern_feature) used to look only in params/refs, never at the feature's own
# sibling top-level keys, so the flat shape was rejected before it ever reached the engine.
# _hoist_feature_sibling_keys mirrors the C++ hoist client-side so both downstream passes
# see the value without either needing its own copy of this lookup.

def _pipeline(feature: dict, notes: list | None = None) -> dict:
    """Run the real ops-normalization pipeline slice these tests care about, in the SAME
    order add_features runs it: hoist -> pattern -> feature types -> ref-slot migration ->
    refs repair. Returns the resulting feature dict."""
    notes = notes if notes is not None else []
    ops = [{"op": "add_feature", "feature": dict(feature)}]
    ops = M._hoist_feature_sibling_keys(ops, notes)
    ops = M._normalize_pattern_ops(ops, notes)
    ops = M._normalize_feature_types(ops, notes)
    ops = M._migrate_ref_slots(ops, notes)
    feat = ops[0]["feature"]
    return M._normalize_feature_refs(feat, feat.get("type"), notes)

def _ops3(profile):
    """box, sketch, extrude(profile) — the shape of the live keychain batch."""
    return [
        {"op": "add_feature", "feature": {"type": "box", "name": "Base"}},
        {"op": "add_feature", "feature": {"type": "sketch", "name": "S"}},
        {"op": "add_feature", "feature": {"type": "extrude", "name": "Boss",
                                          "refs": {"profile": profile}}},
    ]


class TestBatchPlaceholderValidation:
    """Live run 2026-07-30 (Lexus-F keychain): the model wrote profile:"$2"/"$4" meaning
    the 2nd and 4th ops — "$N" is ZERO-BASED — and the batch died on «profile '$2' ->
    '$2' not found», which names no range, no base, and never says $2 WAS the feature
    doing the referencing. Two features and a turn lost to an unstated off-by-one."""

    def test_self_reference_says_so_and_names_the_slot_meant(self):
        with pytest.raises(ValueError) as exc:
            M._validate_batch_placeholders(_ops3("$2"))
        msg = str(exc.value)
        assert "refers to ITSELF" in msg
        assert '"$1"' in msg          # the op it almost certainly meant
        assert "ZERO-BASED" in msg
        # The roster must let the model pick the right index without another call.
        assert "$0=Base (box)" in msg and "$1=S (sketch)" in msg

    def test_out_of_range_reports_the_real_range(self):
        """Must NOT be reported as a forward reference: every out-of-range index is also
        greater than the referencing op, so testing that first makes this unreachable."""
        with pytest.raises(ValueError) as exc:
            M._validate_batch_placeholders(_ops3("$7"))
        msg = str(exc.value)
        assert "out of range" in msg
        assert "3 op(s)" in msg and '"$0".."$2"' in msg
        assert "refers to ITSELF" not in msg

    def test_forward_reference_is_named_as_ordering(self):
        ops = _ops3("$1")
        # Make the extrude reference an op created AFTER it.
        ops.append({"op": "add_feature", "feature": {"type": "fillet", "name": "F"}})
        ops[2]["feature"]["refs"]["profile"] = "$3"
        with pytest.raises(ValueError) as exc:
            M._validate_batch_placeholders(ops)
        assert "creates LATER" in str(exc.value)

    def test_a_valid_backward_reference_passes(self):
        M._validate_batch_placeholders(_ops3("$1"))          # no raise
        M._validate_batch_placeholders(_ops3(["$0", "$1"]))  # nested in a list

    def test_placeholders_nested_in_a_selector_object_are_checked(self):
        """The live payload's real shape is refs.profile=[{"kind":"region","sketch":"$N"}]
        — a bare-string check would walk straight past it."""
        with pytest.raises(ValueError) as exc:
            M._validate_batch_placeholders(
                _ops3([{"kind": "region", "sketch": "$2"}]))
        assert "refers to ITSELF" in str(exc.value)

    def test_a_feature_name_that_looks_like_a_placeholder_is_not_a_ref(self):
        """`name` is not a ref slot; only refs/params are walked."""
        ops = [{"op": "add_feature", "feature": {"type": "box", "name": "$9"}}]
        M._validate_batch_placeholders(ops)  # no raise

    def test_an_empty_or_non_list_batch_is_ignored(self):
        M._validate_batch_placeholders([])
        M._validate_batch_placeholders(None)


def test_flat_fillet_edges_reach_refs_not_params():
    """Live: {"type":"fillet","edges":[...],"params":{"radius":2}} rejected 3x in a row
    with «refs.edges: required for fillet» — the model had the right selectors, wrong bag,
    and "edges" was never even IN a bag, it sat beside params."""
    notes: list = []
    feat = _pipeline({"type": "fillet", "name": "Corner_Fillets",
                      "edges": [{"sid": "Ed7ec5d"}], "params": {"radius": 2}}, notes)
    assert feat["refs"]["edges"] == [{"sid": "Ed7ec5d"}]
    assert "edges" not in feat["params"]
    assert "edges" not in feat
    assert any("edges" in n for n in notes)


def test_flat_loft_sections_reach_refs():
    """Live: «refs.sections: required for loft» — same bug, loft's slot."""
    feat = _pipeline({"type": "loft", "name": "Body",
                      "sections": [{"kind": "region", "sketch": "S1"},
                                   {"kind": "region", "sketch": "S2"}],
                      "params": {}})
    assert feat["refs"]["sections"] == [{"kind": "region", "sketch": "S1"},
                                        {"kind": "region", "sketch": "S2"}]
    assert "sections" not in feat.get("params", {})


def test_flat_linear_pattern_reaches_canonical_schema():
    """Live: «params.count1/spacing1 required for linear_pattern» and
    «refs.dir1/refs.features required for linear_pattern» — a canonically-typed
    linear_pattern with its numbers and refs sitting at the feature's own top level."""
    feat = _pipeline({"type": "linear_pattern", "name": "Row",
                      "dir1": "x", "count1": 4, "spacing1": 10, "features": ["f2"]})
    assert feat["params"]["count1"] == 4
    assert feat["params"]["spacing1"] == 10
    assert feat["refs"]["dir1"] == "axis.x"  # "x" is a datum word: dir1 is in _DATUM_REF_SLOTS
    assert feat["refs"]["features"] == ["f2"]
    for k in ("dir1", "count1", "spacing1", "features"):
        assert k not in feat


def test_flat_generic_pattern_kind_is_detected_from_a_sibling_mode():
    """A generic {"type":"pattern","mode":"circular",...} names its KIND in a sibling key,
    not params — without the hoist, _pattern_kind never sees "mode" and a circular request
    silently became linear (params.get(k) for every _PATTERN_KIND_KEYS candidate is None)."""
    feat = _pipeline({"type": "pattern", "name": "Pat", "source": "f2",
                      "mode": "circular", "axis": "z", "count": 6})
    assert feat["type"] == "circular_pattern"
    assert feat["params"]["count"] == 6
    assert feat["refs"]["axis"] == "axis.z"
    assert feat["refs"]["features"] == ["f2"]


class TestHoistIsLoadBearing:
    """Neuter the hoist and confirm the exact rejections above come back — proves the
    upstream pass, not some other normalizer, is what fixes them."""

    def _pipeline_without_hoist(self, feature: dict) -> dict:
        notes: list = []
        ops = [{"op": "add_feature", "feature": dict(feature)}]
        # Skip M._hoist_feature_sibling_keys — this is the ONLY line different from
        # _pipeline() above.
        ops = M._normalize_pattern_ops(ops, notes)
        ops = M._normalize_feature_types(ops, notes)
        ops = M._migrate_ref_slots(ops, notes)
        feat = ops[0]["feature"]
        return M._normalize_feature_refs(feat, feat.get("type"), notes)

    def test_fillet_edges_stay_stranded_without_the_hoist(self):
        feat = self._pipeline_without_hoist(
            {"type": "fillet", "name": "Corner_Fillets",
             "edges": [{"sid": "Ed7ec5d"}], "params": {"radius": 2}})
        assert "edges" not in feat.get("refs", {})
        assert feat.get("edges") == [{"sid": "Ed7ec5d"}]  # still stranded at top level

    def test_linear_pattern_numbers_stay_stranded_without_the_hoist(self):
        feat = self._pipeline_without_hoist(
            {"type": "linear_pattern", "name": "Row",
             "dir1": "x", "count1": 4, "spacing1": 10, "features": ["f2"]})
        assert "count1" not in feat.get("params", {})
        assert "dir1" not in feat.get("refs", {})

    def test_generic_pattern_mode_is_lost_without_the_hoist(self):
        feat = self._pipeline_without_hoist(
            {"type": "pattern", "name": "Pat", "source": "f2",
             "mode": "circular", "axis": "z", "count": 6})
        # No sibling visibility -> _pattern_kind falls back to its linear default, the
        # exact silent-misdetection bug the hoist fixes.
        assert feat["type"] == "linear_pattern"


# --------------------------------------------------------------------- P1: per-type slots

def test_shell_faces_alias_to_remove():
    """Live: «refs.faces: unknown ref slot for shell» — shell's only ref slot is `remove`."""
    notes: list = []
    feat = M._normalize_feature_refs(
        {"type": "shell", "name": "Hollow", "refs": {"faces": [{"sid": "F1"}]},
         "params": {"thickness": 2}}, "shell", notes)
    assert feat["refs"]["remove"] == [{"sid": "F1"}]
    assert "faces" not in feat["refs"]
    assert any("refs.faces" in n and "refs.remove" in n for n in notes)


@pytest.mark.parametrize("alias", ["faces", "open_faces", "open", "remove_faces"])
def test_every_shell_faces_synonym_aliases_to_remove(alias):
    feat = M._normalize_feature_refs(
        {"type": "shell", "name": "Hollow", "refs": {alias: [{"sid": "F1"}]},
         "params": {"thickness": 2}}, "shell", [])
    assert feat["refs"]["remove"] == [{"sid": "F1"}]


def test_shell_alias_never_shadows_an_explicit_remove():
    """An explicit refs.remove wins; the faces copy is just dropped, never guessed between."""
    feat = {"type": "shell", "refs": {"faces": [{"sid": "WRONG"}],
                                       "remove": [{"sid": "RIGHT"}]},
           "params": {"thickness": 2}}
    out = M._normalize_feature_refs(feat, "shell", [])
    assert out is feat  # untouched: nothing unambiguous to fix
    assert out["refs"]["remove"] == [{"sid": "RIGHT"}]


def test_extrude_face_aliases_to_profile():
    """Live: «refs.face: unknown ref slot for extrude» — extrude has SEVERAL ref slots
    (profile/up_to/up_to2/…) so the engine's own single-slot aliasing deliberately refuses
    to guess there, but "face" naming the sketch profile is unambiguous."""
    notes: list = []
    feat = M._normalize_feature_refs(
        {"type": "extrude", "name": "Boss",
         "refs": {"face": {"kind": "region", "sketch": "S1"}},
         "params": {"depth": 5}}, "extrude", notes)
    assert feat["refs"]["profile"] == [{"kind": "region", "sketch": "S1"}]
    assert "face" not in feat["refs"]
    assert any("refs.face" in n and "refs.profile" in n for n in notes)


@pytest.mark.parametrize("ftype", ["extrude", "surf_extrude"])
def test_extrude_family_face_alias(ftype):
    feat = M._normalize_feature_refs(
        {"type": ftype, "refs": {"face": {"kind": "region", "sketch": "S1"}},
         "params": {"depth": 5}}, ftype, [])
    assert feat["refs"]["profile"] == [{"kind": "region", "sketch": "S1"}]


# ------------------------------------------------- a bare side word in a face/edge slot
# Live 2026-08-04 (claude-sonnet-5, basic-modeling sweep), on two different parts:
# {"type":"hole","refs":{"face":"top"}} died on «'top' is neither a reserved datum nor a
# feature id». The prose mapper needs a face/edge NOUN ("the top face"), so the one-word
# form — for the slot that has ALREADY named the kind — fell through to the datum path.

@pytest.mark.parametrize("word,side", [
    ("top", "top"), ("bottom", "bottom"), ("front", "front"), ("back", "back"),
    ("rear", "back"), ("left", "left"), ("right", "right"), ("underside", "bottom"),
    ("+z", "+z"), ("-x", "-x"),
])
def test_a_bare_side_word_in_refs_face_becomes_a_query(word, side):
    notes: list = []
    feat = M._normalize_feature_refs(
        {"type": "hole", "name": "Bore", "refs": {"face": word},
         "params": {"diameter": 6}}, "hole", notes)
    assert feat["refs"]["face"] == {"of": "face", "side": side}, feat
    assert notes


def test_a_bare_side_word_in_refs_edges_becomes_an_edge_query():
    feat = M._normalize_feature_refs(
        {"type": "fillet", "name": "R", "refs": {"edges": "top"},
         "params": {"radius": 2}}, "fillet", [])
    assert feat["refs"]["edges"] == [{"of": "edge", "side": "top"}], feat


def test_a_feature_id_in_a_face_slot_is_left_for_the_engine():
    """Only a side TOKEN is claimed — an f<N> id still means the feature it names."""
    feat = M._normalize_feature_refs(
        {"type": "hole", "name": "Bore", "refs": {"face": "f1"},
         "params": {"diameter": 6}}, "hole", [])
    assert feat["refs"]["face"] == "f1"


def test_a_plane_slot_keeps_its_datum_reading():
    """refs.plane="top" is the ORIGIN datum, not a face — the side-word map must not steal it."""
    feat = M._normalize_feature_refs(
        {"type": "sketch", "name": "S", "refs": {"plane": "top"}}, "sketch", [])
    assert feat["refs"]["plane"] == "plane.top"


@pytest.mark.parametrize("axis_value,expected", [
    ([0, 0, 1], "axis.z"),
    ([0, 0, -1], "axis.z"),
    ([1, 0, 0], "axis.x"),
    ([0, 1, 0], "axis.y"),
    ([0.02, 0.01, 5.0], "axis.z"),  # dominant component wins over near-zero noise
])
def test_bare_axis_vector_snaps_to_the_nearest_cardinal_datum(axis_value, expected):
    """Live ladder: «refs.axis: selector must be an object» (a bare [x,y,z] list) — the
    engine has no arbitrary-direction selector kind, only a reserved datum or a real
    selector object."""
    notes: list = []
    feat = M._normalize_feature_refs(
        {"type": "circular_pattern", "name": "P",
         "refs": {"axis": axis_value, "features": ["f1"]},
         "params": {"count": 6, "angle": 360}}, "circular_pattern", notes)
    assert feat["refs"]["axis"] == expected
    assert notes


def test_malformed_vector_selector_for_axis_also_snaps():
    """Live ladder rung 2: «refs.axis.kind: not in […]» / «refs.axis.centroid: must be
    [x,y,z]» — a pseudo-selector wrapping the same direction, one shape further along."""
    feat = M._normalize_feature_refs(
        {"type": "circular_pattern", "name": "P",
         "refs": {"axis": {"kind": "region", "centroid": [0, 0, -5]}, "features": ["f1"]},
         "params": {"count": 6, "angle": 360}}, "circular_pattern", [])
    assert feat["refs"]["axis"] == "axis.z"


@pytest.mark.parametrize("wrapper", [
    {"origin": [0, 0, 0], "direction": [0, 0, 1]},
    {"direction": [0, 0, 1]},
    {"vector": [0, 0, 1]},
    {"kind": "axis", "axis": [0, 0, 1]},
])
def test_a_direction_wrapper_for_axis_also_snaps(wrapper):
    """Live 2026-08-04: a revolve arrived with refs.axis={"origin":…,"direction":[0,0,1]}
    and died on «refs.axis.kind: absent not in […]», which says nothing about the
    direction it DID supply. `origin` is ignored on purpose — a reserved axis passes
    through the world origin, so honouring an offset would be a lie."""
    feat = M._normalize_feature_refs(
        {"type": "revolve", "name": "R",
         "refs": {"profile": [{"kind": "region", "sketch": "f1"}], "axis": dict(wrapper)},
         "params": {"angle": 360}}, "revolve", [])
    assert feat["refs"]["axis"] == "axis.z", feat


def test_an_origin_with_no_direction_is_left_for_the_engine():
    feat = {"type": "revolve", "name": "R",
            "refs": {"profile": [{"kind": "region", "sketch": "f1"}],
                     "axis": {"origin": [0, 0, 0]}},
            "params": {"angle": 360}}
    assert M._normalize_feature_refs(feat, "revolve", []) is feat


def test_zero_vector_axis_is_left_for_the_engine():
    """No dominant component -> nothing unambiguous to snap to; leave the engine's error."""
    feat = {"type": "circular_pattern", "name": "P",
           "refs": {"axis": [0, 0, 0], "features": ["f1"]},
           "params": {"count": 6, "angle": 360}}
    assert M._normalize_feature_refs(feat, "circular_pattern", []) is feat


def test_circular_pattern_origin_is_a_teaching_rejection_not_an_alias():
    """Live: «refs.origin: unknown ref slot for circular_pattern». circular_pattern's only
    ref slots are axis/features — there is no point/center slot to alias origin onto, and
    an axis LINE is not interchangeable with a POINT (aliasing it would silently change
    which geometry the rotation is built around, the same guess the shell/extrude aliases
    deliberately avoid making for an ambiguous multi-slot type). Left untouched so the
    engine's own "unknown ref slot" error — which lists the real slots — reaches the model."""
    feat = {"type": "circular_pattern", "name": "P",
           "refs": {"origin": [0, 0, 0], "axis": "axis.z", "features": ["f1"]},
           "params": {"count": 6, "angle": 360}}
    assert M._normalize_feature_refs(feat, "circular_pattern", []) is feat
    assert "circular_pattern" not in M._TYPE_REF_ALIASES


# ---------------------------------------------------------------------------
# The 3D features BEYOND the box/extrude/fillet/hole path — loft, revolve, sweep, shell,
# draft, rib, thread, chamfer, mirror, patterns. Every case below was measured live on
# 2026-07-30 against a fresh engine (CHUDWARE_ENGINE_DAEMON=0 +
# CHUDWARE_LIBCHUDWARE=core/build-occt-linux/bin/libchudware.so), driving the real
# add_features tool on real geometry and reading the VOLUME back, not `ok`.

def test_draft_pull_in_params_reaches_the_pull_slot():
    """SILENT WRONG GEOMETRY, measured live: params.pull:"axis.x" reached no slot at all,
    so the draft was built about the DEFAULT pull and reported ok — 22942.038 mm^3 for
    axis.x, axis.y and axis.z alike, on a 40x30x20 block with a 10 deg taper. refs.pull:
    "axis.x" is correctly rejected («the draft did not complete»). A pull direction the
    engine never sees is worse than a rejection: the model gets a taper it did not ask
    for and no way to tell."""
    ops = M._migrate_ref_slots([{"op": "add_feature", "feature": {
        "type": "draft", "name": "D",
        "params": {"angle": 10, "pull": "axis.x"},
        "refs": {"neutral": {"of": "face", "side": "bottom"},
                 "faces": [{"of": "face", "side": "left"}]}}}], [])
    feat = ops[0]["feature"]
    assert feat["refs"]["pull"] == "axis.x"
    assert "pull" not in feat["params"]


def test_mirror_features_in_params_reaches_the_features_slot():
    """SILENT WRONG GEOMETRY, measured live: params.features:["f2"] on a mirror reached no
    slot, so the mirror fell through to its WHOLE-BODY branch, fused the reflection, and
    FILLED the hole the model wanted mirrored — volume went UP, 23748.673 -> 24000.0, and
    the call reported ok. With the slot migrated it builds 23497.345, the same number the
    canonical refs.features form gives."""
    ops = M._migrate_ref_slots([{"op": "add_feature", "feature": {
        "type": "mirror", "name": "M",
        "params": {"features": ["f2"]}, "refs": {"plane": "plane.right"}}}], [])
    feat = ops[0]["feature"]
    assert feat["refs"]["features"] == ["f2"]
    assert "features" not in feat["params"]


def test_boolean_operand_lists_are_not_stolen_by_the_ref_migration():
    """boolean's flat dialect spells its OPERANDS features/bodies (params.a/params.b after
    _normalize_inline_boolean), which runs AFTER the migration — so those two names must be
    excluded for boolean exactly the way `tool` already is."""
    feat = {"type": "boolean", "name": "B",
            "params": {"op": "cut", "bodies": ["f1", "f2"]}}
    ops = M._migrate_ref_slots([{"op": "add_feature", "feature": dict(feat)}], [])
    assert ops[0]["feature"]["params"]["bodies"] == ["f1", "f2"]
    assert not ops[0]["feature"].get("refs")


def test_sketch_plane_is_not_stolen_by_the_ref_migration():
    """`plane` is a ref slot on mirror, but on a SKETCH _normalize_sketch_ops owns the
    whole "which datum is this drawn on" reading (including "XZ"/"Top Plane"). Migrating it
    early would strand the sketch on the plane.top default — the silent-wrong-datum failure
    that pass exists to prevent."""
    ops = M._migrate_ref_slots([{"op": "add_feature", "feature": {
        "type": "sketch", "name": "S",
        "params": {"plane": "plane.front", "entities": [], "constraints": []}}}], [])
    assert ops[0]["feature"]["params"]["plane"] == "plane.front"


@pytest.mark.parametrize("ftype,alias,canon,extra", [
    # rib: params.width=8 built the 3 mm DEFAULT rib (7650.0 mm^3) and reported ok;
    # thickness=8 builds 8400.0. Silently wrong, measured live.
    ("rib", "width", "thickness", {}),
    ("rib", "rib_thickness", "thickness", {}),
    # thread: params.depth=10 threaded the WHOLE 30 mm cylindrical face (1251.619 mm^3);
    # length=10 gives 1418.832. Silently wrong, measured live.
    ("thread", "depth", "length", {"size": "M8"}),
    # The rest were clean rejections for a value the model DID supply.
    ("shell", "wall_thickness", "thickness", {}),
    ("shell", "wall", "thickness", {}),
    ("chamfer", "size", "length", {}),
    ("chamfer", "d1", "length", {}),
    ("draft", "draft_angle", "angle", {}),
    ("thread", "thread_size", "size", {}),
    ("thread", "diameter", "major_dia", {"pitch": 1.25}),
    ("mirror", "about", "plane", {}),
    ("sweep", "spine", "path", {"mode": "boss"}),
    # SHEET METAL — measured live 2026-07-30 by reading the unfolded blank's X extent on an
    # 80x40x2 base + two 90 deg flanges: `height`/`flange_length` built the 20 mm DEFAULT
    # wall (blank 124.106 mm instead of 134.106), `bend_radius` used the default radius
    # (129.540), `kfactor`/`k` used k=0.45 (135.677). All silently, all reported ok.
    ("sm_base_flange", "sheet_thickness", "thickness", {}),
    ("sm_base_flange", "gauge", "thickness", {}),
    ("sm_base_flange", "bend_radius", "default_bend_radius", {"thickness": 2}),
    ("sm_base_flange", "kfactor", "k_factor", {"thickness": 2}),
    ("sm_edge_flange", "height", "length", {}),
    ("sm_edge_flange", "flange_length", "length", {}),
    ("sm_edge_flange", "bend_radius", "radius", {}),
    ("sm_edge_flange", "k", "k_factor", {}),
    ("sm_edge_flange", "bend_angle", "angle", {}),
    ("sm_hem", "hem_length", "length", {}),
    ("sm_jog", "step", "offset", {}),
])
def test_type_param_alias_reaches_the_key_the_engine_reads(ftype, alias, canon, extra):
    notes: list = []
    ops = M._normalize_type_param_aliases([{"op": "add_feature", "feature": {
        "type": ftype, "name": "F", "params": {alias: 8, **extra}}}], notes)
    params = ops[0]["feature"]["params"]
    assert params[canon] == 8
    assert alias not in params
    assert notes


def test_type_param_alias_never_overrides_an_explicit_canonical_key():
    feat = {"type": "rib", "name": "R", "params": {"thickness": 3, "width": 8}}
    ops = M._normalize_type_param_aliases([{"op": "add_feature", "feature": feat}], [])
    assert ops[0]["feature"]["params"]["thickness"] == 3


def test_type_param_alias_leaves_an_unlisted_type_alone():
    """The table is per-type on purpose: `width` is a real box dimension, not a thickness."""
    ops = [{"op": "add_feature", "feature": {
        "type": "box", "name": "B", "params": {"length": 40, "width": 30, "height": 20}}}]
    assert M._normalize_type_param_aliases(ops, []) is ops


@pytest.mark.parametrize("ftype", ["sm_flat_pattern", "asm_linear_pattern",
                                   "asm_circular_pattern"])
def test_a_canonical_type_containing_the_word_pattern_is_not_retyped(ftype):
    """Measured live 2026-07-30: {"type":"sm_flat_pattern"} — the exact op the sheetmetal
    cheatsheet tells the model to send to unfold the blank — was rewritten to a
    `linear_pattern` because its NAME contains "pattern", then rejected with
    «params.count1: required for linear_pattern; params.spacing1: …; refs.dir1: …;
    refs.features: …». The sheet-metal flat pattern could not be produced at all through
    add_features. The two assembly component patterns fall the same way."""
    ops = [{"op": "add_feature", "feature": {"type": ftype, "name": "F", "params": {}}}]
    notes: list = []
    assert M._normalize_pattern_ops(ops, notes) is ops
    assert notes == []


def test_doc_name_refs_resolve_a_feature_an_earlier_call_built(monkeypatch):
    """Measured live 2026-07-30, one call after the other: a sketch named "Prof" built in
    call 1 and extruded in call 2 died «profile 'Prof' -> 'Prof' not found»; a pattern's
    refs.features:["pin"] died «refs.features[0]: not a feature id»; a boolean's
    params.a:"blk" died «ref 'a' -> 'blk' not found». Splitting work across calls is the
    normal shape of an agent turn, and the document knows every one of those names."""
    monkeypatch.setattr(M.cb, "call", lambda *_a, **_k: {"tree": {"features": [
        {"id": "f1", "name": "blk", "type": "box"},
        {"id": "f2", "name": "pin", "type": "hole"}]}})
    notes: list = []
    ops = M._resolve_doc_name_refs([{"op": "add_feature", "feature": {
        "type": "linear_pattern", "name": "LP",
        "params": {"count1": 4, "spacing1": 8},
        "refs": {"dir1": "axis.x", "features": ["pin"]}}}], "doc-1", notes)
    assert ops[0]["feature"]["refs"]["features"] == ["f2"]
    assert notes and "pin -> f2" in notes[0]


def test_doc_name_refs_leave_an_ambiguous_name_for_the_engine(monkeypatch):
    """Two features share the name — guessing one would be a silent wrong pick."""
    monkeypatch.setattr(M.cb, "call", lambda *_a, **_k: {"tree": {"features": [
        {"id": "f1", "name": "boss", "type": "extrude"},
        {"id": "f4", "name": "boss", "type": "extrude"}]}})
    ops = [{"op": "add_feature", "feature": {
        "type": "mirror", "name": "M",
        "refs": {"plane": "plane.right", "features": ["boss"]}}}]
    assert M._resolve_doc_name_refs(ops, "doc-1", []) is ops


def test_doc_name_refs_never_repoint_a_same_batch_name_at_an_older_namesake(monkeypatch):
    """The same-batch pass runs first and owns those names; a new feature reusing an old
    display name must not have its siblings' refs silently rewired to the OLD feature."""
    monkeypatch.setattr(M.cb, "call", lambda *_a, **_k: {"tree": {"features": [
        {"id": "f1", "name": "boss", "type": "extrude"}]}})
    ops = [{"op": "add_feature", "feature": {"type": "cylinder", "name": "boss",
                                             "params": {"radius": 3, "height": 6}}},
           {"op": "add_feature", "feature": {"type": "mirror", "name": "M",
                                             "refs": {"plane": "plane.right",
                                                      "features": ["boss"]}}}]
    assert M._resolve_doc_name_refs(ops, "doc-1", []) is ops


def test_doc_name_refs_do_not_touch_the_engine_for_a_canonical_batch(monkeypatch):
    """One doc_tree call, and only when an unresolved NAME is actually present — a datum,
    an f<N> id, a "$N" placeholder and a bare sid are none of them."""
    def _boom(*_a, **_k):
        raise AssertionError("doc_tree called for a batch with no unresolved name")
    monkeypatch.setattr(M.cb, "call", _boom)
    ops = [{"op": "add_feature", "feature": {
        "type": "mirror", "name": "M",
        "refs": {"plane": "plane.right", "features": ["f2"]}}},
        {"op": "add_feature", "feature": {
            "type": "thread", "name": "T", "params": {"size": "M8"},
            "refs": {"target_face": "Fa1b2c3"}}}]
    assert M._resolve_doc_name_refs(ops, "doc-1", []) is ops


# --------------------------------------------------------------------------------------
# 2D SKETCH RELATIONS / DIMENSIONS and the 2D EDITING ops.
#
# Measured against the live engine 2026-07-30: of 40 realistic relation payloads and 29
# dimension payloads, 45 were rejected outright — every one a pure SPELLING miss on a
# relation the solver already implements — and a rejected sketch takes the whole batch,
# including every extrude that referenced it. Two flags were worse than rejected: they
# were ACCEPTED and then ignored.

def _sketch(cons, ents=None, **extra):
    """The canonical sketch feature the normalizer is handed, with `cons` constraints."""
    return {"type": "sketch", "name": "S", "refs": {"plane": "plane.top"},
            "params": {"entities": ents if ents is not None else [
                {"type": "line", "id": "e1", "p1": [0, 0], "p2": [40, 3],
                 "construction": False},
                {"type": "line", "id": "e2", "p1": [41, 1], "p2": [38, 25],
                 "construction": False},
                {"type": "circle", "id": "e3", "center": [10, 12], "radius": 4,
                 "construction": False}],
                "constraints": cons}, **extra}


def _norm_cons(cons, ents=None):
    notes: list = []
    feat = M._normalize_sketch_feature(_sketch(cons, ents), notes)
    return feat["params"]["constraints"], notes


@pytest.mark.parametrize("written,canonical", [
    ("coincidence", "coincident"), ("coinc", "coincident"), ("connect", "coincident"),
    ("horiz", "horizontal"), ("vert", "vertical"),
    ("par", "parallel"), ("parallel_to", "parallel"),
    ("perp", "perpendicular"), ("normal", "perpendicular"), ("orthogonal", "perpendicular"),
    ("tangent_to", "tangent"), ("tangency", "tangent"),
    ("equal_length", "equal"), ("equal_radius", "equal"), ("same_length", "equal"),
    ("coaxial", "concentric"), ("colinear", "collinear"),
    ("mid", "midpoint"), ("symmetric_about", "symmetric"),
    ("point_on_line", "point_on"), ("on_curve", "point_on"),
])
def test_relation_synonyms_reach_the_engines_closed_vocabulary(written, canonical):
    """Live probe 2026-07-30: every one of these died on «"perp" is not a constraint type»
    and took the sketch — and with it every feature referencing it — down with it."""
    cons, notes = _norm_cons([{"type": written, "refs": ["e1", "e2", "e1"][:3]}])
    assert cons[0]["type"] == canonical
    assert notes and written in notes[0]


@pytest.mark.parametrize("written,axis", [
    ("horizontal_distance", "h"), ("distance_x", "h"), ("dist_x", "h"),
    ("vertical_distance", "v"), ("distance_y", "v"), ("dist_y", "v"),
])
def test_axis_distance_dialect_becomes_distance_plus_axis(written, axis):
    """A horizontal/vertical dimension is not a distinct relation: it is the engine's
    `distance` with axis:'h'|'v' (validate.cpp:1155). Written as its own type it was
    rejected; the projection it MEANT is what carries the intent."""
    cons, notes = _norm_cons([{"type": written, "refs": ["e1.p1", "e1.p2"], "value": 50}])
    assert cons[0]["type"] == "distance"
    assert cons[0]["axis"] == axis
    assert cons[0]["value"] == {"value": 50, "expr": None}
    assert notes


@pytest.mark.parametrize("axis_word,expected", [("x", "h"), ("y", "v"),
                                                ("horizontal", "h"), ("vertical", "v")])
def test_distance_axis_world_words_map_onto_h_v(axis_word, expected):
    """«axis: must be 'h', 'v', or null» — the engine's projection selector is named for
    the SKETCH frame, not the world axes the model reaches for."""
    cons, _ = _norm_cons([{"type": "distance", "refs": ["e1.p1", "e1.p2"],
                           "value": 50, "axis": axis_word}])
    assert cons[0]["axis"] == expected


@pytest.mark.parametrize("payload,expected", [
    ({"type": "coincident", "a": "e1.p2", "b": "e2.p1"}, ["e1.p2", "e2.p1"]),
    ({"type": "coincident", "entity1": "e1.p2", "entity2": "e2.p1"}, ["e1.p2", "e2.p1"]),
    ({"type": "coincident", "first": "e1.p2", "second": "e2.p1"}, ["e1.p2", "e2.p1"]),
    ({"type": "coincident", "elements": ["e1.p2", "e2.p1"]}, ["e1.p2", "e2.p1"]),
    ({"type": "fix", "refs": "e3"}, ["e3"]),
    ({"type": "fix", "entity": "e3"}, ["e3"]),
])
def test_relation_ref_slots_are_hoisted_into_refs(payload, expected):
    """A relation thought of as "between A and B" rather than as a list lost every one of
    these to «refs: coincident takes 2..2 refs» (live probe)."""
    cons, notes = _norm_cons([payload])
    assert cons[0]["refs"] == expected
    assert notes


def test_refs_split_across_two_dialects_is_completed_not_lost():
    """{"refs":"e1.p2","b":"e2.p1"} names both refs, one per dialect. Taking only the
    first left a 1-ref coincident, which the engine rejects."""
    cons, _ = _norm_cons([{"type": "coincident", "refs": "e1.p2", "b": "e2.p1"}])
    assert cons[0]["refs"] == ["e1.p2", "e2.p1"]


def test_symmetry_axis_in_its_own_slot_becomes_the_third_ref():
    """The engine wants refs=[a, b, axis]; the mirror line in `axis`/`line`/`about` lost
    the relation to «refs: symmetric takes 3..3 refs»."""
    for key in ("axis", "line", "about", "mirror_line"):
        cons, _ = _norm_cons([{"type": "symmetric", "refs": ["e1", "e2"], key: "e3"}])
        assert cons[0]["refs"] == ["e1", "e2", "e3"], key


def test_distance_axis_key_is_not_eaten_as_a_symmetry_axis():
    """`axis` on a distance is the h/v projection selector — a different thing entirely
    from symmetric's mirror line, so the symmetry hoist must be type-gated."""
    cons, _ = _norm_cons([{"type": "distance", "refs": ["e1.p1", "e1.p2"],
                           "value": 50, "axis": "h"}])
    assert cons[0]["refs"] == ["e1.p1", "e1.p2"]
    assert cons[0]["axis"] == "h"


@pytest.mark.parametrize("ctype,key,value", [
    ("distance", "distance", 50), ("distance", "dim", 50), ("length", "length", 50),
    ("radius", "radius", 7), ("diameter", "diameter", 14), ("angle", "angle", 90),
])
def test_dimension_value_named_after_the_quantity_is_read(ctype, key, value):
    """«value: required for radius» — a model naming the number after the quantity it
    measures ({"type":"radius","radius":7}) lost the batch to a missing `value`."""
    refs = ["e3"] if ctype in ("radius", "diameter") else \
        (["e1", "e2"] if ctype == "angle" else ["e1.p1", "e1.p2"])
    cons, notes = _norm_cons([{"type": ctype, "refs": refs, key: value}])
    assert cons[0]["value"] == {"value": value, "expr": None}
    assert key not in cons[0]
    assert notes


@pytest.mark.parametrize("written,expected", [("50mm", 50.0), ("5cm", 50.0)])
def test_unit_literal_dimension_value_converts_to_mm(written, expected):
    """«value: dimension must be a number» — the unit-suffixed string the rest of the
    tool already understands never reached a sketch dimension."""
    cons, _ = _norm_cons([{"type": "length", "refs": ["e1"], "value": written}])
    assert cons[0]["value"] == {"value": expected, "expr": None}


def test_angle_accepts_a_degree_literal():
    cons, _ = _norm_cons([{"type": "angle", "refs": ["e1", "e2"], "value": "90deg"}])
    assert cons[0]["value"] == {"value": 90.0, "expr": None}


@pytest.mark.parametrize("key", ["reference", "is_reference", "reference_dimension"])
def test_reference_dimension_becomes_driven_instead_of_moving_the_geometry(key):
    """THE silent drop. Live probe 2026-07-30: {"type":"length","refs":["e1"],"value":80,
    "reference":true} returned ok and STRETCHED e1 from 40.112 mm to 80 mm — the model
    asked for a measurement and got geometry it never requested, with nothing in the
    response saying so. `driven` is the engine's spelling; every other one was ignored."""
    cons, notes = _norm_cons([{"type": "length", "refs": ["e1"], "value": 80, key: True}])
    assert cons[0]["driven"] is True
    assert key not in cons[0]
    assert notes


def test_driving_is_the_inverse_flag_of_driven():
    cons, _ = _norm_cons([{"type": "length", "refs": ["e1"], "value": 80, "driving": False}])
    assert cons[0]["driven"] is True
    cons, _ = _norm_cons([{"type": "length", "refs": ["e1"], "value": 80, "driving": True}])
    assert cons[0]["driven"] is False


@pytest.mark.parametrize("written,expected", [
    ("e1:p2", "e1.p2"), ("E1.p2", "e1.p2"), ("l1.p2", "e1.p2"), ("e1.2", "e1.p2"),
    ("e1.end", "e1.p2"), ("e1.start", "e1.p1"), ("e3.centre", "e3.center"),
])
def test_point_handle_spellings_resolve(written, expected):
    """«'e1:p2' is not a valid handle» named no alternative. `.start`/`.end` on a LINE are
    p1/p2 — the engine's own sub-names for a line (validate.cpp ENTITY_SUBS)."""
    cons, _ = _norm_cons([{"type": "coincident", "refs": [written, "e2.p1"]}])
    assert cons[0]["refs"][0] == expected


def test_dangling_constraint_ref_names_the_valid_ids():
    """A relation on an entity that does not exist must never be dropped — the sketch then
    solves without the relation the model believes it placed. The engine rejects it but
    names no alternative; the id roster is in hand HERE."""
    with pytest.raises(ValueError) as ex:
        _norm_cons([{"type": "horizontal", "refs": ["e99"]}])
    assert "e99" in str(ex.value)
    assert "e1, e2, e3" in str(ex.value)


def test_batch_placeholder_in_a_constraint_ref_is_taught_not_guessed():
    """"$N" names a FEATURE created earlier in the batch; a sketch relation references
    entities INSIDE this sketch. Guessing between them would silently constrain the
    wrong geometry."""
    with pytest.raises(ValueError) as ex:
        _norm_cons([{"type": "horizontal", "refs": ["$0"]}])
    assert "$N" in str(ex.value) and "FEATURE" in str(ex.value)


@pytest.mark.parametrize("generic,ref,expected", [
    ("dimension", "e3", "radius"), ("smart_dimension", "e1", "length"),
    ("dim", "e1", "length"),
])
def test_generic_dimension_resolves_from_the_entity_it_points_at(generic, ref, expected):
    """SolidWorks has one Smart Dimension button; the engine names the concrete relation.
    The entity type is the only thing that says which one was meant."""
    cons, notes = _norm_cons([{"type": generic, "refs": [ref], "value": 7}])
    assert cons[0]["type"] == expected
    assert notes


def test_generic_dimension_on_an_unresolvable_ref_teaches_the_vocabulary():
    with pytest.raises(ValueError) as ex:
        _norm_cons([{"type": "dimension", "refs": ["e1.p1"], "value": 7}])
    assert "distance" in str(ex.value) and "radius/diameter" in str(ex.value)


@pytest.mark.parametrize("key", ["is_construction", "for_construction",
                                 "construction_geometry", "reference"])
def test_construction_flag_synonyms_are_honoured(key):
    """Live probe: `bool(e.get("construction", False))` silently swallowed every other
    spelling, so a centreline written {"is_construction":true} became REAL profile
    geometry — which is what the extrude then tries to sweep."""
    notes: list = []
    feat = M._normalize_sketch_feature(_sketch([], ents=[
        {"type": "line", "p1": [0, 0], "p2": [10, 0], key: True}]), notes)
    assert feat["params"]["entities"][0]["construction"] is True
    assert notes


def test_construction_false_as_a_string_is_not_inverted_into_true():
    """`bool("false")` is True: {"construction":"false"} silently produced CONSTRUCTION
    geometry, and the extrude then died on "profile sketch has no closed face"."""
    feat = M._normalize_sketch_feature(_sketch([], ents=[
        {"type": "line", "p1": [0, 0], "p2": [10, 0], "construction": "false"}]), [])
    assert feat["params"]["entities"][0]["construction"] is False


def test_centerline_is_a_construction_line():
    """SolidWorks' centreline — where every mirror/revolve workflow starts — has no engine
    entity type and was rejected as "not a sketch entity type"."""
    feat = M._normalize_sketch_feature(_sketch([], ents=[
        {"type": "centerline", "p1": [0, 0], "p2": [0, 40]}]), [])
    ent = feat["params"]["entities"][0]
    assert ent["type"] == "line" and ent["construction"] is True


def test_canonical_sketch_with_canonical_constraints_is_untouched():
    """Conservative contract: a byte-canonical sketch must round-trip by identity, or
    every note this module emits would be a lie about a rewrite that never happened."""
    feat = _sketch([{"id": "c1", "type": "horizontal", "refs": ["e1"]},
                    {"id": "c2", "type": "radius", "refs": ["e3"],
                     "value": {"value": 4, "expr": None}, "driven": False}])
    assert M._normalize_sketch_feature(feat, []) is feat


# ---- the 2D editing ops --------------------------------------------------------------

def _norm_edit(op):
    notes: list = []
    return M._normalize_sketch_edit_ops([op], notes)[0], notes


@pytest.mark.parametrize("payload,expected", [
    ({"op": "sketch_trim", "sketch": "S", "id": "e1", "point": [10, 0]},
     {"entity": "e1", "point": [10, 0]}),
    ({"op": "sketch_trim", "sketch": "S", "entities": ["e1"], "at": [10, 0]},
     {"entity": "e1", "point": [10, 0]}),
    ({"op": "sketch_trim", "sketch": "S", "entity": "e1", "point": {"x": 10, "y": 0}},
     {"entity": "e1", "point": [10, 0]}),
    ({"op": "sketch_extend", "sketch": "S", "id": "e1", "which": "p1"},
     {"entity": "e1", "end": "start"}),
    ({"op": "sketch_offset", "sketch": "S", "entity": "e1", "offset": 3, "side": [0, 1]},
     {"entities": ["e1"], "distance": 3, "side": [0, 1]}),
    ({"op": "sketch_fillet", "sketch": "S", "entities": ["e1", "e2"], "r": 2},
     {"e1": "e1", "e2": "e2", "radius": 2}),
    ({"op": "sketch_chamfer", "sketch": "S", "entities": ["e1", "e2"], "size": 2},
     {"e1": "e1", "e2": "e2", "distance": 2}),
    ({"op": "sketch_mirror_entities", "sketch": "S", "entities": ["e3"], "axis": "e1"},
     {"entities": ["e3"], "line": "e1"}),
    ({"op": "sketch_move", "sketch": "S", "entities": ["e3"], "delta": [5, 5]},
     {"entities": ["e3"], "dx": 5, "dy": 5}),
    ({"op": "sketch_rotate", "sketch": "S", "entities": ["e3"], "center": [0, 0],
      "angle": 30}, {"entities": ["e3"], "cx": 0, "cy": 0, "angle": 30}),
    ({"op": "sketch_toggle_construction", "sketch": "S", "entity": "e3"}, {"id": "e3"}),
])
def test_editing_op_key_dialect(payload, expected):
    """session.cpp reads these key names verbatim and they are not even consistent with
    each other (trim/extend take `entity`, offset/mirror/pattern take `entities`,
    fillet/chamfer take `e1`/`e2`). Live probe 2026-07-30: 19 of 33 realistic payloads
    were rejected on a key name alone."""
    out, notes = _norm_edit(payload)
    for k, v in expected.items():
        assert out[k] == v, k
    assert notes


@pytest.mark.parametrize("key", ["duplicate", "keep_original", "as_copy"])
@pytest.mark.parametrize("opname", ["sketch_move", "sketch_rotate"])
def test_copy_flag_synonyms_stop_destroying_the_original(key, opname):
    """The worst editing-op silent drop: {"duplicate":true} was IGNORED, so the op moved
    the source geometry away instead of copying it — reporting ok while the original was
    gone. Live probe 2026-07-30 measured 4 entities out where 5 were meant."""
    op = {"op": opname, "sketch": "S", "entities": ["e3"], key: True,
          **({"dx": 5, "dy": 5} if opname == "sketch_move"
             else {"cx": 0, "cy": 0, "angle": 30})}
    out, notes = _norm_edit(op)
    assert out["copy"] is True
    assert key not in out
    assert notes


@pytest.mark.parametrize("written,expected", [
    ("polar", "circular"), ("radial", "circular"), ("rectangular", "linear"),
    ("grid", "linear"),
])
def test_sketch_pattern_mode_words(written, expected):
    out, _ = _norm_edit({"op": "sketch_pattern_entities", "sketch": "S",
                         "entities": ["e3"], "mode": written, "count": 4,
                         "center": [0, 0], "angle": 90})
    assert out["mode"] == expected


def test_circular_sketch_pattern_full_sweep_becomes_equal_spacing():
    """`angle` is the PER-STEP rotation, so a 360° step is the identity and all four copies
    landed exactly on the source — an op that reported ok and produced invisible
    duplicates (live probe 2026-07-30, the same "seven hexagons at the origin" failure the
    2026-07-28 keychain run hit one layer up). A 360° per-step is provably a no-op, so
    reading it as the TOTAL sweep can never lose a legitimate intent."""
    out, notes = _norm_edit({"op": "sketch_pattern_entities", "sketch": "S",
                             "entities": ["e3"], "mode": "circular", "count": 4,
                             "center": [0, 0], "angle": 360})
    assert out["equal_spacing"] is True
    assert notes and "PER-STEP" in notes[0]


def test_partial_sweep_circular_pattern_is_left_alone():
    """90° per step is a real, distinct placement for every copy — nothing to infer."""
    out, _ = _norm_edit({"op": "sketch_pattern_entities", "sketch": "S",
                         "entities": ["e3"], "mode": "circular", "count": 4,
                         "center": [0, 0], "angle": 90})
    assert "equal_spacing" not in out


def test_offset_without_a_side_is_a_teaching_rejection():
    """The engine's «requires 'side': [u, v]» does not say what `side` MEANS, and the
    model's next guess is usually a word ("left") or another number."""
    with pytest.raises(ValueError) as ex:
        _norm_edit({"op": "sketch_offset", "sketch": "S", "entities": ["e1"],
                    "distance": 3})
    assert "side the copy goes" in str(ex.value)


def test_offset_both_sides_is_rejected_rather_than_silently_halved():
    """{"both_sides":true} was accepted and ignored — one side came out, and nothing said
    so. The engine offsets one side per op; two ops is the honest answer."""
    with pytest.raises(ValueError) as ex:
        _norm_edit({"op": "sketch_offset", "sketch": "S", "entities": ["e1"],
                    "distance": 3, "side": [0, 1], "both_sides": True})
    assert "two ops" in str(ex.value)


def test_trim_of_a_multi_entity_selection_is_rejected_not_silently_narrowed():
    with pytest.raises(ValueError) as ex:
        _norm_edit({"op": "sketch_trim", "sketch": "S", "entities": ["e1", "e2"],
                    "point": [10, 0]})
    assert "ONE entity per op" in str(ex.value)


def test_canonical_editing_ops_round_trip_unchanged():
    """A canonical op must emit NO note — a note claims a rewrite happened."""
    for op in ({"op": "sketch_trim", "sketch": "S", "entity": "e1", "point": [10, 0]},
               {"op": "sketch_offset", "sketch": "S", "entities": ["e1"],
                "distance": 3, "side": [0, 1]},
               {"op": "sketch_fillet", "sketch": "S", "e1": "e1", "e2": "e2",
                "radius": 2},
               {"op": "sketch_pattern_entities", "sketch": "S", "entities": ["e3"],
                "mode": "linear", "count": 3, "dir": [1, 0], "spacing": 8}):
        out, notes = _norm_edit(op)
        assert out == op
        assert notes == []


# ===========================================================================
# Live 2026-07-30, run-13566637896867 ("sports_bike"). Two defects in one turn,
# both of the worst kind: the model said what it wanted and the harness did
# something else.
# ===========================================================================

def test_subtract_flag_cuts_instead_of_silently_fusing():
    """{"subtract": true} was consumed by nothing, so the engine FUSED the wheels.

    The model then got "cylinder boss added no material ... this primitive lies
    entirely inside it" -- true of the boss it was silently turned into, and
    completely silent about the cut it actually asked for.
    """
    out = M._normalize_primitive_feature({
        "type": "cylinder", "name": "front_tire_fix",
        "params": {"radius": {"value": 300}, "height": {"value": 120},
                   "center": [700, 0, 240], "axis": [0, 1, 0], "subtract": True}})
    assert out.get("mode") == "cut"
    assert "subtract" not in json.dumps(out)
    assert out["at"] == [700, 0, 240]


@pytest.mark.parametrize("key,want", [
    ("subtract", "cut"), ("cut", "cut"), ("difference", "cut"), ("remove", "cut"),
    ("union", "boss"), ("fuse", "boss"), ("boss", "boss"),
])
def test_every_boolean_flag_spelling_reaches_a_mode(key, want):
    out = M._normalize_primitive_feature(
        {"type": "box", "params": {"length": 10, key: True}})
    assert out.get("mode") == want


def test_false_flag_leaves_the_default_and_an_explicit_mode_wins():
    off = M._normalize_primitive_feature(
        {"type": "box", "params": {"length": 10, "subtract": False}})
    assert "mode" not in off and "subtract" not in json.dumps(off)
    explicit = M._normalize_primitive_feature(
        {"type": "box", "mode": "boss", "params": {"length": 10, "subtract": True}})
    assert explicit["mode"] == "boss"


def test_uninterpretable_flag_value_is_left_for_the_engine_to_name():
    """Never re-create the silent drop: only a real yes/no is consumed."""
    out = M._normalize_primitive_feature(
        {"type": "box", "params": {"length": 10, "subtract": {"weird": 1}}})
    assert out["params"]["subtract"] == {"weird": 1}
    assert "mode" not in out


def test_set_param_is_read_as_edit_feature():
    """"unknown op 'set_param'. closest: set_material, set_units, set_appearance,
    set_folder, set_rollback" -- five ops sharing only the `set_` prefix, while
    edit_feature (the answer) was never suggested."""
    notes: list = []
    out = M._normalize_op_elements(
        [{"op": "set_param", "feature": "f1", "param": "center",
          "value": [700, 0, 300]}], notes)
    assert out == [{"op": "edit_feature", "id": "f1",
                    "params": {"center": [700, 0, 300]}}]
    assert any("edit_feature" in n for n in notes), notes


def test_param_edit_accepts_the_params_dict_form():
    notes: list = []
    out = M._normalize_op_elements(
        [{"op": "update_parameter", "id": "f3", "params": {"radius": 12}}], notes)
    assert out == [{"op": "edit_feature", "id": "f3", "params": {"radius": 12}}]


def test_param_edit_aimed_at_nothing_is_refused_not_guessed():
    with pytest.raises(ValueError, match="names no feature"):
        M._normalize_op_elements([{"op": "set_param", "param": "r", "value": 5}], [])
    with pytest.raises(ValueError, match='needs either "params"'):
        M._normalize_op_elements([{"op": "set_param", "feature": "f1"}], [])


def test_set_dim_is_not_hijacked_from_the_sketch_op():
    """set_dim could genuinely mean sketch_set_dim; guessing between two real ops
    is how a normalizer starts confidently building the wrong thing."""
    assert "set_dim" not in M._PARAM_EDIT_OPS
    assert "set_dimension" not in M._PARAM_EDIT_OPS


# ===========================================================================
# Live 2026-07-30, run-21784882079258 (a car). Four wheels arrived as
# {"radius":325,"depth":220,...} and the whole batch was rejected with
# "params.height is required" -- a COMPLETE spec refused over a spelling, and
# `depth` is the canonical key for the extrude the same model had just written.
# Probing the sibling primitives found three more of the same class.
# ===========================================================================

def test_cylinder_depth_is_its_height():
    notes: list = []
    out = M._check_cylinder_height(
        {"type": "cylinder", "name": "WheelFL",
         "params": {"radius": 325, "depth": 220, "center": [1200, 790, 325],
                    "axis": "Y", "mode": "boss"}}, "cylinder", notes)
    assert out["params"]["height"] == 220
    assert "depth" not in out["params"]
    assert notes and "height" in notes[0]


@pytest.mark.parametrize("alias", ["depth", "length", "thickness", "extent", "h"])
def test_every_cylinder_length_spelling_is_accepted(alias):
    out = M._check_cylinder_height(
        {"type": "cylinder", "name": "C", "params": {"radius": 5, alias: 12}},
        "cylinder", [])
    assert out["params"]["height"] == 12


def test_a_cylinder_with_no_length_at_all_is_still_refused():
    with pytest.raises(ValueError, match="height is required"):
        M._check_cylinder_height(
            {"type": "cylinder", "name": "C", "params": {"radius": 5}}, "cylinder", [])


@pytest.mark.parametrize("ftype,params,want", [
    ("box", {"length": 10, "width": 10, "depth": 10},
     {"length": 10, "width": 10, "height": 10}),
    ("cone", {"radius1": 20, "radius2": 5, "depth": 40},
     {"r1": 20, "r2": 5, "height": 40}),
    ("torus", {"radius": 20, "tube_radius": 5},
     {"major_r": 20, "minor_r": 5}),
])
def test_primitive_param_renames(ftype, params, want):
    out = M._normalize_primitive_dims(
        {"type": ftype, "name": "T", "params": dict(params)}, ftype, [])
    assert out["params"] == want


def test_a_rename_never_overwrites_an_explicit_canonical_key():
    """box{length,depth,height}: `depth` is NOT the height here, so leave it and let
    the engine name the real gap (width) rather than invent one."""
    out = M._normalize_primitive_dims(
        {"type": "box", "name": "T", "params": {"length": 10, "depth": 10, "height": 10}},
        "box", [])
    assert out["params"] == {"length": 10, "depth": 10, "height": 10}


def test_single_radius_cone_gets_a_point_on_top():
    """The engine requires r1 AND r2 because it builds frustums too, so the textbook
    cone was refused with "r1: required; r2: required" and no hint that r2 is the TOP
    radius or that a point means 0."""
    for params in ({"radius": 20, "height": 40}, {"r1": 20, "height": 40}):
        notes: list = []
        out = M._normalize_primitive_dims(
            {"type": "cone", "name": "T", "params": dict(params)}, "cone", notes)
        assert out["params"]["r1"] == 20 and out["params"]["r2"] == 0
        assert any("POINT on top" in n for n in notes), notes


def test_a_frustum_keeps_both_radii():
    out = M._normalize_primitive_dims(
        {"type": "cone", "name": "T", "params": {"r1": 20, "r2": 8, "height": 40}},
        "cone", [])
    assert out["params"] == {"r1": 20, "r2": 8, "height": 40}


# ------------------------------------------- pattern directions & bolt-circle PCD
# Free repertoire probe 2026-08-04: a canonically-typed `linear_pattern` carrying
# `params.direction1`/`direction2` — the CAD-UI wording every mechanical model has
# read a thousand times — was rejected with «refs.dir1: required for linear_pattern»,
# and `direction2` had NO alias at all, so a two-way grid was only expressible as the
# literal `refs.dir2`. `bolt_circle` was worse: nothing but the literal key `pcd`
# reached the slot, and `bolt_circle_d` — an alias core/doc/document.cpp explicitly
# accepts — died in validate.cpp before the compiler ever ran.

@pytest.mark.parametrize("spelling", ["direction1", "direction_1", "dir_1", "axis1",
                                      "direction", "dir"])
def test_every_first_direction_spelling_reaches_refs_dir1(spelling):
    feat = _pipeline({"type": "linear_pattern", "name": "Row",
                      "params": {"count1": 3, "spacing1": 35, spelling: "x"},
                      "refs": {"features": ["f2"]}})
    assert feat["refs"]["dir1"] == "axis.x", feat
    assert spelling not in feat.get("params", {})


@pytest.mark.parametrize("spelling", ["direction2", "direction_2", "dir_2", "axis2"])
def test_every_second_direction_spelling_reaches_refs_dir2(spelling):
    feat = _pipeline({"type": "linear_pattern", "name": "Grid",
                      "params": {"count1": 3, "spacing1": 35, "direction1": "x",
                                 "count2": 2, "spacing2": 40, spelling: "y"},
                      "refs": {"features": ["f2"]}})
    assert feat["refs"]["dir1"] == "axis.x", feat
    assert feat["refs"]["dir2"] == "axis.y", feat
    assert feat["params"]["count2"] == 2 and feat["params"]["spacing2"] == 40


def test_the_two_directions_never_swallow_each_other():
    """A dir1 lookup that matched "direction2" would build one row along the wrong axis."""
    feat = _pipeline({"type": "linear_pattern", "name": "Col",
                      "params": {"count2": 2, "spacing2": 40, "direction2": "y",
                                 "count1": 3, "spacing1": 35},
                      "refs": {"features": ["f2"]}})
    assert feat["refs"].get("dir1") is None, feat
    assert feat["refs"]["dir2"] == "axis.y", feat


@pytest.mark.parametrize("spelling", ["bolt_circle_d", "bolt_circle_diameter", "bcd",
                                      "circle_diameter", "pitch_circle_diameter",
                                      "pattern_diameter", "bolt_hole_circle"])
def test_every_bolt_circle_diameter_spelling_reaches_pcd(spelling):
    notes: list = []
    out = M._normalize_feature_dims(
        {"type": "bolt_circle", "name": "BC",
         "params": {"diameter": 6, "count": 6, spelling: 60}}, "bolt_circle", notes)
    assert out["params"]["pcd"] == 60, out
    assert spelling not in out["params"]
    if spelling != "pcd":
        assert any("pcd" in n for n in notes), notes


def test_a_bolt_circle_radius_is_never_read_as_the_diameter():
    """Filling pcd from a radius would put the ring at half its size, silently.

    The engine's «params.pcd: required» names the canonical key, which is the honest
    answer — a scaled guess is not."""
    out = M._normalize_feature_dims(
        {"type": "bolt_circle", "name": "BC",
         "params": {"diameter": 6, "count": 6, "bolt_circle_radius": 30}},
        "bolt_circle", [])
    assert "pcd" not in out["params"]
    assert out["params"]["bolt_circle_radius"] == 30


def test_an_explicit_pcd_wins_over_a_spare_spelling():
    out = M._normalize_feature_dims(
        {"type": "bolt_circle", "name": "BC",
         "params": {"diameter": 6, "count": 6, "pcd": 60, "bcd": 999}},
        "bolt_circle", [])
    assert out["params"]["pcd"] == 60


# ------------------------------------------------------------------ arc dialects
# Live run probe-holster-3 (k3-256k, 2026-08-03) lost FOUR consecutive add_features
# calls to the arc entity alone: the canonical form is centre + radius + start/end
# ANGLES, and the model reached in turn for endpoint+centre, `arc3` with a mid point,
# and start_angle/end_angle. Each of those fully determines one arc, so each builds.

def _arc(entity):
    feat = _sketch_ops({"op": "sketch", "name": "prof", "plane": "XY",
                        "entities": [entity]})
    return feat["params"]["entities"][0]


def test_arc_from_endpoints_and_centre_becomes_angles():
    out = _arc({"type": "arc", "start": [76, 55], "end": [44, 55],
                "center": [60, 39], "sweep": "ccw"})
    assert out["center"] == [60.0, 39.0]
    assert out["start"] == pytest.approx(45.0)
    assert out["end"] == pytest.approx(135.0)
    assert out["radius"] == pytest.approx(22.62741699)
    assert out["ccw"] is True
    # the dialect keys must not survive into the engine payload
    assert "sweep" not in out


def test_arc_sweep_word_cw_is_honoured():
    out = _arc({"type": "arc", "start": [1, 0], "end": [0, 1],
                "center": [0, 0], "sweep": "clockwise"})
    assert out["ccw"] is False
    assert out["start"] == pytest.approx(0.0)
    assert out["end"] == pytest.approx(90.0)


def test_three_point_arc_expands_with_the_traversal_direction():
    out = _arc({"type": "arc3", "start": [76, 55], "mid": [60, 39], "end": [44, 55]})
    assert out["type"] == "arc"
    assert out["center"] == pytest.approx([60.0, 55.0])
    assert out["radius"] == pytest.approx(16.0)
    assert out["start"] == pytest.approx(0.0)
    assert out["end"] == pytest.approx(180.0)
    # start -> mid -> end runs BELOW the centre, i.e. clockwise
    assert out["ccw"] is False
    assert "mid" not in out


def test_arc_start_angle_end_angle_spelling():
    out = _arc({"type": "arc", "center": [60, 55], "radius": 16,
                "start_angle": 0, "end_angle": 180})
    assert out["start"] == 0.0 and out["end"] == 180.0
    assert "start_angle" not in out and "end_angle" not in out


def test_canonical_arc_is_left_alone():
    out = _arc({"type": "arc", "center": [0, 0], "radius": 5,
                "start": 0, "end": 90, "ccw": True})
    assert out["center"] == [0, 0] and out["radius"] == 5
    assert out["start"] == 0 and out["end"] == 90 and out["ccw"] is True


@pytest.mark.parametrize("entity, message", [
    ({"type": "arc", "start": [0, 0], "mid": [5, 0], "end": [10, 0]}, "collinear"),
    ({"type": "arc", "start": [0, 0], "end": [10, 0]}, "infinitely many arcs"),
    ({"type": "arc", "start": [0, 0], "end": [10, 0], "center": [0, 5]},
     "not the same distance from the centre"),
    ({"type": "arc", "start": [0, 0], "end": [10, 0], "center": [5, 0],
      "radius": 99}, "disagrees with the endpoints"),
    ({"type": "arc", "start": [1, 0], "end": [0, 1], "center": [0, 0],
      "sweep": "widdershins"}, "neither clockwise nor counter-clockwise"),
])
def test_an_underspecified_or_contradictory_arc_is_taught_not_guessed(entity, message):
    """Tolerance is never guessing: each rejection names the values it received."""
    with pytest.raises(ValueError) as ei:
        _arc(entity)
    assert message in str(ei.value)


def test_edit_feature_target_nested_in_a_feature_wrapper_is_hoisted():
    """Live probe-holster-7: preflight_ops accepted the flat spelling, then the same
    edit written with an add_feature-style `feature` wrapper died on
    «edit_feature: no feature ''» with the id sitting inside the wrapper."""
    notes: list = []
    ops = M._normalize_op_elements(
        [{"op": "edit_feature",
          "feature": {"id": "f3", "params": {"origin": [0, 15, 60]}}}], notes)
    assert ops == [{"op": "edit_feature", "id": "f3",
                    "params": {"origin": [0, 15, 60]}}]
    assert any("feature.id" in n for n in notes), notes


def test_delete_feature_keeps_reading_the_flat_spellings():
    notes: list = []
    assert M._normalize_op_elements(
        [{"op": "delete_feature", "feature_id": "f3"}], notes,
    ) == [{"op": "delete_feature", "id": "f3"}]
    # …and a call that was already canonical is untouched and unremarked.
    quiet: list = []
    assert M._normalize_op_elements(
        [{"op": "edit_feature", "id": "f3", "params": {"radius": 5}}], quiet,
    ) == [{"op": "edit_feature", "id": "f3", "params": {"radius": 5}}]
    assert quiet == []


def test_edit_feature_placement_beside_params_moves_the_feature():
    """c219 FixToolFlange: {"op":"edit_feature","id":"f8","at":[…],"params":{"height":…}}
    applied the height and silently dropped the move — the engine reads only
    params/refs/name, and a primitive's placement lives in its params."""
    notes: list = []
    ops = M._normalize_op_elements(
        [{"op": "edit_feature", "id": "f8", "at": [5.6569, 5.6569, 304.5],
          "centered": False, "params": {"height": {"value": 4.8, "expr": None}}}], notes)
    assert ops == [{"op": "edit_feature", "id": "f8", "params": {
        "height": {"value": 4.8, "expr": None}, "at": [5.6569, 5.6569, 304.5],
        "centered": False}}]
    assert any("moved" in n and "params" in n for n in notes), notes


@pytest.mark.parametrize(("op", "needle"), [
    ({"op": "edit_feature", "id": "f8", "suppressed": True}, "set_suppressed"),
    ({"op": "edit_feature", "id": "f8", "radius": 3}, "inside \"params\""),
    ({"op": "edit_feature", "id": "f8", "at": [0, 0, 1], "params": {"at": [0, 0, 2]}},
     "both beside and inside"),
])
def test_edit_feature_never_drops_a_key_it_cannot_apply(op, needle):
    with pytest.raises(ValueError, match="edit_feature") as err:
        M._normalize_op_elements([op], [])
    assert needle in str(err.value)


# ------------------------------------------------------- asm_edit_component re-pose
# Live session: "asm_edit_component no-ops on this document". Two silent-success paths
# were behind it — the at/rotate pose dialect the op never reads, and a pose the
# assembly rebuild re-solves away. Neither may ever report success again.

def test_asm_edit_component_at_dialect_becomes_the_transform_the_op_reads():
    notes: list = []
    ops = M._normalize_asm_component_pose(
        [{"op": "asm_edit_component", "id": "k2", "at": [0, 0, 20]}], notes)
    assert ops[0]["transform"] == [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 20, 0, 0, 0, 1]
    # …and the ignored keys are gone, not left beside it.
    assert "at" not in ops[0]
    assert notes


def test_asm_edit_component_rotation_dialect_is_carried_into_the_matrix():
    ops = M._normalize_asm_component_pose(
        [{"op": "asm_edit_component", "id": "k2",
          "rotate": [0, 0, 1], "angle": 90, "at": [5, 0, 0]}], [])
    tf = ops[0]["transform"]
    assert abs(tf[0]) < 1e-12 and abs(tf[1] + 1) < 1e-12  # cos90, -sin90
    assert tf[3] == 5.0


def test_an_explicit_transform_wins_over_pose_keys_beside_it():
    ident = [1, 0, 0, 7, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]
    notes: list = []
    ops = M._normalize_asm_component_pose(
        [{"op": "asm_edit_component", "id": "k2", "transform": ident,
          "at": [99, 0, 0]}], notes)
    assert ops[0]["transform"] == ident and "at" not in ops[0]
    assert notes


def _asm_tree(monkeypatch, fixed: bool, mates: list) -> None:
    monkeypatch.setattr(M.cb, "call", lambda *_a, **_k: {"tree": {
        "components": [
            {"id": "k1", "fixed": True,
             "transform": [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]},
            {"id": "k2", "fixed": fixed,
             "transform": [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]}],
        "mates": mates}})


_T111 = [1, 0, 0, 111.0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]
_MATE_M1 = [{"id": "m1", "type": "coincident",
             "refs": [{"component": "k1"}, {"component": "k2"}]}]


def test_reposing_a_mated_component_is_refused_instead_of_silently_solved_away(monkeypatch):
    _asm_tree(monkeypatch, fixed=False, mates=_MATE_M1)
    out = M._asm_repose_refusal(
        [{"op": "asm_edit_component", "id": "k2", "transform": _T111}], "doc-x")
    assert out is not None
    assert out["ok"] is False and out["reason"] == "component_is_mated"
    assert out["applied"] is False
    assert out["components"] == {"k2": ["m1"]}
    # The refusal has to teach the ways the component CAN be moved.
    assert "asm_edit_mate" in out["hint"] and "simulate_motion" in out["hint"]


def test_a_suppressed_mate_does_not_own_the_pose(monkeypatch):
    _asm_tree(monkeypatch, fixed=False,
              mates=[{**_MATE_M1[0], "suppressed": True}])
    assert M._asm_repose_refusal(
        [{"op": "asm_edit_component", "id": "k2", "transform": _T111}], "doc-x") is None


def test_grounding_the_component_in_the_same_batch_is_not_refused(monkeypatch):
    _asm_tree(monkeypatch, fixed=False, mates=_MATE_M1)
    assert M._asm_repose_refusal(
        [{"op": "asm_edit_component", "id": "k2", "transform": _T111, "fixed": True}],
        "doc-x") is None
    # …and an already-grounded component was never solver-owned to begin with.
    _asm_tree(monkeypatch, fixed=True, mates=_MATE_M1)
    assert M._asm_repose_refusal(
        [{"op": "asm_edit_component", "id": "k2", "transform": _T111}], "doc-x") is None


def test_a_non_pose_component_edit_costs_no_probe_and_is_never_refused(monkeypatch):
    def _boom(*_a, **_k):
        raise AssertionError("no doc_tree probe for a batch with no transform edit")

    monkeypatch.setattr(M.cb, "call", _boom)
    assert M._asm_repose_refusal(
        [{"op": "asm_edit_component", "id": "k2", "name": "Lid"}], "doc-x") is None


def test_a_pose_the_rebuild_discarded_is_reported_not_claimed():
    tree = {"components": [{"id": "k2",
                            "transform": [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]}]}
    rejects = M._asm_transform_rejects(
        [{"op": "asm_edit_component", "id": "k2", "transform": _T111}], tree)
    assert [r["component"] for r in rejects] == ["k2"]
    assert rejects[0]["actual"][3] == 0.0 and rejects[0]["requested"][3] == 111.0
    # A pose the document really carries is not a reject.
    assert M._asm_transform_rejects(
        [{"op": "asm_edit_component", "id": "k2", "transform": _T111}],
        {"components": [{"id": "k2", "transform": _T111}]}) == []


@pytest.fixture
def transform_part():
    if not M.cb.available():
        pytest.skip("native engine unavailable")
    doc = M.cb.call("doc_new", {"kind": "part", "name": "transform_" + uuid.uuid4().hex})["doc_id"]
    result = M.cb.call("doc_apply_ops", {"doc_id": doc, "ops": [
        M.cb.feat("box", f"Body{i}", {"length": 10, "width": 10, "height": 10,
                                    "at": [100 * i, 0, 0], "mode": "new"})
        for i in range(3)]})
    yield doc, result["id_map"]["$1"]
    M.cb.call("doc_close", {"doc_id": doc})


def _transform_apply(doc, feature):
    from services.mcp.test_cad_tools import _call
    result, _ = _call("add_features", {"doc_id": doc, "ops": [feature]})
    return result


def test_transform_nested_rotation_moves_only_middle_body(transform_part):
    doc, body = transform_part
    result = _transform_apply(doc, {"type": "transform", "body": body, "params": {
        "translate": [1820, 0, 1090], "rotate": {"axis": [0, 1, 0], "angle": 180}}})
    assert result["ok"] and result["body_count"] == 3, result
    mass = M.cb.call("eval_mass", {"doc_id": doc})
    bodies = sorted(mass["bodies"], key=lambda b: b["com"][0])
    for actual, expected in zip(bodies, [[0, 0, 0], [200, 0, 0], [1720, 0, 1090]]):
        assert actual["com"] == pytest.approx(expected, abs=1e-8)
    assert [b["volume_mm3"] for b in bodies] == pytest.approx([1000] * 3)
    copied = _transform_apply(doc, {"type": "transform", "body": body, "params": {
        "translate": [0, 0, 50], "copy": True}})
    assert copied["ok"] and copied["body_count"] == 4, copied
    assert M.cb.call("eval_mass", {"doc_id": doc})["volume_mm3"] == pytest.approx(4000)


@pytest.mark.parametrize("body", ["missing-body", {"unrecognized": "body"}])
def test_transform_unresolved_body_is_atomic_refusal(transform_part, body):
    doc, _ = transform_part
    result = _transform_apply(doc, {"type": "transform", "body": body,
                                  "params": {"translate": [100, 0, 0]}})
    assert not result["ok"], result
    assert "body" in json.dumps(result).lower()
    mass = M.cb.call("eval_mass", {"doc_id": doc})
    assert len(mass["bodies"]) == 3
    assert sorted(b["com"][0] for b in mass["bodies"]) == pytest.approx([0, 100, 200])


def test_transform_consumed_body_never_resurrects_a_copy(transform_part):
    doc, body = transform_part
    # A later boss consumes the original feature, unlike a transform lineage.
    M.cb.call("doc_apply_ops", {"doc_id": doc, "ops": [
        M.cb.feat("box", "Boss", {"length": 10, "width": 10, "height": 10,
                                "at": [105, 0, 0], "mode": "add"})]})
    before = M.cb.call("eval_mass", {"doc_id": doc})
    result = _transform_apply(doc, {"type": "transform", "body": body,
                                  "params": {"translate": [0, 0, 100]}})
    assert not result["ok"], result
    assert "consumed" in json.dumps(result) and body in json.dumps(result)
    after = M.cb.call("eval_mass", {"doc_id": doc})
    assert len(after["bodies"]) == len(before["bodies"]) == 3
    assert after["volume_mm3"] == pytest.approx(before["volume_mm3"])


def test_assembly_transform_and_suppression_use_component_ids(transform_part, tmp_path):
    doc, feature = transform_part
    source = str(tmp_path / "component.step")
    M.cb.call("doc_export_step", {"doc_id": doc, "path": source})
    assembly = M.cb.call("doc_new", {"kind": "assembly", "name": uuid.uuid4().hex})["doc_id"]
    try:
        inserted = M.cb.call("doc_apply_ops", {"doc_id": assembly, "ops": [{
            "op": "asm_insert_component", "source": {"path": source}, "fixed": False,
            "transform": [1, 0, 0, 10, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]}]})
        component = inserted["id_map"]["$0"]
        result = _transform_apply(assembly, {"type": "transform", "body": component,
            "params": {"translate": [20, 0, 30], "rotate": {"axis": [0, 0, 1], "angle": 90}}})
        assert result["ok"], result
        tree = M.cb.call("doc_tree", {"doc_id": assembly})["tree"]
        assert len(tree["components"]) == 1
        assert [tree["components"][0]["transform"][i] for i in (3, 7, 11)] == pytest.approx([20, 10, 30])
        copied = _transform_apply(assembly, {"type": "transform", "body": component,
            "params": {"translate": [0, 0, 100], "copy": True}})
        assert copied["ok"], copied
        assert M.cb.call("eval_mass", {"doc_id": assembly})["volume_mm3"] == pytest.approx(6000)
        copy_tree = M.cb.call("doc_tree", {"doc_id": assembly})["tree"]
        other = next(c["id"] for c in copy_tree["components"] if c["id"] != component)
        M.cb.call("doc_apply_ops", {"doc_id": assembly, "ops": [{
            "op": "asm_delete_component", "id": other}]})
        result = _transform_apply(assembly, {"op": "set_suppressed", "id": component, "value": True})
        assert result["ok"], result
        assert M.cb.call("doc_tree", {"doc_id": assembly})["tree"]["components"][0]["suppressed"]
        assert result["body_count"] == 0, result
        restored = _transform_apply(assembly, {"op": "set_suppressed", "id": component, "value": False})
        assert restored["ok"], restored
        assert M.cb.call("eval_mass", {"doc_id": assembly})["volume_mm3"] == pytest.approx(3000)
        anchor = M.cb.call("doc_apply_ops", {"doc_id": assembly, "ops": [{
            "op": "asm_insert_component", "source": {"path": source}, "fixed": True}]})["id_map"]["$0"]
        M.cb.call("doc_apply_ops", {"doc_id": assembly, "ops": [
            {"op": "asm_edit_component", "id": component, "fixed": False}]})
        face = M.cb.call("doc_query", {"doc_id": doc, "query": {"of": "face", "side": "top"}})["matches"][0]
        M.cb.call("doc_apply_ops", {"doc_id": assembly, "ops": [{
            "op": "asm_add_mate", "mate": {"type": "coincident", "refs": [
                {"component": component, "selector": face},
                {"component": anchor, "selector": face}]}}]})
        refused = _transform_apply(assembly, {"type": "transform", "body": component,
            "params": {"translate": [100, 0, 0]}})
        assert not refused["ok"] and "mate" in json.dumps(refused).lower(), refused
        missing = _transform_apply(assembly, {"type": "transform", "body": "missing-component",
            "params": {"translate": [100, 0, 0]}})
        assert not missing["ok"] and "missing-component" in json.dumps(missing), missing
        for target_doc, wrong_id in ((assembly, feature), (doc, component)):
            refused = _transform_apply(target_doc, {"op": "set_suppressed", "id": wrong_id, "value": True})
            assert not refused["ok"] and wrong_id in json.dumps(refused), refused
    finally:
        M.cb.call("doc_close", {"doc_id": assembly})


@pytest.mark.parametrize("shape", ["flat_op", "query_refs"])
def test_asm_add_mate_accepts_the_live_flat_and_query_shapes(transform_part, tmp_path, shape):
    """Live 2026-10-10 (six-axis arm): {op, type, align, refs} failed "requires a 'mate'
    object", then refs [{component, query}] failed "selector must be an object"."""
    doc, _feature = transform_part
    source = str(tmp_path / "component.step")
    M.cb.call("doc_export_step", {"doc_id": doc, "path": source})
    assembly = M.cb.call("doc_new", {"kind": "assembly", "name": uuid.uuid4().hex})["doc_id"]
    try:
        ids = [M.cb.call("doc_apply_ops", {"doc_id": assembly, "ops": [{
            "op": "asm_insert_component", "source": {"path": source}, "fixed": fixed}]}
        )["id_map"]["$0"] for fixed in (True, False)]
        top = M.cb.call("doc_query", {"doc_id": doc, "query": {"of": "face", "side": "top"}})
        centroid = top["matches"][0]["centroid"]
        query = {"of": "face", "surf": "plane", "near": centroid, "count": 1}
        refs = [{"component": cid, "query": query} for cid in ids]
        mate = {"type": "coincident", "align": "aligned", "refs": refs}
        if shape == "flat_op":  # the live flat batch also sent distance values as numbers
            mate = {**mate, "type": "distance", "value": 0}
        op = ({"op": "asm_add_mate", **mate} if shape == "flat_op"
              else {"op": "asm_add_mate", "mate": mate})
        result = _transform_apply(assembly, op)
        assert result["ok"], result
        mates = M.cb.call("doc_tree", {"doc_id": assembly})["tree"]["mates"]
        assert len(mates) == 1 and mates[0]["type"] == mate["type"], mates
        if shape == "flat_op":
            assert mates[0]["value"]["value"] == 0, mates
        assert [r["component"] for r in mates[0]["refs"]] == ids, mates
        assert all(r["selector"].get("centroid") == pytest.approx(centroid)
                   for r in mates[0]["refs"]), mates
    finally:
        M.cb.call("doc_close", {"doc_id": assembly})


@enginetest
def test_sketch_replacement_normalizes_strings_without_rebinding_canonical_handles():
    """Replacement and partial edits preserve the sketch frame and handle identities."""
    from services.agent.agent_turn import _bind_tool
    add = _bind_tool("add_features")
    doc = _cb.call("doc_new", {"name": "sketch_replacement_handles"})["doc_id"]
    try:
        initial = json.loads(add(doc_id=doc, ops=[
            {"type": "sketch", "refs": {"plane": "plane.front"}, "params": {
                "offset": 3, "entities": [{"id": "e7", "type": "circle", "center": [0, 0],
                                          "radius": 5, "construction": False}],
                "constraints": [{"id": "c11", "type": "fix", "refs": ["e7"]}]}},
            {"type": "extrude", "refs": {"profile": "$0"}, "params": {"depth": 4}}]))
        assert initial["ok"], initial
        sketch, body = initial["id_map"]["$0"], initial["id_map"]["$1"]
        replacement = json.loads(add(doc_id=doc, ops=[{
            "op": "edit_feature", "id": sketch, "params": {
                "entities": json.dumps([
                    {"id": "e7", "type": "circle", "center": [0, 0], "radius": 6},
                    {"id": "bore", "type": "circle", "center": [0, 0], "radius": 2}]),
                "constraints": json.dumps([
                    {"id": "locked", "type": "fixed", "refs": ["e7"]},
                    {"id": "c1", "type": "fix", "refs": ["bore"]}])}},
            {"op": "edit_feature", "id": body, "refs": {"profile": sketch}}]))
        assert replacement["ok"], replacement
        # add_features' mass readback is tessellation-based for curved profiles.
        assert replacement["volume_mm3"] == pytest.approx(128 * math.pi, rel=1e-3)
        feature = _cb.call("doc_tree", {"doc_id": doc})["tree"]["features"][0]
        assert feature["refs"]["plane"] == "plane.front"
        assert feature["params"]["offset"] == 3
        entities, constraints = feature["params"]["entities"], feature["params"]["constraints"]
        assert entities[0]["id"] == "e7"
        assert all(e["construction"] is False for e in entities)
        assert {c["refs"][0] for c in constraints} == {e["id"] for e in entities}
        assert len({c["id"] for c in constraints}) == 2

        # Only replace constraints: creation defaults must not clear the entity list.
        partial = json.loads(add(doc_id=doc, ops=[{"op": "edit_feature", "id": sketch,
            "params": {"constraints": json.dumps([
                {"type": "fix", "refs": [e["id"]]} for e in entities])}}]))
        assert partial["ok"] and partial["volume_mm3"] == pytest.approx(replacement["volume_mm3"]), partial
        after = _cb.call("doc_tree", {"doc_id": doc})["tree"]["features"][0]
        assert after["params"]["entities"] == entities
        assert after["refs"] == feature["refs"]

        # Only replace entities: retained constraints must still refer to the same circles.
        changed = [{**e, "radius": 7 if e["id"] == "e7" else 2} for e in entities]
        for e in changed:
            e.pop("construction")
        edited = json.loads(add(doc_id=doc, ops=[{"op": "edit_feature", "id": sketch,
            "params": {"entities": json.dumps(changed)}}]))
        assert edited["ok"] and edited["volume_mm3"] == pytest.approx(180 * math.pi, rel=1e-3), edited
        before = _cb.call("eval_mass", {"doc_id": doc})["com"]
        moved = json.loads(add(doc_id=doc, ops=[{"op": "edit_feature", "id": sketch,
            "params": {"offset": json.dumps({"expr": None, "value": -8.5}),
                       "constraints": json.dumps(after["params"]["constraints"])}}]))
        assert moved["ok"], moved
        final = _cb.call("eval_mass", {"doc_id": doc})
        assert final["volume_mm3"] == pytest.approx(180 * math.pi)
        assert math.dist(before, final["com"]) == pytest.approx(11.5)
    finally:
        _cb.call("doc_close", {"doc_id": doc})


@enginetest
@pytest.mark.parametrize("replace", [False, True])
def test_compound_sketch_fix_pins_all_segments_for_creation_and_replacement(replace):
    from services.agent.agent_turn import _bind_tool
    add = _bind_tool("add_features")
    doc = _cb.call("doc_new", {"name": "compound_sketch_fix"})["doc_id"]
    params = {"entities": [{"id": "e1", "type": "rectangle", "center": [0, 0],
                            "width": 20, "height": 12}],
              "constraints": [{"type": "fix", "refs": ["e1"]}]}
    try:
        first = json.loads(add(doc_id=doc, ops=[{
            "type": "sketch", "refs": {"plane": "plane.top"},
            "params": {"entities": [], "constraints": []} if replace else params}]))
        assert first["ok"], first
        sketch = first["id_map"]["$0"]
        if replace:
            result = json.loads(add(doc_id=doc, ops=[{"op": "edit_feature", "id": sketch,
                "params": {k: json.dumps(v) for k, v in params.items()}}]))
            assert result["ok"], result
        feature = _cb.call("doc_tree", {"doc_id": doc})["tree"]["features"][0]
        entities = feature["params"]["entities"]
        constraints = feature["params"]["constraints"]
        assert {c["refs"][0] for c in constraints if c["type"] == "fix"} == {e["id"] for e in entities}
        assert {"coincident", "horizontal", "vertical"} <= {c["type"] for c in constraints}
        extruded = json.loads(add(doc_id=doc, ops=[{
            "type": "extrude", "refs": {"profile": sketch}, "params": {"depth": 3}}]))
        assert extruded["ok"] and extruded["volume_mm3"] == pytest.approx(720), extruded

        refused = json.loads(add(doc_id=doc, ops=[{"op": "edit_feature", "id": sketch,
            "params": {"entities": params["entities"], "constraints": [
                {"type": "length", "refs": ["e1"], "value": 30}]}}]))
        assert not refused["ok"] and "ambiguous" in refused["error"], refused
        assert _cb.call("eval_mass", {"doc_id": doc})["volume_mm3"] == pytest.approx(720)
    finally:
        _cb.call("doc_close", {"doc_id": doc})


@enginetest
def test_same_batch_reorder_resolves_new_feature_and_before_placeholders():
    from services.agent.agent_turn import _bind_tool
    add = _bind_tool("add_features")
    doc = _cb.call("doc_new", {"name": "reorder_batch_handles"})["doc_id"]
    try:
        seeded = json.loads(add(doc_id=doc, ops=[_CANON_BOX]))
        assert seeded["ok"], seeded
        original = seeded["id_map"]["$0"]
        result = json.loads(add(doc_id=doc, ops=[
            {"type": "sketch", "name": "First", "refs": {"plane": "plane.top"},
             "params": {"entities": [], "constraints": []}},
            {"type": "sketch", "name": "Second", "refs": {"plane": "plane.front"},
             "params": {"entities": [], "constraints": []}},
            {"op": "reorder_feature", "id": "$0", "before": original},
            {"op": "reorder_feature", "id": "$1", "before": "$0"}]))
        assert result["ok"], result
        tree = _cb.call("doc_tree", {"doc_id": doc})["tree"]
        assert [f["id"] for f in tree["features"]] == [
            result["id_map"]["$1"], result["id_map"]["$0"], original]
        assert result["volume_mm3"] == pytest.approx(24000)
    finally:
        _cb.call("doc_close", {"doc_id": doc})


@enginetest
def test_sketch_convert_accepts_stringified_sid_only_ref_and_keeps_a_closed_profile():
    from services.agent.agent_turn import _bind_tool
    add = _bind_tool("add_features")
    doc = _cb.call("doc_new", {"name": "convert_sid_readback"})["doc_id"]
    try:
        initial = json.loads(add(doc_id=doc, ops=[{
            "type": "box", "params": {"length": 10, "width": 10, "height": 10}}]))
        assert initial["ok"], initial
        sid = _cb.call("doc_query", {"doc_id": doc,
            "query": {"of": "face", "side": "top"}})["matches"][0]["sid"]
        result = json.loads(add(doc_id=doc, ops=[
            {"type": "sketch", "refs": {"plane": "plane.top"},
             "params": {"entities": [], "constraints": [], "offset": 10}},
            {"op": "sketch_convert", "sketch": "$0", "fix": True,
             "refs": [json.dumps({"kind": "face", "selector": {"sid": sid}})]},
            {"type": "extrude", "refs": {"profile": "$0"},
             "params": {"depth": 2, "merge": False}}]))
        assert result["ok"], result
        assert result["body_count"] == 2 and result["volume_mm3"] == pytest.approx(1200)
    finally:
        _cb.call("doc_close", {"doc_id": doc})
