"""Canonical signed payload manifests for customer release builders.

This module deliberately has no knowledge of a release private key.  Offline signing
consumes :func:`payload_signature_preimage`; finalization verifies a detached signature
against the explicitly supplied release public key before placing its canonical base64
sidecar beside the manifest.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import stat
import tempfile
import time
import unicodedata
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Callable, Iterable, Mapping, Sequence

MANIFEST_NAME = "payload-manifest.v1.json"
SIGNATURE_NAME = "payload-manifest.v1.sig"
SIGNING_DOMAIN = b"chudware-payload-v1\0"
SCHEMA = "chudware-payload-v1"
PRODUCT = "Chudware"
KINDS = frozenset({"executable", "library", "worker", "policy", "resource"})
_METADATA_NAMES = frozenset({MANIFEST_NAME, SIGNATURE_NAME})
_KEY_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_SOURCE_SUFFIXES = frozenset(
    {
        ".py",
        ".pyc",
        ".pyo",
        ".ts",
        ".tsx",
        ".js",
        ".jsx",
        ".mjs",
        ".cjs",
        ".map",
        ".pdb",
        ".dwo",
        ".dwp",
        ".dbg",
        ".sym",
        ".debug",
    }
)
_SOURCE_SUFFIXES_FOLDED = frozenset(item.casefold() for item in _SOURCE_SUFFIXES)
_REPOSITORY_PARTS = frozenset(
    {
        ".git",
        ".github",
        ".gitlab",
        ".svn",
        ".hg",
        ".gitignore",
        ".gitattributes",
        ".gitmodules",
    }
)
_TEST_PARTS = frozenset({"test", "tests", "testing", "__tests__"})


class PayloadManifestError(RuntimeError):
    """The staged customer payload is unsafe or changed during construction."""


@dataclass(frozen=True)
class FileSnapshot:
    path: str
    size: int
    sha256: str
    mode: int
    kind: str
    device: int
    inode: int
    mtime_ns: int
    ctime_ns: int

    def manifest_row(self) -> dict[str, object]:
        return {
            "path": self.path,
            "size": self.size,
            "sha256": self.sha256,
            "mode": self.mode,
            "kind": self.kind,
        }


def validate_key_id(key_id: str) -> str:
    if not _KEY_ID_RE.fullmatch(key_id):
        raise PayloadManifestError(
            "release key_id must be 1-64 ASCII letters, digits, dots, underscores, or hyphens"
        )
    return key_id


def normalize_payload_path(value: str | os.PathLike[str]) -> str:
    raw = unicodedata.normalize("NFC", os.fspath(value)).replace("\\", "/")
    if not raw or "\0" in raw:
        raise PayloadManifestError("payload path is empty or contains NUL")
    posix = PurePosixPath(raw)
    windows = PureWindowsPath(raw)
    if posix.is_absolute() or windows.is_absolute() or windows.drive:
        raise PayloadManifestError(f"absolute payload path is forbidden: {raw!r}")
    if any(part == ".." for part in posix.parts):
        raise PayloadManifestError(
            f"parent traversal is forbidden in payload path: {raw!r}"
        )
    normalized = posix.as_posix()
    if normalized in {"", "."}:
        raise PayloadManifestError(f"invalid payload path: {raw!r}")
    return normalized


def _is_link_or_reparse(info: os.stat_result) -> bool:
    # FILE_ATTRIBUTE_REPARSE_POINT (0x400) covers Windows junctions as well as symlinks.
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & 0x400
    )


def _root(root: Path) -> Path:
    try:
        root_lstat = root.lstat()
    except FileNotFoundError as exc:
        raise PayloadManifestError(f"payload root does not exist: {root}") from exc
    if _is_link_or_reparse(root_lstat) or not stat.S_ISDIR(root_lstat.st_mode):
        raise PayloadManifestError(
            f"payload root must be a real directory, not a symlink: {root}"
        )
    return root.resolve(strict=True)


def _checked_file(root: Path, relative: str) -> Path:
    current = root
    parts = PurePosixPath(relative).parts
    for index, part in enumerate(parts):
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError as exc:
            raise PayloadManifestError(f"manifest path is missing: {relative}") from exc
        if _is_link_or_reparse(info):
            raise PayloadManifestError(
                f"symlink is forbidden in payload path: {relative}"
            )
        if index != len(parts) - 1 and not stat.S_ISDIR(info.st_mode):
            raise PayloadManifestError(
                f"non-directory payload path component: {relative}"
            )
    if not stat.S_ISREG(info.st_mode):
        raise PayloadManifestError(f"payload entry is not a regular file: {relative}")
    return current


def discover_payload_files(root: Path, *, exclude_metadata: bool = True) -> list[str]:
    root = _root(root)
    found: list[str] = []

    def visit(directory: Path, prefix: str) -> None:
        with os.scandir(directory) as entries:
            ordered = sorted(
                entries, key=lambda entry: unicodedata.normalize("NFC", entry.name)
            )
        for entry in ordered:
            relative = normalize_payload_path(
                f"{prefix}/{entry.name}" if prefix else entry.name
            )
            info = entry.stat(follow_symlinks=False)
            if _is_link_or_reparse(info):
                raise PayloadManifestError(
                    f"symlink is forbidden in payload: {relative}"
                )
            if stat.S_ISDIR(info.st_mode):
                visit(Path(entry.path), relative)
            elif stat.S_ISREG(info.st_mode):
                if not exclude_metadata or relative not in _METADATA_NAMES:
                    found.append(relative)
            else:
                raise PayloadManifestError(
                    f"special file is forbidden in payload: {relative}"
                )

    visit(root, "")
    return found


def reject_forbidden_customer_files(root: Path) -> None:
    for relative in discover_payload_files(root, exclude_metadata=False):
        parts = PurePosixPath(relative).parts
        folded_parts = tuple(part.casefold() for part in parts)
        suffix = Path(parts[-1]).suffix.casefold()
        if any(part in _REPOSITORY_PARTS for part in folded_parts):
            raise PayloadManifestError(
                f"repository metadata is forbidden in customer payload: {relative}"
            )
        if any(
            part in _TEST_PARTS or part.startswith("test_") for part in folded_parts
        ):
            raise PayloadManifestError(
                f"test content is forbidden in customer payload: {relative}"
            )
        if suffix in _SOURCE_SUFFIXES_FOLDED or any(
            part.endswith(".dsym") for part in folded_parts
        ):
            raise PayloadManifestError(
                f"source/debug artifact is forbidden in customer payload: {relative}"
            )
        if any(part in {"prompts", "roles", "cheatsheets"} for part in folded_parts):
            raise PayloadManifestError(
                f"built-in prompt/role source is forbidden in customer payload: {relative}"
            )


def default_kind(relative: str) -> str:
    path = PurePosixPath(relative)
    name = path.name.casefold()
    suffix = path.suffix.casefold()
    if name in {
        "chudware-hostd",
        "chudware-hostd.exe",
        "chudware-worker",
        "chudware-worker.exe",
        "chudware-mcp",
        "chudware-mcp.exe",
    }:
        return "worker"
    if name in {"chud", "chud.exe"}:
        return "executable"
    if suffix in {".dll", ".so", ".dylib"} or ".so." in name:
        return "library"
    if suffix in {".exe", ".ps1", ".bat", ".cmd"} or name in {"chudware", "appimage"}:
        return "executable"
    if name.startswith("security-policy") or "policy" in (
        part.casefold() for part in path.parts[:-1]
    ):
        return "policy"
    return "resource"


# os.stat() and os.fstat() do not agree on Windows for the same file:
#   * st_dev/st_ino come from different APIs (the path form reports FileIdInfo,
#     the handle form the older 64-bit file index);
#   * st_ctime is a creation time there, and the two flavours can report
#     different ones for a freshly copied file. Measured on the release runner:
#     path 1787294547120924100 vs handle 1787293683876298200 for FreeImage.dll,
#     14 minutes apart and stable across retries (NTFS tunnels a creation time
#     onto a replaced name; the handle sees the tunneled value);
#   * st_mode's executable bits come from a PATH heuristic — CPython ORs 0o111
#     in for .exe/.bat/.cmd/.com — which os.fstat() cannot apply because it has
#     no name. Measured: mode=511 through the path, 438 through the handle for
#     chudware-hostd.exe.
# Comparing those fields ACROSS the two flavours therefore rejects payload files
# on Windows for reasons that have nothing to do with the bytes. Size and mtime
# still are, and handle-vs-handle comparisons — the ones that prove the hashed
# bytes did not move — stay strict on every platform.
_CROSS_FLAVOUR_IGNORED = ("dev", "ino", "ctime_ns", "mode") if os.name == "nt" else ()


def _identity_fields(value: os.stat_result) -> dict[str, object]:
    return {
        "dev": value.st_dev,
        "ino": value.st_ino,
        "size": value.st_size,
        "mode": stat.S_IMODE(value.st_mode),
        "mtime_ns": value.st_mtime_ns,
        "ctime_ns": value.st_ctime_ns,
    }


def _identity_mismatch(
    left: os.stat_result, right: os.stat_result, *, ignore: Sequence[str] = ()
) -> str:
    first, second = _identity_fields(left), _identity_fields(right)
    return ", ".join(
        f"{name}={first[name]!r}->{second[name]!r}"
        for name in first
        if name not in ignore and first[name] != second[name]
    )


def _same_identity(left: os.stat_result, right: os.stat_result) -> bool:
    return not _identity_mismatch(left, right)


def _same_open_identity(path_stat: os.stat_result, handle_stat: os.stat_result) -> str:
    """Empty string when a descriptor still refers to the file that was stat'd."""
    return _identity_mismatch(path_stat, handle_stat, ignore=_CROSS_FLAVOUR_IGNORED)


