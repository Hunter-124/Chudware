"""Deterministic molding screens; numeric limits are synthetic test inputs, not guidance."""
import numpy as np
import pytest

from services.dfam.mesh import FaceInfo, PrintMesh
from services.mcp.dfm_common import DfmContext
from services.mcp import dfm_rows_molding as molding


def mesh_box(lower=(0, 0, 0), upper=(10, 10, 10), top_inset=0, inverted=False):
    x0, y0, z0 = lower
    x1, y1, z1 = upper
    vertices = np.array([[x0, y0, z0], [x1, y0, z0], [x1, y1, z0], [x0, y1, z0],
                         [x0 + top_inset, y0 + top_inset, z1],
                         [x1 - top_inset, y0 + top_inset, z1],
                         [x1 - top_inset, y1 - top_inset, z1],
                         [x0 + top_inset, y1 - top_inset, z1]], dtype=float)
    triangles = np.array([[0, 2, 1], [0, 3, 2], [4, 5, 6], [4, 6, 7],
                          [0, 1, 5], [0, 5, 4], [1, 2, 6], [1, 6, 5],
                          [2, 3, 7], [2, 7, 6], [3, 0, 4], [3, 4, 7]])
    if inverted:
        triangles = triangles[:, ::-1]
    return PrintMesh(vertices, triangles, np.arange(len(triangles)),
                     [FaceInfo(f"face{i}", 1, 0, "f1") for i in range(len(triangles))])


def joined(*meshes):
    vertices, triangles, indices, faces = [], [], [], []
    for mesh in meshes:
        triangles.append(mesh.triangles + sum(len(v) for v in vertices))
        indices.append(mesh.tri_face + len(faces))
        vertices.append(mesh.vertices)
        faces.extend(mesh.faces)
    return PrintMesh(np.concatenate(vertices), np.concatenate(triangles),
                     np.concatenate(indices), faces)


def context(mesh=None, direction=(0, 0, 1), source="caller", rules=None, options=None):
    rules = rules or {}
    sources = {key: "synthetic test rule" for key in rules}
    sources["draft.min_deg"] = "synthetic draft rule"
    profile = {"params": {"rules": rules, "rule_sources": sources,
                          "draft": {"min_deg": 1.0}}}
    ctx = DfmContext("test", "injection", profile, list(direction), source,
                     options=options or {})
    if mesh is not None:
        ctx._cache["mesh"] = mesh
    return ctx


def get(ctx, name):
    return molding.rows(ctx, [name])[0]


def test_undercut_depends_on_explicit_pull_and_reports_face_ownership():
    # Two separated solids produce four crossings along Z but two along X.
    mesh = joined(mesh_box(), mesh_box((0, 0, 15), (10, 10, 25)))
    vertical = get(context(mesh), "undercuts")
    horizontal = get(context(mesh, (1, 0, 0)), "undercuts")
    assert vertical["status"] == "fail"
    assert vertical["metrics"]["internal_crossings"] > 0
    assert vertical["regions"][0]["faces"]
    assert vertical["regions"][0]["built_by"] == ["f1"]
    assert len(vertical["regions"][0]["centroid"]) == 3
    assert horizontal["status"] == "pass"
    assert get(context(mesh, source="default"), "undercuts")["status"] == "not_evaluated"
    profile = get(context(mesh, source="profile"), "undercuts")
    inferred = get(context(mesh, source="build_dir"), "undercuts")
    assert profile["status"] == inferred["status"] == "fail"
    assert profile["metrics"]["direction_source"] == "profile"
    assert inferred["metrics"]["direction_source"] == "build_dir"
    assert profile["assumptions"] != inferred["assumptions"]


def test_undercut_closed_cavity_and_convex_part():
    outside = mesh_box()
    cavity = mesh_box((3, 3, 3), (7, 7, 7), inverted=True)
    assert get(context(outside), "undercuts")["status"] == "pass"
    assert get(context(joined(outside, cavity)), "undercuts")["status"] == "fail"


def test_signed_draft_reverses_with_pull_and_does_not_use_absolute_angle():
    tapered = mesh_box(top_inset=1)
    forward = get(context(tapered), "draft")
    reverse = get(context(tapered, (0, 0, -1)), "draft")
    assert forward["status"] == "pass"
    assert forward["metrics"]["min_signed_draft_deg"] > 1
    assert reverse["status"] == "fail"
    assert reverse["metrics"]["min_signed_draft_deg"] < -1
    assert reverse["regions"][0]["faces"]
    assert get(context(mesh_box()), "draft")["status"] == "fail"
    depth_dependent = context(tapered, rules={"draft_min_deg_per_mm_depth": 0.1})
    assert get(depth_dependent, "draft")["status"] == "not_evaluated"
    depth_dependent.direction = [0, 0, -1]
    assert get(depth_dependent, "draft")["status"] == "fail"


