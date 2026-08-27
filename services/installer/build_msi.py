"""Build a per-machine WiX MSI from an already-finalized signed payload."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path, PurePosixPath

from services.installer.build_release import sign_installer, write_artifact_signing_request
from services.installer.payload_manifest import (
    MANIFEST_NAME,
    SIGNATURE_NAME,
    PayloadManifestError,
    discover_payload_files,
    read_manifest,
    snapshot_payload,
    verify_manifest_payload,
    verify_signature,
    verify_snapshot,
)

REPO = Path(__file__).resolve().parent.parent.parent
WXS = REPO / "packaging" / "windows" / "chudware.wxs"
WIX_NS = "http://wixtoolset.org/schemas/v4/wxs"
_VERSION_RE = re.compile(
    r"(?P<numeric>(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*))"
    r"(?:-[0-9A-Za-z.-]+)?"
)

# Fixed UUIDv5 namespaces make every generated identifier stable. In particular,
# ProductCode is UUIDv5(PRODUCT_CODE_NAMESPACE, the full release version), so two
# builds of one version cannot acquire different Windows Installer identities.
PRODUCT_CODE_NAMESPACE = uuid.UUID("5f8cca69-541a-50aa-9540-99ad30ed5d24")
COMPONENT_NAMESPACE = uuid.UUID("d579c0e5-376b-51f7-ba5e-aa78ece45679")


def product_code(version: str) -> str:
    if _VERSION_RE.fullmatch(version) is None:
        raise RuntimeError("version must be major.minor.patch with an optional -prerelease")
    return "{" + str(uuid.uuid5(PRODUCT_CODE_NAMESPACE, version)).upper() + "}"


def product_version(version: str) -> str:
    match = _VERSION_RE.fullmatch(version)
    if match is None:
        raise RuntimeError("version must be major.minor.patch with an optional -prerelease")
    return match.group("numeric")


def _id(prefix: str, value: str) -> str:
    return prefix + hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]


def write_payload_authoring(payload: Path, relative_files: list[str], destination: Path) -> None:
    """Write one stable MSI component for every finalized payload file."""
    ET.register_namespace("", WIX_NS)
    wix = ET.Element(f"{{{WIX_NS}}}Wix")
    directories_fragment = ET.SubElement(wix, f"{{{WIX_NS}}}Fragment")
    directory_ref = ET.SubElement(
        directories_fragment, f"{{{WIX_NS}}}DirectoryRef", {"Id": "INSTALLFOLDER"}
    )
    groups_fragment = ET.SubElement(wix, f"{{{WIX_NS}}}Fragment")
    group = ET.SubElement(
        groups_fragment, f"{{{WIX_NS}}}ComponentGroup", {"Id": "PayloadComponents"}
    )
    directories: dict[tuple[str, ...], ET.Element] = {(): directory_ref}

    for relative in relative_files:
        parts = PurePosixPath(relative).parts
        parent_key: tuple[str, ...] = ()
        for name in parts[:-1]:
            key = parent_key + (name,)
            if key not in directories:
                directories[key] = ET.SubElement(
                    directories[parent_key],
                    f"{{{WIX_NS}}}Directory",
                    {"Id": _id("D_", "/".join(key)), "Name": name},
                )
            parent_key = key

        component_id = _id("C_", relative)
        component = ET.SubElement(
            directories[parent_key],
            f"{{{WIX_NS}}}Component",
            {
                "Id": component_id,
                "Guid": "{" + str(uuid.uuid5(COMPONENT_NAMESPACE, relative)).upper() + "}",
            },
        )
        ET.SubElement(
            component,
            f"{{{WIX_NS}}}File",
            {
                "Id": _id("F_", relative),
                "Source": str((payload / Path(*parts)).resolve(strict=True)),
                "KeyPath": "yes",
            },
        )
        ET.SubElement(group, f"{{{WIX_NS}}}ComponentRef", {"Id": component_id})

    ET.indent(wix, space="  ")
    ET.ElementTree(wix).write(destination, encoding="utf-8", xml_declaration=True)


def _find_wix(configured: Path | None) -> str:
    candidates = []
    if configured is not None:
        candidates.append(str(configured))
    if os.environ.get("WIX"):
        candidates.append(os.environ["WIX"])
    for executable in ("wix.exe", "wix"):
        found = shutil.which(executable)
        if found:
            candidates.append(found)
    for value in candidates:
        if value and Path(value).is_file():
            return value
    raise RuntimeError("wix was not found; install the modern WiX Toolset or set WIX")


def build_msi(
    payload: Path,
    out: Path,
    *,
    version: str,
    release_public_key: str,
    wix: Path | None = None,
) -> Path:
    payload = payload.resolve(strict=True)
    manifest, canonical = read_manifest(payload)
    if manifest.get("platform") != "windows" or manifest.get("arch") != "x86_64":
        raise RuntimeError("MSI payload must be windows/x86_64")
    if manifest.get("version") != version:
        raise RuntimeError("MSI version does not match the finalized payload manifest")
    verify_signature(
        key_id=str(manifest["key_id"]),
        canonical_json=canonical,
        signature_path=payload / SIGNATURE_NAME,
        public_key=release_public_key,
    )
    verify_manifest_payload(payload, canonical)

    expected = {str(row["path"]) for row in manifest["files"]}
    expected.update((MANIFEST_NAME, SIGNATURE_NAME))
    actual = set(discover_payload_files(payload, exclude_metadata=False))
    if actual != expected:
        raise PayloadManifestError(
            "finalized payload file set differs from signed manifest plus its sidecars: "
            f"missing={sorted(expected - actual)}, added={sorted(actual - expected)}"
        )
    relative_files = sorted(actual)
    complete_snapshot = snapshot_payload(payload, relative_files)

    out.mkdir(parents=True, exist_ok=True)
    artifact = out / f"Chudware-{version}-x86_64.msi"
    if artifact.exists():
        raise RuntimeError(f"refusing to replace existing MSI: {artifact}")
    generated = out / "chudware-payload.generated.wxs"
    write_payload_authoring(payload, relative_files, generated)
    command = [
        _find_wix(wix),
        "build",
        str(WXS),
        str(generated),
        "-arch",
        "x64",
        "-d",
        f"ProductCode={product_code(version)}",
        "-d",
        f"ProductVersion={product_version(version)}",
        "-o",
        str(artifact),
    ]
    subprocess.run(command, cwd=REPO, check=True)
    if not artifact.is_file():
        raise RuntimeError(f"WiX succeeded but did not produce {artifact}")
    try:
        verify_snapshot(
            payload, complete_snapshot, require_exact_files=True, exclude_metadata=False
        )
    except PayloadManifestError:
        artifact.unlink(missing_ok=True)
        raise
    sign_installer(artifact)
    write_artifact_signing_request(
        artifact, str(manifest["key_id"]), version, "windows", "x86_64"
    )
    return artifact


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build an MSI from a finalized signed Chudware Windows payload."
    )
    parser.add_argument("--payload", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--release-public-key", required=True)
    parser.add_argument("--wix", type=Path)
    args = parser.parse_args(argv)
    try:
        artifact = build_msi(
            args.payload,
            args.out,
            version=args.version,
            release_public_key=args.release_public_key,
            wix=args.wix,
        )
    except (OSError, RuntimeError, PayloadManifestError, subprocess.CalledProcessError) as exc:
        print(f"build_msi: error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({
        "msi": str(artifact),
        "product_code": product_code(args.version),
        "artifact_signing_request": str(
            artifact.with_name(artifact.name + ".artifact-signing-request.bin")
        ),
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
