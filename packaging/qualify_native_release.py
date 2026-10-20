#!/usr/bin/env python3
"""Inspect prerequisites or qualify existing native release bytes; never sign or publish.

`inspect --platform windows` is read-only and works on any host. `qualify` requires
Windows (not Wine) for EXE/MSI, or macOS for the archived Chudware.app ZIP. The
output uses the existing evidence and signing-request contracts, not an operator
assertion of promotion authority. Run separately for each EXE and MSI artifact.
"""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tempfile

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
try:
    from packaging import generate_release_evidence as evidence
    from packaging import platform_publishers as publishers
    from packaging.release_json import canonical_json_bytes
    from packaging.sign_release import DOMAINS
except ImportError:
    if str(Path(__file__).resolve().parent) not in sys.path:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
    import generate_release_evidence as evidence
    import platform_publishers as publishers
    from release_json import canonical_json_bytes
    from sign_release import DOMAINS

TEST_FILES = ("test-results.json", "core-junit.xml", "host-junit.xml", "core-ctest.log", "host-ctest.log")
EVIDENCE_KINDS = {"sbom.cdx.json": "sbom", "provenance.slsa.json": "provenance",
                  "platform-test.v1.json": "platform-test"}


class QualificationError(ValueError):
    pass


def native_host_error(target: str) -> str | None:
    if target == "windows":
        if sys.platform != "win32" or os.name != "nt" or platform.system() != "Windows":
            return "Authenticode qualification requires a genuine Windows host; Linux, WSL and Wine do not qualify"
        try:
            ntdll = ctypes.WinDLL("ntdll.dll")
            getattr(ntdll, "wine_get_version")
        except AttributeError:
            return None
        except OSError:
            return "cannot establish native Windows host identity"
        return "Wine is diagnostic-only, never native Windows qualification"
    if sys.platform != "darwin" or platform.system() != "Darwin":
        return "notarization qualification requires a genuine macOS host"
    return None


def _available(path: str | None) -> str | None:
    return path if path and Path(path).is_file() and os.access(path, os.X_OK) else None


def tool_inventory(target: str) -> dict:
    optional = {name: shutil.which(name) for name in ("powershell", "powershell.exe", "pwsh",
                                                     "wine", "wine64", "osslsigncode", "rcodesign")}
    if target == "windows":
        powershell = None
        signtool = None
        if sys.platform == "win32" and os.name == "nt" and native_host_error(target) is None:
            powershell = _available(publishers._windows_powershell_executable())
            from services.installer.build_release import find_tool
            root = Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
            signtool = find_tool("SIGNTOOL", "signtool.exe",
                                tuple(root.glob("Windows Kits/10/bin/*/x64/signtool.exe")))
        required = {"Windows PowerShell 5.1 (system Authenticode provider)": powershell,
                    "Windows SDK signtool.exe": signtool}
    else:
        required = {name: _available(publishers._mac_tool(name))
                    for name in ("ditto", "codesign", "xcrun", "spctl")}
    return {"required_native": required, "optional_diagnostic_only": optional,
            "executed": False,
            "limitation": "availability is not version, trust, publisher or notarization verification; alternative tools never authorize promotion"}


