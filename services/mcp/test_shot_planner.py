"""Shot planner: engine-measured candidate cameras, set-ranked views, the lettered grid.

The engine is faked at the cb.call seam (like test_inspect_tools): `fake_view_metrics`
answers doc_view_metrics for a convex part — a face is visible from every camera it faces,
at |cos| = n . d — so these tests pin the planner's decisions, not the rasterizer's.
Run: python3 -m pytest services/mcp/test_shot_planner.py -q
"""
from __future__ import annotations

import math

import numpy as np
import pytest

from services.mcp import cad_render_tools as ct_render
from services.mcp import cad_tools as ct
from services.mcp import shot_planner as sp
from services.viewpoint.utility import FEATURE_SCHEMA, context_from, hand_utility, seen_faces
from services.viewpoint.utility.model import _SHAPES

_SURF_TYPE = {"plane": 0, "cylinder": 1, "cone": 2, "sphere": 3, "torus": 4, "bspline": 5}


def _unit(v):
    v = np.asarray(v, dtype=np.float64)
    return v / np.linalg.norm(v)


def faces_from_matches(matches):
    """doc_view_metrics face rows for doc_query face matches, in doc_query order (one body,
    so face_id i+1 is doc_query topo i+1). A match carrying `_hidden` is occluded from every
    camera: projected but never visible."""
    return [{"body": 1, "face_id": i + 1,
             "surf_type": _SURF_TYPE.get(str(m.get("surf")), 6),
             "area_mm2": float(m.get("area_or_len") or 1.0),
             "centroid": list(m.get("centroid") or [0.0, 0.0, 0.0]),
             "normal": list(m.get("normal") or m.get("axis") or [0.0, 0.0, 1.0]),
             "tri_count": 2, "hidden": bool(m.get("_hidden"))}
            for i, m in enumerate(matches)]


def fake_view_metrics(params, faces):
    """A doc_view_metrics answer for a convex part (see module docstring)."""
    W, H = int(params.get("width") or 192), int(params.get("height") or 144)
    views = []
    for cam in params["cameras"]:
        d = _unit(np.subtract(cam["position"], cam["target"]))
        rows, by_normal = [], {}
        for i, f in enumerate(faces):
            n = _unit(f["normal"])
            c = float(np.dot(n, d))
            if c <= 0.05:
                continue
            px = f["area_mm2"] * c
            vis = 0.0 if f["hidden"] else px
            rows.append([i, vis, 0.0 if f["hidden"] else f["area_mm2"], c, px])
            if vis:
                key = tuple(np.round(n, 3).tolist())
                by_normal[key] = by_normal.get(key, 0.0) + vis
        tot = sum(by_normal.values())
        ent = -sum(v / tot * math.log2(v / tot) for v in by_normal.values()) if tot else 0.0
        views.append({"face_vis": rows, "edge_vis": [], "bg_px": 0, "cap_px": 0,
                      "depth_edge_px": 0, "normal_entropy": ent,
                      "principal_align": float(np.abs(d).max()), "fg_px": int(tot)})
    return {"ok": True, "revision": 1, "width": W, "height": H,
            "faces": [{k: f[k] for k in ("body", "face_id", "surf_type", "area_mm2",
                                         "centroid", "normal", "tri_count")} for f in faces],
            "edges": [], "views": views, "timing_ms": 1.0}


def _face(sid, side, centroid, normal, area, **extra):
    return dict({"kind": "face", "surf": "plane", "side": side, "sid": sid,
                 "centroid": list(centroid), "normal": list(normal), "axis": list(normal),
                 "area_or_len": float(area), "radius": None}, **extra)


