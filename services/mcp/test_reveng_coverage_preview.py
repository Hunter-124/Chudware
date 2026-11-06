import json

import numpy as np
import pytest

from services.mcp import reveng_session_tools as tools
from services.vision.recon_transform import ReconTransform


class Capture:
    def __init__(self):
        self.tools = {}

    def tool(self, *args, **kwargs):
        def register(fn):
            self.tools[fn.__name__] = fn
            return fn
        return register


@pytest.fixture
def registered():
    capture = Capture()
    tools.register_reveng_session_tools(capture)
    return capture.tools


def test_pose_coverage_rejects_distant_views_and_reports_missing_slot_errors(monkeypatch, registered):
    monkeypatch.setattr(tools, "_load_session", lambda *a, **k: {})
    monkeypatch.setattr(tools, "_session_image_paths", lambda _: ["near.jpg", "far.jpg"])
    monkeypatch.setattr(tools, "_score_session_blur", lambda *a: ({}, False, None))
    monkeypatch.setattr(tools, "_pose_bin_session", lambda *a: (
        {"near.jpg": 0, "far.jpg": 10}, {"near.jpg": 25.0, "far.jpg": 70.4},
        [25.0] + [40.0] * 9 + [70.4], None))
    coverage = json.loads(registered["get_reveng_coverage"]("re_test"))["coverage"]
    assert coverage["method"] == "pose_binned"
    assert coverage["captured"] == [0]
    assert 10 in coverage["missing"]
    assert coverage["slot_counts"][10] == 0
    assert coverage["nearest_slot_error_deg"][10] == 70.4
    assert coverage["max_slot_error_deg"] == 25.0


def test_single_registered_pose_does_not_invent_coverage(tmp_path, monkeypatch, registered):
    from types import SimpleNamespace
    path = str(tmp_path / "one.jpg")
    monkeypatch.setattr(tools, "_load_session", lambda *a, **k: {})
    monkeypatch.setattr(tools, "_session_image_paths", lambda _: [path])
    monkeypatch.setattr(tools, "_score_session_blur", lambda *a: ({}, False, None))
    monkeypatch.setattr(tools, "_resolve_sparse_dir", lambda *a: tmp_path)
    camera = SimpleNamespace(center=np.array([1., 0., 0.]), R=np.eye(3))
    monkeypatch.setattr("services.vision.scale_solver.read_colmap_sparse",
                        lambda _: {"one.jpg": camera})
    coverage = json.loads(registered["get_reveng_coverage"]("re_test"))["coverage"]
    assert coverage["method"] == "pose_binned"
    assert coverage["n_registered"] == 1
    assert coverage["captured"] == [] and coverage["missing"] == list(range(11))
    assert coverage["nearest_slot_error_deg"] == [None] * 11


def test_plan_nearest_errors_include_every_registered_direction():
    directions = tools._plan_directions(np)[[0, 1, 2]]
    slots, errors, nearest = tools._bin_views_to_plan(np, directions)
    assert len(nearest) == 11
    for slot, error in zip(slots, errors):
        assert nearest[slot] <= error + 1e-5
    assert max(nearest) > 25


@pytest.mark.parametrize("confirmed", [False, True])
def test_sparse_preview_is_bounded_and_uses_one_frame(tmp_path, monkeypatch, registered, confirmed):
    sparse = tmp_path / "sparse"
    sparse.mkdir()
    (sparse / "cameras.txt").write_text("1 PINHOLE 100 80 50 50 50 40\n")
    (sparse / "images.txt").write_text("1 1 0 0 0 0 0 0 1 frame.jpg\n\n")
    (sparse / "points3D.txt").write_text("".join(
        f"{i + 1} {i + 1} 2 3 255 128 0 0.1\n" for i in range(5001)))
    transform = ReconTransform.identity()
    transform.mm_per_unit = 2
    transform.translation_mm = [10, 0, 0]
    transform.source = "known_dim"
    transform.confirmed = confirmed
    session = {"artifacts": {"sparse_dir": str(sparse)}, "recon_transform": transform.to_json()}
    monkeypatch.setattr(tools, "_load_session", lambda *a, **k: session)
    monkeypatch.setattr(tools, "_session_path", lambda _: tmp_path)
    result = json.loads(registered["reveng_sparse_preview"]("re_test"))
    assert result["ok"]
    assert result["total_points"] == 5001
    assert len(result["points"]) == 5000
    assert "points_native" not in result
    assert result["sparse_points_path"] == str(sparse / "points3D.txt")
    assert result["units"] == ("mm" if confirmed else "reconstruction")
    assert result["points"][0][:3] == ([12, 4, 6] if confirmed else [1, 2, 3])
    assert result["frusta"][0][:3] == ([10, 0, 0] if confirmed else [0, 0, 0])
    assert bool(result["recon_transform"]) == confirmed


def test_result_fallback_is_recorded_body_evidence_not_approximation(monkeypatch, registered):
    session = {"honesty": {"approximate": True}, "result": {
        "state": "failed", "reason": "no bodies", "mesh_fallback": False}}
    monkeypatch.setattr(tools, "_load_session", lambda *a, **k: session)
    result = json.loads(registered["describe_reveng_result"]("re_test"))["result"]
    assert result["state"] == "failed"
    assert not result["scored"] and not result["mesh_fallback"]
    session["result"] = {"state": "built", "mesh_fallback": True}
    session["honesty"]["approximate"] = False
    result = json.loads(registered["describe_reveng_result"]("re_test"))["result"]
    assert result["mesh_fallback"]
