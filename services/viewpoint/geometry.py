"""Validated geometry and camera primitives for the Stage-1 viewpoint oracle."""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np


FEATURE_KINDS = frozenset({"face", "hole", "edge", "boss", "pocket", "region"})
DIFFICULTY_LABELS = ("easy", "medium", "hard")


def _points(value: object, *, name: str, allow_empty: bool = False) -> np.ndarray:
    result = np.asarray(value, dtype=np.float64)
    if allow_empty and result.size == 0:
        return np.empty((0, 3), dtype=np.float64)
    if result.ndim != 2 or result.shape[1] != 3 or len(result) == 0:
        raise ValueError(f"{name} must have shape (N, 3)")
    if not np.all(np.isfinite(result)):
        raise ValueError(f"{name} must contain only finite values")
    return np.ascontiguousarray(result)


def vector3(value: object, *, name: str) -> np.ndarray:
    result = np.asarray(value, dtype=np.float64)
    if result.shape != (3,) or not np.all(np.isfinite(result)):
        raise ValueError(f"{name} must be a finite 3-vector")
    return result


def unit(value: object, *, name: str) -> np.ndarray:
    result = vector3(value, name=name)
    length = float(np.linalg.norm(result))
    if length <= 1e-12:
        raise ValueError(f"{name} must be non-zero")
    return result / length


@dataclass(frozen=True)
class Mesh:
    """Triangle mesh. Vertices and triangle indices are copied into canonical arrays."""

    vertices: np.ndarray
    triangles: np.ndarray

    def __post_init__(self) -> None:
        vertices = _points(self.vertices, name="vertices", allow_empty=True)
        triangles = np.asarray(self.triangles, dtype=np.int64)
        if triangles.ndim != 2 or triangles.shape[1] != 3:
            raise ValueError("triangles must have shape (M, 3)")
        if np.any(triangles < 0) or np.any(triangles >= len(vertices)):
            raise ValueError("triangle index is outside the vertex array")
        object.__setattr__(self, "vertices", vertices.copy())
        object.__setattr__(self, "triangles", np.ascontiguousarray(triangles))


@dataclass(frozen=True)
class Feature:
    """Named inspectable geometry.

    ``triangle_indices`` identifies feature surface triangles in the scene mesh. ``points``
    represents non-surface features (a hole rim, edge, axis, or bounding region) and may
    also add semantic landmark points to a surface feature. ``orientation`` is the face
    normal or feature axis. Its sign is deliberately ignored by the oracle.
    """

    feature_id: str
    kind: str
    triangle_indices: tuple[int, ...] = ()
    points: np.ndarray = field(default_factory=lambda: np.empty((0, 3), dtype=np.float64))
    orientation: np.ndarray | None = None
    difficulty: str | None = None

    def __post_init__(self) -> None:
        if not self.feature_id:
            raise ValueError("feature_id must be non-empty")
        if self.kind not in FEATURE_KINDS:
            raise ValueError(f"unsupported feature kind: {self.kind!r}")
        triangle_indices = tuple(int(index) for index in self.triangle_indices)
        if any(index < 0 for index in triangle_indices) or len(set(triangle_indices)) != len(triangle_indices):
            raise ValueError("triangle_indices must be unique non-negative integers")
        points = _points(self.points, name="feature points", allow_empty=True)
        if not triangle_indices and len(points) == 0:
            raise ValueError("feature needs triangle_indices or points")
        orientation = None if self.orientation is None else unit(self.orientation, name="orientation")
        if self.difficulty is not None and self.difficulty not in DIFFICULTY_LABELS:
            raise ValueError(f"unsupported feature difficulty: {self.difficulty!r}")
        object.__setattr__(self, "triangle_indices", triangle_indices)
        object.__setattr__(self, "points", points.copy())
        object.__setattr__(self, "orientation", orientation)


@dataclass(frozen=True)
class Scene:
    mesh: Mesh
    features: tuple[Feature, ...]

    def __post_init__(self) -> None:
        features = tuple(self.features)
        ids = [feature.feature_id for feature in features]
        if not features or len(ids) != len(set(ids)):
            raise ValueError("scene needs features with unique feature_id values")
        triangle_count = len(self.mesh.triangles)
        for feature in features:
            if any(index >= triangle_count for index in feature.triangle_indices):
                raise ValueError(f"feature {feature.feature_id!r} references a missing triangle")
        object.__setattr__(self, "features", features)

    def feature(self, feature_id: str) -> Feature:
        for feature in self.features:
            if feature.feature_id == feature_id:
                return feature
        raise KeyError(f"unknown feature: {feature_id!r}")

    def feature_points(self, feature: Feature) -> np.ndarray:
        batches: list[np.ndarray] = []
        if feature.triangle_indices:
            triangles = self.mesh.triangles[np.asarray(feature.triangle_indices, dtype=np.int64)]
            batches.append(self.mesh.vertices[np.unique(triangles)])
            batches.append(self.mesh.vertices[triangles].mean(axis=1))
        if len(feature.points):
            batches.append(feature.points)
        return np.unique(np.concatenate(batches, axis=0), axis=0)

    def feature_center(self, feature: Feature | str) -> np.ndarray:
        resolved = self.feature(feature) if isinstance(feature, str) else feature
        return self.feature_points(resolved).mean(axis=0)


