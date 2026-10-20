from __future__ import annotations
import base64
import hashlib
import plistlib
import importlib.util
import io
import tarfile
import json
import os
import shutil
import subprocess
import sys
import zipfile
from argparse import Namespace
from pathlib import Path
import pytest

from services.installer.payload_manifest import build_manifest

_path=Path(__file__).with_name("finalize_release.py")
_spec=importlib.util.spec_from_file_location("finalize_release", _path); assert _spec and _spec.loader
M=importlib.util.module_from_spec(_spec); _spec.loader.exec_module(M)
P = sys.modules[M.verify_artifact.__module__]
_SYSTEM_POWERSHELL = P._windows_powershell_executable
_CERTIFICATE_BYTES = b"disposable native signer DER fixture"
_TEAM_ID = "TESTTEAM01"


@pytest.fixture(autouse=True)
def _disposable_policy_owner(monkeypatch):
    serialization = pytest.importorskip("cryptography.hazmat.primitives.serialization")
    ed25519 = pytest.importorskip("cryptography.hazmat.primitives.asymmetric.ed25519")
    owner = ed25519.Ed25519PrivateKey.generate()
    public = owner.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    monkeypatch.setattr(P, "_OWNER_PUBLIC_KEY", public)
    monkeypatch.setitem(globals(), "_TEST_OWNER", owner)
    monkeypatch.setattr(P, "_windows_powershell_executable", lambda: "powershell.exe")
    monkeypatch.setattr(P, "_mac_tool", lambda name: name)


def _publisher_policy(root: Path) -> tuple[Path, Path]:
    policy = root / "platform-publishers.v1.json"
    signature = root / "platform-publishers.v1.sig"
    raw = M.canonical_json_bytes({
        "schema": "chudware-platform-publishers-v1", "key_id": "release-v1",
        "windows": {"certificate_sha256": [hashlib.sha256(_CERTIFICATE_BYTES).hexdigest()]},
        "macos": {"team_ids": [_TEAM_ID], "bundle_id": P.BUNDLE_ID},
    })
    policy.write_bytes(raw)
    signature.write_bytes(base64.b64encode(_TEST_OWNER.sign(P.POLICY_DOMAIN + raw)) + b"\n")
    return policy, signature


def _portable_policy(root: Path) -> tuple[Path, Path]:
    portable = root / "portable-inputs"
    portable.mkdir(exist_ok=True)
    paths = []
    for name in ("platform-publishers.v1.json", "platform-publishers.v1.sig"):
        destination = portable / name
        shutil.copyfile(root / "handoff" / name, destination)
        paths.append(destination)
    return tuple(paths)


def _windows_native_success(command, **kwargs):
    return subprocess.CompletedProcess(
        command, 0, stdout=base64.b64encode(_CERTIFICATE_BYTES) if command[0] == "powershell.exe" else b"",
        stderr=b"",
    )


def test_safe_extract_rejects_traversal(tmp_path: Path):
    archive=tmp_path/"bad.tar"
    with tarfile.open(archive,"w") as bundle:
        info=tarfile.TarInfo("../secret"); info.size=1; bundle.addfile(info,io.BytesIO(b"x"))
    with pytest.raises(M.FinalizeError, match="unsafe path"):
        M.safe_extract(archive,tmp_path/"out")


def test_evidence_requests_are_domain_separated(tmp_path: Path):
    artifact=tmp_path/"a.AppImage"; artifact.write_bytes(b"artifact")
    locked=tmp_path/"lock"; locked.write_bytes(b"lock")
    args=Namespace(out=tmp_path,version="1.2.3",platform="linux",arch="x86_64",commit="a"*40,locked_input=[locked],test_results=_retained_results(tmp_path))
    requests=M.evidence_requests(args,artifact,[locked])
    assert [p.read_bytes().split(b"\0",1)[0] for p in requests] == [b"chudware-sbom-v1",b"chudware-provenance-v1",b"chudware-platform-test-v1"]

def _retained_results(root: Path) -> Path:
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
            "junit_sha256": M.digest(junit), "log_sha256": M.digest(log),
        })
    path = root / "test-results.json"
    path.write_bytes(M.canonical_json_bytes({
        "schema": "chudware-ctest-results-v1", "passed": True, "commands": records,
    }))
    return path


def _prepared(tmp_path: Path, *, channel="stable"):
    root=tmp_path/"handoff"; root.mkdir()
    lock=root/"requirements.lock"; lock.write_bytes(b"lock")
    payload_root=root/"Chudware"; payload_root.mkdir()
    (payload_root/"Chudware.exe").write_bytes(b"release payload")
    _payload,canonical,_snapshots=build_manifest(
        payload_root,version="1.2.3",platform="windows",arch="x86_64",
        key_id="release-v1",created_at="2026-10-09T00:00:00Z",
    )
    (root/"payload-manifest.v1.json").write_bytes(canonical)
    metadata={"commit":"a"*40,"version":"1.2.3","channel":channel,"platform":"windows","arch":"x86_64","locked_inputs":[{"path":"requirements.lock","sha256":M.digest(lock)}]}
    if channel == "stable":
        for path in _publisher_policy(root):
            metadata["locked_inputs"].append({"path": path.name, "sha256": M.digest(path)})
    _retained_results(root)
    for name in ("test-results.json", "core-junit.xml", "host-junit.xml", "core-ctest.log", "host-ctest.log"):
        metadata["locked_inputs"].append({"path": name, "sha256": M.digest(root / name)})
    (root/"handoff.json").write_bytes(M.canonical_json_bytes(metadata))
    args=Namespace(handoff_metadata=None,commit="a"*40,version="1.2.3",channel=channel,platform="windows",arch="x86_64",locked_input=[])
    return root,args

