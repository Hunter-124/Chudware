from __future__ import annotations

import base64
import hashlib
import importlib.util
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)

def _load_signer():
    path = Path(__file__).with_name("sign_release.py")
    spec = importlib.util.spec_from_file_location("chudware_sign_release", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sign_release = _load_signer()


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


def _load_ci_signer():
    path = Path(__file__).with_name("ci_sign.py")
    spec = importlib.util.spec_from_file_location("chudware_ci_sign", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_artifact_request_must_carry_the_full_verifier_identity():
    # Every verifier (updater, packaging/github_release_manifest.py) recomputes
    # domain\0key_id\0version\0platform\0arch\0sha256. A shorter request would be
    # signed happily and then verify nowhere.
    digest = hashlib.sha256(b"artifact").hexdigest().encode("ascii")
    complete = sign_release.DOMAINS["artifact"] + b"\0".join(
        (b"release-ci-v1", b"0.1.0-beta.42", b"linux", b"x86_64", digest))
    sign_release.validate_request("artifact", complete)
    for truncated in (
        sign_release.DOMAINS["artifact"] + b"release-ci-v1\0" + digest,
        sign_release.DOMAINS["artifact"] + b"\0".join((b"release-ci-v1", b"0.1.0", b"linux", digest)),
        sign_release.DOMAINS["artifact"] + b"\0".join((b"release-ci-v1", b"", b"linux", b"x86_64", digest)),
        sign_release.DOMAINS["artifact"] + b"\0".join(
            (b"release-ci-v1", b"0.1.0", b"linux", b"x86_64", digest.upper())),
    ):
        with pytest.raises(sign_release.SigningError):
            sign_release.validate_request("artifact", truncated)


def test_ci_signer_signs_the_artifact_preimage_from_the_environment_seed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    ci_sign = _load_ci_signer()
    key = Ed25519PrivateKey.generate()
    seed = key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    public = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    artifact = tmp_path / "Chudware-0.1.0-beta.42-linux-x86_64.AppImage"
    artifact.write_bytes(b"appimage")
    monkeypatch.setenv("CHUDWARE_RELEASE_SIGNING_KEY", base64.b64encode(seed).decode("ascii"))

    request_path = tmp_path / "request.bin"
    assert ci_sign.main([
        "--expect-public-key", public.hex(), "--output", str(tmp_path / "artifact.sig"),
        "artifact", "--key-id", "release-ci-v1", "--version", "0.1.0-beta.42",
        "--platform", "linux", "--arch", "x86_64", "--artifact", str(artifact),
        "--request-out", str(request_path),
    ]) == 0
    expected = b"\0".join((
        b"chudware-artifact-v1", b"release-ci-v1", b"0.1.0-beta.42", b"linux", b"x86_64",
        hashlib.sha256(b"appimage").hexdigest().encode("ascii"),
    ))
    assert request_path.read_bytes() == expected
    signature = base64.b64decode((tmp_path / "artifact.sig").read_text(encoding="ascii").strip())
    key.public_key().verify(signature, expected)


def test_ci_signer_refuses_a_seed_that_is_not_the_expected_release_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    ci_sign = _load_ci_signer()
    seed = Ed25519PrivateKey.generate().private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    other = Ed25519PrivateKey.generate().public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    request = tmp_path / "payload.bin"
    request.write_bytes(sign_release.DOMAINS["payload"] + b"body")
    monkeypatch.setenv("CHUDWARE_RELEASE_SIGNING_KEY", base64.b64encode(seed).decode("ascii"))
    assert ci_sign.main(["--expect-public-key", other.hex(), "sign", "payload", str(request)]) == 2
    assert "does not match the expected release public key" in capsys.readouterr().err

    monkeypatch.delenv("CHUDWARE_RELEASE_SIGNING_KEY")
    assert ci_sign.main(["sign", "payload", str(request)]) == 2
    assert "CHUDWARE_RELEASE_SIGNING_KEY is not set" in capsys.readouterr().err


def _publisher_policy_request():
    return sign_release.DOMAINS["platform-publishers"] + sign_release.canonical_json_bytes({
        "schema": "chudware-platform-publishers-v1", "key_id": "release-v1",
        "windows": {"certificate_sha256": ["1" * 64]},
        "macos": {"team_ids": ["TESTTEAM01"], "bundle_id": "com.chudware.Chudware"},
    })


def test_invalid_publisher_policy_never_decrypts_owner_key(tmp_path, monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("malformed policy must not decrypt an owner key")
    monkeypatch.setattr(sign_release, "decrypt_key", forbidden)
    for body in (
        b"{}\\n", b'{"key_id":"release-ci-v1"}\\n',
        sign_release.canonical_json_bytes({"schema": "chudware-platform-publishers-v1"}),
    ):
        with pytest.raises(sign_release.SigningError):
            sign_release.sign_request(
                "platform-publishers", sign_release.DOMAINS["platform-publishers"] + body,
                tmp_path / "owner.age", tmp_path / "identity",
            )


def test_ci_or_caller_key_cannot_authorize_publishers():
    key = Ed25519PrivateKey.generate()
    with pytest.raises(sign_release.SigningError, match="offline release-v1 owner key"):
        sign_release.sign_request_with_key("platform-publishers", _publisher_policy_request(), key)


def test_owner_policy_signing_and_authentication_share_exact_domain(tmp_path, monkeypatch):
    import sys
    policy_module = sys.modules[sign_release.require_owner_signing_key.__module__]
    # Disposable test authority only; never replace or exercise the product key.
    owner = Ed25519PrivateKey.generate()
    public = owner.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    monkeypatch.setattr(policy_module, "_OWNER_PUBLIC_KEY", public)
    request = _publisher_policy_request()
    signature = sign_release.sign_request_with_key("platform-publishers", request, owner)
    policy = tmp_path / "policy.json"
    detached = tmp_path / "policy.sig"
    policy.write_bytes(request[len(sign_release.DOMAINS["platform-publishers"]):])
    detached.write_text(signature + "\n", encoding="ascii")
    authenticated = policy_module.load_policy(policy, detached)
    assert authenticated.sha256 == hashlib.sha256(policy.read_bytes()).hexdigest()
    assert authenticated.certificate_sha256 == ("1" * 64,)
    owner.public_key().verify(base64.b64decode(signature), request)


def test_ci_cli_excludes_owner_policy_purpose_before_loading_seed(tmp_path, monkeypatch):
    ci_sign = _load_ci_signer()
    def forbidden():
        raise AssertionError("CI must not load a key for owner-only publisher policy")
    monkeypatch.setattr(ci_sign, "load_signing_key", forbidden)
    request = tmp_path / "policy-request.bin"
    request.write_bytes(_publisher_policy_request())
    with pytest.raises(SystemExit) as error:
        ci_sign.main(["sign", "platform-publishers", str(request)])
    assert error.value.code == 2