@dataclass(frozen=True)
class CameraPose:
    position: np.ndarray
    target: np.ndarray
    up: np.ndarray
    vertical_fov_deg: float = 45.0
    image_width_px: int = 640
    image_height_px: int = 480
    near: float = 0.01
    far: float = 1000.0

    def __post_init__(self) -> None:
        position = vector3(self.position, name="position")
        target = vector3(self.target, name="target")
        up = unit(self.up, name="up")
        forward = target - position
        if np.linalg.norm(forward) <= 1e-12:
            raise ValueError("camera position and target must differ")
        forward /= np.linalg.norm(forward)
        if np.linalg.norm(np.cross(forward, up)) <= 1e-8:
            raise ValueError("camera up must not be parallel to its view direction")
        if not 0.0 < float(self.vertical_fov_deg) < 179.0:
            raise ValueError("vertical_fov_deg must be in (0, 179)")
        if int(self.image_width_px) <= 0 or int(self.image_height_px) <= 0:
            raise ValueError("image dimensions must be positive")
        if not (0.0 < float(self.near) < float(self.far)):
            raise ValueError("camera clipping range must satisfy 0 < near < far")
        object.__setattr__(self, "position", position.copy())
        object.__setattr__(self, "target", target.copy())
        object.__setattr__(self, "up", up)
        object.__setattr__(self, "vertical_fov_deg", float(self.vertical_fov_deg))
        object.__setattr__(self, "image_width_px", int(self.image_width_px))
        object.__setattr__(self, "image_height_px", int(self.image_height_px))
        object.__setattr__(self, "near", float(self.near))
        object.__setattr__(self, "far", float(self.far))

    @property
    def forward(self) -> np.ndarray:
        return unit(self.target - self.position, name="view direction")

    @property
    def right(self) -> np.ndarray:
        return unit(np.cross(self.forward, self.up), name="camera right")

    @property
    def true_up(self) -> np.ndarray:
        return np.cross(self.right, self.forward)

    def as_dict(self) -> dict[str, object]:
        return {
            "position": self.position.tolist(),
            "target": self.target.tolist(),
            "up": self.up.tolist(),
            "vertical_fov_deg": self.vertical_fov_deg,
            "image_width_px": self.image_width_px,
            "image_height_px": self.image_height_px,
            "near": self.near,
            "far": self.far,
        }


@dataclass(frozen=True)
class Projection:
    pixels: np.ndarray
    ndc: np.ndarray
    depth: np.ndarray

    @property
    def in_front(self) -> np.ndarray:
        return self.depth > 0.0


def project_points(points: np.ndarray, camera: CameraPose) -> Projection:
    """Project world points without clipping; callers inspect returned NDC and depth."""

    points = _points(points, name="points")
    relative = points - camera.position
    depth = relative @ camera.forward
    safe_depth = np.where(np.abs(depth) > 1e-15, depth, np.copysign(1e-15, depth + 1e-30))
    tan_y = np.tan(np.deg2rad(camera.vertical_fov_deg) * 0.5)
    tan_x = tan_y * camera.image_width_px / camera.image_height_px
    ndc_x = (relative @ camera.right) / (safe_depth * tan_x)
    ndc_y = (relative @ camera.true_up) / (safe_depth * tan_y)
    ndc = np.column_stack((ndc_x, ndc_y))
    pixels = np.column_stack(
        (
            (ndc_x + 1.0) * camera.image_width_px * 0.5,
            (1.0 - ndc_y) * camera.image_height_px * 0.5,
        )
    )
    return Projection(pixels=pixels, ndc=ndc, depth=depth)


def camera_from_dict(value: dict[str, object]) -> CameraPose:
    return CameraPose(
        position=value["position"],
        target=value["target"],
        up=value["up"],
        vertical_fov_deg=float(value["vertical_fov_deg"]),
        image_width_px=int(value["image_width_px"]),
        image_height_px=int(value["image_height_px"]),
        near=float(value["near"]),
        far=float(value["far"]),
    )