@pytest.mark.parametrize(("field","replacement"),[("commit","b"*40),("version","9.9.9"),("channel","beta")])
def test_prepared_identity_replacements_rejected(tmp_path: Path, field: str, replacement: str):
    root,args=_prepared(tmp_path)
    setattr(args,field,replacement)
    with pytest.raises(M.FinalizeError,match="identity mismatch"):
        M.load_handoff(args,root)

def test_replaced_lock_and_key_id_derivation(tmp_path: Path):
    root,args=_prepared(tmp_path)
    _metadata,_manifest,key_id=M.load_handoff(args,root)
    assert key_id == "release-v1"
    (root/"requirements.lock").write_bytes(b"tampered")
    with pytest.raises(M.FinalizeError,match="locked input was replaced"):
        M.load_handoff(args,root)

def test_unsigned_stable_windows_rejected_but_beta_truthful(tmp_path: Path):
    artifact=tmp_path/"installer.exe"; artifact.write_bytes(b"exe")
    stable=Namespace(channel="stable",platform="windows",platform_signing_evidence=None,artifact=artifact)
    with pytest.raises(M.FinalizeError,match="Authenticode|authenticode"):
        M._stable_platform_evidence(stable)
    stable.channel="beta"
    M._stable_platform_evidence(stable)


@pytest.mark.parametrize("name", [
    "Chudware-Setup-1.2.3-x86_64.exe",
    "Chudware-1.2.3-x86_64.msi",
    "CHUDWARE-SETUP-1.2.3-X86_64.EXE",
    "CHUDWARE-1.2.3-X86_64.MSI",
    "Chudware-1.2.3-macos-x86_64.zip",
    "Chudware-1.2.3-linux-x86_64.AppImage",
])
def test_prepared_handoff_rejects_retained_final_artifact(tmp_path: Path, name: str):
    handoff, args = _prepared(tmp_path)
    (handoff / name).write_bytes(b"retained arbitrary bytes")
    args.out = tmp_path
    # Reject before even copying portable signing material.
    with pytest.raises(M.FinalizeError, match="final artifact"):
        M.host_script(args, handoff)
    assert not (tmp_path / "portable-inputs").exists()


def _platform_args(tmp_path: Path, platform="windows"):
    artifact = tmp_path / ("installer.exe" if platform == "windows" else "installer.zip")
    artifact.write_bytes(b"arbitrary unsigned bytes")
    policy, signature = _publisher_policy(tmp_path)
    return Namespace(artifact=artifact, platform=platform,
                     kind="authenticode" if platform == "windows" else "notarization",
                     out=tmp_path / "platform-signing.v1.json",
                     platform_publisher_policy=policy, platform_publisher_policy_signature=signature)


@pytest.mark.parametrize("platform", ["windows", "macos"])
def test_platform_evidence_requires_native_host(tmp_path: Path, monkeypatch, platform):
    args = _platform_args(tmp_path, platform)
    monkeypatch.setattr(M.sys, "platform", "linux")
    with pytest.raises(M.PublisherPolicyError, match="requires a .* host"):
        M.write_platform_evidence(args)
    assert not args.out.exists()


def test_failed_authenticode_verification_never_writes_evidence(tmp_path: Path, monkeypatch):
    args = _platform_args(tmp_path)
    monkeypatch.setattr(M.sys, "platform", "win32")
    monkeypatch.setenv("SIGNTOOL", "disposable-signtool")
    calls = []
    def failed(command, *, check):
        calls.append(command)
        assert check is True
        raise subprocess.CalledProcessError(23, command)
    monkeypatch.setattr(P, "run_native", failed)
    with pytest.raises(subprocess.CalledProcessError):
        M.write_platform_evidence(args)
    assert len(calls) == 1 and calls[0][:-1] == ["disposable-signtool", "verify", "/pa", "/v"]
    assert Path(calls[0][-1]) != args.artifact.resolve()
    assert Path(calls[0][-1]).name == args.artifact.name
    assert not args.out.exists()


def test_successful_native_verification_binds_evidence_to_bytes(tmp_path: Path, monkeypatch):
    args = _platform_args(tmp_path)
    monkeypatch.setattr(M.sys, "platform", "win32")
    monkeypatch.setenv("SIGNTOOL", "disposable-signtool")
    monkeypatch.setattr(P, "run_native", _windows_native_success)
    assert M.write_platform_evidence(args) == 0
    evidence = json.loads(args.out.read_bytes())
    assert evidence["verified"] is True
    assert evidence["artifact_sha256"] == M.digest(args.artifact)
    assert evidence["publisher_policy_sha256"] == M.digest(args.platform_publisher_policy)
    assert evidence["publisher"] == {"certificate_sha256": hashlib.sha256(_CERTIFICATE_BYTES).hexdigest()}


def test_artifact_changed_during_native_verification_rejected(tmp_path: Path, monkeypatch):
    args = _platform_args(tmp_path)
    monkeypatch.setattr(M.sys, "platform", "win32")
    monkeypatch.setenv("SIGNTOOL", "disposable-signtool")
    def changed(command, **kwargs):
        args.artifact.write_bytes(b"changed during verification")
        return _windows_native_success(command, **kwargs)
    monkeypatch.setattr(P, "run_native", changed)
    with pytest.raises(M.PublisherPolicyError, match="changed during"):
        M.write_platform_evidence(args)
    assert not args.out.exists()


