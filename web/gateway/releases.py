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
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Literal
from urllib.parse import urlparse

import boto3
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, ValidationError
from fastapi.exceptions import RequestValidationError
from starlette.datastructures import UploadFile

try:
    from . import accounts, entitlement
    from .artifact_names import FORMATS, canonical_artifact
    from .release_evidence import capture_bundle, verify_bundle, native_publisher_verified
    from .auth import get_current_user
    from .config import settings
    from .db import tx
    from .limits import RateLimiter, RELEASE_UPLOAD_BYTES, SMALL_REQUEST_BYTES, bounded_form, read_bounded_body, request_network
except ImportError:  # uvicorn app:app from package dir
    import accounts  # type: ignore
    import entitlement  # type: ignore
    from artifact_names import FORMATS, canonical_artifact  # type: ignore
    from release_evidence import capture_bundle, verify_bundle, native_publisher_verified
    from auth import get_current_user  # type: ignore
    from config import settings  # type: ignore
    from db import tx  # type: ignore
    from limits import RateLimiter, RELEASE_UPLOAD_BYTES, SMALL_REQUEST_BYTES, bounded_form, read_bounded_body, request_network

router = APIRouter(tags=["releases"])

Channel = Literal["stable", "beta"]
Platform = Literal["linux", "windows", "macos"]
Architecture = Literal["x86_64", "arm64"]
# Mirrors artifact_names.FORMATS, which is the runtime source of truth; a
# Literal cannot be built from a tuple in a way FastAPI can introspect, so the
# two are pinned together by a test rather than by construction.
Format = Literal["default", "deb", "rpm", "tar", "msi"]
DEFAULT_FORMAT = "default"

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
# Every file a publication owns, relative to its artifact's basename: the
# artifact itself, the mandatory payload manifest pair, and the evidence triple
# with its signatures. Promotion copies exactly this set and retention deletes
# exactly this set, so the two cannot drift apart.
_ARTIFACT_SUFFIXES = ("",) + _PAYLOAD_SIDECAR_SUFFIXES + tuple(
    f".{filename}{tail}"
    for _, filename in _EVIDENCE_SIDECARS
    for tail in ("", ".sig")
)
_R2_ENV = ("R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY")
_DEFAULT_PUBLIC_BASE_URL = "https://app.chudware.com"

# '~' is in the charset because Debian spells a prerelease with it:
# chudware_0.1.0~beta.21_amd64.deb is the canonical .deb name, and without '~'
# every nightly .deb would 400 on its own filename. It is inert on the paths
# this module builds — nothing here shells out and nothing calls expanduser()
# on an artifact name — while the charset still forbids '/', '\' and a leading
# '.', which is what actually stops traversal.
_SAFE_NAME_RE = re.compile(r"[A-Za-z0-9._~-]{1,128}")
_manifest_by_ip = RateLimiter(120, 60.0)
_publish_by_ip = RateLimiter(20, 60.0)
# CI runners all NAT through one public address, and a single platform leg can
# legitimately make ~15 calls a minute (per-file uploads + status posts); the
# ci/* routes are Bearer-token gated, so this limiter is only an abuse backstop.
_ci_by_ip = RateLimiter(240, 60.0)
_base_version_by_ip = RateLimiter(60, 60.0)
# The portal's downloads view and the marketing site's download page both call
# /available on every visit, so it gets its own budget rather than spending the
# desktop updater's manifest budget.
_available_by_ip = RateLimiter(120, 60.0)
_artifact_by_ip = RateLimiter(30, 60.0)


class PublishRelease(BaseModel):
    channel: Channel
    key_id: str
    version: str
    min_supported: str
    platform: Platform
    arch: Architecture
    # Optional with a default so every existing caller — the shipped desktop
    # updater, the current nightly workflow, the admin manifest form — keeps
    # publishing and reading the AppImage/exe/zip with no change at all.
    format: Format = DEFAULT_FORMAT
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
    format: Format = DEFAULT_FORMAT
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
    format: Format = DEFAULT_FORMAT


def _require_admin(request: Request):
    user = get_current_user(request)
    if getattr(user, "role", None) != "admin":
        raise HTTPException(status_code=403, detail="admin only")
    return user


def _releases_dir() -> Path:
    env = (os.environ.get("CHUDWARE_RELEASES_DIR") or "").strip()
    if settings.deploy_environment == "staging":
        root = Path(env).expanduser().resolve() if env else settings.data_root / "customer-releases"
        if not root.resolve().is_relative_to(settings.data_root.resolve()):
            raise HTTPException(503, "staging release storage must be inside its data root")
        return root
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


def _canonical_artifact(
    version: str, platform: str, arch: str, fmt: str = DEFAULT_FORMAT
) -> str:
    """The one filename this identity may be published under.

    A thin wrapper over ``artifact_names.canonical_artifact``, which is where
    the rule actually lives. It is a separate, dependency-free module because
    the Linux packaging CI has to produce exactly these names with a bare
    ``python3`` on a build runner that has no gateway virtualenv, and a second
    implementation over there would drift on the first prerelease.
    """
    try:
        return canonical_artifact(version, platform, arch, fmt)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


