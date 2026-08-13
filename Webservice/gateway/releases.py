"""Release metadata, signed download manifests, and manual admin uploads.

The customer manifest is deliberately public except for its short-lived R2 URL.
Release publication is authenticated independently with a deployment token so CI
does not need (and cannot accidentally acquire) an administrator browser session.
"""

from __future__ import annotations

import base64
import binascii
import hmac
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Literal
from urllib.parse import urlparse

import boto3
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi import APIRouter, File, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

try:
    from . import accounts, entitlement
    from .auth import get_current_user
    from .db import tx
    from .limits import RateLimiter
except ImportError:  # uvicorn app:app from package dir
    import accounts  # type: ignore
    import entitlement  # type: ignore
    from auth import get_current_user  # type: ignore
    from db import tx  # type: ignore
    from limits import RateLimiter  # type: ignore

router = APIRouter(tags=["releases"])

Channel = Literal["stable", "beta"]
Platform = Literal["linux", "windows", "macos"]
Architecture = Literal["x86_64", "arm64"]

_SEMVER_RE = re.compile(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)")


def _parse_release_version(value: str, field: str) -> tuple[int, int, int]:
    match = _SEMVER_RE.fullmatch(value)
    if match is None:
        raise HTTPException(422, f"{field} must be canonical major.minor.patch")
    return tuple(int(component) for component in match.groups())
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_ARTIFACT_SIGNATURE_DOMAIN = b"chudware-artifact-v1"
_RELEASE_PUBLIC_KEYS = {
    "release-v1": bytes.fromhex(
        "29acbae141bccaf0b22e1a94d34d0bc7361e526d0bfe12c89794bc9322966dd7"
    ),
}
_SAFE_NAME_RE = re.compile(r"[A-Za-z0-9._-]{1,128}")
_manifest_by_ip = RateLimiter(120, 60.0)
_publish_by_ip = RateLimiter(20, 60.0)


class PublishRelease(BaseModel):
    channel: Channel
    key_id: str
    version: str
    min_supported: str
    platform: Platform
    arch: Architecture
    sha256: str
    size: int
    signature: str
    notes_url: str
    artifact_key: str
    payload_manifest_key: str
    payload_manifest_sha256: str
    payload_signature_key: str
    payload_signature_sha256: str
    commit_sha: str
    sbom_key: str
    sbom_sha256: str
    sbom_signature_key: str
    sbom_signature_sha256: str
    provenance_key: str
    provenance_sha256: str
    provenance_signature_key: str
    provenance_signature_sha256: str
    platform_test_key: str
    platform_test_sha256: str
    platform_test_signature_key: str
    platform_test_signature_sha256: str


def _require_admin(request: Request):
    user = get_current_user(request)
    if getattr(user, "role", None) != "admin":
        raise HTTPException(status_code=403, detail="admin only")
    return user


def _releases_dir() -> Path:
    env = (os.environ.get("CHUDWARE_RELEASES_DIR") or "").strip()
    if env:
        return Path(env).expanduser()
    return Path.home() / "chudware-releases"


def _safe_name(name: str) -> str:
    """Release basename: strict charset, no dotfiles, no separators."""
    if not name or name.startswith(".") or not _SAFE_NAME_RE.fullmatch(name):
        raise HTTPException(status_code=400, detail="invalid release name")
    return name


def _canonical_artifact(version: str, platform: str, arch: str) -> str:
    if platform == "linux" and arch == "x86_64":
        return f"Chudware-{version}-linux-x86_64.AppImage"
    if platform == "windows" and arch == "x86_64":
        return f"Chudware-Setup-{version}-x86_64.exe"
    if platform == "macos":
        return f"Chudware-{version}-macos-{arch}.zip"
    raise HTTPException(422, "unsupported platform and architecture combination")



def _artifact_signature_preimage(
    key_id: str, version: str, platform: str, arch: str, sha256: str
) -> bytes:
    return b"\0".join((
        _ARTIFACT_SIGNATURE_DOMAIN, key_id.encode("ascii"), version.encode("ascii"),
        platform.encode("ascii"), arch.encode("ascii"), sha256.encode("ascii"),
    ))


def _verify_artifact_signature(
    key_id: str, version: str, platform: str, arch: str, sha256: str, signature: bytes
) -> None:
    public_key = _RELEASE_PUBLIC_KEYS.get(key_id)
    if public_key is None:
        raise HTTPException(422, "unknown release key_id")
    try:
        Ed25519PublicKey.from_public_bytes(public_key).verify(
            signature, _artifact_signature_preimage(
                key_id, version, platform, arch, sha256
            )
        )
    except InvalidSignature as exc:
        raise HTTPException(422, "signature does not match release identity and sha256") from exc

