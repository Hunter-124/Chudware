"""Behavioral offline operator-preparation regressions using only inert wheels.

No fixture represents a production approval. The smoke uses the real preparation
CLI, base Python venv/ensurepip, pip --no-index/--require-hashes and installed-byte
validation. Run centrally; this file does not fetch or build the chudcode-rpc SDK.
"""
from __future__ import annotations

from argparse import Namespace
from copy import deepcopy
import json
from pathlib import Path
import stat
import subprocess
import sys
import zipfile

import pytest

from scripts import prepare_runtime_wheels as preparation
from services.installer import wheelhouse


SCRIPT = Path(preparation.__file__)
BASE = str(Path(sys._base_executable).absolute())
SOURCE = {"repository": wheelhouse.CHUDCODE_REPOSITORY, "repository_commit": "1" * 40}


def _wheel(directory, name, version="1.0", dependencies=(), *, tag="py3-none-any", files=None):
    directory.mkdir(exist_ok=True)
    normalized = name.replace("-", "_")
    info = f"{normalized}-{version}.dist-info"
    members = {f"{normalized}/__init__.py": "INERT_FIXTURE = True\n",
               f"{info}/METADATA": (f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\nRequires-Python: >=3.11\n"
                                         + "".join(f"Requires-Dist: {dependency}\n" for dependency in dependencies)),
               f"{info}/WHEEL": f"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: {tag}\n"}
    members.update(files or {})
    members[f"{info}/RECORD"] = "".join(f"{path},,\n" for path in [*members, f"{info}/RECORD"])
    filename = f"{normalized}-{version}-{tag}.whl"
    with zipfile.ZipFile(directory / filename, "w") as archive:
        for name_, content in members.items():
            entry = zipfile.ZipInfo(name_, date_time=(2026, 1, 1, 0, 0, 0))
            archive.writestr(entry, content)
    source = ({"kind": "vcs", **SOURCE} if name == "chudcode-rpc" else
              {"kind": "archive", "url": f"https://fixture.invalid/reviewed/{filename}"})
    return {"filename": filename, "name": name, "version": version,
            "sha256": wheelhouse._digest(directory / filename), "source": source}


def _inputs(directory):
    directory.mkdir(parents=True, exist_ok=True)
    context = preparation.target_context()
    platform = context["environment"]["sys_platform"]
    requirements = directory / "requirements.txt"
    requirements.write_text("--no-index\n--only-binary=:all:\nchudcode-rpc[optional]==0.3.1\n"
                            f"inert-root==1.0; sys_platform == '{platform}'\n"
                            "inert-foreign==1.0; sys_platform == 'fixture-foreign'\n")
    release = directory / "release.txt"
    release.write_text("inert-release[verify]==1.0\n")
    gate = directory / "gate.txt"
    gate.write_text("inert-gate==1.0\n")
    native = directory / "CHUDCODE_ARTIFACT.lock"
    native.write_bytes(preparation.canonical(SOURCE))
    wheels = directory / "candidates"
    entries = [_wheel(wheels, "chudcode-rpc", "0.3.1", (
        f"inert-target==1.0; sys_platform == '{platform}'", "inert-optional==1.0; extra == 'optional'")),
        _wheel(wheels, "inert-target"), _wheel(wheels, "inert-root"), _wheel(wheels, "inert-optional"),
        _wheel(wheels, "inert-release", dependencies=("inert-extra==1.0; extra == 'verify'",)),
        _wheel(wheels, "inert-extra"), _wheel(wheels, "inert-gate")]
    manifest = {"schema": preparation.REVIEW_SCHEMA,
                "approval": {"decision": "approved", "reference": "disposable inert-fixture explicit operator review"},
                "requirements_sha256": wheelhouse._digest(requirements),
                "additional_requirements_sha256": [wheelhouse._digest(release), wheelhouse._digest(gate)],
                "native_lock_sha256": wheelhouse._digest(native), "chudcode_source": SOURCE,
                "target": {**context, "wheels": entries}}
    review = directory / "review.json"
    review.write_bytes(preparation.canonical(manifest))
    return Namespace(manifest=review, review_sha256=wheelhouse._digest(review), wheel_directory=wheels,
                     output=directory / "prepared", requirements=requirements, additional_requirements=[release, gate],
                     chudcode_lock=native, target_context=None), manifest


