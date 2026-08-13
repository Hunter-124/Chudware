#!/usr/bin/env python3
"""Upload, download, and register canonical Chudware release artifacts.

Every signature is produced offline and supplied as a detached sidecar.  Runner and
publisher code verifies release trust-ring membership and exact digests but has no
private-key input or signing capability.
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

try:
    from .release_json import canonical_json_bytes, parse_canonical_json_bytes
except ImportError:
    from release_json import canonical_json_bytes, parse_canonical_json_bytes

BUCKET = "chudware-releases"
_PUBLISH_PATH = "/api/releases/latest/publish"
_ARTIFACT_SIGNATURE_DOMAIN = b"chudware-artifact-v1"
_PAYLOAD_MANIFEST_NAME = "payload-manifest.v1.json"
_PAYLOAD_SIGNATURE_NAME = "payload-manifest.v1.sig"
_PAYLOAD_SIGNATURE_DOMAIN = b"chudware-payload-v1\0"
_PAYLOAD_SCHEMA = "chudware-payload-v1"
_PAYLOAD_PRODUCT = "Chudware"
_RELEASE_PUBLIC_KEYS = {
    "release-v1": bytes.fromhex(
        "29acbae141bccaf0b22e1a94d34d0bc7361e526d0bfe12c89794bc9322966dd7"
    ),
}
_ARTIFACT_PATTERNS = (
    (re.compile(r"^Chudware-(?P<version>[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?)-linux-x86_64\.AppImage$"), "linux", "x86_64"),
    (re.compile(r"^Chudware-Setup-(?P<version>[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?)-x86_64\.exe$"), "windows", "x86_64"),
    (re.compile(r"^Chudware-(?P<version>[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?)-macos-(?P<arch>arm64|x86_64)\.zip$"), "macos", None),
)


class PublishError(RuntimeError):
    """Actionable release publication failure."""


_RELEASE_VERSION_RE = re.compile(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)")


def parse_release_version(value: str, field: str = "version") -> tuple[int, int, int]:
    match = _RELEASE_VERSION_RE.fullmatch(value)
    if match is None:
        raise PublishError(f"{field} must be canonical major.minor.patch")
    return tuple(int(component) for component in match.groups())


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




def require_release_key_id(key_id: str) -> bytes:
    public_key = _RELEASE_PUBLIC_KEYS.get(key_id)
    if public_key is None:
        raise PublishError(f"unknown release key id: {key_id!r}")
    return public_key


def artifact_signature_preimage(
    key_id: str, version: str, platform: str, arch: str, sha256: str
) -> bytes:
    require_release_key_id(key_id)
    parse_release_version(version)
    if platform not in {"linux", "windows", "macos"}:
        raise PublishError("artifact signature requires a canonical platform")
    if arch not in {"x86_64", "arm64"}:
        raise PublishError("artifact signature requires a canonical architecture")
    if not re.fullmatch(r"[0-9a-f]{64}", sha256):
        raise PublishError("artifact signature requires a lowercase SHA-256 digest")
    return b"\0".join((
        _ARTIFACT_SIGNATURE_DOMAIN, key_id.encode("ascii"), version.encode("ascii"),
        platform.encode("ascii"), arch.encode("ascii"), sha256.encode("ascii"),
    ))




def verify_artifact_signature(
    key_id: str, version: str, platform: str, arch: str, sha256: str, signature: bytes
) -> None:
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError as exc:
        raise PublishError(
            "cryptography is required for Ed25519 release verification; install it with "
            "`python -m pip install cryptography` on the release runner"
        ) from exc
    try:
        Ed25519PublicKey.from_public_bytes(require_release_key_id(key_id)).verify(
            signature, artifact_signature_preimage(key_id, version, platform, arch, sha256)
        )
    except InvalidSignature as exc:
        raise PublishError(
            f"artifact signature does not match release identity {key_id!r}/"
            f"{version}/{platform}/{arch} and SHA-256"
        ) from exc


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()

def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def canonical_payload_manifest(value: bytes) -> dict[str, Any]:
    try:
        manifest = json.loads(value.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise PublishError("payload manifest is not UTF-8 JSON") from exc
    try:
        from services.installer.payload_manifest import canonical_manifest_bytes
        canonical = canonical_manifest_bytes(manifest)
    except (ImportError, ValueError, TypeError) as exc:
        raise PublishError(f"payload manifest schema is invalid: {exc}") from exc
    if canonical != value:
        raise PublishError("payload manifest bytes are not canonical")
    return manifest


def verify_payload_sidecars(
    manifest_bytes: bytes,
    signature_text: str,
    *,
    key_id: str,
    version: str,
    platform: str,
    arch: str,
) -> tuple[dict[str, Any], bytes]:
    manifest = canonical_payload_manifest(manifest_bytes)
    expected = {
        "schema": _PAYLOAD_SCHEMA,
        "product": _PAYLOAD_PRODUCT,
        "version": version,
        "platform": platform,
        "arch": arch,
        "key_id": key_id,
    }
    mismatches = [name for name, value in expected.items() if manifest.get(name) != value]
    if mismatches:
        raise PublishError(
            "payload manifest identity mismatch for " + ", ".join(sorted(mismatches))
        )
    try:
        signature = base64.b64decode(signature_text, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise PublishError("payload signature is not canonical base64") from exc
    if len(signature) != 64 or base64.b64encode(signature).decode("ascii") != signature_text:
        raise PublishError("payload signature must encode one Ed25519 signature")
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError as exc:
        raise PublishError("cryptography is required for payload signature verification") from exc
    preimage = _PAYLOAD_SIGNATURE_DOMAIN + key_id.encode("ascii") + b"\0" + manifest_bytes
    try:
        Ed25519PublicKey.from_public_bytes(require_release_key_id(key_id)).verify(signature, preimage)
    except InvalidSignature as exc:
        raise PublishError(f"payload signature does not match release key id {key_id!r}") from exc
    return manifest, signature


def upload(args: argparse.Namespace) -> dict[str, Any]:
    artifact = args.artifact.resolve()
    artifact_signature_path = args.artifact_signature.resolve()
    payload_manifest_path = args.payload_manifest.resolve()
    payload_signature_path = args.payload_signature.resolve()
    if not artifact.is_file():
        raise PublishError(f"artifact does not exist: {artifact}")
    if not artifact_signature_path.is_file():
        raise PublishError(f"artifact signature does not exist: {artifact_signature_path}")
    if not payload_manifest_path.is_file():
        raise PublishError(f"payload manifest does not exist: {payload_manifest_path}")
    if not payload_signature_path.is_file():
        raise PublishError(f"payload signature does not exist: {payload_signature_path}")
    version, platform, arch = artifact_identity(artifact.name)
    digest = sha256_file(artifact)
    payload_manifest_bytes = payload_manifest_path.read_bytes()
    try:
        artifact_signature_text = artifact_signature_path.read_text(encoding="ascii").strip()
        payload_signature_text = payload_signature_path.read_text(encoding="ascii").strip()
    except UnicodeError as exc:
        raise PublishError("release signatures must be ASCII") from exc
    try:
        signature_bytes = base64.b64decode(artifact_signature_text, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise PublishError("artifact signature is not canonical base64") from exc
    if len(signature_bytes) != 64 or base64.b64encode(signature_bytes).decode("ascii") != artifact_signature_text:
        raise PublishError("artifact signature must encode one Ed25519 signature")
    verify_artifact_signature(args.key_id, version, platform, arch, digest, signature_bytes)
    verify_payload_sidecars(
        payload_manifest_bytes,
        payload_signature_text,
        key_id=args.key_id,
        version=version,
        platform=platform,
        arch=arch,
    )
    evidence_uploads: list[tuple[str, Path, Path]] = []
    for prefix, filename, domain in _EVIDENCE:
        body_path = getattr(args, prefix).resolve()
        signature_path = getattr(args, prefix + "_signature").resolve()
        if not body_path.is_file() or not signature_path.is_file():
            raise PublishError(f"{prefix} evidence and detached signature are required")
        body = body_path.read_bytes()
        _document, canonical = _canonical_json_bytes(body, prefix)
        _verify_evidence_signature(args.key_id, domain, canonical, signature_path.read_bytes(), prefix)
        evidence_uploads.append((filename, body_path, signature_path))
    signature = artifact_signature_text
    sha_path = artifact.with_name(artifact.name + ".sha256")
    sha_path.write_text(f"{digest}  {artifact.name}\n", encoding="ascii")
    sig_path = artifact_signature_path
    key = f"{args.channel}/{version}/{artifact.name}"
    client = r2_client()
    try:
        client.upload_file(str(artifact), BUCKET, key)
        client.upload_file(str(sha_path), BUCKET, key + ".sha256", ExtraArgs={"ContentType": "text/plain"})
        client.upload_file(str(sig_path), BUCKET, key + ".sig", ExtraArgs={"ContentType": "text/plain"})
        client.upload_file(
            str(payload_manifest_path), BUCKET, key + ".payload-manifest.v1.json",
            ExtraArgs={"ContentType": "application/json"},
        )
        client.upload_file(
            str(payload_signature_path), BUCKET, key + ".payload-manifest.v1.sig",
            ExtraArgs={"ContentType": "text/plain"},
        )
        for filename, body_path, signature_path in evidence_uploads:
            evidence_key = key + "." + filename
            client.upload_file(str(body_path), BUCKET, evidence_key, ExtraArgs={"ContentType": "application/json"})
            client.upload_file(str(signature_path), BUCKET, evidence_key + ".sig", ExtraArgs={"ContentType": "text/plain"})
    except Exception as exc:
        raise describe_r2_error("upload", exc) from exc

    result = {
        "channel": args.channel,
        "key_id": args.key_id,
        "version": version,
        "platform": platform,
        "arch": arch,
        "sha256": digest,
        "size": artifact.stat().st_size,
        "signature": signature,
        "artifact_key": key,
        "artifact_name": artifact.name,
        "payload_manifest_key": key + ".payload-manifest.v1.json",
        "payload_manifest_sha256": sha256_bytes(payload_manifest_bytes),
        "payload_signature_key": key + ".payload-manifest.v1.sig",
        "payload_signature_sha256": sha256_bytes(payload_signature_path.read_bytes()),
    }
    for prefix, filename, _domain in _EVIDENCE:
        body_path = getattr(args, prefix).resolve()
        signature_path = getattr(args, prefix + "_signature").resolve()
        result.update({
            f"{prefix}_key": key + "." + filename,
            f"{prefix}_sha256": sha256_file(body_path),
            f"{prefix}_signature_key": key + "." + filename + ".sig",
            f"{prefix}_signature_sha256": sha256_file(signature_path),
        })
    if args.manifest_out:
        args.manifest_out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return result


_EVIDENCE = (
    ("sbom", "sbom.cdx.json", b"chudware-sbom-v1\0"),
    ("provenance", "provenance.slsa.json", b"chudware-provenance-v1\0"),
    ("platform_test", "platform-test.v1.json", b"chudware-platform-test-v1\0"),
)

def _canonical_json_bytes(raw: bytes, label: str) -> tuple[dict[str, Any], bytes]:
    value = parse_canonical_json_bytes(raw, label, PublishError)
    return value, canonical_json_bytes(value)


def _verify_evidence_signature(key_id: str, domain: bytes, body: bytes, signature_body: bytes, label: str) -> None:
    try:
        signature_text = signature_body.decode("ascii").strip()
        signature = base64.b64decode(signature_text, validate=True)
    except (UnicodeError, ValueError, binascii.Error) as exc:
        raise PublishError(f"{label} signature is not canonical base64") from exc
    if len(signature) != 64 or base64.b64encode(signature).decode() != signature_text:
        raise PublishError(f"{label} signature must encode one Ed25519 signature")
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        Ed25519PublicKey.from_public_bytes(require_release_key_id(key_id)).verify(signature, domain + body)
    except InvalidSignature as exc:
        raise PublishError(f"{label} signature does not match release key id {key_id!r}") from exc


def remote_metadata(args: argparse.Namespace) -> dict[str, Any]:
    parse_release_version(args.version)
    parse_release_version(args.min_supported, "min_supported")
    require_release_key_id(args.key_id)
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
        payload_manifest_key = key + ".payload-manifest.v1.json"
        payload_signature_key = key + ".payload-manifest.v1.sig"
        payload_manifest_bytes = client.get_object(Bucket=BUCKET, Key=payload_manifest_key)["Body"].read()
        payload_signature_bytes = client.get_object(Bucket=BUCKET, Key=payload_signature_key)["Body"].read()
        evidence_bodies = {}
        for prefix, filename, _domain in _EVIDENCE:
            evidence_key = key + "." + filename
            evidence_bodies[prefix] = (
                evidence_key,
                client.get_object(Bucket=BUCKET, Key=evidence_key)["Body"].read(),
                client.get_object(Bucket=BUCKET, Key=evidence_key + ".sig")["Body"].read(),
            )
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
    try:
        payload_signature_text = payload_signature_bytes.decode("ascii").strip()
    except UnicodeError as exc:
        raise PublishError(f"R2 sidecar {payload_signature_key} is not ASCII") from exc
    verify_payload_sidecars(
        payload_manifest_bytes,
        payload_signature_text,
        key_id=args.key_id,
        version=args.version,
        platform=args.platform,
        arch=args.arch,
    )
    verify_artifact_signature(
        args.key_id, args.version, args.platform, args.arch, sha256, decoded_signature
    )
    evidence_metadata: dict[str, Any] = {"commit_sha": args.commit}
    for prefix, _filename, domain in _EVIDENCE:
        evidence_key, body, signature_body = evidence_bodies[prefix]
        document, canonical = _canonical_json_bytes(body, prefix)
        _verify_evidence_signature(args.key_id, domain, canonical, signature_body, prefix)
        if prefix == "sbom":
            component = document.get("metadata", {}).get("component", {})
            if document.get("bomFormat") != "CycloneDX" or component.get("version") != args.version:
                raise PublishError("SBOM version or CycloneDX identity mismatch")
        elif prefix == "provenance":
            subject = document.get("subject")
            external = document.get("predicate", {}).get("buildDefinition", {}).get("externalParameters", {})
            expected = {"version": args.version, "platform": args.platform, "arch": args.arch, "commit": args.commit}
            if not isinstance(subject, list) or not any(item.get("digest", {}).get("sha256") == sha256 for item in subject) or external != expected:
                raise PublishError("provenance artifact or build identity mismatch")
        else:
            expected = {"schema": "platform-test.v1", "version": args.version, "platform": args.platform, "arch": args.arch, "commit": args.commit, "artifact_sha256": sha256}
            if set(document) != set(expected) | {"commands", "passed"} or any(document.get(k) != v for k, v in expected.items()):
                raise PublishError("platform-test identity or schema mismatch")
            if document.get("passed") is not True or not isinstance(document.get("commands"), list) or not document["commands"] or not all(isinstance(command, str) and command.strip() for command in document["commands"]):
                raise PublishError("platform-test commands must be nonempty and passed must be true")
        evidence_metadata.update({
            f"{prefix}_key": evidence_key,
            f"{prefix}_sha256": sha256_bytes(body),
            f"{prefix}_signature_key": evidence_key + ".sig",
            f"{prefix}_signature_sha256": sha256_bytes(signature_body),
        })
    return {
        "key_id": args.key_id,
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
        "payload_manifest_key": payload_manifest_key,
        "payload_manifest_sha256": sha256_bytes(payload_manifest_bytes),
        "payload_signature_key": payload_signature_key,
        "payload_signature_sha256": sha256_bytes(payload_signature_bytes),
        **evidence_metadata,
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
    parsed_version, parsed_platform, parsed_arch = artifact_identity(args.artifact_name)
    if (parsed_version, parsed_platform, parsed_arch) != (args.version, args.platform, args.arch):
        raise PublishError(
            "download identity disagrees with canonical artifact name: "
            f"name says {(parsed_version, parsed_platform, parsed_arch)}, arguments say "
            f"{(args.version, args.platform, args.arch)}"
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
    signature_text = (args.out / (args.artifact_name + ".sig")).read_text(encoding="ascii").strip()
    try:
        signature = base64.b64decode(signature_text, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise PublishError(f"downloaded sidecar {args.artifact_name}.sig is not standard base64") from exc
    if len(signature) != 64:
        raise PublishError(f"downloaded sidecar {args.artifact_name}.sig is not a 64-byte Ed25519 signature")
    verify_artifact_signature(
        args.key_id, args.version, args.platform, args.arch, actual, signature
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

    upload_parser = commands.add_parser("upload", help="verify and upload one artifact and detached sidecars")
    upload_parser.add_argument("artifact", type=Path)
    upload_parser.add_argument("--artifact-signature", type=Path, required=True)
    upload_parser.add_argument("--payload-manifest", type=Path, required=True)
    upload_parser.add_argument("--payload-signature", type=Path, required=True)
    upload_parser.add_argument("--key-id", default="release-v1")
    upload_parser.add_argument("--channel", choices=("stable", "beta"), required=True)
    upload_parser.add_argument("--manifest-out", type=Path)
    upload_parser.add_argument("--sbom", type=Path, required=True)
    upload_parser.add_argument("--sbom-signature", type=Path, required=True)
    upload_parser.add_argument("--provenance", type=Path, required=True)
    upload_parser.add_argument("--provenance-signature", type=Path, required=True)
    upload_parser.add_argument("--platform-test", dest="platform_test", type=Path, required=True)
    upload_parser.add_argument("--platform-test-signature", dest="platform_test_signature", type=Path, required=True)
    upload_parser.set_defaults(func=upload)

    register_parser = commands.add_parser("register", help="register an already-uploaded artifact")
    add_identity_args(register_parser)
    register_parser.add_argument("--key-id", default="release-v1")
    register_parser.add_argument("--min-supported", required=True)
    register_parser.add_argument("--notes-url", required=True)
    register_parser.add_argument("--commit", required=True)
    register_parser.set_defaults(func=register)

    download_parser = commands.add_parser("download", help="download an artifact and both sidecars")
    download_parser.add_argument("--channel", choices=("stable", "beta"), required=True)
    download_parser.add_argument("--version", required=True)
    download_parser.add_argument("--artifact-name", required=True)
    download_parser.add_argument("--key-id", default="release-v1")
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