def _box(*extra):
    """A 40 x 30 x 20 box centred on the origin, plus any extra faces."""
    return [
        _face("Ftop", "top", (0, 0, 10), (0, 0, 1), 1200),
        _face("Fbot", "bottom", (0, 0, -10), (0, 0, -1), 1200),
        _face("Ffront", "front", (0, -15, 0), (0, -1, 0), 800),
        _face("Fback", "back", (0, 15, 0), (0, 1, 0), 800),
        _face("Fright", "right", (20, 0, 0), (1, 0, 0), 600),
        _face("Fleft", "left", (-20, 0, 0), (-1, 0, 0), 600),
    ] + list(extra)


_BOX_BB = {"min": [-20, -15, -10], "max": [20, 15, 10], "size": [40, 30, 20],
           "center": [0, 0, 0]}


def _plate(pocket: bool):
    """A 100 x 100 x 5 plate; with `pocket`, a pocket floor opens on the UNDERSIDE."""
    faces = [
        _face("Ftop", "top", (0, 0, 2.5), (0, 0, 1), 10000),
        _face("Fbot", "bottom", (0, 0, -2.5), (0, 0, -1), 9600),
        _face("Ffront", "front", (0, -50, 0), (0, -1, 0), 500),
        _face("Fback", "back", (0, 50, 0), (0, 1, 0), 500),
        _face("Fright", "right", (50, 0, 0), (1, 0, 0), 500),
        _face("Fleft", "left", (-50, 0, 0), (-1, 0, 0), 500),
    ]
    if pocket:
        faces.append(_face("Fpocket", "bottom", (0, 0, 1.0), (0, 0, -1), 400))
    return faces


_PLATE_BB = {"min": [-50, -50, -2.5], "max": [50, 50, 2.5], "size": [100, 100, 5],
             "center": [0, 0, 0]}


def _install(monkeypatch, matches, bbox):
    """Fake the engine for one document; returns the (method, params) call log."""
    from PIL import Image as PILImage

    faces = faces_from_matches(matches)
    calls: list = []

    def call(method, params):
        calls.append((method, params))
        if method == "doc_bbox":
            return bbox
        if method == "doc_query":
            q = params.get("query") or {}
            if q.get("of") == "edge":
                return {"matches": []}
            side = q.get("side")
            return {"matches": [m for m in matches if side is None or m.get("side") == side]}
        if method == "doc_face_points":
            c = (params.get("selector") or {}).get("centroid") or [0, 0, 0]
            return {"points": [[c[0] + dx, c[1] + dy, c[2]] for dx in (-5, 5) for dy in (-5, 5)]}
        if method == "doc_view_metrics":
            return fake_view_metrics(params, faces)
        if method == "doc_render_view":
            PILImage.new("RGB", (512, 400), (180, 190, 200)).save(params["out_path"], "PNG")
            return {"ok": True, "path": params["out_path"]}
        if method == "doc_tree":
            return {"owner_map": {}, "tree": {"features": [
                {"id": "f1", "name": "Box", "type": "box"}]}}
        raise AssertionError(f"unexpected method {method}")

    monkeypatch.setattr(ct.cb, "call", call)
    monkeypatch.setattr(ct_render, "_doc", lambda doc_id=None: doc_id or "doc-test")
    return calls


def _tool(name):
    from mcp.server.fastmcp import FastMCP
    mcp = FastMCP("test-shot-planner")
    ct.register_cad_tools(mcp)
    return mcp._tool_manager._tools[name].fn


def _weights(path, schema):
    rng = np.random.default_rng(3)
    arrays = {k: rng.normal(0.0, 0.1, size=s) for k, s in _SHAPES.items()}
    arrays["norm_face_std"] = np.ones(_SHAPES["norm_face_std"])
    arrays["norm_ctx_std"] = np.ones(_SHAPES["norm_ctx_std"])
    np.savez(path, feature_schema=np.array(schema), trained_on=np.array("test"),
             report_sha=np.array("0"), kind_bias=np.zeros(6), **arrays)
    return path


