#!/usr/bin/env python3
"""Finalize an immutable prepared release handoff without rebuilding its payload."""
from __future__ import annotations

import argparse
import fnmatch
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
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
# The payload manifest has its OWN canonical form (no trailing newline): the
# detached signature and the C++ verifier both cover exactly those bytes.
from services.installer.payload_manifest import (  # noqa: E402
    PayloadManifestError,
    canonical_manifest_bytes,
)
try:
    from packaging.generate_release_evidence import validated_test_results
except ImportError:
    from generate_release_evidence import validated_test_results
try:
    from packaging.platform_publishers import PublisherPolicyError, load_policy, verify_artifact
except ImportError:
    from platform_publishers import PublisherPolicyError, load_policy, verify_artifact

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


def _parse_payload_manifest(raw: bytes) -> dict[str, Any]:
    """Parse a payload manifest with the PAYLOAD canonical form.

    services/installer/payload_manifest.py emits (and the C++ verifier in
    core/security/integrity.cpp checks) compact key-sorted JSON with NO
    trailing newline, because the detached signature covers exactly those
    bytes. release_json's canonical form requires a terminating LF and is for
    evidence/handoff documents; using it here rejected every real manifest
    with "payload manifest bytes are not canonical".
    """
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise FinalizeError("payload manifest is not UTF-8 JSON") from exc
    if not isinstance(parsed, dict):
        raise FinalizeError("payload manifest must be a JSON object")
    try:
        if canonical_manifest_bytes(parsed) != raw:
            raise FinalizeError("payload manifest bytes are not canonical")
    except PayloadManifestError as exc:
        raise FinalizeError(f"payload manifest is invalid: {exc}") from exc
    return parsed


def load_handoff(args: argparse.Namespace, staging: Path) -> tuple[dict[str, Any], Path, str]:
    manifest_path = _single("payload-manifest.v1.json", staging)
    try:
        payload = _parse_payload_manifest(manifest_path.read_bytes())
    except OSError as exc:
        raise FinalizeError(f"cannot read retained payload manifest: {exc}") from exc
    key_id = payload.get("key_id")
    if not isinstance(key_id, str) or not key_id:
        raise FinalizeError("retained payload manifest has no key_id")
    handoff_path = _single("handoff.json", staging)
    retained_metadata = handoff_path.read_bytes()
    if args.handoff_metadata is not None and args.handoff_metadata.read_bytes() != retained_metadata:
        raise FinalizeError("supplied handoff metadata differs from the retained canonical handoff")
    handoff = parse_canonical_json_bytes(retained_metadata, "prepared handoff", FinalizeError)
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
    try:
        _commands, result_files = validated_test_results(staging / "test-results.json")
    except (OSError, ValueError) as exc:
        raise FinalizeError(f"prepared handoff has invalid retained test results: {exc}") from exc
    for name, raw in result_files.items():
        bindings = [item for item in retained_locks if item.get("path") == name]
        if len(bindings) != 1 or bindings[0].get("sha256") != hashlib.sha256(raw).hexdigest():
            raise FinalizeError("retained test results must be hash-bound in prepared locked_inputs")
    _retained_publisher_policy(args, staging, handoff)
    return handoff, manifest_path, key_id

def _retained_publisher_policy(
    args: argparse.Namespace, staging: Path, metadata: dict[str, Any],
) -> tuple[Path, Path] | None:
    names = ("platform-publishers.v1.json", "platform-publishers.v1.sig")
    paths = tuple(staging / name for name in names)
    supplied = (getattr(args, "platform_publisher_policy", None),
                getattr(args, "platform_publisher_policy_signature", None))
    if bool(supplied[0]) != bool(supplied[1]):
        raise FinalizeError("publisher policy and detached signature must be supplied together")
    present = [path.is_file() for path in paths]
    if any(present) and not all(present):
        raise FinalizeError("retained publisher policy and detached signature must both exist")
    if not all(present):
        if any(supplied) or (args.channel == "stable" and args.platform in ("windows", "macos")):
            raise FinalizeError("stable platform release requires a retained owner-authorized publisher policy")
        return None
    for name, path, source in zip(names, paths, supplied):
        bindings = [item for item in metadata["locked_inputs"] if item.get("path") == name]
        if len(bindings) != 1 or bindings[0].get("sha256") != digest(path):
            raise FinalizeError("publisher policy must be hash-bound in prepared locked_inputs")
        if source is not None and source.read_bytes() != path.read_bytes():
            raise FinalizeError("supplied publisher policy differs from the prepared handoff")
    load_policy(*paths)
    return paths


