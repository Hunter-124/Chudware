from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location("release_evidence", Path(__file__).with_name("generate_release_evidence.py"))
assert _SPEC and _SPEC.loader
evidence = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(evidence)


def _retained_results(root: Path) -> Path:
    root.mkdir()
    records = []
    for suite in ("core", "host"):
        junit = root / f"{suite}-junit.xml"
        junit.write_bytes(
            f'<testsuite name="{suite}" tests="1" failures="0" errors="0" skipped="0" disabled="0">'
            f'<testcase name="{suite}-fixture" status="run"/></testsuite>'.encode("ascii")
        )
        log = root / f"{suite}-ctest.log"
        log.write_bytes(b"disposable unit fixture log\n")
        records.append({
            "command": f"ctest {suite}",
            "argv": ["ctest", "--test-dir", f"build/{suite}", "--timeout", "900",
                     "--no-tests=error", "--output-on-failure", "--output-junit", str(junit)],
            "exit_code": 0, "tests": 1, "failures": 0, "errors": 0, "skipped": 0, "passed": True,
            "junit": junit.name, "log": log.name,
            "junit_sha256": hashlib.sha256(junit.read_bytes()).hexdigest(),
            "log_sha256": hashlib.sha256(log.read_bytes()).hexdigest(),
        })
    path = root / "test-results.json"
    path.write_bytes(evidence._canonical({
        "schema": "chudware-ctest-results-v1", "passed": True, "commands": records,
    }))
    return path


def _args(tmp_path):
    artifact = tmp_path / "artifact"
    artifact.write_bytes(b"release")
    lock = tmp_path / "requirements.lock"
    lock.write_text("dependency==1.0\n")
    return argparse.Namespace(
        artifact=artifact, artifact_sha256=hashlib.sha256(b"release").hexdigest(),
        version="1.2.3", platform="linux", arch="x86_64", commit="a" * 40,
        locked_input=[lock], test_results=_retained_results(tmp_path / "test-results"),
        out=tmp_path / "evidence",
    )


def test_generates_canonical_cyclonedx_slsa_and_exact_platform_schema(tmp_path):
    args = _args(tmp_path)
    outputs = evidence.generate(args)
    assert [path.name for path in outputs] == ["sbom.cdx.json", "provenance.slsa.json", "platform-test.v1.json"]
    sbom, provenance, platform = [json.loads(path.read_bytes()) for path in outputs]
    assert sbom["bomFormat"] == "CycloneDX" and sbom["specVersion"] == "1.5"
    assert sbom["components"][0]["hashes"][0]["content"] == evidence._digest(args.locked_input[0])
    assert provenance["predicateType"] == "https://slsa.dev/provenance/v1"
    assert provenance["subject"] == [{"name": "artifact", "digest": {"sha256": args.artifact_sha256}}]
    assert platform == {"schema": "platform-test.v1", "version": "1.2.3", "platform": "linux", "arch": "x86_64", "commit": "a" * 40, "artifact_sha256": args.artifact_sha256, "commands": ["ctest core", "ctest host"], "passed": True}
    for path in outputs:
        value = json.loads(path.read_bytes())
        assert path.read_bytes() == evidence._canonical(value)


def test_chudcode_notices_are_first_class_sbom_and_provenance_inputs(tmp_path):
    args = _args(tmp_path)
    lock = tmp_path / "CHUDCODE_ARTIFACT.lock"
    notice_hashes = {
        "CHUDCODE-NOTICE": "1" * 64,
        "CHUDCODE-THIRD-PARTY-NOTICES.txt": "2" * 64,
    }
    lock.write_text(
        json.dumps(
            {
                "schema": "chudcode-artifact-lock-v1",
                "repository_commit": "3" * 40,
                "notices": {
                    "repository_commit": "3" * 40,
                    "files": {
                        name: {
                            "source": evidence._CHUDCODE_NOTICE_FILES[name],
                            "sha256": digest,
                        }
                        for name, digest in notice_hashes.items()
                    },
                },
            }
        )
    )
    args.locked_input = [lock]
    sbom_path, provenance_path, _ = evidence.generate(args)
    sbom = json.loads(sbom_path.read_bytes())
    provenance = json.loads(provenance_path.read_bytes())
    by_name = {component["name"]: component for component in sbom["components"]}
    assert set(notice_hashes) < set(by_name)
    for name, digest in notice_hashes.items():
        assert by_name[name]["hashes"] == [{"alg": "SHA-256", "content": digest}]
        assert {
            item["name"]: item["value"] for item in by_name[name]["properties"]
        } == {
            "chudcode:repository_commit": "3" * 40,
            "chudcode:source": evidence._CHUDCODE_NOTICE_FILES[name],
        }
    dependencies = {
        item["uri"]: item["digest"]["sha256"]
        for item in provenance["predicate"]["buildDefinition"]["resolvedDependencies"]
    }
    assert {name: dependencies[name] for name in notice_hashes} == notice_hashes


