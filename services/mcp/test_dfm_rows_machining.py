"""Behavioral tests for the machining-specific DfM screens."""
import pytest

from services.mcp.dfm_common import DfmContext
from services.mcp import dfm_rows_machining as machining


def context(*, features=(), faces=(), edges=(), blends=(), rules=None, options=None, process="cnc_mill"):
    rules = rules if rules is not None else {
        "min_tool_diameter_mm": 4,
        "internal_corner_radius_tool_ratio_min": 1,
        "internal_corner_radius_tool_ratio_recommended": 1.2,
        "hole_max_depth_to_dia_standard": 5,
        "pocket_max_depth_to_tool_dia": 4,
    }
    ctx = DfmContext("internal-test", process,
                     {"params": {"rules": rules, "rule_sources": {k: "test rule" for k in rules}}},
                     [0, 0, 1], "caller", options=options or {})
    ctx._cache["features"] = list(features)
    ctx._cache["faces"] = list(faces)
    ctx._cache["edges:0:None:concave"] = {
        "edges": list(edges), "concave_blends": list(blends), "truncated": False}
    return ctx


def one(ctx, name):
    return machining.rows(ctx, [name])[0]


def hole(fid="h1", **params):
    return {"id": fid, "type": "hole", "params": params}


def face(fid="h1", sid="face1"):
    return {"sid": sid, "surf": "cylinder", "owner_feature": fid, "centroid": [1, 2, 3]}


def test_sharp_vertical_corner_reports_location_and_fillet_operation():
    edge = {"kind": "concave", "curve": "line", "dir": [0, 0, 1], "mid": [2, 3, 4],
            "faces": ["f1", "f2"], "sid": "e1", "owner_feature": "cut"}
    horizontal = dict(edge, sid="e2", dir=[1, 0, 0])
    result = one(context(edges=[edge, horizontal]), "sharp_internal_corners")
    assert result["status"] == "fail"
    assert len(result["regions"]) == 1
    assert result["regions"][0]["centroid"] == [2, 3, 4]
    assert result["regions"][0]["faces"] == ["f1", "f2"]
    assert result["regions"][0]["built_by"] == ["cut"]
    assert result["fix_ops"] == []


def test_cutter_diameter_drives_radius_and_missing_rule_is_not_pass():
    blend = {"surf": "cylinder", "axis": [0, 0, 1], "radius_mm": 1,
             "face_sid": "blend", "adjacent_faces": ["wall"], "center": [1, 2, 3],
             "owner_feature": "round"}
    feature = {"id": "round", "type": "fillet", "params": {"radius": {"value": 1}}}
    result = one(context(features=[feature], blends=[blend], options={"tool_diameter_mm": 6}), "internal_corner_radius")
    assert result["status"] == "fail"
    assert result["limits"]["minimum_radius_mm"] == 3
    assert result["fix_ops"] == []
    assert one(context(rules={}), "sharp_internal_corners")["status"] == "not_evaluated"
    assert one(context(blends=[blend]), "internal_corner_radius")["status"] == "fail"


def test_incomplete_or_directionless_edges_are_not_passes():
    ctx = context(edges=[{"kind": "concave", "curve": "line"}])
    assert one(ctx, "sharp_internal_corners")["status"] == "not_evaluated"
    ctx = context()
    ctx._cache["edges:0:None:concave"]["truncated"] = True
    assert one(ctx, "sharp_internal_corners")["status"] == "not_evaluated"
    assert one(context(process="lathe"), "sharp_internal_corners")["status"] == "not_applicable"


def test_hole_depth_warning_and_unknown_through_hole():
    h = hole(diameter=4, depth=24, end="blind")
    result = one(context(features=[h], faces=[face()]), "hole_depth_diameter")
    assert result["status"] == "warn"
    assert result["regions"][0]["depth_to_diameter"] == 6
    assert result["fix_ops"] == []
    proposal = machining._hole_depth(context(features=[h], faces=[face()]))["fix_ops"][0]
    assert proposal["ops"][0]["params"]["depth"]["value"] == 20
    through = hole(diameter=4, end="through_all")
    assert one(context(features=[through]), "hole_depth_diameter")["status"] == "not_evaluated"
    assert one(context(), "hole_depth_diameter")["status"] == "not_evaluated"


def test_preferred_drill_series_warns_and_keeps_missing_sizes_unknown():
    h = hole(diameter=4.04, depth=5, end="blind")
    result = one(context(features=[h], faces=[face()]), "drill_size")
    assert result["status"] == "warn"
    assert result["fix_ops"] == []
    assert one(context(features=[hole(diameter=4.0)]), "drill_size")["status"] == "pass"
    assert one(context(features=[hole(diameter=50)]), "drill_size")["status"] == "not_evaluated"


def test_pocket_reach_and_unknown_depth(monkeypatch):
    monkeypatch.setattr(machining, "_cam_tool", lambda *args: None)
    p = {"id": "p1", "type": "extrude", "params": {"mode": "cut", "depth": 20, "end": "blind"}}
    result = one(context(features=[p], faces=[face("p1")]), "pocket_depth_reach")
    assert result["status"] == "warn"
    assert result["fix_ops"] == []
    p["params"]["end"] = "through_all"
    assert one(context(features=[p]), "pocket_depth_reach")["status"] == "not_evaluated"


