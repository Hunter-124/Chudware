#!/usr/bin/env python3
"""Write the signed update manifest the in-app updater polls on GitHub Releases.

The updater (host/src/update/updater.cpp) fetches

    https://github.com/<repo>/releases/latest/download/update-<platform>-<arch>.json

(override: CHUDWARE_UPDATE_MANIFEST_URL), requires every field this tool writes,
downloads ``url`` (GitHub redirects it to its asset CDN) and verifies size,
SHA-256 and the Ed25519 signature over

    chudware-artifact-v1 \\0 key_id \\0 version \\0 platform \\0 arch \\0 sha256

against the public keys in core/security/release_keys.h. ``notes_url`` is not
covered by the signature.

The signing seed is read from CHUDWARE_RELEASE_SIGNING_KEY, the same canonical
base64 32-byte Ed25519 seed packaging/ci_sign.py consumes. The produced signature
is verified against the in-repo public key for ``--key-id`` before anything is
written, so a secret that does not match the shipped keyring fails here instead
of in every installed client.

  python packaging/github_release_manifest.py dist/Chudware-0.2.0-linux-x86_64.AppImage \\
      --platform linux --arch x86_64 --version 0.2.0 --tag v0.2.0
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import urllib.parse
from pathlib import Path
from typing import Any

try:
    from packaging.artifact_names import canonical_artifact, split_version
    from packaging.ci_sign import artifact_preimage, file_sha256, load_signing_key
    from packaging.sign_release import SigningError, sign_request_with_key
except ImportError:  # direct execution from packaging/
    from artifact_names import canonical_artifact, split_version  # type: ignore[no-redef]
    from ci_sign import artifact_preimage, file_sha256, load_signing_key  # type: ignore[no-redef]
    from sign_release import SigningError, sign_request_with_key  # type: ignore[no-redef]

DEFAULT_REPO = "Hunter-124/Chudware"
DEFAULT_KEY_ID = "release-ci-v1"
PLATFORMS = ("linux", "windows", "macos")
ARCHES = ("x86_64", "arm64")
# Release trust ring; must equal the packaged (non-developer) keys in
# core/security/release_keys.h. test_github_release_manifest.py enforces that.
RELEASE_PUBLIC_KEYS = {
    "release-v1": bytes.fromhex("29acbae141bccaf0b22e1a94d34d0bc7361e526d0bfe12c89794bc9322966dd7"),
    "release-ci-v1": bytes.fromhex("d7434dc67f4d6e11cd4c8ced9b5ebbccf2e1477b5949d52d7685b6ca43a010f6"),
}
_REPO_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_TAG_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


def manifest_name(platform: str, arch: str) -> str:
    return f"update-{platform}-{arch}.json"


def _validate(args: argparse.Namespace) -> None:
    try:
        split_version(args.version)
    except ValueError as exc:
        raise SigningError(str(exc)) from exc
    if not _REPO_RE.fullmatch(args.repo):
        raise SigningError("--repo must be OWNER/REPO")
    if not _TAG_RE.fullmatch(args.tag):
        raise SigningError("--tag must be a plain release tag")
    if args.key_id not in RELEASE_PUBLIC_KEYS:
        raise SigningError(f"unknown release key id: {args.key_id!r}")
    try:
        expected = canonical_artifact(args.version, args.platform, args.arch)
    except ValueError as exc:
        raise SigningError(str(exc)) from exc
    if args.artifact.name != expected:
        raise SigningError(
            f"artifact must use the updater's canonical name {expected!r}, not {args.artifact.name!r}")
    if not args.artifact.is_file():
        raise SigningError(f"artifact does not exist: {args.artifact}")


def _verify(key_id: str, preimage: bytes, signature: str) -> None:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    try:
        Ed25519PublicKey.from_public_bytes(RELEASE_PUBLIC_KEYS[key_id]).verify(
            base64.b64decode(signature, validate=True), preimage)
    except (InvalidSignature, ValueError) as exc:
        raise SigningError(
            f"signature does not verify against the shipped {key_id} public key; "
            "the signing secret and core/security/release_keys.h disagree") from exc


def build_manifest(args: argparse.Namespace) -> dict[str, Any]:
    _validate(args)
    size = args.artifact.stat().st_size
    sha256 = file_sha256(args.artifact)
    if args.artifact.stat().st_size != size or size == 0:
        raise SigningError("artifact changed or is empty while hashing")
    preimage = artifact_preimage(args.key_id, args.version, args.platform, args.arch, sha256)
    key, key_bytes = load_signing_key()
    try:
        signature = sign_request_with_key("artifact", preimage, key)
    finally:
        for index in range(len(key_bytes)):
            key_bytes[index] = 0
    _verify(args.key_id, preimage, signature)
    tag = urllib.parse.quote(args.tag, safe="")
    return {
        "version": args.version,
        "platform": args.platform,
        "arch": args.arch,
        "key_id": args.key_id,
        "sha256": sha256,
        "size": size,
        "signature": signature,
        "notes_url": args.notes_url or f"https://github.com/{args.repo}/releases/tag/{tag}",
        "url": f"https://github.com/{args.repo}/releases/download/{tag}/"
               f"{urllib.parse.quote(args.artifact.name, safe='')}",
    }


def write_manifest(manifest: dict[str, Any], output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / manifest_name(manifest["platform"], manifest["arch"])
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return path


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("artifact", type=Path, help="the release asset the updater installs")
    ap.add_argument("--platform", choices=PLATFORMS, required=True)
    ap.add_argument("--arch", choices=ARCHES, required=True)
    ap.add_argument("--version", required=True)
    ap.add_argument("--tag", required=True, help="GitHub release tag the asset is attached to")
    ap.add_argument("--repo", default=DEFAULT_REPO, help=f"OWNER/REPO (default {DEFAULT_REPO})")
    ap.add_argument("--key-id", default=DEFAULT_KEY_ID, help=f"release key id (default {DEFAULT_KEY_ID})")
    ap.add_argument("--notes-url", help="release notes URL (default: the release page)")
    ap.add_argument("--output-dir", type=Path, help="default: the artifact's directory")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        manifest = build_manifest(args)
        path = write_manifest(manifest, args.output_dir or args.artifact.parent)
    except (OSError, SigningError) as exc:
        print(f"github_release_manifest: error: {exc}", file=sys.stderr)
        return 2
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
