"""Provision a working OpenFOAM for the CFD bridge (engine.sim.openfoam).

The bridge drives OpenFOAM three ways (see engine/sim/foam_runner.py) — always as
subprocesses, never linked (GPL isolation, contracts/license_debt.md):

  * LINUX (posix)   — any reachable install: a sourced environment, an openfoam.com
    tarball, a distro package, or a **user-space conda-forge env** (no root needed).
    ``--install`` provisions the conda-forge package via micromamba/mamba/conda when
    one is on PATH: ``micromamba create -n openfoam -c conda-forge openfoam``.
  * NATIVE Windows  — a blueCFD-Core (ESI/MinGW) build.  Its installer is an
    interactive/NSIS download we don't fetch automatically; this script only *detects*
    a native build and prints where to drop one in.
  * WSL2            — OpenFOAM installed inside an Ubuntu WSL distro via apt.  Fully
    scriptable, so ``--install`` provisions this on Windows.

Usage:
    python -m tools.provision_openfoam            # report what's detected
    python -m tools.provision_openfoam --install  # user-space conda env (Linux) / WSL apt (Windows)

Manual options (all detected automatically afterwards):
    Linux:   micromamba create -n openfoam -c conda-forge openfoam -y
             or unpack an openfoam.com tarball under ~/OpenFOAM/ (etc/bashrc detected),
             or set AICAD_OPENFOAM to the install root.  No sudo required.
    Windows: install blueCFD-Core (https://bluecfd.github.io/) and set %AICAD_OPENFOAM%,
             or copy it to third_party/openfoam/ in this repo.
"""

from __future__ import annotations

import argparse
import os
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


def install_linux() -> bool:
    """User-space install of the conda-forge OpenFOAM package (no root/sudo)."""
    mgr = next((m for m in ("micromamba", "mamba", "conda") if shutil.which(m)), None)
    if mgr is None:
        print("[provision] no micromamba/mamba/conda on PATH — cannot user-space install.")
        print("            Options (no sudo needed):")
        print("              * install micromamba, then re-run --install")
        print("              * unpack an openfoam.com tarball under ~/OpenFOAM/")
        print("              * distro package (Fedora COPR / Ubuntu apt) if you have root,")
        print("            then re-run this script to confirm detection.")
        return False
    print(f"[provision] creating user-space conda env 'openfoam' via {mgr} (conda-forge, ~1 GB)...")
    r = subprocess.run([mgr, "create", "-n", "openfoam", "-c", "conda-forge", "openfoam", "-y"],
                       text=True, timeout=3600)
    if r.returncode != 0:
        print(f"[provision] {mgr} install failed (network / disk?).")
        return False
    print("[provision] conda-forge OpenFOAM installed — the env's etc/bashrc is auto-detected.")
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
        if os.name == "posix":
            print("[provision] No OpenFOAM detected on this Linux host.")
            print("            Run with --install for a user-space conda-forge env (no sudo),")
            print("            or unpack an openfoam.com tarball under ~/OpenFOAM/,")
            print("            or set AICAD_OPENFOAM to an install root.")
        else:
            print("[provision] No OpenFOAM detected (neither native Windows nor WSL).")
            print("            Run with --install to apt-install into WSL, or install blueCFD-Core")
            print("            natively and set %AICAD_OPENFOAM% / drop it in third_party/openfoam/.")
        return 1
    print(f"[provision] OpenFOAM detected: kind={r.kind}  version={r.version()}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Provision/detect OpenFOAM for the CFD bridge.")
    ap.add_argument("--install", action="store_true",
                    help="user-space conda-forge env (Linux) or apt into WSL (Windows) "
                         "if not already present")
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
        install_linux() if os.name == "posix" else install_wsl()
    return report()


if __name__ == "__main__":
    sys.exit(main())
