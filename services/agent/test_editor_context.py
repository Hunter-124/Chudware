"""Picked model context (composer context picker) rendered into the online prompt.

``worker_rpc._editor_context_block`` resolves each ``context_refs`` chip against the engine
(doc_tree / doc_query / doc_resolve_pick). A fake engine stands in for hostd here; the real
engine path is the same three calls.
"""

from __future__ import annotations

from services.agent import worker_rpc

DOC = "doc-cpp00000003"
ASM = "doc-cpp00000009"

TOP = {"kind": "face", "surf": "plane", "centroid": [0, 0, 20], "axis": [0, 0, 1],
       "normal": [0, 0, 1], "side": "top", "area_or_len": 1200.0, "owner_feature": "f1",
       "sid": "Fa1b2c3"}
EDGE = {"kind": "edge", "surf": "line", "centroid": [0, -15, 20], "axis": [1, 0, 0],
        "area_or_len": 40.0, "owner_feature": "f1", "sid": "E0f0f0f"}
PART_TREE = {"schema": "aicad.part/1", "features": [
    {"id": "f1", "name": "Block", "type": "box",
     "params": {"length": {"value": 40, "expr": None}, "width": {"value": 30, "expr": "w"},
                "height": {"value": 20, "expr": None}}},
    {"id": "f2", "name": "HoleSketch", "type": "sketch",
     "refs": {"plane": {"of": "face", "side": "top"}},
     "params": {"entities": [{"type": "circle"}, {"type": "circle"}, {"type": "line"}],
                "constraints": [{"type": "fix"}]}},
    {"id": "f3", "name": "Fillet1", "type": "fillet", "suppressed": True,
     "params": {"radius": {"value": 2, "expr": None}},
     "refs": {"edges": [{"kind": "edge", "owner_feature": "f1"}] * 4}},
]}
ASM_TREE = {"schema": "aicad.asm/1", "features": [], "components": [
    {"id": "k2", "name": "Wheel", "fixed": True, "source": {"path": "/work/wheel.step+"},
     "transform": [1, 0, 0, 100, 0, 1, 0, 0, 0, 0, 1, 5, 0, 0, 0, 1]},
]}


class FakeEngine:
    def __init__(self, live_faces=(TOP,), live_edges=(EDGE,), fail=False):
        self.calls: list[tuple[str, dict]] = []
        self.live = {"face": list(live_faces), "edge": list(live_edges)}
        self.fail = fail

    def __call__(self, method, params):
        self.calls.append((method, params))
        if self.fail:
            raise RuntimeError("engine unreachable")
        if method == "doc_tree":
            return {"tree": ASM_TREE if params["doc_id"] == ASM else PART_TREE}
        if method == "doc_query":
            return {"matches": self.live[params["query"]["of"]]}
        if method == "doc_resolve_pick":
            if params.get("revision") == 99:
                return {"code": "stale_revision", "message": "document moved on"}
            return {"selector": dict(TOP)}
        raise AssertionError(method)


def _block(refs, engine, **extra):
    params = {"scope": {"doc_id": DOC}, "context_refs": refs, **extra}
    return worker_rpc._editor_context_block(params, engine_call=engine)


def test_face_and_edge_carry_owner_live_geometry_and_targeting():
    engine = FakeEngine()
    text = _block([
        {"kind": "face", "id": "Fa1b2c3", "doc_id": DOC, "doc_name": "Bracket",
         "label": "Face of Block", "selector": TOP, "owner_feature": "f1"},
        {"kind": "edge", "id": "E0f0f0f", "doc_id": DOC, "label": "Edge of Block",
         "selector": EDGE},
    ], engine)
    assert '1. FACE Fa1b2c3 "Face of Block" — doc doc-cpp00000003 (Bracket)' in text
    assert 'owner: feature f1 "Block" (box)' in text
    assert "plane face, normal [0, 0, 1] (top), area 1200 mm², centroid [0, 0, 20]" in text
    assert "line edge, length 40 mm, from [-20, -15, 20] to [20, -15, 20]" in text
    assert text.count("status: verified against the live document") == 2
    # One doc_tree and one doc_query per kind, however many chips name the document.
    assert [m for m, _ in engine.calls].count("doc_tree") == 1
    assert [m for m, _ in engine.calls].count("doc_query") == 2