def inspect(args: argparse.Namespace) -> dict:
    policy_status = "not supplied"
    if bool(args.platform_publisher_policy) != bool(args.platform_publisher_policy_signature):
        policy_status = "policy and detached signature must be supplied together"
    elif args.platform_publisher_policy:
        try:
            policy = publishers.load_policy(args.platform_publisher_policy, args.platform_publisher_policy_signature)
            allowed = policy.certificate_sha256 if args.platform == "windows" else policy.team_ids
            policy_status = "owner authenticated" if allowed else "owner authenticated but authorizes no publishers for this platform"
        except (OSError, ValueError, RecursionError) as exc:
            policy_status = str(exc)
    tests_status = "not supplied"
    if args.test_results:
        try:
            evidence.validated_test_results(args.test_results)
            tests_status = "retained core/host identities and bytes validated; not a new test execution"
        except (OSError, ValueError, RecursionError) as exc:
            tests_status = str(exc)
    return {
        "status": "partial-diagnostic-only", "promotion_authorized": False,
        "platform": args.platform, "native_host_error": native_host_error(args.platform),
        "tools": tool_inventory(args.platform), "publisher_policy": policy_status,
        "retained_tests": tests_status,
        "required_inputs": {
            "artifact": "existing owner-authorized signed EXE or MSI" if args.platform == "windows" else "existing notarized/stapled ZIP containing only Chudware.app",
            "owner_policy": "chudware-platform-publishers-v1 canonical JSON plus offline release-v1 detached owner signature",
            "tests": list(TEST_FILES),
            "build_identity": ["version", "arch", "40-character source commit", "locked input files"],
            "output": "new private directory, distinct for every artifact; no existing output is replaced",
        },
        "qualify_argv": [sys.executable, "packaging/qualify_native_release.py", "qualify", "ARTIFACT",
                         "--platform", args.platform, "--version", "VERSION", "--arch", "x86_64",
                         "--commit", "SOURCE_COMMIT", "--test-results", "RETAINED/test-results.json",
                         "--locked-input", "REVIEWED_LOCK", "--platform-publisher-policy", "platform-publishers.v1.json",
                         "--platform-publisher-policy-signature", "platform-publishers.v1.sig", "--out", "NEW_QUALIFICATION_DIRECTORY"],
    }


