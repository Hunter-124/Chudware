#!/usr/bin/env python3
"""Retained webd release integrity and root-authorized rollback snapshots.

Authorization records are provisioned out of band by the deploy operator under
/var/lib/chudware-deployment-trust/{prod,staging}/<joint SHA-256>. They contain
exactly the full retained runtime-tree SHA-256 plus LF. Service-owned manifests
and their hashes are not an authorization root.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import stat
import tempfile
import sys
from datetime import datetime, timezone
from pathlib import Path

TRUST_ROOT = Path("/var/lib/chudware-deployment-trust")
_DIGEST = re.compile(r"[0-9a-f]{64}")
_COMMIT = re.compile(r"[0-9a-f]{40}")
_FIRST_CUTOVER_RECOVERY = (
    "schema=1\n"
    "target_commit={commit}\n"
    "recovery_scope=manual_gateway_checkout_and_database_schema\n"
    "verified_off_host_encrypted_backup=yes\n"
    "isolated_restore_and_schema_compatibility_drill=yes\n"
    "prior_gateway_source_and_dependencies_available=yes\n"
    "maintenance_and_customer_data_reconciliation_plan=yes\n"
    "operator_accepts_no_automatic_full_rollback=yes\n"
)
_FILES = {
    "hostd_sha256": "bin/chudware-hostd",
    "libchudware_sha256": "bin/libchudware.so",
    "wasm_sha256": "app/Chudware.wasm",
    "javascript_sha256": "app/Chudware.js",
    "chud_sha256": "bin/chud",
    "chudcode_artifact_lock_sha256": "CHUDCODE_ARTIFACT.lock",
    "chudcode_notice_sha256": "CHUDCODE-NOTICE",
    "chudcode_third_party_notices_sha256": "CHUDCODE-THIRD-PARTY-NOTICES.txt",
    "hostd_unit_sha256": "chudware-hostd.service",
    "gateway_unit_sha256": "chudware-gateway.service",
    "staging_gateway_unit_sha256": "chudware-gateway-staging.service",
}
_JOINT_FIELDS = (
    "hostd_sha256", "libchudware_sha256", "advisor_runtime_sha256", "wasm_sha256",
    "javascript_sha256", "chud_sha256", "chudcode_artifact_lock_sha256",
    "chudcode_notice_sha256", "chudcode_third_party_notices_sha256", "hostd_unit_sha256",
    "gateway_unit_sha256", "staging_gateway_unit_sha256", "prompts_sha256",
    "cell_runtime_sha256",
)


class ReleaseError(ValueError):
    pass


def _regular(path: Path) -> os.stat_result:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise ReleaseError("deployment release contains a nonregular file")
    return info


def file_digest(path: Path) -> str:
    _regular(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ReleaseError("deployment release file changed type")
        return hashlib.file_digest(stream, "sha256").hexdigest()


def tree_files(root: Path):
    if not stat.S_ISDIR(root.lstat().st_mode):
        raise ReleaseError("deployment release root is not a real directory")
    files = []
    total_bytes = 0
    for directory, names, filenames in os.walk(root, followlinks=False):
        for name in names:
            if not stat.S_ISDIR((Path(directory) / name).lstat().st_mode):
                raise ReleaseError("deployment release contains a linked directory")
        for name in filenames:
            path = Path(directory) / name
            if path == root / "deploy-manifest":
                continue
            relative = path.relative_to(root).as_posix()
            if any(ord(char) < 32 for char in relative):
                raise ReleaseError("deployment release contains a control-character path")
            info = _regular(path)
            if info.st_mode & (stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX):
                raise ReleaseError("deployment release contains a privileged file mode")
            files.append((relative, path, info))
            total_bytes += info.st_size
            if len(files) > 100000 or info.st_size > 2 * 1024**3 or total_bytes > 8 * 1024**3:
                raise ReleaseError("deployment runtime tree exceeds its resource limits")
    if not files or len(files) > 100000:
        raise ReleaseError("deployment release file count is invalid")
    return sorted(files)


def tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for relative, path, info in tree_files(root):
        digest.update(relative.encode("utf-8") + b"\0")
        digest.update(f"{stat.S_IMODE(info.st_mode):o}\0{info.st_size}\0".encode("ascii"))
        digest.update(file_digest(path).encode("ascii") + b"\n")
    return digest.hexdigest()


def _group_digest(root: Path, paths) -> str:
    digest = hashlib.sha256()
    for path in sorted(paths):
        _regular(path)
        digest.update(path.relative_to(root).as_posix().encode("utf-8") + b"\0")
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        digest.update(b"\0")
    return digest.hexdigest()


def validate(root: Path) -> dict[str, str]:
    tree_files(root)
    manifest_path = root / "deploy-manifest"
    _regular(manifest_path)
    fd = os.open(manifest_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > 65536:
            raise ReleaseError("deployment manifest is not a bounded regular file")
        contents = stream.read(65537)
        if len(contents) > 65536:
            raise ReleaseError("deployment manifest exceeds its byte limit")
    manifest = {}
    for line in contents.decode("utf-8").splitlines():
        key, separator, value = line.partition("=")
        if not separator or not key or key in manifest:
            raise ReleaseError("deployment manifest has invalid or duplicate fields")
        manifest[key] = value
    if manifest.get("schema") != "7":
        raise ReleaseError("deployment release predates complete retained runtime evidence")
    for field, relative in _FILES.items():
        if manifest.get(field) != file_digest(root / relative):
            raise ReleaseError(f"deployment digest mismatch: {relative}")
    prompts = root / "agent/prompts"
    prompt_paths = [path for _, path, _ in tree_files(prompts)]
    if manifest.get("prompts_sha256") != _group_digest(prompts, prompt_paths):
        raise ReleaseError("deployment prompt-tree digest mismatch")
    binaries = root / "bin"
    advisor_paths = [path for _, path, _ in tree_files(binaries)
                     if path.name.startswith("libonnxruntime") or path.parent == binaries / "polymesh-advisor"]
    if manifest.get("advisor_runtime_sha256") != _group_digest(binaries, advisor_paths):
        raise ReleaseError("deployment advisor-runtime digest mismatch")
    cell = root / "cell"
    cell_paths = [path for _, path, _ in tree_files(cell)]
    if manifest.get("cell_runtime_sha256") != _group_digest(cell, cell_paths):
        raise ReleaseError("deployment cell-runtime digest mismatch")
    for field in _JOINT_FIELDS:
        if _DIGEST.fullmatch(manifest.get(field, "")) is None:
            raise ReleaseError("deployment joint identity has invalid input digests")
    joint = hashlib.sha256("".join(manifest[field] + "\n" for field in _JOINT_FIELDS).encode("ascii")).hexdigest()
    if manifest.get("joint_sha256") != joint:
        raise ReleaseError("deployment joint identity does not match its runtime inputs")
    if manifest.get("release_tree_sha256") != tree_digest(root):
        raise ReleaseError("deployment retained file set or full-tree digest changed")
    return manifest


def _authority_directory(directory: Path, authority_uid: int) -> None:
    for parent in (*reversed(directory.parents), directory):
        info = parent.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != authority_uid
                or info.st_mode & 0o022):
            raise ReleaseError("deployment authorization directory is not authority-owned and protected")


def _authority_record(record: Path, expected: bytes, authority_uid: int) -> None:
    _authority_directory(record.parent, authority_uid)
    info = _regular(record)
    if info.st_uid != authority_uid or info.st_mode & 0o022 or info.st_size != len(expected):
        raise ReleaseError("deployment authorization record is not authority-owned and protected")
    fd = os.open(record, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        current = os.fstat(stream.fileno())
        if (not stat.S_ISREG(current.st_mode) or current.st_uid != authority_uid
                or current.st_mode & 0o022 or current.st_size != len(expected)
                or stream.read(len(expected) + 1) != expected):
            raise ReleaseError("deployment authorization does not match the requested identity")


def authorize(joint: str, tree: str, environment: str, *, trust_root: Path = TRUST_ROOT,
              authority_uid: int = 0) -> None:
    if (environment not in {"prod", "staging"} or _DIGEST.fullmatch(joint) is None
            or _DIGEST.fullmatch(tree) is None):
        raise ReleaseError("invalid deployment authorization identity")
    _authority_record(trust_root / environment / joint, tree.encode("ascii") + b"\n", authority_uid)


def promotion_preflight(web_home: Path, environment: str, target_commit: str, *,
                        first_cutover_ack: Path | None = None,
                        trust_root: Path = TRUST_ROOT, authority_uid: int = 0) -> str:
    """Read-only proof of an authorized prior payload, or an explicit first cutover.

    This does not attest gateway source/dependency or database rollback. An ack is
    an independently provisioned operator statement, never created by this tool.
    """
    if environment not in {"prod", "staging"} or _COMMIT.fullmatch(target_commit) is None:
        raise ReleaseError("promotion requires an exact target commit and environment")
    releases = web_home / "releases"
    current = web_home / "current"
    try:
        if not stat.S_ISDIR(web_home.lstat().st_mode) or not stat.S_ISDIR(releases.lstat().st_mode):
            raise ReleaseError("retained payload store must use real directories")
        if not current.is_symlink():
            raise ReleaseError("current retained payload is not a release symlink")
        resolved = current.resolve(strict=True)
        if resolved.parent != releases.resolve(strict=True):
            raise ReleaseError("current payload is outside the retained release store")
        # Do not validate through a linked release directory.
        if not stat.S_ISDIR((releases / resolved.name).lstat().st_mode):
            raise ReleaseError("retained release directory is linked")
        manifest = validate(resolved)
        authorize(manifest["joint_sha256"], manifest["release_tree_sha256"], environment,
                  trust_root=trust_root, authority_uid=authority_uid)
        return "authorized_retained_payload"
    except (OSError, ValueError):
        if first_cutover_ack is None:
            raise ReleaseError("no independently authorized schema-7 rollback payload; explicit first-cutover acknowledgment required") from None
    required = trust_root / environment / "first-cutover" / target_commit
    if first_cutover_ack != required:
        raise ReleaseError("first-cutover acknowledgment must use the exact authority registry path")
    expected = _FIRST_CUTOVER_RECOVERY.format(commit=target_commit).encode("ascii")
    _authority_record(required, expected, authority_uid)
    return "first_cutover_manual_recovery_only"


def runtime_preflight(repo: Path, wheelhouse: Path) -> str:
    """Authenticate the offline worker closure without provisioning any runtime."""
    sys.path.insert(0, str(repo))
    from services.installer.provision import (
        CHUDCODE_LOCK, REQUIREMENTS, WHEEL_LOCK, _target_context,
    )
    from services.installer.wheelhouse import verified_wheelhouse
    context = _target_context("/usr/bin/python3")
    with verified_wheelhouse(wheelhouse, REQUIREMENTS, WHEEL_LOCK, CHUDCODE_LOCK,
                             context["environment"], set(context["tags"])):
        return "authenticated_offline_worker_inputs"


def snapshot(source: Path, parent: Path, environment: str) -> Path:
    files = tree_files(source)
    manifest_path = source / "deploy-manifest"
    manifest_info = _regular(manifest_path)
    if manifest_info.st_size > 65536:
        raise ReleaseError("deployment manifest exceeds its byte limit")
    files.append(("deploy-manifest", manifest_path, manifest_info))
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    destination = Path(tempfile.mkdtemp(prefix=f"{stamp}-rollback-", dir=parent))
    try:
        for relative, path, info in files:
            target = destination / relative
            current = destination
            for component in target.relative_to(destination).parts[:-1]:
                current = current / component
                current.mkdir(mode=0o700, exist_ok=True)
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, "rb") as input_file, target.open("xb") as output:
                if not stat.S_ISREG(os.fstat(input_file.fileno()).st_mode):
                    raise ReleaseError("deployment input changed type during capture")
                copied = 0
                while block := input_file.read(1024 * 1024):
                    copied += len(block)
                    if copied > info.st_size:
                        raise ReleaseError("deployment input grew during capture")
                    output.write(block)
            target.chmod(stat.S_IMODE(info.st_mode))
        manifest = validate(destination)
        authorize(manifest["joint_sha256"], manifest["release_tree_sha256"], environment)
        # Published snapshot directories remain private; ordinary runtime file
        # modes are retained because their identity is authenticated.
        for directory, _, _ in os.walk(destination):
            Path(directory).chmod(0o700)
        return destination
    except BaseException:
        shutil.rmtree(destination)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    digest = commands.add_parser("tree-digest")
    digest.add_argument("root", type=Path)
    capture = commands.add_parser("snapshot")
    capture.add_argument("source", type=Path)
    capture.add_argument("--parent", required=True, type=Path)
    capture.add_argument("--environment", required=True, choices=("prod", "staging"))
    preflight = commands.add_parser("promotion-preflight")
    preflight.add_argument("--web-home", required=True, type=Path)
    preflight.add_argument("--environment", required=True, choices=("prod", "staging"))
    preflight.add_argument("--target-commit", required=True)
    preflight.add_argument("--first-cutover-ack", type=Path)
    runtime = commands.add_parser("runtime-preflight")
    runtime.add_argument("--repo", required=True, type=Path)
    runtime.add_argument("--wheelhouse", required=True, type=Path)
    args = parser.parse_args()
    try:
        if args.command == "tree-digest":
            result = tree_digest(args.root)
        elif args.command == "runtime-preflight":
            result = runtime_preflight(args.repo, args.wheelhouse)
        elif args.command == "promotion-preflight":
            result = promotion_preflight(args.web_home, args.environment, args.target_commit,
                                         first_cutover_ack=args.first_cutover_ack)
        else:
            result = snapshot(args.source, args.parent, args.environment)
        print(result)
        return 0
    except (OSError, ValueError) as exc:
        parser.exit(4 if args.command in {"promotion-preflight", "runtime-preflight"} else 1,
                    f"deployment release rejected: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
