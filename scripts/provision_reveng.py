#!/usr/bin/env python3
"""Provision the Apache-2.0 Brush v0.3.0 release (or its pinned editable source) and the
AGPL-3.0 OpenMVS v2.4.0 release. Both are SHA-256-pinned and run as subprocesses only.

The download/verify logic lives in ``services/vision/reveng_install.py``, which ships with the
app and backs the in-app "Install" button; this script is the developer CLI over it.

No dependency installation or compilation is implicit. Install pycolmap into the
services Python environment separately.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from services.vision.reveng_install import BRUSH_REPOSITORY, install_brush, install_openmvs
from services.vision.reveng_provision import BRUSH_SOURCE, BRUSH_VERSION, OPENMVS_VERSION


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
        description=(f"Install SHA256-verified Brush {BRUSH_VERSION} (Apache-2.0) and OpenMVS {OPENMVS_VERSION} "
                     "(AGPL-3.0, subprocess only) locally. No build or pip install."),
        epilog=(f"Source: {BRUSH_REPOSITORY} @ {BRUSH_SOURCE}. BRUSH_BIN / OPENMVS_BIN override runtime discovery; "
                "XDG_DATA_HOME controls the managed installs. SfM requires pycolmap in the services runtime."))
    parser.add_argument("--only", choices=("brush", "openmvs"),
                        help="Install just one tool (default: both).")
    parser.add_argument("--source-checkout", type=Path, metavar="PATH",
                        help="Instead of installing the Brush binary, clone the exact pinned source into a NEW directory for local modification (no build).")
    args = parser.parse_args()
    if args.source_checkout is not None:
        try:
            print(f"Pinned Brush source: {checkout_source(args.source_checkout)}")
        except (OSError, ValueError, subprocess.CalledProcessError) as exc:
            print(f"Brush provisioning failed: {exc}", file=sys.stderr)
            return 1
        return 0
    status = 0
    if args.only in (None, "brush"):
        try:
            print(f"Verified Brush executable: {install_brush()}")
            if os.environ.get("BRUSH_BIN"):
                print("Note: BRUSH_BIN is set and remains the authoritative runtime override.")
        except (OSError, ValueError, subprocess.CalledProcessError, tarfile.TarError) as exc:
            print(f"Brush provisioning failed: {exc}", file=sys.stderr)
            status = 1
    if args.only in (None, "openmvs"):
        try:
            print(f"Verified OpenMVS tools: {install_openmvs()}")
            if os.environ.get("OPENMVS_BIN"):
                print("Note: OPENMVS_BIN is set and remains the authoritative runtime override.")
        except (OSError, ValueError, zipfile.BadZipFile) as exc:
            print(f"OpenMVS provisioning failed: {exc}", file=sys.stderr)
            status = 1
    return status


if __name__ == "__main__":
    raise SystemExit(main())
