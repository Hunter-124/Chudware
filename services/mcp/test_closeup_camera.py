"""Perspective close-up camera math (cad_viewpoint) and the verify-battery attachment check.

Pure-Python: the camera tests need numpy only; the battery test fakes the engine RPC.
Run: .venv-mcp/bin/python -m pytest services/mcp/test_closeup_camera.py -q
"""
from __future__ import annotations

import json
import math
from unittest import mock

import pytest

np = pytest.importorskip("numpy")

from services.mcp import cad_viewpoint as vp  # noqa: E402

_BOX = {"min": [-10.0, -5.0, 0.0], "max": [10.0, 5.0, 8.0]}


def _corners(bb):
    return [(x, y, z) for x in (bb["min"][0], bb["max"][0])
            for y in (bb["min"][1], bb["max"][1]) for z in (bb["min"][2], bb["max"][2])]


@pytest.mark.parametrize("view_dir", [(1, 1, 0.8), (0, -1, 0), (0, 0, 1), (-0.3, 0.2, -1)])
def test_fit_distance_frames_every_corner_inside_the_margin(view_dir):
    W, H, margin = 512, 384, 0.35
    cam = vp.perspective_camera(_BOX, view_dir, W, H, margin=margin)
    S = vp.perspective_project(_corners(_BOX), vp.perspective_frame(cam))
    fill = 1.0 / (1.0 + 2.0 * margin)
    # Every corner lands inside the padded frame, and the binding corner touches it.
    dx = np.abs(S[:, 0] - W / 2) / (W / 2)
    dy = np.abs(S[:, 1] - H / 2) / (H / 2)
    assert np.all(np.maximum(dx, dy) <= fill + 1e-9)
    assert np.max(np.maximum(dx, dy)) == pytest.approx(fill, rel=1e-6)
    # The camera looks at the box centre along -view_dir.
    c = np.mean(np.asarray(_corners(_BOX)), axis=0)
    d = np.asarray(view_dir, dtype=float) / np.linalg.norm(view_dir)
    assert np.allclose(np.asarray(cam["position"]) - c, d * cam["distance"])


def test_nearer_is_bigger_and_depth_is_reciprocal():
    cam = vp.perspective_camera(_BOX, (0, -1, 0), 400, 400, fov_deg=40)
    fr = vp.perspective_frame(cam)
    near_pt, far_pt = [(5.0, -5.0, 4.0)], [(5.0, 5.0, 4.0)]
    s_near, s_far = vp.perspective_project(near_pt, fr)[0], vp.perspective_project(far_pt, fr)[0]
    assert abs(s_near[0] - 200) > abs(s_far[0] - 200)          # same x offset, nearer = wider
    assert s_near[2] > s_far[2]                                  # larger depth value = nearer
    z = cam["distance"] - 5.0
    assert s_near[2] == pytest.approx(1.0 / z)


def test_explicit_distance_is_honoured_but_never_inside_the_target():
    far = vp.perspective_camera(_BOX, (1, 0, 0), 512, 384, distance=500.0)
    assert far["distance"] == pytest.approx(500.0)
    inside = vp.perspective_camera(_BOX, (1, 0, 0), 512, 384, distance=1.0)
    assert inside["distance"] > 10.0                             # clamped past the box front face


def test_cutaway_near_plane_sits_just_in_front_of_the_target():
    cam = vp.perspective_camera(_BOX, (0, 0, 1), 512, 384, clip_near=True)
    front_depth = cam["distance"] - 4.0                           # box top is 4 mm above centre
    assert cam["near"] < front_depth
    assert cam["near"] > front_depth - 0.05 * 2 * math.dist(_BOX["min"], _BOX["max"])
    behind = vp.perspective_project([(0.0, 0.0, 30.0)], vp.perspective_frame(cam))[0]
    assert math.isnan(behind[0]) and behind[2] == -1e30          # cut away: never wins a z-test


