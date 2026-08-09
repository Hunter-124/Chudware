"""Checkable AGPL corresponding-source metadata for distributed slicer binaries."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

from .contract import ContractValidationError, validate_sha256
from .sidecar import ResolvedBinary, resolve_binary


@dataclass(frozen=True)
class PatchRecord:
    file: str
    sha256: str
    subject: str

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "PatchRecord":
        return cls(
            file=str(payload.get("file", "")),
            sha256=str(payload.get("sha256", "")),
            subject=str(payload.get("subject", "")),
        )


@dataclass(frozen=True)
class CorrespondingSourceManifest:
    upstream_tag: str
    upstream_commit: str
    patches: tuple[PatchRecord, ...]
    build_script_ref: str
    notices: tuple[str, ...]
    source_url: str
    source_sha256: str
    binary_sha256: str
    sidecar_version: str
    sidecar_commit: str
    license: str = "AGPL-3.0-or-later"

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "CorrespondingSourceManifest":
        raw_patches = payload.get("patches", ())
        raw_notices = payload.get("notices", ())
        return cls(
            upstream_tag=str(payload.get("upstream_tag", "")),
            upstream_commit=str(payload.get("upstream_commit", "")),
            patches=tuple(
                PatchRecord.from_dict(item)
                for item in raw_patches
                if isinstance(item, Mapping)
            ) if isinstance(raw_patches, (list, tuple)) else (),
            build_script_ref=str(payload.get("build_script_ref", "")),
            notices=tuple(str(item) for item in raw_notices)
            if isinstance(raw_notices, (list, tuple))
            else (),
            source_url=str(payload.get("source_url", "")),
            source_sha256=str(payload.get("source_sha256", "")),
            binary_sha256=str(payload.get("binary_sha256", "")),
            sidecar_version=str(payload.get("sidecar_version", "")),
            sidecar_commit=str(payload.get("sidecar_commit", "")),
            license=str(payload.get("license", "AGPL-3.0-or-later")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "upstream_tag": self.upstream_tag,
            "upstream_commit": self.upstream_commit,
            "patches": [
                {"file": patch.file, "sha256": patch.sha256, "subject": patch.subject}
                for patch in self.patches
            ],
            "build_script_ref": self.build_script_ref,
            "notices": list(self.notices),
            "source_url": self.source_url,
            "source_sha256": self.source_sha256,
            "binary_sha256": self.binary_sha256,
            "sidecar_version": self.sidecar_version,
            "sidecar_commit": self.sidecar_commit,
            "license": self.license,
        }


@dataclass(frozen=True)
class ComplianceVerification:
    ok: bool
    errors: tuple[str, ...]
    binary_sha256: str | None


def _is_source_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc) and bool(parsed.path)


def validate_manifest(manifest: CorrespondingSourceManifest) -> tuple[str, ...]:
    """Return every release-blocking manifest defect without raising."""

    errors: list[str] = []
    required_text = {
        "upstream_tag": manifest.upstream_tag,
        "upstream_commit": manifest.upstream_commit,
        "build_script_ref": manifest.build_script_ref,
        "sidecar_version": manifest.sidecar_version,
        "sidecar_commit": manifest.sidecar_commit,
    }
    for field_name, value in required_text.items():
        if not value.strip():
            errors.append(f"missing {field_name}")
    if not manifest.patches:
        errors.append("missing patch list")
    for index, patch in enumerate(manifest.patches):
        if not patch.file.strip():
            errors.append(f"patch {index} missing file")
        if not patch.subject.strip():
            errors.append(f"patch {index} missing subject")
        try:
            validate_sha256(patch.sha256, f"patch {index} sha256")
        except ContractValidationError as exc:
            errors.append(str(exc))
    if not manifest.notices:
        errors.append("missing notices")
    elif not all(notice.strip() for notice in manifest.notices):
        errors.append("notices contain an empty reference")
    if not manifest.source_url.strip():
        errors.append("missing corresponding source URL")
    elif not _is_source_url(manifest.source_url):
        errors.append("corresponding source URL must be an absolute HTTP(S) URL")
    for field_name, digest in (
        ("source_sha256", manifest.source_sha256),
        ("binary_sha256", manifest.binary_sha256),
    ):
        try:
            validate_sha256(digest, field_name)
        except ContractValidationError as exc:
            errors.append(str(exc))
    if manifest.license != "AGPL-3.0-or-later":
        errors.append("license must be AGPL-3.0-or-later")
    return tuple(errors)


def hash_file(path: str | Path) -> str:
    resolved = resolve_binary(path, environ={})
    if not isinstance(resolved, ResolvedBinary):
        raise ValueError(f"{resolved.code}: {resolved.message}")
    return resolved.sha256


def verify_distributed_sidecar(
    binary: str | Path,
    manifest: CorrespondingSourceManifest,
    *,
    reported_version: str | None = None,
    reported_commit: str | None = None,
) -> ComplianceVerification:
    """Verify manifest completeness and that it belongs to these distributed bytes."""

    errors = list(validate_manifest(manifest))
    resolved = resolve_binary(binary, environ={})
    actual_hash: str | None = None
    if not isinstance(resolved, ResolvedBinary):
        errors.append(f"{resolved.code}: {resolved.message}")
    else:
        actual_hash = resolved.sha256
        if actual_hash != manifest.binary_sha256:
            errors.append("distributed sidecar hash does not match manifest")
    if reported_version is not None and reported_version != manifest.sidecar_version:
        errors.append("reported sidecar version does not match manifest")
    if reported_commit is not None and reported_commit != manifest.sidecar_commit:
        errors.append("reported sidecar commit does not match manifest")
    return ComplianceVerification(ok=not errors, errors=tuple(errors), binary_sha256=actual_hash)
