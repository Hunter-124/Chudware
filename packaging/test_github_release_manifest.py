"""The GitHub Releases update manifest must be exactly what the updater verifies."""
from __future__ import annotations

import base64
import hashlib
import json
import re
import sys
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat

sys.path.insert(0, str(Path(__file__).resolve().parent))
import github_release_manifest as tool  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
UPDATER = REPO / "host" / "src" / "update" / "updater.cpp"
RELEASE_KEYS = REPO / "core" / "security" / "release_keys.h"


def _function_body(source: str, signature: str) -> str:
    start = source.index(signature)
    return source[start:source.index("\n}\n", start)]


def _updater_fields() -> set[str]:
    body = _function_body(UPDATER.read_text(encoding="utf-8"), "bool manifestFromJson(")
    return set(re.findall(r'value\.(?:at|value)\("([a-z0-9_]+)"', body))


def _updater_targets() -> tuple[set[str], set[str]]:
    source = UPDATER.read_text(encoding="utf-8")
    platforms = set(re.findall(r'return "([a-z0-9_]+)";', _function_body(source, "std::string platformName(")))
    arches = set(re.findall(r'return "([a-z0-9_]+)";', _function_body(source, "std::string architectureName(")))
    return platforms, arches


def _packaged_release_keys() -> dict[str, bytes]:
    header = RELEASE_KEYS.read_text(encoding="utf-8").split("#ifdef CHUDWARE_DEVELOPER_BUILD", 1)[0]
    keys = {}
    for key_id, body in re.findall(r'Key\{\s*"([^"]+)",\s*\{([^}]*)\}', header):
        keys[key_id] = bytes(int(value, 16) for value in re.findall(r"0x([0-9a-fA-F]{2})", body))
    return keys


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


def test_keyring_matches_the_packaged_updater_trust_ring():
    assert tool.RELEASE_PUBLIC_KEYS == _packaged_release_keys()


def test_cli_platforms_match_the_updater_identity():
    platforms, arches = _updater_targets()
    assert set(tool.PLATFORMS) == platforms
    assert set(tool.ARCHES) == arches


@pytest.mark.parametrize("platform, arch, name", [
    ("linux", "x86_64", "Chudware-0.2.0-linux-x86_64.AppImage"),
    ("windows", "x86_64", "Chudware-Setup-0.2.0-x86_64.exe"),
    ("macos", "arm64", "Chudware-0.2.0-macos-arm64.zip"),
])
def test_manifest_has_the_updater_fields_and_a_verifying_signature(tmp_path, signer, platform, arch, name):
    artifact = _artifact(tmp_path, name)
    out = tmp_path / "out"
    assert tool.main([str(artifact), "--platform", platform, "--arch", arch, "--version", "0.2.0",
                      "--tag", "v0.2.0", "--output-dir", str(out)]) == 0

    manifest = json.loads((out / f"update-{platform}-{arch}.json").read_text(encoding="utf-8"))
    assert set(manifest) == _updater_fields()
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    assert manifest == {
        "version": "0.2.0", "min_supported": "0.0.0", "platform": platform, "arch": arch,
        "key_id": "release-ci-v1", "sha256": digest, "size": artifact.stat().st_size,
        "signature": manifest["signature"],
        "notes_url": "https://github.com/Hunter-124/Chudware/releases/tag/v0.2.0",
        "url": f"https://github.com/Hunter-124/Chudware/releases/download/v0.2.0/{name}",
    }
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
