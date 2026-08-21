"""Release metadata, signed download manifests, and manual admin uploads.

The customer manifest is deliberately public except for its short-lived R2 URL.
Release publication is authenticated independently with a deployment token so CI
does not need (and cannot accidentally acquire) an administrator browser session.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import os
import re
import shutil
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


def _parse_prerelease_version(value: str, field: str) -> tuple[int, int, int]:
    """Accept a canonical triple with an OPTIONAL prerelease suffix.

    Nightly betas are ``<base>-beta.<run>`` by construction, so the beta channel
    cannot use ``_parse_release_version``. The returned ordering key is the
    numeric triple only: prerelease suffixes are deliberately NOT ordered here
    because nothing in this module ranks two betas of the same triple, and a
    half-correct semver precedence rule would be worse than none.
    """
    match = _PRERELEASE_SEMVER_RE.fullmatch(value)
    if match is None:
        raise HTTPException(
            422, f"{field} must be major.minor.patch with an optional -prerelease"
        )
    return tuple(int(component) for component in match.groups()[:3])


_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_COMMIT_SHA_RE = re.compile(r"[0-9a-f]{40,64}")
_PRERELEASE_SEMVER_RE = re.compile(
    r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-([0-9A-Za-z.-]+))?"
)
_ARTIFACT_SIGNATURE_DOMAIN = b"chudware-artifact-v1"
_RELEASE_PUBLIC_KEYS = {
    "release-v1": bytes.fromhex(
        "29acbae141bccaf0b22e1a94d34d0bc7361e526d0bfe12c89794bc9322966dd7"
    ),
    # Nightly CI signer. Unconditional, not dev-only: the automatic pipeline is
    # the only thing that publishes betas, and a key the gateway refuses in
    # production would make every nightly a 422 nobody looks at.
    "release-ci-v1": bytes.fromhex(
        "d7434dc67f4d6e11cd4c8ced9b5ebbccf2e1477b5949d52d7685b6ca43a010f6"
    ),
}
_CI_KEY_ID = "release-ci-v1"
_CI_CHANNEL = "beta"
_CI_STATUSES = frozenset(
    {"queued", "building", "testing", "packaging", "publishing", "published", "failed"}
)

# Sidecars that live beside an artifact, keyed by the suffix appended to the
# artifact's basename. The payload manifest pair is mandatory (its columns are
# NOT NULL since 0011); the evidence triple is all-or-none with commit_sha
# (0012) and is what gates promotion to stable.
_PAYLOAD_SIDECAR_SUFFIXES = (".payload-manifest.v1.json", ".payload-manifest.v1.sig")
_EVIDENCE_SIDECARS = (
    ("sbom", "sbom.cdx.json"),
    ("provenance", "provenance.slsa.json"),
    ("platform_test", "platform-test.v1.json"),
)
_EVIDENCE_COLUMNS = ("commit_sha",) + tuple(
    f"{prefix}_{field}"
    for prefix, _ in _EVIDENCE_SIDECARS
    for field in ("key", "sha256", "signature_key", "signature_sha256")
)
_R2_ENV = ("R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY")
_DEFAULT_PUBLIC_BASE_URL = "https://app.chudware.com"

_SAFE_NAME_RE = re.compile(r"[A-Za-z0-9._-]{1,128}")
_manifest_by_ip = RateLimiter(120, 60.0)
_publish_by_ip = RateLimiter(20, 60.0)
# CI runners all NAT through one public address, and a single platform leg can
# legitimately make ~15 calls a minute (per-file uploads + status posts); the
# ci/* routes are Bearer-token gated, so this limiter is only an abuse backstop.
_ci_by_ip = RateLimiter(240, 60.0)
_base_version_by_ip = RateLimiter(60, 60.0)
_artifact_by_ip = RateLimiter(30, 60.0)


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


class CiEvidence(BaseModel):
    """Digests CI claims for the sidecars it staged.

    Advisory, not authoritative: the gateway recomputes every digest from the
    staged bytes and 422s on disagreement. Sending them is what makes a truncated
    upload or a sidecar from the previous run fail loudly instead of being
    recorded as this build's evidence.
    """

    sbom_sha256: str
    sbom_signature_sha256: str
    provenance_sha256: str
    provenance_signature_sha256: str
    platform_test_sha256: str
    platform_test_signature_sha256: str


class CiPublish(BaseModel):
    version: str
    stable_version: str
    platform: Platform
    arch: Architecture
    channel: Channel = _CI_CHANNEL
    key_id: str = _CI_KEY_ID
    min_supported: str
    sha256: str
    size: int
    signature: str
    stable_signature: str
    notes_url: str
    commit_sha: str
    evidence: CiEvidence | None = None


class CiStatus(BaseModel):
    run_id: str
    commit_sha: str
    channel: Channel
    platform: Platform
    arch: Architecture
    version: str
    status: str
    detail: str = ""


class PromoteRelease(BaseModel):
    channel: Channel = _CI_CHANNEL
    version: str
    platform: Platform
    arch: Architecture


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


def _store_dir() -> Path:
    """Root of the self-hosted object store. Distinct from ``_releases_dir()``
    itself, which is the flat manual-upload area the admin page lists."""
    return _releases_dir() / "store"


def _incoming_dir() -> Path:
    """Where CI drops artifacts before the gateway has verified them. Nothing is
    ever served from here: an unverified file under a canonical name is exactly
    the artifact-substitution attack the signature check exists to stop."""
    return _releases_dir() / "incoming"


def _store_path(artifact_key: str) -> Path:
    """Map ``<channel>/<version>/<name>`` onto a file under ``store/``.

    Every component goes through ``_safe_name``, so the charset that already
    forbids separators and dotfiles also forbids ``..`` traversal — and the
    three-component shape is enforced rather than assumed, because a key with a
    fourth segment would otherwise silently create a directory nobody serves.
    """
    parts = PurePosixPath(artifact_key).parts
    if len(parts) != 3 or PurePosixPath(artifact_key).is_absolute():
        raise HTTPException(422, "artifact_key must be <channel>/<version>/<name>")
    root = _store_dir()
    for component in parts:
        root = root / _safe_name(component)
    return root


def _digest_file(path: Path) -> tuple[str, int]:
    """sha256 and byte length, streamed — release artifacts are ~200 MB."""
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            digest.update(chunk)
    return digest.hexdigest(), size


def _decode_signature(value: str, field: str) -> bytes:
    try:
        signature = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise HTTPException(422, f"{field} must be canonical base64") from exc
    if len(signature) != 64 or base64.b64encode(signature).decode("ascii") != value:
        raise HTTPException(422, f"{field} must encode one Ed25519 signature")
    return signature


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
    if not _COMMIT_SHA_RE.fullmatch(value.commit_sha):
        raise HTTPException(422, "commit_sha must be a full lowercase commit id")
    evidence = _EVIDENCE_SIDECARS
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
    signature = _decode_signature(value.signature, "signature")
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


def _r2_missing() -> list[str]:
    return [name for name in _R2_ENV if not (os.environ.get(name) or "").strip()]


def _public_base_url() -> str:
    base = (os.environ.get("CHUDWARE_RELEASES_PUBLIC_BASE_URL") or "").strip()
    return (base or _DEFAULT_PUBLIC_BASE_URL).rstrip("/")


def _self_hosted_url(artifact_key: str) -> str:
    """Absolute URL of the gateway's own artifact route.

    Absolute rather than relative because the desktop updater consumes this
    field with no notion of the origin it fetched the manifest from — and the
    origin is NOT taken from the Host header, which a proxy or an attacker can
    set, and which would redirect every update download.
    """
    return f"{_public_base_url()}/api/releases/artifact/{artifact_key}"


def _download_url(artifact_key: str) -> str:
    """R2 when it is configured, this gateway's own store otherwise.

    Self-hosting is the normal deployment: refusing to hand out a URL just
    because no CDN credential exists would make a complete, correctly signed
    release undownloadable and the updater permanently stuck.
    """
    if _r2_missing():
        return _self_hosted_url(artifact_key)
    return _presigned_url(artifact_key)


def _presigned_url(artifact_key: str) -> str:
    missing = _r2_missing()
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
        # The desktop updater requires these two and rejects a manifest whose
        # target identity is not its own (host/src/update/updater.cpp,
        # manifestFromJson). They are also half of the Ed25519 artifact
        # preimage, so omitting them made every published release unparseable
        # to the shipped client: "key 'platform' not found".
        "platform": str(row["platform"]),
        "arch": str(row["arch"]),
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
        manifest["url"] = _download_url(artifact_key)
        response.headers["Cache-Control"] = "private, no-store"
    else:
        response.headers["Cache-Control"] = "public, max-age=300"
    return manifest


_RELEASE_COLUMNS = (
    "channel", "version", "min_supported", "platform", "arch", "key_id", "sha256",
    "size", "signature", "notes_url", "artifact_key", "payload_manifest_key",
    "payload_manifest_sha256", "payload_signature_key", "payload_signature_sha256",
    "commit_sha", "sbom_key", "sbom_sha256", "sbom_signature_key",
    "sbom_signature_sha256", "provenance_key", "provenance_sha256",
    "provenance_signature_key", "provenance_signature_sha256", "platform_test_key",
    "platform_test_sha256", "platform_test_signature_key", "platform_test_signature_sha256",
)


def _insert_release_row(row: dict[str, Any], extra_columns: tuple[str, ...] = ()) -> None:
    """Insert one immutable release row and advance the channel's support floor.

    The floor is read ``FOR UPDATE`` BEFORE the insert so two concurrent
    publications cannot interleave into a floor that moves backward; the insert
    itself is ``DO NOTHING`` because a release identity, once signed and
    announced, must never be redefined by a later push.
    """
    columns = _RELEASE_COLUMNS + extra_columns
    values = tuple(row.get(column) for column in columns)
    channel = str(row["channel"])
    min_supported = str(row["min_supported"])
    requested_floor = _parse_release_version(min_supported, "min_supported")
    with tx() as cur:
        cur.execute(
            "SELECT min_supported FROM release_channels WHERE channel = %s FOR UPDATE",
            (channel,),
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
            (min_supported, channel),
        )


def _upsert_release(value: PublishRelease) -> None:
    _validate_publish(value)
    _insert_release_row({column: getattr(value, column) for column in _RELEASE_COLUMNS})


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


def _staged(name: str) -> Path:
    return _incoming_dir() / _safe_name(name)


def _require_staged(name: str, what: str) -> Path:
    path = _staged(name)
    if not path.is_file():
        raise HTTPException(422, f"staged {what} is missing from incoming/: {name}")
    return path


def _validate_ci_publish(value: CiPublish) -> tuple[bytes, bytes]:
    """Everything checkable without touching the filesystem. Returns both
    decoded signatures so the caller verifies them against the RECOMPUTED
    digest rather than the client-supplied one."""
    if value.channel != _CI_CHANNEL:
        raise HTTPException(422, "ci publications must target the beta channel")
    if value.key_id != _CI_KEY_ID:
        raise HTTPException(422, f"ci publications must be signed by {_CI_KEY_ID}")
    _parse_prerelease_version(value.version, "version")
    _parse_release_version(value.stable_version, "stable_version")
    _parse_release_version(value.min_supported, "min_supported")
    if not _SHA256_RE.fullmatch(value.sha256):
        raise HTTPException(422, "sha256 must be 64 lowercase hexadecimal characters")
    if value.size <= 0:
        raise HTTPException(422, "size must be positive")
    if not _COMMIT_SHA_RE.fullmatch(value.commit_sha):
        raise HTTPException(422, "commit_sha must be a full lowercase commit id")
    parsed_notes = urlparse(value.notes_url)
    if parsed_notes.scheme not in {"http", "https"} or not parsed_notes.netloc:
        raise HTTPException(422, "notes_url must be an absolute HTTP(S) URL")
    return (
        _decode_signature(value.signature, "signature"),
        _decode_signature(value.stable_signature, "stable_signature"),
    )


def _collect_ci_sidecars(
    value: CiPublish, name: str, artifact_key: str
) -> tuple[dict[str, Path], dict[str, Any]]:
    """Digest the staged sidecars and derive the columns they populate.

    Digests come from the bytes on disk, never from the request: CI's own
    numbers are only used as a cross-check so a stale sidecar left over from the
    previous run cannot be silently recorded as this build's evidence.
    """
    sidecars: dict[str, Path] = {}
    for suffix in _PAYLOAD_SIDECAR_SUFFIXES:
        sidecars[suffix] = _require_staged(name + suffix, "payload manifest sidecar")
    columns: dict[str, Any] = {
        "payload_manifest_key": artifact_key + _PAYLOAD_SIDECAR_SUFFIXES[0],
        "payload_manifest_sha256": _digest_file(sidecars[_PAYLOAD_SIDECAR_SUFFIXES[0]])[0],
        "payload_signature_key": artifact_key + _PAYLOAD_SIDECAR_SUFFIXES[1],
        "payload_signature_sha256": _digest_file(sidecars[_PAYLOAD_SIDECAR_SUFFIXES[1]])[0],
    }
    # 0012 makes the evidence bundle all-or-none WITH commit_sha, so a
    # publication without evidence stores no commit either. That is the schema's
    # deliberate position: a commit id with no provenance proves nothing, and
    # the ci_builds ledger records the commit for every build regardless.
    columns.update({column: None for column in _EVIDENCE_COLUMNS})
    if value.evidence is None:
        return sidecars, columns
    for prefix, filename in _EVIDENCE_SIDECARS:
        document = _require_staged(f"{name}.{filename}", f"{prefix} evidence")
        signature = _require_staged(f"{name}.{filename}.sig", f"{prefix} evidence signature")
        document_digest = _digest_file(document)[0]
        signature_digest = _digest_file(signature)[0]
        for field, actual in (
            (f"{prefix}_sha256", document_digest),
            (f"{prefix}_signature_sha256", signature_digest),
        ):
            if getattr(value.evidence, field) != actual:
                raise HTTPException(422, f"{field} does not match the staged sidecar")
        sidecars[f".{filename}"] = document
        sidecars[f".{filename}.sig"] = signature
        columns[f"{prefix}_key"] = f"{artifact_key}.{filename}"
        columns[f"{prefix}_sha256"] = document_digest
        columns[f"{prefix}_signature_key"] = f"{artifact_key}.{filename}.sig"
        columns[f"{prefix}_signature_sha256"] = signature_digest
    columns["commit_sha"] = value.commit_sha
    return sidecars, columns


def _release_exists(channel: str, version: str, platform: str, arch: str) -> bool:
    with tx() as cur:
        cur.execute(
            """
            SELECT 1 FROM releases
             WHERE channel = %s AND version = %s AND platform = %s AND arch = %s
            """,
            (channel, version, platform, arch),
        )
        return cur.fetchone() is not None


@router.post("/api/releases/ci/publish")
async def ci_publish_release(value: CiPublish, request: Request) -> dict[str, Any]:
    """Register a nightly beta whose bytes CI already staged in ``incoming/``.

    Order matters: the request is validated, then the artifact is re-digested
    from disk, then both signatures are checked against THAT digest, then the
    bytes move into the store, and only then is the row written. Verifying the
    client's own sha256 would authenticate nothing, and inserting before the
    move could publish a manifest pointing at bytes that never arrived.
    """
    client = request.client.host if request.client else "unknown"
    _ci_by_ip.enforce(client, "ci release calls")
    _require_publish_token(request)
    signature, stable_signature = _validate_ci_publish(value)

    name = _safe_name(_canonical_artifact(value.version, value.platform, value.arch))
    staged = _staged(name)
    if not staged.is_file():
        raise HTTPException(404, f"staged artifact is missing from incoming/: {name}")
    digest, size = _digest_file(staged)
    if digest != value.sha256 or size != value.size:
        # A truncated or swapped upload must not linger under a canonical name
        # where the next run's publish would find and trust it.
        staged.unlink(missing_ok=True)
        raise HTTPException(
            422, "staged artifact digest or size does not match the publication"
        )
    for signed_version, blob, field in (
        (value.version, signature, "signature"),
        (value.stable_version, stable_signature, "stable_signature"),
    ):
        try:
            _verify_artifact_signature(
                value.key_id, signed_version, value.platform, value.arch, digest, blob
            )
        except HTTPException as exc:
            if exc.status_code == 422 and field == "stable_signature":
                raise HTTPException(
                    422, "stable_signature does not match the stable release identity"
                ) from exc
            raise

    artifact_key = f"{_CI_CHANNEL}/{value.version}/{name}"
    if _release_exists(_CI_CHANNEL, value.version, value.platform, value.arch):
        raise HTTPException(409, "release identity is immutable and already registered")
    sidecars, columns = _collect_ci_sidecars(value, name, artifact_key)

    destination = _store_path(artifact_key)
    destination.parent.mkdir(parents=True, exist_ok=True)
    moved = [(staged, destination)]
    moved.extend(
        (path, destination.with_name(destination.name + suffix))
        for suffix, path in sidecars.items()
    )
    for source, target in moved:
        # Same filesystem in every supported layout, but replace() across
        # devices raises; shutil.move handles both and is still atomic-ish
        # within a device.
        shutil.move(str(source), str(target))

    columns.update(
        {
            "channel": _CI_CHANNEL,
            "version": value.version,
            "min_supported": value.min_supported,
            "platform": value.platform,
            "arch": value.arch,
            "key_id": value.key_id,
            "sha256": digest,
            "size": size,
            "signature": value.signature,
            "notes_url": value.notes_url,
            "artifact_key": artifact_key,
            "stable_version": value.stable_version,
            "stable_signature": value.stable_signature,
        }
    )
    _insert_release_row(columns, ("stable_version", "stable_signature"))
    return {
        "ok": True,
        "channel": _CI_CHANNEL,
        "version": value.version,
        "stable_version": value.stable_version,
        "artifact_key": artifact_key,
        "sha256": digest,
        "size": size,
        "evidence_complete": value.evidence is not None,
        "stored": [str(target.relative_to(_store_dir())) for _, target in moved],
    }


@router.put("/api/releases/ci/upload/{name}")
async def ci_upload_artifact(name: str, request: Request) -> dict[str, Any]:
    """Stream a CI artifact into ``incoming/`` ahead of publication.

    The Windows/macOS runners cannot reach the gateway host's LAN address (the
    networks do not route), so artifacts arrive here over the public endpoint
    instead of scp. The body streams to a ``.part`` file and is atomically
    renamed into place, so ci/publish never sees a torn upload; nothing in this
    handler is trusted — publish re-digests the bytes from disk before anything
    moves into the store.
    """
    client = request.client.host if request.client else "unknown"
    _ci_by_ip.enforce(client, "ci release calls")
    _require_publish_token(request)
    safe = _safe_name(name)
    target = _incoming_dir() / (safe + ".part")
    target.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    size = 0
    try:
        with target.open("wb") as out:
            async for chunk in request.stream():
                size += len(chunk)
                if size > 2 * 1024**3:
                    raise HTTPException(413, "release artifact exceeds the 2 GiB ceiling")
                digest.update(chunk)
                out.write(chunk)
    except Exception:
        try:
            target.unlink(missing_ok=True)
        except OSError:
            pass  # never mask the real failure with a cleanup failure
        raise
    os.replace(target, _incoming_dir() / safe)
    return {"ok": True, "name": safe, "size": size, "sha256": digest.hexdigest()}


@router.post("/api/releases/ci/status")
async def ci_report_status(value: CiStatus, request: Request) -> dict[str, Any]:
    client = request.client.host if request.client else "unknown"
    _ci_by_ip.enforce(client, "ci release calls")
    _require_publish_token(request)
    if value.status not in _CI_STATUSES:
        raise HTTPException(422, "status must be one of " + ", ".join(sorted(_CI_STATUSES)))
    if not _COMMIT_SHA_RE.fullmatch(value.commit_sha):
        raise HTTPException(422, "commit_sha must be a full lowercase commit id")
    _parse_prerelease_version(value.version, "version")
    if not value.run_id or not _SAFE_NAME_RE.fullmatch(value.run_id):
        raise HTTPException(422, "run_id must be a short opaque identifier")
    with tx() as cur:
        cur.execute(
            """
            INSERT INTO ci_builds
                   (run_id, commit_sha, channel, platform, arch, version, status, detail)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (run_id, platform, arch) DO UPDATE
               SET commit_sha = EXCLUDED.commit_sha,
                   channel    = EXCLUDED.channel,
                   version    = EXCLUDED.version,
                   status     = EXCLUDED.status,
                   detail     = EXCLUDED.detail,
                   updated_at = now()
            RETURNING id, run_id, platform, arch, status, updated_at
            """,
            (
                value.run_id, value.commit_sha, value.channel, value.platform,
                value.arch, value.version, value.status, value.detail[:4096],
            ),
        )
        row = cur.fetchone()
    return {"ok": True, **({} if row is None else dict(row))}


def _recent_ci_builds(
    limit: int, commit_sha: str | None = None, status: str | None = None
) -> list[dict[str, Any]]:
    bounded = max(1, min(int(limit), 200))
    where: list[str] = []
    params: list[Any] = []
    if commit_sha is not None:
        if not _COMMIT_SHA_RE.fullmatch(commit_sha):
            raise HTTPException(422, "commit_sha must be a full lowercase commit id")
        where.append("commit_sha = %s")
        params.append(commit_sha)
    if status is not None:
        if status not in _CI_STATUSES:
            raise HTTPException(422, "status must be one of " + ", ".join(sorted(_CI_STATUSES)))
        where.append("status = %s")
        params.append(status)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    params.append(bounded)
    with tx() as cur:
        cur.execute(
            "SELECT * FROM ci_builds" + clause
            + " ORDER BY updated_at DESC, id DESC LIMIT %s",
            tuple(params),
        )
        rows = cur.fetchall()
    return [dict(row) for row in rows]


@router.get("/api/releases/ci/builds")
async def ci_list_builds(
    request: Request,
    limit: int = 20,
    commit_sha: str | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    """Build ledger for the workflow itself, on the publish token rather than an
    admin session: the nightly planner runs on a runner that has neither a
    browser session nor any business holding database credentials."""
    client = request.client.host if request.client else "unknown"
    _ci_by_ip.enforce(client, "ci release calls")
    _require_publish_token(request)
    return {"builds": _recent_ci_builds(limit, commit_sha, status)}


@router.get("/api/releases/base-version")
async def release_base_version(request: Request) -> dict[str, str]:
    """Version the next nightly beta derives from: highest published stable
    patch + 1, floored at 0.1.0 before the first stable ever ships."""
    client = request.client.host if request.client else "unknown"
    _base_version_by_ip.enforce(client, "release base version checks")
    with tx() as cur:
        cur.execute("SELECT version FROM releases WHERE channel = 'stable'")
        rows = cur.fetchall()
    highest: tuple[int, int, int] | None = None
    for row in rows:
        try:
            parsed = _parse_release_version(str(row["version"]), "version")
        except HTTPException:
            # A row predating the canonical-version constraint must not stop the
            # nightly; it simply does not participate in the maximum.
            continue
        if highest is None or parsed > highest:
            highest = parsed
    if highest is None:
        return {"base": "0.1.0"}
    return {"base": f"{highest[0]}.{highest[1]}.{highest[2] + 1}"}


@router.get("/api/releases/artifact/{artifact_key:path}")
async def download_release_artifact(
    artifact_key: str, request: Request, response: Response
) -> FileResponse:
    """Serve release bytes from the self-hosted store.

    Two independent gates: an entitlement (or admin session), and an exact match
    against a published ``releases.artifact_key``. The second is what keeps this
    from being a read primitive over ``store/`` — an unpublished or deleted key
    is a 404 even when the file happens to still be on disk.
    """
    client = request.client.host if request.client else "unknown"
    _artifact_by_ip.enforce(client, "release artifact downloads")
    if not _may_download(request):
        raise HTTPException(403, "an active entitlement is required to download releases")
    with tx() as cur:
        cur.execute("SELECT artifact_key FROM releases WHERE artifact_key = %s", (artifact_key,))
        row = cur.fetchone()
    if row is None:
        raise HTTPException(404, "release artifact not found")
    path = _store_path(artifact_key)
    if not path.is_file():
        raise HTTPException(404, "release artifact bytes are not present on this host")
    response.headers["Cache-Control"] = "private, no-store"
    return FileResponse(
        path,
        filename=path.name,
        media_type="application/octet-stream",
        headers={"Cache-Control": "private, no-store"},
    )


_EVIDENCE_COMPLETE_SQL = " AND ".join(
    f"{column} IS NOT NULL" for column in _EVIDENCE_COLUMNS
)


@router.get("/api/admin/release-manifests")
async def list_release_manifests(request: Request) -> list[dict[str, Any]]:
    """Every published manifest, plus the two flags the admin page badges rows
    with. Computed in SQL rather than in the browser so a schema change cannot
    leave the UI confidently drawing a promote button on an unpromotable row."""
    _require_admin(request)
    with tx() as cur:
        cur.execute(
            f"""
            SELECT *,
                   (stable_version IS NOT NULL AND stable_signature IS NOT NULL)
                       AS has_stable_pair,
                   ({_EVIDENCE_COMPLETE_SQL}) AS evidence_complete
              FROM releases
             ORDER BY published_at DESC, channel, platform, arch
            """
        )
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
    _parse_prerelease_version(version, "version")
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


def _fetch_release_row(
    channel: str, version: str, platform: str, arch: str
) -> dict[str, Any] | None:
    with tx() as cur:
        cur.execute(
            """
            SELECT * FROM releases
             WHERE channel = %s AND version = %s AND platform = %s AND arch = %s
            """,
            (channel, version, platform, arch),
        )
        row = cur.fetchone()
    return dict(row) if row is not None else None


@router.post("/api/admin/releases/promote")
async def promote_release(value: PromoteRelease, request: Request) -> dict[str, Any]:
    """Publish a beta's stable twin using the signature CI already minted.

    Nothing is re-signed and no byte changes: the artifact is copied to its
    stable name and the beta row's ``stable_signature`` — verified here against
    the stored digest before anything is written — becomes the stable row's
    signature. That is the whole point of signing twice at build time: promotion
    needs no key on this host and cannot be turned into a signing oracle.
    """
    _require_admin(request)
    if value.channel != _CI_CHANNEL:
        raise HTTPException(422, "only beta releases can be promoted")
    _parse_prerelease_version(value.version, "version")
    row = _fetch_release_row(_CI_CHANNEL, value.version, value.platform, value.arch)
    if row is None:
        raise HTTPException(404, "beta release not found")
    stable_version = row.get("stable_version")
    stable_signature = row.get("stable_signature")
    if not stable_version or not stable_signature:
        raise HTTPException(
            422,
            "release carries no signed stable pair; it must be republished through "
            "/api/releases/ci/publish",
        )
    missing_evidence = [column for column in _EVIDENCE_COLUMNS if row.get(column) is None]
    if missing_evidence:
        raise HTTPException(
            422, "release evidence bundle is incomplete: " + ", ".join(missing_evidence)
        )
    stable_version = str(stable_version)
    stable_signature = str(stable_signature)
    _parse_release_version(stable_version, "stable_version")
    _verify_artifact_signature(
        str(row["key_id"]),
        stable_version,
        value.platform,
        value.arch,
        str(row["sha256"]),
        _decode_signature(stable_signature, "stable_signature"),
    )
    if _release_exists("stable", stable_version, value.platform, value.arch):
        raise HTTPException(409, "stable release identity is already registered")

    stable_name = _safe_name(
        _canonical_artifact(stable_version, value.platform, value.arch)
    )
    stable_key = f"stable/{stable_version}/{stable_name}"
    source = _store_path(str(row["artifact_key"]))
    destination = _store_path(stable_key)
    if not source.is_file():
        raise HTTPException(
            409, "beta artifact bytes are not present on this host; cannot promote"
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    suffixes = ("",) + _PAYLOAD_SIDECAR_SUFFIXES + tuple(
        f".{filename}{tail}"
        for _, filename in _EVIDENCE_SIDECARS
        for tail in ("", ".sig")
    )
    for suffix in suffixes:
        sidecar = source.with_name(source.name + suffix)
        if not sidecar.is_file():
            raise HTTPException(
                409, f"beta sidecar is missing on this host; cannot promote: {sidecar.name}"
            )
        shutil.copyfile(sidecar, destination.with_name(destination.name + suffix))

    promoted = {column: row.get(column) for column in _RELEASE_COLUMNS}
    promoted.update(
        {
            "channel": "stable",
            "version": stable_version,
            "signature": stable_signature,
            "artifact_key": stable_key,
            "payload_manifest_key": stable_key + _PAYLOAD_SIDECAR_SUFFIXES[0],
            "payload_signature_key": stable_key + _PAYLOAD_SIDECAR_SUFFIXES[1],
        }
    )
    for prefix, filename in _EVIDENCE_SIDECARS:
        promoted[f"{prefix}_key"] = f"{stable_key}.{filename}"
        promoted[f"{prefix}_signature_key"] = f"{stable_key}.{filename}.sig"
    _insert_release_row(promoted)
    return {
        "ok": True,
        "promoted_from": str(row["artifact_key"]),
        **{
            column: promoted[column]
            for column in ("channel", "version", "min_supported", "platform", "arch",
                           "key_id", "sha256", "size", "signature", "notes_url",
                           "artifact_key", "commit_sha")
        },
    }


@router.get("/api/admin/ci/builds")
async def admin_list_ci_builds(request: Request, limit: int = 20) -> list[dict[str, Any]]:
    _require_admin(request)
    return _recent_ci_builds(limit)


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
