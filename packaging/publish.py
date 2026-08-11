#!/usr/bin/env python3
"""Sign, upload, download, and register canonical Chudware release artifacts.

The upload and registration operations are deliberately separate: CI uploads package bytes
from each platform runner, but does not make a release visible to clients until every build
and parity gate has passed.
"""
from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

BUCKET = "chudware-releases"
_PUBLISH_PATH = "/api/releases/latest/publish"
_ARTIFACT_PATTERNS = (
    (re.compile(r"^Chudware-(?P<version>[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?)-linux-x86_64\.AppImage$"), "linux", "x86_64"),
    (re.compile(r"^Chudware-Setup-(?P<version>[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?)-x86_64\.exe$"), "windows", "x86_64"),
    (re.compile(r"^Chudware-(?P<version>[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?)-macos-(?P<arch>arm64|x86_64)\.zip$"), "macos", None),
)


class PublishError(RuntimeError):
    """Actionable release publication failure."""


def artifact_identity(name: str) -> tuple[str, str, str]:
    for pattern, platform, fixed_arch in _ARTIFACT_PATTERNS:
        match = pattern.fullmatch(name)
        if match:
            return match.group("version"), platform, fixed_arch or match.group("arch")
    raise PublishError(
        f"non-canonical artifact name {name!r}; expected one of "
        "Chudware-<version>-linux-x86_64.AppImage, "
        "Chudware-Setup-<version>-x86_64.exe, or "
        "Chudware-<version>-macos-<arm64|x86_64>.zip"
    )


def require_env(*names: str) -> dict[str, str]:
    missing = [name for name in names if not os.environ.get(name)]
    if missing:
        raise PublishError(
            "release publishing is not configured; set GitHub Actions secrets/environment "
            + ", ".join(missing)
            + ". R2 must first be enabled for the Cloudflare account and the token must have "
              "Object Read & Write access to the chudware-releases bucket."
        )
    return {name: os.environ[name] for name in names}


