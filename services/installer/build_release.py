"""Assemble a distributable Chudware release (payload + installer scripts).

The default command performs the complete Windows packaging pipeline: it stages
``<out>/Chudware/`` and invokes Inno Setup to produce the canonical installer.

The payload contains:
    chudware.exe            the native host (built separately via CMake)
    app/                    Python packages and requirements
    wheelhouse/             offline wheels [--skip-wheels to omit]
    install.ps1 / uninstall.ps1

``provision.py`` creates the managed venv on first run. No non-functional embedded-Python
option is advertised. ``--payload-only`` is available for inspecting the staged payload.

    python -m services.installer.build_release [--out dist] [--skip-wheels] [--zip]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent.parent
APP_PACKAGES = ("engine",)
# Canonical ``services.X`` packages used by the worker and packaged tools.
SERVICE_PACKAGES = ("agent", "mcp", "installer", "plm", "dag", "browser",
                    "deliverables", "ipc", "serving", "verify")
HOST_EXE = REPO / "host" / "build-win" / "bin" / "chudware.exe"
if not HOST_EXE.exists():
    HOST_EXE = REPO / "host" / "build" / "bin" / "Release" / "chudware.exe"
# Engine host daemon (core/hostd). MSVC builds of chudware-hostd are new — absence is a
# warning + skip, not a failure (the app falls back to legacy in-process engine mode).
HOSTD_EXE_CANDIDATES = (
    REPO / "build" / "windows-occt" / "bin" / "chudware-hostd.exe",
    REPO / "build" / "windows-core" / "bin" / "chudware-hostd.exe",
    REPO / "core" / "build" / "bin" / "Release" / "chudware-hostd.exe",
    REPO / "core" / "build" / "bin" / "chudware-hostd.exe",
)
VERSION_HEADER_CANDIDATES = (
    HOST_EXE.parent.parent / "generated" / "api" / "chudware_version.h",
    REPO / "host" / "build-win" / "generated" / "api" / "chudware_version.h",
    REPO / "host" / "build" / "generated" / "api" / "chudware_version.h",
)
ISS_SCRIPT = REPO / "packaging" / "windows" / "chudware.iss"
VERSION_RE = re.compile(r'^#define CHUDWARE_VERSION\s+"([^"]+)"$', re.MULTILINE)
_IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", "build", "samples", "test_*", ".ready.json")


def stage_app(dst: Path) -> None:
    app = dst / "app"
    app.mkdir(parents=True, exist_ok=True)
    for pkg in APP_PACKAGES:
        s = REPO / pkg
        if s.exists():
            shutil.copytree(s, app / pkg, dirs_exist_ok=True, ignore=_IGNORE)
    # services.X packages for the engine worker (PLM hub, dag, browser, …)
    services_src = REPO / "services"
    if services_src.is_dir():
        services_dst = app / "services"
        services_dst.mkdir(parents=True, exist_ok=True)
        init_py = services_src / "__init__.py"
        if init_py.exists():
            shutil.copy2(init_py, services_dst / "__init__.py")
        else:
            (services_dst / "__init__.py").write_text("", encoding="utf-8")
        # Loose top-level services/*.py modules (paths.py, kernel_exec.py, worker_main.py, …) --
        # SERVICE_PACKAGES below only covers subpackages; services.installer.provision imports
        # `services.paths` directly, so these single-file modules must ship too.
        for py in services_src.glob("*.py"):
            if py.name == "__init__.py" or py.name.startswith("test_"):
                continue
            shutil.copy2(py, services_dst / py.name)
        for name in SERVICE_PACKAGES:
            s = services_src / name
            if s.exists():
                shutil.copytree(s, services_dst / name, dirs_exist_ok=True, ignore=_IGNORE)
    # samples are handy to ship even though we ignore them above for other dirs
    sample_src = REPO / "engine" / "samples"
    if sample_src.exists():
        shutil.copytree(sample_src, app / "engine" / "samples", dirs_exist_ok=True, ignore=_IGNORE)
    shutil.copy2(REPO / "requirements.txt", app / "requirements.txt")


def stage_optional_payloads(dst: Path) -> dict:
    staged: dict[str, bool] = {}
    for name in ("llamacpp", "models"):
        src = REPO / name
        if src.exists():
            shutil.copytree(src, dst / name, dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            staged[name] = True
        else:
            staged[name] = False
    return staged


def stage_wheels(dst: Path) -> dict:
    wh = dst / "wheelhouse"
    wh.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, "-m", "pip", "download", "-r", str(REPO / "requirements.txt"),
           "-d", str(wh)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    n = len(list(wh.glob("*.whl"))) + len(list(wh.glob("*.tar.gz")))
    return {"ok": r.returncode == 0, "files": n,
            "tail": (r.stdout + r.stderr).splitlines()[-3:] if r.returncode else []}


def build(out: Path, skip_wheels: bool, make_zip: bool) -> dict[str, Any]:
    dst = out / "Chudware"
    if dst.exists():
        shutil.rmtree(dst)
    dst.mkdir(parents=True)

    manifest: dict = {"components": {}}

    if HOST_EXE.exists():
        shutil.copy2(HOST_EXE, dst / "chudware.exe")
        manifest["components"]["exe"] = True
    else:
        manifest["components"]["exe"] = False  # build it with: cmake --build host/build --config Release

    # chudware-hostd.exe (+ .pdb when present) — staged next to chudware.exe/libchudware.dll so
    # install.ps1 can stop/replace it and register_mcp can pin CHUDWARE_HOSTD_BIN at it.
    hostd = next((p for p in HOSTD_EXE_CANDIDATES if p.exists()), None)
    if hostd is not None:
        shutil.copy2(hostd, dst / "chudware-hostd.exe")
        pdb = hostd.with_suffix(".pdb")
        if pdb.exists():
            shutil.copy2(pdb, dst / "chudware-hostd.pdb")
        manifest["components"]["hostd"] = True
    else:
        print("warning: chudware-hostd.exe not found (cmake --build core\\build --config Release "
              "--target chudware-hostd) — payload ships without the engine daemon", file=sys.stderr)
        manifest["components"]["hostd"] = False

    # Brand + font assets, loaded by the GUI from <exedir>/assets — including logo_icon.png,
    # which IS the runtime GLFW window/taskbar icon. This read `REPO/"host"/"assets"` for its
    # whole life; that path has never existed (the assets live at the repo root), so the guard
    # below was always false and every Windows payload shipped with no assets/ at all — the
    # installed app fell back to a blank window icon and no wordmark.
    assets_src = REPO / "assets"
    if assets_src.exists():
        shutil.copytree(assets_src, dst / "assets", dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("Rubik-VF.ttf", "make_logo.py", "__pycache__"))
        manifest["components"]["assets"] = True
    else:
        print(f"warning: {assets_src} not found — payload ships without brand/font assets",
              file=sys.stderr)
        manifest["components"]["assets"] = False

    stage_app(dst)
    manifest["components"]["app"] = True
    manifest["components"].update(stage_optional_payloads(dst))

    shutil.copy2(REPO / "services" / "installer" / "install.ps1", dst / "install.ps1")
    shutil.copy2(REPO / "services" / "installer" / "uninstall.ps1", dst / "uninstall.ps1")
    shutil.copy2(REPO / "requirements.txt", dst / "requirements.txt")

    if skip_wheels:
        manifest["components"]["wheelhouse"] = "skipped"
    else:
        manifest["components"]["wheelhouse"] = stage_wheels(dst)

    # provision.py creates the managed venv on first run. The manifest therefore has no
    # embedded-Python component state.

    (dst / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    if make_zip:
        archive = shutil.make_archive(str(out / "Chudware-release"), "zip", root_dir=dst.parent,
                                      base_dir="Chudware")
        manifest["zip"] = archive

    manifest["dist"] = str(dst)
    return manifest

def read_built_version() -> str:
    """Read the exact CHUDWARE_VERSION compiled into the staged Windows binary."""
    for header in VERSION_HEADER_CANDIDATES:
        if not header.is_file():
            continue
        match = VERSION_RE.search(header.read_text(encoding="utf-8"))
        if match:
            return match.group(1)
        raise RuntimeError(f"generated version header is malformed: {header}")
    searched = ", ".join(str(path) for path in VERSION_HEADER_CANDIDATES)
    raise RuntimeError(
        "generated chudware_version.h not found; configure/build the Windows host first "
        f"(searched: {searched})"
    )


def find_tool(env_name: str, executable: str, candidates: tuple[Path, ...] = ()) -> str | None:
    configured = os.environ.get(env_name)
    if configured:
        return configured
    found = shutil.which(executable)
    if found:
        return found
    return next((str(path) for path in candidates if path.is_file()), None)


def sign_installer(installer: Path) -> None:
    identity = os.environ.get("CHUDWARE_SIGN_IDENTITY", "")
    notarize_profile = os.environ.get("CHUDWARE_NOTARIZE_PROFILE", "")
    if not identity:
        print("WARNING: unsigned build — SmartScreen/Gatekeeper will warn users", file=sys.stderr)
        return
    if notarize_profile:
        print("note: CHUDWARE_NOTARIZE_PROFILE is macOS-only and is ignored on Windows",
              file=sys.stderr)
    program_files_x86 = Path(os.environ.get("ProgramFiles(x86)", r"C:\\Program Files (x86)"))
    signtool = find_tool(
        "SIGNTOOL",
        "signtool.exe",
        tuple(program_files_x86.glob("Windows Kits/10/bin/*/x64/signtool.exe")),
    )
    if not signtool:
        raise RuntimeError(
            "CHUDWARE_SIGN_IDENTITY is set but signtool.exe was not found; install the "
            "Windows 10/11 SDK or set SIGNTOOL to its full path"
        )
    subprocess.run(
        [
            signtool, "sign", "/n", identity, "/fd", "SHA256",
            "/tr", "http://timestamp.digicert.com", "/td", "SHA256", str(installer),
        ],
        check=True,
    )
    subprocess.run([signtool, "verify", "/pa", "/v", str(installer)], check=True)


def compile_installer(out: Path, payload: Path, version: str) -> Path:
    program_files_x86 = Path(os.environ.get("ProgramFiles(x86)", r"C:\\Program Files (x86)"))
    iscc = find_tool(
        "ISCC",
        "ISCC.exe",
        (program_files_x86 / "Inno Setup 6" / "ISCC.exe",),
    )
    if not iscc:
        raise RuntimeError(
            "ISCC.exe was not found; install Inno Setup 6 or set ISCC to its full path"
        )
    if not (payload / "chudware.exe").is_file():
        raise RuntimeError("staged payload has no chudware.exe; build the Windows host first")
    subprocess.run(
        [
            iscc,
            str(ISS_SCRIPT),
            f"/DPayloadDir={payload.resolve()}",
            f"/DAppVersion={version}",
            f"/DInstallerOutputDir={out.resolve()}",
        ],
        cwd=REPO,
        check=True,
    )
    installer = out / f"Chudware-Setup-{version}-x86_64.exe"
    if not installer.is_file():
        raise RuntimeError(f"Inno Setup succeeded but did not produce {installer}")
    sign_installer(installer)
    return installer


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Assemble the Chudware Windows installer.")
    ap.add_argument("--out", type=Path, default=REPO / "dist")
    ap.add_argument("--skip-wheels", action="store_true")
    ap.add_argument("--zip", action="store_true",
                    help="also retain a zip of the raw installation payload")
    ap.add_argument("--payload-only", action="store_true",
                    help="stage the payload without invoking Inno Setup (developer inspection)")
    args = ap.parse_args(argv)
    try:
        version = read_built_version()
        res = build(args.out, args.skip_wheels, args.zip)
        res["version"] = version
        if not args.payload_only:
            res["installer"] = str(compile_installer(args.out, Path(res["dist"]), version))
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"build_release: error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(res, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
