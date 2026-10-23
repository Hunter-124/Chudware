#!/usr/bin/env python3
"""CI release signer: signs prepared release requests with the pipeline seed.

The owner-workstation signer (packaging/sign_release.py) sources its key from an
age-encrypted file. This signer is its CI counterpart: the key material is a base64
32-byte Ed25519 seed in the environment variable CHUDWARE_RELEASE_SIGNING_KEY
(GitHub Actions secret of the same name, key id release-ci-v1).

Domain separation, request validation, and the signing operation itself are
sign_release's — this module only sources the key, builds the artifact preimage
and prints base64.

  packaging/ci_sign.py sign payload PREPARED.payload-signing-request.bin
  packaging/ci_sign.py artifact --key-id release-ci-v1 --version 0.1.0-beta.42 \
      --platform linux --arch x86_64 --artifact dist/Chudware-....AppImage

The seed is never printed, never written to disk, and is zeroed after use.
"""
from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import os
import sys
from pathlib import Path
from typing import Any

try:
    from packaging.sign_release import (
        DOMAINS,
        SigningError,
        load_private_key,
        sign_request_with_key,
    )
except ImportError:  # direct execution from packaging/
    from sign_release import (  # type: ignore[no-redef]
        DOMAINS,
        SigningError,
        load_private_key,
        sign_request_with_key,
    )

KEY_ENV = "CHUDWARE_RELEASE_SIGNING_KEY"
_PLATFORMS = ("linux", "windows", "macos")
_ARCHES = ("x86_64", "arm64")


def load_signing_key() -> tuple[Any, bytearray]:
    """Return the CI private key plus the raw buffer to zero once signing is done."""
    value = os.environ.get(KEY_ENV)
    if not value or not value.strip():
        raise SigningError(f"{KEY_ENV} is not set; CI signing needs the release-ci-v1 seed")
    raw = bytearray(value.strip().encode("ascii", "strict"))
    if bytes(raw).startswith(b"-----BEGIN"):
        raise SigningError(
            f"{KEY_ENV} must be a canonical base64 32-byte Ed25519 seed, not PEM")
    return load_private_key(bytes(raw)), raw


def _expected_public_key(value: str) -> bytes:
    """Accept the key material as published: 64 hex characters or base64."""
    text = value.strip()
    try:
        if len(text) == 64 and all(ch in "0123456789abcdef" for ch in text.lower()):
            return bytes.fromhex(text)
        decoded = base64.b64decode(text, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise SigningError("expected public key is neither hex nor canonical base64") from exc
    if len(decoded) != 32:
        raise SigningError("expected public key must be 32 raw Ed25519 bytes")
    return decoded


def _assert_key_identity(key: Any, expected: str) -> None:
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    actual = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    if actual != _expected_public_key(expected):
        raise SigningError(
            f"{KEY_ENV} does not match the expected release public key; "
            "the CI secret and the shipped keyring disagree")


def _verify(key: Any, request: bytes, signature: str) -> None:
    from cryptography.exceptions import InvalidSignature

    try:
        key.public_key().verify(base64.b64decode(signature, validate=True), request)
    except (InvalidSignature, ValueError, binascii.Error) as exc:
        raise SigningError("produced signature does not verify against its own request") from exc


def artifact_preimage(key_id: str, version: str, platform: str, arch: str, sha256: str) -> bytes:
    """Build the exact artifact preimage every verifier recomputes.

    Identical shape to packaging/linux/build_appimage.sh, packaging/finalize_release.py,
    services/installer/build_release.py and host/src/update/updater.cpp.
    """
    return b"\0".join((
        b"chudware-artifact-v1", key_id.encode("ascii"), version.encode("ascii"),
        platform.encode("ascii"), arch.encode("ascii"), sha256.encode("ascii"),
    ))


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def artifact_request(key_id: str, version: str, platform: str, arch: str, artifact: Path) -> bytes:
    if not artifact.is_file():
        raise SigningError(f"artifact does not exist: {artifact}")
    return artifact_preimage(key_id, version, platform, arch, file_sha256(artifact))


def _write_exclusive(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="ascii") as stream:
        stream.write(text + "\n")


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--expect-public-key", metavar="KEY",
                    help="hex or base64 Ed25519 public key the CI seed must match")
    ap.add_argument("--output", type=Path,
                    help="also write the base64 signature to this new file")
    modes = ap.add_subparsers(dest="mode", required=True)

    sign = modes.add_parser("sign", help="sign an existing domain-separated request file")
    sign.add_argument("kind", choices=tuple(kind for kind in DOMAINS if kind != "platform-publishers"))
    sign.add_argument("request", type=Path)

    artifact = modes.add_parser("artifact", help="build and sign an artifact identity preimage")
    artifact.add_argument("--key-id", required=True)
    artifact.add_argument("--version", required=True)
    artifact.add_argument("--platform", choices=_PLATFORMS, required=True)
    artifact.add_argument("--arch", choices=_ARCHES, required=True)
    artifact.add_argument("--artifact", type=Path, required=True)
    artifact.add_argument("--request-out", type=Path,
                          help="also retain the exact preimage that was signed")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    key: Any = None
    key_bytes = bytearray()
    try:
        if args.output is not None and args.output.exists():
            raise SigningError(f"refusing to replace signature file: {args.output}")
        if args.mode == "sign":
            kind = args.kind
            request = args.request.read_bytes()
        else:
            kind = "artifact"
            request = artifact_request(args.key_id, args.version, args.platform,
                                       args.arch, args.artifact)
            if args.request_out is not None:
                args.request_out.parent.mkdir(parents=True, exist_ok=True)
                args.request_out.write_bytes(request)
        key, key_bytes = load_signing_key()
        if args.expect_public_key:
            _assert_key_identity(key, args.expect_public_key)
        signature = sign_request_with_key(kind, request, key)
        _verify(key, request, signature)
        if args.output is not None:
            _write_exclusive(args.output, signature)
    except (OSError, SigningError) as exc:
        print(f"ci_sign: error: {exc}", file=sys.stderr)
        return 2
    finally:
        for index in range(len(key_bytes)):
            key_bytes[index] = 0
    print(signature)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