def test_chudcode_notice_provenance_is_required_for_evidence(tmp_path):
    args = _args(tmp_path)
    lock = tmp_path / "CHUDCODE_ARTIFACT.lock"
    lock.write_text(
        json.dumps(
            {
                "schema": "chudcode-artifact-lock-v1",
                "repository_commit": "3" * 40,
            }
        )
    )
    args.locked_input = [lock]
    with pytest.raises(ValueError, match="notice provenance"):
        evidence.generate(args)


def test_missing_retained_test_results_rejected(tmp_path):
    args = _args(tmp_path)
    args.test_results.unlink()
    with pytest.raises(OSError):
        evidence.generate(args)
    assert not args.out.exists()


def test_label_only_success_flags_are_not_evidence(tmp_path):
    args = _args(tmp_path)
    args.passed = True
    args.command = ["ctest core", "ctest host"]
    document = json.loads(args.test_results.read_bytes())
    document["passed"] = False
    args.test_results.write_bytes(evidence._canonical(document))
    with pytest.raises(ValueError, match="passed native suites"):
        evidence.generate(args)
    assert not args.out.exists()


def test_missing_locked_input_rejected(tmp_path):
    args = _args(tmp_path)
    args.locked_input = [tmp_path / "missing.lock"]
    with pytest.raises(ValueError, match="does not exist"):
        evidence.generate(args)


@pytest.mark.parametrize(("field", "value"), [
    ("exit_code", 9), ("passed", False), ("tests", 0), ("tests", True),
    ("failures", 1), ("errors", 1), ("skipped", 1), ("tests", None),
    ("junit", "../core-junit.xml"), ("log", "/tmp/core-ctest.log"),
    ("junit_sha256", "0" * 64), ("log_sha256", "0" * 64),
    ("command", "pytest focused"),
])
def test_invalid_retained_suite_never_emits_positive_evidence(tmp_path, field, value):
    args = _args(tmp_path)
    document = json.loads(args.test_results.read_bytes())
    document["commands"][0][field] = value
    args.test_results.write_bytes(evidence._canonical(document))
    with pytest.raises(ValueError):
        evidence.generate(args)
    assert not args.out.exists()


@pytest.mark.parametrize("replacement", [
    [], ["ctest", "--show-only"],
    ["ctest", "--test-dir", "core", "--timeout", "900", "--no-tests=error",
     "--output-on-failure", "--output-junit", "host-junit.xml"],
    ["echo", "--test-dir", "core", "--timeout", "900", "--no-tests=error",
     "--output-on-failure", "--output-junit", "core-junit.xml"],
    ["ctest", "--test-dir", "core", "--timeout", "0", "--no-tests=error",
     "--output-on-failure", "--output-junit", "core-junit.xml"],
])
def test_nonexecuting_or_wrong_suite_argv_rejected(tmp_path, replacement):
    args = _args(tmp_path)
    document = json.loads(args.test_results.read_bytes())
    document["commands"][0]["argv"] = replacement
    args.test_results.write_bytes(evidence._canonical(document))
    with pytest.raises(ValueError, match="native"):
        evidence.generate(args)
    assert not args.out.exists()


