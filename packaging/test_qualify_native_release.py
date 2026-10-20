from __future__ import annotations

import argparse
import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

_SPEC = importlib.util.spec_from_file_location("qualify_native_release", Path(__file__).with_name("qualify_native_release.py"))
assert _SPEC and _SPEC.loader
Q = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(Q)
P = Q.publishers
CERTIFICATE = b"disposable process-fixture certificate bytes; not native trust"


def _retained(root: Path) -> Path:
    root.mkdir()
    commands = []
    for suite in ("core", "host"):
        junit = root / f"{suite}-junit.xml"
        junit.write_bytes(f'<testsuite tests="1" failures="0" errors="0" skipped="0" disabled="0"><testcase name="{suite}-fixture" status="run"/></testsuite>'.encode())
        log = root / f"{suite}-ctest.log"
        log.write_bytes(b"disposable retained process fixture; not release-host test proof\n")
        commands.append({
            "command": f"ctest {suite}",
            "argv": ["ctest", "--test-dir", f"build/{suite}", "--timeout", "900",
                     "--no-tests=error", "--output-on-failure", "--output-junit", str(junit)],
            "exit_code": 0, "tests": 1, "failures": 0, "errors": 0, "skipped": 0, "passed": True,
            "junit": junit.name, "log": log.name,
            "junit_sha256": hashlib.sha256(junit.read_bytes()).hexdigest(),
            "log_sha256": hashlib.sha256(log.read_bytes()).hexdigest(),
        })
    path = root / "test-results.json"
    path.write_bytes(Q.canonical_json_bytes({"schema": "chudware-ctest-results-v1", "passed": True, "commands": commands}))
    return path


@pytest.fixture
def inputs(tmp_path, monkeypatch):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    owner = Ed25519PrivateKey.generate()
    monkeypatch.setattr(P, "_OWNER_PUBLIC_KEY", owner.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw))
    artifact = tmp_path / "installer.exe"
    artifact.write_bytes(b"disposable unsigned process fixture")
    policy = tmp_path / "platform-publishers.v1.json"
    signature = tmp_path / "platform-publishers.v1.sig"
    raw = Q.canonical_json_bytes({
        "schema": "chudware-platform-publishers-v1", "key_id": "release-v1",
        "windows": {"certificate_sha256": [hashlib.sha256(CERTIFICATE).hexdigest()]},
        "macos": {"team_ids": ["TESTTEAM01"], "bundle_id": P.BUNDLE_ID},
    })
    policy.write_bytes(raw)
    signature.write_bytes(base64.b64encode(owner.sign(P.POLICY_DOMAIN + raw)) + b"\n")
    lock = tmp_path / "reviewed.lock"
    lock.write_bytes(b"disposable process fixture input\n")
    return argparse.Namespace(
        artifact=artifact, platform="windows", version="1.2.3", arch="x86_64", commit="a" * 40,
        platform_publisher_policy=policy, platform_publisher_policy_signature=signature,
        locked_input=[lock], test_results=_retained(tmp_path / "tests"), out=tmp_path / "qualified",
    )


