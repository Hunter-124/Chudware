#!/usr/bin/env python3
"""Pin, verify, fetch, and stage compiled Chudcode release artifacts.

Release packaging calls ``stage`` with an explicit local input.  That path is
strictly offline: only the explicit ``fetch`` subcommand performs network I/O.
"""
from __future__ import annotations

import argparse
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
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
DEFAULT_LOCK = REPO / "vendor" / "chudcode" / "CHUDCODE_ARTIFACT.lock"
SCHEMA = "chudcode-artifact-lock-v1"
RPC_PROTOCOL = 2
SEMVER_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?$")
SUPPORTED_TARGETS = (
    "linux-x86_64",
    "linux-aarch64",
    "macos-x86_64",
    "macos-arm64",
    "windows-x86_64",
)
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


def load_lock(path: Path = DEFAULT_LOCK, *, require_pinned: bool = True) -> dict[str, Any]:
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
        "artifacts", "engine_version", "repository", "repository_commit",
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
    artifacts = lock["artifacts"]
    if not isinstance(artifacts, dict) or tuple(sorted(artifacts)) != tuple(sorted(SUPPORTED_TARGETS)):
        raise ArtifactLockError("artifact lock must define exactly every supported platform target")
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
        completed = subprocess.run(
            [str(path.resolve()), "--version"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ArtifactLockError(
            f"could not execute Chudcode artifact for product-version verification: {path}"
        ) from exc
    actual = completed.stdout.strip()
    expected = f"Chudcode v{lock['version']}"
    if lock["engine_version"] is not None:
        expected += f" (based on omp {lock['engine_version']})"
    if completed.returncode != 0 or actual != expected:
        raise ArtifactLockError(
            "Chudcode product version mismatch: "
            f"expected {expected!r}, got {actual!r} (exit {completed.returncode})"
        )
    return actual


def verify_binary(
    path: Path, target: str, *, lock_path: Path | None = None
) -> dict[str, Any]:
    lock = load_lock(lock_path or DEFAULT_LOCK)
    if target not in SUPPORTED_TARGETS:
        raise ArtifactLockError(f"unsupported Chudcode artifact target: {target}")
    if not path.is_file() or path.is_symlink():
        raise ArtifactLockError(f"required Chudcode binary is missing or unsafe: {path}")
    actual_target = _binary_target(path)
    if actual_target != target:
        raise ArtifactLockError(
            f"wrong-target Chudcode binary: expected {target}, found {actual_target}"
        )
    actual_hash = _sha256(path)
    expected = lock["artifacts"][target]["sha256"]
    if actual_hash != expected:
        raise ArtifactLockError(
            f"Chudcode binary sha256 mismatch for {target}: "
            f"expected {expected}, found {actual_hash}"
        )
    version_output = _verify_executable_version(path, target, lock)
    return {
        "path": str(path),
        "target": target,
        "sha256": actual_hash,
        "version": lock["version"],
        "rpc_protocol": lock["rpc_protocol"],
        "repository_commit": lock["repository_commit"],
        "engine_version": lock["engine_version"],
        "version_output": version_output,
    }


def stage_binary(
    source: Path, destination: Path, target: str, *, lock_path: Path | None = None
) -> dict[str, Any]:
    lock_path = lock_path or DEFAULT_LOCK
    evidence = verify_binary(source, target, lock_path=lock_path)
    expected_name = CANONICAL_NAMES[target]
    if destination.name != expected_name:
        raise ArtifactLockError(
            f"staged Chudcode binary must be named {expected_name}, not {destination.name}"
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".tmp")
    temporary.unlink(missing_ok=True)
    try:
        shutil.copy2(source, temporary)
        if not target.startswith("windows-"):
            temporary.chmod(temporary.stat().st_mode | 0o111)
        verify_binary(temporary, target, lock_path=lock_path)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    evidence["staged"] = str(destination)
    return evidence


def fetch_artifact(
    destination: Path, target: str, *, lock_path: Path | None = None
) -> dict[str, Any]:
    lock_path = lock_path or DEFAULT_LOCK
    lock = load_lock(lock_path)
    artifact = lock["artifacts"].get(target)
    if artifact is None:
        raise ArtifactLockError(f"unsupported Chudcode artifact target: {target}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        prefix="chudcode-", delete=False, dir=destination.parent
    ) as stream:
        temporary = Path(stream.name)
        try:
            with urllib.request.urlopen(artifact["url"], timeout=120) as response:
                if response.geturl() != artifact["url"]:
                    raise ArtifactLockError(
                        "Chudcode artifact URL redirected; pin the final immutable URL"
                    )
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
    lock = {
        "artifacts": artifacts,
        "engine_version": args.engine_version,
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
        load_lock(temporary)
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
    pin.add_argument("--artifact", action="append", default=[], metavar="TARGET=PATH")
    pin.add_argument("--url", action="append", default=[], metavar="TARGET=HTTPS_URL")
    for name in ("verify", "stage", "fetch"):
        command = sub.add_parser(name)
        command.add_argument("--platform")
        command.add_argument("--arch")
        command.add_argument("--target", choices=SUPPORTED_TARGETS)
        if name != "fetch":
            command.add_argument("--input", type=Path, required=True)
        if name != "verify":
            command.add_argument("--output", type=Path, required=True)
    sub.add_parser("check-lock")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "pin":
            result = pin_lock(args)
        elif args.command == "check-lock":
            lock = load_lock(args.lock)
            result = {"lock": str(args.lock), "version": lock["version"], "rpc_protocol": lock["rpc_protocol"], "targets": len(lock["artifacts"])}
        else:
            target = args.target or (_target(args.platform, args.arch) if args.platform and args.arch else host_target())
            if args.command == "verify":
                result = verify_binary(args.input, target, lock_path=args.lock)
            elif args.command == "stage":
                result = stage_binary(args.input, args.output, target, lock_path=args.lock)
            else:
                result = fetch_artifact(args.output, target, lock_path=args.lock)
    except (ArtifactLockError, OSError, urllib.error.URLError) as exc:
        print(f"pin_chudcode: error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
