from __future__ import annotations

import math

import pytest

from services.mcp import core_bridge as cb
from services.mcp.cad_viewpoint import focus_frame


def test_focus_frame_widens_a_degenerate_projected_axis_and_clamps_margin():
    target_bbox = {"min": (0, -2, -1), "max": (0, 2, 1),
                   "size": (0, 4, 2), "center": (0, 0, 0)}
    frame = focus_frame(target_bbox, (1, 0, 0), 160, 128, margin=99)
    assert frame["center"] == (0.0, 0.0, 0.0)
    assert frame["W"] == 160 and frame["H"] == 128
    assert math.isfinite(frame["scale"]) and frame["scale"] > 0


@pytest.mark.skipif(not cb.available(), reason="engine unavailable")
def test_live_focused_frame_matches_engine_echo(tmp_path):
    doc = None
    try:
        made = cb.call("doc_new", {"kind": "part", "name": "ViewpointFocusParity"})
        doc = made.get("doc_id") or made.get("id")
        dim = lambda value: {"value": value, "expr": None}
        ops = [
            {"op": "add_feature", "feature": {"type": "box", "name": "Plate",
                "params": {"length": dim(120), "width": dim(80), "height": dim(12),
                           "centered": True}}},
            {"op": "add_feature", "feature": {"type": "hole", "name": "Bore",
                "params": {"style": "simple", "end": "through_all", "diameter": 5,
                           "face": {"of": "face", "side": "top"},
                           "positions": [[0, 0]]}}},
        ]
        applied = cb.call("doc_apply_ops", {"doc_id": doc, "base_revision": -1, "ops": ops})
        statuses = applied["rebuild"]["feature_status"]
        assert all(item.get("ok") for item in statuses), statuses

        target_bbox = {"min": (-3.0, -3.0, -6.0), "max": (3.0, 3.0, 6.0),
                       "size": (6.0, 6.0, 12.0), "center": (0.0, 0.0, 0.0)}
        direction = (0.37, -0.51, 0.78)
        W, H, margin = 317, 241, 0.23
        rendered = cb.call("doc_render_view", {
            "doc_id": doc,
            "out_path": str(tmp_path / "focused.png"),
            "view": {"dir": list(direction), "width": W, "height": H,
                     "focus": {"min": list(target_bbox["min"]),
                               "max": list(target_bbox["max"]),
                               "margin": margin}},
        })
        assert rendered.get("ok") is True, rendered
        engine = rendered["frame"]
        mirrored = focus_frame(target_bbox, direction, W, H, margin)
        assert mirrored["scale"] == pytest.approx(engine["scale"], abs=1e-6)
        assert mirrored["cu"] == pytest.approx(engine["cu"], abs=1e-6)
        assert mirrored["cv"] == pytest.approx(engine["cv"], abs=1e-6)
        for py_key, cpp_key in (("center", "center"), ("r", "right"),
                                ("u", "up"), ("d", "dir")):
            assert mirrored[py_key] == pytest.approx(engine[cpp_key], abs=1e-6)
    finally:
        if doc:
            try:
                cb.call("doc_close", {"doc_id": doc})
            except Exception:
                pass