@pytest.fixture
def process_verifiers(tmp_path, monkeypatch, inputs):
    """Real Linux child processes exercise the actual native verifier control flow.

    Only host/tool locations are substituted. These bytes and the disposable owner
    are NOT native OS trust or owner-authorized customer release qualification.
    """
    if os.name != "posix":
        pytest.skip("POSIX process fixtures; genuine native acceptance is separate")
    config = tmp_path / "fixture-config.json"
    calls = tmp_path / "native-calls.jsonl"
    state = {"mode": "accept", "artifact": str(inputs.artifact),
             "policy": str(inputs.platform_publisher_policy), "lock": str(inputs.locked_input[0]),
             "junit": str(inputs.test_results.parent / "core-junit.xml"),
             "certificate": base64.b64encode(CERTIFICATE).decode(), "calls": str(calls)}
    config.write_text(json.dumps(state))
    code = '''import base64, json, os, pathlib, re, sys
config = json.loads(pathlib.Path(os.environ["CHUDWARE_QUALIFY_PROCESS_FIXTURE"]).read_text())
name = pathlib.Path(sys.argv[0]).name
with pathlib.Path(config["calls"]).open("a") as stream:
    stream.write(json.dumps({"name": name, "argv": sys.argv[1:]}) + "\\n")
mode = config["mode"]
if name == "signtool.exe":
    artifact = pathlib.Path(sys.argv[-1])
    if artifact == pathlib.Path(config["artifact"]) or artifact.stat().st_mode & 0o222:
        sys.exit(91)
    if artifact.read_bytes() != pathlib.Path(config["artifact"]).read_bytes():
        sys.exit(92)
    if mode == "signtool-failure":
        sys.exit(23)
    if mode == "artifact-mutation":
        pathlib.Path(config["artifact"]).write_bytes(b"mutated artifact")
    if mode == "snapshot-mutation":
        artifact.chmod(0o600)
        artifact.write_bytes(b"mutated snapshot")
    for field in ("policy", "lock", "junit"):
        if mode == field + "-mutation":
            path = pathlib.Path(config[field])
            path.write_bytes(path.read_bytes() + b"\\n")
    if mode == "excess-output":
        sys.stdout.buffer.write(b"x" * (2 * 1024**2))
else:
    if mode == "inspector-failure":
        sys.exit(37)
    if mode == "invalid-certificate":
        sys.stdout.buffer.write(b"not base64")
    else:
        certificate = b"unauthorized publisher" if mode == "wrong-publisher" else base64.b64decode(config["certificate"])
        sys.stdout.buffer.write(base64.b64encode(certificate))
'''
    tools = {}
    for name in ("signtool.exe", "powershell.exe"):
        path = tmp_path / name
        path.write_text(f"#!{sys.executable}\n" + code)
        path.chmod(0o700)
        tools[name] = path
    monkeypatch.setenv("CHUDWARE_QUALIFY_PROCESS_FIXTURE", str(config))
    monkeypatch.setenv("SIGNTOOL", str(tools["signtool.exe"]))
    monkeypatch.setattr(P, "_windows_powershell_executable", lambda: str(tools["powershell.exe"]))
    monkeypatch.setattr(P.sys, "platform", "win32")
    monkeypatch.setattr(Q, "native_host_error", lambda target: None)

    def set_mode(mode):
        state["mode"] = mode
        config.write_text(json.dumps(state))

    return set_mode, calls


def _argv(args):
    return ["qualify", str(args.artifact), "--platform", args.platform,
            "--version", args.version, "--arch", args.arch, "--commit", args.commit,
            "--test-results", str(args.test_results), "--locked-input", str(args.locked_input[0]),
            "--platform-publisher-policy", str(args.platform_publisher_policy),
            "--platform-publisher-policy-signature", str(args.platform_publisher_policy_signature),
            "--out", str(args.out)]


def test_inspection_cli_never_claims_native_or_promotion_authority():
    process = subprocess.run([sys.executable, str(Path(Q.__file__)), "inspect", "--platform", "windows"],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True, text=True)
    report = json.loads(process.stdout)
    assert report["promotion_authorized"] is False
    assert report["tools"]["executed"] is False
    assert report["publisher_policy"] == "not supplied"
    assert report["retained_tests"] == "not supplied"
    assert set(report["tools"]["optional_diagnostic_only"]) >= {"wine", "osslsigncode", "rcodesign", "powershell.exe"}


