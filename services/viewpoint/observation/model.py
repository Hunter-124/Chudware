"""Public, information-limited viewpoint observation types.

The level-specific dataclasses intentionally have no generic payload mapping: their fields are the
capability boundary.  In particular, no observation can carry a ``Feature`` or an oracle label.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, TypeAlias

import numpy as np

from services.viewpoint.geometry import CameraPose

InformationLevel: TypeAlias = Literal["full", "depth_only", "depth_plus_prior", "sparse_points"]
INFORMATION_LEVELS: tuple[InformationLevel, ...] = (
    "full",
    "depth_only",
    "depth_plus_prior",
    "sparse_points",
)


def _readonly_array(value: object, shape_tail: tuple[int, ...], *, name: str, dtype: object = np.float64) -> np.ndarray:
    result = np.asarray(value, dtype=dtype)
    if shape_tail and (result.ndim < len(shape_tail) or tuple(result.shape[-len(shape_tail) :]) != shape_tail):
        raise ValueError(f"{name} must end with shape {shape_tail}")
    result = np.ascontiguousarray(result).copy()
    result.setflags(write=False)
    return result


def _probability(value: float, *, name: str) -> float:
    result = float(value)
    if not np.isfinite(result) or not 0.0 <= result <= 1.0:
        raise ValueError(f"{name} must be finite and in [0, 1]")
    return result


@dataclass(frozen=True, slots=True)
class CorruptionConfig:
    """Independent, seeded sensor corruptions; zero disables each corruption."""

    depth_noise_std: float = 0.0
    grazing_dropout_probability: float = 0.0
    grazing_cosine_threshold: float = 0.25
    calibration_position_std: float = 0.0
    calibration_angle_std_deg: float = 0.0

    def __post_init__(self) -> None:
        for name in ("depth_noise_std", "calibration_position_std", "calibration_angle_std_deg"):
            value = float(getattr(self, name))
            if not np.isfinite(value) or value < 0.0:
                raise ValueError(f"{name} must be finite and non-negative")
            object.__setattr__(self, name, value)
        object.__setattr__(
            self,
            "grazing_dropout_probability",
            _probability(self.grazing_dropout_probability, name="grazing_dropout_probability"),
        )
        threshold = float(self.grazing_cosine_threshold)
        if not np.isfinite(threshold) or not 0.0 < threshold <= 1.0:
            raise ValueError("grazing_cosine_threshold must be finite and in (0, 1]")
        object.__setattr__(self, "grazing_cosine_threshold", threshold)


@dataclass(frozen=True, slots=True)
class ObservationConfig:
    """Resolution and deliberate coarsening of the simulated camera observation."""

    depth_height: int = 24
    depth_width: int = 32
    sparse_point_count: int = 96
    feature_region_quantum_px: float = 12.0
    feature_uncertainty_px: float = 16.0
    prior_voxel_size: float = 0.02
    ray_epsilon: float = 1e-8

    def __post_init__(self) -> None:
        for name in ("depth_height", "depth_width", "sparse_point_count"):
            value = int(getattr(self, name))
            if value <= 0:
                raise ValueError(f"{name} must be positive")
            object.__setattr__(self, name, value)
        for name in ("feature_region_quantum_px", "feature_uncertainty_px", "prior_voxel_size", "ray_epsilon"):
            value = float(getattr(self, name))
            if not np.isfinite(value) or value <= 0.0:
                raise ValueError(f"{name} must be finite and positive")
            object.__setattr__(self, name, value)


@dataclass(frozen=True, slots=True)
class WorkspaceBounds:
    minimum: np.ndarray
    maximum: np.ndarray

    def __post_init__(self) -> None:
        minimum = _readonly_array(self.minimum, (3,), name="minimum")
        maximum = _readonly_array(self.maximum, (3,), name="maximum")
        if minimum.shape != (3,) or maximum.shape != (3,) or not np.all(np.isfinite(minimum)) or not np.all(np.isfinite(maximum)):
            raise ValueError("workspace bounds must be finite three-vectors")
        if np.any(maximum < minimum):
            raise ValueError("workspace maximum must not be below minimum")
        object.__setattr__(self, "minimum", minimum)
        object.__setattr__(self, "maximum", maximum)

    def as_vector(self) -> np.ndarray:
        return np.concatenate((self.minimum, self.maximum))


@dataclass(frozen=True, slots=True)
class FeatureRegion2D:
    """Quantized image-space hypothesis; never the analytic target geometry."""

    bounds_normalized: np.ndarray
    depth_interval: np.ndarray
    uncertainty_px: float
    confidence: float

    def __post_init__(self) -> None:
        bounds = _readonly_array(self.bounds_normalized, (4,), name="bounds_normalized")
        depths = _readonly_array(self.depth_interval, (2,), name="depth_interval")
        if bounds.shape != (4,) or not np.all(np.isfinite(bounds)) or np.any(bounds < 0.0) or np.any(bounds > 1.0):
            raise ValueError("bounds_normalized must be a four-vector in [0, 1]")
        if bounds[2] < bounds[0] or bounds[3] < bounds[1]:
            raise ValueError("feature bounds must be ordered")
        if depths.shape != (2,) or not np.all(np.isfinite(depths)) or depths[0] < 0.0 or depths[1] < depths[0]:
            raise ValueError("depth_interval must be a finite ordered non-negative pair")
        uncertainty = float(self.uncertainty_px)
        if not np.isfinite(uncertainty) or uncertainty <= 0.0:
            raise ValueError("uncertainty_px must be finite and positive")
        object.__setattr__(self, "bounds_normalized", bounds)
        object.__setattr__(self, "depth_interval", depths)
        object.__setattr__(self, "uncertainty_px", uncertainty)
        object.__setattr__(self, "confidence", _probability(self.confidence, name="confidence"))

    def as_vector(self) -> np.ndarray:
        return np.concatenate(
            (self.bounds_normalized, self.depth_interval, np.asarray((self.uncertainty_px, self.confidence)))
        )


@dataclass(frozen=True, slots=True)
class VisitRecord:
    camera: CameraPose
    visibility_estimate: float
    valid_return_fraction: float
    feature_confidence: float

    def __post_init__(self) -> None:
        if not isinstance(self.camera, CameraPose):
            raise ValueError("camera must be CameraPose")
        for name in ("visibility_estimate", "valid_return_fraction", "feature_confidence"):
            object.__setattr__(self, name, _probability(getattr(self, name), name=name))

    def as_vector(self) -> np.ndarray:
        return np.concatenate(
            (
                self.camera.position,
                self.camera.forward,
                np.asarray((self.visibility_estimate, self.valid_return_fraction, self.feature_confidence)),
            )
        )


@dataclass(frozen=True, slots=True)
class _ObservationBase:
    level: InformationLevel
    camera: CameraPose
    workspace_bounds: WorkspaceBounds
    feature_region: FeatureRegion2D
    visits: tuple[VisitRecord, ...]
    visibility_estimate: float
    valid_return_fraction: float

    def __post_init__(self) -> None:
        if self.level not in INFORMATION_LEVELS:
            raise ValueError(f"unsupported information level: {self.level!r}")
        if not isinstance(self.camera, CameraPose):
            raise ValueError("camera must be CameraPose")
        if not isinstance(self.workspace_bounds, WorkspaceBounds) or not isinstance(self.feature_region, FeatureRegion2D):
            raise ValueError("invalid observation metadata")
        visits = tuple(self.visits)
        if any(not isinstance(visit, VisitRecord) for visit in visits):
            raise ValueError("visits must contain VisitRecord values")
        object.__setattr__(self, "visits", visits)
        object.__setattr__(self, "visibility_estimate", _probability(self.visibility_estimate, name="visibility_estimate"))
        object.__setattr__(self, "valid_return_fraction", _probability(self.valid_return_fraction, name="valid_return_fraction"))


@dataclass(frozen=True, slots=True)
class FullObservation(_ObservationBase):
    mesh_vertices: np.ndarray
    mesh_triangles: np.ndarray
    depth_map: np.ndarray
    normal_map: np.ndarray

    def __post_init__(self) -> None:
        super(FullObservation, self).__post_init__()
        if self.level != "full":
            raise ValueError("FullObservation requires level='full'")
        vertices = _readonly_array(self.mesh_vertices, (3,), name="mesh_vertices")
        triangles = _readonly_array(self.mesh_triangles, (3,), name="mesh_triangles", dtype=np.int64)
        depth = _readonly_array(self.depth_map, (), name="depth_map")
        normals = _readonly_array(self.normal_map, (3,), name="normal_map")
        if vertices.ndim != 2 or triangles.ndim != 2 or depth.ndim != 2 or normals.shape != depth.shape + (3,):
            raise ValueError("invalid full observation array shape")
        object.__setattr__(self, "mesh_vertices", vertices)
        object.__setattr__(self, "mesh_triangles", triangles)
        object.__setattr__(self, "depth_map", depth)
        object.__setattr__(self, "normal_map", normals)


@dataclass(frozen=True, slots=True)
class DepthOnlyObservation(_ObservationBase):
    depth_map: np.ndarray
    normal_map: np.ndarray

    def __post_init__(self) -> None:
        super(DepthOnlyObservation, self).__post_init__()
        if self.level != "depth_only":
            raise ValueError("DepthOnlyObservation requires level='depth_only'")
        depth = _readonly_array(self.depth_map, (), name="depth_map")
        normals = _readonly_array(self.normal_map, (3,), name="normal_map")
        if depth.ndim != 2 or normals.shape != depth.shape + (3,):
            raise ValueError("invalid depth observation array shape")
        object.__setattr__(self, "depth_map", depth)
        object.__setattr__(self, "normal_map", normals)


@dataclass(frozen=True, slots=True)
class DepthPlusPriorObservation(DepthOnlyObservation):
    accumulated_points: np.ndarray
    accumulated_normals: np.ndarray

    def __post_init__(self) -> None:
        _ObservationBase.__post_init__(self)
        if self.level != "depth_plus_prior":
            raise ValueError("DepthPlusPriorObservation requires level='depth_plus_prior'")
        depth = _readonly_array(self.depth_map, (), name="depth_map")
        normal_map = _readonly_array(self.normal_map, (3,), name="normal_map")
        points = _readonly_array(self.accumulated_points, (3,), name="accumulated_points")
        normals = _readonly_array(self.accumulated_normals, (3,), name="accumulated_normals")
        if depth.ndim != 2 or normal_map.shape != depth.shape + (3,):
            raise ValueError("invalid depth-plus-prior map shape")
        if points.ndim != 2 or normals.shape != points.shape:
            raise ValueError("accumulated points and normals must have matching (N, 3) shapes")
        object.__setattr__(self, "depth_map", depth)
        object.__setattr__(self, "normal_map", normal_map)
        object.__setattr__(self, "accumulated_points", points)
        object.__setattr__(self, "accumulated_normals", normals)


@dataclass(frozen=True, slots=True)
class SparsePointsObservation(_ObservationBase):
    points: np.ndarray
    normals: np.ndarray

    def __post_init__(self) -> None:
        super(SparsePointsObservation, self).__post_init__()
        if self.level != "sparse_points":
            raise ValueError("SparsePointsObservation requires level='sparse_points'")
        points = _readonly_array(self.points, (3,), name="points")
        normals = _readonly_array(self.normals, (3,), name="normals")
        if points.ndim != 2 or normals.shape != points.shape:
            raise ValueError("sparse points and normals must have matching (N, 3) shapes")
        object.__setattr__(self, "points", points)
        object.__setattr__(self, "normals", normals)


Observation: TypeAlias = FullObservation | DepthOnlyObservation | DepthPlusPriorObservation | SparsePointsObservation