def _reject_preexisting_artifacts(staging: Path) -> None:
    # A prepared handoff contains payloads, never installers left by an earlier
    # build. In particular, a failed native builder must not adopt retained bytes.
    patterns = ("Chudware-Setup-*.exe", "Chudware-*.msi", "Chudware-*.zip",
                "Chudware-*.AppImage", "Chudware-release.zip")
    for artifact in staging.rglob("*"):
        if any(fnmatch.fnmatchcase(artifact.name.lower(), pattern.lower()) for pattern in patterns):
            raise FinalizeError(f"prepared handoff contains a final artifact: {artifact.name}")



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
    for name in ("platform-publishers.v1.json", "platform-publishers.v1.sig"):
        source = staging / name
        if source.is_file():
            shutil.copyfile(source, support / name)
    snapshot = args.out / "builder"
    for relative in ("packaging/finalize_release.py", "packaging/generate_release_evidence.py", "packaging/release_json.py", "packaging/platform_publishers.py", "scripts/run_release_ctest.py", "packaging/macos/build_app.sh", "packaging/macos/Chudware.icns", "packaging/macos/RELEASE_NOTES.md", "packaging/windows/chudware.iss", "packaging/windows/chudware.wxs", "services/installer/build_release.py", "services/installer/build_msi.py", "services/installer/payload_manifest.py", "LICENSE-EULA.txt"):
        source = REPO / relative
        destination = snapshot / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
    return signature, public_key