@pytest.fixture
def inputs(tmp_path):
    return _inputs(tmp_path)


def _rewrite(args, manifest):
    args.manifest.write_bytes(preparation.canonical(manifest))
    args.review_sha256 = wheelhouse._digest(args.manifest)


def _cli_args(args):
    command = ["prepare", "--manifest", str(args.manifest), "--review-sha256", args.review_sha256,
               "--wheel-directory", str(args.wheel_directory), "--output", str(args.output),
               "--requirements", str(args.requirements), "--chudcode-lock", str(args.chudcode_lock)]
    for path in args.additional_requirements:
        command.extend(("--additional-requirements", str(path)))
    if args.target_context:
        command.extend(("--target-context", str(args.target_context)))
    return command


def _cli(command):
    return subprocess.run([BASE, "-I", "-S", str(SCRIPT), *command], check=False,
                          env=wheelhouse.pip_environment(), capture_output=True, text=True, timeout=120)


def _unchanged_failure(args, match):
    with pytest.raises((wheelhouse.WheelhouseError, ValueError, OSError), match=match):
        preparation.prepare(args)
    assert not args.output.exists()
    assert not list(args.output.parent.glob(".runtime-preparation-*"))


def test_cli_preparation_install_and_base_authenticated_validation_offline(inputs, tmp_path):
    args, manifest = inputs
    original = {path.name: path.read_bytes() for path in args.wheel_directory.iterdir()}
    prepared = _cli(_cli_args(args))
    assert prepared.returncode == 0, prepared.stderr
    assert json.loads(prepared.stdout)["status"] == "prepared"
    lock = json.loads((args.output / "RUNTIME_WHEELS.lock").read_bytes())
    assert set(lock) == {"schema", "requirements_sha256", "chudcode_source", "targets"}
    assert (args.output / "RUNTIME_WHEELS.lock").read_bytes() == preparation.canonical(preparation.runtime_lock(manifest))
    assert original == {path.name: path.read_bytes() for path in args.wheel_directory.iterdir()}
    venv = tmp_path / "disposable-installed-runtime"
    created = subprocess.run([BASE, "-I", "-S", "-m", "venv", str(venv)], check=False,
                             env=wheelhouse.pip_environment(), capture_output=True, text=True, timeout=120)
    assert created.returncode == 0, created.stderr
    python = venv / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    additional = [args.output / "declarations/additional-000.txt", args.output / "declarations/additional-001.txt"]
    with wheelhouse.verified_wheelhouse(args.output / "wheelhouse", args.output / "declarations/requirements.txt",
                                       args.output / "RUNTIME_WHEELS.lock", args.output / "CHUDCODE_ARTIFACT.lock",
                                       additional_requirements=additional) as identity:
        install = subprocess.run(wheelhouse.install_command(python, identity["requirements"]), check=False,
                                 env=wheelhouse.pip_environment(), capture_output=True, text=True, timeout=120)
        assert install.returncode == 0, install.stderr
    validation = ["validate-installed", "--bundle", str(args.output), "--venv", str(venv),
                  "--review-sha256", args.review_sha256]
    checked = _cli(validation)
    assert checked.returncode == 0, checked.stderr
    assert json.loads(checked.stdout)["status"] == "installed-runtime-validated"
    site = venv / ("Lib/site-packages" if sys.platform == "win32" else
                   f"lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages")
    payload = site / "chudcode_rpc/__init__.py"
    payload.write_text("raise AssertionError('candidate code must never execute')\n")
    rejected = _cli(validation)
    assert rejected.returncode != 0
    assert "installed payload differs" in rejected.stderr
    assert "candidate code must never execute" not in rejected.stderr


def test_candidate_is_not_approved_or_an_installable_runtime_lock(inputs, monkeypatch):
    args, _ = inputs
    args.output = args.output.parent / "candidate.json"
    assert preparation.candidate(args)["status"] == "unapproved-candidate"
    args.manifest = args.output
    args.review_sha256 = wheelhouse._digest(args.manifest)
    args.output = args.output.parent / "attempted-installable-output"
    monkeypatch.setattr(wheelhouse, "_copy_wheel", lambda *_: pytest.fail("candidate approval must precede wheel copy"))
    _unchanged_failure(args, "explicit operator approval")
    candidate = json.loads(args.manifest.read_bytes())
    assert candidate["approval"] == {"decision": "pending", "reference": None}
    assert all(entry["source"] is None for entry in candidate["target"]["wheels"])


