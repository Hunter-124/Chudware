from __future__ import annotations

from math import cos, radians

import numpy as np
import pytest

from services.viewpoint.geometry import CameraPose, Feature, Mesh, Scene
from services.viewpoint.oracle import OracleConfig, aggregate_quality, evaluate


def _camera(
    position: tuple[float, float, float] = (0.0, 0.0, 5.0),
    *,
    target: tuple[float, float, float] = (0.0, 0.0, 0.0),
    fov: float = 90.0,
    size: int = 100,
    near: float = 0.01,
    far: float = 100.0,
) -> CameraPose:
    return CameraPose(
        position=position,
        target=target,
        up=(0.0, 1.0, 0.0),
        vertical_fov_deg=fov,
        image_width_px=size,
        image_height_px=size,
        near=near,
        far=far,
    )


def _square_scene(*, x0: float = -1.0, x1: float = 1.0, y0: float = -1.0, y1: float = 1.0) -> Scene:
    vertices = np.asarray(((x0, y0, 0.0), (x1, y0, 0.0), (x1, y1, 0.0), (x0, y1, 0.0)))
    mesh = Mesh(vertices, np.asarray(((0, 1, 2), (0, 2, 3))))
    feature = Feature("face", "face", triangle_indices=(0, 1), orientation=np.asarray((0.0, 0.0, 1.0)))
    return Scene(mesh, (feature,))


def _point_scene(points: np.ndarray, *, kind: str = "region", orientation: np.ndarray | None = None) -> Scene:
    mesh = Mesh(np.empty((0, 3)), np.empty((0, 3), dtype=np.int64))
    return Scene(mesh, (Feature("interest", kind, points=points, orientation=orientation),))


def test_foreshortening_is_hand_computed_head_on_and_at_80_degrees() -> None:
    scene = _square_scene()
    head_on = evaluate(scene, "face", _camera())
    angle = radians(80.0)
    grazing_position = (5.0 * np.sin(angle), 0.0, 5.0 * np.cos(angle))
    grazing = evaluate(scene, "face", _camera(grazing_position))

    assert head_on.foreshortening_angle_deg == pytest.approx(0.0, abs=1e-10)
    assert head_on.foreshortening_quality == pytest.approx(1.0)
    assert grazing.foreshortening_angle_deg == pytest.approx(80.0)
    assert grazing.foreshortening_quality == pytest.approx(cos(radians(80.0)))


def test_edge_axis_foreshortening_prefers_a_side_on_view() -> None:
    scene = _point_scene(
        np.asarray(((-0.5, 0.0, 0.0), (0.5, 0.0, 0.0))),
        kind="edge",
        orientation=np.asarray((1.0, 0.0, 0.0)),
    )
    label = evaluate(scene, "interest", _camera())
    assert label.foreshortening_angle_deg == pytest.approx(90.0)
    assert label.foreshortening_quality == pytest.approx(1.0)


def test_in_frame_coverage_uses_clipped_projected_area() -> None:
    # At depth 2 with a 90-degree square frustum, x in [-2, 2] is in frame. This
    # rectangle spans x=[0, 4], so exactly half its projected area is in frame.
    scene = _square_scene(x0=0.0, x1=4.0)
    label = evaluate(scene, "face", _camera(position=(0.0, 0.0, 2.0)))
    assert label.in_frame_coverage == pytest.approx(0.5, abs=1e-12)


def test_hole_is_fully_occluded_by_boss_using_triangle_rays() -> None:
    boss = np.asarray(((-1.0, -1.0, 2.0), (1.0, -1.0, 2.0), (1.0, 1.0, 2.0), (-1.0, 1.0, 2.0)))
    mesh = Mesh(boss, np.asarray(((0, 1, 2), (0, 2, 3))))
    rim = np.asarray(((-0.2, -0.2, 0.0), (0.2, -0.2, 0.0), (0.2, 0.2, 0.0), (-0.2, 0.2, 0.0)))
    scene = Scene(mesh, (Feature("hole", "hole", points=rim, orientation=np.asarray((0.0, 0.0, 1.0))),))
    label = evaluate(scene, "hole", _camera())
    assert label.occlusion_fraction == pytest.approx(1.0)
    assert label.quality_components()["visibility"] == pytest.approx(0.0)