def test_missing_draft_citation_does_not_discard_undercut_result():
    ctx = context(mesh_box())
    ctx.profile["params"]["rule_sources"].clear()
    draft, undercuts = molding.rows(ctx, ["draft", "undercuts"])
    assert draft["status"] == "not_evaluated"
    assert undercuts["status"] == "pass"


def test_rib_ratio_uses_authored_thickness_and_caller_nominal_wall():
    ctx = context(rules={"rib_thickness_ratio_max": 0.5}, options={"target_wall_mm": 4})
    ctx._cache["features"] = [{"id": "rib1", "type": "rib", "params": {"thickness": {"value": 3}}}]
    ctx._cache["faces"] = [{"sid": "s1", "owner_feature": "rib1", "centroid": [1, 2, 3]}]
    result = get(ctx, "rib_thickness_ratio")
    assert result["status"] == "fail"
    assert result["regions"][0]["ratio"] == pytest.approx(0.75)
    assert result["regions"][0]["built_by"] == ["rib1"]
    assert result["fix_ops"] == []
    ctx.options.clear()
    assert get(ctx, "rib_thickness_ratio")["status"] == "not_evaluated"
    ctx.options["target_wall_mm"] = 8
    assert get(ctx, "rib_thickness_ratio")["status"] == "pass"


def test_rib_missing_rule_or_unclassified_geometry_is_not_a_pass():
    ctx = context(options={"target_wall_mm": 4})
    ctx._cache["features"] = [{"id": "r", "type": "rib", "params": {"thickness": 2}}]
    assert get(ctx, "rib_thickness_ratio")["status"] == "not_evaluated"
    ctx._cache["features"] = []
    assert get(ctx, "rib_thickness_ratio")["status"] == "not_evaluated"


def test_boss_wall_uses_coaxial_bore_and_shared_end_face(monkeypatch):
    ctx = context(rules={"boss_wall_ratio_max": 0.6}, options={"target_wall_mm": 4})
    ctx._cache["faces"] = [
        {"sid": "boss", "surf": "cylinder", "concavity": "boss", "body": 1,
         "axis": [0, 0, 1], "axis_point": [0, 0, 0], "radius": 6,
         "centroid": [0, 0, 3], "owner_feature": "post"},
        {"sid": "bore", "surf": "cylinder", "concavity": "bore", "body": 1,
         "axis": [0, 0, 1], "axis_point": [0, 0, 1], "radius": 3,
         "centroid": [0, 0, 3], "owner_feature": "hole"},
        {"sid": "end", "surf": "plane", "body": 1}]
    monkeypatch.setattr(molding.cb, "call", lambda method, params: {
        "edges": [{"between_sids": ["boss", "end"]}, {"between_sids": ["bore", "end"]}]})
    result = get(ctx, "boss_wall_ratio")
    assert result["status"] == "warn"
    assert result["regions"][0]["boss_wall_mm"] == pytest.approx(3)
    assert result["regions"][0]["wall_ratio"] == pytest.approx(0.75)
    ctx._cache["faces"][1]["axis_point"] = [1, 0, 1]
    assert get(ctx, "boss_wall_ratio")["status"] == "not_evaluated"