@pytest.mark.parametrize("mode", ["missing-host", "duplicate-core", "extra-suite", "counts"])
def test_required_suites_and_xml_counts_are_recomputed(tmp_path, mode):
    args = _args(tmp_path)
    document = json.loads(args.test_results.read_bytes())
    if mode == "missing-host":
        document["commands"].pop()
    elif mode == "duplicate-core":
        document["commands"][1] = dict(document["commands"][0])
    elif mode == "extra-suite":
        document["commands"].append(dict(document["commands"][0]))
    else:
        document["commands"][0]["tests"] = 8
    args.test_results.write_bytes(evidence._canonical(document))
    with pytest.raises(ValueError):
        evidence.generate(args)
    assert not args.out.exists()


@pytest.mark.parametrize("raw", [
    b"not XML",
    b'<testsuite tests="1" failures="0" errors="0" skipped="0" disabled="0"><testcase name="failure"><failure/></testcase></testsuite>',
    b'<testsuite tests="0" failures="0" errors="0" skipped="0" disabled="0"/>',
])
def test_malformed_failed_or_empty_xml_is_not_positive_evidence(tmp_path, raw):
    args = _args(tmp_path)
    document = json.loads(args.test_results.read_bytes())
    junit = args.test_results.parent / "core-junit.xml"
    junit.write_bytes(raw)
    document["commands"][0]["junit_sha256"] = hashlib.sha256(raw).hexdigest()
    args.test_results.write_bytes(evidence._canonical(document))
    with pytest.raises(ValueError):
        evidence.generate(args)
    assert not args.out.exists()


def test_noncanonical_results_rejected(tmp_path):
    args = _args(tmp_path)
    document = json.loads(args.test_results.read_bytes())
    args.test_results.write_text(json.dumps(document, indent=2))
    with pytest.raises(ValueError, match="canonical"):
        evidence.generate(args)
    assert not args.out.exists()


def test_replaced_junit_and_log_bytes_rejected(tmp_path):
    args = _args(tmp_path)
    for filename in ("core-junit.xml", "core-ctest.log"):
        source = args.test_results.parent / filename
        original = source.read_bytes()
        source.write_bytes(b"replaced")
        with pytest.raises(ValueError, match="bytes were replaced"):
            evidence.generate(args)
        assert not args.out.exists()
        source.write_bytes(original)


def test_retained_files_cannot_escape_through_symlinks(tmp_path):
    args = _args(tmp_path)
    source = args.test_results.parent / "core-junit.xml"
    outside = tmp_path / "outside.xml"
    outside.write_bytes(source.read_bytes())
    source.unlink()
    try:
        source.symlink_to(outside)
    except OSError:
        pytest.skip("fixture symlinks unavailable on this host")
    with pytest.raises(ValueError, match="unsafe junit path"):
        evidence.generate(args)
    assert not args.out.exists()


def test_retained_inputs_are_bound_to_signed_artifact_and_commit(tmp_path):
    args = _args(tmp_path)
    sbom_path, provenance_path, _ = evidence.generate(args)
    sbom = json.loads(sbom_path.read_bytes())
    provenance = json.loads(provenance_path.read_bytes())
    assert provenance["subject"][0]["digest"]["sha256"] == evidence._digest(args.artifact)
    assert provenance["predicate"]["buildDefinition"]["externalParameters"]["commit"] == args.commit
    dependencies = {item["uri"]: item["digest"]["sha256"]
                    for item in provenance["predicate"]["buildDefinition"]["resolvedDependencies"]}
    components = {item["name"]: item["hashes"][0]["content"] for item in sbom["components"]}
    retained = args.out / "retained-test-results"
    names = {"test-results.json", "core-junit.xml", "host-junit.xml",
             "core-ctest.log", "host-ctest.log"}
    assert {path.name for path in retained.iterdir()} == names
    for name in names:
        source = args.test_results.parent / name
        copied = retained / name
        assert copied.read_bytes() == source.read_bytes()
        assert dependencies[f"retained-test-results/{name}"] == evidence._digest(source)
        assert components[f"retained-test-results/{name}"] == evidence._digest(source)


def test_changed_artifact_cannot_get_positive_test_evidence(tmp_path):
    args = _args(tmp_path)
    args.artifact.write_bytes(b"not the requested artifact")
    with pytest.raises(ValueError, match="artifact bytes"):
        evidence.generate(args)
    assert not args.out.exists()