def _validate_payload_metadata(value: PublishRelease) -> None:
    for field in ("payload_manifest_sha256", "payload_signature_sha256"):
        if not _SHA256_RE.fullmatch(getattr(value, field)):
            raise HTTPException(422, f"{field} must be 64 lowercase hexadecimal characters")
    expected_manifest_key = value.artifact_key + ".payload-manifest.v1.json"
    expected_signature_key = value.artifact_key + ".payload-manifest.v1.sig"
    if value.payload_manifest_key != expected_manifest_key:
        raise HTTPException(422, f"payload_manifest_key must be {expected_manifest_key}")
    if value.payload_signature_key != expected_signature_key:
        raise HTTPException(422, f"payload_signature_key must be {expected_signature_key}")

def _validate_evidence_metadata(value: PublishRelease) -> None:
    if not re.fullmatch(r"[0-9a-f]{40,64}", value.commit_sha):
        raise HTTPException(422, "commit_sha must be a full lowercase commit id")
    evidence = (
        ("sbom", "sbom.cdx.json"),
        ("provenance", "provenance.slsa.json"),
        ("platform_test", "platform-test.v1.json"),
    )
    for prefix, filename in evidence:
        digest_field = f"{prefix}_sha256"
        signature_digest_field = f"{prefix}_signature_sha256"
        for field in (digest_field, signature_digest_field):
            if not _SHA256_RE.fullmatch(getattr(value, field)):
                raise HTTPException(422, f"{field} must be 64 lowercase hexadecimal characters")
        expected_key = value.artifact_key + "." + filename
        expected_signature_key = expected_key + ".sig"
        if getattr(value, f"{prefix}_key") != expected_key:
            raise HTTPException(422, f"{prefix}_key must be {expected_key}")
        if getattr(value, f"{prefix}_signature_key") != expected_signature_key:
            raise HTTPException(422, f"{prefix}_signature_key must be {expected_signature_key}")


