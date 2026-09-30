"""Persisted simulation/mate boundaries must be visible in the default render grid."""
from __future__ import annotations

import uuid

import pytest

from services.mcp import cad_common as cc
from services.mcp import cad_render_tools as render
from services.mcp import shot_planner as sp


class _Tools:
    def __init__(self):
        self.tools = {}

    def tool(self):
        def register(fn):
            self.tools[fn.__name__] = fn
            return fn
        return register


@pytest.fixture
def block(tmp_path, monkeypatch):
    import services.paths as paths
    monkeypatch.setattr(paths, "CACHE", tmp_path / "cache")
    if not cc.cb.available():
        pytest.skip("engine library unavailable")
    doc = cc.cb.call("doc_new", {"name": f"RenderBC_{uuid.uuid4().hex}",
                                  "resume_if_open": False})["doc_id"]
    cc.cb.call("doc_apply_ops", {"doc_id": doc, "ops": [
        cc.cb.feat("box", "Block", {"length": cc.cb.dim(80), "width": cc.cb.dim(40),
                                      "height": cc.cb.dim(20)})]})
    faces = cc.cb.call("doc_query", {"doc_id": doc, "query": {"of": "face"}})["matches"]
    try:
        yield doc, faces
    finally:
        cc.cb.call("doc_close", {"doc_id": doc})


@pytest.mark.parametrize("targeted", [False, True])
@pytest.mark.parametrize("stored_as", ["query", "sid", "gui"])
def test_persisted_fixed_and_loaded_faces_are_covered(block, monkeypatch, targeted, stored_as):
    doc, faces = block
    fixed = next(f for f in faces if f["side"] == "left")
    loaded = next(f for f in faces if f["side"] == "right")
    top = next(f for f in faces if f["side"] == "top")
    if stored_as == "query":
        fixtures, loads = ["left"], [{"face": "right", "force": [0, 0, -200]}]
    elif stored_as == "sid":
        fixtures, loads = [fixed["sid"]], [{"face": loaded["sid"], "force": [0, 0, -200]}]
    else:
        fixtures, loads = [fixed], [{"faces": [loaded], "type": "force", "vec": [0, 0, -200]}]
    cc.cb.call("sim_study_add", {"doc_id": doc, "study": {
        "type": "static", "name": "Cantilever setup", "fixtures": fixtures, "loads": loads}})
    observed = {}
    original = sp.plan_shots

    def plan(*args, **kwargs):
        observed["roles"] = kwargs["roles"]
        observed["plan"] = original(*args, **kwargs, mode="hand")
        return observed["plan"]

    monkeypatch.setattr(sp, "plan_shots", plan)
    tools = _Tools()
    render.register_cad_render_tools(tools)
    tools.tools["render_part"](doc_id=doc, target=top["sid"] if targeted else None)
    bc = {faces.index(fixed), faces.index(loaded)}
    assert set(observed["roles"]["bc"]) == bc
    planned = observed["plan"]
    assert planned["coverage"]["bc"] > 0
    # Each BC must actually occupy legible pixels in at least one selected tile,
    # not merely appear in the roles passed to the ranking function.
    for ordinal in bc:
        fi = next(i for i, f in enumerate(planned["metrics"]["faces"])
                  if f["ordinal"] == ordinal)
        assert any(any(int(row[0]) == fi and row[1] > 4 and row[3] > .15
                       for row in planned["metrics"]["views"][view["metrics_index"]]["face_vis"])
                   for view in planned["views"])
    if targeted:
        assert observed["roles"]["target"] == [faces.index(top)]
        assert set(observed["roles"]["context"]) == {
            i for i, face in enumerate(faces) if face["side"] not in ("top", "bottom")}


def test_stale_study_selector_never_becomes_all_faces(block):
    doc, faces = block
    stale = dict(faces[0], sid="F000000")
    cc.cb.call("sim_study_add", {"doc_id": doc, "study": {
        "type": "static", "name": "Stale pick", "fixtures": [stale], "loads": []}})
    assert render._planning_roles(doc)["bc"] == []


