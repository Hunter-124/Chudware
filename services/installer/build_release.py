"""Assemble a source-free, signed Chudware Windows customer payload.

Release construction is deliberately two-phase. ``--prepare-signing`` stages compiled
workers and runtime assets, writes canonical ``payload-manifest.v1.json``, and emits the
exact domain-separated preimage outside the payload. After offline Ed25519 signing,
``--finalize-signing <sig>`` re-verifies every byte, writes the canonical signature
sidecar, and may create the installer/archive. An unverified customer payload is never
archived or passed to Inno Setup.

Developer provisioning remains available through ``services.installer.provision`` and
is not copied into customer releases.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from services.installer.payload_manifest import (
    MANIFEST_NAME,
    SIGNATURE_NAME,
    PayloadManifestError,
    build_manifest,
    create_verified_zip,
    finalize_signature,
    payload_signature_preimage,
    read_manifest,
    verify_manifest_payload,
    verify_signature,
    verify_snapshot,
    write_manifest,
    write_signing_preimage,
)

REPO = Path(__file__).resolve().parent.parent.parent
HOST_EXE = REPO / "host" / "build-win" / "bin" / "chudware.exe"
if not HOST_EXE.exists():
    HOST_EXE = REPO / "host" / "build" / "bin" / "Release" / "chudware.exe"
UNINSTALL_HELPER_EXE = HOST_EXE.parent / "chudware-uninstall-helper.exe"
# CMake stages the complete GUI runtime next to chudware.exe. Release packaging copies
# that verified tree rather than maintaining a brittle executable/resource allowlist.
FORBIDDEN_RUNTIME_SUFFIXES = {
    ".c",
    ".cc",
    ".cpp",
    ".cxx",
    ".h",
    ".hh",
    ".hpp",
    ".inl",
    ".py",
    ".pyc",
    ".pyo",
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".map",
    ".pdb",
}
FORBIDDEN_RUNTIME_DIRS = {
    ".git",
    ".github",
    ".svn",
    "__pycache__",
    "node_modules",
    "CMakeFiles",
}
REQUIRED_WINDOWS_RUNTIME = (
    "chudware.exe",
    "chudware_cef_helper.exe",
    "libcef.dll",
    "chrome_elf.dll",
    "icudtl.dat",
    "v8_context_snapshot.bin",
    "resources.pak",
    "chrome_100_percent.pak",
    "chrome_200_percent.pak",
)
HOSTD_EXE_CANDIDATES = (
    REPO / "build" / "windows-occt" / "bin" / "chudware-hostd.exe",
    REPO / "build" / "windows-core" / "bin" / "chudware-hostd.exe",
    REPO / "core" / "build" / "bin" / "Release" / "chudware-hostd.exe",
    REPO / "core" / "build" / "bin" / "chudware-hostd.exe",
)
VERSION_HEADER_CANDIDATES = (
    HOST_EXE.parent.parent / "generated" / "api" / "chudware_version.h",
    REPO / "host" / "build-win" / "generated" / "api" / "chudware_version.h",
    REPO / "host" / "build" / "generated" / "api" / "chudware_version.h",
)
ISS_SCRIPT = REPO / "packaging" / "windows" / "chudware.iss"
VERSION_RE = re.compile(r'^#define CHUDWARE_VERSION\s+"([^"]+)"$', re.MULTILINE)


def _copy_required(source: Path | None, destination: Path, label: str) -> None:
    if source is None or not source.is_file() or source.is_symlink():
        raise RuntimeError(f"required compiled {label} not found or unsafe: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def _runtime_file_allowed(relative: Path) -> bool:
    if any(
        part in FORBIDDEN_RUNTIME_DIRS or part.endswith(".dSYM")
        for part in relative.parts
    ):
        return False
    name = relative.name.lower()
    return (
        relative.suffix.lower() not in FORBIDDEN_RUNTIME_SUFFIXES
        and not name.startswith("test_")
        and not re.search(r"_test\.[^.]+$", name)
    )


def stage_compiled_payload(
    dst: Path,
    *,
    host_bin: Path,
    hostd_bin: Path,
    worker_bin: Path,
    mcp_bin: Path,
    chud_bin: Path | None,
    uninstall_helper_bin: Path,
) -> None:
    """Stage the complete CMake-produced GUI runtime plus protected sidecars."""
    runtime_root = host_bin.parent
    missing = [
        name for name in REQUIRED_WINDOWS_RUNTIME if not (runtime_root / name).is_file()
    ]
    locales = runtime_root / "locales"
    assets = runtime_root / "assets"
    if (
        missing
        or not assets.is_dir()
        or not locales.is_dir()
        or not any(locales.glob("*.pak"))
    ):
        details = ", ".join(missing) or "assets/locales closure"
        raise RuntimeError(
            f"incomplete Windows GUI runtime beside {host_bin}: {details}"
        )
    for source in sorted(runtime_root.rglob("*")):
        relative = source.relative_to(runtime_root)
        if source.is_symlink():
            raise RuntimeError(f"unsafe runtime symlink: {source}")
        if source.is_dir():
            continue
        if not _runtime_file_allowed(relative):
            continue
        target = dst / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    _copy_required(hostd_bin, dst / "chudware-hostd.exe", "host daemon")
    _copy_required(worker_bin, dst / "chudware-worker.exe", "worker")
    _copy_required(mcp_bin, dst / "chudware-mcp.exe", "MCP server")
    if chud_bin is None:
        raise RuntimeError("required compiled Chudcode artifact not found")
    from scripts import pin_chudcode

    try:
        pin_chudcode.stage_binary(chud_bin, dst / "chud.exe", "windows-x86_64")
        pin_chudcode.stage_notices(dst)
    except pin_chudcode.ArtifactLockError as exc:
        raise RuntimeError(f"invalid Chudcode release artifact: {exc}") from exc
    shutil.copy2(pin_chudcode.DEFAULT_LOCK, dst / "CHUDCODE_ARTIFACT.lock")
    _copy_required(
        uninstall_helper_bin, dst / "chudware-uninstall-helper.exe", "uninstall helper"
    )
    for name in ("LICENSE", "THIRD_PARTY_NOTICES.md"):
        shutil.copy2(REPO / name, dst / name)


def build(
    out: Path,
    *,
    version: str,
    key_id: str,
    created_at: str | None,
    make_zip: bool,
    prepare_signing: bool,
    payload_signature: Path | None,
    release_public_key: str | None,
    host_bin: Path | None,
    hostd_bin: Path | None,
    worker_bin: Path | None,
    mcp_bin: Path | None,
    chud_bin: Path | None,
    uninstall_helper_bin: Path | None,
    platform: str = "windows",
    arch: str = "x86_64",
) -> dict[str, Any]:
    """Stage and authenticate a source-free customer payload.

    ``prepare_signing`` emits the exact signing preimage outside the payload and
    intentionally cannot archive or compile an installer.  Finalization restages the
    same deterministic payload, verifies the supplied detached signature, and writes
    its canonical base64 sidecar adjacent to the manifest.
    """
    if prepare_signing:
        if payload_signature is not None or make_zip:
            raise RuntimeError(
                "--prepare-signing cannot be combined with a signature or --zip"
            )
        preimage_path = out / "payload-manifest.v1.preimage"
        if preimage_path.exists():
            raise RuntimeError(
                f"refusing to replace existing signing preimage: {preimage_path}"
            )
    elif payload_signature is None or release_public_key is None:
        raise RuntimeError(
            "customer payload finalization requires --payload-signature and --release-public-key"
        )
    dst = out / "Chudware"
    if dst.exists():
        raise RuntimeError(f"refusing to replace existing payload directory: {dst}")
    out.mkdir(parents=True, exist_ok=True)
    dst.mkdir()

    stage_compiled_payload(
        dst,
        host_bin=host_bin,
        hostd_bin=hostd_bin,
        worker_bin=worker_bin,
        uninstall_helper_bin=uninstall_helper_bin,
        mcp_bin=mcp_bin,
        chud_bin=chud_bin,
    )
    shutil.copy2(REPO / "services" / "installer" / "install.ps1", dst / "install.ps1")

    manifest, canonical_json, snapshots = build_manifest(
        dst,
        version=version,
        platform=platform,
        arch=arch,
        key_id=key_id,
        created_at=created_at,
    )
    manifest_path = write_manifest(dst, canonical_json)
    verify_snapshot(dst, snapshots)

    result: dict[str, Any] = {
        "dist": str(dst),
        "payload_manifest": str(manifest_path),
        "files": len(manifest["files"]),
        "key_id": key_id,
    }
    if prepare_signing:
        write_signing_preimage(preimage_path, key_id, canonical_json)
        result["signing_preimage"] = str(preimage_path)
        result["signing_preimage_sha256"] = hashlib.sha256(
            payload_signature_preimage(key_id, canonical_json)
        ).hexdigest()
        return result

    signature_path = finalize_signature(
        dst,
        key_id=key_id,
        canonical_json=canonical_json,
        signature_path=payload_signature,
        public_key=release_public_key,
    )
    on_disk_manifest, on_disk_json = read_manifest(dst)
    if on_disk_json != canonical_json or on_disk_manifest["key_id"] != key_id:
        raise PayloadManifestError(
            "payload manifest changed during signature finalization"
        )
    verify_signature(
        key_id=key_id,
        canonical_json=canonical_json,
        signature_path=signature_path,
        public_key=release_public_key,
    )
    verify_snapshot(dst, snapshots)
    result["payload_signature"] = str(signature_path)

    if make_zip:
        archive = create_verified_zip(dst, out / "Chudware-release.zip")
        result["zip"] = str(archive)
    return result


def finalize_prepared_payload(
    out: Path,
    *,
    signature_source: Path,
    release_public_key: str,
    make_zip: bool,
) -> dict[str, Any]:
    """Verify and finalize a payload previously emitted by ``--prepare-signing``."""
    dst = out / "Chudware"
    manifest_path = dst / MANIFEST_NAME
    if (dst / SIGNATURE_NAME).exists():
        raise RuntimeError(
            f"prepared payload is already finalized: {dst / SIGNATURE_NAME}"
        )
    manifest, canonical_json = read_manifest(dst)
    manifest, snapshots = verify_manifest_payload(dst, canonical_json)
    key_id = str(manifest["key_id"])
    signature_path = finalize_signature(
        dst,
        key_id=key_id,
        canonical_json=canonical_json,
        signature_path=signature_source,
        public_key=release_public_key,
    )
    on_disk_manifest, on_disk_json = read_manifest(dst)
    if on_disk_json != canonical_json or on_disk_manifest["key_id"] != key_id:
        raise PayloadManifestError(
            "payload manifest changed during signature finalization"
        )
    verify_signature(
        key_id=key_id,
        canonical_json=canonical_json,
        signature_path=signature_path,
        public_key=release_public_key,
    )
    verify_snapshot(dst, snapshots)
    result: dict[str, Any] = {
        "dist": str(dst),
        "version": str(manifest["version"]),
        "payload_manifest": str(manifest_path),
        "payload_signature": str(signature_path),
        "files": len(manifest["files"]),
        "key_id": key_id,
    }
    if make_zip:
        result["zip"] = str(create_verified_zip(dst, out / "Chudware-release.zip"))
    return result


def write_artifact_signing_request(
    artifact: Path, key_id: str, version: str, platform: str, arch: str
) -> Path:
    """Emit the identity-bound offline artifact-signing request after creation."""
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    request = artifact.with_name(artifact.name + ".artifact-signing-request.bin")
    request.write_bytes(
        b"\0".join(
            (
                b"chudware-artifact-v1",
                key_id.encode("ascii"),
                version.encode("ascii"),
                platform.encode("ascii"),
                arch.encode("ascii"),
                digest.encode("ascii"),
            )
        )
    )
    return request


def read_built_version() -> str:
    """Read the exact CHUDWARE_VERSION compiled into the staged Windows binary."""
    for header in VERSION_HEADER_CANDIDATES:
        if not header.is_file():
            continue
        match = VERSION_RE.search(header.read_text(encoding="utf-8"))
        if match:
            return match.group(1)
        raise RuntimeError(f"generated version header is malformed: {header}")
    searched = ", ".join(str(path) for path in VERSION_HEADER_CANDIDATES)
    raise RuntimeError(
        "generated chudware_version.h not found; configure/build the Windows host first "
        f"(searched: {searched})"
    )


def find_tool(
    env_name: str, executable: str, candidates: tuple[Path, ...] = ()
) -> str | None:
    configured = os.environ.get(env_name)
    if configured:
        return configured
    found = shutil.which(executable)
    if found:
        return found
    return next((str(path) for path in candidates if path.is_file()), None)


def sign_installer(installer: Path) -> None:
    identity = os.environ.get("CHUDWARE_SIGN_IDENTITY", "")
    notarize_profile = os.environ.get("CHUDWARE_NOTARIZE_PROFILE", "")
    if not identity:
        print(
            "WARNING: unsigned build — SmartScreen/Gatekeeper will warn users",
            file=sys.stderr,
        )
        return
    if notarize_profile:
        print(
            "note: CHUDWARE_NOTARIZE_PROFILE is macOS-only and is ignored on Windows",
            file=sys.stderr,
        )
    program_files_x86 = Path(
        os.environ.get("ProgramFiles(x86)", r"C:\\Program Files (x86)")
    )
    signtool = find_tool(
        "SIGNTOOL",
        "signtool.exe",
        tuple(program_files_x86.glob("Windows Kits/10/bin/*/x64/signtool.exe")),
    )
    if not signtool:
        raise RuntimeError(
            "CHUDWARE_SIGN_IDENTITY is set but signtool.exe was not found; install the "
            "Windows 10/11 SDK or set SIGNTOOL to its full path"
        )
    subprocess.run(
        [
            signtool,
            "sign",
            "/n",
            identity,
            "/fd",
            "SHA256",
            "/tr",
            "http://timestamp.digicert.com",
            "/td",
            "SHA256",
            str(installer),
        ],
        check=True,
    )
    subprocess.run([signtool, "verify", "/pa", "/v", str(installer)], check=True)


def compile_installer(
    out: Path, payload: Path, version: str, release_public_key: str
) -> Path:
    program_files_x86 = Path(
        os.environ.get("ProgramFiles(x86)", r"C:\\Program Files (x86)")
    )
    iscc = find_tool(
        "ISCC",
        "ISCC.exe",
        (program_files_x86 / "Inno Setup 6" / "ISCC.exe",),
    )
    if not iscc:
        raise RuntimeError(
            "ISCC.exe was not found; install Inno Setup 6 or set ISCC to its full path"
        )
    if not (payload / "chudware.exe").is_file():
        raise RuntimeError(
            "staged payload has no chudware.exe; build the Windows host first"
        )
    manifest_path = payload / MANIFEST_NAME
    signature_path = payload / SIGNATURE_NAME
    manifest, canonical_json = read_manifest(payload)
    verify_signature(
        key_id=str(manifest["key_id"]),
        canonical_json=canonical_json,
        signature_path=signature_path,
        public_key=release_public_key,
    )
    manifest, payload_snapshot = verify_manifest_payload(payload, canonical_json)
    helper_rows = [
        row
        for row in manifest["files"]
        if row["path"] == "chudware-uninstall-helper.exe"
    ]
    if len(helper_rows) != 1 or helper_rows[0]["kind"] != "executable":
        raise RuntimeError(
            "signed payload manifest must own exactly one executable "
            "chudware-uninstall-helper.exe"
        )
    uninstall_helper_sha256 = str(helper_rows[0]["sha256"])
    installer = out / f"Chudware-Setup-{version}-x86_64.exe"
    if installer.exists():
        raise RuntimeError(f"refusing to replace existing installer: {installer}")
    subprocess.run(
        [
            iscc,
            str(ISS_SCRIPT),
            f"/DPayloadDir={payload.resolve()}",
            f"/DAppVersion={version}",
            f"/DInstallerOutputDir={out.resolve()}",
            f"/DUninstallHelperSha256={uninstall_helper_sha256}",
        ],
        cwd=REPO,
        check=True,
    )
    if not installer.is_file():
        raise RuntimeError(f"Inno Setup succeeded but did not produce {installer}")
    try:
        verify_snapshot(payload, payload_snapshot)
    except PayloadManifestError:
        installer.unlink(missing_ok=True)
        raise
    sign_installer(installer)
    return installer


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        description="Assemble the signed Chudware Windows installer."
    )
    ap.add_argument("--out", type=Path, default=REPO / "dist")
    ap.add_argument(
        "--zip",
        action="store_true",
        help="also retain a deterministic zip of the authenticated payload",
    )
    ap.add_argument(
        "--payload-only",
        action="store_true",
        help="stage/finalize the payload without invoking Inno Setup",
    )
    phase = ap.add_mutually_exclusive_group()
    phase.add_argument(
        "--prepare-signing",
        action="store_true",
        help="stage payload and emit its exact signing preimage",
    )
    phase.add_argument(
        "--finalize-signing",
        type=Path,
        metavar="SIG",
        help="finalize an existing prepared payload with an offline signature",
    )
    ap.add_argument(
        "--payload-signature",
        type=Path,
        metavar="SIG",
        help="one-pass finalization with a signature for this deterministic payload",
    )
    ap.add_argument(
        "--release-public-key",
        default=os.environ.get("CHUDWARE_RELEASE_PUBLIC_KEY"),
        help="Ed25519 public key PEM path or canonical base64",
    )
    ap.add_argument(
        "--key-id", default=os.environ.get("CHUDWARE_RELEASE_KEY_ID", "release-v1")
    )
    ap.add_argument(
        "--created-at",
        help="canonical UTC timestamp; defaults reproducibly from inputs",
    )
    ap.add_argument("--version", help="override version-header discovery")
    ap.add_argument("--host-bin", type=Path, default=HOST_EXE)
    ap.add_argument("--uninstall-helper-bin", type=Path, default=UNINSTALL_HELPER_EXE)
    ap.add_argument(
        "--hostd-bin",
        type=Path,
        default=next((path for path in HOSTD_EXE_CANDIDATES if path.is_file()), None),
    )
    ap.add_argument(
        "--worker-bin",
        type=Path,
        default=Path(os.environ["CHUDWARE_WORKER_BIN"])
        if os.environ.get("CHUDWARE_WORKER_BIN")
        else None,
    )
    ap.add_argument(
        "--mcp-bin",
        type=Path,
        default=Path(os.environ["CHUDWARE_MCP_BIN"])
        if os.environ.get("CHUDWARE_MCP_BIN")
        else None,
    )
    ap.add_argument(
        "--chud-bin",
        type=Path,
        default=Path(os.environ["CHUDWARE_CHUD_BIN"])
        if os.environ.get("CHUDWARE_CHUD_BIN")
        else None,
    )
    args = ap.parse_args(argv)
    try:
        if args.finalize_signing is not None:
            if args.payload_signature is not None:
                raise RuntimeError(
                    "--finalize-signing and --payload-signature are mutually exclusive"
                )
            if not args.release_public_key:
                raise RuntimeError("--finalize-signing requires --release-public-key")
            res = finalize_prepared_payload(
                args.out,
                signature_source=args.finalize_signing,
                release_public_key=args.release_public_key,
                make_zip=args.zip,
            )
            version = str(res["version"])
        else:
            if not args.key_id:
                raise RuntimeError(
                    "staging requires --key-id (or CHUDWARE_RELEASE_KEY_ID)"
                )
            if args.prepare_signing and not args.payload_only:
                raise RuntimeError("--prepare-signing requires --payload-only")
            version = args.version or read_built_version()
            res = build(
                args.out,
                version=version,
                key_id=args.key_id,
                created_at=args.created_at,
                make_zip=args.zip,
                prepare_signing=args.prepare_signing,
                payload_signature=args.payload_signature,
                release_public_key=args.release_public_key,
                host_bin=args.host_bin,
                hostd_bin=args.hostd_bin,
                worker_bin=args.worker_bin,
                mcp_bin=args.mcp_bin,
                chud_bin=args.chud_bin,
                uninstall_helper_bin=args.uninstall_helper_bin,
            )
            res["version"] = version
        if not args.payload_only:
            installer = compile_installer(
                args.out, Path(res["dist"]), version, args.release_public_key
            )
            res["installer"] = str(installer)
            res["artifact_signing_request"] = str(
                write_artifact_signing_request(
                    installer, str(res["key_id"]), version, "windows", "x86_64"
                )
            )
    except (
        OSError,
        RuntimeError,
        PayloadManifestError,
        subprocess.CalledProcessError,
    ) as exc:
        print(f"build_release: error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(res, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
