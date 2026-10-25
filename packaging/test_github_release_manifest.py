"""The GitHub Releases update manifest must satisfy the updater's security contract."""
from __future__ import annotations

import base64
import hashlib
import json
import sys
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat

sys.path.insert(0, str(Path(__file__).resolve().parent))
import github_release_manifest as tool  # noqa: E402


@pytest.fixture
def signer(monkeypatch):
    key = Ed25519PrivateKey.generate()
    seed = key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    public = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    monkeypatch.setitem(tool.RELEASE_PUBLIC_KEYS, "release-ci-v1", public)
    monkeypatch.setenv("CHUDWARE_RELEASE_SIGNING_KEY", base64.b64encode(seed).decode("ascii"))
    return public


def _artifact(tmp_path: Path, name: str) -> Path:
    path = tmp_path / name
    path.write_bytes(b"inert release artifact fixture")
    return path


@pytest.mark.parametrize("platform, arch, name", [
    ("linux", "x86_64", "Chudware-0.2.0-linux-x86_64.AppImage"),
    ("windows", "x86_64", "Chudware-Setup-0.2.0-x86_64.exe"),
    ("macos", "arm64", "Chudware-0.2.0-macos-arm64.zip"),
])
def test_manifest_download_metadata_and_signature(tmp_path, signer, platform, arch, name):
    artifact = _artifact(tmp_path, name)
    out = tmp_path / "out"
    assert tool.main([str(artifact), "--platform", platform, "--arch", arch, "--version", "0.2.0",
                      "--tag", "v0.2.0", "--output-dir", str(out)]) == 0

    manifest = json.loads((out / f"update-{platform}-{arch}.json").read_text(encoding="utf-8"))
    assert "min_supported" not in manifest
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    assert manifest["sha256"] == digest
    assert manifest["size"] == artifact.stat().st_size
    assert manifest["url"] == f"https://github.com/Hunter-124/Chudware/releases/download/v0.2.0/{name}"
    assert manifest["notes_url"] == "https://github.com/Hunter-124/Chudware/releases/tag/v0.2.0"
    preimage = b"\0".join(value.encode("ascii") for value in (
        "chudware-artifact-v1", "release-ci-v1", "0.2.0", platform, arch, digest))
    Ed25519PublicKey.from_public_bytes(signer).verify(base64.b64decode(manifest["signature"]), preimage)


def test_a_secret_outside_the_shipped_keyring_writes_nothing(tmp_path, signer, monkeypatch, capsys):
    other = Ed25519PrivateKey.generate().public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    monkeypatch.setitem(tool.RELEASE_PUBLIC_KEYS, "release-ci-v1", other)
    artifact = _artifact(tmp_path, "Chudware-0.2.0-linux-x86_64.AppImage")
    assert tool.main([str(artifact), "--platform", "linux", "--arch", "x86_64", "--version", "0.2.0",
                      "--tag", "v0.2.0"]) == 2
    assert "core/security/release_keys.h disagree" in capsys.readouterr().err
    assert not (tmp_path / "update-linux-x86_64.json").exists()


@pytest.mark.parametrize("args, message", [
    (["--platform", "linux", "--arch", "x86_64", "--version", "0.3.0", "--tag", "v0.3.0"], "canonical name"),
    (["--platform", "linux", "--arch", "arm64", "--version", "0.2.0", "--tag", "v0.2.0"], "unsupported platform"),
    (["--platform", "linux", "--arch", "x86_64", "--version", "0.2.0", "--tag", "v0.2.0",
      "--key-id", "dev-test-v1"], "unknown release key id"),
    (["--platform", "linux", "--arch", "x86_64", "--version", "0.2.0", "--tag", "../v0.2.0"], "plain release tag"),
])
def test_mismatched_identity_is_refused_before_signing(tmp_path, signer, monkeypatch, capsys, args, message):
    def forbidden():
        raise AssertionError("the signing key must not be loaded for an invalid request")

    monkeypatch.setattr(tool, "load_signing_key", forbidden)
    artifact = _artifact(tmp_path, "Chudware-0.2.0-linux-x86_64.AppImage")
    assert tool.main([str(artifact), *args]) == 2
    assert message in capsys.readouterr().err


def test_existing_manifest_is_never_replaced(tmp_path, signer):
    artifact = _artifact(tmp_path, "Chudware-0.2.0-linux-x86_64.AppImage")
    existing = tmp_path / "update-linux-x86_64.json"
    existing.write_text("{}\n", encoding="utf-8")
    assert tool.main([str(artifact), "--platform", "linux", "--arch", "x86_64", "--version", "0.2.0",
                      "--tag", "v0.2.0"]) == 2
    assert existing.read_text(encoding="utf-8") == "{}\n"