def r2_client() -> Any:
    env = require_env("R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY")
    try:
        import boto3  # type: ignore[import-not-found]
    except ImportError as exc:
        raise PublishError(
            "boto3 is required for R2 publication; install it with "
            "`python -m pip install boto3` on the release runner"
        ) from exc
    return boto3.client(
        "s3",
        endpoint_url=env["R2_ENDPOINT"],
        aws_access_key_id=env["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=env["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
    )


def describe_r2_error(action: str, exc: Exception) -> PublishError:
    return PublishError(
        f"R2 {action} failed: {exc}. If Cloudflare reports error 10042, enable R2 for the "
        "account, create the chudware-releases bucket, and provision an Object Read & Write "
        "R2 API token; then set R2_ENDPOINT, R2_ACCESS_KEY_ID, and R2_SECRET_ACCESS_KEY."
    )


def load_signing_key() -> Any:
    value = require_env("CHUDWARE_RELEASE_SIGNING_KEY")["CHUDWARE_RELEASE_SIGNING_KEY"].strip()
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    except ImportError as exc:
        raise PublishError(
            "cryptography is required for Ed25519 release signing; install it with "
            "`python -m pip install cryptography` on the release runner"
        ) from exc

    raw: bytes
    if value.startswith("-----BEGIN"):
        raw = value.encode("ascii")
    else:
        try:
            raw = base64.b64decode("".join(value.split()), validate=True)
        except (ValueError, binascii.Error) as exc:
            raise PublishError(
                "CHUDWARE_RELEASE_SIGNING_KEY must be a PEM Ed25519 private key, base64-encoded "
                "PEM, or the base64 encoding of a 32-byte Ed25519 seed"
            ) from exc

    if raw.startswith(b"-----BEGIN"):
        try:
            key = serialization.load_pem_private_key(raw, password=None)
        except (TypeError, ValueError) as exc:
            raise PublishError("CHUDWARE_RELEASE_SIGNING_KEY contains an invalid PEM private key") from exc
        if not isinstance(key, Ed25519PrivateKey):
            raise PublishError("CHUDWARE_RELEASE_SIGNING_KEY is not an Ed25519 private key")
        return key
    if len(raw) != 32:
        raise PublishError(
            "raw CHUDWARE_RELEASE_SIGNING_KEY must decode to exactly 32 bytes (an Ed25519 seed)"
        )
    return Ed25519PrivateKey.from_private_bytes(raw)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def upload(args: argparse.Namespace) -> dict[str, Any]:
    artifact = args.artifact.resolve()
    if not artifact.is_file():
        raise PublishError(f"artifact does not exist: {artifact}")
    version, platform, arch = artifact_identity(artifact.name)
    digest = sha256_file(artifact)
    signature = base64.b64encode(load_signing_key().sign(digest.encode("ascii"))).decode("ascii")
    sha_path = artifact.with_name(artifact.name + ".sha256")
    sig_path = artifact.with_name(artifact.name + ".sig")
    sha_path.write_text(f"{digest}  {artifact.name}\n", encoding="ascii")
    sig_path.write_text(signature + "\n", encoding="ascii")

    key = f"{args.channel}/{version}/{artifact.name}"
    client = r2_client()
    try:
        client.upload_file(str(artifact), BUCKET, key)
        client.upload_file(str(sha_path), BUCKET, key + ".sha256", ExtraArgs={"ContentType": "text/plain"})
        client.upload_file(str(sig_path), BUCKET, key + ".sig", ExtraArgs={"ContentType": "text/plain"})
    except Exception as exc:
        raise describe_r2_error("upload", exc) from exc

    result = {
        "channel": args.channel,
        "version": version,
        "platform": platform,
        "arch": arch,
        "sha256": digest,
        "size": artifact.stat().st_size,
        "signature": signature,
        "artifact_key": key,
        "artifact_name": artifact.name,
    }
    if args.manifest_out:
        args.manifest_out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return result


def remote_metadata(args: argparse.Namespace) -> dict[str, Any]:
    parsed_version, parsed_platform, parsed_arch = artifact_identity(args.artifact_name)
    if (parsed_version, parsed_platform, parsed_arch) != (args.version, args.platform, args.arch):
        raise PublishError(
            "registration arguments disagree with canonical artifact name: "
            f"name says {(parsed_version, parsed_platform, parsed_arch)}, arguments say "
            f"{(args.version, args.platform, args.arch)}"
        )
    key = f"{args.channel}/{args.version}/{args.artifact_name}"
    client = r2_client()
    try:
        head = client.head_object(Bucket=BUCKET, Key=key)
        sha_body = client.get_object(Bucket=BUCKET, Key=key + ".sha256")["Body"].read().decode("ascii")
        sig_body = client.get_object(Bucket=BUCKET, Key=key + ".sig")["Body"].read().decode("ascii")
    except Exception as exc:
        raise describe_r2_error("metadata read", exc) from exc
    sha256 = sha_body.split()[0] if sha_body.split() else ""
    signature = sig_body.strip()
    if not re.fullmatch(r"[0-9a-f]{64}", sha256):
        raise PublishError(f"R2 sidecar {key}.sha256 does not contain a lowercase SHA-256")
    try:
        decoded_signature = base64.b64decode(signature, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise PublishError(f"R2 sidecar {key}.sig is not standard base64") from exc
    if len(decoded_signature) != 64:
        raise PublishError(f"R2 sidecar {key}.sig is not a 64-byte Ed25519 signature")
    return {
        "channel": args.channel,
        "version": args.version,
        "min_supported": args.min_supported,
        "platform": args.platform,
        "arch": args.arch,
        "sha256": sha256,
        "size": int(head["ContentLength"]),
        "signature": signature,
        "notes_url": args.notes_url,
        "artifact_key": key,
    }


def register(args: argparse.Namespace) -> dict[str, Any]:
    manifest = remote_metadata(args)
    env = require_env("CHUDWARE_GATEWAY_URL", "CHUDWARE_GATEWAY_RELEASE_TOKEN")
    url = env["CHUDWARE_GATEWAY_URL"].rstrip("/") + _PUBLISH_PATH
    request = urllib.request.Request(
        url,
        data=json.dumps(manifest).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {env['CHUDWARE_GATEWAY_RELEASE_TOKEN']}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read()
            if response.status < 200 or response.status >= 300:
                raise PublishError(f"gateway returned HTTP {response.status}: {body.decode('utf-8', 'replace')}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise PublishError(f"gateway registration failed with HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise PublishError(f"gateway registration failed for {url}: {exc.reason}") from exc
    return manifest


def download(args: argparse.Namespace) -> dict[str, Any]:
    parsed_version, _, _ = artifact_identity(args.artifact_name)
    if parsed_version != args.version:
        raise PublishError(
            f"download version {args.version!r} disagrees with artifact name version {parsed_version!r}"
        )
    key = f"{args.channel}/{args.version}/{args.artifact_name}"
    args.out.mkdir(parents=True, exist_ok=True)
    client = r2_client()
    downloaded = []
    try:
        for suffix in ("", ".sha256", ".sig"):
            destination = args.out / (args.artifact_name + suffix)
            client.download_file(BUCKET, key + suffix, str(destination))
            downloaded.append(str(destination))
    except Exception as exc:
        raise describe_r2_error("download", exc) from exc
    artifact = args.out / args.artifact_name
    sha_path = args.out / (args.artifact_name + ".sha256")
    sha_parts = sha_path.read_text(encoding="ascii").split()
    expected = sha_parts[0] if sha_parts else ""
    actual = sha256_file(artifact)
    if not re.fullmatch(r"[0-9a-f]{64}", expected) or actual != expected:
        raise PublishError(
            f"downloaded artifact checksum mismatch for {args.artifact_name}: "
            f"expected {expected!r}, got {actual}"
        )
    return {"artifact_key": key, "downloaded": downloaded}


def add_identity_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--channel", choices=("stable", "beta"), required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--platform", choices=("linux", "windows", "macos"), required=True)
    parser.add_argument("--arch", choices=("x86_64", "arm64"), required=True)
    parser.add_argument("--artifact-name", required=True)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    commands = root.add_subparsers(dest="command", required=True)

    upload_parser = commands.add_parser("upload", help="sign and upload one artifact and sidecars")
    upload_parser.add_argument("artifact", type=Path)
    upload_parser.add_argument("--channel", choices=("stable", "beta"), required=True)
    upload_parser.add_argument("--manifest-out", type=Path)
    upload_parser.set_defaults(func=upload)

    register_parser = commands.add_parser("register", help="register an already-uploaded artifact")
    add_identity_args(register_parser)
    register_parser.add_argument("--min-supported", required=True)
    register_parser.add_argument("--notes-url", required=True)
    register_parser.set_defaults(func=register)

    download_parser = commands.add_parser("download", help="download an artifact and both sidecars")
    download_parser.add_argument("--channel", choices=("stable", "beta"), required=True)
    download_parser.add_argument("--version", required=True)
    download_parser.add_argument("--artifact-name", required=True)
    download_parser.add_argument("--out", type=Path, required=True)
    download_parser.set_defaults(func=download)
    return root


def main(argv: list[str] | None = None) -> int:
    try:
        args = parser().parse_args(argv)
        result = args.func(args)
    except (PublishError, OSError) as exc:
        print(f"publish: error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