def host_script(args: argparse.Namespace, handoff_dir: Path) -> Path:
    _reject_preexisting_artifacts(handoff_dir)
    load_handoff(args, handoff_dir)
    signature, public_key = _copy_portable_inputs(args, handoff_dir)
    channel = getattr(args, "channel", "beta")
    ext = ".ps1" if args.platform == "windows" else ".sh"
    output = args.out / f"finalize-{args.platform}-{args.arch}{ext}"
    if args.platform == "windows":
        command = (
            "Set-Location -LiteralPath $builder\n"
            "if ((Test-Path -LiteralPath $artifact) -or (Test-Path -LiteralPath $msi)) {\n"
            "  throw 'prepared handoff contains a final artifact'\n"
            "}\n"
            "Invoke-Native python -m services.installer.build_release --out $handoff --finalize-signing $signature "
            "--release-public-key $publicKey\n"
            "Invoke-Native python -m services.installer.build_msi --payload (Join-Path $handoff 'Chudware') "
            "--out $handoff --version " + repr(args.version) + " --release-public-key $publicKey\n"
            "if (-not (Test-Path -LiteralPath $artifact -PathType Leaf) -or "
            "-not (Test-Path -LiteralPath $msi -PathType Leaf)) { throw 'native builders did not produce both installers' }\n"
            "$evidence = Join-Path $root 'platform-signing.v1.json'\n"
            "$msiEvidence = Join-Path $root 'platform-signing-msi.v1.json'\n"
            "if (($env:CHUDWARE_RELEASE_CHANNEL -eq 'stable') -or "
            "((Test-Path -LiteralPath $publisherPolicy -PathType Leaf) -and "
            "(Test-Path -LiteralPath $publisherPolicySignature -PathType Leaf) -and $env:CHUDWARE_SIGN_IDENTITY)) {\n"
            "  if (-not $env:CHUDWARE_SIGN_IDENTITY) { throw 'stable Windows requires CHUDWARE_SIGN_IDENTITY' }\n"
            "  Invoke-Native python packaging/finalize_release.py platform-evidence $artifact --platform windows --kind authenticode --platform-publisher-policy $publisherPolicy --platform-publisher-policy-signature $publisherPolicySignature --out $evidence\n"
            "  Invoke-Native python packaging/finalize_release.py platform-evidence $msi --platform windows --kind authenticode --platform-publisher-policy $publisherPolicy --platform-publisher-policy-signature $publisherPolicySignature --out $msiEvidence\n"
            "  Invoke-Native python packaging/finalize_release.py complete $artifact --bundle-root $root --platform-signing-evidence $evidence\n"
            "  Invoke-Native python packaging/finalize_release.py windows-msi-complete $msi --bundle-root $root --platform-signing-evidence $msiEvidence\n"
            "} else {\n"
            "  Invoke-Native python packaging/finalize_release.py complete $artifact --bundle-root $root\n"
            "  Invoke-Native python packaging/finalize_release.py windows-msi-complete $msi --bundle-root $root\n"
            "}\n"
            "Write-Host \"MSI finalized at $msi; publish-bundle-msi.v1.json carries its signing requests and format msi.\""
        )
        text = (
            "$ErrorActionPreference = 'Stop'\n"
            "function Invoke-Native {\n"
            "  $program, $nativeArgs = $args\n"
            "  & $program @nativeArgs\n"
            "  if ($LASTEXITCODE -ne 0) { throw \"$program failed with exit code $LASTEXITCODE\" }\n"
            "}\n"
            "$root = Split-Path -Parent $MyInvocation.MyCommand.Path\n"
            "$builder = Join-Path $root 'builder'\n"
            "$handoff = Join-Path $root 'handoff'\n"
            "$signature = Join-Path $root 'portable-inputs/payload-manifest.v1.sig'\n"
            "$publicKey = Join-Path $root 'portable-inputs/release-public-key'\n"
            "$publisherPolicy = Join-Path $root 'portable-inputs/platform-publishers.v1.json'\n"
            "$publisherPolicySignature = Join-Path $root 'portable-inputs/platform-publishers.v1.sig'\n"
            "$artifact = Join-Path $handoff ('Chudware-Setup-' + " + repr(args.version) + " + '-x86_64.exe')\n"
            "$msi = Join-Path $handoff ('Chudware-' + " + repr(args.version) + " + '-x86_64.msi')\n"
            "$env:CHUDWARE_RELEASE_CHANNEL = " + repr(channel) + "\n" + command + "\n"
        )
    else:
        artifact = f"$ROOT/handoff/Chudware-{args.version}-macos-{args.arch}.zip"
        complete_command = f'python3 packaging/finalize_release.py complete "{artifact}" --bundle-root "$ROOT"'
        text = (
            '#!/usr/bin/env bash\nset -euo pipefail\nROOT="$(cd "$(dirname "$0")" && pwd)"\ncd "$ROOT/builder"\n'
            'packaging/macos/build_app.sh --out "$ROOT/handoff" --finalize-signing "$ROOT/portable-inputs/payload-manifest.v1.sig" --release-public-key "$ROOT/portable-inputs/release-public-key"\n'
            f"if [ {shlex.quote(channel)} = stable ] || "
            '{ [ -f "$ROOT/portable-inputs/platform-publishers.v1.json" ] && '
            '[ -f "$ROOT/portable-inputs/platform-publishers.v1.sig" ] && '
            '[ -n "${CHUDWARE_SIGN_IDENTITY:-}" ] && [ -n "${CHUDWARE_NOTARIZE_PROFILE:-}" ]; }; then\n'
            f'  python3 packaging/finalize_release.py platform-evidence "{artifact}" --platform macos --kind notarization '
            '--platform-publisher-policy "$ROOT/portable-inputs/platform-publishers.v1.json" '
            '--platform-publisher-policy-signature "$ROOT/portable-inputs/platform-publishers.v1.sig" '
            '--out "$ROOT/platform-signing.v1.json"\n'
            + complete_command + ' --platform-signing-evidence "$ROOT/platform-signing.v1.json"\n'
            + "else\n" + complete_command + "\nfi\n"
        )
    output.write_text(text, encoding="utf-8", newline="\n")
    output.chmod(0o700)
    return output




