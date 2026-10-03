"""Authoritative removable-media inventory and managed G-code publishing.

A printer's file menu shows bare names with no timestamps, so a drive that holds two
generations of a part is a print waiting to go wrong. This module owns the two operations
that keep removable print media unambiguous:

``inventory_print_media``
    Read-only walk of the real file tree (never manifest-only, never following symlinks):
    every file and directory with sizes, SHA-256, printable flags, duplicate groups, managed
    ``current``/``superseded``/``modified`` status, unknown printables, interrupted
    publishes, and a whole-drive ``inventory_token`` that is independent of pagination.

``publish_print_media``
    Replaces a project's COMPLETE print set as one revision under ``PRINT/<PROJECT>/``.
    Every file is named ``R<rrr><pp>_<PART>.<ext>`` so its first six characters (which
    survive FAT 8.3 short-name truncation) identify release and part. Sources must be
    ``print_ready`` slice_part packets whose G-code hashes match their manifest. The
    previous managed revision (hash-verified) and caller-reviewed ``retire_paths`` are
    archived into a non-printable ZIP and verified BEFORE anything leaves the printer's
    view; new files are staged with a non-printable suffix and read back. A transaction
    journal makes every swap reversible: controlled failures roll back in-process, and an
    interrupted publish is reported by inventory and recovered (explicitly) by the next
    publish. Unrecognized files are never touched.

Layout on the drive::

    PRINT/<PROJECT>/R00201_UPPER_ARM.gcode     current printable jobs
    PRINT/<PROJECT>/chudware_manifest.json     managed sidecar manifest
    PRINT/<PROJECT>/README.txt                 human-readable mapping
    CHUDWARE/ARCHIVE/<PROJECT>/<PROJECT>_RETIRED_BY_R002.zip   retired toolpaths
    CHUDWARE/JOURNAL.json                      present only while a publish is pending
    CHUDWARE/.staging/<txn>/*.part             transient, never printable, never in tokens

Public functions return ``{"ok": True, ...}`` or ``{"ok": False, "error": code,
"message": ...}``; they never raise.
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import errno
import hashlib
import json
import os
import re
import shutil
import stat
import time
import uuid
import zipfile
from pathlib import Path
from typing import Any, Iterator, Optional

try:  # POSIX (Linux/macOS): the drive lock is an flock on the mount root directory itself.
    import fcntl
except ImportError:  # Windows: msvcrt byte-range lock on CHUDWARE/.lock (token-excluded)
    fcntl = None  # type: ignore[assignment]
try:
    import msvcrt
except ImportError:
    msvcrt = None  # type: ignore[assignment]

SCHEMA_MEDIA_MANIFEST = "chudware.print_media/1"
SCHEMA_JOURNAL = "chudware.print_media_txn/1"
SCHEMA_SOURCE_JOB = "chudware.print_job/1"
SCHEMA_INVENTORY_TOKEN = "chudware.print_media_inventory/1"

PRINTABLE_SUFFIXES = frozenset({".gcode", ".gco", ".g", ".bgcode"})
PRINT_DIR = "PRINT"
META_DIR = "CHUDWARE"
STAGING_DIR = f"{META_DIR}/.staging"
LOCK_FILE = f"{META_DIR}/.lock"
JOURNAL_PATH = f"{META_DIR}/JOURNAL.json"
ARCHIVE_DIR = f"{META_DIR}/ARCHIVE"
MANIFEST_NAME = "chudware_manifest.json"
README_NAME = "README.txt"
STAGED_SUFFIX = ".part"

MAX_REVISION = 999
MAX_PARTS = 99
MAX_PAGE = 1000
UNRELATED_HASH_LIMIT = 64 * 1024 * 1024
METADATA_LIMIT = 4 * 1024 * 1024
SOURCE_MANIFEST_LIMIT = 16 * 1024 * 1024
LOCK_WAIT_S = 2.0
_CHUNK = 1024 * 1024

_PROJECT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,31}")
_SHA_RE = re.compile(r"[0-9a-f]{64}")
_MANAGED_NAME_RE = re.compile(r"R(\d{3})(\d{2})_", re.IGNORECASE)
_ARCHIVE_NAME_RE = re.compile(r"_RETIRED_BY_R(\d{3})\.zip", re.IGNORECASE)
_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL", PRINT_DIR, META_DIR}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)
_JOB_KEYS = frozenset({"part_id", "manifest_path", "gcode_path"})
_EVIDENCE_KEYS = ("slicer_warnings", "gcode_check", "printability")


class _Refusal(Exception):
    """A structured refusal; converted to ``{"ok": False, ...}`` at the public boundary."""

    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details

    def result(self) -> dict:
        return {"ok": False, "error": self.code, "message": self.message, **self.details}


# --------------------------------------------------------------------------------------
# Small filesystem helpers (module-level so tests can inject failures)
# --------------------------------------------------------------------------------------

def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z")


def _is_printable(name: str) -> bool:
    return os.path.splitext(name)[1].lower() in PRINTABLE_SUFFIXES


def _open_nofollow(path: Path) -> int:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_BINARY", 0)
    return os.open(path, flags)


def _hash_path(path: Path, *, keep_bytes: bool = False) -> tuple[str, int, Optional[bytes]]:
    """SHA-256 of a regular file without following a final symlink."""
    fd = _open_nofollow(path)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(errno.EINVAL, "not a regular file", str(path))
        digest = hashlib.sha256()
        size = 0
        chunks: Optional[list[bytes]] = [] if keep_bytes else None
        while chunk := os.read(fd, _CHUNK):
            digest.update(chunk)
            size += len(chunk)
            if chunks is not None:
                chunks.append(chunk)
        return digest.hexdigest(), size, (b"".join(chunks) if chunks is not None else None)
    finally:
        os.close(fd)


def _hash_or_none(path: Path) -> Optional[str]:
    try:
        return _hash_path(path)[0]
    except OSError:
        return None


def _fsync_dir(path: Path) -> None:
    with contextlib.suppress(OSError, AttributeError):
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _move(src: Path, dst: Path) -> None:
    """Same-filesystem rename; the only primitive that changes what the printer sees."""
    os.replace(src, dst)
    _fsync_dir(dst.parent)


def _lexists(path: Path) -> bool:
    try:
        os.lstat(path)
        return True
    except FileNotFoundError:
        return False


def _check_components(rootp: Path, rel: str) -> None:
    """Refuse if any existing component of ``rel`` (below the root) is a symlink."""
    current = rootp
    for part in rel.split("/"):
        current = current / part
        try:
            mode = os.lstat(current).st_mode
        except FileNotFoundError:
            return
        if stat.S_ISLNK(mode):
            raise _Refusal("media_symlink",
                           f"{current.relative_to(rootp).as_posix()} is a symlink; print media "
                           "mutations never traverse symlinks", path=rel)


def _ensure_dir(rootp: Path, rel: str, created: Optional[list[Path]] = None) -> Path:
    current = rootp
    for part in rel.split("/"):
        current = current / part
        try:
            mode = os.lstat(current).st_mode
        except FileNotFoundError:
            os.mkdir(current)
            if created is not None:
                created.append(current)
            continue
        if stat.S_ISLNK(mode):
            raise _Refusal("media_symlink", f"{current} is a symlink; refusing to write through it",
                           path=rel)
        if not stat.S_ISDIR(mode):
            raise _Refusal("media_path_conflict", f"{current} exists and is not a directory",
                           path=rel)
    return current


def _write_staged(path: Path, data: bytes) -> None:
    with open(path, "xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def _atomic_replace(rootp: Path, rel: str, data: bytes, tmp_dir: Path) -> None:
    _check_components(rootp, rel)
    tmp = tmp_dir / f"meta-{uuid.uuid4().hex[:12]}{STAGED_SUFFIX}"
    _write_staged(tmp, data)
    _move(tmp, rootp / rel)


def _drop_page_cache(fd: int) -> None:
    advise = getattr(os, "posix_fadvise", None)
    if advise is not None:
        with contextlib.suppress(OSError):
            advise(fd, 0, 0, os.POSIX_FADV_DONTNEED)


def _stage_copy(src: Path, dst: Path, expected_sha256: str) -> int:
    """Copy ``src`` to a new non-printable staged file, then read it back from the media."""
    digest = hashlib.sha256()
    size = 0
    with open(src, "rb") as fin, open(dst, "xb") as fout:
        while chunk := fin.read(_CHUNK):
            digest.update(chunk)
            fout.write(chunk)
            size += len(chunk)
        fout.flush()
        os.fsync(fout.fileno())
        _drop_page_cache(fout.fileno())
    if digest.hexdigest() != expected_sha256:
        raise _Refusal("source_changed",
                       f"{src} changed while it was being copied (sha256 {digest.hexdigest()} != "
                       f"manifest {expected_sha256}); slice again or restore the packet")
    readback = _hash_path(dst)[0]
    if readback != expected_sha256:
        raise _Refusal("staging_readback_mismatch",
                       f"the copy of {src.name} read back from the drive does not match "
                       f"(sha256 {readback}); the drive may be failing")
    return size


def _remove_tree(path: Path) -> None:
    try:
        mode = os.lstat(path).st_mode
    except FileNotFoundError:
        return
    if stat.S_ISDIR(mode):
        shutil.rmtree(path)
    else:
        os.unlink(path)


# --------------------------------------------------------------------------------------
# Argument validation
# --------------------------------------------------------------------------------------

def _root_path(root: Any) -> Path:
    if not isinstance(root, str) or not root.strip():
        raise _Refusal("root_invalid", "root must be the mounted drive path as a string")
    path = Path(os.path.abspath(os.path.expanduser(root)))
    try:
        mode = os.lstat(path).st_mode
    except FileNotFoundError:
        raise _Refusal("root_missing",
                       f"{path} does not exist; mount the drive first (the media root is never "
                       "created)", root=str(path)) from None
    except OSError as exc:
        raise _Refusal("root_unreadable", f"{path}: {exc.strerror or exc}", root=str(path)) from None
    if stat.S_ISLNK(mode):
        raise _Refusal("root_symlink", f"{path} is a symlink; pass the real mount point",
                       root=str(path))
    if not stat.S_ISDIR(mode):
        raise _Refusal("root_not_directory", f"{path} is not a directory", root=str(path))
    return path


def _project_folder(project: Any) -> str:
    if not isinstance(project, str) or not _PROJECT_RE.fullmatch(project):
        raise _Refusal("project_invalid",
                       "project must be 1-32 characters of A-Z, 0-9, '_' or '-' starting with a "
                       "letter or digit (it becomes the PRINT/<PROJECT> folder)",
                       project=project if isinstance(project, str) else None)
    folder = project.upper()
    if folder in _RESERVED_NAMES:
        raise _Refusal("project_invalid", f"{project!r} is a reserved name", project=project)
    return folder


def _normalize_rel(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value or "\\" in value:
        raise _Refusal("path_invalid", f"{field}: {value!r} is not a drive-relative POSIX path",
                       path=value if isinstance(value, str) else None)
    if value.startswith("/") or re.match(r"[A-Za-z]:", value):
        raise _Refusal("path_traversal",
                       f"{field}: {value!r} must be relative to the drive root (copy it from the "
                       "inventory)", path=value)
    parts = value.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise _Refusal("path_traversal", f"{field}: {value!r} escapes or is not normalized",
                       path=value)
    return "/".join(parts)


def _normalize_retire_paths(retire_paths: Any) -> list[str]:
    if retire_paths is None:
        return []
    if not isinstance(retire_paths, list):
        raise _Refusal("retire_paths_invalid", "retire_paths must be a list of inventory paths")
    out: list[str] = []
    for value in retire_paths:
        rel = _normalize_rel(value, "retire_paths")
        if rel not in out:
            out.append(rel)
    return out


def _slug(part_id: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "_", part_id).strip("_").upper()[:40].rstrip("_")
    return slug or "PART"


def _read_source_manifest(path: Path) -> dict:
    with open(path, "rb") as stream:
        data = stream.read(SOURCE_MANIFEST_LIMIT + 1)
    if len(data) > SOURCE_MANIFEST_LIMIT:
        raise ValueError("manifest is larger than 16 MiB")
    obj = json.loads(data.decode("utf-8"))
    if not isinstance(obj, dict):
        raise ValueError("manifest is not a JSON object")
    return {"obj": obj, "sha256": hashlib.sha256(data).hexdigest()}


def _validate_jobs(jobs: Any) -> list[dict]:
    if not isinstance(jobs, list) or not jobs:
        raise _Refusal("jobs_invalid",
                       "jobs must be a non-empty list: the COMPLETE desired print set for the "
                       "project")
    if len(jobs) > MAX_PARTS:
        raise _Refusal("part_limit", f"{len(jobs)} jobs exceed the {MAX_PARTS}-part limit of the "
                       "R<rrr><pp> naming scheme; split the project")
    errors: list[dict] = []
    sources: list[dict] = []
    seen: set[str] = set()
    for index, job in enumerate(jobs):
        def bad(code: str, message: str, **extra: Any) -> None:
            errors.append({"index": index, "error": code, "message": message, **extra})

        if not isinstance(job, dict):
            bad("job_invalid", "each job must be an object")
            continue
        unknown = sorted(set(job) - _JOB_KEYS)
        if unknown:
            bad("job_invalid", f"unknown job keys {unknown}; allowed: {sorted(_JOB_KEYS)}")
            continue
        part_id = job.get("part_id")
        if (not isinstance(part_id, str) or not part_id.strip() or len(part_id) > 128
                or any(ord(ch) < 32 for ch in part_id)):
            bad("part_id_invalid", "part_id must be a non-empty printable string of <=128 chars")
            continue
        if part_id in seen:
            bad("duplicate_part_id", f"part_id {part_id!r} appears more than once",
                part_id=part_id)
            continue
        seen.add(part_id)
        manifest_value = job.get("manifest_path")
        if not isinstance(manifest_value, str) or not manifest_value:
            bad("manifest_path_invalid", "manifest_path must name an existing .print.json",
                part_id=part_id)
            continue
        manifest_path = Path(os.path.abspath(os.path.expanduser(manifest_value)))
        try:
            loaded = _read_source_manifest(manifest_path)
        except (OSError, ValueError, UnicodeDecodeError) as exc:
            bad("manifest_unreadable", f"{manifest_path}: {exc}", part_id=part_id)
            continue
        manifest = loaded["obj"]
        evidence = {key: manifest.get(key) for key in _EVIDENCE_KEYS}
        if manifest.get("schema") != SCHEMA_SOURCE_JOB:
            bad("manifest_schema",
                f"{manifest_path} is not a {SCHEMA_SOURCE_JOB} packet (schema "
                f"{manifest.get('schema')!r}); slice with slice_part", part_id=part_id)
            continue
        if manifest.get("print_ready") is not True:
            bad("not_print_ready",
                f"{manifest_path} is not print_ready; fix the failing checks and slice again",
                part_id=part_id, evidence=evidence)
            continue
        expected = manifest.get("gcode_sha256")
        if not isinstance(expected, str) or not _SHA_RE.fullmatch(expected.lower()):
            bad("manifest_hash_missing", f"{manifest_path} has no valid gcode_sha256",
                part_id=part_id)
            continue
        expected = expected.lower()
        gcode_value = job.get("gcode_path") if job.get("gcode_path") is not None else \
            manifest.get("gcode")
        if not isinstance(gcode_value, str) or not gcode_value:
            bad("gcode_path_invalid", "no gcode_path override and the manifest names no G-code",
                part_id=part_id)
            continue
        gcode_path = Path(os.path.expanduser(gcode_value))
        if not gcode_path.is_absolute():
            gcode_path = manifest_path.parent / gcode_path
        gcode_path = Path(os.path.abspath(gcode_path))
        if not _is_printable(gcode_path.name):
            bad("gcode_not_printable",
                f"{gcode_path.name} is not a printable G-code file "
                f"({', '.join(sorted(PRINTABLE_SUFFIXES))})", part_id=part_id)
            continue
        try:
            if not gcode_path.is_file():
                raise FileNotFoundError(str(gcode_path))
            with open(gcode_path, "rb") as stream:
                digest = hashlib.sha256()
                size = 0
                while chunk := stream.read(_CHUNK):
                    digest.update(chunk)
                    size += len(chunk)
            actual = digest.hexdigest()
        except OSError as exc:
            bad("gcode_unreadable", f"{gcode_path}: {exc}", part_id=part_id)
            continue
        if actual != expected:
            bad("gcode_hash_mismatch",
                f"{gcode_path} sha256 {actual} does not match the manifest's {expected}; this "
                "is not the G-code that was verified", part_id=part_id,
                gcode_path=str(gcode_path), actual_sha256=actual, manifest_sha256=expected)
            continue
        sources.append({
            "part_id": part_id,
            "slug": _slug(part_id),
            "ext": gcode_path.suffix.lower(),
            "gcode_path": gcode_path,
            "sha256": expected,
            "size": size,
            "manifest_path": manifest_path,
            "manifest_sha256": loaded["sha256"],
            "part": manifest.get("part"),
            "printer": manifest.get("printer"),
            "warnings": evidence,
        })
    if errors:
        raise _Refusal("jobs_invalid",
                       f"{len(errors)} job(s) failed source verification; nothing was changed",
                       job_errors=errors)
    return sources


# --------------------------------------------------------------------------------------
# Inventory
# --------------------------------------------------------------------------------------

def _scan(rootp: Path) -> dict:
    records: list[dict] = []
    archives: list[dict] = []
    staging: list[str] = []

    def file_record(abs_path: str, parts: tuple[str, ...], st: os.stat_result) -> dict:
        rel = "/".join(parts)
        printable = _is_printable(parts[-1])
        rec: dict = {"path": rel, "type": "file", "size": st.st_size, "printable": printable,
                     "sha256": None}
        keep = rel == JOURNAL_PATH or (
            len(parts) == 3 and parts[0] == PRINT_DIR and parts[2] in (MANIFEST_NAME, README_NAME))
        must_hash = (printable or keep or parts[0] in (PRINT_DIR, META_DIR)
                     or st.st_size <= UNRELATED_HASH_LIMIT)
        if not must_hash:
            rec["hash_skipped"] = True
            rec["mtime_ns"] = st.st_mtime_ns
            return rec
        try:
            digest, size, data = _hash_path(
                Path(abs_path), keep_bytes=keep and st.st_size <= METADATA_LIMIT)
        except OSError as exc:
            return {"path": rel, "type": "unreadable", "printable": printable,
                    "error": exc.strerror or str(exc)}
        rec["sha256"] = digest
        rec["size"] = size
        if data is not None:
            rec["_data"] = data
        return rec

    def walk(directory: str, prefix: tuple[str, ...]) -> None:
        try:
            with os.scandir(directory) as iterator:
                entries = sorted(iterator, key=lambda entry: entry.name)
        except OSError as exc:
            records.append({"path": "/".join(prefix) or ".", "type": "unreadable",
                            "printable": False, "error": exc.strerror or str(exc)})
            return
        for entry in entries:
            parts = prefix + (entry.name,)
            rel = "/".join(parts)
            try:
                st = entry.stat(follow_symlinks=False)
            except OSError as exc:
                records.append({"path": rel, "type": "unreadable", "printable": False,
                                "error": exc.strerror or str(exc)})
                continue
            mode = st.st_mode
            if stat.S_ISLNK(mode):
                try:
                    target = os.readlink(entry.path)
                except OSError:
                    target = None
                records.append({"path": rel, "type": "symlink", "target": target,
                                "printable": _is_printable(entry.name)})
            elif stat.S_ISDIR(mode):
                if rel == STAGING_DIR:
                    for base, dirs, files in os.walk(entry.path, followlinks=False):
                        for name in sorted(dirs + files):
                            staging.append(
                                Path(base, name).relative_to(rootp).as_posix())
                    continue
                records.append({"path": rel, "type": "dir", "printable": False})
                walk(entry.path, parts)
            elif not stat.S_ISREG(mode):
                records.append({"path": rel, "type": "special", "printable": False})
            elif rel == LOCK_FILE:
                continue
            elif (len(parts) == 4 and parts[0] == META_DIR and parts[1] == "ARCHIVE"
                  and entry.name.lower().endswith(".zip")):
                archives.append({"path": rel, "project": parts[2], "size": st.st_size})
            else:
                records.append(file_record(entry.path, parts, st))

    walk(str(rootp), ())
    records.sort(key=lambda rec: rec["path"])
    return {"records": records, "archives": archives, "staging": sorted(staging)}


def _token(records: list[dict]) -> str:
    digest = hashlib.sha256(SCHEMA_INVENTORY_TOKEN.encode() + b"\n")
    for rec in records:
        kind = rec["type"]
        if kind == "file":
            content = rec["sha256"] or f"mtime:{rec.get('mtime_ns')}"
            signature = f"F|{rec['size']}|{content}"
        elif kind == "symlink":
            signature = f"L|{rec.get('target')}"
        elif kind == "unreadable":
            signature = f"U|{rec.get('error')}"
        elif kind == "dir":
            if rec["path"] == META_DIR or rec["path"].startswith(META_DIR + "/"):
                continue  # our own bookkeeping dirs (lock/archive/staging hosts); files still count
            signature = "D"
        else:
            signature = "S"
        digest.update(json.dumps([rec["path"], signature]).encode("utf-8") + b"\n")
    return digest.hexdigest()


def _check_sha(value: Any) -> bool:
    return isinstance(value, str) and bool(_SHA_RE.fullmatch(value))


def _validate_media_manifest(obj: Any, folder: str) -> dict:
    if not isinstance(obj, dict) or obj.get("schema") != SCHEMA_MEDIA_MANIFEST:
        raise ValueError(f"schema is not {SCHEMA_MEDIA_MANIFEST}")
    if obj.get("project") != folder:
        raise ValueError(f"project {obj.get('project')!r} does not match folder {folder!r}")
    revision = obj.get("revision")
    if isinstance(revision, bool) or not isinstance(revision, int) or not 1 <= revision <= 999:
        raise ValueError("revision is not an integer in 1..999")
    prefix = f"{PRINT_DIR}/{folder}/"

    def in_folder(path: Any) -> bool:
        if not isinstance(path, str) or not path.startswith(prefix):
            return False
        name = path[len(prefix):]
        return bool(name) and "/" not in name and "\\" not in name and name not in (".", "..")

    current = obj.get("current")
    if not isinstance(current, list):
        raise ValueError("current is not a list")
    for entry in current:
        if (not isinstance(entry, dict) or not isinstance(entry.get("part_id"), str)
                or not in_folder(entry.get("path")) or not _check_sha(entry.get("sha256"))):
            raise ValueError("a current entry lacks part_id/path/sha256 inside the folder")
    history = obj.get("history", [])
    if not isinstance(history, list):
        raise ValueError("history is not a list")
    for item in history:
        if not isinstance(item, dict) or not isinstance(item.get("files"), list):
            raise ValueError("history entry is malformed")
        rev = item.get("revision")
        if isinstance(rev, bool) or not isinstance(rev, int):
            raise ValueError("history revision is not an integer")
        for f in item["files"]:
            if not isinstance(f, dict) or not in_folder(f.get("path")) or not _check_sha(
                    f.get("sha256")):
                raise ValueError("history file entry is malformed")
    numbers = obj.get("part_numbers", {})
    if not isinstance(numbers, dict) or not all(
            isinstance(k, str) and isinstance(v, int) and not isinstance(v, bool)
            and 1 <= v <= MAX_PARTS for k, v in numbers.items()):
        raise ValueError("part_numbers is malformed")
    retired = obj.get("retired", [])
    if not isinstance(retired, list) or not all(
            isinstance(r, dict) and isinstance(r.get("path"), str) for r in retired):
        raise ValueError("retired is malformed")
    readme = obj.get("readme_sha256")
    if readme is not None and not _check_sha(readme):
        raise ValueError("readme_sha256 is malformed")
    return obj


def _load_projects(records: list[dict]) -> tuple[dict[str, dict], dict[str, str]]:
    projects: dict[str, dict] = {}
    invalid: dict[str, str] = {}
    for rec in records:
        parts = rec["path"].split("/")
        if len(parts) != 3 or parts[0] != PRINT_DIR or parts[2] != MANIFEST_NAME:
            continue
        folder = parts[1]
        if rec["type"] != "file":
            invalid[folder] = f"{rec['path']} is a {rec['type']}, not a manifest file"
            continue
        data = rec.get("_data")
        if data is None:
            invalid[folder] = f"{rec['path']} is larger than {METADATA_LIMIT} bytes"
            continue
        try:
            projects[folder] = _validate_media_manifest(json.loads(data.decode("utf-8")), folder)
        except (ValueError, UnicodeDecodeError) as exc:
            invalid[folder] = f"{rec['path']}: {exc}"
    return projects, invalid


def _load_journal(records: list[dict]) -> Optional[dict]:
    rec = next((r for r in records if r["path"] == JOURNAL_PATH), None)
    if rec is None:
        return None
    if rec["type"] != "file" or rec.get("_data") is None:
        return {"valid": False, "error": f"{JOURNAL_PATH} is not a readable small file"}
    try:
        obj = json.loads(rec["_data"].decode("utf-8"))
        _validate_journal(obj)
    except (ValueError, UnicodeDecodeError) as exc:
        return {"valid": False, "error": str(exc)}
    return {"valid": True, "obj": obj}


def _validate_journal(obj: Any) -> None:
    if not isinstance(obj, dict) or obj.get("schema") != SCHEMA_JOURNAL:
        raise ValueError(f"journal schema is not {SCHEMA_JOURNAL}")
    txn = obj.get("txn")
    if not isinstance(txn, str) or not re.fullmatch(r"[0-9a-f]{12,32}", txn):
        raise ValueError("journal txn id is malformed")
    if obj.get("phase") not in ("swapping", "committed"):
        raise ValueError("journal phase is unknown")
    staging_prefix = f"{STAGING_DIR}/{txn}/"
    try:
        folder = _project_folder(obj.get("project"))
    except _Refusal as exc:
        raise ValueError(exc.message) from None
    revision = obj.get("revision")
    if type(revision) is not int or not 1 <= revision <= MAX_REVISION:
        raise ValueError("journal revision is malformed")
    project_prefix = f"{PRINT_DIR}/{folder}/"

    def rel(value: Any, prefix: Optional[str] = None) -> None:
        try:
            normalized = _normalize_rel(value, "journal")
        except _Refusal as exc:
            raise ValueError(exc.message) from None
        if prefix is not None and not normalized.startswith(prefix):
            raise ValueError(f"journal path {value!r} is outside {prefix}")

    for item in obj.get("retire", []):
        rel(item.get("path"))
        rel(item.get("held"), staging_prefix)
        if not _is_printable(item["path"]):
            raise ValueError("journal retirement is not a printable file")
        if not _check_sha(item.get("sha256")):
            raise ValueError("journal retire hash malformed")
    for item in obj.get("place", []):
        rel(item.get("path"), project_prefix)
        name = item["path"][len(project_prefix):]
        match = _MANAGED_NAME_RE.match(name)
        if ("/" in name or not match or int(match[1]) != revision
                or not 1 <= int(match[2]) <= MAX_PARTS or not _is_printable(name)):
            raise ValueError("journal placement is not a file of this release")
        if not _check_sha(item.get("sha256")):
            raise ValueError("journal place hash malformed")
    archive = obj.get("archive")
    if archive is not None:
        rel(archive.get("path"), f"{ARCHIVE_DIR}/")
        if archive["path"] != f"{ARCHIVE_DIR}/{folder}/{folder}_RETIRED_BY_R{revision:03d}.zip":
            raise ValueError("journal archive belongs to another project or release")
        if not _check_sha(archive.get("sha256")):
            raise ValueError("journal archive hash malformed")
    for item in obj.get("metadata", []):
        rel(item.get("path"), project_prefix)
        if item["path"] not in (project_prefix + README_NAME, project_prefix + MANIFEST_NAME):
            raise ValueError("journal metadata is not a managed metadata file")
        if not _check_sha(item.get("new_sha256")):
            raise ValueError("journal metadata hash malformed")
        if item.get("old") is not None and not isinstance(item.get("old"), str):
            raise ValueError("journal metadata old text malformed")


def _similarity_key(name: str) -> str:
    stem = os.path.splitext(name.rsplit("/", 1)[-1])[0].lower()
    stem = re.sub(r"^r\d{5}_", "", stem)
    stem = re.sub(r"(?:[\s_.-]+(?:v|rev|r|copy|final|new|old)?\d*|(?:v|rev|r)?\d+)+$", "",
                  stem)
    return re.sub(r"[^a-z0-9]+", "", stem)


def _inventory(rootp: Path) -> dict:
    scan = _scan(rootp)
    records = scan["records"]
    token = _token(records)
    projects, invalid = _load_projects(records)
    journal = _load_journal(records)
    by_path = {rec["path"]: rec for rec in records}

    current_index: dict[str, tuple[str, dict, dict]] = {}
    history_index: dict[str, dict[str, tuple[str, int, Any]]] = {}
    metadata_paths: dict[str, Optional[str]] = {}
    for folder, manifest in projects.items():
        for entry in manifest["current"]:
            current_index[entry["path"]] = (folder, manifest, entry)
        for item in manifest.get("history", []):
            for f in item["files"]:
                history_index.setdefault(f["path"], {})[f["sha256"]] = (
                    folder, item["revision"], f.get("part_id"))
        metadata_paths[f"{PRINT_DIR}/{folder}/{MANIFEST_NAME}"] = None
        metadata_paths[f"{PRINT_DIR}/{folder}/{README_NAME}"] = manifest.get("readme_sha256")

    for rec in records:
        kind = rec["type"]
        path = rec["path"]
        if kind != "file":
            rec["status"] = kind
            continue
        sha = rec["sha256"]
        if path in current_index:
            folder, manifest, entry = current_index[path]
            rec["status"] = "current" if sha == entry["sha256"] else "modified"
            rec["managed"] = {"project": folder, "part_id": entry["part_id"],
                              "part_number": entry.get("part_number"),
                              "revision": manifest["revision"],
                              "expected_sha256": entry["sha256"]}
        elif sha is not None and sha in history_index.get(path, {}):
            folder, revision, part_id = history_index[path][sha]
            rec["status"] = "superseded"
            rec["managed"] = {"project": folder, "part_id": part_id, "revision": revision}
        elif path in metadata_paths and metadata_paths[path] in (None, sha):
            rec["status"] = "managed_metadata"
        elif path == JOURNAL_PATH:
            rec["status"] = "transaction_journal"
        elif rec["printable"]:
            rec["status"] = "unmanaged"
        else:
            rec["status"] = "other"

    printable = [rec for rec in records if rec.get("printable") and rec["type"] in (
        "file", "symlink", "unreadable")]
    managed_projects = []
    for folder in sorted(projects):
        manifest = projects[folder]
        jobs = []
        for entry in manifest["current"]:
            rec = by_path.get(entry["path"])
            if rec is None or rec["type"] != "file":
                status = "missing" if rec is None else rec["type"]
            else:
                status = rec["status"]
            jobs.append({"part_id": entry["part_id"], "part_number": entry.get("part_number"),
                         "path": entry["path"], "sha256": entry["sha256"], "status": status})
        managed_projects.append({
            "project": folder, "revision": manifest["revision"],
            "release": f"R{manifest['revision']:03d}",
            "published_at": manifest.get("published_at"),
            "folder": f"{PRINT_DIR}/{folder}",
            "manifest_path": f"{PRINT_DIR}/{folder}/{MANIFEST_NAME}",
            "jobs": jobs,
            "superseded_present": [r["path"] for r in records if r.get("status") == "superseded"
                                   and r["managed"]["project"] == folder],
            "complete": all(job["status"] == "current" for job in jobs),
        })

    groups: dict[str, list[dict]] = {}
    for rec in printable:
        if rec.get("sha256"):
            groups.setdefault(rec["sha256"], []).append(rec)
    duplicate_groups = [
        {"sha256": sha, "size": members[0]["size"],
         "files": [{"path": m["path"], "status": m["status"]} for m in members]}
        for sha, members in sorted(groups.items()) if len(members) > 1]
    hints: dict[str, list[str]] = {}
    for rec in printable:
        key = _similarity_key(rec["path"])
        if key:
            hints.setdefault(key, []).append(rec["path"])
    similar = [{"key": key, "paths": paths} for key, paths in sorted(hints.items())
               if len(paths) > 1]

    journal_summary = None
    if journal is not None:
        if journal["valid"]:
            obj = journal["obj"]
            journal_summary = {
                "valid": True, "txn": obj["txn"], "phase": obj["phase"],
                "project": obj.get("project"), "revision": obj.get("revision"),
                "started_at": obj.get("started_at"),
                "pending_new_files": [p["path"] for p in obj.get("place", [])],
                "pending_retired_files": [r["path"] for r in obj.get("retire", [])],
            }
        else:
            journal_summary = {"valid": False, "error": journal["error"]}

    def public_row(rec: dict) -> dict:
        return {k: v for k, v in rec.items() if not k.startswith("_")}

    unmanaged = [public_row(r) for r in printable if r["status"] == "unmanaged"]
    warnings: list[dict] = []
    if journal_summary is not None:
        warnings.append({
            "code": "interrupted_publish",
            "message": (
                "a previous publish did not finish; the printable set may be inconsistent. "
                "The next publish_print_media call (with this inventory's token) "
                + ("finishes" if journal_summary.get("phase") == "committed" else "rolls back")
                + " that transaction first and reports it" if journal_summary["valid"] else
                f"{JOURNAL_PATH} is unreadable ({journal_summary['error']}); publishing is "
                "refused until it is resolved by hand"),
            "journal": journal_summary})
    for folder, reason in sorted(invalid.items()):
        warnings.append({"code": "managed_manifest_invalid", "message": reason,
                         "project": folder})
    modified = [r["path"] for r in records if r.get("status") == "modified"]
    if modified:
        warnings.append({"code": "managed_file_modified",
                         "message": "managed print files no longer match their recorded "
                                    "sha256; publishing refuses until they are reviewed",
                         "paths": modified})
    missing = [job["path"] for proj in managed_projects for job in proj["jobs"]
               if job["status"] != "current" and job["status"] != "modified"]
    if missing:
        warnings.append({"code": "managed_file_missing",
                         "message": "managed print files are missing from the drive",
                         "paths": missing})
    superseded = [r["path"] for r in records if r.get("status") == "superseded"]
    if superseded:
        warnings.append({"code": "superseded_printables_present",
                         "message": "older managed revisions are still printable; the next "
                                    "publish of that project retires them",
                         "paths": superseded})
    if unmanaged:
        warnings.append({"code": "unmanaged_printables",
                         "message": f"{len(unmanaged)} printable file(s) are not managed by any "
                                    "published project; review them and pass retire_paths to "
                                    "archive the obsolete ones",
                         "paths": [r["path"] for r in unmanaged]})
    if duplicate_groups:
        warnings.append({"code": "duplicate_printables",
                         "message": "identical G-code is selectable under several names",
                         "groups": [[f["path"] for f in g["files"]] for g in duplicate_groups]})
    symlinks = [public_row(r) for r in records if r["type"] == "symlink"]
    if symlinks:
        warnings.append({"code": "symlinks_present",
                         "message": "symlinks are listed but never followed; publishing refuses "
                                    "if one sits under PRINT/ or CHUDWARE/",
                         "paths": [r["path"] for r in symlinks]})
    unreadable = [r["path"] for r in records if r["type"] == "unreadable"]
    if unreadable:
        warnings.append({"code": "unreadable_entries",
                         "message": "these entries could not be read and are not hashed",
                         "paths": unreadable})
    skipped = [r["path"] for r in records if r.get("hash_skipped")]
    if skipped:
        warnings.append({"code": "large_files_not_hashed",
                         "message": f"non-printable files over {UNRELATED_HASH_LIMIT} bytes "
                                    "outside PRINT/ and CHUDWARE/ are tracked by size+mtime",
                         "paths": skipped})
    if scan["staging"] and journal_summary is None:
        warnings.append({"code": "stale_staging",
                         "message": "non-printable leftovers of an aborted staging step exist; "
                                    "the next publish removes them",
                         "paths": scan["staging"]})

    return {
        "ok": True,
        "root": str(rootp),
        "inventory_token": token,
        "total": len(records),
        "files": [public_row(r) for r in records],
        "printable_files": [public_row(r) for r in printable],
        "current_printables": [public_row(r) for r in printable if r["status"] == "current"],
        "unmanaged_printables": unmanaged,
        "duplicate_groups": duplicate_groups,
        "similar_name_hints": similar,
        "managed_projects": managed_projects,
        "archives": scan["archives"],
        "symlinks": symlinks,
        "interrupted_publish": journal_summary,
        "staging_leftovers": scan["staging"],
        "reconciled": not (unmanaged or modified or missing or superseded or duplicate_groups
                           or journal_summary or invalid),
        "warnings": warnings,
        "_records": records,
        "_projects": projects,
        "_invalid": invalid,
        "_journal": journal,
    }


def _summary(inv: dict) -> dict:
    keys = ("inventory_token", "total", "printable_files", "current_printables",
            "unmanaged_printables", "duplicate_groups", "similar_name_hints",
            "managed_projects", "archives", "symlinks", "interrupted_publish",
            "staging_leftovers", "reconciled", "warnings")
    return {key: inv[key] for key in keys}


def _msvcrt_try_lock(fd: int) -> bool:
    try:
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        return True
    except OSError:
        return False


@contextlib.contextmanager
def _probe(rootp: Path) -> Iterator[bool]:
    """Yield True when a publish currently holds the drive lock. Writes nothing."""
    if fcntl is not None:
        fd = os.open(rootp, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | os.O_CLOEXEC)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                yield True
                return
            try:
                yield False
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
        return
    if msvcrt is None:
        yield False
        return
    try:  # never create the lock file from a read-only inventory
        fd = os.open(rootp / LOCK_FILE, os.O_RDWR | getattr(os, "O_BINARY", 0))
    except OSError:
        yield False
        return
    try:
        locked = _msvcrt_try_lock(fd)
        try:
            yield not locked
        finally:
            if locked:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    finally:
        os.close(fd)


@contextlib.contextmanager
def _drive_lock(rootp: Path) -> Iterator[None]:
    """Exclusive per-drive lock, released by the OS if the process dies.

    POSIX: ``flock`` on the mount root directory, so nothing is written to the drive.
    Windows: ``msvcrt.locking`` on ``CHUDWARE/.lock``; the scan skips that file and the
    token skips the ``CHUDWARE`` directory row, so taking the lock never changes the token.
    """
    deadline = time.monotonic() + LOCK_WAIT_S

    def wait_or_refuse() -> None:
        if time.monotonic() >= deadline:
            raise _Refusal("media_busy", "another publish holds this drive's lock; retry when it "
                                         "finishes")
        time.sleep(0.05)

    if fcntl is not None:
        fd = os.open(rootp, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | os.O_CLOEXEC)
        try:
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    wait_or_refuse()
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
        return
    if msvcrt is None:
        raise _Refusal("lock_unsupported_platform",
                       "this platform offers neither fcntl.flock nor msvcrt.locking; publishing "
                       "to removable media is refused")
    _ensure_dir(rootp, META_DIR)
    fd = os.open(rootp / LOCK_FILE, os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0))
    try:
        while not _msvcrt_try_lock(fd):
            wait_or_refuse()
        try:
            yield
        finally:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    finally:
        os.close(fd)


def inventory_print_media(root: str, offset: int = 0, limit: int = 200) -> dict:
    """Read-only, authoritative inventory of removable print media (see module docstring).

    ``files`` is one page (``offset``/``limit``) of every directory and file row; the
    printable summaries, duplicate groups, managed projects, warnings and
    ``inventory_token`` always cover the WHOLE drive regardless of the page.
    """
    try:
        for name, value, low, high in (("offset", offset, 0, None), ("limit", limit, 1, MAX_PAGE)):
            if isinstance(value, bool) or not isinstance(value, int) or value < low or (
                    high is not None and value > high):
                bound = f"{low}..{high}" if high is not None else f">= {low}"
                raise _Refusal("page_invalid", f"{name} must be an integer {bound}")
        rootp = _root_path(root)
        with _probe(rootp) as busy:
            inv = _inventory(rootp)
        if busy:
            inv["warnings"].insert(0, {
                "code": "publish_in_progress",
                "message": "a publish holds the drive lock; this snapshot may be mid-change"})
        result = {k: v for k, v in inv.items() if not k.startswith("_")}
        result["files"] = inv["files"][offset:offset + limit]
        result.update(offset=offset, limit=limit, busy=busy,
                      truncated=offset + limit < inv["total"])
        return result
    except _Refusal as refusal:
        return refusal.result()
    except Exception as exc:  # noqa: BLE001 - the public boundary never raises
        return {"ok": False, "error": "internal_error", "message": f"{type(exc).__name__}: {exc}"}


# --------------------------------------------------------------------------------------
# Transaction recovery
# --------------------------------------------------------------------------------------

def _rollback(rootp: Path, journal: dict) -> list[str]:
    """Undo a swapping transaction from the journal; return unresolved problems."""
    problems: list[str] = []
    txn_dir = rootp / STAGING_DIR / journal["txn"]
    try:
        _ensure_dir(rootp, f"{STAGING_DIR}/{journal['txn']}")
    except (OSError, _Refusal) as exc:
        return [f"cannot prepare {txn_dir}: {exc}"]
    for item in reversed(journal.get("metadata", [])):
        rel = item["path"]
        path = rootp / rel
        old = item.get("old")
        old_sha = hashlib.sha256(old.encode("utf-8")).hexdigest() if old is not None else None
        try:
            _check_components(rootp, rel)
            current = _hash_or_none(path) if _lexists(path) else None
            if _lexists(path) and current is None:
                problems.append(f"{rel} is not a regular file")
            elif current is not None and current not in (item["new_sha256"], old_sha):
                problems.append(f"{rel} changed during the publish (sha256 {current})")
            elif old is None:
                if current is not None:
                    os.unlink(path)
            elif current != old_sha:
                _atomic_replace(rootp, rel, old.encode("utf-8"), txn_dir)
        except (OSError, _Refusal) as exc:
            problems.append(f"{rel}: {exc}")
    for item in reversed(journal.get("place", [])):
        path = rootp / item["path"]
        if not _lexists(path):
            continue
        if _hash_or_none(path) != item["sha256"]:
            problems.append(f"{item['path']} exists with unexpected content; left in place")
            continue
        try:
            os.unlink(path)
        except OSError as exc:
            problems.append(f"{item['path']}: {exc}")
    for item in reversed(journal.get("retire", [])):
        original = rootp / item["path"]
        held = rootp / item["held"]
        held_present = _lexists(held)
        if _lexists(original):
            if _hash_or_none(original) != item["sha256"]:
                problems.append(f"{item['path']} was replaced by different content")
            continue
        if not held_present:
            problems.append(f"{item['path']} is missing and its held copy {item['held']} is gone")
            continue
        if _hash_or_none(held) != item["sha256"]:
            problems.append(f"held copy {item['held']} does not match {item['path']}")
            continue
        try:
            _check_components(rootp, item["path"])
            _move(held, original)
        except (OSError, _Refusal) as exc:
            problems.append(f"restore {item['path']}: {exc}")
    archive = journal.get("archive")
    if archive is not None and not problems:
        # Only once every retired file is back in place does its archive copy go away.
        path = rootp / archive["path"]
        if _lexists(path):
            if _hash_or_none(path) == archive["sha256"]:
                try:
                    os.unlink(path)
                except OSError as exc:
                    problems.append(f"{archive['path']}: {exc}")
                for directory in (path.parent, path.parent.parent):  # ARCHIVE/<P>, ARCHIVE
                    with contextlib.suppress(OSError):
                        os.rmdir(directory)
            else:
                problems.append(f"{archive['path']} exists with unexpected content")
    if not problems:
        _remove_tree(txn_dir)
        with contextlib.suppress(FileNotFoundError):
            os.unlink(rootp / JOURNAL_PATH)
        _tidy_staging(rootp)
    return problems


def _roll_forward(rootp: Path, journal: dict) -> list[str]:
    """Finish a committed transaction: the new set is live, retired bytes are archived."""
    problems: list[str] = []
    archive = journal.get("archive")
    if archive is not None and _hash_or_none(rootp / archive["path"]) != archive["sha256"]:
        return [f"archive {archive['path']} is missing or altered; held retired files are kept "
                f"under {STAGING_DIR}/{journal['txn']}"]
    for item in journal.get("place", []):
        if _hash_or_none(rootp / item["path"]) != item["sha256"]:
            problems.append(f"{item['path']} is missing or altered")
    if problems:
        return problems
    _remove_tree(rootp / STAGING_DIR / journal["txn"])
    with contextlib.suppress(FileNotFoundError):
        os.unlink(rootp / JOURNAL_PATH)
    _tidy_staging(rootp)
    return []


def _tidy_staging(rootp: Path) -> None:
    with contextlib.suppress(OSError):
        os.rmdir(rootp / STAGING_DIR)


def _recover(rootp: Path, journal_info: dict) -> dict:
    if not journal_info["valid"]:
        raise _Refusal("journal_invalid",
                       f"{JOURNAL_PATH} is unreadable ({journal_info['error']}); inspect the drive "
                       "and resolve it by hand before publishing")
    journal = journal_info["obj"]
    # Recovery runs before normal publish planning. A drive can have been remounted or
    # edited since the journal was written; validate every path before any rollback deletes.
    paths = [JOURNAL_PATH, f"{STAGING_DIR}/{journal['txn']}"]
    for item in journal.get("retire", []):
        paths.extend((item["path"], item["held"]))
    paths.extend(item["path"] for item in journal.get("place", []))
    paths.extend(item["path"] for item in journal.get("metadata", []))
    if journal.get("archive") is not None:
        paths.append(journal["archive"]["path"])
    for path in paths:
        _check_components(rootp, path)
    committed = journal["phase"] == "committed"
    problems = _roll_forward(rootp, journal) if committed else _rollback(rootp, journal)
    if problems:
        raise _Refusal("recovery_failed",
                       "an interrupted publish could not be "
                       + ("completed" if committed else "rolled back")
                       + "; nothing new was published", problems=problems, txn=journal["txn"])
    return {"txn": journal["txn"], "project": journal.get("project"),
            "revision": journal.get("revision"),
            "action": "completed" if committed else "rolled_back"}


# --------------------------------------------------------------------------------------
# Publish
# --------------------------------------------------------------------------------------

def _build_archive(rootp: Path, staged_zip: Path, retire: list[dict], info: dict) -> str:
    with zipfile.ZipFile(staged_zip, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        for item in retire:
            path = rootp / item["path"]
            fd = _open_nofollow(path)
            try:
                st = os.fstat(fd)
                if not stat.S_ISREG(st.st_mode):
                    raise _Refusal("retire_not_regular", f"{item['path']} is not a regular file")
                stamp = time.localtime(max(st.st_mtime, 315532800 + 86400))[:6]
                member = zipfile.ZipInfo(item["path"], date_time=stamp)
                member.compress_type = zipfile.ZIP_DEFLATED
                digest = hashlib.sha256()
                with archive.open(member, "w", force_zip64=st.st_size >= 2 ** 31) as out:
                    while chunk := os.read(fd, _CHUNK):
                        digest.update(chunk)
                        out.write(chunk)
            finally:
                os.close(fd)
            if digest.hexdigest() != item["sha256"]:
                raise _Refusal("retire_file_changed",
                               f"{item['path']} changed after inventory (sha256 "
                               f"{digest.hexdigest()}); re-run the inventory")
        archive.writestr("RETIRED.json", json.dumps(info, indent=2))
    with open(staged_zip, "rb+") as stream:
        os.fsync(stream.fileno())
        _drop_page_cache(stream.fileno())
    with zipfile.ZipFile(staged_zip) as archive:
        for item in retire:
            digest = hashlib.sha256()
            with archive.open(item["path"]) as member:
                while chunk := member.read(_CHUNK):
                    digest.update(chunk)
            if digest.hexdigest() != item["sha256"]:
                raise _Refusal("archive_verify_failed",
                               f"archived bytes of {item['path']} do not read back intact")
    return _hash_path(staged_zip)[0]


def _readme(folder: str, revision: int, published_at: str, current: list[dict],
            archive_rel: Optional[str]) -> str:
    lines = [
        f"Chudware managed print folder: {PRINT_DIR}/{folder}",
        f"Current release: R{revision:03d} (published {published_at})",
        "",
        "Print ONLY the files listed here. The first six characters of each file name",
        "identify it even when a printer shortens the name: R + 3-digit release + 2-digit part.",
        f"Example: R{revision:03d}01 = release {revision}, part 01.",
        "",
    ]
    width = max(len(entry["file_name"]) for entry in current)
    for entry in current:
        lines.append(f"  {entry['file_name']:<{width}}  part_id={entry['part_id']}  "
                     f"sha256={entry['sha256'][:16]}")
    lines += [
        "",
        f"Older releases are kept as non-printable ZIP archives in {ARCHIVE_DIR}/{folder}/.",
        f"Latest archive: {archive_rel}" if archive_rel else "No release has been retired yet.",
        "Do not copy G-code into this folder by hand: publish through Chudware so the old",
        "files are archived and retired and this list stays true.",
        "",
    ]
    return "\n".join(lines)


def publish_print_media(root: str, project: str, jobs: list[dict], expected_inventory: str,
                        media_idle: bool = False,
                        retire_paths: Optional[list[str]] = None) -> dict:
    """Publish the COMPLETE print set of ``project`` as one managed revision (see module doc).

    Requires ``media_idle=True`` (offline USB media cannot report printer activity) and the
    ``inventory_token`` from an ``inventory_print_media`` call made after the last change.
    """
    try:
        return _publish(root, project, jobs, expected_inventory, media_idle, retire_paths)
    except _Refusal as refusal:
        return refusal.result()
    except Exception as exc:  # noqa: BLE001 - the public boundary never raises
        return {"ok": False, "error": "internal_error", "message": f"{type(exc).__name__}: {exc}"}


def _publish(root: Any, project: Any, jobs: Any, expected_inventory: Any, media_idle: Any,
             retire_paths: Any) -> dict:
    if media_idle is not True:
        raise _Refusal("media_not_confirmed_idle",
                       "pass media_idle=True only after confirming no printer is printing from "
                       "this drive; offline USB media cannot report printer activity")
    rootp = _root_path(root)
    folder = _project_folder(project)
    if not isinstance(expected_inventory, str) or not _SHA_RE.fullmatch(expected_inventory):
        raise _Refusal("inventory_token_required",
                       "expected_inventory must be the inventory_token from inventory_print_media")
    retire = _normalize_retire_paths(retire_paths)
    sources = _validate_jobs(jobs)

    with _drive_lock(rootp):
        before = _inventory(rootp)
        if before["inventory_token"] != expected_inventory:
            raise _Refusal("inventory_changed",
                           "the drive changed since that inventory; run inventory_print_media "
                           "again, review it, and publish with the new token. Nothing was changed",
                           inventory_token=before["inventory_token"],
                           warnings=before["warnings"])
        recovered = None
        if before["_journal"] is not None:
            recovered = _recover(rootp, before["_journal"])
            before = _inventory(rootp)
        try:
            result = _publish_locked(rootp, folder, sources, retire, before)
        except _Refusal as refusal:
            if recovered is not None:
                refusal.details["recovered"] = recovered
            raise
        if recovered is not None:
            result["recovered"] = recovered
            result["warnings"].insert(0, {
                "code": "recovered_interrupted_publish",
                "message": f"an interrupted publish (txn {recovered['txn']}) was "
                           f"{recovered['action'].replace('_', ' ')} before this publish"})
        return result


def _publish_locked(rootp: Path, folder: str, sources: list[dict], retire: list[str],
                    before: dict) -> dict:
    records = before["_records"]
    by_path = {rec["path"]: rec for rec in records}
    projects = before["_projects"]
    if folder in before["_invalid"]:
        raise _Refusal("managed_manifest_invalid",
                       f"{before['_invalid'][folder]}; restore or remove it by hand before "
                       "publishing", project=folder)
    linked = [rec["path"] for rec in records if rec["type"] == "symlink" and (
        rec["path"].split("/")[0] in (PRINT_DIR, META_DIR))]
    if linked:
        raise _Refusal("media_symlink", "symlinks under PRINT/ or CHUDWARE/ are refused",
                       paths=linked)
    folder_rel = f"{PRINT_DIR}/{folder}"
    blocked = [rec["path"] for rec in records if rec["type"] in ("unreadable", "special")
               and (rec["path"] == folder_rel or rec["path"].startswith(folder_rel + "/"))]
    if blocked:
        raise _Refusal("media_unreadable", f"entries under {folder_rel} cannot be read",
                       paths=blocked)
    for top in (PRINT_DIR, META_DIR, folder_rel):
        rec = by_path.get(top)
        if rec is not None and rec["type"] != "dir":
            raise _Refusal("media_path_conflict", f"{top} exists and is not a directory",
                           path=top)
    prev = projects.get(folder)
    explicit = set(retire)

    # ---- previous managed set: verified before anything is planned ----
    plan: list[dict] = []
    planned: set[str] = set()
    missing_previous: list[str] = []
    if prev is not None:
        for entry in prev["current"]:
            rec = by_path.get(entry["path"])
            if rec is None:
                missing_previous.append(entry["path"])
                continue
            if rec["type"] != "file":
                raise _Refusal("managed_file_modified",
                               f"{entry['path']} is a {rec['type']}, not the managed file")
            if rec["sha256"] != entry["sha256"]:
                if entry["path"] in explicit:
                    continue  # reviewed by the caller: archived below as an explicit retire
                raise _Refusal("managed_file_modified",
                               f"{entry['path']} no longer matches its recorded sha256; review it "
                               "and pass it in retire_paths to archive it, or restore it",
                               path=entry["path"], expected_sha256=entry["sha256"],
                               actual_sha256=rec["sha256"])
            plan.append({"path": entry["path"], "sha256": rec["sha256"], "size": rec["size"],
                         "reason": "previous_revision", "part_id": entry["part_id"],
                         "revision": prev["revision"]})
            planned.add(entry["path"])
        for rec in records:
            if rec.get("status") == "superseded" and rec["managed"]["project"] == folder \
                    and rec["path"] not in planned:
                plan.append({"path": rec["path"], "sha256": rec["sha256"], "size": rec["size"],
                             "reason": "superseded_leftover",
                             "part_id": rec["managed"]["part_id"],
                             "revision": rec["managed"]["revision"]})
                planned.add(rec["path"])

    # ---- idempotency: same verified set, nothing else pending ----
    desired = {s["part_id"]: (s["sha256"], s["ext"]) for s in sources}
    if prev is not None:
        prev_set = {e["part_id"]: (e["sha256"], os.path.splitext(e["path"])[1].lower())
                    for e in prev["current"]}
        recorded = {r["path"] for r in prev.get("retired", [])}
        pending = [p for p in retire if p in by_path or p not in recorded]
        superseded = [p for p in plan if p["reason"] == "superseded_leftover"]
        if (desired == prev_set and not missing_previous and len(plan) == len(prev["current"])
                and not superseded and not pending):
            return _result(rootp, folder, before, status="unchanged",
                           revision=prev["revision"], current=prev["current"], retired=[],
                           archive=None, warnings=[])

    # ---- explicit, caller-reviewed retire paths ----
    other_current = {e["path"]: f for f, m in projects.items() if f != folder
                     for e in m["current"]}
    for rel in retire:
        if rel in planned:
            continue
        rec = by_path.get(rel)
        if rec is None:
            raise _Refusal("retire_path_missing",
                           f"retire path {rel} is not on the drive; re-run the inventory",
                           path=rel)
        if rec["type"] != "file":
            raise _Refusal("retire_path_not_file", f"retire path {rel} is a {rec['type']}",
                           path=rel)
        if not rec["printable"]:
            raise _Refusal("retire_path_not_printable",
                           f"retire path {rel} is not a printable file; only printable files "
                           "are retired", path=rel)
        owner = other_current.get(rel) or (rec.get("managed") or {}).get("project")
        if owner is None:
            owner = next((f for f in projects if f != folder
                          and rel.startswith(f"{PRINT_DIR}/{f}/")), None)
        if owner is not None and owner != folder:
            raise _Refusal("retire_path_managed_elsewhere",
                           f"{rel} belongs to managed project {owner}; publish that project "
                           "(with its own retire_paths) instead", path=rel, project=owner)
        if rel.split("/")[0] == META_DIR:
            raise _Refusal("retire_path_invalid", f"{rel} is Chudware metadata", path=rel)
        plan.append({"path": rel, "sha256": rec["sha256"], "size": rec["size"],
                     "reason": "explicit"})
        planned.add(rel)
    loose = [rec["path"] for rec in records if rec.get("printable")
             and rec["path"].startswith(folder_rel + "/") and rec["path"] not in planned]
    if loose:
        raise _Refusal("unmanaged_in_project_folder",
                       f"{folder_rel} holds printable files that are not this project's managed "
                       "jobs; review them and pass them in retire_paths (they are archived, "
                       "not deleted) or move them out by hand", paths=loose)

    # ---- revision, part numbers, destination names ----
    highest = prev["revision"] if prev is not None else 0
    for item in (prev or {}).get("history", []):
        highest = max(highest, item["revision"])
    folder_prefix = folder_rel + "/"
    existing_names = [rec["path"][len(folder_prefix):] for rec in records
                      if rec["path"].startswith(folder_prefix)
                      and "/" not in rec["path"][len(folder_prefix):]]
    for name in existing_names:
        match = _MANAGED_NAME_RE.match(name)
        if match:
            highest = max(highest, int(match.group(1)))
    for archive in before["archives"]:
        match = _ARCHIVE_NAME_RE.search(archive["path"])
        if archive["project"] == folder and match:
            highest = max(highest, int(match.group(1)))
    revision = highest + 1
    if revision > MAX_REVISION:
        raise _Refusal("revision_limit",
                       f"project {folder} has used all {MAX_REVISION} releases of the R<rrr> "
                       "naming scheme; publish under a new project name", project=folder)
    numbers: dict[str, int] = dict((prev or {}).get("part_numbers", {}))
    for entry in (prev or {}).get("current", []):
        if isinstance(entry.get("part_number"), int):
            numbers.setdefault(entry["part_id"], entry["part_number"])
    assigned: dict[str, int] = {}
    for source in sources:
        if source["part_id"] in numbers and numbers[source["part_id"]] not in assigned.values():
            assigned[source["part_id"]] = numbers[source["part_id"]]
    for source in sources:
        if source["part_id"] in assigned:
            continue
        taken = set(assigned.values())
        fresh = [n for n in range(1, MAX_PARTS + 1) if n not in taken
                 and n not in numbers.values()]
        reuse = [n for n in range(1, MAX_PARTS + 1) if n not in taken]
        pick = (fresh or reuse or [None])[0]
        if pick is None:
            raise _Refusal("part_limit", f"no free part number in 01..{MAX_PARTS:02d}")
        assigned[source["part_id"]] = pick
    for source in sources:
        number = assigned[source["part_id"]]
        name = f"R{revision:03d}{number:02d}_{source['slug']}{source['ext']}"
        source.update(part_number=number, file_name=name, dest=f"{folder_rel}/{name}")
    remaining = {n.lower() for n in existing_names if f"{folder_prefix}{n}" not in planned}
    remaining_short = {n[:6].lower() for n in remaining}
    for source in sources:
        if source["file_name"].lower() in remaining or \
                source["file_name"][:6].lower() in remaining_short:
            raise _Refusal("destination_exists",
                           f"{source['dest']} (or its 6-character short name) is already taken "
                           "by an unrecognized file; it is never overwritten",
                           path=source["dest"])
    if prev is None:
        clashes = [n for n in remaining if n in (MANIFEST_NAME.lower(), README_NAME.lower())]
        if clashes:
            raise _Refusal("destination_exists",
                           f"{folder_rel} already holds unrecognized {clashes}; refusing to "
                           "overwrite them", paths=clashes)
    readme_rel = f"{folder_rel}/{README_NAME}"
    readme_rec = by_path.get(readme_rel)
    if prev is not None and readme_rec is not None and readme_rec.get("status") != \
            "managed_metadata":
        raise _Refusal("managed_file_modified",
                       f"{readme_rel} was edited since it was published; restore or remove it",
                       path=readme_rel)
    archive_rel = None
    if plan:
        archive_rel = f"{ARCHIVE_DIR}/{folder}/{folder}_RETIRED_BY_R{revision:03d}.zip"
        if any(a["path"].lower() == archive_rel.lower() for a in before["archives"]) or \
                archive_rel in by_path:
            raise _Refusal("destination_exists", f"{archive_rel} already exists",
                           path=archive_rel)
    need = sum(s["size"] for s in sources) + sum(p["size"] for p in plan) + 2 * 1024 * 1024
    free = shutil.disk_usage(rootp).free
    if free < need:
        raise _Refusal("insufficient_space",
                       f"the drive has {free} bytes free and this publish needs about {need}",
                       free_bytes=free, needed_bytes=need)
    for rel in [s["dest"] for s in sources] + [p["path"] for p in plan] + [readme_rel]:
        _check_components(rootp, rel)

    # ---- stage everything off the printer's view, verified ----
    if before["staging_leftovers"]:
        _remove_tree(rootp / STAGING_DIR)
    txn = uuid.uuid4().hex[:16]
    created: list[Path] = []
    txn_rel = f"{STAGING_DIR}/{txn}"
    published_at = _now()
    try:
        txn_dir = _ensure_dir(rootp, txn_rel, created)
        for index, source in enumerate(sources):
            source["staged"] = f"{txn_rel}/new-{index:02d}{STAGED_SUFFIX}"
            _stage_copy(source["gcode_path"], rootp / source["staged"], source["sha256"])
        archive_sha = None
        if plan:
            for index, item in enumerate(plan):
                item["held"] = f"{txn_rel}/retired-{index:02d}{STAGED_SUFFIX}"
            info = {"schema": "chudware.print_media_archive/1", "project": folder,
                    "retired_by_revision": revision, "archived_at": published_at,
                    "files": [{k: item[k] for k in ("path", "sha256", "size", "reason")}
                              | {k: item[k] for k in ("part_id", "revision") if k in item}
                              for item in plan]}
            staged_zip = txn_dir / f"archive{STAGED_SUFFIX}"
            archive_sha = _build_archive(rootp, staged_zip, plan, info)
        current = [{
            "part_id": s["part_id"], "part_number": s["part_number"],
            "code": s["file_name"][:6], "file_name": s["file_name"], "path": s["dest"],
            "sha256": s["sha256"], "size": s["size"], "part": s["part"],
            "printer": s["printer"],
            "source": {"manifest_path": str(s["manifest_path"]),
                       "manifest_sha256": s["manifest_sha256"],
                       "gcode_path": str(s["gcode_path"]), "gcode_sha256": s["sha256"]},
            "warnings": s["warnings"],
        } for s in sources]
        readme_bytes = _readme(folder, revision, published_at, current, archive_rel).encode()
        history = list((prev or {}).get("history", []))
        if prev is not None:
            history.append({
                "revision": prev["revision"], "release": f"R{prev['revision']:03d}",
                "published_at": prev.get("published_at"), "retired_by": revision,
                "archive": archive_rel,
                "files": [{"part_id": e["part_id"], "path": e["path"], "sha256": e["sha256"],
                           "present_at_retirement": e["path"] in planned}
                          for e in prev["current"]]})
        manifest = {
            "schema": SCHEMA_MEDIA_MANIFEST, "project": folder, "revision": revision,
            "release": f"R{revision:03d}", "published_at": published_at,
            "folder": folder_rel,
            "instructions": (
                "Print only the files in 'current'. Each name starts with R<release:3><part:2>; "
                f"R{revision:03d}01 is release {revision}, part 01. Retired releases are "
                f"non-printable ZIP archives under {ARCHIVE_DIR}/{folder}/. Publish through "
                "Chudware; files copied here by hand are reported as unmanaged."),
            "part_numbers": {**numbers, **assigned},
            "current": current,
            "retired": [{k: v for k, v in item.items() if k != "held"} for item in plan],
            "archive": {"path": archive_rel, "sha256": archive_sha} if archive_rel else None,
            "history": history,
            "readme_sha256": hashlib.sha256(readme_bytes).hexdigest(),
        }
        manifest_bytes = json.dumps(manifest, indent=2).encode("utf-8")
        staged_readme = txn_dir / f"readme{STAGED_SUFFIX}"
        staged_manifest = txn_dir / f"manifest{STAGED_SUFFIX}"
        _write_staged(staged_readme, readme_bytes)
        _write_staged(staged_manifest, manifest_bytes)
        manifest_rel = f"{folder_rel}/{MANIFEST_NAME}"
        old_manifest = by_path.get(manifest_rel, {}).get("_data") if prev is not None else None
        old_readme = None
        if prev is not None and readme_rec is not None:
            old_readme = _hash_path(rootp / readme_rel, keep_bytes=True)[2]
        journal = {
            "schema": SCHEMA_JOURNAL, "txn": txn, "phase": "swapping", "project": folder,
            "revision": revision, "started_at": published_at,
            "retire": [{"path": p["path"], "held": p["held"], "sha256": p["sha256"]}
                       for p in plan],
            "place": [{"path": s["dest"], "staged": s["staged"], "sha256": s["sha256"]}
                      for s in sources],
            "archive": {"path": archive_rel, "sha256": archive_sha} if archive_rel else None,
            "metadata": [
                {"path": readme_rel, "new_sha256": manifest["readme_sha256"],
                 "old": old_readme.decode("utf-8") if old_readme is not None else None},
                {"path": manifest_rel,
                 "new_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
                 "old": old_manifest.decode("utf-8") if old_manifest is not None else None},
            ],
        }
        journal_bytes = json.dumps(journal, indent=2).encode("utf-8")
    except BaseException as exc:
        _remove_tree(rootp / txn_rel)
        for path in reversed(created):
            with contextlib.suppress(OSError):
                os.rmdir(path)
        if isinstance(exc, _Refusal):
            exc.details.setdefault("current_set_untouched", True)
            raise
        if not isinstance(exc, Exception):
            raise
        raise _Refusal("staging_failed",
                       f"staging the new revision failed ({type(exc).__name__}: {exc}); the "
                       "drive's current print set is untouched", current_set_untouched=True
                       ) from exc

    # ---- swap under a journal; every step is reversible until 'committed' ----
    try:
        _atomic_replace(rootp, JOURNAL_PATH, journal_bytes, txn_dir)
        _ensure_dir(rootp, folder_rel)
        if archive_rel:
            _ensure_dir(rootp, f"{ARCHIVE_DIR}/{folder}")
            if _lexists(rootp / archive_rel):
                raise _Refusal("destination_exists", f"{archive_rel} appeared during publish")
            _move(txn_dir / f"archive{STAGED_SUFFIX}", rootp / archive_rel)
        for item in plan:
            if _hash_or_none(rootp / item["path"]) != item["sha256"]:
                raise _Refusal("retire_file_changed", f"{item['path']} changed during publish")
            _move(rootp / item["path"], rootp / item["held"])
        for source in sources:
            if _lexists(rootp / source["dest"]):
                raise _Refusal("destination_exists", f"{source['dest']} appeared during publish")
            _move(rootp / source["staged"], rootp / source["dest"])
        _move(staged_readme, rootp / readme_rel)
        _move(staged_manifest, rootp / manifest_rel)
    except BaseException as exc:
        problems = _rollback(rootp, journal)
        if not isinstance(exc, Exception):
            raise
        if problems:
            raise _Refusal("rollback_incomplete",
                           f"publishing failed ({type(exc).__name__}: {exc}) and the rollback "
                           f"could not finish; {JOURNAL_PATH} records the pending transaction "
                           "and inventory reports it", problems=problems, txn=txn) from exc
        raise _Refusal("publish_rolled_back",
                       f"publishing failed ({type(exc).__name__}: {exc}); the previous print set "
                       "was restored and nothing new was published",
                       current_set_untouched=True) from exc

    # ---- commit: retired bytes live in the verified archive; drop the held copies ----
    warnings: list[dict] = []
    journal["phase"] = "committed"
    try:
        _atomic_replace(rootp, JOURNAL_PATH, json.dumps(journal, indent=2).encode("utf-8"),
                        txn_dir)
        _remove_tree(txn_dir)
        os.unlink(rootp / JOURNAL_PATH)
        _tidy_staging(rootp)
    except OSError as exc:
        warnings.append({"code": "cleanup_incomplete",
                         "message": f"the new set is live but transaction cleanup failed ({exc}); "
                                    "the next publish finishes it"})
    if missing_previous:
        warnings.append({"code": "previous_files_missing",
                         "message": "these files of the previous release were already gone",
                         "paths": missing_previous})
    after = _inventory(rootp)
    after_by_path = {rec["path"]: rec for rec in after["_records"]}
    failures = []
    for entry in current:
        rec = after_by_path.get(entry["path"])
        if rec is None or rec.get("status") != "current":
            failures.append(f"{entry['path']} is not present as the current managed file")
    for item in plan:
        if item["path"] in after_by_path:
            failures.append(f"retired {item['path']} is still on the drive")
    stray = [rec["path"] for rec in after["_records"] if rec.get("printable")
             and rec["path"].startswith(folder_prefix) and rec.get("status") != "current"]
    if stray:
        failures.append(f"non-current printables remain in {folder_rel}: {stray}")
    if failures:
        raise _Refusal("post_publish_verification_failed",
                       "the drive does not show exactly the published set after publishing",
                       problems=failures, inventory=_summary(after))
    return _result(rootp, folder, after, status="published", revision=revision,
                   current=current, retired=manifest["retired"],
                   archive=manifest["archive"], warnings=warnings)


def _result(rootp: Path, folder: str, inv: dict, *, status: str, revision: int,
            current: list[dict], retired: list[dict], archive: Optional[dict],
            warnings: list[dict]) -> dict:
    unmanaged = [r["path"] for r in inv["unmanaged_printables"]]
    reconciled = bool(inv["reconciled"])
    warnings = list(warnings)
    if not reconciled:
        status = f"{status}_unreconciled"
        warnings.append({
            "code": "drive_not_reconciled",
            "message": "the project's set is published, but the drive still holds printables "
                       "that are not current managed jobs (see unmanaged_printables and "
                       "inventory.warnings); review them and pass retire_paths to archive the "
                       "obsolete ones"})
    return {
        "ok": True,
        "status": status,
        "changed": status.startswith("published"),
        "root": str(rootp),
        "project": folder,
        "folder": f"{PRINT_DIR}/{folder}",
        "revision": revision,
        "release": f"R{revision:03d}",
        "current": [{k: e.get(k) for k in ("part_id", "part_number", "file_name", "path",
                                           "sha256", "size", "warnings")} for e in current],
        "retired": retired,
        "archive": archive,
        "unmanaged_printables": unmanaged,
        "reconciled": reconciled,
        "inventory_token": inv["inventory_token"],
        "inventory": _summary(inv),
        "warnings": warnings,
    }
