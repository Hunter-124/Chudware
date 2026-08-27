"""Create signing requests for one generated Windows evidence bundle."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sign_release import DOMAINS


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    for name, kind in (
        ("sbom.cdx.json", "sbom"),
        ("provenance.slsa.json", "provenance"),
        ("platform-test.v1.json", "platform-test"),
    ):
        body = (args.directory / name).read_bytes()
        (args.directory / (name + ".signing-request.bin")).write_bytes(
            DOMAINS[kind] + body
        )
    document = json.loads(
        (args.directory / "platform-test.v1.json").read_text(encoding="utf-8")
    )
    missing = {"ctest core", "ctest host"} - set(document.get("commands", []))
    if missing or document.get("passed") is not True:
        raise SystemExit(
            "platform-test evidence does not record the executed suites: "
            + repr(sorted(missing))
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
