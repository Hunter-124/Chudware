"""Typed, deterministic contract for an arms-length slicer process."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Mapping, Sequence

_SHA256_LEN = 64
_PROFILE_KINDS = frozenset({"printer", "filament", "process"})
_OUTPUT_FORMATS = frozenset({"gcode", "bgcode", "3mf"})


class ContractValidationError(ValueError):
    """A local request or sidecar response violates the public slice contract."""


def _require_text(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ContractValidationError(f"{field_name} must be a non-empty string")


def validate_sha256(value: str, field_name: str = "sha256") -> None:
    if (
        not isinstance(value, str)
        or len(value) != _SHA256_LEN
        or any(ch not in "0123456789abcdef" for ch in value)
    ):
        raise ContractValidationError(f"{field_name} must be a lowercase SHA-256 hex digest")


def _canonical_text(value: Any) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ContractValidationError("canonical JSON does not permit non-finite floats")
        # Seventeen significant digits are sufficient for an IEEE-754 round trip.
        rendered = format(value, ".17g")
        return "0" if rendered == "-0" else rendered
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise ContractValidationError("canonical JSON object keys must be strings")
        return "{" + ",".join(
            f"{_canonical_text(key)}:{_canonical_text(value[key])}" for key in sorted(value)
        ) + "}"
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return "[" + ",".join(_canonical_text(item) for item in value) + "]"
    if isinstance(value, Path):
        return _canonical_text(str(value))
    raise ContractValidationError(f"unsupported canonical JSON value: {type(value).__name__}")


def canonical_json(value: Any) -> bytes:
    """Return canonical UTF-8 JSON with sorted keys and round-trip floats."""

    return _canonical_text(value).encode("utf-8")


def content_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


@dataclass(frozen=True)
class ProfileRef:
    kind: str
    id: str
    rev: int
    bundle_sha256: str

    def __post_init__(self) -> None:
        if self.kind not in _PROFILE_KINDS:
            raise ContractValidationError(
                f"profile kind must be one of {sorted(_PROFILE_KINDS)}, got {self.kind!r}"
            )
        _require_text(self.id, "profile.id")
        if not isinstance(self.rev, int) or isinstance(self.rev, bool) or self.rev < 0:
            raise ContractValidationError("profile.rev must be a non-negative integer")
        validate_sha256(self.bundle_sha256, "profile.bundle_sha256")

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "id": self.id,
            "rev": self.rev,
            "bundle_sha256": self.bundle_sha256,
        }


@dataclass(frozen=True)
class ModelRef:
    """Exact model bytes and placement handed to the sidecar."""

    id: str
    path: str
    mesh_sha256: str
    transform: Mapping[str, Any] = field(default_factory=dict)
    extruder: int = 0
    overrides: Mapping[str, Any] = field(default_factory=dict)
    modifiers: tuple[Mapping[str, Any], ...] = ()

    def __post_init__(self) -> None:
        _require_text(self.id, "model.id")
        _require_text(self.path, "model.path")
        validate_sha256(self.mesh_sha256, "model.mesh_sha256")
        if not isinstance(self.extruder, int) or isinstance(self.extruder, bool) or self.extruder < 0:
            raise ContractValidationError("model.extruder must be a non-negative integer")
        # Validate every opaque value now, rather than failing after a slicer is spawned.
        canonical_json(self.transform)
        canonical_json(self.overrides)
        canonical_json(self.modifiers)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "path": self.path,
            "mesh_sha256": self.mesh_sha256,
            "transform": dict(self.transform),
            "extruder": self.extruder,
            "overrides": dict(self.overrides),
            "modifiers": [dict(modifier) for modifier in self.modifiers],
        }


@dataclass(frozen=True)
class Determinism:
    seed: int
    strip_timestamps: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.seed, int) or isinstance(self.seed, bool) or self.seed < 0:
            raise ContractValidationError("determinism.seed must be a non-negative integer")
        if not self.strip_timestamps:
            raise ContractValidationError("determinism.strip_timestamps must be true")

    def to_dict(self) -> dict[str, Any]:
        return {"seed": self.seed, "strip_timestamps": self.strip_timestamps}


@dataclass(frozen=True)
class SliceRequest:
    project_id: str
    project_rev: int
    plate_id: str
    sidecar_build_id: str
    profiles: tuple[ProfileRef, ...]
    models: tuple[ModelRef, ...]
    determinism: Determinism
    output_format: str = "gcode"
    thumbnails: tuple[tuple[int, int], ...] = ()
    overrides: Mapping[str, Any] = field(default_factory=dict)
    request_id: str = field(init=False)

    def __post_init__(self) -> None:
        _require_text(self.project_id, "project_id")
        _require_text(self.plate_id, "plate_id")
        _require_text(self.sidecar_build_id, "sidecar_build_id")
        if not isinstance(self.project_rev, int) or isinstance(self.project_rev, bool) or self.project_rev < 0:
            raise ContractValidationError("project_rev must be a non-negative integer")
        if self.output_format not in _OUTPUT_FORMATS:
            raise ContractValidationError(
                f"output_format must be one of {sorted(_OUTPUT_FORMATS)}"
            )
        if not self.models:
            raise ContractValidationError("a slice request must contain at least one model")
        model_ids = [model.id for model in self.models]
        if len(model_ids) != len(set(model_ids)):
            raise ContractValidationError("model ids must be unique")
        profile_keys = [(profile.kind, profile.id) for profile in self.profiles]
        if len(profile_keys) != len(set(profile_keys)):
            raise ContractValidationError("profile references must be unique")
        counts = {kind: sum(profile.kind == kind for profile in self.profiles) for kind in _PROFILE_KINDS}
        if counts["printer"] != 1 or counts["process"] != 1 or counts["filament"] < 1:
            raise ContractValidationError(
                "profiles must contain one printer, one process, and at least one filament"
            )
        for width, height in self.thumbnails:
            if (
                not isinstance(width, int)
                or isinstance(width, bool)
                or not isinstance(height, int)
                or isinstance(height, bool)
                or width <= 0
                or height <= 0
            ):
                raise ContractValidationError("thumbnail dimensions must be positive integers")
        canonical_json(self.overrides)
        object.__setattr__(self, "request_id", content_sha256(self.identity_dict()))

    def identity_dict(self) -> dict[str, Any]:
        """All semantic inputs, excluding the digest derived from them."""

        return {
            "project_id": self.project_id,
            "project_rev": self.project_rev,
            "plate_id": self.plate_id,
            "sidecar_build_id": self.sidecar_build_id,
            "profiles": [
                profile.to_dict()
                for profile in sorted(self.profiles, key=lambda profile: (profile.kind, profile.id))
            ],
            "instances": [
                model.to_dict() for model in sorted(self.models, key=lambda model: model.id)
            ],
            "overrides": dict(self.overrides),
            "output": {
                "format": self.output_format,
                "thumbnails": [
                    {"w": width, "h": height} for width, height in self.thumbnails
                ],
            },
            "determinism": self.determinism.to_dict(),
        }

    def to_dict(self) -> dict[str, Any]:
        payload = self.identity_dict()
        return {"schema": "chudware.slice_request/1", "request_id": self.request_id, **payload}


@dataclass(frozen=True)
class Artifact:
    path: str
    sha256: str
    bytes: int
    format: str

    def __post_init__(self) -> None:
        _require_text(self.path, "artifact.path")
        validate_sha256(self.sha256, "artifact.sha256")
        if not isinstance(self.bytes, int) or isinstance(self.bytes, bool) or self.bytes < 0:
            raise ContractValidationError("artifact.bytes must be a non-negative integer")
        if self.format not in _OUTPUT_FORMATS:
            raise ContractValidationError("artifact.format is unsupported")

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "sha256": self.sha256,
            "bytes": self.bytes,
            "format": self.format,
        }


@dataclass(frozen=True)
class PerObjectStats:
    object_id: str
    estimated_time_s: float
    filament_mm: tuple[float, ...]
    filament_g: tuple[float, ...]
    layer_count: int

    def __post_init__(self) -> None:
        _require_text(self.object_id, "per_object_stats.object_id")
        numeric = (self.estimated_time_s, *self.filament_mm, *self.filament_g)
        if any(
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(float(value))
            or value < 0
            for value in numeric
        ):
            raise ContractValidationError("per-object statistics must be finite and non-negative")
        if not isinstance(self.layer_count, int) or isinstance(self.layer_count, bool) or self.layer_count < 0:
            raise ContractValidationError("per_object_stats.layer_count must be non-negative")

    def to_dict(self) -> dict[str, Any]:
        return {
            "object_id": self.object_id,
            "estimated_time_s": self.estimated_time_s,
            "filament_mm": list(self.filament_mm),
            "filament_g": list(self.filament_g),
            "layer_count": self.layer_count,
        }


@dataclass(frozen=True)
class SliceProvenance:
    request_id: str
    sidecar_name: str
    sidecar_version: str
    sidecar_commit: str
    sidecar_build_id: str
    binary_sha256: str
    input_sha256: Mapping[str, str]
    output_sha256: str
    profile_hashes: Mapping[str, str]

    def __post_init__(self) -> None:
        validate_sha256(self.request_id, "provenance.request_id")
        _require_text(self.sidecar_name, "provenance.sidecar_name")
        _require_text(self.sidecar_version, "provenance.sidecar_version")
        _require_text(self.sidecar_commit, "provenance.sidecar_commit")
        _require_text(self.sidecar_build_id, "provenance.sidecar_build_id")
        validate_sha256(self.binary_sha256, "provenance.binary_sha256")
        validate_sha256(self.output_sha256, "provenance.output_sha256")
        if not self.input_sha256:
            raise ContractValidationError("provenance.input_sha256 must not be empty")
        if not self.profile_hashes:
            raise ContractValidationError("provenance.profile_hashes must not be empty")
        for key, digest in self.input_sha256.items():
            _require_text(key, "provenance input id")
            validate_sha256(digest, f"provenance.input_sha256[{key!r}]")
        for key, digest in self.profile_hashes.items():
            _require_text(key, "provenance profile id")
            validate_sha256(digest, f"provenance.profile_hashes[{key!r}]")

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "sidecar": {
                "name": self.sidecar_name,
                "version": self.sidecar_version,
                "commit": self.sidecar_commit,
                "build_id": self.sidecar_build_id,
                "binary_sha256": self.binary_sha256,
            },
            "inputs": dict(sorted(self.input_sha256.items())),
            "output_sha256": self.output_sha256,
            "profile_hashes": dict(sorted(self.profile_hashes.items())),
        }


@dataclass(frozen=True)
class SliceResult:
    request_id: str
    artifact: Artifact
    per_object_stats: tuple[PerObjectStats, ...]
    warnings: tuple[str, ...]
    provenance: SliceProvenance
    status: str = "ok"

    def __post_init__(self) -> None:
        validate_sha256(self.request_id, "result.request_id")
        if self.status != "ok":
            raise ContractValidationError("a SliceResult represents only a successful slice")
        if self.request_id != self.provenance.request_id:
            raise ContractValidationError("result and provenance request ids differ")
        if self.artifact.sha256 != self.provenance.output_sha256:
            raise ContractValidationError("artifact and provenance output hashes differ")
        if any(not isinstance(warning, str) or not warning.strip() for warning in self.warnings):
            raise ContractValidationError("warnings must be non-empty strings")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "chudware.slice_result/1",
            "request_id": self.request_id,
            "status": self.status,
            "artifact": self.artifact.to_dict(),
            "per_object_stats": [item.to_dict() for item in self.per_object_stats],
            "warnings": list(self.warnings),
            "provenance": self.provenance.to_dict(),
        }


@dataclass(frozen=True)
class SliceRefusal:
    """Expected, printable failure result; callers need not surface an exception trace."""

    code: str
    message: str
    missing_prerequisite: str | None = None
    detail: str | None = None
    status: str = field(default="refused", init=False)

    def __post_init__(self) -> None:
        _require_text(self.code, "refusal.code")
        _require_text(self.message, "refusal.message")
        if self.missing_prerequisite is not None:
            _require_text(self.missing_prerequisite, "refusal.missing_prerequisite")

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "status": self.status,
            "code": self.code,
            "message": self.message,
        }
        if self.missing_prerequisite is not None:
            payload["missing_prerequisite"] = self.missing_prerequisite
        if self.detail is not None:
            payload["detail"] = self.detail
        return payload