# ---------------------------------------------------------------------------------- sets
def test_a_complementary_set_beats_four_near_duplicate_views():
    dups = [(-1, -1, 1), (-1, -3, 1), (-3, -1, 1), (-1, -1, 3)]     # all see -X, -Y, +Z
    others = [(1, 1, -1), (1, -1, -1), (-1, 1, -1)]
    dirs = dups + others
    m = fake_view_metrics({"cameras": [{"position": (100 * _unit(d)).tolist(),
                                        "target": [0, 0, 0]} for d in dirs]},
                          faces_from_matches(_box()))
    for i, f in enumerate(m["faces"]):
        f["ordinal"] = i
    for v, d in zip(m["views"], dirs):
        v["dir"], v["section"] = _unit(d).tolist(), None

    picks = sp.greedy_select(m, sp.set_scorer(m, None, context_from(m)), k=4)
    chosen = [p["index"] for p in picks]
    assert len(chosen) == 4
    assert sum(1 for i in chosen if i < len(dups)) <= 1, chosen
    assert dirs.index((1, 1, -1)) in chosen          # the exact opposite of the first pick
    assert seen_faces(m, chosen).all()
    assert hand_utility(m, chosen, None) > hand_utility(m, list(range(len(dups))), None) + 1.0


def test_the_underside_view_is_chosen_when_the_back_carries_the_feature(monkeypatch):
    _install(monkeypatch, _plate(pocket=False), _PLATE_BB)
    plain = sp.plan_shots("doc-plate", k=1, mode="hand")
    assert plain["views"][0]["dir"][2] > 0.0        # nothing underneath: look from above

    _install(monkeypatch, _plate(pocket=True), _PLATE_BB)
    one = sp.plan_shots("doc-plate", k=1, mode="hand")
    assert one["views"][0]["dir"][2] < 0.0          # the pocket pulls the camera underneath
    assert "Fpocket" in [b["sid"] for b in one["views"][0]["summary"]["best_faces"]]

    two = sp.plan_shots("doc-plate", k=2, mode="hand")
    zs = sorted(v["dir"][2] for v in two["views"])
    assert zs[0] < 0.0 < zs[1]
    assert "Fpocket" not in two["unseen"] and "Ftop" not in two["unseen"]


def _rib_box():
    """The box with a thin rib standing on its top face; the rib's informative faces are its
    two SIDES, which face opposite ways (+X and -X)."""
    return _box(_face("FribL", "left", (-1, 0, 15), (-1, 0, 0), 300),
                _face("FribR", "right", (1, 0, 15), (1, 0, 0), 300),
                _face("FribTop", "top", (0, 0, 20), (0, 0, 1), 60))


def test_a_feature_read_from_two_opposite_sides_gets_both_sides_and_no_flat_view(monkeypatch):
    faces = _rib_box()
    _install(monkeypatch, faces, {"min": [-20, -15, -10], "max": [20, 15, 20],
                                  "size": [40, 30, 30], "center": [0, 0, 5]})
    rib = {"matches": faces[6:8], "label": "rib", "kind": "feature",
           "bbox": {"min": [-1, -15, 10], "max": [1, 15, 20], "size": [2, 30, 10],
                    "center": [0, 0, 15]}}
    plan = sp.plan_shots("doc-rib", target=rib, k=4, mode="hand")
    xs = [v["dir"][0] for v in plan["views"]]
    # Each side of the rib is only legible from its own half-space: the set must hold both.
    assert max(xs) > 0.3 and min(xs) < -0.3, xs
    for v in plan["views"]:
        # No plan view that collapses a dimension, and no tile spent on the rest of the part.
        assert v["summary"]["principal_align"] <= 0.95, v["label"]
        assert v["summary"]["target_visible"] > 0.0, v["label"]
    dirs = np.asarray([v["dir"] for v in plan["views"]])
    cos = dirs @ dirs.T
    assert (cos[np.triu_indices(len(dirs), 1)] < math.cos(math.radians(30))).all(), cos


