#!/usr/bin/env python3
"""Check or provision stock PrusaSlicer; never download vendor profiles into repo."""
from __future__ import annotations

import argparse
import json
import platform
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from services.manufacturing.slicer.contract import SliceRefusal
from services.manufacturing.slicer.prusaslicer import discover, install_hint


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--check', action='store_true', help='Print discovery JSON without installing')
    mode.add_argument('--install', action='store_true', help='Install user flatpak on Linux; give download instructions elsewhere')
    args = parser.parse_args()
    if args.install:
        if platform.system() != 'Linux':
            print(install_hint())
            return 0
        flatpak = shutil.which('flatpak')
        if flatpak is None:
            print('Install your distribution flatpak package first, then rerun --install.', file=sys.stderr)
            return 1
        for cmd in ([flatpak, 'remote-add', '--user', '--if-not-exists', 'flathub',
                     'https://dl.flathub.org/repo/flathub.flatpakrepo'],
                    [flatpak, 'install', '--user', '--noninteractive', '-y', 'flathub', 'com.prusa3d.PrusaSlicer']):
            result = subprocess.run(cmd, check=False)
            if result.returncode:
                return result.returncode
    result = discover()
    print(json.dumps(result.to_dict(), indent=2))
    return 1 if isinstance(result, SliceRefusal) else 0


if __name__ == '__main__':
    raise SystemExit(main())