def finalize_linux(args: argparse.Namespace, handoff_dir: Path) -> Path:
    appdir = _single("AppDir", handoff_dir)
    manifest = _parse_payload_manifest(_single("payload-manifest.v1.json", handoff_dir).read_bytes())
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
               "--test-results", str(args.test_results), "--out", str(evidence)]
    for locked in locked_inputs:
        command += ["--locked-input", str(locked)]
    platform_report = getattr(args, "platform_signing_evidence", None)
    if platform_report is not None:
        handoff = args.bundle_root.resolve() / "handoff"
        command += ["--platform-signing-evidence", str(platform_report),
                    "--platform-publisher-policy", str(handoff / "platform-publishers.v1.json"),
                    "--platform-publisher-policy-signature", str(handoff / "platform-publishers.v1.sig")]
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
    if document.get("verified") is not True or any(document.get(key) != value for key, value in expected.items()):
        raise FinalizeError(f"stable {args.platform} release has invalid {required} evidence")
    root = args.bundle_root.resolve()
    policy = root / "portable-inputs/platform-publishers.v1.json"
    signature = root / "portable-inputs/platform-publishers.v1.sig"
    for portable in (policy, signature):
        retained = root / "handoff" / portable.name
        if portable.read_bytes() != retained.read_bytes():
            raise FinalizeError("portable publisher policy differs from the prepared handoff")
    verified = verify_artifact(args.artifact, args.platform, policy, signature)
    if any(document.get(key) != value for key, value in verified.items()):
        raise FinalizeError("platform evidence publisher or policy binding does not match native verification")


def complete(args: argparse.Namespace) -> int:
    root = args.bundle_root.resolve()
    handoff = root / "handoff"
    metadata, manifest_path, key_id = load_handoff(args, handoff)
    args.out = root
    args.artifact = args.artifact.resolve()
    _stable_platform_evidence(args)
    args.test_results = handoff / "test-results.json"
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
        "test_results": _relative(root / "evidence/retained-test-results/test-results.json", root, "test results"),
        "test_results_sha256": digest(root / "evidence/retained-test-results/test-results.json"),
    }
    path = root / "publish-bundle.v1.json"
    path.write_bytes(canonical_json_bytes(manifest))
    print(json.dumps(manifest, sort_keys=True))
    return 0


