"""Build one gateway ci/publish body for any Windows release format."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--format", required=True)
    parser.add_argument("--signature", type=Path, required=True)
    parser.add_argument("--stable-signature", type=Path, required=True)
    parser.add_argument("--evidence-dir", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    body: dict[str, object] = {
        "version": os.environ["RELEASE_VERSION"],
        "stable_version": os.environ["RELEASE_STABLE_VERSION"],
        "platform": "windows",
        "arch": "x86_64",
        "channel": "beta",
        # Explicit for BOTH Windows formats; the .exe no longer relies on the
        # gateway model's backwards-compatible default.
        "format": args.format,
        "key_id": os.environ["CHUDWARE_RELEASE_KEY_ID"],
        "min_supported": os.environ["CHUDWARE_RELEASE_MIN_SUPPORTED"],
        "sha256": digest(args.artifact),
        "size": args.artifact.stat().st_size,
        "signature": args.signature.read_text(encoding="ascii").strip(),
        "stable_signature": args.stable_signature.read_text(encoding="ascii").strip(),
        "notes_url": os.environ["CHUDWARE_RELEASE_NOTES_URL"],
        "commit_sha": os.environ["RELEASE_COMMIT"],
    }
    if args.evidence_dir is not None:
        evidence: dict[str, str] = {}
        for field, name in (
            ("sbom", "sbom.cdx.json"),
            ("provenance", "provenance.slsa.json"),
            ("platform_test", "platform-test.v1.json"),
        ):
            document = args.evidence_dir / name
            evidence[field + "_sha256"] = digest(document)
            evidence[field + "_signature_sha256"] = digest(
                document.with_name(document.name + ".sig")
            )
        body["evidence"] = evidence
    args.output.write_text(json.dumps(body), encoding="ascii")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
