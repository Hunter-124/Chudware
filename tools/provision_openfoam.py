"""Provision a working OpenFOAM for the CFD bridge (engine.sim.openfoam).

The bridge can drive OpenFOAM two ways (see engine/sim/foam_runner.py):

  * NATIVE Windows  — a blueCFD-Core (ESI/MinGW) build.  Preferred (no WSL hop), but its
    installer is an interactive/NSIS download we don't fetch automatically here; this script
    only *detects* a native build and prints where to drop one in.
  * WSL2            — OpenFOAM installed inside an Ubuntu WSL distro via apt.  Fully scriptable,
    so this is what ``--install`` provisions.

Usage:
    python -m tools.provision_openfoam            # report what's detected
    python -m tools.provision_openfoam --install  # apt-install OpenFOAM (ESI) into WSL

Native install (manual, user-preferred):
    Download blueCFD-Core from https://bluecfd.github.io/ , install it, then either
    set  %AICAD_OPENFOAM%  to its root, or copy it to  third_party/openfoam/  in this repo.
    Re-run this script to confirm detection.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys

# ESI release the bridge case templates target (simpleFoam, snappyHexMesh layer syntax).
OF_VERSION = "openfoam2412"


def _wsl_available() -> bool:
    return shutil.which("wsl") is not None


def _wsl(cmd: str, timeout: float = 1800) -> subprocess.CompletedProcess:
    return subprocess.run(["wsl", "-e", "bash", "-lc", cmd],
                          text=True, timeout=timeout)


def install_wsl() -> bool:
    """Add the OpenFOAM.com apt repo and install the default ESI package into WSL."""
    if not _wsl_available():
        print("[provision] no `wsl` on PATH — cannot apt-install. Use native blueCFD instead.")
        return False
    print(f"[provision] installing {OF_VERSION}-default into WSL (apt, requires passwordless sudo)...")
    script = (
        "set -e; "
        "curl -s https://dl.openfoam.com/add-debian-repo.sh | sudo bash; "
        "sudo apt-get update -qq; "
        f"sudo DEBIAN_FRONTEND=noninteractive apt-get install -y {OF_VERSION}-default; "
        f"source /usr/lib/openfoam/{OF_VERSION}/etc/bashrc; "
        "which simpleFoam blockMesh snappyHexMesh"
    )
    r = _wsl(script)
    if r.returncode != 0:
        print("[provision] WSL install failed (check passwordless sudo / network).")
        return False
    print("[provision] WSL install OK.")
    return True


def report() -> int:
    """Print the detected runner; exit 0 if any OpenFOAM is reachable, else 1."""
    # Import lazily so the script runs even from a bare checkout.
    try:
        from engine.sim.foam_runner import get_runner, reset_cache
    except Exception as e:  # noqa: BLE001
        print(f"[provision] could not import foam_runner: {e}")
        return 1
    reset_cache()
    r = get_runner()
    if r is None:
        print("[provision] No OpenFOAM detected (neither native Windows nor WSL).")
        print("            Run with --install to apt-install into WSL, or install blueCFD-Core")
        print("            natively and set %AICAD_OPENFOAM% / drop it in third_party/openfoam/.")
        return 1
    print(f"[provision] OpenFOAM detected: kind={r.kind}  version={r.version()}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Provision/detect OpenFOAM for the CFD bridge.")
    ap.add_argument("--install", action="store_true",
                    help="apt-install OpenFOAM (ESI) into WSL if not already present")
    args = ap.parse_args(argv)

    if args.install:
        try:
            from engine.sim.foam_runner import get_runner, reset_cache
            reset_cache()
            if get_runner() is not None:
                print("[provision] OpenFOAM already available — nothing to do.")
                return report()
        except Exception:
            pass
        install_wsl()
    return report()


if __name__ == "__main__":
    sys.exit(main())