def test_gui_selector_without_sid_joins_exact_geometry(block):
    doc, faces = block
    fixed = dict(faces[0])
    fixed.pop("sid", None)
    cc.cb.call("sim_study_add", {"doc_id": doc, "study": {
        "type": "static", "name": "Legacy GUI pick", "fixtures": [fixed], "loads": []}})
    assert render._planning_roles(doc)["bc"] == [0]


def test_mate_selector_joins_transformed_world_geometry_not_local_sid():
    local = {"kind": "face", "sid": "Flocal", "surf": "plane",
             "centroid": [2, 0, 0], "area_or_len": 20, "normal": [1, 0, 0]}
    world = dict(local, sid="Fworld", centroid=[10, 2, 0], normal=[0, 1, 0])
    other = dict(world, sid="Fother", normal=[0, -1, 0])
    component = {"transform": [0, -1, 0, 10, 1, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]}
    topo = {"face": [other, world], "edge": []}
    ref = {"component": "k1", "selector": local}
    assert render._mate_face_matches(ref, {"k1": component}, topo) == [world]
    assert render._mate_face_matches(ref, {"k1": dict(component, suppressed=True)}, topo) == []
    assert render._mate_face_matches(ref, {"k1": component},
                                     {"face": [world, dict(world)], "edge": []}) == []


def test_contact_mate_faces_join_the_rendered_assembly(block, tmp_path):
    doc, faces = block
    source = tmp_path / "Beam.step+"
    cc.cb.call("doc_save", {"doc_id": doc, "path": str(source)})
    assembly = cc.cb.call("doc_new", {"kind": "assembly",
                                       "name": f"MateBC_{uuid.uuid4().hex}",
                                       "resume_if_open": False})["doc_id"]
    try:
        cc.cb.call("doc_apply_ops", {"doc_id": assembly, "ops": [
            {"op": "asm_insert_component", "source": {"path": str(source)}, "name": "First"},
            {"op": "asm_insert_component", "source": {"path": str(source)}, "name": "Second",
             "transform": [1, 0, 0, 80, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]}]})
        components = cc.cb.call("doc_tree", {"doc_id": assembly})["tree"]["components"]
        right = next(f for f in faces if f["side"] == "right")
        left = next(f for f in faces if f["side"] == "left")
        cc.cb.call("doc_apply_ops", {"doc_id": assembly, "ops": [
            {"op": "asm_add_mate", "mate": {"type": "coincident", "align": "anti",
             "refs": [{"component": components[0]["id"], "selector": right},
                      {"component": components[1]["id"], "selector": left}]}}]})
        joined = cc.cb.call("doc_query", {"doc_id": assembly,
                                          "query": {"of": "face"}})["matches"]
        expected = {i for i, f in enumerate(joined)
                    if f["surf"] == "plane"
                    and abs(f["centroid"][0] - right["centroid"][0]) < 1e-4
                    and abs(f["area_or_len"] - right["area_or_len"]) < 1e-4}
        assert len(expected) == 2
        target = next(f for f in joined if f["side"] == "top")
        roles = render._planning_roles(assembly, [target])
        assert set(roles["bc"]) == expected
        assert roles["target"] == [joined.index(target)]
    finally:
        cc.cb.call("doc_close", {"doc_id": assembly})