def _validate_publish(value: PublishRelease) -> None:
    _parse_release_version(value.version, "version")
    _parse_release_version(value.min_supported, "min_supported")
    if not _SHA256_RE.fullmatch(value.sha256):
        raise HTTPException(422, "sha256 must be 64 lowercase hexadecimal characters")
    if value.key_id not in _RELEASE_PUBLIC_KEYS:
        raise HTTPException(422, "unknown release key_id")
    if value.size <= 0:
        raise HTTPException(422, "size must be positive")
    try:
        signature = base64.b64decode(value.signature, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise HTTPException(422, "signature must be canonical base64") from exc
    if len(signature) != 64 or base64.b64encode(signature).decode("ascii") != value.signature:
        raise HTTPException(422, "signature must encode one Ed25519 signature")
    _verify_artifact_signature(
        value.key_id, value.version, value.platform, value.arch, value.sha256, signature
    )
    parsed_notes = urlparse(value.notes_url)
    if parsed_notes.scheme not in {"http", "https"} or not parsed_notes.netloc:
        raise HTTPException(422, "notes_url must be an absolute HTTP(S) URL")
    expected_name = _canonical_artifact(value.version, value.platform, value.arch)
    expected_key = f"{value.channel}/{value.version}/{expected_name}"
    path = PurePosixPath(value.artifact_key)
    if path.is_absolute() or ".." in path.parts or value.artifact_key != expected_key:
        raise HTTPException(422, f"artifact_key must be {expected_key}")
    _validate_payload_metadata(value)
    _validate_evidence_metadata(value)


def _require_publish_token(request: Request) -> None:
    configured = (os.environ.get("CHUDWARE_GATEWAY_RELEASE_TOKEN") or "").strip()
    if not configured:
        raise HTTPException(
            503,
            "release publishing is not configured; set CHUDWARE_GATEWAY_RELEASE_TOKEN",
        )
    authorization = request.headers.get("authorization", "")
    supplied = authorization[7:] if authorization.startswith("Bearer ") else ""
    if not supplied or not hmac.compare_digest(supplied, configured):
        raise HTTPException(401, "invalid release publishing token")


def _fetch_latest(channel: str, platform: str, arch: str) -> dict[str, Any] | None:
    with tx() as cur:
        cur.execute(
            """
            SELECT version, min_supported, key_id, sha256, size, signature, notes_url,
                   artifact_key, payload_manifest_key, payload_manifest_sha256,
                   payload_signature_key, payload_signature_sha256
              FROM releases
             WHERE channel = %s AND platform = %s AND arch = %s
             ORDER BY published_at DESC, version DESC
             LIMIT 1
            """,
            (channel, platform, arch),
        )
        row = cur.fetchone()
    return dict(row) if row is not None else None


def _active_entitlement(token: str) -> bool:
    try:
        payload = entitlement.verify_compact(token)
        account = accounts.get_account(payload["email"])
        return bool(
            account is not None
            and entitlement._subject_matches(payload["email"], payload["sub"])
            and entitlement._entitlement_window(account, int(time.time()))
            is not None
        )
    except Exception:
        # Invalid credentials are intentionally indistinguishable from no credentials:
        # both receive the exact anonymous metadata body, without a URL.
        return False


def _may_download(request: Request) -> bool:
    if getattr(request.state, "user", None) is not None:
        return True
    token = request.headers.get("x-chudware-entitlement", "")
    if not token:
        authorization = request.headers.get("authorization", "")
        token = authorization[7:] if authorization.startswith("Bearer ") else ""
    return bool(token) and _active_entitlement(token)


def _presigned_url(artifact_key: str) -> str:
    required = ("R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY")
    missing = [name for name in required if not (os.environ.get(name) or "").strip()]
    if missing:
        raise HTTPException(
            503,
            "release downloads are not configured; set " + ", ".join(missing),
        )
    client = boto3.client(
        "s3",
        endpoint_url=os.environ["R2_ENDPOINT"],
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
    )
    return str(
        client.generate_presigned_url(
            "get_object",
            Params={
                "Bucket": os.environ.get("CHUDWARE_RELEASES_BUCKET", "chudware-releases"),
                "Key": artifact_key,
            },
            ExpiresIn=900,
        )
    )


@router.get("/api/releases/latest")
async def latest_release(
    request: Request,
    response: Response,
    channel: Channel,
    platform: Platform,
    arch: Architecture,
) -> dict[str, Any]:
    client = request.client.host if request.client else "unknown"
    _manifest_by_ip.enforce(client, "release manifest checks")
    row = _fetch_latest(channel, platform, arch)
    if row is None:
        raise HTTPException(404, "release not found")
    artifact_key = str(row.pop("artifact_key"))
    manifest = {
        "version": str(row["version"]),
        "min_supported": str(row["min_supported"]),
        "key_id": str(row["key_id"]),
        "sha256": str(row["sha256"]),
        "size": int(row["size"]),
        "signature": str(row["signature"]),
        "notes_url": str(row["notes_url"]),
        "payload_manifest_key": str(row["payload_manifest_key"]),
        "payload_manifest_sha256": str(row["payload_manifest_sha256"]),
        "payload_signature_key": str(row["payload_signature_key"]),
        "payload_signature_sha256": str(row["payload_signature_sha256"]),
    }
    if _may_download(request):
        manifest["url"] = _presigned_url(artifact_key)
        response.headers["Cache-Control"] = "private, no-store"
    else:
        response.headers["Cache-Control"] = "public, max-age=300"
    return manifest


def _upsert_release(value: PublishRelease) -> None:
    _validate_publish(value)
    columns = (
        "channel", "version", "min_supported", "platform", "arch", "key_id", "sha256",
        "size", "signature", "notes_url", "artifact_key", "payload_manifest_key",
        "payload_manifest_sha256", "payload_signature_key", "payload_signature_sha256",
        "commit_sha", "sbom_key", "sbom_sha256", "sbom_signature_key",
        "sbom_signature_sha256", "provenance_key", "provenance_sha256",
        "provenance_signature_key", "provenance_signature_sha256", "platform_test_key",
        "platform_test_sha256", "platform_test_signature_key", "platform_test_signature_sha256",
    )
    values = tuple(getattr(value, column) for column in columns)
    requested_floor = _parse_release_version(value.min_supported, "min_supported")
    with tx() as cur:
        cur.execute(
            "SELECT min_supported FROM release_channels WHERE channel = %s FOR UPDATE",
            (value.channel,),
        )
        channel_row = cur.fetchone()
        if channel_row is None:
            raise HTTPException(503, "release channel schema is not initialized")
        current_floor = channel_row["min_supported"]
        if current_floor is not None:
            try:
                parsed_current_floor = _parse_release_version(str(current_floor), "stored min_supported")
            except HTTPException as exc:
                raise HTTPException(503, "stored release floor is invalid") from exc
            if requested_floor < parsed_current_floor:
                raise HTTPException(409, "min_supported cannot move backward")
        cur.execute(
            f"""
            INSERT INTO releases ({', '.join(columns)}, published_at)
            VALUES ({', '.join(['%s'] * len(columns))}, now())
            ON CONFLICT (channel, version, platform, arch) DO NOTHING
            """,
            values,
        )
        if getattr(cur, "rowcount", 1) == 0:
            raise HTTPException(409, "release identity is immutable and already registered")
        cur.execute(
            "UPDATE release_channels SET min_supported = %s, updated_at = now() WHERE channel = %s",
            (value.min_supported, value.channel),
        )


@router.post("/api/releases/latest/publish")
async def publish_release(value: PublishRelease, request: Request) -> dict[str, Any]:
    client = request.client.host if request.client else "unknown"
    _publish_by_ip.enforce(client, "release publications")
    _require_publish_token(request)
    _upsert_release(value)
    return {
        "ok": True,
        "artifact_key": value.artifact_key,
        "payload_manifest_key": value.payload_manifest_key,
        "payload_signature_key": value.payload_signature_key,
        "sbom_key": value.sbom_key,
        "sbom_signature_key": value.sbom_signature_key,
        "provenance_key": value.provenance_key,
        "provenance_signature_key": value.provenance_signature_key,
        "platform_test_key": value.platform_test_key,
        "platform_test_signature_key": value.platform_test_signature_key,
    }


@router.get("/api/admin/release-manifests")
async def list_release_manifests(request: Request) -> list[dict[str, Any]]:
    _require_admin(request)
    with tx() as cur:
        cur.execute("SELECT * FROM releases ORDER BY published_at DESC, channel, platform, arch")
        rows = cur.fetchall()
    return [dict(row) for row in rows]


@router.post("/api/admin/release-manifests")
async def admin_publish_release(
    value: PublishRelease, request: Request
) -> dict[str, Any]:
    _require_admin(request)
    _upsert_release(value)
    return {"ok": True}


@router.delete("/api/admin/release-manifests")
async def delete_release_manifest(
    request: Request,
    channel: Channel,
    version: str,
    platform: Platform,
    arch: Architecture,
) -> dict[str, Any]:
    _require_admin(request)
    if not _SEMVER_RE.fullmatch(version):
        raise HTTPException(422, "version must be canonical semantic version")
    with tx() as cur:
        cur.execute(
            """
            DELETE FROM releases
             WHERE channel = %s AND version = %s AND platform = %s AND arch = %s
            """,
            (channel, version, platform, arch),
        )
        deleted = cur.rowcount
    if not deleted:
        raise HTTPException(404, "release manifest not found")
    return {"ok": True}


@router.get("/api/admin/releases")
async def list_releases(request: Request) -> list[dict[str, Any]]:
    _require_admin(request)
    directory = _releases_dir()
    if not directory.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for entry in directory.iterdir():
        if not entry.is_file() or entry.name.startswith("."):
            continue
        st = entry.stat()
        out.append(
            {
                "name": entry.name,
                "size": st.st_size,
                "mtime": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(),
            }
        )
    out.sort(key=lambda item: item["name"])
    return out


@router.get("/api/admin/releases/download/{name}")
async def download_release(name: str, request: Request) -> FileResponse:
    _require_admin(request)
    safe = _safe_name(name)
    path = _releases_dir() / safe
    if not path.is_file():
        raise HTTPException(status_code=404, detail="release not found")
    return FileResponse(path, filename=safe)


@router.post("/api/admin/releases/upload")
async def upload_release(request: Request, file: UploadFile = File(...)) -> dict[str, Any]:
    _require_admin(request)
    safe = _safe_name(file.filename or "")
    directory = _releases_dir()
    directory.mkdir(parents=True, exist_ok=True)
    dest = directory / safe
    tmp = dest.with_name(dest.name + ".part")
    try:
        with tmp.open("wb") as out:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                out.write(chunk)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    finally:
        await file.close()
    tmp.replace(dest)
    st = dest.stat()
    return {
        "name": dest.name,
        "size": st.st_size,
        "mtime": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(),
    }


@router.delete("/api/admin/releases/{name}")
async def delete_release(name: str, request: Request) -> dict[str, Any]:
    _require_admin(request)
    safe = _safe_name(name)
    path = _releases_dir() / safe
    if not path.is_file():
        raise HTTPException(status_code=404, detail="release not found")
    path.unlink()
    return {"ok": True}
