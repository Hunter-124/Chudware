"""Learned-viewpoint foundations: analytic labels, seeded poses, and versioned data."""

from .dataset import (
    DatasetValidationError,
    SCHEMA_VERSION,
    build_dataset,
    geometry_hash,
    read_dataset,
    validate_dataset,
    write_dataset,
)
from .geometry import CameraPose, Feature, Mesh, Scene, camera_from_dict, project_points
from .oracle import (
    GEOMETRIC_LIMITATIONS,
    ORACLE_VERSION,
    InspectabilityLabel,
    OracleConfig,
    aggregate_quality,
    evaluate,
)
from .sampling import (
    generate_candidate_set,
    hemisphere_above_plane,
    near_tangent_grazing,
    orbit_bands,
    uniform_sphere,
)

__all__ = [
    "CameraPose",
    "DatasetValidationError",
    "Feature",
    "GEOMETRIC_LIMITATIONS",
    "InspectabilityLabel",
    "Mesh",
    "ORACLE_VERSION",
    "OracleConfig",
    "SCHEMA_VERSION",
    "Scene",
    "aggregate_quality",
    "build_dataset",
    "camera_from_dict",
    "evaluate",
    "generate_candidate_set",
    "geometry_hash",
    "hemisphere_above_plane",
    "near_tangent_grazing",
    "orbit_bands",
    "project_points",
    "read_dataset",
    "uniform_sphere",
    "validate_dataset",
    "write_dataset",
]
