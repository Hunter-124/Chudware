#!/usr/bin/env python3
"""Provision the Apache-2.0 Brush v0.3.0 release, or its pinned editable source.

No dependency installation or compilation is implicit. Install pycolmap into the
services Python environment separately. OpenMVS is never downloaded here.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from services.vision.reveng_provision import (
    BRUSH_SHA256, BRUSH_SOURCE, BRUSH_URL, BRUSH_VERSION, brush_install_dir,
)

BRUSH_REPOSITORY = "https://github.com/ArthurBrussee/brush.git"


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def extract_release(archive: Path, destination: Path) -> Path:
    """Extract only regular files/directories; never follow archive links."""
    with tarfile.open(archive, "r:xz") as bundle:
        members = bundle.getmembers()
        seen = set()
        total = 0
        for member in members:
            name = PurePosixPath(member.name)
            if name.is_absolute() or ".." in name.parts or "\\" in member.name:
                raise ValueError(f"Unsafe archive path: {member.name}")
            if not (member.isfile() or member.isdir()):
                raise ValueError(f"Archive links/devices are forbidden: {member.name}")
            if name in seen:
                raise ValueError(f"Duplicate archive path: {member.name}")
            seen.add(name)
            total += member.size
            if total > 4 * 1024 ** 3:
                raise ValueError("Release exceeds the 4 GiB extraction limit")
        for member in members:
            target = destination.joinpath(*PurePosixPath(member.name).parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            source = bundle.extractfile(member)
            if source is None:
                raise ValueError(f"Missing archive member: {member.name}")
            with source, target.open("xb") as output:
                shutil.copyfileobj(source, output)
            target.chmod(0o755 if member.mode & 0o111 else 0o644)
    binaries = list(destination.rglob("brush_app"))
    if len(binaries) != 1 or not binaries[0].is_file():
        raise ValueError("Release must contain exactly one regular brush_app executable")
    executable = binaries[0]
    with executable.open("rb") as stream:
        if stream.read(4) != b"\x7fELF":
            raise ValueError("Release brush_app is not an ELF executable")
    executable.chmod(0o755)
    return executable


def install() -> Path:
    if platform.system() != "Linux" or platform.machine().lower() not in ("x86_64", "amd64"):
        raise ValueError("The pinned binary is Linux x86_64 only; use --source-checkout PATH on other hosts.")
    target = brush_install_dir()
    target.parent.mkdir(parents=True, exist_ok=True)
    receipt_path = target / "install.json"
    if target.exists():
        try:
            receipt = json.loads(receipt_path.read_text())
            executable = target / "brush_app"
            if (receipt["archive_sha256"] == BRUSH_SHA256
                    and receipt["source_commit"] == BRUSH_SOURCE
                    and receipt["executable_sha256"] == digest(executable)
                    and os.access(executable, os.X_OK)):
                return executable
        except (OSError, ValueError, KeyError):
            pass
        raise ValueError(f"Existing install is not a verified {BRUSH_VERSION} release: {target}. Move it aside before reinstalling.")
    with tempfile.TemporaryDirectory(prefix=".brush-install-", dir=target.parent) as work:
        work = Path(work)
        archive = work / "release.tar.xz"
        request = urllib.request.Request(BRUSH_URL, headers={"User-Agent": "Chudware-Brush-Provisioner/1"})
        checksum = hashlib.sha256()
        size = 0
        with urllib.request.urlopen(request, timeout=60) as response, archive.open("xb") as output:
            while chunk := response.read(1024 * 1024):
                size += len(chunk)
                if size > 2 * 1024 ** 3:
                    raise ValueError("Release exceeds the 2 GiB download limit")
                checksum.update(chunk)
                output.write(chunk)
        if checksum.hexdigest() != BRUSH_SHA256:
            raise ValueError(f"Brush release SHA256 mismatch: expected {BRUSH_SHA256}, received {checksum.hexdigest()}")
        extracted = work / "extracted"
        extracted.mkdir()
        executable = extract_release(archive, extracted)
        # Upstream packages may wrap the app and resources in one directory.
        payload = executable.parent
        receipt = {"version": BRUSH_VERSION, "license": "Apache-2.0",
                   "source_repository": BRUSH_REPOSITORY, "source_commit": BRUSH_SOURCE,
                   "archive_url": BRUSH_URL, "archive_sha256": BRUSH_SHA256,
                   "executable_sha256": digest(executable)}
        (payload / "install.json").write_text(json.dumps(receipt, indent=2) + "\n")
        payload.rename(target)
    return target / "brush_app"


def checkout_source(destination: Path) -> Path:
    """Create a new checkout without ever altering an existing user's tree."""
    destination = destination.expanduser().resolve()
    if destination.exists():
        raise ValueError(f"Source destination already exists; not modifying it: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".brush-source-", dir=destination.parent) as work:
        source = Path(work) / "source"
        subprocess.run(["git", "clone", "--no-checkout", BRUSH_REPOSITORY, str(source)], check=True)
        subprocess.run(["git", "-C", str(source), "checkout", "--detach", BRUSH_SOURCE], check=True)
        actual = subprocess.run(["git", "-C", str(source), "rev-parse", "HEAD"],
                                check=True, capture_output=True, text=True).stdout.strip()
        if actual != BRUSH_SOURCE:
            raise ValueError(f"Unexpected source commit: {actual}")
        source.rename(destination)
    return destination


def main() -> int:
    parser = argparse.ArgumentParser(
        description=f"Install SHA256-verified Brush {BRUSH_VERSION} (Apache-2.0) locally. No build or pip install.",
        epilog=f"Source: {BRUSH_REPOSITORY} @ {BRUSH_SOURCE}. BRUSH_BIN overrides runtime discovery; XDG_DATA_HOME controls the managed install. SfM requires pycolmap in the services runtime.")
    parser.add_argument("--source-checkout", type=Path, metavar="PATH",
                        help="Instead of installing the binary, clone the exact pinned source into a NEW directory for local modification (no build).")
    args = parser.parse_args()
    try:
        if args.source_checkout is not None:
            print(f"Pinned Brush source: {checkout_source(args.source_checkout)}")
        else:
            print(f"Verified Brush executable: {install()}")
            if os.environ.get("BRUSH_BIN"):
                print("Note: BRUSH_BIN is set and remains the authoritative runtime override.")
    except (OSError, ValueError, subprocess.CalledProcessError, tarfile.TarError) as exc:
        print(f"Brush provisioning failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
