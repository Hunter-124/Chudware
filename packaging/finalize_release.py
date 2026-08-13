#!/usr/bin/env python3
"""Finalize an immutable prepared release handoff without rebuilding its payload."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path
from typing import Any

try:
    from packaging.release_json import canonical_json_bytes, parse_canonical_json_bytes
except ImportError:
    from release_json import canonical_json_bytes, parse_canonical_json_bytes

REPO = Path(__file__).resolve().parent.parent
_DOMAINS = {
    "sbom.cdx.json": b"chudware-sbom-v1\0",
    "provenance.slsa.json": b"chudware-provenance-v1\0",
    "platform-test.v1.json": b"chudware-platform-test-v1\0",
}


class FinalizeError(RuntimeError):
    pass


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def safe_extract(archive: Path, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "r:*") as bundle:
        root = out.resolve()
        for member in bundle.getmembers():
            destination = (out / member.name).resolve()
            if root not in destination.parents and destination != root:
                raise FinalizeError(f"handoff contains unsafe path: {member.name}")
            if member.issym() or member.islnk():
                raise FinalizeError(f"handoff contains unsupported link: {member.name}")
        bundle.extractall(out, filter="data")


def _single(pattern: str, root: Path) -> Path:
    matches = list(root.rglob(pattern))
    if len(matches) != 1:
        raise FinalizeError(f"handoff must contain exactly one {pattern}, found {len(matches)}")
    return matches[0]


def _relative(path: Path, root: Path, label: str) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError as exc:
        raise FinalizeError(f"{label} must be retained inside the portable finalize bundle") from exc


def load_handoff(args: argparse.Namespace, staging: Path) -> tuple[dict[str, Any], Path, str]:
    manifest_path = _single("payload-manifest.v1.json", staging)
    try:
        payload = parse_canonical_json_bytes(manifest_path.read_bytes(), "payload manifest", FinalizeError)
    except OSError as exc:
        raise FinalizeError(f"cannot read retained payload manifest: {exc}") from exc
    key_id = payload.get("key_id")
    if not isinstance(key_id, str) or not key_id:
        raise FinalizeError("retained payload manifest has no key_id")
    handoff_path = args.handoff_metadata
    if handoff_path is None:
        candidates = list(staging.rglob("handoff.json"))
        if len(candidates) != 1:
            raise FinalizeError("exactly one prepared handoff.json is required")
        handoff_path = candidates[0]
    handoff = parse_canonical_json_bytes(handoff_path.read_bytes(), "prepared handoff", FinalizeError)
    expected = {
        "commit": args.commit,
        "version": args.version,
        "channel": args.channel,
        "platform": args.platform,
        "arch": args.arch,
    }
    mismatches = [field for field, value in expected.items() if handoff.get(field) != value]
    if mismatches:
        raise FinalizeError("prepared handoff identity mismatch for " + ", ".join(mismatches))
    retained_locks = handoff.get("locked_inputs")
    if not isinstance(retained_locks, list) or not retained_locks or not all(isinstance(item, dict) for item in retained_locks):
        raise FinalizeError("prepared handoff must bind nonempty locked_inputs")
    for item in retained_locks:
        relative = item.get("path")
        expected_digest = item.get("sha256")
        if not isinstance(relative, str) or Path(relative).is_absolute() or ".." in Path(relative).parts:
            raise FinalizeError("prepared handoff contains an unsafe locked input path")
        lock = staging / relative
        if not lock.is_file() or digest(lock) != expected_digest:
            raise FinalizeError(f"prepared locked input was replaced: {relative}")
    if args.locked_input:
        supplied = {digest(path) for path in args.locked_input if path.is_file()}
        retained = {item["sha256"] for item in retained_locks}
        if supplied != retained:
            raise FinalizeError("supplied locked inputs do not match prepared handoff")
    return handoff, manifest_path, key_id


def _copy_portable_inputs(args: argparse.Namespace, staging: Path) -> tuple[Path, Path]:
    support = args.out / "portable-inputs"
    support.mkdir()
    signature = support / "payload-manifest.v1.sig"
    shutil.copyfile(args.payload_signature, signature)
    public_key = support / "release-public-key"
    key_source = Path(args.release_public_key)
    if key_source.is_file():
        shutil.copyfile(key_source, public_key)
    else:
        public_key.write_text(args.release_public_key + "\n", encoding="ascii")
    snapshot = args.out / "builder"
    for relative in ("packaging/finalize_release.py", "packaging/generate_release_evidence.py", "packaging/release_json.py", "packaging/macos/build_app.sh", "packaging/macos/Chudware.icns", "packaging/macos/RELEASE_NOTES.md", "packaging/windows/chudware.iss", "services/installer/build_release.py", "services/installer/payload_manifest.py", "LICENSE-EULA.txt"):
        source = REPO / relative
        destination = snapshot / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
    return signature, public_key


def host_script(args: argparse.Namespace, handoff_dir: Path) -> Path:
    signature, public_key = _copy_portable_inputs(args, handoff_dir)
    channel = getattr(args, "channel", "beta")
    ext = ".ps1" if args.platform == "windows" else ".sh"
    output = args.out / f"finalize-{args.platform}-{args.arch}{ext}"
    if args.platform == "windows":
        command = (
            "Set-Location $builder\n"
            "python -m services.installer.build_release --out $handoff --finalize-signing $signature "
            "--release-public-key $publicKey\n"
            "$evidence = Join-Path $root 'platform-signing.v1.json'\n"
            "if ($env:CHUDWARE_RELEASE_CHANNEL -eq 'stable') {\n"
            "  if (-not $env:CHUDWARE_SIGN_IDENTITY) { throw 'stable Windows requires CHUDWARE_SIGN_IDENTITY' }\n"
            "  & signtool.exe verify /pa /v $artifact\n"
            "  python packaging/finalize_release.py platform-evidence $artifact --platform windows --kind authenticode --out $evidence\n"
            "  python packaging/finalize_release.py complete $artifact --bundle-root $root --platform-signing-evidence $evidence\n"
            "} else { python packaging/finalize_release.py complete $artifact --bundle-root $root }"
        )
        text = "$ErrorActionPreference = 'Stop'\n$root = Split-Path -Parent $MyInvocation.MyCommand.Path\n$builder = Join-Path $root 'builder'\n$handoff = Join-Path $root 'handoff'\n$signature = Join-Path $root 'portable-inputs/payload-manifest.v1.sig'\n$publicKey = Join-Path $root 'portable-inputs/release-public-key'\n$artifact = Join-Path $handoff ('Chudware-Setup-' + " + repr(args.version) + " + '-x86_64.exe')\n$env:CHUDWARE_RELEASE_CHANNEL = " + repr(channel) + "\n" + command + "\n"
    else:
        stable = ""
        if channel == "stable":
            stable = "codesign --verify --deep --strict --verbose=2 \"$ROOT/handoff/Chudware.app\"\nxcrun stapler validate \"$ROOT/handoff/Chudware.app\"\nspctl --assess --type execute --verbose=2 \"$ROOT/handoff/Chudware.app\"\npython3 packaging/finalize_release.py platform-evidence \"$ROOT/handoff/Chudware-" + args.version + "-macos-" + args.arch + ".zip\" --platform macos --kind notarization --out \"$ROOT/platform-signing.v1.json\"\n"
        evidence_arg = " --platform-signing-evidence \"$ROOT/platform-signing.v1.json\"" if channel == "stable" else ""
        text = "#!/usr/bin/env bash\nset -euo pipefail\nROOT=\"$(cd \"$(dirname \"$0\")\" && pwd)\"\ncd \"$ROOT/builder\"\n" + \
            "packaging/macos/build_app.sh --out \"$ROOT/handoff\" --finalize-signing \"$ROOT/portable-inputs/payload-manifest.v1.sig\" --release-public-key \"$ROOT/portable-inputs/release-public-key\"\n" + stable + \
            f"python3 packaging/finalize_release.py complete \"$ROOT/handoff/Chudware-{args.version}-macos-{args.arch}.zip\" --bundle-root \"$ROOT\"{evidence_arg}\n"
    output.write_text(text, encoding="utf-8", newline="\n")
    output.chmod(0o700)
    return output




def finalize_linux(args: argparse.Namespace, handoff_dir: Path) -> Path:
    appdir = _single("AppDir", handoff_dir)
    manifest = parse_canonical_json_bytes(_single("payload-manifest.v1.json", handoff_dir).read_bytes(), "payload manifest", FinalizeError)
    command = [str(REPO / "packaging/linux/build_appimage.sh"), "--out", str(appdir.parent),
               "--key-id", str(manifest["key_id"]), "--finalize-signing", str(args.payload_signature.resolve()),
               "--release-public-key", args.release_public_key]
    subprocess.run(command, cwd=REPO, check=True)
    artifacts = list(appdir.parent.glob("Chudware-*-linux-x86_64.AppImage"))
    if len(artifacts) != 1:
        raise FinalizeError(f"Linux finalizer produced {len(artifacts)} AppImages")
    return artifacts[0]


def evidence_requests(args: argparse.Namespace, artifact: Path, locked_inputs: list[Path]) -> list[Path]:
    evidence = args.out / "evidence"
    command = [sys.executable, str(REPO / "packaging/generate_release_evidence.py"), str(artifact),
               "--artifact-sha256", digest(artifact), "--version", args.version,
               "--platform", args.platform, "--arch", args.arch, "--commit", args.commit,
               "--passed", "--out", str(evidence)]
    for locked in locked_inputs:
        command += ["--locked-input", str(locked)]
    for test_command in args.command:
        command += ["--command", test_command]
    subprocess.run(command, cwd=REPO, check=True)
    requests = []
    for name, domain in _DOMAINS.items():
        body = evidence / name
        request = evidence / (name + ".signing-request.bin")
        request.write_bytes(domain + body.read_bytes())
        requests.append(request)
    return requests


def _stable_platform_evidence(args: argparse.Namespace) -> None:
    if args.channel != "stable":
        return
    required = {"windows": "authenticode", "macos": "notarization"}.get(args.platform)
    if required is None:
        return
    if args.platform_signing_evidence is None:
        raise FinalizeError(f"stable {args.platform} release requires verified {required} evidence")
    document = parse_canonical_json_bytes(args.platform_signing_evidence.read_bytes(), "platform signing evidence", FinalizeError)
    expected = {"schema": "platform-signing.v1", "platform": args.platform, "artifact_sha256": digest(args.artifact), "verified": True, "kind": required}
    if any(document.get(key) != value for key, value in expected.items()):
        raise FinalizeError(f"stable {args.platform} release has invalid {required} evidence")


def complete(args: argparse.Namespace) -> int:
    root = args.bundle_root.resolve()
    handoff = root / "handoff"
    metadata, manifest_path, key_id = load_handoff(args, handoff)
    args.out = root
    args.artifact = args.artifact.resolve()
    _stable_platform_evidence(args)
    locks = [handoff / item["path"] for item in metadata["locked_inputs"]]
    requests = evidence_requests(args, args.artifact, locks)
    artifact_request = args.artifact.with_name(args.artifact.name + ".artifact-signing-request.bin")
    artifact_request.write_bytes(b"\0".join((
        b"chudware-artifact-v1", key_id.encode("ascii"), args.version.encode("ascii"),
        args.platform.encode("ascii"), args.arch.encode("ascii"),
        digest(args.artifact).encode("ascii"),
    )))
    manifest = {
        "schema": "publish-bundle.v1", "artifact": _relative(args.artifact, root, "artifact"),
        "artifact_sha256": digest(args.artifact), "artifact_signing_request": _relative(artifact_request, root, "artifact request"),
        "evidence_signing_requests": [_relative(path, root, "evidence request") for path in requests],
        "payload_manifest": _relative(manifest_path, root, "payload manifest"), "key_id": key_id,
        "platform": args.platform, "arch": args.arch, "version": args.version,
        "commit": args.commit, "channel": args.channel,
    }
    path = root / "publish-bundle.v1.json"
    path.write_bytes(canonical_json_bytes(manifest))
    print(json.dumps(manifest, sort_keys=True))
    return 0


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("handoff", type=Path)
    ap.add_argument("--payload-signature", type=Path, required=True)
    ap.add_argument("--release-public-key", required=True)
    ap.add_argument("--handoff-metadata", type=Path)
    ap.add_argument("--platform", choices=("linux", "windows", "macos"), required=True)
    ap.add_argument("--arch", choices=("x86_64", "arm64"), required=True)
    ap.add_argument("--version", required=True); ap.add_argument("--commit", required=True)
    ap.add_argument("--channel", choices=("stable", "beta", "preview"), required=True)
    ap.add_argument("--locked-input", type=Path, action="append", default=[])
    ap.add_argument("--command", action="append", required=True); ap.add_argument("--out", type=Path, required=True)
    return ap


def complete_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Complete evidence and publish bundle generation on the platform host")
    ap.add_argument("artifact", type=Path)
    ap.add_argument("--bundle-root", type=Path, required=True)
    ap.add_argument("--platform-signing-evidence", type=Path)
    return ap
def platform_evidence_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Record evidence only after native verification commands succeed")
    ap.add_argument("artifact", type=Path)
    ap.add_argument("--platform", choices=("windows", "macos"), required=True)
    ap.add_argument("--kind", choices=("authenticode", "notarization"), required=True)
    ap.add_argument("--out", type=Path, required=True)
    return ap


def write_platform_evidence(args: argparse.Namespace) -> int:
    expected_kind = {"windows": "authenticode", "macos": "notarization"}[args.platform]
    if args.kind != expected_kind or not args.artifact.is_file():
        raise FinalizeError("platform evidence identity is invalid")
    args.out.write_bytes(canonical_json_bytes({"schema": "platform-signing.v1", "platform": args.platform, "kind": args.kind, "artifact_sha256": digest(args.artifact), "verified": True}))
    return 0




def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    if raw and raw[0] == "platform-evidence":
        try:
            return write_platform_evidence(platform_evidence_parser().parse_args(raw[1:]))
        except (OSError, FinalizeError) as exc:
            print(f"finalize_release: error: {exc}", file=sys.stderr)
            return 2
    if raw and raw[0] == "complete":
        try:
            partial = complete_parser().parse_args(raw[1:])
            handoff = parse_canonical_json_bytes((partial.bundle_root / "handoff" / "handoff.json").read_bytes(), "prepared handoff", FinalizeError)
            commands_path = partial.bundle_root / "handoff" / "test-commands.txt"
            commands = [line for line in commands_path.read_text(encoding="utf-8").splitlines() if line.strip()]
            args = argparse.Namespace(
                artifact=partial.artifact, bundle_root=partial.bundle_root, out=partial.bundle_root,
                platform_signing_evidence=partial.platform_signing_evidence, handoff_metadata=None,
                payload_signature=partial.bundle_root / "portable-inputs" / "payload-manifest.v1.sig",
                release_public_key=str(partial.bundle_root / "portable-inputs" / "release-public-key"),
                locked_input=[], command=commands,
                **{key: handoff[key] for key in ("platform", "arch", "version", "commit", "channel")},
            )
            return complete(args)
        except (OSError, KeyError, FinalizeError, subprocess.CalledProcessError) as exc:
            print(f"finalize_release: error: {exc}", file=sys.stderr)
            return 2
    args = parser().parse_args(raw)
    try:
        if not args.handoff.is_file() or not args.payload_signature.is_file():
            raise FinalizeError("handoff and payload signature must exist")
        args.out.mkdir(parents=True, exist_ok=True)
        staging = args.out / "handoff"
        if staging.exists():
            raise FinalizeError(f"refusing to replace staging directory: {staging}")
        safe_extract(args.handoff, staging)
        metadata, _manifest, _key_id = load_handoff(args, staging)
        if args.platform != "linux":
            script = host_script(args, staging)
            print(json.dumps({"status": "platform-host-required", "script": str(script)}, sort_keys=True))
            return 3
        artifact = finalize_linux(args, staging)
        completion = argparse.Namespace(**vars(args), artifact=artifact, bundle_root=args.out, platform_signing_evidence=None)
        return complete(completion)
    except (OSError, FinalizeError, subprocess.CalledProcessError, tarfile.TarError) as exc:
        print(f"finalize_release: error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
