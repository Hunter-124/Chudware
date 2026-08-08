"""Owned printer, filament, and process profile models."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from .contract import (
    ContractValidationError,
    ProfileRef,
    canonical_json,
    content_sha256,
)


def _validate_identity(profile_id: str, name: str, rev: int) -> None:
    if not isinstance(profile_id, str) or not profile_id.strip():
        raise ContractValidationError("profile id must be a non-empty string")
    if not isinstance(name, str) or not name.strip():
        raise ContractValidationError("profile name must be a non-empty string")
    if not isinstance(rev, int) or isinstance(rev, bool) or rev < 0:
        raise ContractValidationError("profile rev must be a non-negative integer")


def _validate_payload(*values: Any) -> None:
    for value in values:
        canonical_json(value)


class _Profile:
    kind: str

    def to_dict(self) -> dict[str, Any]:
        raise NotImplementedError

    @property
    def profile_hash(self) -> str:
        """Hash every persisted profile field in canonical order."""

        return content_sha256(self.to_dict())

    @property
    def bundle_sha256(self) -> str:
        return self.profile_hash

    def to_ref(self) -> ProfileRef:
        return ProfileRef(
            kind=self.kind,
            id=self.id,
            rev=self.rev,
            bundle_sha256=self.profile_hash,
        )


@dataclass(frozen=True)
class PrinterProfile(_Profile):
    id: str
    name: str
    rev: int
    vendor: str
    model: str
    build_volume: Mapping[str, Any]
    extruders: tuple[Mapping[str, Any], ...]
    bed: Mapping[str, Any]
    chamber: Mapping[str, Any]
    gcode_flavor: str
    kinematics: str
    supports_arcs: bool = False
    supports_bgcode: bool = False
    firmware_retraction: bool = False
    inherits: str | None = None
    raw_bundle: Mapping[str, Any] = field(default_factory=dict)
    kind: str = field(default="printer", init=False)

    def __post_init__(self) -> None:
        _validate_identity(self.id, self.name, self.rev)
        for value, field_name in (
            (self.vendor, "vendor"),
            (self.model, "model"),
            (self.gcode_flavor, "gcode_flavor"),
            (self.kinematics, "kinematics"),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ContractValidationError(f"printer {field_name} must be non-empty")
        if not self.extruders:
            raise ContractValidationError("printer profile must define at least one extruder")
        _validate_payload(
            self.build_volume,
            self.extruders,
            self.bed,
            self.chamber,
            self.raw_bundle,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "id": self.id,
            "name": self.name,
            "rev": self.rev,
            "vendor": self.vendor,
            "model": self.model,
            "build_volume": dict(self.build_volume),
            "extruders": [dict(extruder) for extruder in self.extruders],
            "bed": dict(self.bed),
            "chamber": dict(self.chamber),
            "gcode_flavor": self.gcode_flavor,
            "kinematics": self.kinematics,
            "supports_arcs": self.supports_arcs,
            "supports_bgcode": self.supports_bgcode,
            "firmware_retraction": self.firmware_retraction,
            "inherits": self.inherits,
            "raw_bundle": dict(self.raw_bundle),
        }


@dataclass(frozen=True)
class FilamentProfile(_Profile):
    id: str
    name: str
    rev: int
    typed: Mapping[str, Any]
    raw_bundle: Mapping[str, Any]
    inherits: str | None = None
    extruder_hint: int | None = None
    kind: str = field(default="filament", init=False)

    def __post_init__(self) -> None:
        _validate_identity(self.id, self.name, self.rev)
        if self.extruder_hint is not None and (
            not isinstance(self.extruder_hint, int)
            or isinstance(self.extruder_hint, bool)
            or self.extruder_hint < 0
        ):
            raise ContractValidationError("filament extruder_hint must be non-negative")
        _validate_payload(self.typed, self.raw_bundle)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "id": self.id,
            "name": self.name,
            "rev": self.rev,
            "typed": dict(self.typed),
            "raw_bundle": dict(self.raw_bundle),
            "inherits": self.inherits,
            "extruder_hint": self.extruder_hint,
        }


@dataclass(frozen=True)
class ProcessProfile(_Profile):
    id: str
    name: str
    rev: int
    typed: Mapping[str, Any]
    raw_bundle: Mapping[str, Any]
    inherits: str | None = None
    kind: str = field(default="process", init=False)

    def __post_init__(self) -> None:
        _validate_identity(self.id, self.name, self.rev)
        _validate_payload(self.typed, self.raw_bundle)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "id": self.id,
            "name": self.name,
            "rev": self.rev,
            "typed": dict(self.typed),
            "raw_bundle": dict(self.raw_bundle),
            "inherits": self.inherits,
        }


def hash_profile(profile: _Profile) -> str:
    return profile.profile_hash