def _artifact_signature_preimage(
    key_id: str, version: str, platform: str, arch: str, sha256: str
) -> bytes:
    """FROZEN. Do not add ``format`` — or anything else — to this tuple.

    Every shipped client verifies a release against
    (domain, key_id, version, platform, arch, sha256), so a new field here
    makes every future release fail signature verification on every installed
    copy of 0.1.1 and permanently strands them with no update path. That is
    also why it does not NEED format: sha256 already binds the exact bytes, so
    the .deb's signature cannot be replayed onto the AppImage of the same
    version. Format is a routing key that selects WHICH artifact a caller
    wants; the digest is the identity claim.
    """
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
    expected_name = _canonical_artifact(
        value.version, value.platform, value.arch, value.format
    )
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


async def _publication_body(request: Request, model: type[BaseModel]) -> Any:
    """Parse only after the publishing token is checked, including without app middleware."""
    raw = await read_bounded_body(request, SMALL_REQUEST_BYTES, "release metadata too large")
    try:
        return model.model_validate_json(raw)
    except ValidationError as exc:
        raise RequestValidationError([
            dict(error, loc=("body", *error["loc"])) for error in exc.errors()
        ]) from exc


def _fetch_latest(
    channel: str, platform: str, arch: str, fmt: str = DEFAULT_FORMAT
) -> dict[str, Any] | None:
    with tx() as cur:
        cur.execute(
            """
            SELECT version, min_supported, key_id, sha256, size, signature, notes_url,
                   artifact_key, payload_manifest_key, payload_manifest_sha256,
                   payload_signature_key, payload_signature_sha256
              FROM releases
             WHERE channel = %s AND platform = %s AND arch = %s AND format = %s
             ORDER BY published_at DESC, version DESC
             LIMIT 1
            """,
            (channel, platform, arch, fmt),
        )
        row = cur.fetchone()
    return dict(row) if row is not None else None


def _fetch_available(channel: str) -> list[dict[str, Any]]:
    """Newest publication per (platform, arch, format) for one channel.

    No artifact_key, no signature, no URL: this is the anonymous catalogue the
    downloads UI paints itself from, and it must stay safe to cache in a CDN for
    five minutes. DISTINCT ON does the newest-per-tuple selection in one index
    scan over releases_latest_idx rather than one query per tuple.
    """
    with tx() as cur:
        cur.execute(
            """
            SELECT DISTINCT ON (platform, arch, format)
                   platform, arch, format, version, size, sha256, notes_url
              FROM releases
             WHERE channel = %s
             ORDER BY platform, arch, format, published_at DESC, version DESC
            """,
            (channel,),
        )
        rows = [dict(row) for row in cur.fetchall()]
    # Presentation order is deliberate rather than lexical: Windows and Linux
    # lead the catalogue, and Windows offers its MSI before the legacy .exe.
    # ``default`` remains the API/query default even though it is second there.
    platform_order = {"windows": 0, "linux": 1, "macos": 2}
    format_order = {
        "windows": {"msi": 0, "default": 1},
        "linux": {"default": 0, "deb": 1, "rpm": 2, "tar": 3},
        "macos": {"default": 0},
    }
    rows.sort(
        key=lambda row: (
            platform_order.get(str(row["platform"]), len(platform_order)),
            str(row["arch"]),
            format_order.get(str(row["platform"]), {}).get(
                str(row["format"]), len(FORMATS)
            ),
        )
    )
    return rows


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


# Stateless, HMAC-signed, 15 minutes, bound to one account. It exists because
# `curl … | sh` has no browser session and no desktop entitlement: the portal
# mints one of these on a click and the install script presents it. The signing
# domain is deliberately NOT the entitlement domain — an entitlement is a
# long-lived, Ed25519-signed capability the desktop app verifies offline, this
# is a short-lived bearer string only this gateway ever checks, and a shared
# domain would let one be replayed as the other.
_DOWNLOAD_TOKEN_DOMAIN = b"chudware-download-token-v1"
DOWNLOAD_TOKEN_TTL = 900
DOWNLOAD_TOKEN_HEADER = "x-chudware-download-token"


def _download_token_mac(email: str, expires_at: int) -> str:
    secret = (settings.session_secret or "").encode("utf-8")
    if not secret:
        # No secret means every token would share one forgeable MAC. Refusing is
        # the only safe answer, and a deployment with no session_secret cannot
        # authenticate the portal caller who would ask for one anyway.
        raise HTTPException(503, "download tokens are not configured")
    return hmac.new(
        secret,
        b"\0".join(
            (_DOWNLOAD_TOKEN_DOMAIN, email.encode("utf-8"), str(expires_at).encode("ascii"))
        ),
        hashlib.sha256,
    ).hexdigest()


def mint_download_token(email: str, now: int | None = None) -> tuple[str, int]:
    """``(token, ttl_seconds)`` for *email*. Nothing is persisted."""
    issued_at = int(time.time()) if now is None else int(now)
    expires_at = issued_at + DOWNLOAD_TOKEN_TTL
    encoded = base64.urlsafe_b64encode(email.encode("utf-8")).decode("ascii").rstrip("=")
    return f"{encoded}.{expires_at}.{_download_token_mac(email, expires_at)}", DOWNLOAD_TOKEN_TTL