@pytest.mark.parametrize("msi", [False, True])
def test_forged_positive_evidence_cannot_create_signing_requests(tmp_path: Path, monkeypatch, msi):
    handoff, args = _prepared(tmp_path)
    artifact = handoff / ("Chudware-1.2.3-x86_64.msi" if msi else "Chudware-Setup-1.2.3-x86_64.exe")
    artifact.write_bytes(b"not an installer")
    evidence = tmp_path / "platform-signing.v1.json"
    evidence.write_bytes(M.canonical_json_bytes({
        "schema": "platform-signing.v1", "platform": "windows", "kind": "authenticode",
        "artifact_sha256": M.digest(artifact), "verified": True,
    }))
    args.artifact = artifact
    args.bundle_root = tmp_path
    args.platform_signing_evidence = evidence
    _portable_policy(tmp_path)
    monkeypatch.setattr(M.sys, "platform", "win32")
    monkeypatch.setenv("SIGNTOOL", "disposable-signtool")
    def failed(command, **kwargs):
        raise subprocess.CalledProcessError(9, command)
    monkeypatch.setattr(P, "run_native", failed)
    with pytest.raises(subprocess.CalledProcessError):
        (M.complete_windows_msi if msi else M.complete)(args)
    assert not list(tmp_path.rglob("*.signing-request.bin"))
    assert not list(tmp_path.glob("publish-bundle*.json"))
    assert not list(tmp_path.rglob("platform-test.v1.json"))


@pytest.mark.parametrize("failed_step", ["codesign", "xcrun", "spctl", None])
def test_macos_evidence_verifies_archived_app(tmp_path: Path, monkeypatch, failed_step):
    args = _platform_args(tmp_path, "macos")
    with zipfile.ZipFile(args.artifact, "w") as archive:
        archive.writestr("Chudware.app/Contents/placeholder", b"fixture")
    monkeypatch.setattr(M.sys, "platform", "darwin")
    calls = []
    def run(command, *, check, **kwargs):
        assert check is True
        calls.append(command[0])
        if command[0] == "ditto":
            contents = Path(command[-1]) / "Chudware.app/Contents"
            contents.mkdir(parents=True)
            (contents / "Info.plist").write_bytes(plistlib.dumps({"CFBundleIdentifier": P.BUNDLE_ID}))
        if command[0] == failed_step:
            raise subprocess.CalledProcessError(4, command)
        return subprocess.CompletedProcess(command, 0, stdout="",
            stderr=f"TeamIdentifier={_TEAM_ID}\nIdentifier={P.BUNDLE_ID}\n")
    monkeypatch.setattr(P, "run_native", run)
    if failed_step:
        with pytest.raises(subprocess.CalledProcessError):
            M.write_platform_evidence(args)
        assert not args.out.exists()
        assert calls[-1] == failed_step
    else:
        assert M.write_platform_evidence(args) == 0
        assert calls == ["ditto", "codesign", "xcrun", "spctl", "codesign"]
        assert json.loads(args.out.read_bytes())["artifact_sha256"] == M.digest(args.artifact)


try:
    _WINDOWS_POWERSHELL = _SYSTEM_POWERSHELL() if os.name == "nt" else None
except P.PublisherPolicyError:
    _WINDOWS_POWERSHELL = None  # Wine is not a native qualification host.
if _WINDOWS_POWERSHELL and not Path(_WINDOWS_POWERSHELL).is_file():
    _WINDOWS_POWERSHELL = None