@pytest.mark.parametrize("platform", ["windows", "macos"])
def test_unsigned_beta_native_byproduct_is_explicitly_not_verified(tmp_path, platform):
    args = _args(tmp_path)
    args.platform = platform
    _, provenance_path, platform_path = evidence.generate(args)
    provenance = json.loads(provenance_path.read_bytes())
    descriptor = provenance["predicate"]["runDetails"]["byproducts"][0]
    report = descriptor["annotations"]["report"]
    assert descriptor["name"] == "platform-signing.v1.json"
    assert descriptor["digest"]["sha256"] == evidence._digest(args.out / descriptor["name"])
    assert set(descriptor["annotations"]) == {"report"}
    assert report["verified"] is False
    assert report["artifact_sha256"] == args.artifact_sha256
    assert report["platform"] == platform
    assert report["kind"] == ("authenticode" if platform == "windows" else "notarization")
    assert report["reason"] == "owner_authorized_native_verification_not_supplied"
    assert json.loads(platform_path.read_bytes())["passed"] is True
    assert provenance["predicate"]["buildDefinition"]["externalParameters"] == {
        "version": args.version, "platform": platform, "arch": args.arch, "commit": args.commit,
    }


@pytest.mark.parametrize("field", [
    "platform_signing_evidence", "platform_publisher_policy", "platform_publisher_policy_signature",
])
def test_partial_native_attestation_inputs_cannot_emit_evidence(tmp_path, field):
    args = _args(tmp_path)
    args.platform = "windows"
    setattr(args, field, tmp_path / "missing-native-input")
    with pytest.raises(ValueError, match="supplied together"):
        evidence.generate(args)
    assert not args.out.exists()


def _native_fixture(args, monkeypatch):
    import base64
    import subprocess
    import sys
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    policy_module = sys.modules[evidence.verify_artifact.__module__]
    owner = Ed25519PrivateKey.generate()
    monkeypatch.setattr(policy_module, "_OWNER_PUBLIC_KEY",
        owner.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw))
    monkeypatch.setattr(policy_module, "_windows_powershell_executable", lambda: "powershell.exe")
    monkeypatch.setattr(policy_module, "_mac_tool", lambda name: name)
    certificate = b"disposable native signer DER fixture"
    document = {
        "schema": "chudware-platform-publishers-v1", "key_id": "release-v1",
        "windows": {"certificate_sha256": [hashlib.sha256(certificate).hexdigest()]},
        "macos": {"team_ids": ["TESTTEAM01"], "bundle_id": policy_module.BUNDLE_ID},
    }
    args.platform = "windows"
    args.platform_publisher_policy = args.artifact.parent / "platform-publishers.v1.json"
    args.platform_publisher_policy_signature = args.artifact.parent / "platform-publishers.v1.sig"
    raw = evidence._canonical(document)
    args.platform_publisher_policy.write_bytes(raw)
    args.platform_publisher_policy_signature.write_bytes(
        base64.b64encode(owner.sign(policy_module.POLICY_DOMAIN + raw)) + b"\n")
    args.platform_signing_evidence = args.artifact.parent / "native-report.v1.json"
    report = {
        "schema": "platform-signing.v1", "platform": "windows", "kind": "authenticode",
        "artifact_sha256": args.artifact_sha256, "verified": True,
        "publisher_policy_sha256": hashlib.sha256(raw).hexdigest(),
        "publisher": {"certificate_sha256": hashlib.sha256(certificate).hexdigest()},
    }
    args.platform_signing_evidence.write_bytes(evidence._canonical(report))
    monkeypatch.setattr(policy_module.sys, "platform", "win32")
    monkeypatch.setenv("SIGNTOOL", "disposable-signtool")
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0,
            stdout=base64.b64encode(certificate) if command[0] == "powershell.exe" else b"",
            stderr=b"")
    monkeypatch.setattr(policy_module, "run_native", run)
    return document, report, calls


