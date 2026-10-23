#!/usr/bin/env python3
"""Pin, verify, fetch, and stage compiled Chudcode release artifacts and notices.

Release packaging calls ``stage`` and ``stage-notices`` with explicit local inputs.
Those paths are strictly offline: only the explicit ``fetch`` and ``ensure``
subcommands perform network I/O.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
import platform as host_platform
import re
import shutil
import struct
import sys
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
DEFAULT_LOCK = REPO / "vendor" / "chudcode" / "CHUDCODE_ARTIFACT.lock"
SCHEMA = "chudcode-artifact-lock-v1"
RPC_PROTOCOL = 2
NOTICE_FILES = {
    "CHUDCODE-NOTICE": "NOTICE",
    "CHUDCODE-THIRD-PARTY-NOTICES.txt": "THIRD-PARTY-NOTICES.txt",
}
LEGACY_NOTICE_NAMES = frozenset(
    {"OMP-NOTICE", "OMP-THIRD-PARTY-NOTICES.txt"}
)
DEFAULT_NOTICE_DIR = DEFAULT_LOCK.parent
SEMVER_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?$")
SUPPORTED_TARGETS = (
    "linux-x86_64",
    "macos-arm64",
    "macos-x86_64",
    "windows-x86_64",
)
# Targets that older retained locks may still list; they load for verification
# but are never published.
LEGACY_TARGETS = ("linux-aarch64",)
CANONICAL_NAMES = {target: ("chud.exe" if target.startswith("windows-") else "chud") for target in SUPPORTED_TARGETS}


class ArtifactLockError(RuntimeError):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii") + b"\n"


def _target(platform_name: str, arch: str) -> str:
    platforms = {"linux": "linux", "darwin": "macos", "macos": "macos", "windows": "windows", "win32": "windows"}
    arches = {"x86_64": "x86_64", "amd64": "x86_64", "aarch64": "aarch64", "arm64": "arm64"}
    system = platforms.get(platform_name.casefold())
    machine = arches.get(arch.casefold())
    if system == "linux" and machine == "arm64":
        machine = "aarch64"
    if system == "macos" and machine == "aarch64":
        machine = "arm64"
    target = f"{system}-{machine}"
    if system is None or machine is None or target not in SUPPORTED_TARGETS:
        raise ArtifactLockError(f"unsupported Chudcode artifact target: {platform_name}-{arch}")
    return target


def host_target() -> str:
    return _target(sys.platform, host_platform.machine())


def _require_string(value: Any, label: str, *, pattern: str | None = None) -> str:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise ArtifactLockError(f"{label} must be a non-empty trimmed string")
    if pattern == "sha" and (len(value) != 64 or any(c not in "0123456789abcdef" for c in value)):
        raise ArtifactLockError(f"{label} must be a lowercase sha256")
    if pattern == "commit" and (len(value) != 40 or any(c not in "0123456789abcdef" for c in value)):
        raise ArtifactLockError(f"{label} must be a 40-character lowercase repository commit")
    return value


def load_lock(
    path: Path = DEFAULT_LOCK, *, require_pinned: bool = True, strict_targets: bool = False
) -> dict[str, Any]:
    if path.is_symlink():
        raise ArtifactLockError(f"Chudcode artifact lock must not be a symlink: {path}")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ArtifactLockError(f"Chudcode artifact lock is missing: {path}") from exc
    try:
        lock = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ArtifactLockError(f"Chudcode artifact lock is not valid JSON: {path}") from exc
    try:
        canonical = _canonical_json(lock)
    except (TypeError, UnicodeEncodeError) as exc:
        raise ArtifactLockError(f"Chudcode artifact lock is not canonical ASCII JSON: {path}") from exc
    if canonical != raw:
        raise ArtifactLockError(f"Chudcode artifact lock is not canonical JSON: {path}")
    if not isinstance(lock, dict) or set(lock) != {
        "artifacts", "engine_version", "notices", "repository", "repository_commit",
        "rpc_protocol", "schema", "version",
    }:
        raise ArtifactLockError("Chudcode artifact lock has unexpected or missing top-level keys")
    if lock["schema"] != SCHEMA:
        raise ArtifactLockError(f"unsupported Chudcode artifact lock schema: {lock['schema']!r}")
    product_version = _require_string(lock["version"], "version")
    if not SEMVER_RE.fullmatch(product_version):
        raise ArtifactLockError("version must be a Chudcode semantic product version")
    engine_version = lock["engine_version"]
    if engine_version is not None:
        engine_version = _require_string(engine_version, "engine_version")
        if not SEMVER_RE.fullmatch(engine_version):
            raise ArtifactLockError("engine_version must be a semantic provenance version")
    repository = _require_string(lock["repository"], "repository")
    parsed_repository = urllib.parse.urlparse(repository)
    if (parsed_repository.scheme != "https" or not parsed_repository.netloc
            or parsed_repository.username or parsed_repository.password):
        raise ArtifactLockError("repository must be an unauthenticated HTTPS URL")
    _require_string(lock["repository_commit"], "repository_commit", pattern="commit")
    if type(lock["rpc_protocol"]) is not int or lock["rpc_protocol"] != RPC_PROTOCOL:
        raise ArtifactLockError(f"rpc_protocol must be {RPC_PROTOCOL}")
    notices = lock["notices"]
    if not isinstance(notices, dict) or set(notices) != {"files", "repository_commit"}:
        raise ArtifactLockError("notice provenance has unexpected or missing keys")
    if notices["repository_commit"] != lock["repository_commit"]:
        raise ArtifactLockError("notice provenance repository commit must match the artifact source commit")
    notice_files = notices["files"]
    if not isinstance(notice_files, dict) or set(notice_files) != set(NOTICE_FILES):
        raise ArtifactLockError("notice provenance must define exactly every Chudcode notice")
    for payload_name, source_name in NOTICE_FILES.items():
        notice = notice_files[payload_name]
        if not isinstance(notice, dict) or set(notice) != {"sha256", "source"}:
            raise ArtifactLockError(f"notice {payload_name} has unexpected or missing keys")
        if notice["source"] != source_name:
            raise ArtifactLockError(
                f"notice {payload_name} must record source filename {source_name}"
            )
        _require_string(
            notice["sha256"], f"notice {payload_name} sha256", pattern="sha"
        )
    artifacts = lock["artifacts"]
    if not isinstance(artifacts, dict) or not set(SUPPORTED_TARGETS) <= set(artifacts):
        raise ArtifactLockError("artifact lock must define every supported platform target")
    # Older retained locks may also list legacy targets; verification still loads
    # them. Only publication (pin/check-lock) demands the exact set.
    extra = set(artifacts) - set(SUPPORTED_TARGETS)
    if extra and (strict_targets or not extra <= set(LEGACY_TARGETS)):
        raise ArtifactLockError(
            "artifact lock must define exactly every supported platform target; "
            f"unexpected: {', '.join(sorted(extra))}"
        )
    for target in extra:
        if not isinstance(artifacts[target], dict) or set(artifacts[target]) != {"filename", "sha256", "url"}:
            raise ArtifactLockError(f"legacy artifact {target} has unexpected or missing keys")
    for target in SUPPORTED_TARGETS:
        artifact = artifacts[target]
        if not isinstance(artifact, dict) or set(artifact) != {"filename", "sha256", "url"}:
            raise ArtifactLockError(f"artifact {target} has unexpected or missing keys")
        if artifact["filename"] != CANONICAL_NAMES[target]:
            raise ArtifactLockError(f"artifact {target} must use canonical filename {CANONICAL_NAMES[target]}")
        if artifact["url"] is None or artifact["sha256"] is None:
            if require_pinned:
                raise ArtifactLockError(f"artifact {target} is not pinned; run pin_chudcode.py pin")
            if artifact["url"] is not None or artifact["sha256"] is not None:
                raise ArtifactLockError(f"artifact {target} must set url and sha256 together")
            continue
        url = _require_string(artifact["url"], f"artifact {target} url")
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
            raise ArtifactLockError(f"artifact {target} URL must be an unauthenticated HTTPS URL")
        _require_string(artifact["sha256"], f"artifact {target} sha256", pattern="sha")
    return lock


def verify_notices(
    directory: Path | None = None,
    *,
    lock_path: Path | None = None,
    require_pinned: bool = True,
) -> dict[str, Any]:
    directory = directory or DEFAULT_NOTICE_DIR
    lock_path = lock_path or DEFAULT_LOCK
    lock = load_lock(lock_path, require_pinned=require_pinned)
    if not directory.is_dir() or directory.is_symlink():
        raise ArtifactLockError(
            f"Chudcode notice directory is missing or unsafe: {directory}"
        )
    for legacy_name in LEGACY_NOTICE_NAMES:
        if (directory / legacy_name).exists():
            raise ArtifactLockError(
                f"stale OMP notice is forbidden in Chudcode notice directory: "
                f"{directory / legacy_name}"
            )
    evidence: dict[str, str] = {}
    for payload_name in NOTICE_FILES:
        path = directory / payload_name
        if not path.is_file() or path.is_symlink():
            raise ArtifactLockError(
                f"required Chudcode notice is missing or unsafe: {path}"
            )
        actual = _sha256(path)
        expected = lock["notices"]["files"][payload_name]["sha256"]
        if actual != expected:
            raise ArtifactLockError(
                f"Chudcode notice sha256 mismatch for {payload_name}: "
                f"expected {expected}, found {actual}"
            )
        evidence[payload_name] = actual
    return {
        "directory": str(directory),
        "notices": evidence,
        "repository_commit": lock["notices"]["repository_commit"],
    }


def stage_notices(
    destination: Path,
    *,
    source: Path | None = None,
    lock_path: Path | None = None,
) -> dict[str, Any]:
    source = source or DEFAULT_NOTICE_DIR
    lock_path = lock_path or DEFAULT_LOCK
    # Notices are pinned by their own hashes; they do not wait on every binary target.
    evidence = verify_notices(source, lock_path=lock_path, require_pinned=False)
    if destination.exists() and (
        not destination.is_dir() or destination.is_symlink()
    ):
        raise ArtifactLockError(
            f"Chudcode notice destination is not a safe directory: {destination}"
        )
    destination.mkdir(parents=True, exist_ok=True)
    for legacy_name in LEGACY_NOTICE_NAMES:
        if (destination / legacy_name).exists():
            raise ArtifactLockError(
                f"stale OMP notice is forbidden in Chudcode payload: "
                f"{destination / legacy_name}"
            )
    temporaries: list[tuple[Path, Path]] = []
    try:
        for payload_name in NOTICE_FILES:
            target = destination / payload_name
            temporary = target.with_name(target.name + ".tmp")
            temporary.unlink(missing_ok=True)
            temporaries.append((temporary, target))
            shutil.copy2(source / payload_name, temporary)
            actual = _sha256(temporary)
            expected = evidence["notices"][payload_name]
            if actual != expected:
                raise ArtifactLockError(
                    f"staged Chudcode notice sha256 mismatch for {payload_name}: "
                    f"expected {expected}, found {actual}"
                )
        for temporary, target in temporaries:
            os.replace(temporary, target)
    finally:
        for temporary, _target_path in temporaries:
            temporary.unlink(missing_ok=True)
    evidence["staged"] = str(destination)
    return evidence


def _binary_target(path: Path) -> str:
    try:
        with path.open("rb") as stream:
            data = stream.read(4096)
    except OSError as exc:
        raise ArtifactLockError(f"Chudcode binary is unreadable: {path}") from exc
    if len(data) < 20:
        raise ArtifactLockError(f"Chudcode binary is too short to identify: {path}")
    if data.startswith(b"\x7fELF"):
        endian = "<" if data[5] == 1 else ">" if data[5] == 2 else None
        if endian is None:
            raise ArtifactLockError(f"invalid ELF byte order: {path}")
        machine = struct.unpack_from(endian + "H", data, 18)[0]
        arches = {62: "x86_64", 183: "aarch64"}
        if machine not in arches:
            raise ArtifactLockError(f"unsupported ELF machine {machine}: {path}")
        return f"linux-{arches[machine]}"
    if data.startswith(b"MZ"):
        if len(data) < 64:
            raise ArtifactLockError(f"truncated PE binary: {path}")
        pe_offset = struct.unpack_from("<I", data, 0x3C)[0]
        if pe_offset + 6 > len(data) or data[pe_offset:pe_offset + 4] != b"PE\0\0":
            raise ArtifactLockError(f"invalid PE binary: {path}")
        machine = struct.unpack_from("<H", data, pe_offset + 4)[0]
        if machine != 0x8664:
            raise ArtifactLockError(f"unsupported PE machine 0x{machine:04x}: {path}")
        return "windows-x86_64"
    magic = data[:4]
    thin = {
        b"\xcf\xfa\xed\xfe": ("<", "macos"),
        b"\xfe\xed\xfa\xcf": (">", "macos"),
    }
    if magic in thin:
        endian, system = thin[magic]
        cpu = struct.unpack_from(endian + "I", data, 4)[0]
        arches = {0x01000007: "x86_64", 0x0100000C: "arm64"}
        if cpu not in arches:
            raise ArtifactLockError(f"unsupported Mach-O CPU 0x{cpu:08x}: {path}")
        return f"{system}-{arches[cpu]}"
    raise ArtifactLockError(f"unrecognized executable format: {path}")


def _verify_executable_version(
    path: Path, target: str, lock: dict[str, Any]
) -> str:
    current = host_target()
    if target != current:
        raise ArtifactLockError(
            f"cannot executable-verify {target} Chudcode artifact on {current}"
        )
    try:
        with tempfile.TemporaryDirectory(prefix="version-home-", dir=path.parent) as directory:
            environment = {
                name: os.environ[name]
                for name in ("SystemRoot", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP")
                if name in os.environ
            }
            environment.update({
                "PATH": os.defpath, "HOME": directory, "USERPROFILE": directory,
                "APPDATA": directory, "LOCALAPPDATA": directory,
                "XDG_CONFIG_HOME": directory, "XDG_DATA_HOME": directory,
                "XDG_CACHE_HOME": directory,
            })
            completed = subprocess.run(
                [str(path.resolve()), "--version"], capture_output=True,
                text=True, timeout=10, check=False, cwd=directory, env=environment,
            )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ArtifactLockError(
            f"could not execute Chudcode artifact for product-version verification: {path}"
        ) from exc
    actual = completed.stdout.strip()
    expected = f"Chudcode v{lock['version']}"
    if completed.returncode != 0 or actual != expected:
        raise ArtifactLockError(
            "Chudcode product version mismatch: "
            f"expected {expected!r}, got {actual!r} (exit {completed.returncode})"
        )
    return actual


def _pinned_artifact(lock: dict[str, Any], target: str) -> dict[str, Any]:
    """The artifact row for ``target``, refusing an unpinned one.

    Verify/stage/fetch act on ONE target, so only that target's pin is load-bearing.
    Demanding the whole lock be pinned (which check-lock still does) made staging
    impossible on every platform until the last one was pinned: production went
    unshippable waiting on a Windows artifact it never uses.
    """
    artifact = lock["artifacts"].get(target)
    if artifact is None:
        raise ArtifactLockError(f"unsupported Chudcode artifact target: {target}")
    if artifact["url"] is None or artifact["sha256"] is None:
        raise ArtifactLockError(f"artifact {target} is not pinned; run pin_chudcode.py pin")
    return artifact


@contextmanager
def _verified_snapshot(path: Path, target: str, lock_path: Path):
    lock = load_lock(lock_path, require_pinned=False)
    if target not in SUPPORTED_TARGETS:
        raise ArtifactLockError(f"unsupported Chudcode artifact target: {target}")
    if not path.is_file() or path.is_symlink():
        raise ArtifactLockError(f"required Chudcode binary is missing or unsafe: {path}")
    with tempfile.TemporaryDirectory(prefix="chudware-verified-artifact-") as directory:
        snapshot = Path(directory) / CANONICAL_NAMES[target]
        # External paths are never executed. A concurrent input change can only
        # make this private copy fail its target/digest checks.
        with path.open("rb") as source, snapshot.open("xb") as output:
            shutil.copyfileobj(source, output)
        snapshot.chmod(0o700)
        actual_target = _binary_target(snapshot)
        if actual_target != target:
            raise ArtifactLockError(
                f"wrong-target Chudcode binary: expected {target}, found {actual_target}"
            )
        actual_hash = _sha256(snapshot)
        expected = _pinned_artifact(lock, target)["sha256"]
        if actual_hash != expected:
            raise ArtifactLockError(
                f"Chudcode binary sha256 mismatch for {target}: expected {expected}, found {actual_hash}"
            )
        version_output = _verify_executable_version(snapshot, target, lock)
        yield snapshot, {
            "path": str(path), "target": target, "sha256": actual_hash,
            "version": lock["version"], "rpc_protocol": lock["rpc_protocol"],
            "repository_commit": lock["repository_commit"],
            "engine_version": lock["engine_version"], "version_output": version_output,
        }


def verify_binary(
    path: Path, target: str, *, lock_path: Path | None = None
) -> dict[str, Any]:
    with _verified_snapshot(path, target, lock_path or DEFAULT_LOCK) as (_, evidence):
        return evidence


def stage_binary(
    source: Path, destination: Path, target: str, *, lock_path: Path | None = None
) -> dict[str, Any]:
    lock_path = lock_path or DEFAULT_LOCK
    if target not in SUPPORTED_TARGETS:
        raise ArtifactLockError(f"unsupported Chudcode artifact target: {target}")
    expected_name = CANONICAL_NAMES[target]
    if destination.name != expected_name:
        raise ArtifactLockError(
            f"staged Chudcode binary must be named {expected_name}, not {destination.name}"
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    with _verified_snapshot(source, target, lock_path) as (snapshot, evidence):
        fd, name = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
        temporary = Path(name)
        try:
            with os.fdopen(fd, "wb") as output, snapshot.open("rb") as input_file:
                shutil.copyfileobj(input_file, output)
            temporary.chmod(0o700)
            if _sha256(temporary) != evidence["sha256"]:
                raise ArtifactLockError("staged Chudcode snapshot changed before publication")
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
    evidence["staged"] = str(destination)
    return evidence


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})
_GITHUB_ASSET_HOSTS = frozenset({"objects.githubusercontent.com", "release-assets.githubusercontent.com"})


def _is_github_release_download(url: str) -> bool:
    parsed = urllib.parse.urlparse(url)
    parts = parsed.path.split("/")
    return (parsed.scheme == "https" and parsed.netloc == "github.com"
            and len(parts) == 7 and parts[3:5] == ["releases", "download"]
            and not parsed.query and not parsed.fragment)


def _open_pinned_url(url: str):
    """Open a pinned artifact URL without credentials.

    GitHub serves a release download through exactly one redirect to its asset
    CDN; that hop is followed and every other redirect is refused. The lock's
    SHA-256, not the transport, authenticates the downloaded bytes.
    """
    opener = urllib.request.build_opener(_NoRedirect())
    try:
        return opener.open(url, timeout=120)
    except urllib.error.HTTPError as exc:
        if exc.code not in _REDIRECT_CODES:
            raise
        location = exc.headers.get("Location", "")
        exc.close()
    if not _is_github_release_download(url):
        raise ArtifactLockError("Chudcode artifact URL redirected; pin the final immutable URL")
    destination = urllib.parse.urlparse(location)
    if (destination.scheme != "https" or destination.hostname not in _GITHUB_ASSET_HOSTS
            or destination.username or destination.password):
        raise ArtifactLockError("GitHub release asset redirected outside its HTTPS asset CDN")
    return opener.open(location, timeout=120)


def fetch_artifact(
    destination: Path, target: str, *, lock_path: Path | None = None,
) -> dict[str, Any]:
    lock_path = lock_path or DEFAULT_LOCK
    lock = load_lock(lock_path, require_pinned=False)
    artifact = _pinned_artifact(lock, target)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        prefix="chudcode-", delete=False, dir=destination.parent
    ) as stream:
        temporary = Path(stream.name)
        try:
            with _open_pinned_url(artifact["url"]) as response:
                shutil.copyfileobj(response, stream)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
    try:
        if not target.startswith("windows-"):
            temporary.chmod(temporary.stat().st_mode | 0o111)
        return stage_binary(temporary, destination, target, lock_path=lock_path)
    finally:
        temporary.unlink(missing_ok=True)


def ensure_artifact(
    cache_dir: Path, target: str, *, source: Path | None = None,
    lock_path: Path | None = None,
) -> dict[str, Any]:
    """Use an explicit verified input or a digest-keyed, verified download."""
    lock_path = lock_path or DEFAULT_LOCK
    if source is not None:
        return verify_binary(source, target, lock_path=lock_path)
    lock = load_lock(lock_path, require_pinned=False)
    artifact = _pinned_artifact(lock, target)
    destination = cache_dir / artifact["sha256"] / CANONICAL_NAMES[target]
    if destination.exists() or destination.is_symlink():
        # A damaged cache is a failure, not permission to replace an explicit
        # installation or execute untrusted bytes. Remove it deliberately first.
        return verify_binary(destination, target, lock_path=lock_path)
    fetch_artifact(destination, target, lock_path=lock_path)
    return verify_binary(destination, target, lock_path=lock_path)


def _parse_assignments(values: list[str], label: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for value in values:
        target, separator, item = value.partition("=")
        if not separator or target not in SUPPORTED_TARGETS or not item or target in parsed:
            raise ArtifactLockError(f"invalid or duplicate {label}: {value!r}")
        parsed[target] = item
    if set(parsed) != set(SUPPORTED_TARGETS):
        missing = ", ".join(sorted(set(SUPPORTED_TARGETS) - set(parsed)))
        raise ArtifactLockError(f"{label} must define every supported target; missing: {missing}")
    return parsed


def pin_lock(args: argparse.Namespace) -> dict[str, Any]:
    binaries = _parse_assignments(args.artifact, "--artifact TARGET=PATH")
    urls = _parse_assignments(args.url, "--url TARGET=URL")
    artifacts: dict[str, Any] = {}
    for target in SUPPORTED_TARGETS:
        path = Path(binaries[target])
        if not path.is_file() or path.is_symlink():
            raise ArtifactLockError(
                f"required Chudcode pin input is missing or unsafe: {path}"
            )
        actual = _binary_target(path)
        if actual != target:
            raise ArtifactLockError(f"wrong-target Chudcode binary for {target}: found {actual}")
        url = urls[target]
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
            raise ArtifactLockError(f"artifact {target} URL must be an unauthenticated HTTPS URL")
        artifacts[target] = {"filename": CANONICAL_NAMES[target], "sha256": _sha256(path), "url": url}
    notice_files: dict[str, dict[str, str]] = {}
    for payload_name, source_name in NOTICE_FILES.items():
        path = args.notice_dir / payload_name
        if not path.is_file() or path.is_symlink():
            raise ArtifactLockError(
                f"required Chudcode notice pin input is missing or unsafe: {path}"
            )
        notice_files[payload_name] = {
            "sha256": _sha256(path),
            "source": source_name,
        }
    lock = {
        "artifacts": artifacts,
        "engine_version": args.engine_version,
        "notices": {
            "files": notice_files,
            "repository_commit": args.repository_commit,
        },
        "repository": args.repository,
        "repository_commit": args.repository_commit,
        "rpc_protocol": args.rpc_protocol,
        "schema": SCHEMA,
        "version": args.version,
    }
    args.lock.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        prefix=args.lock.name + ".", delete=False, dir=args.lock.parent
    ) as stream:
        temporary = Path(stream.name)
        stream.write(_canonical_json(lock))
    try:
        load_lock(temporary, strict_targets=True)
        verify_notices(args.notice_dir, lock_path=temporary)
        current = host_target()
        verify_binary(Path(binaries[current]), current, lock_path=temporary)
        os.replace(temporary, args.lock)
    finally:
        temporary.unlink(missing_ok=True)
    return {
        "lock": str(args.lock),
        "targets": len(artifacts),
        "version": args.version,
        "engine_version": args.engine_version,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock", type=Path, default=DEFAULT_LOCK)
    sub = parser.add_subparsers(dest="command", required=True)
    pin = sub.add_parser("pin", help="atomically pin one verified binary and URL per release target")
    pin.add_argument("--version", required=True)
    pin.add_argument("--engine-version")
    pin.add_argument("--rpc-protocol", type=int, choices=(RPC_PROTOCOL,), required=True)
    pin.add_argument("--repository", default="https://github.com/Hunter-124/chudcode.git")
    pin.add_argument("--repository-commit", required=True)
    pin.add_argument("--notice-dir", type=Path, default=DEFAULT_NOTICE_DIR)
    pin.add_argument("--artifact", action="append", default=[], metavar="TARGET=PATH")
    pin.add_argument("--url", action="append", default=[], metavar="TARGET=HTTPS_URL")
    for name in ("verify", "stage", "fetch", "ensure"):
        command = sub.add_parser(name)
        command.add_argument("--platform")
        command.add_argument("--arch")
        command.add_argument("--target", choices=SUPPORTED_TARGETS)
        if name not in ("fetch", "ensure"):
            command.add_argument("--input", type=Path, required=True)
        if name not in ("verify", "ensure"):
            command.add_argument("--output", type=Path, required=True)
        if name == "ensure":
            command.add_argument("--cache-dir", type=Path, required=True)
            command.add_argument("--input", type=Path)
    sub.add_parser("check-lock")
    verify_notice_parser = sub.add_parser(
        "verify-notices",
        help="verify locked notices independently of binary target availability",
    )
    verify_notice_parser.add_argument(
        "--source-dir", type=Path, default=DEFAULT_NOTICE_DIR
    )
    stage_notice_parser = sub.add_parser(
        "stage-notices",
        help="verify and stage the two artifact-locked Chudcode notices",
    )
    stage_notice_parser.add_argument(
        "--source-dir", type=Path, default=DEFAULT_NOTICE_DIR
    )
    stage_notice_parser.add_argument("--output-dir", type=Path, required=True)
    return parser


def _selected_target(args: argparse.Namespace) -> str:
    has_target = args.target is not None
    has_platform = args.platform is not None
    has_arch = args.arch is not None
    if has_target:
        if has_platform or has_arch:
            raise ArtifactLockError(
                "--target cannot be combined with --platform or --arch"
            )
        return args.target
    if has_platform != has_arch:
        raise ArtifactLockError("--platform and --arch must be provided together")
    if not has_platform:
        raise ArtifactLockError(
            "exactly one target selector is required: --target or --platform plus --arch"
        )
    return _target(args.platform, args.arch)


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "pin":
            result = pin_lock(args)
        elif args.command == "check-lock":
            lock = load_lock(args.lock, strict_targets=True)
            notice_evidence = verify_notices(args.lock.parent, lock_path=args.lock)
            result = {
                "lock": str(args.lock),
                "version": lock["version"],
                "rpc_protocol": lock["rpc_protocol"],
                "targets": len(lock["artifacts"]),
                "notices": notice_evidence["notices"],
            }
        elif args.command == "verify-notices":
            result = verify_notices(
                args.source_dir, lock_path=args.lock, require_pinned=False
            )
        elif args.command == "stage-notices":
            result = stage_notices(
                args.output_dir, source=args.source_dir, lock_path=args.lock
            )
        else:
            target = _selected_target(args)
            if args.command == "verify":
                result = verify_binary(args.input, target, lock_path=args.lock)
            elif args.command == "stage":
                result = stage_binary(args.input, args.output, target, lock_path=args.lock)
            elif args.command == "ensure":
                result = ensure_artifact(
                    args.cache_dir, target, source=args.input, lock_path=args.lock,
                )
            else:
                result = fetch_artifact(args.output, target, lock_path=args.lock)
    except (ArtifactLockError, OSError, urllib.error.URLError) as exc:
        print(f"pin_chudcode: error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