@pytest.mark.skipif(not _WINDOWS_POWERSHELL, reason="requires Windows PowerShell 5.1; not proven by Linux or pwsh")
@pytest.mark.parametrize("channel", ["beta", "stable"])
@pytest.mark.parametrize("failed_step", [
    "payload-verifier", "exe-builder", "msi-builder", "signtool-verifier",
    "exe-evidence", "msi-evidence", "exe-complete", "msi-complete", None,
])
def test_generated_windows_script_stops_native_failures(tmp_path: Path, monkeypatch, channel, failed_step):
    """Execute the emitted script against disposable native-command fixtures.

    This tests PowerShell sequencing, not the authenticity of fixture installers.
    The real verifier's reject/accept contract is covered separately.
    """
    if channel == "beta" and failed_step in ("signtool-verifier", "exe-evidence", "msi-evidence"):
        pytest.skip("beta does not claim Authenticode verification")
    tmp_path = tmp_path / "finalize [literal] caf\u00e9 & O'Brien 100%"
    tmp_path.mkdir()
    handoff, args = _prepared(tmp_path, channel=channel)
    args.out = tmp_path
    def portable(_args, _handoff):
        (tmp_path / "builder").mkdir()
        return tmp_path / "signature", tmp_path / "public-key"
    monkeypatch.setattr(M, "_copy_portable_inputs", portable)
    script = M.host_script(args, handoff)
    fixture = tmp_path / "native-fixture.py"
    fixture.write_text(
        "import json, os, pathlib, sys\n"
        "root = pathlib.Path(os.environ['FIXTURE_ROOT'])\n"
        "args = sys.argv[1:]\n"
        "def step(name):\n"
        "    with (root / 'calls.jsonl').open('a') as f: f.write(json.dumps(name) + '\\n')\n"
        "    if os.environ.get('FAIL_STEP') == name: sys.exit(37)\n"
        "if 'services.installer.build_release' in args:\n"
        "    step('payload-verifier'); step('exe-builder')\n"
        "    (root / 'handoff/Chudware-Setup-1.2.3-x86_64.exe').write_bytes(b'fixture')\n"
        "elif 'services.installer.build_msi' in args:\n"
        "    step('msi-builder')\n"
        "    (root / 'handoff/Chudware-1.2.3-x86_64.msi').write_bytes(b'fixture')\n"
        "elif 'platform-evidence' in args:\n"
        "    step('signtool-verifier')\n"
        "    step('msi-evidence' if args[2].endswith('.msi') else 'exe-evidence')\n"
        "    pathlib.Path(args[args.index('--out') + 1]).write_text('{}')\n"
        "else:\n"
        "    is_msi = 'windows-msi-complete' in args\n"
        "    step('msi-complete' if is_msi else 'exe-complete')\n"
        "    (root / ('publish-bundle-msi.v1.json' if is_msi else 'publish-bundle.v1.json')).write_text('{}')\n",
        encoding="ascii",
    )
    tools = tmp_path / "tools"
    tools.mkdir()
    (tools / "python.cmd").write_text(
        '@"%CHUDWARE_FIXTURE_PYTHON%" "%CHUDWARE_FIXTURE_DRIVER%" %*\n@exit /b %errorlevel%\n',
        encoding="ascii",
    )
    environment = dict(os.environ, PATH=str(tools) + os.pathsep + os.environ["PATH"],
                       FIXTURE_ROOT=str(tmp_path), FAIL_STEP=failed_step or "",
                       CHUDWARE_SIGN_IDENTITY="disposable fixture only",
                       CHUDWARE_FIXTURE_PYTHON=sys.executable, CHUDWARE_FIXTURE_DRIVER=str(fixture))
    result = subprocess.run([_WINDOWS_POWERSHELL, "-NoProfile", "-NonInteractive",
                             "-ExecutionPolicy", "Bypass", "-File", str(script)],
                            env=environment, capture_output=True, text=True)
    assert (tmp_path / "calls.jsonl").is_file(), result.stdout + result.stderr
    calls = [json.loads(line) for line in (tmp_path / "calls.jsonl").read_text().splitlines()]
    expected = ["payload-verifier", "exe-builder", "msi-builder"]
    if channel == "stable":
        expected += ["signtool-verifier", "exe-evidence", "signtool-verifier", "msi-evidence"]
    expected += ["exe-complete", "msi-complete"]
    if failed_step:
        assert result.returncode != 0, result.stdout + result.stderr
        assert calls == expected[:expected.index(failed_step) + 1]
        assert not (tmp_path / "publish-bundle-msi.v1.json").exists()
        if failed_step != "msi-complete":
            assert not (tmp_path / "publish-bundle.v1.json").exists()
    else:
        assert result.returncode == 0, result.stdout + result.stderr
        assert calls == expected
        assert (tmp_path / "publish-bundle.v1.json").exists()
        assert (tmp_path / "publish-bundle-msi.v1.json").exists()


@pytest.mark.skipif(not _WINDOWS_POWERSHELL, reason="requires Windows PowerShell 5.1")
@pytest.mark.parametrize("name", ["Chudware-Setup-1.2.3-x86_64.exe", "Chudware-1.2.3-x86_64.msi"])
def test_generated_script_rejects_artifact_added_after_preparation(tmp_path: Path, monkeypatch, name):
    tmp_path = tmp_path / "finalize [literal] caf\u00e9 & O'Brien 100%"
    tmp_path.mkdir()
    handoff, args = _prepared(tmp_path, channel="beta")
    args.out = tmp_path
    def portable(_args, _handoff):
        (tmp_path / "builder").mkdir()
        return tmp_path / "signature", tmp_path / "public-key"
    monkeypatch.setattr(M, "_copy_portable_inputs", portable)
    script = M.host_script(args, handoff)
    retained = handoff / name
    retained.write_bytes(b"retained arbitrary bytes")
    result = subprocess.run([_WINDOWS_POWERSHELL, "-NoProfile", "-NonInteractive",
                             "-ExecutionPolicy", "Bypass", "-File", str(script)],
                            capture_output=True, text=True)
    assert result.returncode != 0
    assert "prepared handoff contains a final artifact" in result.stderr
    assert retained.read_bytes() == b"retained arbitrary bytes"
    assert not list(tmp_path.rglob("*.signing-request.bin"))
    assert not list(tmp_path.glob("publish-bundle*.json"))


