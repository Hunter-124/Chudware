"""Pure contract tests for thickness study routing, attribution and section cameras."""
from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest

from services.mcp import cad_eval_tools as tools


def _region(thickness=0.6, x=0.0, face_id=1):
    return {
        "min_thickness_mm": thickness, "at": [x, 0, 5], "opposite_at": [x, thickness, 0],
        "wall_direction": [0, 1, 0], "centroid": [x, 0, 2.5],
        "bbox": {"min": [x - 2, 0, 0], "max": [x + 2, thickness, 5]},
        "area_mm2": 20, "face_ids": [face_id], "kind": "wall", "opposing_cos": -1.0,
    }


def test_process_minimum_uses_bound_profile_then_native_fdm(monkeypatch):
    intake_tools = importlib.import_module("services.mcp.intake_tools")

    monkeypatch.setattr(intake_tools, "_get", lambda *_: {
        "process": "injection", "params": {"min_wall_mm": 1.7}})
    calls = []
    monkeypatch.setattr(intake_tools, "_dfm", lambda method, params: (
        calls.append((method, params)) or {"params": {"min_wall_mm": 1.0}}))
    assert tools._wall_minimum("d", None) == 1.7
    assert calls == []
    monkeypatch.setattr(intake_tools, "_get", lambda *_: None)
    assert tools._wall_minimum("d", None) == 1.0
    assert calls == [("dfm_get_profile", {"process": "fdm"})]
    assert tools._wall_minimum("d", 0.9) == 0.9


@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf"), True])
def test_process_minimum_rejects_nonpositive_or_nonfinite_threshold(value):
    with pytest.raises(ValueError, match="positive, finite"):
        tools._wall_minimum("d", value)


def test_study_merges_bodies_and_credits_each_regions_own_feature(monkeypatch):
    calls = []
    faces = [{"body": b, "topo": 1, "sid": f"face{b}", "surf": "plane"} for b in (1, 2)]

    def call(method, params):
        calls.append((method, params))
        if method == "doc_query":
            return {"matches": faces}
        assert method == "eval_thickness"
        body = params["body"]
        value = 0.8 if body == 2 else 0.9
        return {
            "min_thickness_mm": value, "max_thickness_mm": 10, "mean_thickness_mm": 2,
            "n_sampled": 10, "minimum_sample": {"face_id": 1, "at": [100 * body, 0, 0]},
            "measurement_basis": {"method": "inscribed_sphere", "spacing_mm": body * 0.2,
                                  "n_vertices": 11, "n_unmeasured": 1,
                                  "requested_spacing_mm": 0.25, "budget_limited": body == 2},
            "thin_area_mm2": body * 20, "n_thin_regions": 1,
            "thin_regions": [_region(value, 100 * body)],
        }

    monkeypatch.setattr(tools.cb, "call", call)
    monkeypatch.setattr(tools, "_target_feature_roster", lambda _: (
        [{"id": "f1", "name": "Plate"}, {"id": "f2", "name": "Shim"}],
        {"f1": {"faces": [[1, 1]]}, "f2": {"faces": [[2, 1]]}}))
    result = tools._wall_thickness_result("d", 1.0, 0.25)
    assert result["min_thickness_mm"] == 0.8
    assert result["thin_area_mm2"] == 60 and result["n_thin_regions"] == 2
    assert result["measurement_basis"]["spacing_mm"] == 0.4
    assert result["measurement_basis"]["budget_limited"] is True
    assert result["measurement_basis"]["n_vertices"] == 22
    assert result["minimum_sample"]["body"] == 2
    assert result["minimum_sample"]["face"] == "face2"
    assert result["minimum_sample"]["built_by"]["name"] == "Shim"
    regions = result["thin_regions"]
    assert [r["index"] for r in regions] == [1, 2]
    assert regions[0]["body"] == 2 and regions[0]["faces"] == ["face2"]
    assert regions[0]["built_by"] == [{"id": "f2", "name": "Shim"}]
    assert regions[1]["body"] == 1 and regions[1]["faces"] == ["face1"]
    for method, params in calls:
        if method == "eval_thickness":
            assert params["min_wall_mm"] == 1.0 and params["resolution"] == 0.25


@pytest.mark.parametrize("view", [[1, 1, 0.8], [0, 1, 0], [0, -1, 0]])
def test_region_section_cuts_across_the_wall_through_its_middle(view):
    region = _region()
    plane, direction = tools._wall_region_section(region, view)
    assert plane["point"] == region["centroid"]
    # The cut contains the measured wall direction, so it shows the wall's thickness...
    assert sum(a * b for a, b in zip(plane["normal"], region["wall_direction"])) == pytest.approx(0)
    # ...and crosses the region's longest extent (z here), not along it.
    assert abs(plane["normal"][2]) == pytest.approx(1)
    assert sum(a * b for a, b in zip(plane["normal"], direction)) > 0


def test_study_image_adds_at_most_three_numbered_sections_without_recolouring(monkeypatch, tmp_path):
    from PIL import Image

    from services.mcp import shot_planner

    regions = [dict(_region(), index=i, body=1, faces=["face1"],
                    built_by=[{"id": "f1", "name": "Rib"}]) for i in range(1, 5)]
    result = {"min_wall_mm": 1.0, "min_thickness_mm": 0.6, "thin_area_mm2": 80,
              "n_thin_regions": 4, "thin_regions": regions,
              "measurement_basis": {"requested_spacing_mm": 0.2},
              "colour_legend": "red <= 1 mm; blue >= 3 mm", "basis_note": "inscribed sphere"}
    views = [{"dir": [1, 1, 1], "camera": {"position": [10, 10, 10]}}
             for _ in range(2)]
    plans = []
    monkeypatch.setattr(shot_planner, "plan_shots", lambda doc, **kw: (
        plans.append((doc, kw)) or {"views": views}))
    paths = iter(str(tmp_path / f"tile{i}.png") for i in range(5))
    monkeypatch.setattr(tools, "_tmp", lambda _: next(paths))
    monkeypatch.setattr(tools, "render_output_path", lambda *_: str(tmp_path / "study.png"))
    captured = []

    def render(doc, direction, png, **kwargs):
        captured.append(kwargs)
        Image.new("RGB", (64, 48), (255, 0, 0)).save(png)
        return png, None, {"ok": True}

    monkeypatch.setattr(tools, "_render_view_png", render)
    monkeypatch.setattr(tools, "_captioned_render", lambda png, doc, labels, caption: [png, caption])
    image, text = tools._wall_thickness_image("d", result)
    assert Path(image).is_file() and plans == [("d", {"k": 2})]
    assert len(captured) == 5
    assert all(c["thickness"] == {"min_wall_mm": 1.0, "resolution": 0.2} for c in captured)
    assert all("camera" in c for c in captured[:2])
    assert all("focus" in c and c["section"]["point"] == regions[i]["centroid"]
               for i, c in enumerate(captured[2:]))
    caption = json.loads(text)
    assert len(caption["regions"]) == 4
    assert caption["panels"][2:] == ["Region 1: 0.6 mm", "Region 2: 0.6 mm", "Region 3: 0.6 mm"]
    assert caption["regions"][0]["built_by"][0]["name"] == "Rib"