def _read_regular_file(path: Path, label: str) -> bytes:
    try:
        before = path.lstat()
        if _is_link_or_reparse(before) or not stat.S_ISREG(before.st_mode):
            raise PayloadManifestError(f"{label} is not a regular file: {path}")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise PayloadManifestError(
            f"cannot safely open {label}: {path}: {exc}"
        ) from exc
    content = bytearray()
    try:
        opened = os.fstat(descriptor)
        mismatch = _same_open_identity(before, opened)
        if mismatch:
            raise PayloadManifestError(
                f"{label} changed while opening: {path} ({mismatch})"
            )
        while chunk := os.read(descriptor, 1024 * 1024):
            content.extend(chunk)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    final = path.lstat()
    mismatch = _identity_mismatch(opened, after) or _same_open_identity(final, after)
    if mismatch:
        raise PayloadManifestError(
            f"{label} changed while reading: {path} ({mismatch})"
        )
    return bytes(content)


def _snapshot_file(
    root: Path, relative: str, kind: str, *, attempts: int = 4
) -> FileSnapshot:
    """Snapshot one payload file, retrying a transient metadata change.

    Every attempt is self-consistent on its own: the descriptor is proven to be
    the file that was stat'd, and the bytes hashed are proven unchanged
    afterwards. Retrying therefore weakens nothing — it only tolerates a file
    whose metadata was still settling when staging reached it. Windows does
    exactly that: a freshly copied DLL can report one timestamp through the path
    and another through the handle, which failed a byte-correct Windows release
    with "payload file changed while opening: FreeImage.dll".
    """
    last: PayloadManifestError | None = None
    for attempt in range(attempts):
        try:
            return _snapshot_file_once(root, relative, kind)
        except PayloadManifestError as exc:
            if "changed while" not in str(exc):
                raise
            last = exc
            if attempt + 1 < attempts:
                time.sleep(0.25 * (attempt + 1))
    assert last is not None
    raise last


