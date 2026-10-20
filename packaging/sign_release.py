#!/usr/bin/env python3
"""Offline owner-workstation release signer.

The release private key must be an age-encrypted file.  It is decrypted directly to
this process over stdout and is never written to disk or accepted through an
environment variable.  Requests are exact preimages emitted by trusted builders or
evidence generators; the signer only signs recognized, domain-separated requests.
"""
from __future__ import annotations

import argparse
import base64
import binascii
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

try:
    from packaging.release_json import canonical_json_bytes, parse_canonical_json_bytes
except ImportError:  # direct execution from packaging/
    from release_json import canonical_json_bytes, parse_canonical_json_bytes
try:
    from packaging.platform_publishers import (
        POLICY_DOMAIN, PublisherPolicyError, require_owner_signing_key, validate_policy_document,
    )
except ImportError:
    from platform_publishers import (
        POLICY_DOMAIN, PublisherPolicyError, require_owner_signing_key, validate_policy_document,
    )

DOMAINS = {
    "payload": b"chudware-payload-v1\0",
    "artifact": b"chudware-artifact-v1\0",
    "sbom": b"chudware-sbom-v1\0",
    "provenance": b"chudware-provenance-v1\0",
    "platform-test": b"chudware-platform-test-v1\0",
    "platform-publishers": POLICY_DOMAIN,
}

class SigningError(RuntimeError):
    pass


def decrypt_key(path: Path, identity: Path, *, age: str = "age") -> bytes:
    if not path.is_file() or path.suffix != ".age":
        raise SigningError("release key must be an existing age-encrypted .age file")
    if not identity.is_file():
        raise SigningError(f"age identity does not exist: {identity}")
    try:
        result = subprocess.run(
            [age, "--decrypt", "--identity", str(identity), str(path)],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        detail = getattr(exc, "stderr", b"").decode("utf-8", "replace").strip()
        raise SigningError(f"age could not decrypt release key{': ' + detail if detail else ''}") from exc
    return result.stdout


def load_private_key(value: bytes) -> Any:
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    except ImportError as exc:
        raise SigningError("cryptography is required for offline release signing") from exc
    raw = value.strip()
    if raw.startswith(b"-----BEGIN"):
        try:
            key = serialization.load_pem_private_key(raw, password=None)
        except (TypeError, ValueError) as exc:
            raise SigningError("decrypted release key is not valid unencrypted PEM") from exc
        if not isinstance(key, Ed25519PrivateKey):
            raise SigningError("decrypted release key is not Ed25519")
        return key
    try:
        seed = base64.b64decode(raw, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise SigningError("decrypted release key must be PEM or canonical base64 seed") from exc
    if len(seed) != 32 or base64.b64encode(seed) != raw:
        raise SigningError("decrypted release key must be a canonical base64 32-byte Ed25519 seed")
    return Ed25519PrivateKey.from_private_bytes(seed)


def validate_request(kind: str, request: bytes) -> None:
    domain = DOMAINS[kind]
    if not request.startswith(domain) or len(request) == len(domain):
        raise SigningError(f"request is not in the {kind!r} release-signing domain")
    if kind == "artifact":
        # Exactly the preimage every builder emits and every verifier recomputes:
        # key id, version, platform, arch, lowercase sha256 of the artifact bytes
        # (packaging/linux/build_appimage.sh, services/installer/build_release.py,
        # packaging/finalize_release.py, Webservice/gateway/releases.py,
        # host/src/update/updater.cpp). A short request here would sign an identity
        # nothing can verify.
        parts = request[len(domain):].split(b"\0")
        if len(parts) != 5 or not all(parts[:4]) or len(parts[4]) != 64:
            raise SigningError(
                "artifact request must be key id, version, platform, arch and lowercase SHA-256")
        try:
            digest = parts[4].decode("ascii")
            identity = b"\0".join(parts[:4]).decode("ascii")
        except UnicodeDecodeError as exc:
            raise SigningError("artifact request is not ASCII") from exc
        if any(ch not in "0123456789abcdef" for ch in digest):
            raise SigningError("artifact digest is not lowercase SHA-256")
        if any(ch in "\r\n" for ch in identity):
            raise SigningError("artifact request identity contains a line break")
    elif kind == "platform-publishers":
        try:
            validate_policy_document(request[len(domain):])
        except PublisherPolicyError as exc:
            raise SigningError(str(exc)) from exc
    elif kind in {"sbom", "provenance", "platform-test"}:
        body = request[len(domain):]
        try:
            parsed = parse_canonical_json_bytes(body, f"{kind} request body")
        except ValueError as exc:
            raise SigningError(str(exc)) from exc
        if body != canonical_json_bytes(parsed):
            raise SigningError(f"{kind} request body is not canonical JSON")


def sign_request_with_key(kind: str, request: bytes, key: Any) -> str:
    """Sign a validated request with an already-loaded Ed25519 private key.

    The one place a signature is produced. sign_request() sources the key from an
    age-encrypted file (owner workstation); packaging/ci_sign.py sources it from
    the CI secret. Neither reimplements validation or crypto.
    """
    validate_request(kind, request)
    if kind == "platform-publishers":
        try:
            require_owner_signing_key(key)
        except PublisherPolicyError as exc:
            raise SigningError(str(exc)) from exc
    return base64.b64encode(key.sign(request)).decode("ascii")


def sign_request(kind: str, request: bytes, encrypted_key: Path, identity: Path,
                 *, age: str = "age") -> str:
    # Validate BEFORE decrypting: a malformed request must never cause the release
    # key to be decrypted at all.
    validate_request(kind, request)
    key_bytes = bytearray(decrypt_key(encrypted_key, identity, age=age))
    try:
        return sign_request_with_key(kind, request, load_private_key(bytes(key_bytes)))
    finally:
        for index in range(len(key_bytes)):
            key_bytes[index] = 0


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("kind", choices=tuple(DOMAINS))
    ap.add_argument("request", type=Path, help="exact domain-separated request file")
    ap.add_argument("--encrypted-key", type=Path, required=True, help="age-encrypted .age release key")
    ap.add_argument("--identity", type=Path, required=True, help="age identity file")
    ap.add_argument("--output", type=Path, required=True, help="new detached signature file")
    ap.add_argument("--age-command", default="age", help=argparse.SUPPRESS)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.output.exists():
            raise SigningError(f"refusing to replace detached signature: {args.output}")
        request = args.request.read_bytes()
        signature = sign_request(args.kind, request, args.encrypted_key, args.identity,
                                 age=args.age_command)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="ascii") as stream:
            stream.write(signature + "\n")
    except (OSError, SigningError) as exc:
        print(f"sign_release: error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"kind": args.kind, "request": str(args.request), "signature": str(args.output)},
                     sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