@pytest.mark.parametrize("channel", ["beta", "stable"])
@pytest.mark.parametrize("msi", [False, True])
def test_completion_happy_path_preserves_publish_contract(tmp_path: Path, monkeypatch, channel, msi):
    handoff, args = _prepared(tmp_path, channel=channel)
    args.artifact = handoff / ("Chudware-1.2.3-x86_64.msi" if msi else "Chudware-Setup-1.2.3-x86_64.exe")
    args.artifact.write_bytes(b"disposable fixture")
    args.bundle_root = tmp_path
    args.platform_signing_evidence = None
    real_run = M.subprocess.run
    native_calls = []
    if channel == "stable":
        policy, signature = _portable_policy(tmp_path)
        monkeypatch.setattr(M.sys, "platform", "win32")
        monkeypatch.setenv("SIGNTOOL", "disposable-signtool")
        def run(command, **kwargs):
            if command[0] in ("disposable-signtool", "powershell.exe"):
                native_calls.append(command)
                assert kwargs["check"] is True
                return _windows_native_success(command, **kwargs)
            if len(command) > 1 and Path(command[1]).name == "generate_release_evidence.py":
                # Keep the disposable owner authority inside this unit-test
                # process; the product public root is never changed in a child.
                generator = sys.modules[M.validated_test_results.__module__]
                status = generator.main(command[2:])
                if status and kwargs.get("check"):
                    raise subprocess.CalledProcessError(status, command)
                return subprocess.CompletedProcess(command, status)
            return real_run(command, **kwargs)
        monkeypatch.setattr(M.subprocess, "run", run)
        monkeypatch.setattr(P, "run_native", run)
        platform_args = Namespace(artifact=args.artifact, platform="windows", kind="authenticode",
                                  out=tmp_path / "platform-signing.v1.json",
                                  platform_publisher_policy=policy, platform_publisher_policy_signature=signature)
        M.write_platform_evidence(platform_args)
        args.platform_signing_evidence = platform_args.out
    assert (M.complete_windows_msi if msi else M.complete)(args) == 0
    manifest = json.loads((tmp_path / ("publish-bundle-msi.v1.json" if msi else "publish-bundle.v1.json")).read_bytes())
    assert manifest["artifact_sha256"] == M.digest(args.artifact)
    assert manifest["channel"] == channel
    retained = tmp_path / manifest["test_results"]
    assert manifest["test_results_sha256"] == M.digest(retained)
    assert retained.read_bytes() == (handoff / "test-results.json").read_bytes()
    if msi:
        assert manifest["format"] == "msi"
    request = (tmp_path / manifest["artifact_signing_request"]).read_bytes().split(b"\0")
    assert request == [b"chudware-artifact-v1", b"release-v1", b"1.2.3",
                       b"windows", b"x86_64", M.digest(args.artifact).encode("ascii")]
    assert len(manifest["evidence_signing_requests"]) == 3
    for relative in manifest["evidence_signing_requests"]:
        assert (tmp_path / relative).is_file()
    assert len(native_calls) == (6 if channel == "stable" else 0)
    evidence_root = tmp_path / ("msi/evidence" if msi else "evidence")
    provenance = json.loads((evidence_root / "provenance.slsa.json").read_bytes())
    descriptor = provenance["predicate"]["runDetails"]["byproducts"][0]
    report = descriptor["annotations"]["report"]
    assert report["verified"] is (channel == "stable")
    assert report["artifact_sha256"] == M.digest(args.artifact)
    assert descriptor["digest"]["sha256"] == M.digest(evidence_root / "platform-signing.v1.json")
    if channel == "stable":
        assert descriptor["annotations"]["publisher_policy"]["key_id"] == "release-v1"
        assert descriptor["annotations"]["publisher_policy_signature"] == signature.read_text().strip()


def test_evidence_cli_handles_native_rejection_without_positive_output(tmp_path: Path, monkeypatch):
    args = _platform_args(tmp_path)
    monkeypatch.setattr(M.sys, "platform", "win32")
    monkeypatch.setenv("SIGNTOOL", "disposable-signtool")
    def failed(command, **kwargs):
        raise subprocess.CalledProcessError(71, command)
    monkeypatch.setattr(P, "run_native", failed)
    assert M.main(["platform-evidence", str(args.artifact), "--platform", "windows",
                   "--kind", "authenticode",
                   "--platform-publisher-policy", str(args.platform_publisher_policy),
                   "--platform-publisher-policy-signature", str(args.platform_publisher_policy_signature),
                   "--out", str(args.out)]) == 2
    assert not args.out.exists()


@pytest.mark.parametrize("failed_verification", [True, False])
@pytest.mark.skipif(not _WINDOWS_POWERSHELL, reason="requires Windows native process semantics")
def test_native_verification_process_controls_evidence(tmp_path: Path, monkeypatch, failed_verification):
    """SigTool zero alone must not turn unsigned bytes into authorized evidence."""
    args = _platform_args(tmp_path)
    verifier = tmp_path / "signtool-fixture.cmd"
    verifier.write_text("@exit /b " + ("29" if failed_verification else "0") + "\n", encoding="ascii")
    monkeypatch.setenv("SIGNTOOL", str(verifier))
    monkeypatch.setattr(P, "_windows_powershell_executable", _SYSTEM_POWERSHELL)
    with pytest.raises(subprocess.CalledProcessError):
        M.write_platform_evidence(args)
    assert not args.out.exists()


@pytest.mark.parametrize("name", ["Chudware-Setup-1.2.3-x86_64.exe", "Chudware-1.2.3-x86_64.msi"])
def test_prepare_cli_refuses_archive_with_retained_installer(tmp_path: Path, name):
    handoff, _args = _prepared(tmp_path, channel="beta")
    (handoff / name).write_bytes(b"retained bytes")
    archive = tmp_path / "prepared.tar"
    with tarfile.open(archive, "w") as bundle:
        for path in handoff.iterdir():
            bundle.add(path, arcname=path.name)
    signature = tmp_path / "signature"
    signature.write_bytes(b"disposable")
    output = tmp_path / "output"
    assert M.main([str(archive), "--payload-signature", str(signature),
                   "--release-public-key", "disposable", "--platform", "windows",
                   "--arch", "x86_64", "--version", "1.2.3", "--commit", "a" * 40,
                   "--channel", "beta", "--out", str(output)]) == 2
    assert not list(output.glob("finalize-*.ps1"))
    assert not list(output.glob("publish-bundle*.json"))
    assert not list(output.rglob("*.signing-request.bin"))