@pytest.mark.parametrize("xray", [False, True])
@pytest.mark.parametrize("invariant", ["hidden", "anchor", "unique"])
def test_bc_sid_tags_are_unique_and_anchor_only_their_winning_pixels(
        monkeypatch, tmp_path, xray, invariant):
    import numpy as np
    from PIL import Image

    positions, triangles, triangle_faces = [], [], []
    # The outer BC owns a ring around the nearer target, while the hidden BC
    # has no winning pixels. X-ray must use target winners, not the cover.
    for fi, (extent, depth) in enumerate(
            ((10, 2 if xray else -2), (7, 0), (3, 1), (2, -1))):
        offset = len(positions)
        positions.extend([[-extent, -extent, depth], [extent, -extent, depth],
                          [extent, extent, depth], [-extent, extent, depth]])
        triangles.extend([[offset, offset + 1, offset + 2],
                          [offset, offset + 2, offset + 3]])
        triangle_faces.extend([fi, fi])
    mesh = {"pos": np.asarray(positions, dtype=float),
            "idx": np.asarray(triangles, dtype=np.int32),
            "tri_face": np.asarray(triangle_faces, dtype=np.int32),
            "faces": [{}, {}, {}, {}], "edges": []}
    matches = [{"sid": sid, "centroid": [0, 0, 0]}
               for sid in ("Fcover", "Fouter", "Finner", "Fhidden")]
    topo = {"face": matches, "face_sid": {m["sid"]: i + 1 for i, m in enumerate(matches)},
            "edge_sid": {}}
    specs = [{"label": m["sid"], "matches": [m], "colour": (200, 60, 240),
              "colour_name": "purple", "kind": "face"}
             for m in (matches[1], matches[3])]
    monkeypatch.setattr(cc, "_doc_mesh", lambda _doc: mesh)
    monkeypatch.setattr(cc, "_doc_topo_maps", lambda _doc: topo)
    monkeypatch.setattr(cc, "_mesh_topo_index",
                        lambda _mesh: ({i + 1: i for i in range(4)}, {}))
    monkeypatch.setattr(cc, "_resolve_highlight_specs", lambda *_args: specs)
    monkeypatch.setattr(cc, "_handoff_doc_name", lambda _doc: "BC tag visibility")
    W, H = 512, 384
    frame = cc._mesh_render_frame(mesh, (0, 0, 1), W, H)
    _, winners, _ = cc._mesh_depth_buffer(mesh, frame)
    if xray:
        target_mesh = dict(mesh, idx=mesh["idx"][2:6], tri_face=mesh["tri_face"][2:6])
        _, target_winners, _ = cc._mesh_depth_buffer(target_mesh, frame)
        winners[target_winners >= 0] = target_winners[target_winners >= 0]
    placed = []
    place = cc._place_label

    def record(draw, font, anchor, text, *args, **kwargs):
        rect = place(draw, font, anchor, text, *args, **kwargs)
        sid = text.removeprefix("* ")
        if rect is not None and sid in topo["face_sid"]:
            placed.append((sid, text, anchor))
        return rect

    monkeypatch.setattr(cc, "_place_label", record)
    source = tmp_path / "bc_source.png"
    Image.new("RGB", (W, H), (180, 190, 200)).save(source)
    _, info = cc._annotate_render(
        str(source), "bc-tags", (0, 0, 1), frame=frame,
        out_png=str(tmp_path / "bc_tags.png"), highlight=["Fouter", "Fhidden"],
        callouts=False, edges=False, scale=False,
        emphasis={"matches": matches[1:3], "xray": xray, "draw_label": False},
        face_tags={"max": 4, "priority": {2: 1, 3: 0, 4: 1}, "min_px": 8})
    assert info["available"], info["note"]
    assert not (winners == 3).any()
    if invariant == "hidden":
        assert all(sid != "Fhidden" for sid, _text, _anchor in placed)
    elif invariant == "anchor":
        for sid, _text, (ax, ay) in placed:
            assert winners[ay, ax] == topo["face_sid"][sid] - 1
    else:
        assert len({sid for sid, _text, _anchor in placed}) == len(placed)
        assert ("Fouter", "* Fouter") in [(sid, text) for sid, text, _anchor in placed]
        assert {tag["sid"] for tag in info["tags"]} == {
            sid for sid, _text, _anchor in placed}
