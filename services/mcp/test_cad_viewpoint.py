from __future__ import annotations

import json
import math

import numpy as np
import pytest

from services.mcp import core_bridge as cb
from services.mcp.cad_viewpoint import (
    candidate_dirs,
    focus_frame,
    ideal_view_dirs,
    score_view,
)


def _bbox(points) -> dict:
    p = np.asarray(points, dtype=np.float64)
    lo, hi = p.min(axis=0), p.max(axis=0)
    return {
        "min": tuple(lo),
        "max": tuple(hi),
        "size": tuple(hi - lo),
        "center": tuple((lo + hi) * 0.5),
    }


def _mesh_from_quads(quads) -> dict:
    positions = []
    triangles = []
    for quad in quads:
        base = len(positions)
        positions.extend(quad)
        triangles.extend(((base, base + 1, base + 2), (base, base + 2, base + 3)))
    idx = np.asarray(triangles, dtype=np.int64)
    return {
        "pos": np.asarray(positions, dtype=np.float64),
        "idx": idx,
        "tri_face": np.full(len(idx), -1, dtype=np.int32),
        "faces": [],
        "edges": [],
        "last_body": 1,
    }


def _positive_x_face(x: float, radius: float = 1.0):
    # Winding gives +X: cross((0,2r,0), (0,2r,2r)) = (+4r²,0,0).
    r = radius
    return ((x, -r, -r), (x, r, -r), (x, r, r), (x, -r, r))


def _cube_mesh() -> dict:
    return _mesh_from_quads([
        _positive_x_face(1.0),
        ((-1, -1, -1), (-1, -1, 1), (-1, 1, 1), (-1, 1, -1)),  # -X
        ((-1, 1, -1), (-1, 1, 1), (1, 1, 1), (1, 1, -1)),      # +Y
        ((-1, -1, -1), (1, -1, -1), (1, -1, 1), (-1, -1, 1)), # -Y
        ((-1, -1, 1), (1, -1, 1), (1, 1, 1), (-1, 1, 1)),      # +Z
        ((-1, -1, -1), (-1, 1, -1), (1, 1, -1), (1, -1, -1)), # -Z
    ])


def _angle_deg(a, b) -> float:
    dot = sum(float(x) * float(y) for x, y in zip(a, b))
    dot = max(-1.0, min(1.0, dot))
    return math.degrees(math.acos(dot))


def test_occlusion_demotes_a_blocked_direction_below_an_open_one():
    # The target is at x=0 and an equally-sized wall is nearer at x=2.  Looking along +X
    # hides every target pixel; tilting toward +Y produces parallax and exposes part of it.
    mesh = _mesh_from_quads([_positive_x_face(0.0), _positive_x_face(2.0)])
    target = np.array([True, True, False, False])
    target_bbox = _bbox(mesh["pos"][:4])
    blocked = score_view(mesh, target, focus_frame(target_bbox, (1, 0, 0), 160, 128))
    open_view = score_view(
        mesh, target,
        focus_frame(target_bbox, (math.cos(math.radians(35)), math.sin(math.radians(35)), 0),
                    160, 128),
    )
    assert blocked["target_px"] > 0
    assert blocked["unoccluded_px"] == 0
    assert blocked["occluder_px"] == blocked["target_px"]
    assert open_view["visible_frac"] > blocked["visible_frac"]

    ranked = ideal_view_dirs(mesh, target_bbox, target_tris=target, normals=[(1, 0, 0)],
                             hint=(1, 0, 0), n=2, min_sep_deg=0)
    assert ranked[0]["visible_frac"] > blocked["visible_frac"]
    assert ranked[0]["label"] != "hint"


def test_dead_on_face_scores_below_a_mild_tilt():
    mesh = _mesh_from_quads([_positive_x_face(0.0)])
    target_bbox = _bbox(mesh["pos"])
    ranked = ideal_view_dirs(mesh, target_bbox, target_tris=np.array([0, 1]),
                             normals=[(1, 0, 0)], hint=(1, 0, 0),
                             n=len(candidate_dirs(target_bbox, (1, 0, 0), [(1, 0, 0)])),
                             min_sep_deg=0)
    dead_on = next(item for item in ranked if item["label"] == "hint")
    assert dead_on["foreshorten"] == pytest.approx(1.0)
    assert ranked[0]["score"] > dead_on["score"]
    assert 0.75 < ranked[0]["foreshorten"] < 0.98


def test_minimum_angular_separation_is_honoured():
    """Separation is measured on the UNDIRECTED axis, so an antipodal camera is rejected too.

    Front and back of one target are mirror images; a multi-panel sheet that spends a panel on
    each has learned nothing. Only three mutually 57-degrees-apart AXES exist in 3-space, so a
    request for four comes back as three rather than padded with a duplicate."""
    mesh = _cube_mesh()
    target_bbox = _bbox(mesh["pos"])
    selected = ideal_view_dirs(mesh, target_bbox, target_tris=None,
                               normals=[(1, 0, 0)], n=4, min_sep_deg=57.0)
    assert 0 < len(selected) <= 3
    for i, lhs in enumerate(selected):
        for rhs in selected[i + 1:]:
            angle = _angle_deg(lhs["dir"], rhs["dir"])
            assert min(angle, 180.0 - angle) >= 57.0 - 1e-10

    # A separation the sphere can actually satisfy still fills the request.
    four = ideal_view_dirs(mesh, target_bbox, target_tris=None,
                           normals=[(1, 0, 0)], n=4, min_sep_deg=35.0)
    assert len(four) == 4
    for i, lhs in enumerate(four):
        for rhs in four[i + 1:]:
            angle = _angle_deg(lhs["dir"], rhs["dir"])
            assert min(angle, 180.0 - angle) >= 35.0 - 1e-10


def test_candidate_and_measured_results_are_deterministic():
    mesh = _cube_mesh()
    target_bbox = _bbox(mesh["pos"])
    kwargs = dict(target_tris=None, normals=[(1, 0, 0), (-1, 0, 0), (1, 0, 0)],
                  n=3, min_sep_deg=50.0)
    first = ideal_view_dirs(mesh, target_bbox, **kwargs)
    second = ideal_view_dirs(mesh, target_bbox, **kwargs)
    dirs1 = json.dumps([item["dir"] for item in first], separators=(",", ":")).encode()
    dirs2 = json.dumps([item["dir"] for item in second], separators=(",", ":")).encode()
    assert dirs1 == dirs2
    assert first == second


def test_mesh_none_degrades_to_lattice_without_raising():
    target_bbox = {"min": (-1, -2, -3), "max": (1, 2, 3),
                   "size": (2, 4, 6), "center": (0, 0, 0)}
    got = ideal_view_dirs(None, target_bbox, normals=[(0, 0, 1)], n=3)
    assert len(got) == 3
    assert got[0]["label"] == "normal"
    for item in got:
        assert item["score"] is None
        assert item["visible_frac"] is None
        assert item["target_px"] is None


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
