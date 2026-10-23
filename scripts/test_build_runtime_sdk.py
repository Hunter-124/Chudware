"""Offline approved-source/toolchain boundaries; no SDK source builds run implicitly.

The optional real setuptools regression uses an already available independently
hashed public wheel. It never downloads it. All source/approval records are
explicit disposable fixtures, not production provenance or base attestations.
"""
from __future__ import annotations

from argparse import Namespace
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tarfile

import pytest

from scripts import build_runtime_sdk as builder
from scripts import prepare_runtime_wheels as preparation
from scripts.test_prepare_runtime_wheels import _wheel
from services.installer.wheelhouse import WheelhouseError, pip_environment

BASE = str(Path(sys._base_executable).absolute())
SOURCE = {"repository": "https://github.com/Hunter-124/chudcode.git", "repository_commit": "1" * 40}
PUBLIC_BACKEND_SHA256 = "29b23c360f22f414dc7336bb39178cc7bcbf6021ed2733cde173f09dba19abb3"
PUBLIC_BACKEND_URL = "https://files.pythonhosted.org/packages/5d/40/e1e72872c6354b306daef1703549e8e83b4d43cfea356311bf722a043752/setuptools-83.0.0-py3-none-any.whl"


def _archive(path, extra=(), *, dynamic=False):
    project = ('[build-system]\nrequires=["setuptools>=77.0.3"]\nbuild-backend="setuptools.build_meta"\n'
               '[project]\nname="chudcode-rpc"\nversion="0.3.1"\nrequires-python=">=3.11"\n'
               'readme="README.md"\nlicense="MIT"\n'
               '[tool.setuptools]\npackage-dir={""="src"}\n[tool.setuptools.packages.find]\nwhere=["src"]\n')
    if dynamic:
        project = project.replace('readme="README.md"', 'dynamic=["description"]\nreadme="README.md"')
    members = [("python/chudcode-rpc/pyproject.toml", project.encode()),
               ("python/chudcode-rpc/README.md", b"Inert SDK build fixture\n"),
               ("python/chudcode-rpc/LICENSE", b"MIT inert test fixture\n"),
               ("python/chudcode-rpc/src/chudcode_rpc/__init__.py", b"INERT_FIXTURE = True\n")]
    with tarfile.open(path, "w") as archive:
        for name, contents in [*members, *extra]:
            member = tarfile.TarInfo(name)
            member.size = len(contents)
            member.mode = 0o644
            archive.addfile(member, io.BytesIO(contents))


def _inputs(directory):
    archive = directory / "source.tar"
    _archive(archive)
    native = directory / "native.lock"
    native.write_bytes(preparation.canonical(SOURCE))
    backend = directory / "backend"
    entry = _wheel(backend, "setuptools", "83.0.0")
    review = {"schema": builder.BUILD_SCHEMA,
              "approval": {"decision": "approved", "reference": "disposable inert source/toolchain fixture review"},
              "source_archive_sha256": preparation._digest(archive), "source_subdirectory": "python/chudcode-rpc",
              "chudcode_source": SOURCE, "native_lock_sha256": preparation._digest(native),
              "base_python": {"executable_sha256": preparation._digest(Path(BASE).resolve()),
                              "version": ".".join(map(str, sys.version_info[:3])),
                              "identity_reference": "disposable selected base prerequisite; not a release-image attestation"},
              "backend_wheel": entry, "target": preparation.target_context(), "source_date_epoch": 1767225600}
    manifest = directory / "build-review.json"
    manifest.write_bytes(preparation.canonical(review))
    return Namespace(manifest=manifest, review_sha256=preparation._digest(manifest), source_archive=archive,
                     chudcode_lock=native, wheel_directory=backend, output=directory / "built", timeout_seconds=120), review


def _rewrite(args, review):
    args.manifest.write_bytes(preparation.canonical(review))
    args.review_sha256 = preparation._digest(args.manifest)


def _cli(args):
    return subprocess.run([BASE, "-I", "-S", str(Path(builder.__file__)), "build-sdk",
                           "--manifest", str(args.manifest), "--review-sha256", args.review_sha256,
                           "--source-archive", str(args.source_archive), "--chudcode-lock", str(args.chudcode_lock),
                           "--wheel-directory", str(args.wheel_directory), "--output", str(args.output),
                           "--timeout-seconds", str(args.timeout_seconds)],
                          env=pip_environment(), check=False, capture_output=True, text=True, timeout=300)


@pytest.mark.parametrize("failure", ["approval", "review-substitution", "source-substitution", "backend-substitution", "native-substitution",
                                      "source-traversal", "source-repository", "base-substitution", "base-reference", "target-substitution", "epoch", "backend-version"])
def test_cli_refuses_unapproved_substituted_or_incomplete_build_inputs(tmp_path, failure):
    args, review = _inputs(tmp_path)
    if failure == "approval":
        review["approval"]["decision"] = "pending"
    elif failure == "source-traversal":
        review["source_subdirectory"] = "../outside"
    elif failure == "source-repository":
        review["chudcode_source"] = {**SOURCE, "repository": "https://fixture.invalid/unreviewed.git"}
    elif failure == "base-substitution":
        review["base_python"]["executable_sha256"] = "0" * 64
    elif failure == "base-reference":
        review["base_python"]["identity_reference"] = ""
    elif failure == "target-substitution":
        review["target"]["environment"]["platform_release"] += "-substitution"
    elif failure == "epoch":
        review["source_date_epoch"] = True
    elif failure == "backend-version":
        review["backend_wheel"]["version"] = "80.10.2"
    _rewrite(args, review)
    if failure == "review-substitution":
        args.manifest.write_bytes(args.manifest.read_bytes() + b" ")
    elif failure == "source-substitution":
        with args.source_archive.open("ab") as stream:
            stream.write(b"source substitution")
    elif failure == "backend-substitution":
        with (args.wheel_directory / builder.BACKEND_FILENAME).open("ab") as stream:
            stream.write(b"backend substitution")
    elif failure == "native-substitution":
        args.chudcode_lock.write_bytes(preparation.canonical({**SOURCE, "repository_commit": "2" * 40}))
    result = _cli(args)
    assert result.returncode == 1
    error = json.loads(result.stderr)
    assert error["status"] == "error"
    assert "SDK" in error["reason"]
    assert not args.output.exists()
    assert not list(tmp_path.glob(".sdk-build-publication-*"))


