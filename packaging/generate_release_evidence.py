#!/usr/bin/env python3
"""Generate canonical CycloneDX, SLSA provenance, and platform-test evidence."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any

try:
    from packaging.release_json import canonical_json_bytes
except ImportError:  # direct execution from packaging/
    from release_json import canonical_json_bytes

_SHA256 = re.compile(r"[0-9a-f]{64}")


def _canonical(value: dict[str, Any]) -> bytes:
    return canonical_json_bytes(value)


def _digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def locked_components(paths: list[Path]) -> list[dict[str, Any]]:
    components = []
    for path in sorted(paths, key=lambda item: item.as_posix()):
        if not path.is_file():
            raise ValueError(f"locked input does not exist: {path}")
        components.append({
            "type": "file",
            "name": path.as_posix(),
            "hashes": [{"alg": "SHA-256", "content": _digest(path)}],
        })
    if not components:
        raise ValueError("at least one locked input is required")
    return components


def generate(args: argparse.Namespace) -> list[Path]:
    if not _SHA256.fullmatch(args.artifact_sha256):
        raise ValueError("artifact_sha256 must be lowercase SHA-256")
    commands = [command.strip() for command in args.command if command.strip()]
    if not commands:
        raise ValueError("at least one nonempty platform test command is required")
    if not args.passed:
        raise ValueError("platform evidence may only be emitted after tests passed")
    components = locked_components(args.locked_input)
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
            "buildDefinition": {"buildType": "https://chudware.com/build/platform/v1", "externalParameters": {"version": args.version, "platform": args.platform, "arch": args.arch, "commit": args.commit}, "resolvedDependencies": [{"uri": component["name"], "digest": {"sha256": component["hashes"][0]["content"]}} for component in components]},
            "runDetails": {"builder": {"id": "https://chudware.com/builders/platform-v1"}, "metadata": {"invocationId": f"{args.commit}:{args.platform}:{args.arch}"}},
        },
    }
    platform_test = {"schema": "platform-test.v1", "version": args.version, "platform": args.platform, "arch": args.arch, "commit": args.commit, "artifact_sha256": args.artifact_sha256, "commands": commands, "passed": True}
    args.out.mkdir(parents=True, exist_ok=True)
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
    result.add_argument("--command", action="append", required=True)
    result.add_argument("--passed", action="store_true", required=True)
    result.add_argument("--out", type=Path, required=True)
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if not args.artifact.is_file():
        raise SystemExit(f"artifact does not exist: {args.artifact}")
    actual = _digest(args.artifact)
    if actual != args.artifact_sha256:
        raise SystemExit(f"artifact digest mismatch: expected {args.artifact_sha256}, got {actual}")
    for output in generate(args):
        print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