def complete_windows_msi(args: argparse.Namespace) -> int:
    """Generate isolated evidence/signing requests for the tagged MSI."""
    root = args.bundle_root.resolve()
    handoff = root / "handoff"
    metadata, manifest_path, key_id = load_handoff(args, handoff)
    if args.platform != "windows" or args.arch != "x86_64":
        raise FinalizeError("windows-msi-complete requires a windows/x86_64 handoff")
    args.artifact = args.artifact.resolve()
    args.out = root / "msi"
    args.out.mkdir(exist_ok=True)
    _stable_platform_evidence(args)
    args.test_results = handoff / "test-results.json"
    locks = [handoff / item["path"] for item in metadata["locked_inputs"]]
    requests = evidence_requests(args, args.artifact, locks)
    artifact_request = args.artifact.with_name(
        args.artifact.name + ".artifact-signing-request.bin"
    )
    artifact_request.write_bytes(b"\0".join((
        b"chudware-artifact-v1", key_id.encode("ascii"), args.version.encode("ascii"),
        b"windows", b"x86_64", digest(args.artifact).encode("ascii"),
    )))
    manifest = {
        "schema": "publish-bundle.v1",
        "format": "msi",
        "artifact": _relative(args.artifact, root, "artifact"),
        "artifact_sha256": digest(args.artifact),
        "artifact_signing_request": _relative(
            artifact_request, root, "artifact request"
        ),
        "evidence_signing_requests": [
            _relative(path, root, "evidence request") for path in requests
        ],
        "payload_manifest": _relative(manifest_path, root, "payload manifest"),
        "key_id": key_id,
        "platform": "windows",
        "arch": "x86_64",
        "version": args.version,
        "commit": args.commit,
        "channel": args.channel,
        "test_results": _relative(args.out / "evidence/retained-test-results/test-results.json", root, "test results"),
        "test_results_sha256": digest(args.out / "evidence/retained-test-results/test-results.json"),
    }
    path = root / "publish-bundle-msi.v1.json"
    path.write_bytes(canonical_json_bytes(manifest))
    print(json.dumps(manifest, sort_keys=True))
    return 0


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("handoff", type=Path)
    ap.add_argument("--payload-signature", type=Path, required=True)
    ap.add_argument("--release-public-key", required=True)
    ap.add_argument("--handoff-metadata", type=Path)
    ap.add_argument("--platform-publisher-policy", type=Path)
    ap.add_argument("--platform-publisher-policy-signature", type=Path)
    ap.add_argument("--platform", choices=("linux", "windows", "macos"), required=True)
    ap.add_argument("--arch", choices=("x86_64", "arm64"), required=True)
    ap.add_argument("--version", required=True); ap.add_argument("--commit", required=True)
    ap.add_argument("--channel", choices=("stable", "beta", "preview"), required=True)
    ap.add_argument("--locked-input", type=Path, action="append", default=[])
    ap.add_argument("--out", type=Path, required=True)
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
    ap.add_argument("--platform-publisher-policy", type=Path, required=True)
    ap.add_argument("--platform-publisher-policy-signature", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    return ap




def write_platform_evidence(args: argparse.Namespace) -> int:
    expected_kind = {"windows": "authenticode", "macos": "notarization"}[args.platform]
    if args.kind != expected_kind or not args.artifact.is_file():
        raise FinalizeError("platform evidence identity is invalid")
    verified = verify_artifact(args.artifact, args.platform,
                               args.platform_publisher_policy, args.platform_publisher_policy_signature)
    args.out.write_bytes(canonical_json_bytes({"schema": "platform-signing.v1", "platform": args.platform,
                                               "kind": args.kind, "verified": True, **verified}))
    return 0




def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    if raw and raw[0] == "platform-evidence":
        try:
            return write_platform_evidence(platform_evidence_parser().parse_args(raw[1:]))
        except (OSError, FinalizeError, PublisherPolicyError, subprocess.CalledProcessError) as exc:
            print(f"finalize_release: error: {exc}", file=sys.stderr)
            return 2
    if raw and raw[0] == "windows-msi-complete":
        try:
            partial = complete_parser().parse_args(raw[1:])
            handoff = parse_canonical_json_bytes(
                (partial.bundle_root / "handoff" / "handoff.json").read_bytes(),
                "prepared handoff",
                FinalizeError,
            )
            args = argparse.Namespace(
                artifact=partial.artifact,
                bundle_root=partial.bundle_root,
                platform_signing_evidence=partial.platform_signing_evidence,
                handoff_metadata=None,
                payload_signature=(
                    partial.bundle_root / "portable-inputs" / "payload-manifest.v1.sig"
                ),
                release_public_key=str(
                    partial.bundle_root / "portable-inputs" / "release-public-key"
                ),
                locked_input=[],
                **{
                    key: handoff[key]
                    for key in ("platform", "arch", "version", "commit", "channel")
                },
            )
            return complete_windows_msi(args)
        except (OSError, KeyError, FinalizeError, PublisherPolicyError, subprocess.CalledProcessError) as exc:
            print(f"finalize_release: error: {exc}", file=sys.stderr)
            return 2
    if raw and raw[0] == "complete":
        try:
            partial = complete_parser().parse_args(raw[1:])
            handoff = parse_canonical_json_bytes((partial.bundle_root / "handoff" / "handoff.json").read_bytes(), "prepared handoff", FinalizeError)
            args = argparse.Namespace(
                artifact=partial.artifact, bundle_root=partial.bundle_root, out=partial.bundle_root,
                platform_signing_evidence=partial.platform_signing_evidence, handoff_metadata=None,
                payload_signature=partial.bundle_root / "portable-inputs" / "payload-manifest.v1.sig",
                release_public_key=str(partial.bundle_root / "portable-inputs" / "release-public-key"),
                locked_input=[],
                **{key: handoff[key] for key in ("platform", "arch", "version", "commit", "channel")},
            )
            return complete(args)
        except (OSError, KeyError, FinalizeError, PublisherPolicyError, subprocess.CalledProcessError) as exc:
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
        _reject_preexisting_artifacts(staging)
        metadata, _manifest, _key_id = load_handoff(args, staging)
        if args.platform != "linux":
            script = host_script(args, staging)
            print(json.dumps({"status": "platform-host-required", "script": str(script)}, sort_keys=True))
            return 3
        artifact = finalize_linux(args, staging)
        completion = argparse.Namespace(**vars(args), artifact=artifact, bundle_root=args.out, platform_signing_evidence=None)
        return complete(completion)
    except (OSError, FinalizeError, PublisherPolicyError, subprocess.CalledProcessError, tarfile.TarError) as exc:
        print(f"finalize_release: error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