def test_apparent_scale_marks_subpixel_feature_unresolvable() -> None:
    # focal length is 50 px at 90 degrees. A 0.01-unit segment at depth 10 spans
    # 50 * 0.01 / 10 = 0.05 pixels.
    points = np.asarray(((-0.005, 0.0, 0.0), (0.005, 0.0, 0.0)))
    scene = _point_scene(points, kind="edge", orientation=np.asarray((1.0, 0.0, 0.0)))
    label = evaluate(scene, "interest", _camera(position=(0.0, 0.0, 10.0)), OracleConfig(min_resolvable_px=8.0))
    assert label.apparent_scale_px == pytest.approx(0.05)
    assert label.scale_quality == pytest.approx(0.05 / 8.0)


def test_silhouette_fraction_distinguishes_isolated_from_embedded_face() -> None:
    isolated = evaluate(_square_scene(), "face", _camera())
    assert isolated.silhouette_informativeness == pytest.approx(1.0)

    inner = np.asarray(((-1.0, -1.0, 0.0), (1.0, -1.0, 0.0), (1.0, 1.0, 0.0), (-1.0, 1.0, 0.0)))
    background = np.asarray(((-5.0, -5.0, 0.0), (5.0, -5.0, 0.0), (5.0, 5.0, 0.0), (-5.0, 5.0, 0.0)))
    vertices = np.concatenate((inner, background))
    triangles = np.asarray(((0, 1, 2), (0, 2, 3), (4, 5, 6), (4, 6, 7)))
    embedded_scene = Scene(
        Mesh(vertices, triangles),
        (Feature("embedded", "face", triangle_indices=(0, 1), orientation=np.asarray((0.0, 0.0, 1.0))),),
    )
    embedded = evaluate(embedded_scene, "embedded", _camera())
    assert embedded.silhouette_informativeness == pytest.approx(0.0)


def test_depth_validity_is_landmark_fraction_inside_clip_range() -> None:
    points = np.asarray(((-0.5, 0.0, 0.0), (0.0, 0.0, 2.0), (0.5, 0.0, 4.5)))
    scene = _point_scene(points)
    # Camera depths are 5, 3, and 0.5. Only depth 3 is in [1, 4].
    label = evaluate(scene, "interest", _camera(near=1.0, far=4.0))
    assert label.depth_validity == pytest.approx(1.0 / 3.0)


def test_duplicate_features_are_ambiguous_from_two_distinct_poses() -> None:
    left = np.asarray(((-2.0, -0.5, 0.0), (-1.0, -0.5, 0.0), (-1.0, 0.5, 0.0), (-2.0, 0.5, 0.0)))
    right = left + np.asarray((3.0, 0.0, 0.0))
    vertices = np.concatenate((left, right))
    triangles = np.asarray(((0, 1, 2), (0, 2, 3), (4, 5, 6), (4, 6, 7)))
    scene = Scene(
        Mesh(vertices, triangles),
        (
            Feature("left", "face", triangle_indices=(0, 1), orientation=np.asarray((0.0, 0.0, 1.0))),
            Feature("right", "face", triangle_indices=(2, 3), orientation=np.asarray((0.0, 0.0, 1.0))),
        ),
    )
    first = evaluate(scene, "left", _camera(position=(0.0, 0.0, 5.0)))
    second = evaluate(scene, "left", _camera(position=(0.0, 0.0, 8.0)))
    assert first.ambiguity == 1.0 and first.ambiguous_feature_ids == ("right",)
    assert second.ambiguity == 1.0 and second.ambiguous_feature_ids == ("right",)


def test_aggregate_is_the_documented_weighted_geometric_mean() -> None:
    components = {
        "coverage": 0.5,
        "visibility": 0.75,
        "foreshortening": 1.0,
        "scale": 0.5,
        "silhouette": 0.25,
        "depth": 1.0,
        "uniqueness": 1.0,
    }
    config = OracleConfig(weights=tuple((name, 1.0) for name in components))
    assert aggregate_quality(components, config) == pytest.approx(np.prod(tuple(components.values())) ** (1.0 / 7.0))