@pytest.mark.parametrize("failure", ["pending", "blank-reference", "substituted-review", "duplicate-key", "invalid-source"])
def test_invalid_operator_review_rejected_before_copy(inputs, monkeypatch, failure):
    args, manifest = inputs
    if failure == "pending":
        manifest["approval"]["decision"] = "pending"
    elif failure == "blank-reference":
        manifest["approval"]["reference"] = ""
    elif failure == "invalid-source":
        manifest["target"]["wheels"][0]["source"] = None
    _rewrite(args, manifest)
    if failure == "substituted-review":
        args.manifest.write_bytes(args.manifest.read_bytes() + b" ")
    elif failure == "duplicate-key":
        args.manifest.write_bytes(args.manifest.read_bytes().replace(b'"schema":', b'"schema":"duplicate","schema":', 1))
        args.review_sha256 = wheelhouse._digest(args.manifest)
    monkeypatch.setattr(wheelhouse, "_copy_wheel", lambda *_: pytest.fail("invalid review must precede any wheel copy"))
    _unchanged_failure(args, "approval|digest|duplicate review key|source identity")


@pytest.mark.parametrize("failure", ["changed-root", "changed-release", "omitted-gate", "reordered-additional", "native-changed", "sdk-source", "target-environment", "target-tags"])
def test_reviewed_declarations_source_and_exact_target_cannot_be_substituted(inputs, failure):
    args, manifest = inputs
    if failure == "changed-root":
        args.requirements.write_text("chudcode-rpc==0.3.1\n")
    elif failure == "changed-release":
        args.additional_requirements[0].write_text("inert-release==1.0\n")
    elif failure == "omitted-gate":
        args.additional_requirements.pop()
    elif failure == "reordered-additional":
        args.additional_requirements.reverse()
    elif failure == "native-changed":
        args.chudcode_lock.write_bytes(preparation.canonical({**SOURCE, "repository_commit": "2" * 40}))
    elif failure == "sdk-source":
        manifest["target"]["wheels"][0]["source"]["repository_commit"] = "2" * 40
        _rewrite(args, manifest)
    elif failure == "target-environment":
        manifest["target"]["environment"]["platform_release"] += "-changed"
        _rewrite(args, manifest)
    else:
        manifest["target"]["tags"] = ["py3-none-any"]
        _rewrite(args, manifest)
    _unchanged_failure(args, "changed|omitted|source differs|environment/tags")


@pytest.mark.parametrize("name", ["inert-target", "inert-root", "inert-optional", "inert-extra", "inert-gate"])
def test_missing_target_root_transitive_extra_or_gate_closure_is_rejected(inputs, name):
    args, manifest = inputs
    removed = next(entry for entry in manifest["target"]["wheels"] if entry["name"] == name)
    manifest["target"]["wheels"].remove(removed)
    (args.wheel_directory / removed["filename"]).unlink()
    _rewrite(args, manifest)
    _unchanged_failure(args, name)