@pytest.mark.skipif(not ct.cb.available(), reason="engine library not staged")
def test_every_planner_tile_carries_edges_tags_tint_and_the_painted_target():
    cb = ct.cb
    d = cb.call("doc_new", {"name": "OverlayBlock", "resume_if_open": False})["doc_id"]
    try:
        cb.call("doc_apply_ops", {"doc_id": d, "ops": [
            cb.feat("box", "Block", {"length": cb.dim(60), "width": cb.dim(40),
                                     "height": cb.dim(30)}),
            cb.feat("hole", "Bore", {"style": "simple", "end": "through_all",
                                     "diameter": cb.dim(10)},
                    {"face": {"of": "face", "side": "top"}})]})
        top = next(m for m in cb.call("doc_query", {"doc_id": d, "query": {"of": "face"}})
                   ["matches"] if m.get("side") == "top")
        plan = sp.plan_shots(d, target=top["sid"], k=4, mode="hand")
        _png, info = sp.render_shot_grid(d, plan)
        assert info["overlay_problems"] == {} and "OVERLAY FAILED" not in info["caption"]
        for r in info["tiles"]:
            ann = r["annotation"]
            assert ann["hlr"] and ann["tags"], r["letter"]
            if not r["section"]:
                assert ann["edges"] and ann["tinted_px"] > 0, r["letter"]
            emp = ann["emphasis"]
            assert emp["target_px"] > 0 and top["sid"] in emp["visible_sids"], r["letter"]
    finally:
        cb.call("doc_close", {"doc_id": d})


def test_section_candidates_look_at_the_kept_halfs_cut_face():
    inner = {"min": [-3, -3, -3], "max": [3, 3, 3], "size": [6, 6, 6], "center": [0, 0, 0]}
    cands = sp.candidate_cameras(_BOX_BB, target_bbox=inner, target_normals=[(0, 0, 1)],
                                 internal=True)
    sections = [c for c in cands if c["section"]]
    assert sections and len(sections) < len(cands)
    normals = {tuple(np.round(c["section"]["normal"], 6)) for c in sections}
    for axis in ((1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1)):
        assert tuple(float(x) for x in axis) in normals
    for c in sections:
        n = np.asarray(c["section"]["normal"])
        # Kept half is dot(p - point, n) <= 0: the camera sits in the removed half, 45 deg
        # off the plane normal, so the cut face is in view.
        assert float(np.dot(c["dir"], n)) == pytest.approx(math.cos(math.radians(45)), abs=1e-6)
        pos = np.asarray(c["camera"]["position"]) - np.asarray(c["section"]["point"])
        assert float(np.dot(pos, n)) > 0.0


def test_mirrored_sections_do_not_repeat_a_cut_but_xray_can_add_context():
    origin = {"point": [0, 0, 0], "normal": [0, 1, 0]}
    mirrored = {"point": [0, 0, 0], "normal": [0, -1, 0]}
    views = [{"dir": [0, 1, 0], "section": origin, "reveal_key": "bore"},
             {"dir": [0, -1, 0], "section": mirrored, "reveal_key": "bore"},
             {"dir": [0, 0, 1], "section": None, "xray": True, "reveal_key": "bore"},
             {"dir": [0, 0, -1], "section": None}]
    picks = sp.greedy_select({"views": views}, lambda selected: len(selected), k=4,
                             pinned=[0])
    assert [p["index"] for p in picks] == [0, 2, 3]


def test_nearby_cameras_and_same_target_footprint_cannot_share_a_grid():
    directions = [(1, 0, 0), (.96, .28, 0), (.7, .7, 0), (0, 1, 0), (-1, 0, 0)]
    metrics = fake_view_metrics(
        {"cameras": [{"position": (100 * _unit(d)).tolist(), "target": [0, 0, 0]}
                     for d in directions]},
        faces_from_matches([_face("target", "right", (1, 0, 0), (1, 0, 0), 50)]))
    for v, direction in zip(metrics["views"], directions):
        v["dir"] = _unit(direction).tolist()
    metrics["views"][2]["face_vis"] = list(metrics["views"][0]["face_vis"])
    picks = sp.greedy_select(metrics, lambda selected: len(selected), k=5, target_faces=[0])
    chosen = {p["index"] for p in picks}
    assert 0 in chosen and 1 not in chosen  # 16-degree duplicate
    assert 2 not in chosen                   # 45-degree view, same target footprint