@pytest.mark.skipif(sys.platform in ("win32", "darwin"), reason="non-native refusal requires Linux")
@pytest.mark.parametrize("target", ["windows", "macos"])
def test_non_native_cli_cannot_emit_positive_evidence(inputs, target):
    inputs.platform = target
    result = subprocess.run([sys.executable, str(Path(Q.__file__)), *_argv(inputs)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    assert result.returncode == 2
    assert not inputs.out.exists()
    assert "genuine" in result.stderr


@pytest.mark.parametrize("suffix", [".exe", ".msi"])
def test_process_qualified_evidence_preserves_release_contract(inputs, process_verifiers, suffix):
    # This is fixture control-flow success, deliberately NOT genuine native proof.
    inputs.artifact = inputs.artifact.rename(inputs.artifact.with_suffix(suffix))
    config = Path(os.environ["CHUDWARE_QUALIFY_PROCESS_FIXTURE"])
    state = json.loads(config.read_text())
    state["artifact"] = str(inputs.artifact)
    config.write_text(json.dumps(state))
    result = Q.qualify(inputs)
    assert result["promotion_authorized"] is False
    assert result["signed"] is False and result["published"] is False
    report = json.loads((inputs.out / "platform-signing.v1.json").read_bytes())
    assert report["publisher"] == {"certificate_sha256": hashlib.sha256(CERTIFICATE).hexdigest()}
    assert report["artifact_sha256"] == hashlib.sha256(inputs.artifact.read_bytes()).hexdigest()
    tests = json.loads((inputs.out / "platform-test.v1.json").read_bytes())
    assert set(tests) == {"schema", "version", "platform", "arch", "commit", "artifact_sha256", "commands", "passed"}
    assert tests["commands"] == ["ctest core", "ctest host"]
    assert tests["schema"] == "platform-test.v1" and tests["passed"] is True
    Q.evidence.validated_test_results(inputs.out / "retained-test-results/test-results.json")
    provenance = json.loads((inputs.out / "provenance.slsa.json").read_bytes())
    descriptor = provenance["predicate"]["runDetails"]["byproducts"][0]
    assert descriptor["annotations"]["report"] == report
    assert descriptor["annotations"]["publisher_policy"] == json.loads(inputs.platform_publisher_policy.read_bytes())
    assert descriptor["digest"]["sha256"] == hashlib.sha256((inputs.out / "platform-signing.v1.json").read_bytes()).hexdigest()
    assert provenance["subject"] == [{"name": inputs.artifact.name, "digest": {"sha256": report["artifact_sha256"]}}]
    for name, kind in Q.EVIDENCE_KINDS.items():
        assert (inputs.out / (name + ".signing-request.bin")).read_bytes() == Q.DOMAINS[kind] + (inputs.out / name).read_bytes()
    calls = [json.loads(line) for line in process_verifiers[1].read_text().splitlines()]
    assert [call["name"] for call in calls] == ["signtool.exe", "powershell.exe"] * 2


@pytest.mark.parametrize("mode,exception", [
    ("signtool-failure", subprocess.CalledProcessError),
    ("inspector-failure", subprocess.CalledProcessError),
    ("wrong-publisher", P.PublisherPolicyError),
    ("invalid-certificate", P.PublisherPolicyError),
    ("artifact-mutation", P.PublisherPolicyError),
    ("snapshot-mutation", P.PublisherPolicyError),
    ("policy-mutation", Q.QualificationError),
    ("lock-mutation", Q.QualificationError),
    ("junit-mutation", Q.QualificationError),
    ("excess-output", P.PublisherPolicyError),
])
def test_real_process_failure_identity_and_mutation_are_terminal(inputs, process_verifiers, mode, exception):
    process_verifiers[0](mode)
    with pytest.raises(exception):
        Q.qualify(inputs)
    assert not inputs.out.exists()
    calls = [json.loads(line) for line in process_verifiers[1].read_text().splitlines()]
    if mode == "signtool-failure":
        assert [call["name"] for call in calls] == ["signtool.exe"]


@pytest.mark.parametrize("corruption", ["missing-policy", "invalid-owner-signature", "forged-ctest-counts", "replaced-junit"])
def test_bad_authority_and_retained_test_inputs_never_reach_processes(inputs, process_verifiers, corruption):
    if corruption == "missing-policy":
        inputs.platform_publisher_policy.unlink()
    elif corruption == "invalid-owner-signature":
        inputs.platform_publisher_policy_signature.write_bytes(base64.b64encode(b"z" * 64) + b"\n")
    elif corruption == "forged-ctest-counts":
        document = json.loads(inputs.test_results.read_bytes())
        document["commands"][0]["tests"] += 1
        inputs.test_results.write_bytes(Q.canonical_json_bytes(document))
    else:
        (inputs.test_results.parent / "core-junit.xml").write_bytes(b"<testsuite/>")
    with pytest.raises((OSError, ValueError)):
        Q.qualify(inputs)
    assert not process_verifiers[1].exists()
    assert not inputs.out.exists()


def test_fake_positive_json_does_not_replace_real_subprocess_verification(inputs, process_verifiers):
    process_verifiers[0]("signtool-failure")
    report_path = inputs.artifact.parent / "fake-positive.json"
    report_path.write_bytes(Q.canonical_json_bytes({
        "schema": "platform-signing.v1", "platform": "windows", "kind": "authenticode", "verified": True,
        "artifact_sha256": hashlib.sha256(inputs.artifact.read_bytes()).hexdigest(),
        "publisher_policy_sha256": hashlib.sha256(inputs.platform_publisher_policy.read_bytes()).hexdigest(),
        "publisher": {"certificate_sha256": hashlib.sha256(CERTIFICATE).hexdigest()},
    }))
    args = argparse.Namespace(**vars(inputs), artifact_sha256=hashlib.sha256(inputs.artifact.read_bytes()).hexdigest(),
                              platform_signing_evidence=report_path)
    with pytest.raises(subprocess.CalledProcessError) as failure:
        Q.evidence.generate(args)
    assert failure.value.returncode == 23
    assert not inputs.out.exists()


def test_existing_output_is_never_replaced(inputs, process_verifiers):
    inputs.out.mkdir()
    sentinel = inputs.out / "platform-signing.v1.json"
    sentinel.write_bytes(b"existing operator bytes")
    with pytest.raises(Q.QualificationError, match="replace"):
        Q.qualify(inputs)
    assert sentinel.read_bytes() == b"existing operator bytes"
    assert not process_verifiers[1].exists()


def test_cli_preserves_native_nonzero_and_leaves_no_report(inputs, process_verifiers, capsys):
    process_verifiers[0]("inspector-failure")
    assert Q.main(_argv(inputs)) == 2
    assert "exited 37" in capsys.readouterr().err
    assert not inputs.out.exists()


def test_bounded_runner_rejects_timeout(monkeypatch):
    monkeypatch.setattr(P, "NATIVE_TIMEOUT", 0.1)
    with pytest.raises(P.PublisherPolicyError, match="timed out"):
        P.run_native([sys.executable, "-c", "import time; time.sleep(30)"], check=True)


def test_bounded_reader_rejects_oversize_and_symlinks(tmp_path):
    source = tmp_path / "input"
    source.write_bytes(b"12345")
    with pytest.raises(P.PublisherPolicyError, match="byte limit"):
        P.read_bounded(source, 4)
    if os.name == "posix":
        link = tmp_path / "link"
        link.symlink_to(source)
        with pytest.raises(P.PublisherPolicyError, match="symlink"):
            P.read_bounded(link)
        fifo = tmp_path / "fifo"
        os.mkfifo(fifo)
        with pytest.raises(P.PublisherPolicyError, match="regular"):
            P.read_bounded(fifo)


@pytest.mark.parametrize("suffix", [".exe", ".msi"])
def test_genuine_windows_sdk_rejects_unsigned_bytes_and_cleans_private_snapshots(inputs, monkeypatch, suffix):
    """Real SDK/OS refusal only; disposable policy/test inputs are not release proof."""
    if Q.native_host_error("windows"):
        pytest.skip("requires genuine Windows, never Wine or cross-platform verifier success")
    tools = Q.tool_inventory("windows")["required_native"]
    configured = tools["Windows SDK signtool.exe"]
    assert configured, "Windows SDK signtool.exe or SIGNTOOL is required for genuine native rejection coverage"
    signtool = Path(configured).resolve(strict=True)
    assert signtool.is_file() and signtool.name.lower() == "signtool.exe", "SIGNTOOL must name the real Windows SDK executable"
    powershell = tools["Windows PowerShell 5.1 (system Authenticode provider)"]
    assert powershell and Path(powershell).is_file(), "system Windows PowerShell 5.1 is required"
    # A relocated official SDK is valid; a fixture named signtool.exe is not.
    # Authenticate it with the genuine OS provider before exercising its refusal.
    literal = "'" + str(signtool).replace("'", "''") + "'"
    script = (
        "$ErrorActionPreference = 'Stop'; "
        "if ($PSVersionTable.PSVersion.Major -ne 5 -or $PSVersionTable.PSVersion.Minor -lt 1) "
        "{ throw 'Windows PowerShell 5.1 is required' }; "
        "$signature = Get-AuthenticodeSignature -LiteralPath " + literal + "; "
        "if ($signature.Status -ne 'Valid' -or $signature.SignatureType -ne 'Authenticode' "
        "-or -not $signature.SignerCertificate "
        "-or $signature.SignerCertificate.GetNameInfo("
        "[Security.Cryptography.X509Certificates.X509NameType]::SimpleName, $false) "
        "-ne 'Microsoft Corporation') { throw 'real Microsoft SDK signtool.exe is required' }"
    )
    P.run_native([powershell, "-NoProfile", "-NonInteractive", "-Command", script], check=True)
    monkeypatch.setenv("SIGNTOOL", str(signtool))
    # Check Windows cleanup of the read-only verification snapshot on rejection.
    monkeypatch.setattr(P.tempfile, "tempdir", str(inputs.artifact.parent))
    inputs.artifact = inputs.artifact.rename(inputs.artifact.with_suffix(suffix))
    with pytest.raises(subprocess.CalledProcessError) as rejected:
        Q.qualify(inputs)
    assert rejected.value.returncode != 0
    assert Path(rejected.value.cmd[0]) == signtool
    assert not inputs.out.exists()
    assert not list(inputs.artifact.parent.glob("chudware-publisher-verify-*"))
    assert not list(inputs.artifact.parent.glob("chudware-native-qualification-*"))


@pytest.mark.parametrize("artifact_kind", ["windows-exe", "windows-msi", "macos-zip"])
def test_genuine_native_acceptance_requires_authorized_operator_fixtures(tmp_path, artifact_kind):
    """Opt-in real OS trust, not process fixtures or a positive report JSON.

    CHUDWARE_NATIVE_QUALIFY_FIXTURES points to explicit public fixture inputs:
    an object keyed windows-exe/windows-msi/macos-zip, each containing the same
    argument fields accepted by `qualify` (artifact, version, arch, commit,
    locked_input list, test_results, platform_publisher_policy and signature).
    No owner-root, native-tool or verification overrides are available here.
    """
    target = "macos" if artifact_kind == "macos-zip" else "windows"
    if Q.native_host_error(target):
        pytest.skip("requires genuine native OS, never Wine/cross-platform tool success")
    fixture_path = os.environ.get("CHUDWARE_NATIVE_QUALIFY_FIXTURES")
    if not fixture_path:
        pytest.skip("requires independently authorized signed/notarized artifacts, owner policy and retained CTest inputs")
    fixture_source = Path(fixture_path).resolve(strict=True)
    fixtures = json.loads(P.read_bounded(fixture_source))
    record = fixtures[artifact_kind]
    assert set(record) == {"artifact", "version", "arch", "commit", "locked_input", "test_results",
                           "platform_publisher_policy", "platform_publisher_policy_signature"}
    assert isinstance(record["locked_input"], list) and record["locked_input"]
    values = dict(record)
    def fixture_input(value):
        path = Path(value)
        return path if path.is_absolute() else fixture_source.parent / path
    for field in ("artifact", "test_results", "platform_publisher_policy", "platform_publisher_policy_signature"):
        values[field] = fixture_input(record[field])
    values["locked_input"] = [fixture_input(value) for value in record["locked_input"]]
    args = argparse.Namespace(**values, platform=target, out=tmp_path / artifact_kind)
    result = Q.qualify(args)
    report = json.loads((args.out / "platform-signing.v1.json").read_bytes())
    assert report["verified"] is True
    assert report["artifact_sha256"] == result["artifact_sha256"] == P._digest(args.artifact)
    assert report["publisher_policy_sha256"] == hashlib.sha256(args.platform_publisher_policy.read_bytes()).hexdigest()
    assert result["promotion_authorized"] is False


@pytest.fixture
def mac_process_verifiers(tmp_path, monkeypatch, inputs):
    import plistlib
    import zipfile
    if os.name != "posix":
        pytest.skip("POSIX process fixtures; genuine macOS acceptance is separate")
    inputs.platform = "macos"
    inputs.artifact = inputs.artifact.with_suffix(".zip")
    with zipfile.ZipFile(inputs.artifact, "w") as archive:
        archive.writestr("Chudware.app/Contents/Info.plist",
                         plistlib.dumps({"CFBundleIdentifier": P.BUNDLE_ID}))
    config = tmp_path / "mac-fixture-config.json"
    calls = tmp_path / "mac-native-calls.jsonl"
    state = {"mode": "accept", "artifact": str(inputs.artifact), "calls": str(calls)}
    config.write_text(json.dumps(state))
    code = '''import json, pathlib, sys, zipfile
config = json.loads(pathlib.Path(__file__).with_name("mac-fixture-config.json").read_text())
name = pathlib.Path(sys.argv[0]).name
mode = config["mode"]
with pathlib.Path(config["calls"]).open("a") as stream:
    stream.write(json.dumps({"name": name, "argv": sys.argv[1:]}) + "\\n")
if mode == name + "-failure":
    sys.exit(41)
if name == "ditto":
    artifact = pathlib.Path(sys.argv[-2])
    if artifact == pathlib.Path(config["artifact"]) or artifact.stat().st_mode & 0o222:
        sys.exit(91)
    with zipfile.ZipFile(artifact) as archive:
        archive.extractall(sys.argv[-1])
    if mode == "artifact-mutation":
        with pathlib.Path(config["artifact"]).open("ab") as stream:
            stream.write(b"changed archive")
if name == "codesign" and "--display" in sys.argv:
    team = "OTHERTEAM1" if mode == "wrong-team" else "TESTTEAM01"
    bundle = "com.example.Other" if mode == "wrong-bundle" else "com.chudware.Chudware"
    sys.stderr.write("TeamIdentifier=" + team + "\\nIdentifier=" + bundle + "\\n")
    if mode == "ambiguous-team":
        sys.stderr.write("TeamIdentifier=TESTTEAM01\\n")
'''
    tools = {}
    for name in ("ditto", "codesign", "xcrun", "spctl"):
        path = tmp_path / name
        path.write_text(f"#!{sys.executable}\n" + code)
        path.chmod(0o700)
        tools[name] = str(path)
    monkeypatch.setattr(P, "_mac_tool", lambda name: tools[name])
    monkeypatch.setattr(P.sys, "platform", "darwin")
    monkeypatch.setattr(Q, "native_host_error", lambda target: None)

    def set_mode(mode):
        state["mode"] = mode
        config.write_text(json.dumps(state))

    return set_mode, calls


@pytest.mark.parametrize("mode", ["ditto-failure", "codesign-failure", "xcrun-failure", "spctl-failure",
                                 "wrong-team", "wrong-bundle", "ambiguous-team", "artifact-mutation"])
def test_archived_mac_process_failures_never_produce_evidence(inputs, mac_process_verifiers, mode):
    mac_process_verifiers[0](mode)
    expected = subprocess.CalledProcessError if mode.endswith("-failure") else P.PublisherPolicyError
    with pytest.raises(expected):
        Q.qualify(inputs)
    assert not inputs.out.exists()
    calls = [json.loads(line) for line in mac_process_verifiers[1].read_text().splitlines()]
    if mode.endswith("-failure"):
        assert calls[-1]["name"] == mode.removesuffix("-failure")


def test_archived_mac_process_flow_checks_extracted_bundle_and_preserves_team(inputs, mac_process_verifiers):
    # Deliberately fixture-only control flow; native acceptance above has no overrides.
    Q.qualify(inputs)
    report = json.loads((inputs.out / "platform-signing.v1.json").read_bytes())
    assert report["publisher"] == {"team_id": "TESTTEAM01", "bundle_id": P.BUNDLE_ID}
    assert report["kind"] == "notarization"
    calls = [json.loads(line) for line in mac_process_verifiers[1].read_text().splitlines()]
    assert [call["name"] for call in calls] == ["ditto", "codesign", "xcrun", "spctl", "codesign"] * 2


def test_wine_cannot_claim_genuine_windows_host(monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(Q, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(Q, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(Q, "platform", SimpleNamespace(system=lambda: "Windows"))
    monkeypatch.setattr(Q, "ctypes", SimpleNamespace(WinDLL=lambda name: SimpleNamespace(wine_get_version=lambda: b"wine")))
    assert "Wine" in Q.native_host_error("windows")