@pytest.mark.parametrize("failure", ["absent", "tampered", "duplicate-identity", "unreviewed-extra", "wheel-symlink", "ancestor-symlink", "incompatible-tag", "archive-traversal", "archive-duplicate", "archive-symlink", "overlapping-payload", "wheel-header-tags"])
def test_unapproved_ambiguous_unsafe_and_incompatible_wheel_inputs_fail_atomically(inputs, tmp_path, failure):
    args, manifest = inputs
    sdk_wheel = manifest["target"]["wheels"][0]
    path = args.wheel_directory / sdk_wheel["filename"]
    if failure == "absent":
        path.unlink()
    elif failure == "tampered":
        with path.open("ab") as stream:
            stream.write(b"unreviewed substitution")
    elif failure == "duplicate-identity":
        manifest["target"]["wheels"].append(deepcopy(sdk_wheel))
    elif failure == "unreviewed-extra":
        _wheel(args.wheel_directory, "unreviewed-extra")
    elif failure == "wheel-symlink":
        moved = tmp_path / "outside.whl"
        path.rename(moved)
        path.symlink_to(moved)
    elif failure == "ancestor-symlink":
        linked = tmp_path / "linked-input"
        linked.symlink_to(args.wheel_directory, target_is_directory=True)
        args.wheel_directory = linked
    elif failure == "incompatible-tag":
        path.unlink()
        replacement = _wheel(args.wheel_directory, "chudcode-rpc", "0.3.1", tag="cp20-cp20-fixture_unsupported")
        manifest["target"]["wheels"][0] = replacement
    elif failure == "overlapping-payload":
        entry = manifest["target"]["wheels"][1]
        with zipfile.ZipFile(args.wheel_directory / entry["filename"], "a") as archive:
            archive.writestr("chudcode_rpc/__init__.py", "conflicting reviewed payload\n")
        entry["sha256"] = wheelhouse._digest(args.wheel_directory / entry["filename"])
    elif failure == "wheel-header-tags":
        with zipfile.ZipFile(path) as archive:
            members = {member: archive.read(member) for member in archive.namelist()}
        members["chudcode_rpc-0.3.1.dist-info/WHEEL"] = b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: cp20-cp20-fixture_unsupported\n"
        with zipfile.ZipFile(path, "w") as archive:
            for name, value in members.items():
                archive.writestr(name, value)
        sdk_wheel["sha256"] = wheelhouse._digest(path)
    else:
        with zipfile.ZipFile(path, "a") as archive:
            if failure == "archive-traversal":
                archive.writestr("../outside.py", "must not escape\n")
            elif failure == "archive-duplicate":
                with pytest.warns(UserWarning):
                    archive.writestr("chudcode_rpc/__init__.py", "duplicate\n")
            else:
                entry = zipfile.ZipInfo("chudcode_rpc/unsafe-link")
                entry.create_system = 3
                entry.external_attr = (stat.S_IFLNK | 0o777) << 16
                archive.writestr(entry, "../../outside.py")
        sdk_wheel["sha256"] = wheelhouse._digest(path)
    _rewrite(args, manifest)
    _unchanged_failure(args, "missing|hash mismatch|duplicate|unexpected|symlink|support selected|unsafe wheel|overlapping|header/tags")
    assert not (tmp_path / "outside.py").exists()


@pytest.mark.parametrize("failure", ["existing-output", "output-parent-symlink", "filename-traversal", "output-traversal"])
def test_writes_do_not_replace_or_traverse_operator_paths(inputs, tmp_path, failure):
    args, manifest = inputs
    sentinel = tmp_path / "retained"
    sentinel.mkdir()
    (sentinel / "user-file").write_text("original user environment")
    if failure == "existing-output":
        args.output = sentinel
    elif failure == "output-parent-symlink":
        linked = tmp_path / "output-link"
        linked.symlink_to(sentinel, target_is_directory=True)
        args.output = linked / "new-output"
    elif failure == "filename-traversal":
        manifest["target"]["wheels"][0]["filename"] = "../retained/user-file"
        _rewrite(args, manifest)
    else:
        args.output = tmp_path / "missing" / ".." / "retained"
    with pytest.raises(wheelhouse.WheelhouseError, match="exists|symlink|traversal"):
        preparation.prepare(args)
    assert (sentinel / "user-file").read_text() == "original user environment"
    assert list(sentinel.iterdir()) == [sentinel / "user-file"]


def test_cli_failure_is_machine_readable_and_preserves_existing_environment(inputs, tmp_path):
    args, _ = inputs
    args.output.mkdir()
    sentinel = args.output / "user-file"
    sentinel.write_text("untouched")
    result = _cli(_cli_args(args))
    assert result.returncode == 1
    assert json.loads(result.stderr)["status"] == "error"
    assert sentinel.read_text() == "untouched"


def test_atomic_publication_refuses_racing_destination(tmp_path):
    staged = tmp_path / "staged"
    staged.mkdir()
    (staged / "reviewed").write_text("reviewed data")
    destination = tmp_path / "already-there"
    destination.mkdir()
    (destination / "original").write_text("original data")
    with pytest.raises(OSError):
        preparation._publish(staged, destination)
    assert (destination / "original").read_text() == "original data"
    assert (staged / "reviewed").read_text() == "reviewed data"
