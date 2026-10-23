#!/usr/bin/env python3
"""Generate canonical CycloneDX, SLSA provenance, and platform-test evidence."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import re
import sys
import subprocess
from pathlib import Path
from typing import Any

try:
    from packaging.release_json import canonical_json_bytes, parse_canonical_json_bytes
except ImportError:  # direct execution from packaging/
    from release_json import canonical_json_bytes, parse_canonical_json_bytes

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
from scripts.run_release_ctest import parse_junit_counts
try:
    from packaging.platform_publishers import load_policy, read_bounded, verify_artifact
except ImportError:
    from platform_publishers import load_policy, read_bounded, verify_artifact

_SHA256 = re.compile(r"[0-9a-f]{64}")
_CHUDCODE_NOTICE_FILES = {
    "CHUDCODE-NOTICE": "NOTICE",
    "CHUDCODE-THIRD-PARTY-NOTICES.txt": "THIRD-PARTY-NOTICES.txt",
}


def _chudcode_notice_components(path: Path) -> list[dict[str, Any]]:
    if path.name != "CHUDCODE_ARTIFACT.lock":
        return []
    try:
        lock = json.loads(path.read_bytes())
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Chudcode artifact lock is unreadable: {path}") from exc
    if not isinstance(lock, dict) or lock.get("schema") != "chudcode-artifact-lock-v1":
        raise ValueError(f"Chudcode artifact lock has an invalid schema: {path}")
    commit = lock.get("repository_commit")
    notices = lock.get("notices")
    if (
        not isinstance(commit, str)
        or re.fullmatch(r"[0-9a-f]{40}", commit) is None
        or not isinstance(notices, dict)
        or set(notices) != {"files", "repository_commit"}
        or notices.get("repository_commit") != commit
        or not isinstance(notices.get("files"), dict)
        or set(notices["files"]) != set(_CHUDCODE_NOTICE_FILES)
    ):
        raise ValueError(f"Chudcode artifact lock has invalid notice provenance: {path}")
    components: list[dict[str, Any]] = []
    for payload_name, source_name in _CHUDCODE_NOTICE_FILES.items():
        notice = notices["files"][payload_name]
        if (
            not isinstance(notice, dict)
            or set(notice) != {"sha256", "source"}
            or notice.get("source") != source_name
            or not isinstance(notice.get("sha256"), str)
            or _SHA256.fullmatch(notice["sha256"]) is None
        ):
            raise ValueError(
                f"Chudcode artifact lock has invalid notice record: {payload_name}"
            )
        components.append(
            {
                "type": "file",
                "name": payload_name,
                "hashes": [{"alg": "SHA-256", "content": notice["sha256"]}],
                "properties": [
                    {"name": "chudcode:repository_commit", "value": commit},
                    {"name": "chudcode:source", "value": source_name},
                ],
            }
        )
    return components


def _canonical(value: dict[str, Any]) -> bytes:
    return canonical_json_bytes(value)


def _digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def locked_components(paths: list[Path], names: dict[Path, str] | None = None) -> list[dict[str, Any]]:
    components = []
    names = names or {}
    for path in sorted(paths, key=lambda item: names.get(item, item.as_posix())):
        if not path.is_file():
            raise ValueError(f"locked input does not exist: {path}")
        components.append({
            "type": "file",
            "name": names.get(path, path.as_posix()),
            "hashes": [{"alg": "SHA-256", "content": _digest(path)}],
        })
        components.extend(_chudcode_notice_components(path))
    if not components:
        raise ValueError("at least one locked input is required")
    return components

def validated_test_results(path: Path) -> tuple[list[str], dict[str, bytes]]:
    """Consume successful native CTest records and their exact retained bytes."""
    if path.is_symlink():
        raise ValueError("retained test results must not be a symlink")
    raw = read_bounded(path)
    document = parse_canonical_json_bytes(raw, "retained test results")
    if (set(document) != {"schema", "passed", "commands"}
            or document.get("schema") != "chudware-ctest-results-v1"
            or document.get("passed") is not True):
        raise ValueError("retained test results must record passed native suites")
    entries = document.get("commands")
    if not isinstance(entries, list) or len(entries) != 2:
        raise ValueError("retained test results require exactly core and host suites")
    by_command = {}
    retained = {"test-results.json": raw}
    fields = {"command", "argv", "exit_code", "tests", "failures", "errors", "skipped",
              "passed", "junit", "log", "junit_sha256", "log_sha256"}
    root = path.parent.resolve()
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != fields:
            raise ValueError("retained native test record has invalid fields")
        command = entry["command"]
        if command not in ("ctest core", "ctest host") or command in by_command:
            raise ValueError("retained test results require distinct core and host suites")
        suite = command.split()[1]
        for field in ("exit_code", "tests", "failures", "errors", "skipped"):
            if type(entry[field]) is not int or entry[field] < 0:
                raise ValueError(f"retained {command} has invalid {field}")
        if (entry["passed"] is not True or entry["exit_code"] != 0
                or entry["failures"] != 0 or entry["errors"] != 0
                or entry["tests"] <= entry["skipped"]):
            raise ValueError(f"retained {command} did not pass executed tests")
        argv = entry["argv"]
        if (not isinstance(argv, list) or len(argv) != 9
                or not all(isinstance(value, str) and value.strip() for value in argv)
                or argv[0].replace("\\", "/").rsplit("/", 1)[-1].lower() not in ("ctest", "ctest.exe")
                or argv[1] != "--test-dir" or argv[3] != "--timeout"
                or argv[5:8] != ["--no-tests=error", "--output-on-failure", "--output-junit"]
                or argv[8].replace("\\", "/").rsplit("/", 1)[-1] != f"{suite}-junit.xml"):
            raise ValueError(f"retained {command} does not record a native test invocation")
        try:
            timeout = float(argv[4])
        except ValueError as exc:
            raise ValueError(f"retained {command} has invalid native timeout") from exc
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError(f"retained {command} has invalid native timeout")
        for field, name in (("junit", f"{suite}-junit.xml"), ("log", f"{suite}-ctest.log")):
            if entry[field] != name:
                raise ValueError(f"retained {command} has an unsafe {field} basename")
            source = path.parent / name
            if source.is_symlink() or source.resolve().parent != root:
                raise ValueError(f"retained {command} has an unsafe {field} path")
            body = read_bounded(source)
            expected_digest = entry[field + "_sha256"]
            if (not isinstance(expected_digest, str) or _SHA256.fullmatch(expected_digest) is None
                    or hashlib.sha256(body).hexdigest() != expected_digest):
                raise ValueError(f"retained {command} {field} bytes were replaced")
            retained[name] = body
        counts = parse_junit_counts(retained[f"{suite}-junit.xml"])
        if any(entry[field] != counts[field] for field in ("tests", "failures", "errors", "skipped")):
            raise ValueError(f"retained {command} counts differ from JUnit")
        by_command[command] = entry
    return ["ctest core", "ctest host"], retained


def native_platform_byproduct(args: argparse.Namespace) -> tuple[dict | None, bytes | None]:
    """Carry native facts and owner authorization inside signed SLSA provenance."""
    inputs = (getattr(args, "platform_signing_evidence", None),
              getattr(args, "platform_publisher_policy", None),
              getattr(args, "platform_publisher_policy_signature", None))
    if any(inputs) and not all(inputs):
        raise ValueError("native platform report, publisher policy and signature must be supplied together")
    if args.platform not in ("windows", "macos"):
        if any(inputs):
            raise ValueError("native publisher attestation is only applicable to Windows/macOS")
        return None, None
    kind = {"windows": "authenticode", "macos": "notarization"}[args.platform]
    if all(inputs):
        report_path, policy_path, signature_path = inputs
        raw = read_bounded(report_path)
        report = parse_canonical_json_bytes(raw, "native platform verification report")
        if (report.get("verified") is not True or report.get("schema") != "platform-signing.v1"
                or report.get("platform") != args.platform or report.get("kind") != kind
                or report.get("artifact_sha256") != args.artifact_sha256):
            raise ValueError("native platform report does not match the evidence artifact")
        verified = verify_artifact(args.artifact, args.platform, policy_path, signature_path)
        expected = {"schema": "platform-signing.v1", "platform": args.platform, "kind": kind,
                    "verified": True, **verified}
        if report != expected:
            raise ValueError("native platform report differs from actual owner-authorized verification")
        policy = load_policy(policy_path, signature_path)
        if policy.sha256 != verified["publisher_policy_sha256"]:
            raise ValueError("publisher policy changed after native attestation verification")
        annotations = {
            "report": report,
            "publisher_policy": parse_canonical_json_bytes(policy.raw, "platform publisher policy"),
            "publisher_policy_signature": policy.signature.strip().decode("ascii"),
        }
    else:
        report = {"schema": "platform-signing.v1", "platform": args.platform, "kind": kind,
                  "artifact_sha256": args.artifact_sha256, "verified": False,
                  "reason": "owner_authorized_native_verification_not_supplied"}
        raw = _canonical(report)
        annotations = {"report": report}
    return {"name": "platform-signing.v1.json",
            "digest": {"sha256": hashlib.sha256(raw).hexdigest()},
            "annotations": annotations}, raw



def generate(args: argparse.Namespace) -> list[Path]:
    if not _SHA256.fullmatch(args.artifact_sha256):
        raise ValueError("artifact_sha256 must be lowercase SHA-256")
    if not args.artifact.is_file() or _digest(args.artifact) != args.artifact_sha256:
        raise ValueError("artifact bytes do not match the evidence identity")
    commands, retained = validated_test_results(args.test_results)
    components = locked_components(args.locked_input, getattr(args, "locked_input_names", None))
    native_byproduct, native_report = native_platform_byproduct(args)
    components.extend({
        "type": "file", "name": f"retained-test-results/{name}",
        "hashes": [{"alg": "SHA-256", "content": hashlib.sha256(raw).hexdigest()}],
    } for name, raw in sorted(retained.items()))
    sbom = {
        "bomFormat": "CycloneDX", "specVersion": "1.5", "serialNumber": f"urn:uuid:{args.commit[:8]}-{args.artifact_sha256[:4]}-4000-8000-{args.artifact_sha256[4:16]}",
        "version": 1, "metadata": {"component": {"type": "application", "name": "Chudware", "version": args.version}},
        "components": components,
    }
    provenance = {
        "_type": "https://in-toto.io/Statement/v1",
        "subject": [{"name": args.artifact.name, "digest": {"sha256": args.artifact_sha256}}],
        "predicateType": "https://slsa.dev/provenance/v1",
        "predicate": {
            "buildDefinition": {"buildType": "https://github.com/Hunter-124/Chudware/packaging/platform-build/v1", "externalParameters": {"version": args.version, "platform": args.platform, "arch": args.arch, "commit": args.commit}, "resolvedDependencies": [{"uri": component["name"], "digest": {"sha256": component["hashes"][0]["content"]}} for component in components]},
            "runDetails": {"builder": {"id": "https://github.com/Hunter-124/Chudware/.github/workflows/release.yml"}, "metadata": {"invocationId": f"{args.commit}:{args.platform}:{args.arch}"}},
        },
    }
    if native_byproduct is not None:
        provenance["predicate"]["runDetails"]["byproducts"] = [native_byproduct]
    platform_test = {"schema": "platform-test.v1", "version": args.version, "platform": args.platform, "arch": args.arch, "commit": args.commit, "artifact_sha256": args.artifact_sha256, "commands": commands, "passed": True}
    if _digest(args.artifact) != args.artifact_sha256:
        raise ValueError("artifact changed during evidence generation")
    args.out.mkdir(parents=True, exist_ok=True)
    retained_out = args.out / "retained-test-results"
    retained_out.mkdir(exist_ok=True)
    for name, raw in retained.items():
        (retained_out / name).write_bytes(raw)
    if native_report is not None:
        (args.out / "platform-signing.v1.json").write_bytes(native_report)
    outputs = []
    for name, value in (("sbom.cdx.json", sbom), ("provenance.slsa.json", provenance), ("platform-test.v1.json", platform_test)):
        output = args.out / name
        output.write_bytes(_canonical(value))
        outputs.append(output)
    return outputs


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("artifact", type=Path)
    result.add_argument("--artifact-sha256", required=True)
    result.add_argument("--version", required=True)
    result.add_argument("--platform", choices=("linux", "windows", "macos"), required=True)
    result.add_argument("--arch", choices=("x86_64", "arm64"), required=True)
    result.add_argument("--commit", required=True)
    result.add_argument("--locked-input", type=Path, action="append", required=True)
    result.add_argument("--test-results", type=Path, required=True,
                        help="canonical retained native CTest results with adjacent JUnit/log files")
    result.add_argument("--platform-signing-evidence", type=Path)
    result.add_argument("--platform-publisher-policy", type=Path)
    result.add_argument("--platform-publisher-policy-signature", type=Path)
    result.add_argument("--out", type=Path, required=True)
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        outputs = generate(args)
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"generate_release_evidence: error: {exc}", file=sys.stderr)
        return 2
    for output in outputs:
        print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