@pytest.mark.parametrize("failure", ["traversal", "absolute", "duplicate", "symlink", "hardlink", "device", "outside-subtree", "dynamic-project", "setup-hook"])
def test_source_extraction_refuses_unreviewable_archive_and_project_shapes(tmp_path, failure):
    archive = tmp_path / "source.tar"
    extra = []
    if failure == "traversal":
        extra = [("python/chudcode-rpc/../../outside", b"must never escape")]
    elif failure == "absolute":
        extra = [("/tmp/escape", b"must never escape")]
    elif failure == "duplicate":
        extra = [("python/chudcode-rpc/README.md", b"duplicate")]
    elif failure == "outside-subtree":
        extra = [("python/unreviewed/setup.py", b"must never execute")]
    elif failure == "setup-hook":
        extra = [("python/chudcode-rpc/setup.py", b"raise AssertionError('must never execute')\n")]
    _archive(archive, extra, dynamic=failure == "dynamic-project")
    if failure in ("symlink", "hardlink", "device"):
        with tarfile.open(archive, "a") as tar:
            entry = tarfile.TarInfo("python/chudcode-rpc/unsafe-member")
            entry.type = {"symlink": tarfile.SYMTYPE, "hardlink": tarfile.LNKTYPE, "device": tarfile.CHRTYPE}[failure]
            entry.linkname = "../../outside"
            tar.addfile(entry)
    destination = tmp_path / "private-source"
    with pytest.raises(WheelhouseError, match="unsafe|declarations differ|setup configuration"):
        builder._source_archive(archive.read_bytes(), destination, 1767225600)
    assert not (tmp_path / "outside").exists()


def test_source_extraction_normalizes_file_modes_and_epoch_without_importing_source(tmp_path):
    archive = tmp_path / "source.tar"
    _archive(archive, [("python/chudcode-rpc/src/chudcode_rpc/poison.py", b"raise AssertionError('source must not import during inspection')\n")])
    source = builder._source_archive(archive.read_bytes(), tmp_path / "source", 1767225600)
    poison = source / "src/chudcode_rpc/poison.py"
    assert poison.read_bytes().startswith(b"raise AssertionError")
    assert int(poison.stat().st_mtime) == 1767225600
    assert stat.S_IMODE(poison.stat().st_mode) == 0o644


@pytest.mark.parametrize("dependency", ["unreviewed-backend>=1", "unreviewed-backend @ https://fixture.invalid/package.whl"])
def test_backend_closure_rejects_unreviewed_mandatory_dependencies_before_execution(tmp_path, dependency):
    backend = tmp_path / "backend"
    entry = _wheel(backend, "setuptools", "83.0.0", dependencies=(dependency,),
                   files={"setuptools/poison.py": "raise AssertionError('unapproved backend must not import')\n"})
    with pytest.raises(WheelhouseError, match="mandatory dependency closure"):
        builder._backend_identity(backend / builder.BACKEND_FILENAME, entry, preparation.target_context())


def test_builder_refuses_output_replacement_before_backend_execution(tmp_path):
    args, _ = _inputs(tmp_path)
    args.output.mkdir()
    sentinel = args.output / "original"
    sentinel.write_text("existing environment")
    result = _cli(args)
    assert result.returncode == 1
    assert "output already exists" in json.loads(result.stderr)["reason"]
    assert sentinel.read_text() == "existing environment"


def test_real_reviewed_public_backend_build_is_reproducible_and_remains_unapproved(tmp_path):
    provided = os.environ.get("CHUDWARE_TEST_SETUPTOOLS_WHEEL")
    if not provided:
        pytest.skip("operator must supply independently hashed setuptools83 wheel offline; no download fallback")
    backend = Path(provided)
    assert backend.name == builder.BACKEND_FILENAME
    assert preparation._digest(backend) == PUBLIC_BACKEND_SHA256
    args, review = _inputs(tmp_path)
    args.wheel_directory = backend.parent
    review["backend_wheel"] = {"filename": builder.BACKEND_FILENAME, "name": "setuptools", "version": "83.0.0",
                               "sha256": PUBLIC_BACKEND_SHA256, "source": {"kind": "archive", "url": PUBLIC_BACKEND_URL}}
    _rewrite(args, review)
    first = _cli(args)
    assert first.returncode == 0, first.stderr
    first_wheel = (args.output / builder.SDK_FILENAME).read_bytes()
    first_result = json.loads((args.output / "SDK_BUILD_RESULT.json").read_bytes())
    assert first_result["status"] == "unapproved-built-candidate"
    assert not (args.output / "RUNTIME_WHEELS.lock").exists()
    args.output = tmp_path / "second-independent-build"
    second = _cli(args)
    assert second.returncode == 0, second.stderr
    assert (args.output / builder.SDK_FILENAME).read_bytes() == first_wheel
    second_result = json.loads((args.output / "SDK_BUILD_RESULT.json").read_bytes())
    assert first_result["wheel"] == second_result["wheel"]