def test_native_attestation_embeds_authenticated_policy_and_actual_report(tmp_path, monkeypatch):
    args = _args(tmp_path)
    policy, report, calls = _native_fixture(args, monkeypatch)
    _, provenance_path, _ = evidence.generate(args)
    descriptor = json.loads(provenance_path.read_bytes())["predicate"]["runDetails"]["byproducts"][0]
    assert [call[0] for call in calls] == ["disposable-signtool", "powershell.exe"]
    annotations = descriptor["annotations"]
    assert annotations["report"] == report
    assert annotations["publisher_policy"] == policy
    assert annotations["publisher_policy_signature"] == args.platform_publisher_policy_signature.read_text().strip()
    assert descriptor["digest"] == {"sha256": evidence._digest(args.platform_signing_evidence)}
    assert (args.out / descriptor["name"]).read_bytes() == args.platform_signing_evidence.read_bytes()


@pytest.mark.parametrize("field", ["publisher", "publisher_policy_sha256", "artifact_sha256", "verified"])
def test_forged_native_report_is_not_transferred_to_signed_provenance(tmp_path, monkeypatch, field):
    args = _args(tmp_path)
    _policy, report, _calls = _native_fixture(args, monkeypatch)
    report[field] = ({"certificate_sha256": "5" * 64} if field == "publisher"
                     else False if field == "verified" else "5" * 64)
    args.platform_signing_evidence.write_bytes(evidence._canonical(report))
    with pytest.raises(ValueError):
        evidence.generate(args)
    assert not args.out.exists()


def test_failed_actual_native_verifier_cannot_emit_attestation(tmp_path, monkeypatch):
    import subprocess
    import sys
    args = _args(tmp_path)
    _native_fixture(args, monkeypatch)
    policy_module = sys.modules[evidence.verify_artifact.__module__]
    def failed(command, **kwargs):
        raise subprocess.CalledProcessError(33, command)
    monkeypatch.setattr(policy_module, "run_native", failed)
    with pytest.raises(subprocess.CalledProcessError):
        evidence.generate(args)
    assert not args.out.exists()


def test_unauthorized_policy_cannot_be_embedded_as_native_success(tmp_path, monkeypatch):
    args = _args(tmp_path)
    _native_fixture(args, monkeypatch)
    args.platform_publisher_policy_signature.write_bytes(b"invalid signature")
    with pytest.raises(ValueError, match="signature"):
        evidence.generate(args)
    assert not args.out.exists()


def test_macos_native_attestation_preserves_team_and_sealed_bundle_binding(tmp_path, monkeypatch):
    import plistlib
    import subprocess
    import sys
    import zipfile
    args = _args(tmp_path)
    policy, _old_report, calls = _native_fixture(args, monkeypatch)
    policy_module = sys.modules[evidence.verify_artifact.__module__]
    args.platform = "macos"
    monkeypatch.setattr(policy_module.sys, "platform", "darwin")
    with zipfile.ZipFile(args.artifact, "w") as archive:
        archive.writestr("Chudware.app/Contents/placeholder", b"fixture")
    args.artifact_sha256 = evidence._digest(args.artifact)
    report = {
        "schema": "platform-signing.v1", "platform": "macos", "kind": "notarization",
        "artifact_sha256": args.artifact_sha256, "verified": True,
        "publisher_policy_sha256": evidence._digest(args.platform_publisher_policy),
        "publisher": {"team_id": "TESTTEAM01", "bundle_id": policy_module.BUNDLE_ID},
    }
    args.platform_signing_evidence.write_bytes(evidence._canonical(report))
    def run(command, **kwargs):
        calls.append(command)
        if command[0] == "ditto":
            contents = Path(command[-1]) / "Chudware.app/Contents"
            contents.mkdir(parents=True)
            (contents / "Info.plist").write_bytes(plistlib.dumps({"CFBundleIdentifier": policy_module.BUNDLE_ID}))
        return subprocess.CompletedProcess(command, 0, stdout="",
            stderr=f"TeamIdentifier=TESTTEAM01\nIdentifier={policy_module.BUNDLE_ID}\n")
    monkeypatch.setattr(policy_module, "run_native", run)
    _, provenance_path, _ = evidence.generate(args)
    descriptor = json.loads(provenance_path.read_bytes())["predicate"]["runDetails"]["byproducts"][0]
    assert [command[0] for command in calls] == ["ditto", "codesign", "xcrun", "spctl", "codesign"]
    assert descriptor["annotations"]["report"] == report
    assert descriptor["annotations"]["publisher_policy"] == policy
    assert descriptor["digest"] == {"sha256": evidence._digest(args.platform_signing_evidence)}
