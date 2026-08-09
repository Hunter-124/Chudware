"""Seeded camera-pose sampling and angularly diverse candidate-set generation."""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np

from .geometry import CameraPose, Scene, unit, vector3


def _camera_up(forward: np.ndarray, preferred: np.ndarray | None = None) -> np.ndarray:
    candidates = [] if preferred is None else [unit(preferred, name="preferred up")]
    candidates.extend((np.asarray((0.0, 0.0, 1.0)), np.asarray((0.0, 1.0, 0.0)), np.asarray((1.0, 0.0, 0.0))))
    return max(candidates, key=lambda candidate: float(np.linalg.norm(np.cross(forward, candidate))))


def _poses(
    target: np.ndarray,
    directions: np.ndarray,
    radius: float,
    *,
    preferred_up: np.ndarray | None,
    vertical_fov_deg: float,
    image_size: tuple[int, int],
    near: float,
    far: float,
) -> list[CameraPose]:
    if not np.isfinite(radius) or radius <= 0.0:
        raise ValueError("radius must be positive")
    target = vector3(target, name="target")
    return [
        CameraPose(
            position=target + radius * direction,
            target=target,
            up=_camera_up(-direction, preferred_up),
            vertical_fov_deg=vertical_fov_deg,
            image_width_px=image_size[0],
            image_height_px=image_size[1],
            near=near,
            far=far,
        )
        for direction in directions
    ]


def _random_unit_vectors(rng: np.random.Generator, n: int) -> np.ndarray:
    directions = rng.normal(size=(n, 3))
    lengths = np.linalg.norm(directions, axis=1)
    while np.any(lengths <= 1e-12):
        bad = lengths <= 1e-12
        directions[bad] = rng.normal(size=(int(bad.sum()), 3))
        lengths = np.linalg.norm(directions, axis=1)
    return directions / lengths[:, None]


def uniform_sphere(
    target: np.ndarray,
    n: int,
    *,
    radius: float,
    seed: int,
    vertical_fov_deg: float = 45.0,
    image_size: tuple[int, int] = (640, 480),
    near: float = 0.01,
    far: float = 1000.0,
) -> list[CameraPose]:
    """Sample isotropic positions by normalizing independent Gaussian vectors."""

    if n <= 0:
        raise ValueError("n must be positive")
    directions = _random_unit_vectors(np.random.default_rng(seed), n)
    return _poses(target, directions, radius, preferred_up=None, vertical_fov_deg=vertical_fov_deg, image_size=image_size, near=near, far=far)


def hemisphere_above_plane(
    target: np.ndarray,
    n: int,
    *,
    radius: float,
    seed: int,
    fixture_normal: np.ndarray = np.asarray((0.0, 0.0, 1.0)),
    vertical_fov_deg: float = 45.0,
    image_size: tuple[int, int] = (640, 480),
    near: float = 0.01,
    far: float = 1000.0,
) -> list[CameraPose]:
    """Uniform hemisphere obtained by reflecting sphere samples across the fixture plane."""

    if n <= 0:
        raise ValueError("n must be positive")
    normal = unit(fixture_normal, name="fixture_normal")
    directions = _random_unit_vectors(np.random.default_rng(seed), n)
    dot = directions @ normal
    directions[dot < 0.0] -= 2.0 * dot[dot < 0.0, None] * normal
    return _poses(target, directions, radius, preferred_up=normal, vertical_fov_deg=vertical_fov_deg, image_size=image_size, near=near, far=far)