def test_wall_limits_and_unknown_nominal_uniformity(monkeypatch):
    ctx = context(rules={"wall_min_mm": 1, "wall_max_mm": 4,
                         "wall_uniformity_max_ratio": 2})
    ctx._cache["faces"] = [{"body": 1, "topo": 5, "sid": "s5", "owner_feature": "f1"}]
    sample = {"thin_regions": [{"body": 1, "face_ids": [5], "centroid": [2, 3, 4],
                                "min_thickness_mm": 0.5}]}
    monkeypatch.setattr(molding.cb, "call", lambda method, params: {
        "material": {"source": "document_material", "key": "abs"}})
    monkeypatch.setattr(molding, "_thickness", lambda ctx, threshold: (sample, 0.5, 2.0))
    wall = get(ctx, "molding_wall")
    assert wall["status"] == "fail"
    assert wall["regions"][0]["faces"] == ["s5"]
    assert wall["regions"][0]["built_by"] == ["f1"]
    assert get(ctx, "wall_uniformity")["status"] == "not_evaluated"
    monkeypatch.setattr(molding, "_thickness", lambda ctx, threshold: ({}, 2.0, 3.0))
    assert get(ctx, "molding_wall")["status"] == "pass"
    monkeypatch.setattr(molding, "_thickness", lambda ctx, threshold: ({}, 2.0, 5.0))
    assert get(ctx, "molding_wall")["status"] == "not_evaluated"
    monkeypatch.setattr(molding.cb, "call", lambda method, params: {
        "material": {"source": "document_material", "key": "pla"}})
    assert get(ctx, "molding_wall")["status"] == "not_evaluated"
    monkeypatch.setattr(molding.cb, "call", lambda method, params: {
        "material": {"source": "default", "key": "abs"}})
    assert get(ctx, "molding_wall")["status"] == "not_evaluated"


def test_mold_corner_sharp_and_blend_findings(monkeypatch):
    ctx = context(rules={"internal_corner_radius_wall_ratio": 0.5},
                  options={"target_wall_mm": 4})
    ctx._cache["faces"] = [{"sid": "blend", "owner_feature": "f2", "centroid": [4, 5, 6]}]
    monkeypatch.setattr(ctx, "edge_analysis", lambda **kwargs: {
        "edges": [{"kind": "concave", "mid": [1, 2, 3], "faces": ["s1", "s2"],
                   "owner_feature": "f1"}],
        "concave_blends": [{"face_sid": "blend", "radius_mm": 1, "owner_feature": "f2"}]})
    result = get(ctx, "corner_radius_molded")
    assert result["status"] == "fail"
    assert len(result["regions"]) == 2
    assert result["limits"]["min_radius_mm"] == 2
    assert result["fix_ops"] == []
    ctx.options.clear()
    assert get(ctx, "corner_radius_molded")["status"] == "not_evaluated"


@pytest.mark.skipif(not molding.cb.available(), reason="native engine unavailable")
def test_native_side_hole_obstructs_z_pull():
    doc = molding.cb.call("doc_new", {"kind": "part", "name": "mold_side_hole", "internal": True})["doc_id"]
    try:
        built = molding.cb.call("doc_apply_ops", {"doc_id": doc, "ops": [
            molding.cb.feat("box", "block", {"length": 30, "width": 30, "height": 30}),
            molding.cb.feat("hole", "cross hole", {"style": "simple", "end": "through_all",
                                                      "diameter": 8},
                            {"face": {"of": "face", "side": "front"}})]})
        assert all(s.get("ok") for s in built["rebuild"]["feature_status"]), built
        ctx = DfmContext(doc, "injection", {"params": {}}, [0, 0, 1], "caller")
        result = get(ctx, "undercuts")
        assert result["status"] == "fail", result
        assert result["metrics"]["internal_crossings"] > 0
        assert result["regions"][0]["faces"]
    finally:
        molding.cb.call("doc_close", {"doc_id": doc})


@pytest.mark.skipif(not molding.cb.available(), reason="native engine unavailable")
def test_native_boss_wall_uniformity():
    doc = molding.cb.call("doc_new", {"kind": "part", "name": "mold_boss", "internal": True})["doc_id"]
    try:
        built = molding.cb.call("doc_apply_ops", {"doc_id": doc, "ops": [
            molding.cb.feat("box", "wall", {"length": 50, "width": 40, "height": 2}),
            molding.cb.feat("cylinder", "boss", {"radius": 6, "height": 4, "at": [0, 0, 2.9]})]})
        assert all(s.get("ok") for s in built["rebuild"]["feature_status"]), built
        assigned = molding.cb.call("doc_apply_ops", {"doc_id": doc, "ops": [
            {"op": "set_material", "key": "abs"}]})
        assert all(s.get("ok") for s in assigned["rebuild"]["feature_status"]), assigned
        ctx = context(rules={"wall_uniformity_deviation_by_material": {"abs": 0.25}})
        ctx.doc_id = doc
        result = get(ctx, "wall_uniformity")
        assert result["status"] == "warn", result
        assert result["regions"]
        assert result["metrics"]["nominal_wall_mm"] > 0
        assert any(r["thickness_mm"] > result["limits"]["maximum_mm"] for r in result["regions"])
    finally:
        molding.cb.call("doc_close", {"doc_id": doc})