def qualify(args: argparse.Namespace) -> dict:
    error = native_host_error(args.platform)
    if error:
        raise QualificationError(error)
    suffixes = (".exe", ".msi") if args.platform == "windows" else (".zip",)
    if args.artifact.suffix.lower() not in suffixes:
        raise QualificationError("artifact format does not match the native qualification platform")
    if not re.fullmatch(r"[0-9a-f]{40}", args.commit):
        raise QualificationError("commit must be the exact lowercase 40-character source identity")
    if not args.version or any(ord(value) < 32 for value in args.version):
        raise QualificationError("version must be a nonempty release identity without control characters")
    if args.out.exists() or args.out.is_symlink():
        raise QualificationError("refusing to replace an existing qualification output")
    if not args.out.parent.is_dir():
        raise QualificationError("qualification output parent must already exist")
    # Authenticate owner policy before invoking any native executable.
    policy = publishers.load_policy(args.platform_publisher_policy, args.platform_publisher_policy_signature)
    if not (policy.certificate_sha256 if args.platform == "windows" else policy.team_ids):
        raise QualificationError("owner policy authorizes no publishers for this platform")
    original_digest = publishers._digest(args.artifact)
    with tempfile.TemporaryDirectory(prefix="chudware-native-qualification-", dir=args.out.parent) as temporary:
        root = Path(temporary)
        captured: dict[Path, tuple[Path, str]] = {}

        def capture(source: Path, destination: Path) -> Path:
            raw = publishers.read_bounded(source)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(raw)
            captured[source] = (destination, hashlib.sha256(raw).hexdigest())
            return destination

        policy_path = capture(args.platform_publisher_policy, root / "platform-publishers.v1.json")
        signature_path = capture(args.platform_publisher_policy_signature, root / "platform-publishers.v1.sig")
        captured_policy = publishers.load_policy(policy_path, signature_path)
        if captured_policy.raw != policy.raw or captured_policy.signature != policy.signature:
            raise QualificationError("owner policy changed during qualification capture")
        test_root = root / "tests"
        for name in TEST_FILES:
            source = args.test_results if name == "test-results.json" else args.test_results.parent / name
            capture(source, test_root / name)
        test_results = test_root / "test-results.json"
        evidence.validated_test_results(test_results)
        locks = []
        labels = {}
        if len(args.locked_input) > 128:
            raise QualificationError("too many locked inputs")
        if len({path.resolve() for path in args.locked_input}) != len(args.locked_input):
            raise QualificationError("locked inputs must be distinct")
        for index, source in enumerate(args.locked_input):
            snapshot = capture(source, root / "locked-inputs" / str(index) / source.name)
            locks.append(snapshot)
            labels[snapshot] = source.as_posix()
        verified = publishers.verify_artifact(args.artifact, args.platform, policy_path, signature_path)
        if verified["artifact_sha256"] != original_digest:
            raise QualificationError("artifact changed before native qualification")
        report = {"schema": "platform-signing.v1", "platform": args.platform,
                  "kind": "authenticode" if args.platform == "windows" else "notarization",
                  "verified": True, **verified}
        report_path = root / "platform-signing.v1.json"
        report_path.write_bytes(canonical_json_bytes(report))
        staged = root / "evidence"
        generation_args = argparse.Namespace(
            artifact=args.artifact, artifact_sha256=original_digest, version=args.version,
            platform=args.platform, arch=args.arch, commit=args.commit,
            test_results=test_results, locked_input=locks, locked_input_names=labels,
            platform_signing_evidence=report_path, platform_publisher_policy=policy_path,
            platform_publisher_policy_signature=signature_path, out=staged,
        )
        # Reuses the producer's actual artifact re-verification; a supplied JSON
        # positive can never substitute for native trust and publisher membership.
        outputs = evidence.generate(generation_args)
        for output in outputs:
            request = output.with_name(output.name + ".signing-request.bin")
            request.write_bytes(DOMAINS[EVIDENCE_KINDS[output.name]] + output.read_bytes())
        shutil.copyfile(policy_path, staged / policy_path.name)
        shutil.copyfile(signature_path, staged / signature_path.name)
        shutil.copytree(root / "locked-inputs", staged / "locked-inputs")
        if publishers._digest(args.artifact) != original_digest:
            raise QualificationError("artifact changed during qualification")
        for source, (snapshot, digest) in captured.items():
            if (hashlib.sha256(publishers.read_bounded(source)).hexdigest() != digest
                    or hashlib.sha256(publishers.read_bounded(snapshot)).hexdigest() != digest):
                raise QualificationError("qualification input changed during verification")
        args.out.mkdir(mode=0o700)
        try:
            for source in staged.iterdir():
                source.rename(args.out / source.name)
        except BaseException:
            shutil.rmtree(args.out)
            raise
    return {"status": "native-qualified-unsigned-evidence", "promotion_authorized": False,
            "artifact_sha256": original_digest, "platform": args.platform,
            "out": str(args.out), "signed": False, "published": False,
            "limitation": "existing offline evidence signing and release publication/promotion checks remain required"}


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest="command", required=True)
    diagnostic = commands.add_parser("inspect", help="report native tools, policy and retained fixture requirements without executing tools")
    qualification = commands.add_parser("qualify", help="verify native bytes and emit existing unsigned evidence and signing requests")
    for command in (diagnostic, qualification):
        command.add_argument("--platform", choices=("windows", "macos"), required=True)
        command.add_argument("--platform-publisher-policy", type=Path, required=command is qualification)
        command.add_argument("--platform-publisher-policy-signature", type=Path, required=command is qualification)
        command.add_argument("--test-results", type=Path, required=command is qualification)
    qualification.add_argument("artifact", type=Path)
    qualification.add_argument("--version", required=True)
    qualification.add_argument("--arch", choices=("x86_64", "arm64"), required=True)
    qualification.add_argument("--commit", required=True)
    qualification.add_argument("--locked-input", type=Path, action="append", required=True)
    qualification.add_argument("--out", type=Path, required=True)
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        result = inspect(args) if args.command == "inspect" else qualify(args)
    except (OSError, ValueError, RecursionError, subprocess.CalledProcessError) as exc:
        # Do not print captured native output, environment values or credential data.
        detail = f"native verifier exited {exc.returncode}" if isinstance(exc, subprocess.CalledProcessError) else str(exc)
        print(f"qualify_native_release: error: {detail}", file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