def _redeem_download_token(token: str, now: int | None = None) -> str | None:
    """The email a valid, unexpired token belongs to, or None.

    Signature first, then expiry, then account status — an account suspended
    after the token was minted must stop downloading immediately, which is the
    whole reason status is re-read here instead of being baked into the token.
    """
    encoded, _, rest = (token or "").partition(".")
    expiry, _, signature = rest.partition(".")
    if not encoded or not expiry or not signature:
        return None
    try:
        email = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode("utf-8")
        expires_at = int(expiry)
    except (ValueError, binascii.Error, UnicodeDecodeError):
        return None
    try:
        expected = _download_token_mac(email, expires_at)
    except HTTPException:
        return None
    if not hmac.compare_digest(signature, expected):
        return None
    # Parenthesised deliberately: `a if c else b >= x` binds the comparison to
    # the else-branch only, which made every unexpired token look expired.
    checked_at = int(time.time()) if now is None else int(now)
    if checked_at >= expires_at:
        return None
    account = accounts.get_account(email)
    if account is None or getattr(account, "status", "") != "active":
        return None
    return email


def _download_token_from(request: Request) -> str:
    """Header first, then the query string.

    Both are required: a browser fetch can set the header, but `curl -fsSL`
    following the manifest's redirect to the artifact route cannot carry one, so
    the install script appends ?download_token= instead.
    """
    header = request.headers.get(DOWNLOAD_TOKEN_HEADER, "")
    return header or request.query_params.get("download_token", "")


def _may_download(request: Request) -> bool:
    if getattr(request.state, "user", None) is not None:
        return True
    token = request.headers.get("x-chudware-entitlement", "")
    if not token:
        authorization = request.headers.get("authorization", "")
        token = authorization[7:] if authorization.startswith("Bearer ") else ""
    if token and _active_entitlement(token):
        return True
    download_token = _download_token_from(request)
    return bool(download_token) and _redeem_download_token(download_token) is not None


def _r2_missing() -> list[str]:
    return [name for name in _R2_ENV if not (os.environ.get(name) or "").strip()]


def _public_base_url() -> str:
    base = (os.environ.get("CHUDWARE_RELEASES_PUBLIC_BASE_URL") or "").strip()
    if settings.deploy_environment == "staging":
        origin = settings.public_base_url.rstrip("/")
        if base and base.rstrip("/") != origin:
            raise HTTPException(503, "staging release origin must match its gateway")
        return origin
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


def _r2_store():
    bucket = os.environ.get("CHUDWARE_RELEASES_BUCKET") or (
        "chudware-releases-staging" if settings.deploy_environment == "staging" else "chudware-releases"
    )
    if settings.deploy_environment == "staging" and bucket != "chudware-releases-staging":
        raise HTTPException(503, "staging requires its own release bucket")
    return boto3.client(
        "s3", endpoint_url=os.environ["R2_ENDPOINT"],
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"], region_name="auto",
    ), bucket


@contextmanager
def _capture_published_bundle(artifact_key: str):
    source = _store_path(artifact_key)
    if _r2_missing():
        with capture_bundle(source) as captured:
            yield captured
        return
    client, bucket = _r2_store()
    with tempfile.TemporaryDirectory(prefix="chudware-object-publication-") as directory:
        captured = Path(directory) / source.name
        for suffix in _ARTIFACT_SUFFIXES:
            target = captured.with_name(captured.name + suffix)
            limit = 16 * 1024**2 if suffix else _ARTIFACT_CEILING
            try:
                result = client.get_object(Bucket=bucket, Key=artifact_key + suffix)
                body = result["Body"]
                try:
                    total = 0
                    with target.open("xb") as out:
                        while block := body.read(1024 * 1024):
                            total += len(block)
                            if total > limit:
                                raise HTTPException(422, "release object exceeds its byte limit")
                            out.write(block)
                finally:
                    body.close()
            except HTTPException:
                raise
            except Exception as exc:
                raise HTTPException(422, "release object is missing or unreadable") from exc
        yield captured