@pytest.mark.parametrize("msi", [False, True])
def test_failed_evidence_generator_stops_completion_requests(tmp_path: Path, monkeypatch, msi):
    handoff, args = _prepared(tmp_path, channel="beta")
    args.artifact = handoff / ("Chudware-1.2.3-x86_64.msi" if msi else "Chudware-Setup-1.2.3-x86_64.exe")
    args.artifact.write_bytes(b"disposable fixture")
    args.bundle_root = tmp_path
    args.platform_signing_evidence = None
    def failed(command, *, check, **kwargs):
        assert check is True
        raise subprocess.CalledProcessError(18, command)
    monkeypatch.setattr(M.subprocess, "run", failed)
    with pytest.raises(subprocess.CalledProcessError):
        (M.complete_windows_msi if msi else M.complete)(args)
    assert not list(tmp_path.rglob("*.signing-request.bin"))
    assert not list(tmp_path.glob("publish-bundle*.json"))


@pytest.mark.parametrize("channel", ["beta", "stable"])
@pytest.mark.parametrize("msi", [False, True])
def test_missing_retained_results_blocks_all_completion(tmp_path, monkeypatch, channel, msi):
    handoff, args = _prepared(tmp_path, channel=channel)
    (handoff / "test-results.json").unlink()
    args.artifact = handoff / ("Chudware-1.2.3-x86_64.msi" if msi else "Chudware-Setup-1.2.3-x86_64.exe")
    args.artifact.write_bytes(b"disposable fixture")
    args.bundle_root = tmp_path
    args.platform_signing_evidence = None
    def forbidden(*_args, **_kwargs):
        raise AssertionError("missing retained tests must not reach native commands")
    monkeypatch.setattr(M.subprocess, "run", forbidden)
    with pytest.raises(M.FinalizeError, match="locked input|retained test results"):
        (M.complete_windows_msi if msi else M.complete)(args)
    assert not list(tmp_path.rglob("*.signing-request.bin"))
    assert not list(tmp_path.glob("publish-bundle*.json"))


def test_forged_result_counts_cannot_create_requests(tmp_path, monkeypatch):
    handoff, args = _prepared(tmp_path, channel="beta")
    results = handoff / "test-results.json"
    document = json.loads(results.read_bytes())
    document["commands"][0]["tests"] = 17
    results.write_bytes(M.canonical_json_bytes(document))
    args.artifact = handoff / "Chudware-Setup-1.2.3-x86_64.exe"
    args.artifact.write_bytes(b"disposable fixture")
    args.bundle_root = tmp_path
    args.platform_signing_evidence = None
    def forbidden(*_args, **_kwargs):
        raise AssertionError("forged counts must not reach native commands")
    monkeypatch.setattr(M.subprocess, "run", forbidden)
    with pytest.raises(M.FinalizeError, match="locked input|counts differ from JUnit"):
        M.complete(args)
    assert not list(tmp_path.rglob("*.signing-request.bin"))
    assert not (tmp_path / "publish-bundle.v1.json").exists()


def test_stable_without_owner_policy_fails_before_native_building(tmp_path, monkeypatch):
    handoff, args = _prepared(tmp_path)
    metadata_path = handoff / "handoff.json"
    metadata = json.loads(metadata_path.read_bytes())
    metadata["locked_inputs"] = [item for item in metadata["locked_inputs"]
                                 if not item["path"].startswith("platform-publishers.")]
    metadata_path.write_bytes(M.canonical_json_bytes(metadata))
    (handoff / "platform-publishers.v1.json").unlink()
    (handoff / "platform-publishers.v1.sig").unlink()
    args.out = tmp_path
    def forbidden(*_args, **_kwargs):
        raise AssertionError("unauthorized publisher policy must not reach a builder")
    monkeypatch.setattr(M.subprocess, "run", forbidden)
    with pytest.raises(M.FinalizeError, match="owner-authorized publisher policy"):
        M.host_script(args, handoff)
    assert not list(tmp_path.glob("finalize-*.ps1"))


def test_authenticated_policy_cannot_replace_prepared_hash_binding(tmp_path):
    handoff, args = _prepared(tmp_path)
    policy = handoff / "platform-publishers.v1.json"
    signature = handoff / "platform-publishers.v1.sig"
    document = json.loads(policy.read_bytes())
    document["windows"]["certificate_sha256"] = ["2" * 64]
    raw = M.canonical_json_bytes(document)
    policy.write_bytes(raw)
    signature.write_bytes(base64.b64encode(_TEST_OWNER.sign(P.POLICY_DOMAIN + raw)) + b"\n")
    with pytest.raises(M.FinalizeError, match="locked input was replaced"):
        M.load_handoff(args, handoff)


