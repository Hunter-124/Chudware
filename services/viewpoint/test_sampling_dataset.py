from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import numpy as np
import pytest

from services.viewpoint.dataset import (
    DatasetValidationError,
    SCHEMA_VERSION,
    build_dataset,
    geometry_hash,
    read_dataset,
    validate_dataset,
)
from services.viewpoint.geometry import Feature, Mesh, Scene
from services.viewpoint.sampling import (
    generate_candidate_set,
    hemisphere_above_plane,
    near_tangent_grazing,
    orbit_bands,
    uniform_sphere,
)


def _scene() -> Scene:
    vertices = np.asarray(((-1.0, -1.0, 0.0), (1.0, -1.0, 0.0), (1.0, 1.0, 0.0), (-1.0, 1.0, 0.0)))
    return Scene(
        Mesh(vertices, np.asarray(((0, 1, 2), (0, 2, 3)))),
        (Feature("datum-face", "face", triangle_indices=(0, 1), orientation=np.asarray((0.0, 0.0, 1.0))),),
    )


def _positions(poses: list) -> np.ndarray:
    return np.asarray([pose.position for pose in poses])


@pytest.mark.parametrize(
    "sampler,extra",
    (
        (uniform_sphere, {}),
        (hemisphere_above_plane, {"fixture_normal": np.asarray((0.0, 0.0, 1.0))}),
        (orbit_bands, {"fixture_normal": np.asarray((0.0, 0.0, 1.0))}),
        (near_tangent_grazing, {"orientation": np.asarray((0.0, 0.0, 1.0))}),
    ),
)
def test_all_samplers_are_seed_deterministic(sampler, extra: dict[str, object]) -> None:
    arguments = dict(target=np.zeros(3), n=12, radius=5.0, seed=123, **extra)
    first = sampler(**arguments)
    second = sampler(**arguments)
    different = sampler(**{**arguments, "seed": 124})
    assert np.array_equal(_positions(first), _positions(second))
    assert not np.array_equal(_positions(first), _positions(different))


def test_hemisphere_and_grazing_constraints_are_geometrically_exact() -> None:
    hemisphere = hemisphere_above_plane(np.zeros(3), 100, radius=2.0, seed=7)
    assert np.all(_positions(hemisphere)[:, 2] >= -1e-12)

    grazing = near_tangent_grazing(
        np.zeros(3), 20, radius=2.0, seed=9, orientation=np.asarray((0.0, 0.0, 1.0)), grazing_angle_deg=5.0
    )
    directions = _positions(grazing) / 2.0
    assert np.allclose(np.abs(directions[:, 2]), np.sin(np.deg2rad(5.0)))


def test_candidate_set_is_deterministic_and_angularly_diverse() -> None:
    candidates = generate_candidate_set(
        np.zeros(3),
        8,
        radius=5.0,
        seed=99,
        strategy="mixed",
        feature_orientation=np.asarray((0.0, 0.0, 1.0)),
    )
    repeated = generate_candidate_set(
        np.zeros(3),
        8,
        radius=5.0,
        seed=99,
        strategy="mixed",
        feature_orientation=np.asarray((0.0, 0.0, 1.0)),
    )
    directions = _positions(candidates) / 5.0
    cosine = np.clip(directions @ directions.T, -1.0, 1.0)
    angles = np.arccos(cosine)
    angles[np.eye(len(angles), dtype=bool)] = np.inf
    assert len(candidates) == 8
    assert np.array_equal(_positions(candidates), _positions(repeated))
    assert float(angles.min()) > np.deg2rad(35.0)


def test_dataset_has_stable_geometry_hash_and_complete_record_provenance() -> None:
    scene = _scene()
    poses = generate_candidate_set(np.zeros(3), 3, radius=5.0, seed=11, strategy="orbit_bands")
    dataset = build_dataset(
        scene,
        "datum-face",
        poses,
        sampler="orbit_bands",
        seed=11,
        sampler_parameters={"radius": 5.0, "count": 3},
        created_utc="2026-08-08T00:00:00Z",
    )
    assert dataset["schema_version"] == SCHEMA_VERSION
    assert dataset["geometry"]["sha256"] == geometry_hash(scene)
    assert len(dataset["records"]) == 3
    for index, record in enumerate(dataset["records"]):
        provenance = record["provenance"]
        assert provenance["geometry_hash"] == dataset["geometry"]["sha256"]
        assert provenance["feature_id"] == "datum-face"
        assert provenance["sampler"] == {"name": "orbit_bands", "parameters": {"radius": 5.0, "count": 3}}
        assert provenance["seed"] == 11
        assert provenance["candidate_index"] == index
        assert provenance["renderer_version"].startswith("none;")
        assert provenance["oracle_version"] == dataset["oracle_version"]


def test_schema_and_provenance_reject_any_missing_required_field() -> None:
    scene = _scene()
    pose = generate_candidate_set(np.zeros(3), 1, radius=5.0, seed=3)[0]
    dataset = build_dataset(
        scene,
        "datum-face",
        [pose],
        sampler="mixed",
        seed=3,
        created_utc="2026-08-08T00:00:00Z",
    )
    missing_schema = deepcopy(dataset)
    del missing_schema["schema_version"]
    with pytest.raises(DatasetValidationError, match="schema_version"):
        validate_dataset(missing_schema)

    missing_provenance = deepcopy(dataset)
    del missing_provenance["records"][0]["provenance"]["geometry_hash"]
    with pytest.raises(DatasetValidationError, match="geometry_hash"):
        validate_dataset(missing_provenance)


def test_checked_in_fixture_is_a_valid_reproducible_dataset() -> None:
    fixture = Path(__file__).parents[2] / "fixtures" / "viewpoint" / "stage1_oracle_v1.json"
    dataset = read_dataset(fixture)
    assert dataset["schema_version"] == SCHEMA_VERSION
    assert dataset["created_utc"] == "2026-08-08T00:00:00Z"
