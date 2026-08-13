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


def _args(tmp_path, *, passed=True):
    artifact = tmp_path / "artifact"
    artifact.write_bytes(b"release")
    lock = tmp_path / "requirements.lock"
    lock.write_text("dependency==1.0\n")
    return argparse.Namespace(
        artifact=artifact, artifact_sha256=hashlib.sha256(b"release").hexdigest(),
        version="1.2.3", platform="linux", arch="x86_64", commit="a" * 40,
        locked_input=[lock], command=["pytest focused", "package smoke"], passed=passed,
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
    assert platform == {"schema": "platform-test.v1", "version": "1.2.3", "platform": "linux", "arch": "x86_64", "commit": "a" * 40, "artifact_sha256": args.artifact_sha256, "commands": ["pytest focused", "package smoke"], "passed": True}
    for path in outputs:
        value = json.loads(path.read_bytes())
        assert path.read_bytes() == evidence._canonical(value)


def test_failed_or_empty_test_evidence_is_rejected(tmp_path):
    args = _args(tmp_path, passed=False)
    with pytest.raises(ValueError, match="passed"):
        evidence.generate(args)
    args.passed = True
    args.command = ["  "]
    with pytest.raises(ValueError, match="nonempty"):
        evidence.generate(args)


def test_missing_locked_input_rejected(tmp_path):
    args = _args(tmp_path)
    args.locked_input = [tmp_path / "missing.lock"]
    with pytest.raises(ValueError, match="does not exist"):
        evidence.generate(args)