def test_echo_frame_matches_the_locally_built_frame():
    cam = vp.perspective_camera(_BOX, (1, 1, 0.8), 512, 384)
    local = vp.perspective_frame(cam)
    echo = {"projection": "perspective", "position": cam["position"],
            "right": local["r"], "up": local["u"], "forward": local["f"],
            "focal_length_px": local["focal"], "principal_x_px": 256.0,
            "principal_y_px": 192.0, "near": cam["near"], "far": cam["far"],
            "width": 512, "height": 384}
    got = vp.frame_from_echo(echo, {"target": cam["target"]})
    pts = _corners(_BOX)
    assert np.allclose(vp.perspective_project(pts, got), vp.perspective_project(pts, local))
    assert got["scale"] == pytest.approx(local["focal"] / cam["distance"])


def test_near_plane_clip_keeps_only_the_visible_part_of_a_triangle():
    poly = vp.clip_polygon_depth([(0, 0, -1.0), (1, 0, 3.0), (0, 1, 3.0)], 1.0, 100.0)
    assert len(poly) == 4 and all(p[2] >= 1.0 - 1e-12 for p in poly)


def test_battery_flags_each_detached_feature_with_its_closeup_call():
    from services.verify import battery

    engine = {"ok": False, "summary": "2 feature(s) not properly attached",
              "solid_count": 2, "flagged": ["f5", "f7"], "independent": [],
              "features": [
                  {"feature_id": "f5", "verdict": "overhang", "location": [-23.0, 19.0, 2.0],
                   "detail": 'f5 "Rib" OVERHANGS its support'},
                  {"feature_id": "f7", "verdict": "floating", "location": [0.0, -30.0, 6.0],
                   "detail": 'f7 "Tab" is NOT attached'}]}

    def call(method, params):
        if method == "eval_feature_attachment":
            return engine
        if method == "doc_bbox":
            return {"center": [0.0, 0.0, 0.0]}
        raise AssertionError(method)

    with mock.patch("services.mcp.core_bridge.call", side_effect=call), \
            mock.patch.object(battery, "_is_drawing_doc", return_value=False):
        out = battery.verify("d1", ["attachment"])
    assert out["scores"]["feature_attachment"] < 1.0
    details = [f["detail"] for f in out["findings"]]
    assert any(d.startswith('f5 "Rib"') and 'target="f5"' in d for d in details)
    # The floating tab sits toward -Y (front): the suggested camera looks from the front.
    tab = next(d for d in details if d.startswith("f7"))
    assert '"azimuth": 0' in tab and '"elevation": 5' in tab
    assert out["interpreted"] == [{"from": "attachment", "to": "feature_attachment"}]

    engine.update(ok=True, features=[], flagged=[])
    with mock.patch("services.mcp.core_bridge.call", side_effect=call), \
            mock.patch.object(battery, "_is_drawing_doc", return_value=False):
        clean = battery.verify("d1", ["feature_attachment"])
    assert clean["scores"]["feature_attachment"] == 1.0 and clean["findings"] == []
    assert json.dumps(clean)


def test_defect_camera_looks_at_the_evidence():
    """An overhang is viewed from outside and BELOW (the bare underside); a floating rib
    from near-level, perpendicular to its gap (plate top and rib bottom separate)."""
    from services.mcp import cad_render_tools as rt

    part = {"center": [0.0, 0.0, 0.0]}
    over = rt._defect_view_dir({"verdict": "overhang", "location": [-20.0, 20.0, 2.0]}, part)
    assert over[2] < -0.3                                        # from below
    assert over[0] < 0 and over[1] > 0                           # from the overhang's side
    flo = rt._defect_view_dir({"verdict": "floating", "location": [10.0, 0.0, 10.0],
                               "nearest_point": [10.0, 0.0, 6.0],
                               "nearest_point_on_part": [10.0, 0.0, 2.0]}, part)
    assert abs(flo[2]) < 0.2                                     # near-level
    assert abs(flo[0]) > 0.9                                     # clear bearing faces outward (+X side)
