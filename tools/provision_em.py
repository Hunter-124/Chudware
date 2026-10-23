"""Provision openEMS (FDTD EM solver) for services.sim.em.

openEMS is GPLv3 and is driven **only** via its Python modules (``import openEMS`` /
``import CSXCAD``) inside the Python sim-worker process — never linked into the C++
core (contracts/license_debt.md). There is no PyPI wheel; the official install is a
source build of CSXCAD + openEMS + their Python bindings.

Usage:
    python -m tools.provision_em            # report what's detected
    python -m tools.provision_em --install  # source-build into a user prefix + pip install

Install layout (``--install``):
    * prefix:  $CHUDWARE_OPENEMS or ~/.local/opt/openEMS (Linux/macOS)
    * source:  ~/.cache/chudware/openEMS-Project (git clone --recursive)
    * python:  openEMS's own venv under <prefix>/venv is preferred for detection;
               ``--install`` also tries to pip-install into mcp-venv / runtime venv

Windows: openEMS ships prebuilt MSVC packages (see https://docs.openems.de/python/install.html).
``--install`` on Windows prints the official recipe and does not attempt a full MSVC
source build (not reliably scriptable without VS Build Tools). Detection still works
once the modules are importable.

Manual options (all auto-detected afterwards):
    * build with ./update_openEMS.sh ~/opt/openEMS --python and pip-install the modules
    * set CHUDWARE_OPENEMS to the install prefix
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

OPENEMS_REPO = "https://github.com/thliebig/openEMS-Project.git"
_INSTALL_HINT = (
    "no PyPI wheel — build from source: "
    "https://docs.openems.de/install (Fedora: dnf install cmake gcc-c++ "
    "tinyxml-devel hdf5-devel vtk-devel boost-devel CGAL-devel fparser-devel "
    "python3-devel python3-cython, then "
    "`python -m tools.provision_em --install`), "
    "or use the openSUSE/Ubuntu packages; see docs/reference/EM_SIMULATION.md"
)

_FEDORA_DEPS = (
    "cmake", "gcc-c++", "tinyxml-devel", "hdf5-devel", "vtk-devel",
    "boost-devel", "CGAL-devel", "fparser-devel", "git",
    "python3-devel", "python3-pip", "python3-cython",
)


def _default_prefix() -> Path:
    env = os.environ.get("CHUDWARE_OPENEMS") or os.environ.get("OPENEMS_PREFIX")
    if env:
        return Path(os.path.expanduser(os.path.expandvars(env)))
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
        return base / "Programs" / "openEMS"
    return Path.home() / ".local" / "opt" / "openEMS"


def _src_dir() -> Path:
    cache = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(cache) / "chudware" / "openEMS-Project"


def _candidate_pythons() -> list[Path]:
    """Interpreters we should try importing / installing openEMS into, preferred first."""
    out: list[Path] = []
    seen: set[str] = set()
    win = os.name == "nt"

    def add(p: Optional[Path | str]) -> None:
        if not p:
            return
        path = Path(p)
        s = str(path).replace("\\", "/")
        # Skip cross-platform path layouts so a Linux probe does not end on a
        # missing Scripts/python.exe (and vice versa).
        if win:
            if s.endswith("/bin/python") or s.endswith("/bin/python3"):
                return
        else:
            if "Scripts/python" in s or s.endswith("python.exe"):
                return
        try:
            key = str(path.resolve()) if path.exists() else str(path)
        except OSError:
            key = str(path)
        if key in seen:
            return
        seen.add(key)
        out.append(path)

    add(os.environ.get("CHUDWARE_PYTHON"))

    # openEMS's own install venv (created by update_openEMS.sh --python).
    add(_default_prefix() / "venv" / ("Scripts" if win else "bin") / ("python.exe" if win else "python"))

    home = Path.home()
    if win:
        appdata = Path(os.environ.get("APPDATA") or (home / "AppData" / "Roaming"))
        add(appdata / "Chudware" / "mcp-venv" / "Scripts" / "python.exe")
        add(appdata / "Chudware" / "runtime" / "venv" / "Scripts" / "python.exe")
    else:
        add(home / ".local" / "share" / "Chudware" / "mcp-venv" / "bin" / "python")
        add(home / ".local" / "share" / "Chudware" / "runtime" / "venv" / "bin" / "python")

    here = Path(__file__).resolve().parent.parent
    if win:
        for rel in (
            "runtime/venv/Scripts/python.exe",
            ".venv/Scripts/python.exe",
            "venv/Scripts/python.exe",
        ):
            add(here / rel)
    else:
        for rel in (
            "runtime/venv/bin/python",
            ".venv/bin/python",
            "venv/bin/python",
        ):
            add(here / rel)

    add(sys.executable)
    add(shutil.which("python3") or shutil.which("python"))
    return out


def _probe_env() -> dict:
    """Env for import probes — must find the shared libs under the openEMS prefix."""
    env = {**os.environ, "PYTHONNOUSERSITE": "1"}
    prefix = _default_prefix()
    lib = prefix / "lib"
    if lib.is_dir():
        ld = env.get("LD_LIBRARY_PATH", "")
        env["LD_LIBRARY_PATH"] = f"{lib}{os.pathsep}{ld}" if ld else str(lib)
        dyld = env.get("DYLD_LIBRARY_PATH", "")
        env["DYLD_LIBRARY_PATH"] = f"{lib}{os.pathsep}{dyld}" if dyld else str(lib)
        # Windows: PATH for DLLs
        if os.name == "nt":
            env["PATH"] = f"{lib}{os.pathsep}{prefix / 'bin'}{os.pathsep}{env.get('PATH', '')}"
    return env


def _import_probe(py: Path) -> tuple[bool, Optional[str], str]:
    """Return (ok, version, detail) for ``import openEMS, CSXCAD`` under *py*."""
    if not py or not Path(py).exists():
        return False, None, f"interpreter missing: {py}"
    code = (
        "import json,sys\n"
        "try:\n"
        "    import openEMS, CSXCAD\n"
        "    ver = getattr(openEMS, '__version__', None)\n"
        "    print(json.dumps({'ok': True, 'version': ver}))\n"
        "except Exception as e:\n"
        "    print(json.dumps({'ok': False, 'detail': f'{type(e).__name__}: {e}'}))\n"
        "    sys.exit(0)\n"
    )
    try:
        r = subprocess.run(
            [str(py), "-c", code],
            capture_output=True, text=True, timeout=60,
            env=_probe_env(),
        )
    except Exception as exc:  # noqa: BLE001
        return False, None, f"probe failed: {exc}"
    raw = (r.stdout or "").strip().splitlines()
    if not raw:
        err = (r.stderr or "").strip()[:200]
        return False, None, err or f"empty probe output (rc={r.returncode})"
    try:
        data = json.loads(raw[-1])
    except json.JSONDecodeError:
        return False, None, raw[-1][:200]
    if data.get("ok"):
        return True, data.get("version"), "openEMS + CSXCAD python modules importable"
    return False, None, data.get("detail") or "import failed"


def find_openems() -> dict:
    """Probe candidate interpreters; return status dict (never raises)."""
    last_detail = "no candidate python"
    for py in _candidate_pythons():
        if not Path(py).exists():
            continue
        ok, ver, detail = _import_probe(py)
        if ok:
            return {
                "tool": "openems",
                "installed": True,
                "python": str(py),
                "version": ver,
                "prefix": str(_default_prefix()) if _default_prefix().exists() else None,
                "detail": detail,
                "hint": None,
            }
        last_detail = f"{py}: {detail}"
    existing = [str(p) for p in _candidate_pythons() if Path(p).exists()]
    if not existing:
        last_detail = "no candidate python interpreters found on this host"
    return {
        "tool": "openems",
        "installed": False,
        "python": None,
        "version": None,
        "prefix": str(_default_prefix()) if _default_prefix().exists() else None,
        "detail": last_detail,
        "hint": _INSTALL_HINT,
    }


def status() -> dict:
    """Structured availability report (never raises)."""
    return find_openems()


def _have_cmd(name: str) -> bool:
    return shutil.which(name) is not None


def _try_install_build_deps() -> None:
    """Best-effort install of Fedora/RHEL/Debian build deps. Non-fatal if it fails."""
    if platform.system().lower() != "linux":
        return
    if not _have_cmd("dnf") and not _have_cmd("apt-get"):
        return
    missing_markers = []
    if not Path("/usr/include/tinyxml.h").exists() and not Path("/usr/include/tinyxml/tinyxml.h").exists():
        missing_markers.append("tinyxml")
    if not Path("/usr/include/CGAL").exists():
        missing_markers.append("CGAL")
    if not missing_markers and _have_cmd("cmake") and _have_cmd("g++"):
        return

    if _have_cmd("dnf"):
        cmd = ["sudo", "dnf", "install", "-y", *_FEDORA_DEPS]
        print(f"[provision] ensuring Fedora build deps via dnf ({', '.join(missing_markers) or 'refresh'})...")
        try:
            subprocess.run(cmd, timeout=1800, check=False)
        except Exception as exc:  # noqa: BLE001
            print(f"[provision] dnf deps step skipped/failed: {exc}")
    elif _have_cmd("apt-get"):
        pkgs = [
            "build-essential", "cmake", "git", "libtinyxml-dev", "libhdf5-dev",
            "libvtk9-dev", "libboost-all-dev", "libcgal-dev", "python3-dev",
            "python3-pip", "cython3",
        ]
        print("[provision] ensuring Debian/Ubuntu build deps via apt-get...")
        try:
            subprocess.run(["sudo", "apt-get", "update", "-qq"], timeout=600, check=False)
            subprocess.run(
                ["sudo", "DEBIAN_FRONTEND=noninteractive", "apt-get", "install", "-y", *pkgs],
                timeout=1800, check=False,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"[provision] apt deps step skipped/failed: {exc}")


def _clone_or_update_src(src: Path) -> bool:
    src.parent.mkdir(parents=True, exist_ok=True)
    if (src / ".git").is_dir():
        print(f"[provision] updating openEMS-Project at {src} ...")
        r = subprocess.run(
            ["git", "-C", str(src), "pull", "--ff-only"],
            text=True, timeout=600,
        )
        subprocess.run(
            ["git", "-C", str(src), "submodule", "update", "--init", "--recursive"],
            text=True, timeout=1800, check=False,
        )
        return r.returncode == 0 or (src / "update_openEMS.sh").exists()

    print(f"[provision] cloning {OPENEMS_REPO} (recursive) -> {src} ...")
    r = subprocess.run(
        ["git", "clone", "--recursive", OPENEMS_REPO, str(src)],
        text=True, timeout=3600,
    )
    if r.returncode != 0:
        print("[provision] git clone failed (network / disk?).")
        return False
    return True


def _target_python() -> Path:
    """Prefer a real venv interpreter for the pip install step."""
    for py in _candidate_pythons():
        if not py.exists():
            continue
        s = str(py)
        if "mcp-venv" in s or "runtime" in s or ".venv" in s or "/venv/" in s:
            return py
    for py in _candidate_pythons():
        if py.exists():
            return py
    return Path(sys.executable)


def _run_update_script(src: Path, prefix: Path) -> bool:
    script = src / "update_openEMS.sh"
    if not script.exists():
        print(f"[provision] missing {script}")
        return False
    prefix.mkdir(parents=True, exist_ok=True)
    # --python builds the Python modules into <prefix>/venv by default.
    cmd = ["bash", str(script), str(prefix), "--python"]
    print(f"[provision] building openEMS into {prefix} (this can take 10–30+ min)...")
    print(f"[provision] $ {' '.join(cmd)}")
    env = os.environ.copy()
    py = _target_python()
    env.setdefault("PYTHON", str(py))
    r = subprocess.run(cmd, cwd=str(src), env=env, timeout=7200)
    if r.returncode != 0:
        print(f"[provision] update_openEMS.sh failed (rc={r.returncode})")
        return False
    return True


def _pip_install_modules(src: Path, prefix: Path, py: Path) -> bool:
    """pip install the CSXCAD + openEMS python packages into *py*."""
    csx = src / "CSXCAD" / "python"
    oem = src / "openEMS" / "python"
    if not csx.is_dir() or not oem.is_dir():
        print(f"[provision] python package dirs missing under {src} — cannot pip install")
        return False

    print(f"[provision] ensuring setuptools/wheel/Cython/numpy in {py} ...")
    subprocess.run(
        [str(py), "-m", "pip", "install", "-q", "setuptools", "wheel", "Cython", "numpy"],
        timeout=600, check=False,
    )

    env = os.environ.copy()
    lib = prefix / "lib"
    inc = prefix / "include"
    # setup.py / pyproject require these exact names.
    env["CSXCAD_INSTALL_PATH"] = str(prefix)
    env["OPENEMS_INSTALL_PATH"] = str(prefix)
    env["CSXCAD_INSTALL"] = str(prefix)
    env["OPENEMS_INSTALL"] = str(prefix)
    ld = env.get("LD_LIBRARY_PATH", "")
    env["LD_LIBRARY_PATH"] = f"{lib}{os.pathsep}{ld}" if ld else str(lib)
    dyld = env.get("DYLD_LIBRARY_PATH", "")
    env["DYLD_LIBRARY_PATH"] = f"{lib}{os.pathsep}{dyld}" if dyld else str(lib)
    env["CFLAGS"] = f"-I{inc} " + env.get("CFLAGS", "")
    env["LDFLAGS"] = f"-L{lib} " + env.get("LDFLAGS", "")
    env["CPPFLAGS"] = f"-I{inc} " + env.get("CPPFLAGS", "")

    for pkg_dir, label in ((csx, "CSXCAD"), (oem, "openEMS")):
        print(f"[provision] pip-installing {label} into {py} from {pkg_dir} ...")
        r = subprocess.run(
            [str(py), "-m", "pip", "install", "--no-build-isolation", "."],
            cwd=str(pkg_dir), env=env, timeout=1800,
        )
        if r.returncode != 0:
            print(f"[provision] pip install . failed for {label}; trying setup.py install ...")
            r2 = subprocess.run(
                [str(py), "setup.py", "build_ext", f"-I{inc}", f"-L{lib}"],
                cwd=str(pkg_dir), env=env, timeout=1800, check=False,
            )
            r3 = subprocess.run(
                [str(py), "setup.py", "install"],
                cwd=str(pkg_dir), env=env, timeout=1800, check=False,
            )
            if r2.returncode != 0 or r3.returncode != 0:
                print(f"[provision] {label} python install failed")
                return False
    return True


def install_linux() -> bool:
    """Source-build openEMS + ensure modules are importable."""
    if not _have_cmd("git") or not _have_cmd("cmake") or not (_have_cmd("g++") or _have_cmd("c++")):
        print("[provision] need git + cmake + g++ on PATH to build openEMS from source.")
        print(f"            {_INSTALL_HINT}")
        return False

    _try_install_build_deps()

    src = _src_dir()
    prefix = _default_prefix()
    py = _target_python()
    print(f"[provision] target python: {py}")
    print(f"[provision] install prefix: {prefix}")
    print(f"[provision] source dir: {src}")

    if not _clone_or_update_src(src):
        return False

    # If the openEMS venv already has the modules, skip the heavy rebuild (idempotent).
    oem_py = prefix / "venv" / "bin" / "python"
    if oem_py.exists():
        ok, ver, _ = _import_probe(oem_py)
        if ok:
            print(f"[provision] openEMS already built at {prefix} (version={ver})")
            # Still try to mirror into the preferred runtime venv.
            if Path(py).resolve() != oem_py.resolve():
                _pip_install_modules(src, prefix, py)
            st = find_openems()
            if st["installed"]:
                print(f"[provision] openEMS OK via {st['python']} (version={st['version']})")
                return True

    t0 = time.perf_counter()
    if not _run_update_script(src, prefix):
        print("[provision] build script failed — attempting module install anyway if sources exist")
    print(f"[provision] update_openEMS wall-time: {time.perf_counter() - t0:.1f}s")

    # Prefer detecting the built-in venv first; also try the target runtime venv.
    st = find_openems()
    if st["installed"]:
        print(f"[provision] openEMS OK via {st['python']} (version={st['version']})")
        # Best-effort: also install into mcp-venv when different.
        if Path(py).resolve() != Path(st["python"]).resolve():
            _pip_install_modules(src, prefix, py)
            st2 = find_openems()
            if st2["installed"]:
                print(f"[provision] also available via {st2['python']}")
        return True

    # Build succeeded but modules only in prefix/venv which we somehow missed — try pip.
    if not _pip_install_modules(src, prefix, py):
        if Path(py).resolve() != Path(sys.executable).resolve():
            print(f"[provision] retrying pip install into {sys.executable} ...")
            if not _pip_install_modules(src, prefix, Path(sys.executable)):
                # Last resort: accept prefix/venv if importable after LD path setup.
                st = find_openems()
                if st["installed"]:
                    print(f"[provision] openEMS OK via {st['python']} (version={st['version']})")
                    return True
                return False
        else:
            st = find_openems()
            if st["installed"]:
                print(f"[provision] openEMS OK via {st['python']} (version={st['version']})")
                return True
            return False

    st = find_openems()
    if st["installed"]:
        print(f"[provision] openEMS OK via {st['python']} (version={st['version']})")
        return True
    print(f"[provision] modules still not importable: {st['detail']}")
    return False


def install_windows() -> bool:
    """Windows cannot reliably MSVC-build unattended; print the official recipe."""
    st = find_openems()
    if st["installed"]:
        print(f"[provision] openEMS already available via {st['python']}")
        return True
    print("[provision] openEMS on Windows requires the prebuilt MSVC package + matching wheels.")
    print("            Official recipe (https://docs.openems.de/python/install.html):")
    print("              1. Download the latest openEMS_win MSVC build and unpack to C:\\opt\\openEMS")
    print("              2. pip install the CSXCAD/openEMS wheels under C:\\opt\\openEMS\\python")
    print("                 matching your venv's Python (cp310/cp311).")
    print("            This provisioner will detect the modules once they import.")
    print(f"            hint: {_INSTALL_HINT}")
    return False


def install() -> bool:
    if platform.system().lower() == "windows" or os.name == "nt":
        return install_windows()
    return install_linux()


def report() -> int:
    """Print detection result; exit 0 if openEMS modules import, else 1."""
    st = find_openems()
    if st["installed"]:
        print(
            f"[provision] openEMS detected: python={st['python']}  "
            f"version={st['version']}  detail={st['detail']}"
        )
        return 0
    print("[provision] No openEMS/CSXCAD python modules importable.")
    if os.name == "posix":
        print("            Run with --install to source-build into ~/.local/opt/openEMS")
        print("            and pip-install into the mcp/runtime venv (needs build deps).")
    else:
        print("            Install the MSVC openEMS_win package + wheels (see --install).")
    print(f"            last probe: {st['detail']}")
    print(f"            hint: {st['hint']}")
    return 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Provision/detect openEMS (FDTD) for the EM sim bridge."
    )
    ap.add_argument(
        "--install",
        action="store_true",
        help="source-build openEMS + pip-install modules (Linux/macOS); print Windows recipe",
    )
    ap.add_argument(
        "--json",
        action="store_true",
        help="emit status() as JSON (exit code still reflects presence)",
    )
    args = ap.parse_args(argv)

    if args.install:
        st = find_openems()
        if st["installed"]:
            print("[provision] openEMS already available — nothing to do.")
        else:
            install()

    if args.json:
        st = find_openems()
        print(json.dumps(st, indent=2))
        return 0 if st["installed"] else 1
    return report()


if __name__ == "__main__":
    sys.exit(main())