@pytest.mark.skipif(not ct.cb.available(), reason="engine library not staged")
def test_whole_part_grid_reveals_a_blind_bore_and_never_repeats_a_section_plane():
    cb = ct.cb
    d = cb.call("doc_new", {"name": "WholeBlindBore", "resume_if_open": False})["doc_id"]
    try:
        cb.call("doc_apply_ops", {"doc_id": d, "ops": [
            cb.feat("box", "Block", {"length": cb.dim(60), "width": cb.dim(40),
                                     "height": cb.dim(30)}),
            cb.feat("hole", "BlindBore", {"style": "simple", "end": "blind",
                                         "diameter": cb.dim(10), "depth": cb.dim(20)},
                    {"face": {"of": "face", "side": "top"}})]})
        plan = sp.plan_shots(d, k=4, mode="hand")
        bore_sids = {f["sid"] for f in plan["metrics"]["faces"]
                     if f.get("surf_type") == 1}
        assert bore_sids
        revealing = [v for v in plan["views"] if (v["section"] or v.get("xray"))
                     and bore_sids.intersection(
                         b["sid"] for b in v["summary"]["best_faces"])]
        assert revealing, [v["label"] for v in plan["views"]]
        planes = [sp._section_key(v["section"]) for v in plan["views"] if v["section"]]
        assert len(planes) == len(set(planes))
        assert len(plan["views"]) == 4
        # Only one tile may be a featureless reverse side when the bore is
        # otherwise available in an exterior or sectional view.
        cyl = {i for i, f in enumerate(plan["metrics"]["faces"])
               if f.get("surf_type") == 1}
        blank = 0
        for view in plan["views"]:
            if view["section"]:
                continue
            row = plan["metrics"]["views"][view["metrics_index"]]["face_vis"]
            if not any(int(r[0]) in cyl and float(r[1]) > 4 for r in row):
                blank += 1
        assert blank <= 1
        target_plan = sp.plan_shots(d, target="BlindBore", k=4, mode="hand")
        target_views = target_plan["views"]
        assert len(target_views) == 4
        dirs = np.asarray([v["dir"] for v in target_views])
        assert (dirs @ dirs.T)[np.triu_indices(4, 1)].max() <= (
            math.cos(math.radians(25)) + 1e-6)
        bore_axis = _unit(next(f["axis"] for f in target_plan["metrics"]["faces"]
                               if f.get("surf_type") == 1))
        assert sum(abs(float(np.dot(d, bore_axis))) >
                   math.cos(math.radians(35)) for d in dirs) <= 1
        assert any(v["section"] for v in target_views)
        xrays = [v for v in target_views if v.get("xray")]
        assert xrays and all(.32 <= abs(float(np.dot(v["dir"], bore_axis))) <= .78
                              and min(abs(v["dir"][0]), abs(v["dir"][1])) >= .25
                              for v in xrays)
    finally:
        cb.call("doc_close", {"doc_id": d})