def test_counterbore_and_tap_drill_against_selected_fastener():
    cb = hole(style="cbore", screw="M6", diameter=6, cbore_d=10, cbore_depth=6)
    result = one(context(features=[cb], faces=[face()]), "counterbore_fastener")
    assert result["status"] == "warn"
    assert result["fix_ops"] == []
    tap = hole(style="tap", thread="M6x1.0", diameter=4.5)
    result = one(context(features=[tap], faces=[face()]), "tap_drill_size")
    assert result["status"] == "warn"
    assert result["fix_ops"] == []
    assert one(context(features=[hole(style="tap", diameter=5)]), "tap_drill_size")["status"] == "not_evaluated"


def test_countersink_compares_selected_fastener():
    h = hole(style="csink", screw="M6", csink_d=10, csink_angle=82)
    result = one(context(features=[h], faces=[face()]), "countersink_fastener")
    assert result["status"] == "warn"
    assert result["regions"][0]["minimum_head_diameter_mm"] == 12
    assert result["regions"][0]["required_angle_deg"] == 90
    h["params"].update(csink_d=12, csink_angle=90)
    assert one(context(features=[h], faces=[face()]), "countersink_fastener")["status"] == "pass"
    h["params"]["screw"] = "unknown"
    assert one(context(features=[h]), "countersink_fastener")["status"] == "not_evaluated"


def test_tapped_thread_shorter_than_material_guideline_is_located(monkeypatch):
    from services.mcp import core_bridge as cb
    thread = {"id": "t1", "type": "thread", "params": {
        "style": "internal", "size": "M6", "length": 8}}
    monkeypatch.setattr(cb, "call", lambda method, params: {
        "material": {"source": "part", "key": "aluminum_6061_t6"}})
    ctx = context(features=[thread], faces=[face("t1")])
    result = one(ctx, "tap_engagement")
    assert result["status"] == "warn"
    assert result["regions"][0]["guideline_engagement_mm"] == 12
    assert result["regions"][0]["built_by"] == ["t1"]
    thread["params"]["length"] = 14
    assert one(ctx, "tap_engagement")["status"] == "not_evaluated"


def test_pocket_width_uses_concave_floor_and_opposed_wall_distance(monkeypatch):
    from services.mcp import core_bridge as cb
    floor = {"sid": "floor", "surf": "plane", "normal": [0, 0, 1],
             "centroid": [0, 0, 0], "owner_feature": "p"}
    left = {"sid": "left", "surf": "plane", "normal": [1, 0, 0],
            "centroid": [0, 0, 1], "owner_feature": "p"}
    right = {"sid": "right", "surf": "plane", "normal": [-1, 0, 0],
             "centroid": [3, 0, 1], "owner_feature": "p"}
    edges = [{"kind": "concave", "faces": ["floor", sid]} for sid in ("left", "right")]
    monkeypatch.setattr(cb, "call", lambda method, params: {"distance": 3}
                        if method == "eval_measure" else {})
    result = one(context(faces=[floor, left, right], edges=edges,
                         options={"tool_diameter_mm": 4}), "pocket_depth_width")
    assert result["status"] == "fail"
    assert result["regions"][0]["pocket_width_mm"] == 3
    assert result["regions"][0]["faces"] == ["floor", "left", "right"]


@pytest.mark.parametrize("check", [
    "tolerance_feasibility", "pocket_depth_width", "hole_edge_distance", "countersink_fastener",
])
def test_unavailable_measurements_are_not_invented(check):
    assert one(context(), check)["status"] == "not_evaluated"


def test_native_pocket_corner_returns_only_verified_fix():
    from services.mcp import core_bridge as cb

    if not cb.available():
        pytest.skip("native engine unavailable")
    doc = cb.call("doc_new", {"kind": "part", "name": "cnc_corner", "internal": True})["doc_id"]
    try:
        block = cb.call("doc_apply_ops", {"doc_id": doc, "ops": [
            cb.feat("box", "stock", {"length": 40, "width": 40, "height": 12})]})
        assert all(s.get("ok") for s in block["rebuild"]["feature_status"]), block
        pocket = cb.call("doc_apply_ops", {"doc_id": doc, "ops": [
            cb.feat("sketch", "pocket profile", {"entities": [
                {"type": "rectangle", "center": [0, 0], "width": 20, "height": 20}]},
                {"plane": {"of": "face", "side": "top"}}),
            cb.feat("extrude", "pocket", {"depth": 5, "mode": "cut", "flip": True},
                    {"profile": "$0"})]})
        assert all(s.get("ok") for s in pocket["rebuild"]["feature_status"]), pocket
        before = cb.call("doc_tree", {"doc_id": doc})["revision"]
        profile = {"params": {"rules": {"internal_corner_radius_tool_ratio_min": 1,
                                          "internal_corner_radius_tool_ratio_recommended": 1.1},
                              "rule_sources": {"internal_corner_radius_tool_ratio_min":
                                  "machining_reference.json internal-corner-radius-cutter"}}}
        ctx = DfmContext(doc, "cnc_mill", profile, [0, 0, 1], "caller",
                         options={"tool_diameter_mm": 4})
        row = one(ctx, "sharp_internal_corners")
        assert row["status"] == "fail", row
        assert row["regions"] and row["fix_ops"], row
        assert all(f["verified"] is True and f["verification"]["builds"]
                   for f in row["fix_ops"])
        assert cb.call("doc_tree", {"doc_id": doc})["revision"] == before
    finally:
        cb.call("doc_close", {"doc_id": doc})