def test_moved_face_is_flagged_and_unresolved_pick_is_resolved_server_side():
    moved = _block([{"kind": "face", "id": "Fa1b2c3", "doc_id": DOC, "selector": TOP}],
                   FakeEngine(live_faces=()))
    assert "NOT in the current geometry" in moved
    assert "area 1200 mm²" in moved   # the as-picked values are still given

    engine = FakeEngine()
    sent_early = _block([{"kind": "face", "id": "face:1:6", "doc_id": DOC, "body_id": 1,
                          "topo_id": 6, "revision": 4}], engine)
    assert ("doc_resolve_pick", {"doc_id": DOC, "body_id": 1, "kind": "face", "topo_id": 6,
                                 "revision": 4}) in engine.calls
    assert "1. FACE Fa1b2c3" in sent_early and "face:1:6" not in sent_early

    stale = _block([{"kind": "edge", "id": "edge:1:2", "doc_id": DOC, "body_id": 1,
                     "topo_id": 2, "revision": 99}], FakeEngine())
    assert "(unresolved pick)" in stale and "document moved on" in stale


def test_feature_sketch_component_and_body_summaries():
    text = _block([
        {"kind": "feature", "id": "f3", "doc_id": DOC, "label": "Fillet1", "type": "fillet"},
        {"kind": "sketch", "id": "f2", "doc_id": DOC, "label": "HoleSketch"},
        {"kind": "component", "id": "k2", "doc_id": ASM, "label": "Wheel"},
        {"kind": "body", "id": "b1", "doc_id": DOC, "label": "Body1", "body_id": 1,
         "selector": {"kind": "body", "owner_feature": "f1"}},
        {"kind": "feature", "id": "f9", "doc_id": DOC, "label": "Gone"},
    ], FakeEngine())
    assert "fillet: radius=2; refs.edges: 4 ref(s) on f1; SUPPRESSED" in text
    assert ('sketch: on {"of":"face","side":"top"}; 3 entities (2 circle, 1 line); '
            "1 constraints") in text
    assert "part file /work/wheel.step+; placed at [100, 0, 5]; fixed" in text
    assert 'body ordinal 1; created by feature f1 "Block" (box)' in text
    assert "5. FEATURE f9" in text and "not found in the document's feature tree" in text


def test_assembly_face_names_component_frame_and_skips_live_verification():
    engine = FakeEngine()
    text = worker_rpc._editor_context_block(
        {"scope": {"doc_id": ASM}, "context_refs": [
            {"kind": "face", "id": "Fa1b2c3", "doc_id": ASM, "label": "Face of Wheel",
             "selector": TOP, "component": "k2", "owner_feature": "f1"}]},
        engine_call=engine)
    assert 'in component k2 "Wheel" (part file /work/wheel.step+)' in text
    assert "component's own part coordinates" in text
    assert "owner: feature f1 of that component's part" in text
    assert "doc_query" not in [m for m, _ in engine.calls]


def test_selection_dedupes_picked_faces_bounds_refs_and_survives_engine_failure():
    many = [{"kind": "feature", "id": f"f{i}", "doc_id": DOC} for i in range(30)]
    text = _block(many, FakeEngine(fail=True),
                  scope={"doc_id": DOC, "selection": [TOP, EDGE]})
    assert "(+6 more picked item(s) not shown)" in text
    assert "24. FEATURE f23" in text and "25. FEATURE" not in text

    deduped = worker_rpc._editor_context_block(
        {"scope": {"doc_id": DOC, "selection": [TOP, EDGE]},
         "context_refs": [{"kind": "face", "id": "Fa1b2c3", "doc_id": DOC, "selector": TOP}]},
        engine_call=FakeEngine())
    assert "- selected geometry (1): E0f0f0f: line edge" in deduped

    # No open document: the picked items are still described (they name their own docs).
    no_doc = worker_rpc._editor_context_block(
        {"context_refs": [{"kind": "sketch", "id": "f2", "doc_id": DOC}]},
        engine_call=FakeEngine())
    assert no_doc.startswith("PICKED CONTEXT") and "ACTIVE DOCUMENT" not in no_doc
    assert worker_rpc._editor_context_block({}, engine_call=FakeEngine()) == ""