def test_caller_payload_key_cannot_authorize_publishers(tmp_path, monkeypatch):
    handoff, args = _prepared(tmp_path)
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    other = Ed25519PrivateKey.generate()
    policy = handoff / "platform-publishers.v1.json"
    signature = handoff / "platform-publishers.v1.sig"
    signature.write_bytes(base64.b64encode(other.sign(P.POLICY_DOMAIN + policy.read_bytes())) + b"\n")
    metadata_path = handoff / "handoff.json"
    metadata = json.loads(metadata_path.read_bytes())
    for item in metadata["locked_inputs"]:
        if item["path"] == signature.name:
            item["sha256"] = M.digest(signature)
    metadata_path.write_bytes(M.canonical_json_bytes(metadata))
    args.release_public_key = other.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()
    def forbidden(*_args, **_kwargs):
        raise AssertionError("caller key must not authorize native publisher checks")
    monkeypatch.setattr(M.subprocess, "run", forbidden)
    with pytest.raises(M.PublisherPolicyError, match="not authorized by the offline owner"):
        M.load_handoff(args, handoff)


def test_invalid_policy_stops_before_native_verification(tmp_path, monkeypatch):
    args = _platform_args(tmp_path)
    args.platform_publisher_policy_signature.write_bytes(b"A" * 88 + b"\n")
    def forbidden(*_args, **_kwargs):
        raise AssertionError("invalid owner policy must not reach native verification")
    monkeypatch.setattr(P, "run_native", forbidden)
    with pytest.raises(M.PublisherPolicyError):
        M.write_platform_evidence(args)
    assert not args.out.exists()


def test_valid_platform_trust_for_unauthorized_signer_is_rejected(tmp_path, monkeypatch):
    args = _platform_args(tmp_path)
    monkeypatch.setattr(M.sys, "platform", "win32")
    monkeypatch.setenv("SIGNTOOL", "disposable-signtool")
    def another_publisher(command, **kwargs):
        return subprocess.CompletedProcess(command, 0,
            stdout=base64.b64encode(b"another publisher certificate") if command[0] == "powershell.exe" else b"",
            stderr=b"")
    monkeypatch.setattr(P, "run_native", another_publisher)
    with pytest.raises(M.PublisherPolicyError, match="not an owner-authorized Chudware publisher"):
        M.write_platform_evidence(args)
    assert not args.out.exists()


def test_native_tools_receive_readonly_private_snapshot(tmp_path, monkeypatch):
    args = _platform_args(tmp_path)
    monkeypatch.setattr(M.sys, "platform", "win32")
    monkeypatch.setenv("SIGNTOOL", "disposable-signtool")
    def run(command, **kwargs):
        if command[0] == "disposable-signtool":
            snapshot = Path(command[-1])
            assert snapshot != args.artifact.resolve()
            assert snapshot.read_bytes() == args.artifact.read_bytes()
            assert not snapshot.stat().st_mode & 0o222
            private_policy = snapshot.parent.parent / "platform-publishers.v1.json"
            assert private_policy.read_bytes() == args.platform_publisher_policy.read_bytes()
            assert not private_policy.stat().st_mode & 0o222
        return _windows_native_success(command, **kwargs)
    monkeypatch.setattr(P, "run_native", run)
    assert M.write_platform_evidence(args) == 0


@pytest.mark.parametrize("field", ["publisher", "publisher_policy_sha256"])
def test_forged_publisher_binding_cannot_complete_release(tmp_path, monkeypatch, field):
    handoff, args = _prepared(tmp_path)
    policy, signature = _portable_policy(tmp_path)
    args.artifact = handoff / "Chudware-Setup-1.2.3-x86_64.exe"
    args.artifact.write_bytes(b"disposable fixture")
    args.bundle_root = tmp_path
    args.platform_signing_evidence = tmp_path / "platform-signing.v1.json"
    document = {
        "schema": "platform-signing.v1", "platform": "windows", "kind": "authenticode",
        "artifact_sha256": M.digest(args.artifact), "verified": True,
        "publisher_policy_sha256": M.digest(policy),
        "publisher": {"certificate_sha256": hashlib.sha256(_CERTIFICATE_BYTES).hexdigest()},
    }
    document[field] = {"certificate_sha256": "3" * 64} if field == "publisher" else "3" * 64
    args.platform_signing_evidence.write_bytes(M.canonical_json_bytes(document))
    monkeypatch.setattr(M.sys, "platform", "win32")
    monkeypatch.setenv("SIGNTOOL", "disposable-signtool")
    monkeypatch.setattr(P, "run_native", _windows_native_success)
    with pytest.raises(M.FinalizeError, match="publisher or policy binding"):
        M.complete(args)
    assert not list(tmp_path.rglob("*.signing-request.bin"))
    assert not (tmp_path / "publish-bundle.v1.json").exists()


def test_policy_changed_during_native_verification_is_rejected(tmp_path, monkeypatch):
    args = _platform_args(tmp_path)
    monkeypatch.setattr(M.sys, "platform", "win32")
    monkeypatch.setenv("SIGNTOOL", "disposable-signtool")
    def changed(command, **kwargs):
        if command[0] == "disposable-signtool":
            args.platform_publisher_policy.write_bytes(args.platform_publisher_policy.read_bytes() + b"\n")
        return _windows_native_success(command, **kwargs)
    monkeypatch.setattr(P, "run_native", changed)
    with pytest.raises(M.PublisherPolicyError, match="changed during native verification"):
        M.write_platform_evidence(args)
    assert not args.out.exists()


