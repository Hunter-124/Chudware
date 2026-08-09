"""Versioned Stage-1 dataset records with strict, dependency-free validation."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Mapping, Sequence

import numpy as np

from .geometry import CameraPose, Scene
from .oracle import InspectabilityLabel, ORACLE_VERSION, OracleConfig, evaluate


SCHEMA_VERSION = "chudware.viewpoint.stage1/v1"
RENDERER_VERSION = "none; analytic-numpy/1.0.0"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_LABEL_FIELDS = frozenset(
    {
        "in_frame_coverage",
        "occlusion_fraction",
        "foreshortening_angle_deg",
        "foreshortening_quality",
        "apparent_scale_px",
        "scale_quality",
        "silhouette_informativeness",
        "depth_validity",
        "ambiguity",
        "ambiguous_feature_ids",
        "quality_components",
        "aggregate",
    }
)
_QUALITY_FIELDS = ("coverage", "visibility", "foreshortening", "scale", "silhouette", "depth", "uniqueness")
_POSE_FIELDS = frozenset(
    {"position", "target", "up", "vertical_fov_deg", "image_width_px", "image_height_px", "near", "far"}
)
_PROVENANCE_FIELDS = frozenset(
    {"geometry_hash", "feature_id", "sampler", "seed", "candidate_index", "renderer_version", "oracle_version"}
)
_RECORD_FIELDS = frozenset({"record_id", "pose", "labels", "provenance"})
_DATASET_FIELDS = frozenset(
    {"schema_version", "dataset_id", "created_utc", "geometry", "feature_id", "oracle_version", "renderer_version", "records"}
)


class DatasetValidationError(ValueError):
    pass


def _canonical_float64(array: np.ndarray) -> bytes:
    canonical = np.ascontiguousarray(array, dtype="<f8")
    return str(canonical.shape).encode("ascii") + canonical.tobytes(order="C")


def _canonical_int64(array: np.ndarray) -> bytes:
    canonical = np.ascontiguousarray(array, dtype="<i8")
    return str(canonical.shape).encode("ascii") + canonical.tobytes(order="C")


def geometry_hash(scene: Scene) -> str:
    """Hash mesh and named-feature geometry in a byte-order-stable representation."""

    digest = hashlib.sha256()
    digest.update(b"chudware-viewpoint-geometry-v1\0")
    digest.update(_canonical_float64(scene.mesh.vertices))
    digest.update(_canonical_int64(scene.mesh.triangles))
    for feature in sorted(scene.features, key=lambda item: item.feature_id):
        digest.update(feature.feature_id.encode("utf-8") + b"\0")
        digest.update(feature.kind.encode("ascii") + b"\0")
        digest.update(_canonical_int64(np.asarray(feature.triangle_indices, dtype=np.int64)))
        digest.update(_canonical_float64(feature.points))
        if feature.orientation is None:
            digest.update(b"orientation:none\0")
        else:
            digest.update(_canonical_float64(feature.orientation.reshape((1, 3))))
    return digest.hexdigest()


def build_dataset(
    scene: Scene,
    feature_id: str,
    poses: Sequence[CameraPose],
    labels: Sequence[InspectabilityLabel] | None = None,
    *,
    sampler: str,
    seed: int,
    sampler_parameters: Mapping[str, Any] | None = None,
    dataset_id: str | None = None,
    created_utc: str | None = None,
    config: OracleConfig = OracleConfig(),
) -> dict[str, Any]:
    """Build and validate a complete in-memory dataset.

    When ``labels`` is omitted the analytic oracle evaluates every pose. Supplying labels
    permits batched callers to avoid recomputation; cardinality is always checked.
    """

    scene.feature(feature_id)
    if not poses:
        raise ValueError("poses must not be empty")
    resolved_labels = list(labels) if labels is not None else [evaluate(scene, feature_id, pose, config) for pose in poses]
    if len(resolved_labels) != len(poses):
        raise ValueError("labels and poses must have equal length")
    digest = geometry_hash(scene)
    dataset_id = dataset_id or f"{digest[:16]}:{feature_id}:{sampler}:{int(seed)}"
    created_utc = created_utc or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    sampler_value = {"name": sampler, "parameters": dict(sampler_parameters or {})}
    records = []
    for index, (pose, label) in enumerate(zip(poses, resolved_labels)):
        records.append(
            {
                "record_id": f"{dataset_id}:{index}",
                "pose": pose.as_dict(),
                "labels": label.as_dict(),
                "provenance": {
                    "geometry_hash": digest,
                    "feature_id": feature_id,
                    "sampler": sampler_value,
                    "seed": int(seed),
                    "candidate_index": index,
                    "renderer_version": RENDERER_VERSION,
                    "oracle_version": ORACLE_VERSION,
                },
            }
        )
    dataset = {
        "schema_version": SCHEMA_VERSION,
        "dataset_id": dataset_id,
        "created_utc": created_utc,
        "geometry": {
            "sha256": digest,
            "vertex_count": len(scene.mesh.vertices),
            "triangle_count": len(scene.mesh.triangles),
        },
        "feature_id": feature_id,
        "oracle_version": ORACLE_VERSION,
        "renderer_version": RENDERER_VERSION,
        "records": records,
    }
    validate_dataset(dataset)
    return dataset


def _require_exact(value: object, fields: frozenset[str], path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise DatasetValidationError(f"{path} must be an object")
    missing = fields - value.keys()
    extra = value.keys() - fields
    if missing:
        raise DatasetValidationError(f"{path} missing required field(s): {', '.join(sorted(missing))}")
    if extra:
        raise DatasetValidationError(f"{path} has unknown field(s): {', '.join(sorted(extra))}")
    return value


def _finite_number(value: object, path: str, *, minimum: float | None = None, maximum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not np.isfinite(value):
        raise DatasetValidationError(f"{path} must be a finite number")
    number = float(value)
    if minimum is not None and number < minimum:
        raise DatasetValidationError(f"{path} must be >= {minimum}")
    if maximum is not None and number > maximum:
        raise DatasetValidationError(f"{path} must be <= {maximum}")
    return number


def _vector3(value: object, path: str) -> None:
    if not isinstance(value, list) or len(value) != 3:
        raise DatasetValidationError(f"{path} must be a three-number array")
    for index, component in enumerate(value):
        _finite_number(component, f"{path}[{index}]")


def _validate_pose(value: object, path: str) -> None:
    pose = _require_exact(value, _POSE_FIELDS, path)
    for name in ("position", "target", "up"):
        _vector3(pose[name], f"{path}.{name}")
    _finite_number(pose["vertical_fov_deg"], f"{path}.vertical_fov_deg", minimum=np.nextafter(0.0, 1.0), maximum=179.0)
    for name in ("image_width_px", "image_height_px"):
        if isinstance(pose[name], bool) or not isinstance(pose[name], int) or pose[name] <= 0:
            raise DatasetValidationError(f"{path}.{name} must be a positive integer")
    near = _finite_number(pose["near"], f"{path}.near", minimum=np.nextafter(0.0, 1.0))
    far = _finite_number(pose["far"], f"{path}.far", minimum=np.nextafter(0.0, 1.0))
    if near >= far:
        raise DatasetValidationError(f"{path} clipping range must satisfy near < far")


def _validate_labels(value: object, path: str) -> None:
    labels = _require_exact(value, _LABEL_FIELDS, path)
    bounded = (
        "in_frame_coverage",
        "occlusion_fraction",
        "foreshortening_quality",
        "scale_quality",
        "silhouette_informativeness",
        "depth_validity",
        "ambiguity",
        "aggregate",
    )
    for name in bounded:
        _finite_number(labels[name], f"{path}.{name}", minimum=0.0, maximum=1.0)
    _finite_number(labels["apparent_scale_px"], f"{path}.apparent_scale_px", minimum=0.0)
    angle = labels["foreshortening_angle_deg"]
    if angle is not None:
        _finite_number(angle, f"{path}.foreshortening_angle_deg", minimum=0.0, maximum=90.0)
    ambiguous_ids = labels["ambiguous_feature_ids"]
    if not isinstance(ambiguous_ids, list) or any(not isinstance(item, str) or not item for item in ambiguous_ids):
        raise DatasetValidationError(f"{path}.ambiguous_feature_ids must be an array of non-empty strings")
    quality = _require_exact(
        labels["quality_components"], frozenset(_QUALITY_FIELDS), f"{path}.quality_components"
    )
    for name in _QUALITY_FIELDS:
        _finite_number(quality[name], f"{path}.quality_components.{name}", minimum=0.0, maximum=1.0)


def validate_dataset(value: object) -> None:
    dataset = _require_exact(value, _DATASET_FIELDS, "dataset")
    if dataset["schema_version"] != SCHEMA_VERSION:
        raise DatasetValidationError(f"dataset.schema_version must equal {SCHEMA_VERSION!r}")
    for name in ("dataset_id", "created_utc", "feature_id", "oracle_version", "renderer_version"):
        if not isinstance(dataset[name], str) or not dataset[name]:
            raise DatasetValidationError(f"dataset.{name} must be a non-empty string")
    if dataset["oracle_version"] != ORACLE_VERSION or dataset["renderer_version"] != RENDERER_VERSION:
        raise DatasetValidationError("dataset oracle/renderer version does not match this implementation")
    geometry = _require_exact(dataset["geometry"], frozenset({"sha256", "vertex_count", "triangle_count"}), "dataset.geometry")
    if not isinstance(geometry["sha256"], str) or not _SHA256.fullmatch(geometry["sha256"]):
        raise DatasetValidationError("dataset.geometry.sha256 must be a lowercase SHA-256 digest")
    for name in ("vertex_count", "triangle_count"):
        if isinstance(geometry[name], bool) or not isinstance(geometry[name], int) or geometry[name] < 0:
            raise DatasetValidationError(f"dataset.geometry.{name} must be a non-negative integer")
    records = dataset["records"]
    if not isinstance(records, list) or not records:
        raise DatasetValidationError("dataset.records must be a non-empty array")
    seen_ids: set[str] = set()
    for index, raw_record in enumerate(records):
        path = f"dataset.records[{index}]"
        record = _require_exact(raw_record, _RECORD_FIELDS, path)
        if not isinstance(record["record_id"], str) or not record["record_id"] or record["record_id"] in seen_ids:
            raise DatasetValidationError(f"{path}.record_id must be a unique non-empty string")
        seen_ids.add(record["record_id"])
        _validate_pose(record["pose"], f"{path}.pose")
        _validate_labels(record["labels"], f"{path}.labels")
        provenance = _require_exact(record["provenance"], _PROVENANCE_FIELDS, f"{path}.provenance")
        if provenance["geometry_hash"] != geometry["sha256"] or provenance["feature_id"] != dataset["feature_id"]:
            raise DatasetValidationError(f"{path}.provenance geometry/feature does not match dataset")
        if provenance["oracle_version"] != dataset["oracle_version"] or provenance["renderer_version"] != dataset["renderer_version"]:
            raise DatasetValidationError(f"{path}.provenance version does not match dataset")
        if isinstance(provenance["seed"], bool) or not isinstance(provenance["seed"], int):
            raise DatasetValidationError(f"{path}.provenance.seed must be an integer")
        if provenance["candidate_index"] != index:
            raise DatasetValidationError(f"{path}.provenance.candidate_index must equal record position")
        sampler = _require_exact(provenance["sampler"], frozenset({"name", "parameters"}), f"{path}.provenance.sampler")
        if not isinstance(sampler["name"], str) or not sampler["name"] or not isinstance(sampler["parameters"], dict):
            raise DatasetValidationError(f"{path}.provenance.sampler needs a name and parameter object")


def write_dataset(path: str | Path, dataset: Mapping[str, Any]) -> None:
    validate_dataset(dataset)
    Path(path).write_text(json.dumps(dataset, indent=2, sort_keys=False) + "\n", encoding="utf-8")


def read_dataset(path: str | Path) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise DatasetValidationError(f"cannot read viewpoint dataset: {error}") from error
    validate_dataset(value)
    return value