def _snapshot_file_once(root: Path, relative: str, kind: str) -> FileSnapshot:
    if kind not in KINDS:
        raise PayloadManifestError(f"invalid payload kind {kind!r} for {relative}")
    path = _checked_file(root, relative)
    before = path.lstat()
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise PayloadManifestError(
            f"cannot safely open payload file: {relative}: {exc}"
        ) from exc
    digest = hashlib.sha256()
    try:
        opened = os.fstat(descriptor)
        mismatch = _same_open_identity(before, opened)
        if not stat.S_ISREG(opened.st_mode) or mismatch:
            raise PayloadManifestError(
                f"payload file changed while opening: {relative} ({mismatch or 'not a regular file'})"
            )
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    final = path.lstat()
    mismatch = _identity_mismatch(opened, after) or _same_open_identity(final, after)
    if mismatch:
        raise PayloadManifestError(
            f"payload file changed while hashing: {relative} ({mismatch})"
        )
    return FileSnapshot(
        path=relative,
        size=after.st_size,
        sha256=digest.hexdigest(),
        mode=stat.S_IMODE(after.st_mode),
        kind=kind,
        device=after.st_dev,
        inode=after.st_ino,
        mtime_ns=after.st_mtime_ns,
        ctime_ns=after.st_ctime_ns,
    )


