from __future__ import annotations

import argparse
import base64
import hashlib
import importlib.util
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

def _load_signer():
    path = Path(__file__).with_name("sign_release.py")
    spec = importlib.util.spec_from_file_location("chudware_sign_release", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sign_release = _load_signer()


def _load_publish():
    path = Path(__file__).with_name("publish.py")
    spec = importlib.util.spec_from_file_location("chudware_publish", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_signer_rejects_wrong_domain_before_decrypt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    called = False
    def forbidden(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("must not decrypt a malformed request")
    monkeypatch.setattr(sign_release, "decrypt_key", forbidden)
    with pytest.raises(sign_release.SigningError, match="domain"):
        sign_release.sign_request("artifact", b"chudware-payload-v1\0bad", tmp_path / "k.age", tmp_path / "id")
    assert not called


def test_signer_accepts_only_age_file_and_never_passes_plaintext_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    identity = tmp_path / "identity.txt"
    identity.write_text("identity")
    plaintext = tmp_path / "release.key"
    plaintext.write_text("secret")
    with pytest.raises(sign_release.SigningError, match="age-encrypted"):
        sign_release.decrypt_key(plaintext, identity)

    encrypted = tmp_path / "release.key.age"
    encrypted.write_bytes(b"ciphertext")
    seen = {}
    class Result:
        stdout = b"decrypted"
        stderr = b""
    def fake_run(command, **kwargs):
        seen["command"] = command
        seen["kwargs"] = kwargs
        return Result()
    monkeypatch.setattr(sign_release.subprocess, "run", fake_run)
    assert sign_release.decrypt_key(encrypted, identity) == b"decrypted"
    assert seen["command"] == ["age", "--decrypt", "--identity", str(identity), str(encrypted)]
    assert seen["kwargs"]["stdout"] is sign_release.subprocess.PIPE


def test_publish_rejects_wrong_key_and_digest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    publish = _load_publish()
    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    monkeypatch.setattr(publish, "_RELEASE_PUBLIC_KEYS", {"test": public})
    digest = hashlib.sha256(b"artifact").hexdigest()
    preimage = publish.artifact_signature_preimage(
        "test", "1.2.3", "linux", "x86_64", digest
    )
    signature = key.sign(preimage)
    publish.verify_artifact_signature(
        "test", "1.2.3", "linux", "x86_64", digest, signature
    )
    with pytest.raises(publish.PublishError, match="unknown release key"):
        publish.verify_artifact_signature(
            "other", "1.2.3", "linux", "x86_64", digest, signature
        )
    for identity in (
        ("1.2.4", "linux", "x86_64", digest),
        ("1.2.3", "windows", "x86_64", digest),
        ("1.2.3", "linux", "arm64", digest),
        ("1.2.3", "linux", "x86_64", hashlib.sha256(b"changed").hexdigest()),
    ):
        with pytest.raises(publish.PublishError, match="does not match"):
            publish.verify_artifact_signature("test", *identity, signature)


def test_publisher_has_no_private_key_or_signing_command():
    publish = _load_publish()
    source = Path(publish.__file__).read_text(encoding="utf-8")
    assert "CHUDWARE_RELEASE_SIGNING_KEY" not in source
    assert "Ed25519PrivateKey" not in source
    assert not hasattr(publish, "load_signing_key")
    assert not hasattr(publish, "release_signing_key")
    upload = publish.parser().parse_args([
        "upload", "artifact", "--artifact-signature", "artifact.sig",
        "--payload-manifest", "payload.json", "--payload-signature", "payload.sig",
        "--sbom", "sbom.json", "--sbom-signature", "sbom.sig",
        "--provenance", "provenance.json", "--provenance-signature", "provenance.sig",
        "--platform-test", "platform.json", "--platform-test-signature", "platform.sig",
        "--channel", "beta",
    ])
    assert isinstance(upload, argparse.Namespace)


def test_artifact_request_domain_is_not_payload_domain():
    key = Ed25519PrivateKey.generate()
    request = sign_release.DOMAINS["artifact"] + b"test\0" + b"a" * 64
    signature = key.sign(request)
    key.public_key().verify(signature, request)
    with pytest.raises(Exception):
        key.public_key().verify(signature, sign_release.DOMAINS["payload"] + request)

def test_evidence_signing_requires_exact_single_lf_canonical_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    canonical = sign_release.canonical_json_bytes({"é": 1, "a": 2})
    assert canonical == b'{"a":2,"\xc3\xa9":1}\n'
    request = sign_release.DOMAINS["sbom"] + canonical
    sign_release.validate_request("sbom", request)
    for replacement in (canonical[:-1], canonical + b"\n", canonical.replace(b"\n", b"\r\n")):
        with pytest.raises(sign_release.SigningError, match="canonical"):
            sign_release.validate_request("sbom", sign_release.DOMAINS["sbom"] + replacement)


@pytest.mark.parametrize(
    ("value", "expected"),
    [("1.2.3", (1, 2, 3)), ("2.10.0", (2, 10, 0)), ("10.0.12", (10, 0, 12))],
)
def test_publisher_parses_canonical_release_versions(value: str, expected: tuple[int, int, int]):
    publish = _load_publish()
    assert publish.parse_release_version(value) == expected


@pytest.mark.parametrize("value", ["01.2.3", "1.02.3", "1.2.03", "1.2", "1.2.3-rc.1", "1.2.3+build", "v1.2.3"])
def test_publisher_rejects_noncanonical_or_prerelease_versions(value: str):
    publish = _load_publish()
    with pytest.raises(publish.PublishError, match="canonical major.minor.patch"):
        publish.parse_release_version(value)