@pytest.mark.skipif(not ct.cb.available(), reason="engine library not staged")
def test_cross_drilled_whole_part_reveals_its_intersecting_passages():
    cb = ct.cb
    d = cb.call("doc_new", {"name": "PlannerCrossDrill", "resume_if_open": False})["doc_id"]
    try:
        cb.call("doc_apply_ops", {"doc_id": d, "ops": [
            cb.feat("box", "Block", {"length": cb.dim(70), "width": cb.dim(50),
                                     "height": cb.dim(40)}),
            cb.feat("hole", "CrossX", {"style": "simple", "end": "through_all",
                                       "diameter": cb.dim(12), "at": [0, 0]},
                    {"face": {"of": "face", "side": "right"}}),
            cb.feat("hole", "CrossY", {"style": "simple", "end": "through_all",
                                       "diameter": cb.dim(10), "at": [0, 0]},
                    {"face": {"of": "face", "side": "front"}})]})
        plan = sp.plan_shots(d, k=4, mode="hand")
        passages = {i for i, f in enumerate(plan["metrics"]["faces"])
                    if f.get("surf_type") == 1}
        assert len(passages) >= 2
        assert any(v["section"] and sum(
            int(row[0]) in passages and float(row[1]) > 4
            for row in plan["metrics"]["views"][v["metrics_index"]]["face_vis"]) >= 2
                   for v in plan["views"])
    finally:
        cb.call("doc_close", {"doc_id": d})


# ---------------------------------------------------------------------------------- model
def test_a_model_with_a_mismatched_feature_schema_is_refused_for_hand(monkeypatch, tmp_path):
    _install(monkeypatch, _box(), _BOX_BB)
    monkeypatch.setattr(sp._um, "DEFAULT_WEIGHTS",
                        _weights(tmp_path / "old.npz", "viewutil.features/0"))
    plan = sp.plan_shots("doc-box", k=2)
    assert plan["model"] == "hand"
    assert "viewutil.features/0" in plan["model_note"] and FEATURE_SCHEMA in plan["model_note"]
    with pytest.raises(ValueError, match="refused"):
        sp.plan_shots("doc-box", k=2, mode="model")
    _png, info = sp.render_shot_grid("doc-box", plan)
    assert "Views ranked by hand (utility model refused" in info["caption"]

    monkeypatch.setattr(sp._um, "DEFAULT_WEIGHTS", _weights(tmp_path / "ok.npz", FEATURE_SCHEMA))
    assert sp.plan_shots("doc-box", k=2)["model"].startswith("utility_v1@")


# ---------------------------------------------------------------------------------- delivery
def test_the_default_render_is_one_lettered_grid_listing_the_faces_no_tile_sees(monkeypatch):
    hidden = _face("Fhidden", "top", (0, 0, 5), (0, 0, 1), 50, _hidden=True)
    calls = _install(monkeypatch, _box(hidden), _BOX_BB)
    img, caption = _tool("render_part")(doc_id="doc-box")
    renders = [p for m, p in calls if m == "doc_render_view"]
    assert len(renders) == 4
    # Every tile is an exact planner camera at the tile size.
    for p in renders:
        cam = p["view"]["camera"]
        assert cam["projection"] == "perspective" and (cam["width"], cam["height"]) == (512, 384)
    from PIL import Image as PILImage
    with PILImage.open(getattr(img, "path", img)) as im:
        assert im.size == (2 * 512 + 4, 2 * 400 + 4)
    for letter in "ABCD":
        assert f" {letter}: dir [" in caption
    unseen = caption.split("Faces NO tile sees", 1)[1].split("—", 1)[0]
    assert "Fhidden" in unseen
    assert "Ftop" not in unseen and "Fbot" not in unseen
    assert "Union legible coverage" in caption
    # The fake engine has no tessellation: every overlay is missing, and the caption says so.
    assert "OVERLAY FAILED" in caption and "tessellation unavailable" in caption


def test_an_explicit_view_is_still_one_single_shot(monkeypatch):
    calls = _install(monkeypatch, _box(), _BOX_BB)
    render_part = _tool("render_part")
    render_part(doc_id="doc-box", view="front")
    render_part(doc_id="doc-box", view="front", target={"of": "face", "side": "top"})
    assert sum(1 for m, _p in calls if m == "doc_render_view") == 2
    assert not any(m == "doc_view_metrics" for m, _p in calls)