def snapshot_payload(
    root: Path,
    paths: Iterable[str | os.PathLike[str]],
    *,
    kind_for_path: Callable[[str], str] = default_kind,
) -> tuple[FileSnapshot, ...]:
    root = _root(root)
    normalized: list[str] = []
    seen: dict[str, str] = {}
    for value in paths:
        relative = normalize_payload_path(value)
        collision_key = relative.casefold()
        if collision_key in seen:
            raise PayloadManifestError(
                f"duplicate normalized payload path: {seen[collision_key]!r} and {relative!r}"
            )
        seen[collision_key] = relative
        normalized.append(relative)
    return tuple(
        _snapshot_file(root, relative, kind_for_path(relative))
        for relative in sorted(normalized)
    )


def verify_snapshot(
    root: Path,
    snapshots: Sequence[FileSnapshot],
    *,
    require_exact_files: bool = True,
    exclude_metadata: bool = True,
) -> None:
    root = _root(root)
    expected = {snapshot.path: snapshot for snapshot in snapshots}
    if len(expected) != len(snapshots):
        raise PayloadManifestError("snapshot contains duplicate paths")
    if require_exact_files:
        actual = set(discover_payload_files(root, exclude_metadata=exclude_metadata))
        if actual != set(expected):
            missing = sorted(set(expected) - actual)
            added = sorted(actual - set(expected))
            raise PayloadManifestError(
                f"payload file set changed; missing={missing}, added={added}"
            )
    for relative, original in expected.items():
        current = _snapshot_file(root, relative, original.kind)
        if current != original:
            raise PayloadManifestError(
                f"payload file changed after manifest hashing: {relative}"
            )