def _plane_basis(normal: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    normal = unit(normal, name="normal")
    helper = np.asarray((1.0, 0.0, 0.0)) if abs(normal[0]) < 0.8 else np.asarray((0.0, 1.0, 0.0))
    first = unit(np.cross(normal, helper), name="plane basis")
    return first, np.cross(normal, first)


def orbit_bands(
    target: np.ndarray,
    n: int,
    *,
    radius: float,
    seed: int,
    elevations_deg: Sequence[float] = (15.0, 35.0, 60.0),
    fixture_normal: np.ndarray = np.asarray((0.0, 0.0, 1.0)),
    vertical_fov_deg: float = 45.0,
    image_size: tuple[int, int] = (640, 480),
    near: float = 0.01,
    far: float = 1000.0,
) -> list[CameraPose]:
    """Sample azimuths on discrete elevation bands above a fixture plane."""

    if n <= 0 or not elevations_deg:
        raise ValueError("n and elevations_deg must be non-empty")
    elevations = np.deg2rad(np.asarray(tuple(elevations_deg), dtype=np.float64))
    if np.any(~np.isfinite(elevations)) or np.any(np.abs(elevations) >= np.pi / 2.0):
        raise ValueError("orbit elevations must lie strictly between -90 and 90 degrees")
    normal = unit(fixture_normal, name="fixture_normal")
    first, second = _plane_basis(normal)
    rng = np.random.default_rng(seed)
    # Stratified azimuth plus a seeded phase avoids random clusters while preserving seed control.
    phase = rng.uniform(0.0, 2.0 * np.pi)
    azimuth = phase + 2.0 * np.pi * (np.arange(n) + rng.uniform(0.0, 1.0, n)) / n
    elevation = elevations[np.arange(n) % len(elevations)]
    tangent = np.cos(azimuth)[:, None] * first + np.sin(azimuth)[:, None] * second
    directions = np.cos(elevation)[:, None] * tangent + np.sin(elevation)[:, None] * normal
    return _poses(target, directions, radius, preferred_up=normal, vertical_fov_deg=vertical_fov_deg, image_size=image_size, near=near, far=far)


def near_tangent_grazing(
    target: np.ndarray,
    n: int,
    *,
    radius: float,
    seed: int,
    orientation: np.ndarray,
    grazing_angle_deg: float = 5.0,
    vertical_fov_deg: float = 45.0,
    image_size: tuple[int, int] = (640, 480),
    near: float = 0.01,
    far: float = 1000.0,
) -> list[CameraPose]:
    """Sample view positions ``grazing_angle_deg`` above either side of the tangent plane."""

    if n <= 0 or not 0.0 <= grazing_angle_deg < 90.0:
        raise ValueError("n must be positive and grazing_angle_deg must be in [0, 90)")
    normal = unit(orientation, name="orientation")
    first, second = _plane_basis(normal)
    rng = np.random.default_rng(seed)
    azimuth = rng.uniform(0.0, 2.0 * np.pi, n)
    tangent = np.cos(azimuth)[:, None] * first + np.sin(azimuth)[:, None] * second
    angle = np.deg2rad(grazing_angle_deg)
    sides = rng.choice(np.asarray((-1.0, 1.0)), size=n)
    directions = np.cos(angle) * tangent + np.sin(angle) * sides[:, None] * normal
    return _poses(target, directions, radius, preferred_up=normal, vertical_fov_deg=vertical_fov_deg, image_size=image_size, near=near, far=far)


def _direction_from_pose(pose: CameraPose) -> np.ndarray:
    return unit(pose.position - pose.target, name="candidate direction")


def _farthest_subset(pool: list[CameraPose], n: int) -> list[CameraPose]:
    if n > len(pool):
        raise ValueError("candidate pool is smaller than requested set")
    directions = np.asarray([_direction_from_pose(pose) for pose in pool])
    chosen = [0]
    nearest_angle = np.full(len(pool), np.inf)
    while len(chosen) < n:
        last = directions[chosen[-1]]
        angles = np.arccos(np.clip(directions @ last, -1.0, 1.0))
        nearest_angle = np.minimum(nearest_angle, angles)
        nearest_angle[chosen] = -1.0
        chosen.append(int(np.argmax(nearest_angle)))
    return [pool[index] for index in chosen]


def generate_candidate_set(
    target: np.ndarray,
    n: int,
    *,
    radius: float,
    seed: int,
    strategy: str = "mixed",
    fixture_normal: np.ndarray = np.asarray((0.0, 0.0, 1.0)),
    feature_orientation: np.ndarray | None = None,
    vertical_fov_deg: float = 45.0,
    image_size: tuple[int, int] = (640, 480),
    near: float = 0.01,
    far: float = 1000.0,
) -> list[CameraPose]:
    """Generate ``n`` poses, greedily maximizing the minimum angular separation.

    Each source sampler creates a 4N pool. ``mixed`` combines sphere, fixture hemisphere,
    orbit-band, and (when an orientation is supplied) grazing pools before farthest-point
    selection. Every random choice is derived from ``seed``.
    """

    if n <= 0:
        raise ValueError("n must be positive")
    pool_size = max(4 * n, n)
    common = dict(radius=radius, vertical_fov_deg=vertical_fov_deg, image_size=image_size, near=near, far=far)
    if strategy == "uniform_sphere":
        pool = uniform_sphere(target, pool_size, seed=seed, **common)
    elif strategy == "hemisphere":
        pool = hemisphere_above_plane(target, pool_size, seed=seed, fixture_normal=fixture_normal, **common)
    elif strategy == "orbit_bands":
        pool = orbit_bands(target, pool_size, seed=seed, fixture_normal=fixture_normal, **common)
    elif strategy == "near_tangent":
        if feature_orientation is None:
            raise ValueError("near_tangent strategy requires feature_orientation")
        pool = near_tangent_grazing(target, pool_size, seed=seed, orientation=feature_orientation, **common)
    elif strategy == "mixed":
        per_sampler = max(n, 2)
        pool = uniform_sphere(target, per_sampler, seed=seed, **common)
        pool += hemisphere_above_plane(target, per_sampler, seed=seed + 1, fixture_normal=fixture_normal, **common)
        pool += orbit_bands(target, per_sampler, seed=seed + 2, fixture_normal=fixture_normal, **common)
        if feature_orientation is not None:
            pool += near_tangent_grazing(target, per_sampler, seed=seed + 3, orientation=feature_orientation, **common)
    else:
        raise ValueError(f"unknown candidate strategy: {strategy!r}")
    return _farthest_subset(pool, n)


def generate_benchmark_candidate_set(
    scene: Scene,
    feature_id: str,
    n: int,
    *,
    seed: int,
    image_size: tuple[int, int] = (320, 240),
) -> list[CameraPose]:
    """Generate a nested-prefix pool for the hard mechanical-part benchmark.

    Unlike :func:`generate_candidate_set`, these are deliberately *render attempts*,
    not an angularly optimized pose set.  Position, framing target, distance, field of
    view, and clipping margins are all seeded random variables.  Generate the largest
    required pool once and use its prefixes when comparing best-of-N search costs.
    """

    if n <= 0:
        raise ValueError("n must be positive")
    if len(image_size) != 2 or min(int(value) for value in image_size) <= 0:
        raise ValueError("image_size must contain two positive dimensions")
    scene.feature(feature_id)
    vertices = scene.mesh.vertices
    if len(vertices) == 0:
        raise ValueError("benchmark scene needs mesh geometry")
    lower = vertices.min(axis=0)
    upper = vertices.max(axis=0)
    part_center = (lower + upper) * 0.5
    part_radius = float(np.linalg.norm(vertices - part_center, axis=1).max())
    if not np.isfinite(part_radius) or part_radius <= 1e-12:
        raise ValueError("benchmark scene needs non-degenerate geometry")

    rng = np.random.default_rng(seed)
    poses: list[CameraPose] = []
    for _ in range(n):
        direction = _random_unit_vectors(rng, 1)[0]
        offset = _random_unit_vectors(rng, 1)[0]
        offset_radius = part_radius * float(rng.uniform(0.0, 0.48)) ** (1.0 / 3.0)
        target = part_center + offset * offset_radius
        distance = part_radius * float(np.exp(rng.uniform(np.log(1.65), np.log(6.5))))
        vertical_fov_deg = float(rng.uniform(26.0, 74.0))
        near_margin = part_radius * float(rng.uniform(0.22, 1.75))
        far_margin = part_radius * float(rng.uniform(0.22, 1.75))
        position = part_center + direction * distance
        forward = unit(target - position, name="benchmark view direction")
        center_depth = float(np.dot(part_center - position, forward))
        near = max(part_radius * 0.005, center_depth - near_margin)
        far = max(near + part_radius * 0.05, center_depth + far_margin)
        poses.append(
            CameraPose(
                position=position,
                target=target,
                up=_camera_up(forward),
                vertical_fov_deg=vertical_fov_deg,
                image_width_px=int(image_size[0]),
                image_height_px=int(image_size[1]),
                near=near,
                far=far,
            )
        )
    return poses
