from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

_ALLOWED_LICENSES = {"owned", "licensed", "synthetic", "third_party"}
_ALLOWED_PROCESSES = {"cnc_mill", "fdm"}
_ALLOWED_MOUNTS = {"fixed", "moved", "unknown"}
_ALLOWED_SPLITS = {"train", "calib", "test_locked"}
_ALLOWED_SYNTHETIC_SOURCES = {None, "kubric", "twin", "procedural"}
_ALLOWED_APPEARANCE = {"real", "geometry_only"}

_REQUIRED = {
    "clip_id", "uri", "sha256", "bytes", "codec", "resolution", "fps_nominal",
    "fps_measured", "duration_s", "device_id", "machine_serial", "controller_kind",
    "run_id", "job_id", "job_revision", "artifact_sha256", "program_sha256",
    "material_id", "tool_ids", "fixture_id", "wcs_id", "process_family", "media_id",
    "camera_id", "camera_serial", "station_profile_id", "intrinsics_id", "extrinsics_id",
    "calibration_date", "calibration_valid_until", "mount_state", "lighting_id",
    "exposure_mode", "transport_secure", "start_wall_utc", "start_tick", "clock_source",
    "clock_error_ms", "tick_grid_hz", "alignment_method", "controller_log_uri",
    "controller_log_sha256", "sensor_log_uri", "sensor_log_sha256", "program_uri",
    "program_sidecar_sha256", "license", "license_ref", "consent_ref", "operator_id_hash",
    "redaction_applied", "retention_class", "export_restricted", "annotation_pass",
    "annotator_ids", "double_annotated", "adjudicated", "iaa_kappa",
    "label_schema_version", "synthetic", "synthetic_source", "twin_snapshot_id",
    "twin_frame_hash", "appearance_domain", "split_group", "split", "locked_at",
}


def split_group(machine_serial: str, job_id: str, calendar_day: str) -> str:
    source = f"{machine_serial}|{job_id}|{calendar_day}".encode("utf-8")
    return hashlib.sha1(source).hexdigest()


@dataclass(frozen=True)
class ManifestRecord:
    values: Mapping[str, Any]

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ManifestRecord":
        missing = sorted(_REQUIRED - set(raw))
        extra = sorted(set(raw) - _REQUIRED)
        if missing:
            raise ValueError(f"manifest record missing required fields: {', '.join(missing)}")
        if extra:
            raise ValueError(f"manifest record has unknown fields: {', '.join(extra)}")
        values = dict(raw)
        for field in (
            "clip_id", "uri", "sha256", "license_ref", "consent_ref", "retention_class",
            "operator_id_hash", "machine_serial", "job_id", "camera_id", "split_group",
        ):
            if not isinstance(values[field], str) or not values[field].strip():
                raise ValueError(f"manifest field {field} must be a non-empty string")
        if values["license"] not in _ALLOWED_LICENSES:
            raise ValueError("manifest license is invalid")
        if values["process_family"] not in _ALLOWED_PROCESSES:
            raise ValueError("manifest process_family is invalid")
        if values["mount_state"] not in _ALLOWED_MOUNTS:
            raise ValueError("manifest mount_state is invalid")
        if values["split"] not in _ALLOWED_SPLITS:
            raise ValueError("manifest split is invalid")
        if values["synthetic_source"] not in _ALLOWED_SYNTHETIC_SOURCES:
            raise ValueError("manifest synthetic_source is invalid")
        if values["appearance_domain"] not in _ALLOWED_APPEARANCE:
            raise ValueError("manifest appearance_domain is invalid")
        if values["tick_grid_hz"] != 4:
            raise ValueError("perception manifests must use the 4 Hz tick grid")
        for field in ("redaction_applied", "export_restricted", "synthetic", "transport_secure"):
            if not isinstance(values[field], bool):
                raise ValueError(f"manifest field {field} must be boolean")
        if values["bytes"] < 0 or values["duration_s"] <= 0:
            raise ValueError("asset bytes must be non-negative and duration must be positive")
        if values["synthetic"]:
            if values["license"] != "synthetic" or values["synthetic_source"] is None:
                raise ValueError("synthetic records require synthetic license and source")
            if values["split"] != "train":
                raise ValueError("synthetic records may appear only in train")
        else:
            if values["synthetic_source"] is not None:
                raise ValueError("real records cannot name a synthetic source")
            if values["appearance_domain"] != "real":
                raise ValueError("real records require appearance_domain=real")
        if values["split"] == "test_locked" and values["synthetic"]:
            raise ValueError("test_locked is real-only")
        return cls(values=values)

    def to_dict(self) -> dict[str, Any]:
        return dict(self.values)


def publication_eligible(
    record: ManifestRecord | Mapping[str, Any],
    *,
    approved_license_refs: set[str] | frozenset[str],
    approved_consent_refs: set[str] | frozenset[str],
) -> bool:
    """Return whether a reviewed record may enter a public dataset subset.

    References are checked against release-specific approval sets so a merely
    non-empty manifest string can never be mistaken for legal review.
    """
    parsed = record if isinstance(record, ManifestRecord) else ManifestRecord.from_dict(record)
    values = parsed.values
    return (
        not values["export_restricted"]
        and values["redaction_applied"]
        and values["license"] in {"owned", "licensed", "synthetic"}
        and values["license_ref"] in approved_license_refs
        and values["consent_ref"] in approved_consent_refs
    )


def validate_manifest(records: Sequence[Mapping[str, Any]]) -> list[ManifestRecord]:
    parsed = [ManifestRecord.from_dict(record) for record in records]
    clip_ids = [record.values["clip_id"] for record in parsed]
    if len(set(clip_ids)) != len(clip_ids):
        raise ValueError("manifest clip_id values must be unique")
    group_splits: dict[str, str] = {}
    for record in parsed:
        group = str(record.values["split_group"])
        split = str(record.values["split"])
        previous = group_splits.setdefault(group, split)
        if previous != split:
            raise ValueError(f"split group {group} leaks across {previous} and {split}")
    return parsed


def load_manifest(path: str | Path) -> list[ManifestRecord]:
    with Path(path).open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, list):
        raise ValueError("manifest root must be an array")
    return validate_manifest(payload)
