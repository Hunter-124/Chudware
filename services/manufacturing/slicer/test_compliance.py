from __future__ import annotations

import hashlib
from pathlib import Path

from services.manufacturing.slicer.compliance import (
    CorrespondingSourceManifest,
    PatchRecord,
    validate_manifest,
    verify_distributed_sidecar,
)


FAKE_SLICER = Path(__file__).resolve().parents[3] / "fixtures/mfg/print/fake_slicer.py"
COMMIT = "4f81e8d11a2d7427953481df3b0545a193c4c78f"


def _manifest(**changes: object) -> CorrespondingSourceManifest:
    values: dict[str, object] = {
        "upstream_tag": "version_2.9.6",
        "upstream_commit": "a" * 40,
        "patches": (PatchRecord("patches/0001-contract.patch", "b" * 64, "contract output"),),
        "build_script_ref": "scripts/build_slicer.sh@abc123",
        "notices": ("THIRD_PARTY_NOTICES.txt", "MODIFICATIONS.md", "SOURCE_OFFER.txt"),
        "source_url": "https://downloads.example.test/slicer/source.tar.zst",
        "source_sha256": "c" * 64,
        "binary_sha256": hashlib.sha256(FAKE_SLICER.read_bytes()).hexdigest(),
        "sidecar_version": "0.1.0-fixture",
        "sidecar_commit": COMMIT,
    }
    values.update(changes)
    return CorrespondingSourceManifest(**values)  # type: ignore[arg-type]


def test_matching_distributed_sidecar_manifest_verifies() -> None:
    result = verify_distributed_sidecar(
        FAKE_SLICER,
        _manifest(),
        reported_version="0.1.0-fixture",
        reported_commit=COMMIT,
    )

    assert result.ok
    assert result.errors == ()
    assert result.binary_sha256 == hashlib.sha256(FAKE_SLICER.read_bytes()).hexdigest()


def test_manifest_without_patch_list_fails() -> None:
    errors = validate_manifest(_manifest(patches=()))

    assert "missing patch list" in errors


def test_manifest_without_source_url_fails() -> None:
    errors = validate_manifest(_manifest(source_url=""))

    assert "missing corresponding source URL" in errors


def test_manifest_hash_must_match_distributed_binary() -> None:
    result = verify_distributed_sidecar(FAKE_SLICER, _manifest(binary_sha256="0" * 64))

    assert not result.ok
    assert "distributed sidecar hash does not match manifest" in result.errors