def _created_at(value: str | None, snapshots: Sequence[FileSnapshot]) -> str:
    if value is None:
        source_epoch = os.environ.get("SOURCE_DATE_EPOCH")
        if source_epoch is not None:
            try:
                epoch = int(source_epoch)
            except ValueError as exc:
                raise PayloadManifestError(
                    "SOURCE_DATE_EPOCH must be an integer"
                ) from exc
        else:
            epoch = max(
                (snapshot.mtime_ns // 1_000_000_000 for snapshot in snapshots),
                default=0,
            )
        value = datetime.fromtimestamp(epoch, tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError as exc:
        raise PayloadManifestError(
            "created_at must use canonical UTC seconds: YYYY-MM-DDTHH:MM:SSZ"
        ) from exc
    if parsed.strftime("%Y-%m-%dT%H:%M:%SZ") != value:
        raise PayloadManifestError("created_at is not canonical")
    return value


def build_manifest(
    root: Path,
    *,
    version: str,
    platform: str,
    arch: str,
    key_id: str,
    created_at: str | None = None,
    paths: Iterable[str | os.PathLike[str]] | None = None,
    kind_for_path: Callable[[str], str] = default_kind,
) -> tuple[dict[str, object], bytes, tuple[FileSnapshot, ...]]:
    if not version or not platform or not arch:
        raise PayloadManifestError("version, platform, and arch are required")
    root = _root(root)
    reject_forbidden_customer_files(root)
    relative_paths = discover_payload_files(root) if paths is None else list(paths)
    snapshots = snapshot_payload(root, relative_paths, kind_for_path=kind_for_path)
    manifest: dict[str, object] = {
        "schema": SCHEMA,
        "product": PRODUCT,
        "version": version,
        "platform": platform,
        "arch": arch,
        "created_at": _created_at(created_at, snapshots),
        "key_id": validate_key_id(key_id),
        "files": [snapshot.manifest_row() for snapshot in snapshots],
    }
    canonical = canonical_manifest_bytes(manifest)
    return manifest, canonical, snapshots


def canonical_manifest_bytes(manifest: Mapping[str, object]) -> bytes:
    expected = {
        "schema",
        "product",
        "version",
        "platform",
        "arch",
        "created_at",
        "key_id",
        "files",
    }
    if set(manifest) != expected:
        raise PayloadManifestError(f"manifest keys must be exactly {sorted(expected)}")
    if manifest["schema"] != SCHEMA or manifest["product"] != PRODUCT:
        raise PayloadManifestError("manifest schema/product mismatch")
    for field in ("version", "platform", "arch"):
        if not isinstance(manifest[field], str) or not manifest[field]:
            raise PayloadManifestError(f"manifest {field} must be a non-empty string")
    if not isinstance(manifest["created_at"], str):
        raise PayloadManifestError("manifest created_at must be a string")
    _created_at(manifest["created_at"], ())
    if not isinstance(manifest["key_id"], str):
        raise PayloadManifestError("manifest key_id must be a string")
    validate_key_id(manifest["key_id"])
    files = manifest["files"]
    if not isinstance(files, list):
        raise PayloadManifestError("manifest files must be a list")
    paths: list[str] = []
    for row in files:
        if not isinstance(row, dict) or set(row) != {
            "path",
            "size",
            "sha256",
            "mode",
            "kind",
        }:
            raise PayloadManifestError("manifest file rows have an invalid schema")
        if not isinstance(row["path"], str):
            raise PayloadManifestError("manifest file path must be a string")
        relative = normalize_payload_path(row["path"])
        if (
            relative != row["path"]
            or not isinstance(row["kind"], str)
            or row["kind"] not in KINDS
        ):
            raise PayloadManifestError(
                f"manifest file row is not canonical: {relative}"
            )
        if type(row["size"]) is not int or row["size"] < 0:
            raise PayloadManifestError(f"invalid size for manifest file: {relative}")
        if type(row["mode"]) is not int or not 0 <= row["mode"] <= 0o7777:
            raise PayloadManifestError(f"invalid mode for manifest file: {relative}")
        if not isinstance(row["sha256"], str) or not re.fullmatch(
            r"[0-9a-f]{64}", row["sha256"]
        ):
            raise PayloadManifestError(f"invalid SHA-256 for manifest file: {relative}")
        paths.append(relative)
    if paths != sorted(paths) or len({path.casefold() for path in paths}) != len(paths):
        raise PayloadManifestError(
            "manifest file paths must be sorted and normalized-unique"
        )
    return json.dumps(
        manifest,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def payload_signature_preimage(key_id: str, canonical_json: bytes) -> bytes:
    validate_key_id(key_id)
    return SIGNING_DOMAIN + key_id.encode("ascii") + b"\0" + canonical_json


def write_manifest(root: Path, canonical_json: bytes) -> Path:
    parsed = json.loads(canonical_json.decode("utf-8"))
    if canonical_manifest_bytes(parsed) != canonical_json:
        raise PayloadManifestError(
            "refusing to write non-canonical payload manifest bytes"
        )
    target = _root(root) / MANIFEST_NAME
    _atomic_write(target, canonical_json)
    return target


def read_manifest(root: Path) -> tuple[dict[str, object], bytes]:
    canonical_json = _read_regular_file(_root(root) / MANIFEST_NAME, "payload manifest")
    try:
        manifest = json.loads(canonical_json.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise PayloadManifestError("payload manifest is not UTF-8 JSON") from exc
    if canonical_manifest_bytes(manifest) != canonical_json:
        raise PayloadManifestError("payload manifest bytes are not canonical")
    return manifest, canonical_json


def read_signature(path: Path) -> bytes:
    try:
        text = _read_regular_file(path, "payload signature").decode("ascii")
    except UnicodeError as exc:
        raise PayloadManifestError(f"payload signature is not ASCII: {path}") from exc
    if text.endswith("\r\n"):
        encoded = text[:-2]
    elif text.endswith("\n"):
        encoded = text[:-1]
    else:
        encoded = text
    if not encoded or encoded.strip() != encoded or "\n" in encoded or "\r" in encoded:
        raise PayloadManifestError(
            "payload signature sidecar must contain one canonical base64 line"
        )
    try:
        signature = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise PayloadManifestError("payload signature is not standard base64") from exc
    if len(signature) != 64 or base64.b64encode(signature).decode("ascii") != encoded:
        raise PayloadManifestError(
            "payload signature must be one canonical Ed25519 signature"
        )
    return signature


def verify_manifest_payload(
    root: Path,
    canonical_json: bytes,
) -> tuple[dict[str, object], tuple[FileSnapshot, ...]]:
    """Verify exact canonical manifest bytes and every owned file under ``root``."""
    try:
        manifest = json.loads(canonical_json.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise PayloadManifestError("payload manifest is not UTF-8 JSON") from exc
    if canonical_manifest_bytes(manifest) != canonical_json:
        raise PayloadManifestError("payload manifest bytes are not canonical")
    rows = manifest["files"]
    kinds = {str(row["path"]): str(row["kind"]) for row in rows}
    snapshots = snapshot_payload(
        root,
        [str(row["path"]) for row in rows],
        kind_for_path=lambda relative: kinds[relative],
    )
    actual_rows = [snapshot.manifest_row() for snapshot in snapshots]
    if actual_rows != rows:
        raise PayloadManifestError("payload files do not match the signed manifest")
    verify_snapshot(root, snapshots, require_exact_files=True, exclude_metadata=True)
    return manifest, snapshots


def _load_public_key(value: str):
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError as exc:
        raise PayloadManifestError(
            "cryptography is required to finalize a signed payload; install release dependencies"
        ) from exc
    candidate = Path(value)
    try:
        data = (
            _read_regular_file(candidate, "release public key")
            if candidate.is_file()
            else value.encode("ascii")
        )
    except (OSError, UnicodeError) as exc:
        raise PayloadManifestError(
            "release public key must be a readable PEM file or canonical base64"
        ) from exc
    if data.startswith(b"-----BEGIN"):
        try:
            key = serialization.load_pem_public_key(data)
        except (TypeError, ValueError) as exc:
            raise PayloadManifestError("release public key PEM is invalid") from exc
        if not isinstance(key, Ed25519PublicKey):
            raise PayloadManifestError("release public key is not Ed25519")
        return key
    try:
        encoded = data.decode("ascii").strip()
        raw = base64.b64decode(encoded, validate=True)
    except (UnicodeError, ValueError, binascii.Error) as exc:
        raise PayloadManifestError(
            "release public key is not canonical base64"
        ) from exc
    if len(raw) != 32 or base64.b64encode(raw).decode("ascii") != encoded:
        raise PayloadManifestError(
            "release public key must encode exactly 32 Ed25519 bytes"
        )
    return Ed25519PublicKey.from_public_bytes(raw)


def verify_signature(
    *,
    key_id: str,
    canonical_json: bytes,
    signature_path: Path,
    public_key: str,
) -> bytes:
    signature = read_signature(signature_path)
    preimage = payload_signature_preimage(key_id, canonical_json)
    try:
        _load_public_key(public_key).verify(signature, preimage)
    except PayloadManifestError:
        raise
    except Exception as exc:
        raise PayloadManifestError(
            "payload signature does not verify for manifest/key_id"
        ) from exc
    return signature


def finalize_signature(
    root: Path,
    *,
    key_id: str,
    canonical_json: bytes,
    signature_path: Path,
    public_key: str,
) -> Path:
    signature = verify_signature(
        key_id=key_id,
        canonical_json=canonical_json,
        signature_path=signature_path,
        public_key=public_key,
    )
    target = _root(root) / SIGNATURE_NAME
    _atomic_write(target, base64.b64encode(signature) + b"\n")
    return target


def write_signing_preimage(path: Path, key_id: str, canonical_json: bytes) -> Path:
    _atomic_write(path, payload_signature_preimage(key_id, canonical_json))
    return path


def _atomic_write(target: Path, content: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=target.name + ".", dir=target.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def create_verified_zip(root: Path, destination: Path) -> Path:
    """Archive exactly the snapshotted bytes and reject concurrent staging mutations."""
    root = _root(root)
    relative_paths = discover_payload_files(root, exclude_metadata=False)
    snapshots = snapshot_payload(root, relative_paths)
    snapshot_by_path = {snapshot.path: snapshot for snapshot in snapshots}
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise PayloadManifestError(
            f"refusing to replace existing archive: {destination}"
        )
    temporary = destination.with_name(destination.name + ".tmp")
    if temporary.exists():
        raise PayloadManifestError(
            f"stale temporary archive must be removed explicitly: {temporary}"
        )
    try:
        with zipfile.ZipFile(
            temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
        ) as archive:
            for relative in sorted(snapshot_by_path):
                snapshot = snapshot_by_path[relative]
                source = _checked_file(root, relative)
                timestamp = max(snapshot.mtime_ns // 1_000_000_000, 315532800)
                info = zipfile.ZipInfo(
                    f"{root.name}/{relative}",
                    date_time=time.gmtime(timestamp)[:6],
                )
                info.create_system = 3
                info.external_attr = (stat.S_IFREG | snapshot.mode) << 16
                flags = (
                    os.O_RDONLY
                    | getattr(os, "O_BINARY", 0)
                    | getattr(os, "O_NOFOLLOW", 0)
                )
                descriptor = os.open(source, flags)
                digest = hashlib.sha256()
                try:
                    before = os.fstat(descriptor)
                    with archive.open(info, "w") as output:
                        while chunk := os.read(descriptor, 1024 * 1024):
                            output.write(chunk)
                            digest.update(chunk)
                    after = os.fstat(descriptor)
                finally:
                    os.close(descriptor)
                if (
                    not _same_identity(before, after)
                    or after.st_size != snapshot.size
                    or digest.hexdigest() != snapshot.sha256
                ):
                    raise PayloadManifestError(
                        f"payload mutated during archive creation: {relative}"
                    )
        verify_snapshot(
            root, snapshots, require_exact_files=True, exclude_metadata=False
        )
        os.replace(temporary, destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return destination