def _presigned_url(artifact_key: str) -> str:
    missing = _r2_missing()
    if missing:
        raise HTTPException(
            503,
            "release downloads are not configured; set " + ", ".join(missing),
        )
    client, bucket = _r2_store()
    return str(
        client.generate_presigned_url(
            "get_object",
            Params={
                "Bucket": bucket,
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
    # OPTIONAL, and it must stay optional. The shipped 0.1.1 desktop updater
    # requests channel+platform+arch and nothing else; making format required
    # would 422 every installed client's update check.
    format: Format = DEFAULT_FORMAT,
) -> dict[str, Any]:
    client = request_network(request)
    _manifest_by_ip.enforce(client, "release manifest checks")
    row = _fetch_latest(channel, platform, arch, format)
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
        # to the shipped client: "key 'platform' not found". The row was
        # selected by exactly these two values, so echoing them is exact.
        "platform": platform,
        "arch": arch,
        # Echoed for the same reason: the row was selected by it. An updater
        # that does not know about formats never sends one, gets "default"
        # back, and ignores a field it does not read.
        "format": format,
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


# Cross-origin readers of the download catalogue. chudware.com (Cloudflare
# Pages) is a DIFFERENT origin from app.chudware.com, and without an
# Access-Control-Allow-Origin header its download page can never show a real
# version or size and permanently renders its degraded static card.
#
# An explicit allowlist, echoed only on an exact match — never a wildcard and
# never a reflected arbitrary Origin. The response happens to contain nothing
# secret today, but "safe to read cross-origin" is a property of this route's
# body, and a wildcard or a reflector would keep granting access if that body
# ever gained a field. The allowlist costs nothing and does not.
_CATALOGUE_CORS_ORIGINS = ("https://chudware.com", "https://www.chudware.com")
# Astro's dev/preview port, and ONLY while dev_mode is on, so a production
# deployment never carries a localhost origin in its allowlist.
_CATALOGUE_CORS_DEV_ORIGINS = ("http://localhost:4321", "http://127.0.0.1:4321")


def _catalogue_cors_origins() -> tuple[str, ...]:
    if settings.dev_mode:
        return _CATALOGUE_CORS_ORIGINS + _CATALOGUE_CORS_DEV_ORIGINS
    return _CATALOGUE_CORS_ORIGINS


def _apply_catalogue_cors(request: Request, response: Response) -> None:
    """Echo an allowed Origin, and always Vary on it.

    ``Vary: Origin`` is unconditional: this route is cached for five minutes at
    the edge, and without it Cloudflare would store one origin's response —
    header included or omitted — and serve it to every other caller.

    No Access-Control-Allow-Credentials and no OPTIONS handler: the consumers
    send a bare GET with no custom request headers, which browsers dispatch with
    no preflight at all. Adding a permissive preflight would open a door nothing
    is knocking on.
    """
    response.headers["Vary"] = "Origin"
    origin = request.headers.get("origin", "")
    if origin and origin in _catalogue_cors_origins():
        response.headers["Access-Control-Allow-Origin"] = origin


@router.get("/api/releases/available")
async def available_releases(
    request: Request, response: Response, channel: Channel = "stable"
) -> dict[str, Any]:
    """The public download catalogue: what exists, for whom, how big.

    Anonymous by design and identical for every caller — no URL, no signature,
    no artifact key — so the downloads UI can auto-detect a visitor's platform
    and paint real versions and sizes before anybody signs in, and so the whole
    response is safe to cache for five minutes at the edge.

    A (platform, arch) tuple with no publication is simply ABSENT from builds.
    That is the contract every consumer keys its "no build yet" state off:
    macos/arm64 has never published, and inventing a placeholder row here would
    put a download button in front of bytes that do not exist.
    """
    client = request_network(request)
    _available_by_ip.enforce(client, "release catalogue reads")
    builds = [
        {
            "platform": str(row["platform"]),
            "arch": str(row["arch"]),
            "format": str(row["format"]),
            "version": str(row["version"]),
            "size": int(row["size"]),
            "sha256": str(row["sha256"]),
            "notes_url": str(row["notes_url"]),
        }
        for row in _fetch_available(channel)
    ]
    response.headers["Cache-Control"] = "public, max-age=300"
    _apply_catalogue_cors(request, response)
    return {"channel": channel, "builds": builds}


_RELEASE_COLUMNS = (
    "channel", "version", "min_supported", "platform", "arch", "key_id", "sha256",
    "size", "signature", "notes_url", "artifact_key", "payload_manifest_key",
    "payload_manifest_sha256", "payload_signature_key", "payload_signature_sha256",
    "commit_sha", "sbom_key", "sbom_sha256", "sbom_signature_key",
    "sbom_signature_sha256", "provenance_key", "provenance_sha256",
    "provenance_signature_key", "provenance_signature_sha256", "platform_test_key",
    "platform_test_sha256", "platform_test_signature_key", "platform_test_signature_sha256",
    # Appended, not inserted in identity order: this tuple is positional in the
    # generated INSERT, and tests pin column positions.
    "format", "native_publisher_verified",
)


def _insert_release_row(row: dict[str, Any], extra_columns: tuple[str, ...] = ()) -> None:
    """Insert one immutable release row and advance the channel's support floor.

    The floor is read ``FOR UPDATE`` BEFORE the insert so two concurrent
    publications cannot interleave into a floor that moves backward; the insert
    itself is ``DO NOTHING`` because a release identity, once signed and
    announced, must never be redefined by a later push.
    """
    columns = _RELEASE_COLUMNS + extra_columns
    values = tuple(row.get(column, False) if column == "native_publisher_verified"
                   else row.get(column) for column in columns)
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
            ON CONFLICT (channel, version, platform, arch, format) DO NOTHING
            """,
            values,
        )
        if getattr(cur, "rowcount", 1) == 0:
            raise HTTPException(409, "release identity is immutable and already registered")
        cur.execute(
            "UPDATE release_channels SET min_supported = %s, updated_at = now() WHERE channel = %s",
            (min_supported, channel),
        )


@contextmanager
def _publication_lock():
    # One lock for the rare publication/promotion operations. It spans checking
    # immutable identity, placing the bundle and inserting its row across workers.
    import fcntl
    import stat
    root = _releases_dir()
    root.mkdir(parents=True, exist_ok=True)
    fd = os.open(root / ".publication.lock",
                 os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_mode & 0o022):
            raise HTTPException(503, "release publication lock is unsafe")
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _upsert_release(value: PublishRelease) -> None:
    _validate_publish(value)
    with _publication_lock():
        row = value.model_dump()
        with _capture_published_bundle(value.artifact_key) as artifact:
            parsed = verify_bundle(artifact, row, _RELEASE_PUBLIC_KEYS)
            row["native_publisher_verified"] = native_publisher_verified(
                parsed, row, _RELEASE_PUBLIC_KEYS, required=value.channel == "stable",
            )
        _insert_release_row(row)


@router.post("/api/releases/latest/publish")
async def publish_release(request: Request) -> dict[str, Any]:
    _publish_by_ip.enforce(request_network(request), "release publications")
    _require_publish_token(request)
    value = await _publication_body(request, PublishRelease)
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
    value: CiPublish, artifact: Path, artifact_key: str
) -> tuple[dict[str, Path], dict[str, Any]]:
    """Derive publication columns from captured, authenticated sidecar bytes."""
    name = artifact.name
    sidecars = {
        suffix: artifact.with_name(name + suffix)
        for suffix in _PAYLOAD_SIDECAR_SUFFIXES
    }
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
        document = artifact.with_name(f"{name}.{filename}")
        signature = artifact.with_name(f"{name}.{filename}.sig")
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


def _release_exists(
    channel: str, version: str, platform: str, arch: str, fmt: str = DEFAULT_FORMAT
) -> bool:
    with tx() as cur:
        cur.execute(
            """
            SELECT 1 FROM releases
             WHERE channel = %s AND version = %s AND platform = %s AND arch = %s
               AND format = %s
            """,
            (channel, version, platform, arch, fmt),
        )
        return cur.fetchone() is not None


@router.post("/api/releases/ci/publish")
async def ci_publish_release(request: Request) -> dict[str, Any]:
    """Register a nightly beta whose bytes CI already staged in ``incoming/``.

    Order matters: the request is validated, then the artifact is re-digested
    from disk, then both signatures are checked against THAT digest, then the
    bytes move into the store, and only then is the row written. Verifying the
    client's own sha256 would authenticate nothing, and inserting before the
    move could publish a manifest pointing at bytes that never arrived.
    """
    _ci_by_ip.enforce(request_network(request), "ci release calls")
    _require_publish_token(request)
    value = await _publication_body(request, CiPublish)
    signature, stable_signature = _validate_ci_publish(value)
    with _publication_lock():
        return _publish_ci_bundle(value, signature, stable_signature)


def _publish_ci_bundle(value: CiPublish, signature: bytes, stable_signature: bytes) -> dict[str, Any]:

    name = _safe_name(
        _canonical_artifact(value.version, value.platform, value.arch, value.format)
    )
    staged = _staged(name)
    if not staged.is_file():
        raise HTTPException(404, f"staged artifact is missing from incoming/: {name}")
    artifact_key = f"{_CI_CHANNEL}/{value.version}/{name}"
    if _release_exists(
        _CI_CHANNEL, value.version, value.platform, value.arch, value.format
    ):
        raise HTTPException(409, "release identity is immutable and already registered")
    with capture_bundle(staged, evidence=value.evidence is not None) as artifact:
        digest, size = _digest_file(artifact)
        if digest != value.sha256 or size != value.size:
            raise HTTPException(422, "staged artifact digest or size does not match the publication")
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
        sidecars, columns = _collect_ci_sidecars(value, artifact, artifact_key)
        identity = {**value.model_dump(), **columns}
        parsed = verify_bundle(
            artifact, identity, _RELEASE_PUBLIC_KEYS,
            evidence=value.evidence is not None,
        )
        columns["native_publisher_verified"] = native_publisher_verified(
            parsed, identity, _RELEASE_PUBLIC_KEYS,
        )
        destination = _store_path(artifact_key)
        destination.parent.mkdir(parents=True, exist_ok=True)
        moved = [(artifact, destination)]
        moved.extend(
            (path, destination.with_name(destination.name + suffix))
            for suffix, path in sidecars.items()
        )
        for source, target in moved:
            shutil.move(str(source), str(target))
        columns.update(
            {
                "channel": _CI_CHANNEL, "version": value.version,
                "min_supported": value.min_supported, "platform": value.platform,
                "arch": value.arch, "format": value.format, "key_id": value.key_id,
                "sha256": digest, "size": size, "signature": value.signature,
                "notes_url": value.notes_url, "artifact_key": artifact_key,
                "stable_version": value.stable_version,
                "stable_signature": value.stable_signature,
            }
        )
        _insert_release_row(columns, ("stable_version", "stable_signature"))
    for suffix in ("", *sidecars):
        staged.with_name(staged.name + suffix).unlink(missing_ok=True)
    return {
        "ok": True, "channel": _CI_CHANNEL, "version": value.version,
        "format": value.format, "stable_version": value.stable_version,
        "artifact_key": artifact_key, "sha256": digest, "size": size,
        "evidence_complete": value.evidence is not None,
        "stored": [str(target.relative_to(_store_dir())) for _, target in moved],
    }




async def _receive_chunk(
    request: Request, part: Path, safe: str, offset: int, complete: bool
) -> dict[str, Any]:
    """Append one chunk at ``offset``, and finalize when ``complete`` is set."""
    if offset < 0:
        raise HTTPException(422, "offset must not be negative")
    current = part.stat().st_size if part.exists() else 0
    if offset != current:
        raise HTTPException(
            409, f"chunk offset {offset} does not continue the staged {current} bytes"
        )
    size = current
    with part.open("r+b" if current else "wb") as out:
        out.seek(current)
        async for chunk in request.stream():
            size += len(chunk)
            if size > RELEASE_UPLOAD_BYTES:
                raise HTTPException(413, "release artifact exceeds the 2 GiB ceiling")
            out.write(chunk)
        out.flush()
        os.fsync(out.fileno())
    if not complete:
        return {"ok": True, "name": safe, "received": size, "complete": False}
    digest = hashlib.sha256()
    with part.open("rb") as staged:
        for block in iter(lambda: staged.read(1024 * 1024), b""):
            digest.update(block)
    os.replace(part, part.with_name(safe))
    return {
        "ok": True, "name": safe, "size": size, "sha256": digest.hexdigest(),
        "complete": True,
    }


@router.put("/api/releases/ci/upload/{name}")
async def ci_upload_artifact(
    name: str,
    request: Request,
    offset: int | None = None,
    complete: bool = False,
) -> dict[str, Any]:
    """Stream a CI artifact into ``incoming/`` ahead of publication.

    The Windows/macOS runners cannot reach the gateway host's LAN address (the
    networks do not route), so artifacts arrive here over the public endpoint
    instead of scp. The body streams to a ``.part`` file and is atomically
    renamed into place, so ci/publish never sees a torn upload; nothing in this
    handler is trusted — publish re-digests the bytes from disk before anything
    moves into the store.

    ``offset`` uploads one chunk at that byte position instead of the whole
    artifact, and ``complete=1`` renames the assembled ``.part`` into place.
    That exists because the public origin sits behind a proxy that answers 413
    for bodies over 100 MB, and a Windows installer is ~300 MB. The offset must
    equal the current ``.part`` size, so a retried or interleaved chunk cannot
    silently corrupt an artifact, and only the finalizing call reports a digest.
    """
    client = request_network(request)
    _ci_by_ip.enforce(client, "ci release calls")
    _require_publish_token(request)
    safe = _safe_name(name)
    target = _incoming_dir() / (safe + ".part")
    target.parent.mkdir(parents=True, exist_ok=True)
    if offset is not None:
        return await _receive_chunk(request, target, safe, offset, complete)
    digest = hashlib.sha256()
    size = 0
    try:
        with target.open("wb") as out:
            async for chunk in request.stream():
                size += len(chunk)
                if size > RELEASE_UPLOAD_BYTES:
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
async def ci_report_status(request: Request) -> dict[str, Any]:
    _ci_by_ip.enforce(request_network(request), "ci release calls")
    _require_publish_token(request)
    value = await _publication_body(request, CiStatus)
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
    client = request_network(request)
    _ci_by_ip.enforce(client, "ci release calls")
    _require_publish_token(request)
    return {"builds": _recent_ci_builds(limit, commit_sha, status)}


@router.get("/api/releases/base-version")
async def release_base_version(request: Request) -> dict[str, str]:
    """Version the next nightly beta derives from: highest published stable
    patch + 1, floored at 0.1.0 before the first stable ever ships."""
    client = request_network(request)
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
    client = request_network(request)
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
                   ({_EVIDENCE_COMPLETE_SQL}) AS evidence_complete,
                   (channel = 'beta' AND stable_version IS NOT NULL
                    AND stable_signature IS NOT NULL AND ({_EVIDENCE_COMPLETE_SQL})
                    AND (platform = 'linux' OR native_publisher_verified)) AS promotable
              FROM releases
             ORDER BY published_at DESC, channel, platform, arch, format
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
    format: Format = DEFAULT_FORMAT,
) -> dict[str, Any]:
    _require_admin(request)
    _parse_prerelease_version(version, "version")
    with tx() as cur:
        cur.execute(
            """
            DELETE FROM releases
             WHERE channel = %s AND version = %s AND platform = %s AND arch = %s
               AND format = %s
            """,
            (channel, version, platform, arch, format),
        )
        deleted = cur.rowcount
    if not deleted:
        raise HTTPException(404, "release manifest not found")
    return {"ok": True}


def _fetch_release_row(
    channel: str, version: str, platform: str, arch: str, fmt: str = DEFAULT_FORMAT
) -> dict[str, Any] | None:
    with tx() as cur:
        cur.execute(
            """
            SELECT * FROM releases
             WHERE channel = %s AND version = %s AND platform = %s AND arch = %s
               AND format = %s
            """,
            (channel, version, platform, arch, fmt),
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
    with _publication_lock():
        return _promote_release(value)


def _promote_release(value: PromoteRelease) -> dict[str, Any]:
    if value.channel != _CI_CHANNEL:
        raise HTTPException(422, "only beta releases can be promoted")
    _parse_prerelease_version(value.version, "version")
    row = _fetch_release_row(
        _CI_CHANNEL, value.version, value.platform, value.arch, value.format
    )
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
    if _release_exists(
        "stable", stable_version, value.platform, value.arch, value.format
    ):
        raise HTTPException(409, "stable release identity is already registered")

    # The promoted row keeps the beta's format — the bytes are the same bytes,
    # so a .deb beta promotes to a .deb stable and never to the AppImage.
    stable_name = _safe_name(
        _canonical_artifact(stable_version, value.platform, value.arch, value.format)
    )
    stable_key = f"stable/{stable_version}/{stable_name}"
    source = _store_path(str(row["artifact_key"]))
    destination = _store_path(stable_key)
    if not source.is_file():
        raise HTTPException(
            409, "beta artifact bytes are not present on this host; cannot promote"
        )
    with capture_bundle(source) as artifact:
        parsed = verify_bundle(artifact, row, _RELEASE_PUBLIC_KEYS)
        native_publisher_verified(parsed, row, _RELEASE_PUBLIC_KEYS, required=True)
        destination.parent.mkdir(parents=True, exist_ok=True)
        for suffix in _ARTIFACT_SUFFIXES:
            sidecar = artifact.with_name(artifact.name + suffix)
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
                           "format", "key_id", "sha256", "size", "signature",
                           "notes_url", "artifact_key", "commit_sha")
        },
    }


# Retention.
#
# Measured, not estimated. The live 0.1.1 manifests give the AppImage as
# 340 724 216 B, the Windows installer 239 491 858 B and the macOS zip
# 121 394 445 B — 669 MiB, ~0.70 GB, of artifact per nightly today. The Linux
# packaging build measures a .deb at 0.293x and an .rpm at 0.315x the AppDir
# payload, which against the real payload is 340-410 MiB of NEW bytes per
# nightly on top of the AppImage; a .tar.zst sysroot, if it also publishes
# nightly, adds roughly another AppImage. So a nightly goes from ~0.70 GB to
# ~1.0 GB (deb + rpm) or ~1.35 GB (all four Linux formats).
#
# Production's filesystem is 97% used with 3.9 GB free and ~6.8 GB already in
# chudware-releases. Unbounded, that is about ten nights before the disk is full
# and takes the gateway, PostgreSQL and the tunnel with it — so retention is not
# an admin convenience, it runs on a daily systemd timer
# (Webservice/deploy/chudware-release-retention.timer).
#
# N = 3 beta versions per (platform, arch, format), re-checked now that a Linux
# platform has four formats instead of one. The bound is per tuple, so four
# formats means four times the directories at the same N — that is the intended
# behaviour, not an oversight: the .deb's history is as useful to a .deb user as
# the AppImage's is to an AppImage user, and keeping the newest of one format
# while dropping another would make "roll back one build" work for some users
# and not others. Three is the smallest count that stays honest: the newest is
# what /api/releases/latest serves, the one before it is what a tester rolls
# back to when the newest is broken, and the third is slack for a night when CI
# publishes twice. It bounds beta at ~3 GB (deb + rpm) to ~4 GB (all four),
# which fits the disk once the current unbounded history is pruned.
#
# Stable is deliberately NOT bounded here: a customer who pinned 0.1.0 must
# still be able to download 0.1.0. That pile grows ~1 GB per SHIPPED release
# rather than per night, and capping it is a product decision about how long an
# old version stays downloadable, not something this function should decide.
BETA_RETENTION_VERSIONS = 3

# An artifact CI staged but never published — a 422 on a digest mismatch, a run
# cancelled between upload and publish, an abandoned `.part` from a chunked
# upload — sits in incoming/ forever. It is a second unbounded pile, and nothing
# was watching it. A day of grace is generous: publication follows an upload
# within one workflow run, minutes apart.
INCOMING_ORPHAN_AGE_SECONDS = 24 * 60 * 60


def _prunable_beta_rows(keep: int) -> list[dict[str, Any]]:
    """Beta rows older than the newest *keep* per (platform, arch, format).

    Stable is never a candidate. A customer who pinned 0.1.0 must still be able
    to download 0.1.0, and a stable artifact is the only copy of its bytes —
    promotion copied them out of the beta directory, it did not link them.
    """
    with tx() as cur:
        cur.execute(
            """
            SELECT channel, version, platform, arch, format, artifact_key, size
              FROM releases
             WHERE channel = %s
             ORDER BY platform, arch, format, published_at DESC, version DESC
            """,
            (_CI_CHANNEL,),
        )
        rows = [dict(row) for row in cur.fetchall()]
    seen: dict[tuple[str, str, str], int] = {}
    prunable: list[dict[str, Any]] = []
    for row in rows:
        tuple_key = (str(row["platform"]), str(row["arch"]), str(row["format"]))
        rank = seen.get(tuple_key, 0)
        seen[tuple_key] = rank + 1
        if rank >= keep:
            prunable.append(row)
    return prunable


def _published_artifact_names() -> set[str]:
    """Basename of every published artifact_key, for the incoming/ sweep."""
    with tx() as cur:
        cur.execute("SELECT artifact_key FROM releases")
        return {
            PurePosixPath(str(row["artifact_key"])).name for row in cur.fetchall()
        }


def _incoming_orphans(now: float | None = None) -> list[Path]:
    """Staged files older than a day that no release row accounts for.

    An artifact stays in incoming/ only between its upload and its publication —
    minutes, inside one workflow run — because ci/publish MOVES it into store/.
    Anything still here a day later is the residue of a failed publish (a 422 on
    a digest mismatch, a cancelled run, an abandoned ``.part`` from a chunked
    upload) and nothing will ever come back for it.

    The age bound is what makes this safe rather than a race with a live upload:
    a run whose publish is still pending is minutes old, not a day.
    """
    incoming = _incoming_dir()
    if not incoming.is_dir():
        return []
    cutoff = (time.time() if now is None else now) - INCOMING_ORPHAN_AGE_SECONDS
    published = _published_artifact_names()
    orphans: list[Path] = []
    for path in sorted(incoming.iterdir()):
        if not path.is_file():
            continue
        try:
            if path.stat().st_mtime > cutoff:
                continue
        except OSError:
            continue
        # A sidecar is <artifact><suffix>; strip the suffix before matching so a
        # published artifact's sidecars are recognised as accounted for too.
        stem = path.name
        for suffix in _ARTIFACT_SUFFIXES:
            if suffix and stem.endswith(suffix):
                stem = stem[: -len(suffix)]
                break
        if stem not in published:
            orphans.append(path)
    return orphans


def prune_release_store(
    *, dry_run: bool = False, keep: int = BETA_RETENTION_VERSIONS
) -> dict[str, Any]:
    """Drop superseded beta publications, their bytes, and staging orphans.

    Idempotent: a second call with nothing newly superseded reports zero of
    everything and touches nothing.

    Rows are deleted FIRST, in one transaction, and bytes second. That order is
    the safe one — a row whose bytes are gone would advertise a download that
    404s, while bytes whose row is gone are simply unreachable, because the
    artifact route requires an exact `releases.artifact_key` match before it
    opens anything. This is also why nothing here can delete an artifact a live
    row still points at: the only files it unlinks are the ones derived from the
    basename of a key whose row it just removed, and `artifact_key` is UNIQUE.

    There is deliberately NO orphan sweep over `store/`. ci/publish moves bytes
    into place BEFORE inserting its row, so a file with no row may be a
    publication in flight, and only directories this call emptied itself are
    removed — for the same reason. ``incoming/`` is different and IS swept: a
    file there is by definition unpublished, and the one-day age bound separates
    residue from an upload in flight.
    """
    keep = max(1, int(keep))
    prunable = _prunable_beta_rows(keep)
    orphans = _incoming_orphans()
    report: dict[str, Any] = {
        "ok": True,
        "dry_run": bool(dry_run),
        "keep_beta_versions": keep,
        "pruned": [],
        "deleted_files": 0,
        "freed_bytes": 0,
        "incoming_orphans": [],
        "incoming_freed_bytes": 0,
        "errors": [],
    }
    for path in orphans:
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        report["incoming_orphans"].append({"name": path.name, "bytes": size})
        report["incoming_freed_bytes"] += size
    if not prunable and not orphans:
        return report

    # Resolve and measure before deleting anything, so a dry run reports the
    # same numbers a real run would free.
    plans: list[tuple[dict[str, Any], list[Path]]] = []
    for row in prunable:
        artifact_key = str(row["artifact_key"])
        try:
            base = _store_path(artifact_key)
        except HTTPException:
            # A key that no longer maps onto the store layout (hand-edited row)
            # must not stop the sweep; its row is still superseded.
            report["errors"].append(f"{artifact_key}: not a store path")
            plans.append((row, []))
            continue
        files = [
            candidate
            for candidate in (base.with_name(base.name + s) for s in _ARTIFACT_SUFFIXES)
            if candidate.is_file()
        ]
        bytes_on_disk = 0
        for path in files:
            try:
                bytes_on_disk += path.stat().st_size
            except OSError:
                pass
        plans.append((row, files))
        report["pruned"].append(
            {
                "channel": str(row["channel"]),
                "version": str(row["version"]),
                "platform": str(row["platform"]),
                "arch": str(row["arch"]),
                "format": str(row["format"]),
                "artifact_key": artifact_key,
                "files": len(files),
                "bytes": bytes_on_disk,
            }
        )
        report["deleted_files"] += len(files)
        report["freed_bytes"] += bytes_on_disk

    if dry_run:
        return report

    keys = [str(row["artifact_key"]) for row in prunable]
    if keys:
        with tx() as cur:
            cur.execute("DELETE FROM releases WHERE artifact_key = ANY(%s)", (keys,))

    emptied: set[Path] = set()
    for _, files in plans:
        for path in files:
            try:
                path.unlink()
                emptied.add(path.parent)
            except OSError as exc:
                report["errors"].append(f"{path.name}: {exc.strerror or exc}")
    for directory in sorted(emptied, key=lambda p: len(p.parts), reverse=True):
        try:
            next(directory.iterdir())
        except StopIteration:
            try:
                directory.rmdir()
            except OSError as exc:
                report["errors"].append(f"{directory.name}/: {exc.strerror or exc}")
        except OSError as exc:
            report["errors"].append(f"{directory.name}/: {exc.strerror or exc}")
    for path in orphans:
        try:
            path.unlink()
        except OSError as exc:
            report["errors"].append(f"incoming/{path.name}: {exc.strerror or exc}")
    return report


@router.post("/api/admin/releases/prune")
async def prune_releases(request: Request, dry_run: bool = False) -> dict[str, Any]:
    """Apply the retention policy now. Idempotent; ``?dry_run=true`` reports
    exactly what a real run would remove and changes nothing."""
    _require_admin(request)
    return prune_release_store(dry_run=dry_run)


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
async def upload_release(request: Request) -> dict[str, Any]:
    _require_admin(request)
    # A role failure must not spool a multipart body to the gateway disk.
    async with bounded_form(request, RELEASE_UPLOAD_BYTES + SMALL_REQUEST_BYTES) as form:
        file = form.get("file")
        if not isinstance(file, UploadFile):
            raise HTTPException(422, "file required")
        return await _store_admin_upload(file)


async def _store_admin_upload(file: UploadFile) -> dict[str, Any]:
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
                if out.tell() + len(chunk) > RELEASE_UPLOAD_BYTES:
                    raise HTTPException(413, "release artifact exceeds the 2 GiB ceiling")
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