@pytest.mark.parametrize(("team", "identifier", "plist_identifier"), [
    ("OTHERTEAM1", P.BUNDLE_ID, P.BUNDLE_ID),
    (_TEAM_ID, "com.example.Other", P.BUNDLE_ID),
    (_TEAM_ID, P.BUNDLE_ID, "com.example.Other"),
])
def test_macos_native_publisher_and_sealed_bundle_id_are_required(
    tmp_path, monkeypatch, team, identifier, plist_identifier
):
    args = _platform_args(tmp_path, "macos")
    with zipfile.ZipFile(args.artifact, "w") as archive:
        archive.writestr("Chudware.app/Contents/placeholder", b"fixture")
    monkeypatch.setattr(M.sys, "platform", "darwin")
    def run(command, **kwargs):
        if command[0] == "ditto":
            contents = Path(command[-1]) / "Chudware.app/Contents"
            contents.mkdir(parents=True)
            (contents / "Info.plist").write_bytes(plistlib.dumps({"CFBundleIdentifier": plist_identifier}))
        return subprocess.CompletedProcess(command, 0, stdout="",
            stderr=f"TeamIdentifier={team}\nIdentifier={identifier}\n")
    monkeypatch.setattr(P, "run_native", run)
    with pytest.raises(M.PublisherPolicyError):
        M.write_platform_evidence(args)
    assert not args.out.exists()


def test_external_handoff_metadata_must_match_retained_bytes(tmp_path):
    handoff, args = _prepared(tmp_path, channel="beta")
    external = tmp_path / "trusted-handoff.json"
    external.write_bytes((handoff / "handoff.json").read_bytes())
    args.handoff_metadata = external
    metadata, _manifest, _key = M.load_handoff(args, handoff)
    assert metadata["commit"] == args.commit
    replacement = json.loads(external.read_bytes())
    replacement["locked_inputs"][0]["sha256"] = "4" * 64
    external.write_bytes(M.canonical_json_bytes(replacement))
    with pytest.raises(M.FinalizeError, match="differs from the retained"):
        M.load_handoff(args, handoff)


def test_owner_policy_signature_accepts_windows_text_line_ending(tmp_path):
    args = _platform_args(tmp_path)
    signature = args.platform_publisher_policy_signature
    signature.write_bytes(signature.read_bytes().replace(b"\n", b"\r\n"))
    policy = P.load_policy(args.platform_publisher_policy, signature)
    assert policy.sha256 == M.digest(args.platform_publisher_policy)


@pytest.mark.parametrize("entry", ["../outside", "/absolute", "Chudware.app/../outside",
                                  r"Chudware.app\..\outside"])
def test_native_mac_extraction_rejects_unsafe_archive_before_commands(tmp_path, monkeypatch, entry):
    args = _platform_args(tmp_path, "macos")
    with zipfile.ZipFile(args.artifact, "w") as archive:
        archive.writestr(entry, b"fixture")
    monkeypatch.setattr(M.sys, "platform", "darwin")
    def forbidden(*_args, **_kwargs):
        raise AssertionError("unsafe archive must not reach native extraction")
    monkeypatch.setattr(P, "run_native", forbidden)
    with pytest.raises(M.PublisherPolicyError, match="unsafe path"):
        M.write_platform_evidence(args)
    assert not args.out.exists()


@pytest.mark.parametrize("target", ["/absolute", "../../../outside"])
def test_native_mac_extraction_rejects_unsafe_link_before_commands(tmp_path, monkeypatch, target):
    args = _platform_args(tmp_path, "macos")
    with zipfile.ZipFile(args.artifact, "w") as archive:
        entry = zipfile.ZipInfo("Chudware.app/Contents/link")
        entry.create_system = 3
        entry.external_attr = 0o120777 << 16
        archive.writestr(entry, target.encode("utf-8"))
    monkeypatch.setattr(M.sys, "platform", "darwin")
    def forbidden(*_args, **_kwargs):
        raise AssertionError("unsafe link must not reach native extraction")
    monkeypatch.setattr(P, "run_native", forbidden)
    with pytest.raises(M.PublisherPolicyError, match="unsafe link"):
        M.write_platform_evidence(args)
    assert not args.out.exists()


def test_owner_policy_request_tool_emits_exact_preimage_without_signing(tmp_path):
    policy, _signature = _publisher_policy(tmp_path)
    request = tmp_path / "publisher-request.bin"
    assert P.main([str(policy), "--out", str(request)]) == 0
    assert request.read_bytes() == P.POLICY_DOMAIN + policy.read_bytes()
    assert P.main([str(policy), "--out", str(request)]) == 2


def test_prepared_windows_payload_helpers_are_not_retained_installers(tmp_path, monkeypatch):
    handoff, args = _prepared(tmp_path, channel="beta")
    payload = handoff / "Chudware"
    for name in ("chudware-hostd.exe", "chudware-worker.exe",
                 "chudware-mcp.exe", "chudware-uninstall-helper.exe"):
        (payload / name).write_bytes(b"disposable compiled-payload fixture")
    _manifest, canonical, _snapshots = build_manifest(
        payload, version="1.2.3", platform="windows", arch="x86_64",
        key_id="release-v1", created_at="2026-10-09T00:00:00Z",
    )
    (handoff / "payload-manifest.v1.json").write_bytes(canonical)
    args.out = tmp_path
    def portable(_args, _handoff):
        (tmp_path / "builder").mkdir()
        return tmp_path / "signature", tmp_path / "public-key"
    monkeypatch.setattr(M, "_copy_portable_inputs", portable)
    assert M.host_script(args, handoff).is_file()
