import numpy as np

from services.viewpoint.geometry import CameraPose, Feature, Mesh, Scene
from training.viewpoint.data import ViewpointBatch, encode_camera_pose, encode_candidate, split_by_part


def _batch():
    rng = np.random.default_rng(2)
    count = 12
    return ViewpointBatch(
        part_ids=tuple(f"part_{i // 2}" for i in range(count)),
        task=rng.normal(size=(count, 14)).astype(np.float32),
        geometry=rng.normal(size=(count, 3, 18)).astype(np.float32),
        geometry_mask=np.ones((count, 3), bool),
        feature_mask=np.tile(np.asarray((1, 0, 0), bool), (count, 1)),
        candidates=rng.normal(size=(count, 5, 48)).astype(np.float32),
        candidate_mask=np.ones((count, 5), bool),
        components=rng.uniform(size=(count, 5, 7)).astype(np.float32),
        difficulties=tuple(("easy", "hard")[i % 2] for i in range(count)),
    ).validate()


def test_split_has_no_part_leakage_and_keeps_rows_together():
    train, validation, test = split_by_part(_batch(), seed=8, train_fraction=0.5, validation_fraction=0.2)
    groups = [set(item.part_ids) for item in (train, validation, test)]
    assert groups[0].isdisjoint(groups[1])
    assert groups[0].isdisjoint(groups[2])
    assert groups[1].isdisjoint(groups[2])
    assert sum(len(item.part_ids) for item in (train, validation, test)) == 12
    assert all(item.part_ids.count(part) == 2 for item in (train, validation, test) for part in set(item.part_ids))
    assert all(len(item.difficulties) == len(item.part_ids) for item in (train, validation, test))


def test_camera_encoding_is_continuous_orthonormal_six_d_not_euler():
    pose = CameraPose(
        position=np.asarray((3.0, 1.0, 2.0)), target=np.asarray((0.2, -0.1, 0.0)),
        up=np.asarray((0.0, 0.0, 1.0)), vertical_fov_deg=50.0,
    )
    encoded = encode_camera_pose(pose, part_center=np.zeros(3), part_radius=2.0)
    assert encoded.shape == (12,)
    np.testing.assert_allclose(np.linalg.norm(encoded[:3]), 1.0, atol=1e-6)
    np.testing.assert_allclose(np.linalg.norm(encoded[3:6]), 1.0, atol=1e-6)
    np.testing.assert_allclose(np.dot(encoded[:3], encoded[3:6]), 0.0, atol=1e-6)


def test_candidate_encoding_exposes_view_dependent_occlusion_without_oracle_labels():
    vertices = np.asarray(
        (
            (-1.0, -1.0, 2.0),
            (1.0, -1.0, 2.0),
            (1.0, 1.0, 2.0),
            (-1.0, 1.0, 2.0),
        )
    )
    scene = Scene(
        Mesh(vertices, np.asarray(((0, 1, 2), (0, 2, 3)))),
        (
            Feature(
                "target",
                "hole",
                points=np.asarray(
                    (
                        (-0.4, -0.4, 0.0),
                        (0.4, -0.4, 0.0),
                        (0.4, 0.4, 0.0),
                        (-0.4, 0.4, 0.0),
                    )
                ),
                orientation=np.asarray((0.0, 0.0, 1.0)),
            ),
        ),
    )
    blocked = CameraPose(
        position=np.asarray((0.0, 0.0, 5.0)),
        target=np.zeros(3),
        up=np.asarray((0.0, 1.0, 0.0)),
    )
    open_side = CameraPose(
        position=np.asarray((5.0, 0.0, 0.0)),
        target=np.zeros(3),
        up=np.asarray((0.0, 0.0, 1.0)),
    )
    blocked_encoding = encode_candidate(scene, "target", blocked)
    open_encoding = encode_candidate(scene, "target", open_side)
    assert blocked_encoding.shape == (48,)
    assert open_encoding.shape == (48,)
    assert blocked_encoding[26:35].mean() < open_encoding[26:35].mean()
    assert blocked_encoding[35:44].mean() < open_encoding[35:44].mean()
