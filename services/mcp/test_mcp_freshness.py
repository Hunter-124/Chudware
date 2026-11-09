"""Behavioral checks for cad_guide topic aliases."""

from services.mcp.cad_guide import cad_guide_text


def test_thickness_guide_aliases_route_to_evaluate():
    text = cad_guide_text("evaluate")
    assert cad_guide_text("thickness") == text
    assert cad_guide_text("wall_thickness") == text


def test_sketch_tool_names_route_to_the_sketch_topic():
    text = cad_guide_text("sketch")
    for alias in ("sketch_probe", "sketch_health", "check_sketch", "sketch_fully_define"):
        assert cad_guide_text(alias) == text


def test_stress_guide_alias():
    assert cad_guide_text("stress-concentration") == cad_guide_text("stress_risers")


def test_invalid_body_and_fillet_adjustment_helpers():
    from services.mcp import cad_doc_tools as cdt
    assert cdt._invalid_body_warning({"invalid_bodies": []}) is None
    assert cdt._invalid_body_warning(None) is None
    text = cdt._invalid_body_warning({"invalid_bodies": [0]})
    assert text.startswith("result body invalid (brep_check)"), text
    feats = [{"id": "f2", "data": {"fillet": {
        "adjustment": "upper", "requested": {"radius": 3.0}, "effective": {"radius": 0.3}}}},
        {"id": "f3", "data": {"fillet": {"adjustment": "none", "requested": {"radius": 1},
                                         "effective": {"radius": 1}}}},
        {"id": "f4", "data": None}]
    rows = cdt._adjusted_dress(feats)
    assert [r["id"] for r in rows] == ["f2"]
    assert rows[0]["adjusted"] is True and rows[0]["change_fraction"] == 0.9


def test_render_failure_reason_distinguishes_empty_from_no_mesh(monkeypatch):
    from services.mcp import cad_common
    from services.mcp import core_bridge as cb
    assert cad_common.render_failure_reason("d", {"reason": "boom"}) == "boom"

    def fake(method, params=None, *a, **k):
        if method == "doc_bbox":
            return {"size": [1, 1, 1]}
        return {"bodies": [{}, {}]}
    monkeypatch.setattr(cb, "call", fake)
    msg = cad_common.render_failure_reason("d", {})
    assert "no mesh" in msg and "2 bodies" in msg and "check_geometry_health" in msg
    monkeypatch.setattr(cb, "call", lambda m, p=None, *a, **k: {})
    assert cad_common.render_failure_reason("d", {}) == "no bodies to render"
