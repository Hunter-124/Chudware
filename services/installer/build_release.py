"""Assemble a distributable AI-CAD release (payload + installer scripts).

Produces  <out>/AI-CAD/  containing:
    AI-CAD.exe              the native host (built separately via CMake)
    app/                    Python packages (engine, mcp_server, agent, installer) + requirements
    wheelhouse/             offline wheels for the runtime (pip download)   [--skip-wheels to omit]
    python-embed/           portable CPython for self-contained installs    [--with-python]
    install.ps1 / uninstall.ps1
This is the "embed-and-extract" payload: install.ps1 provisions %APPDATA%/AI-CAD from it and
registers the MCP server. Optionally zips the folder.

    python -m installer.build_release [--out dist] [--skip-wheels] [--with-python] [--zip]
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
APP_PACKAGES = ("engine", "mcp_server", "agent", "installer")
# Nested packages under services/ that the engine worker imports as services.X
# (services.plm drives the PLM hub; without it plm_*/git_* never register).
SERVICE_PACKAGES = ("plm", "dag", "browser", "deliverables", "ipc", "serving", "verify")
HOST_EXE = REPO / "host" / "build-win" / "bin" / "Billet.exe"
if not HOST_EXE.exists():
    HOST_EXE = REPO / "host" / "build" / "bin" / "Release" / "AI-CAD.exe"
# Engine host daemon (core/hostd). MSVC builds of aicad-hostd are new — absence is a
# warning + skip, not a failure (the app falls back to legacy in-process engine mode).
HOSTD_EXE_CANDIDATES = (
    REPO / "core" / "build" / "bin" / "Release" / "aicad-hostd.exe",
    REPO / "core" / "build" / "bin" / "aicad-hostd.exe",
)
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
        for name in SERVICE_PACKAGES:
            s = services_src / name
            if s.exists():
                shutil.copytree(s, services_dst / name, dirs_exist_ok=True, ignore=_IGNORE)
        agent_src = REPO / "agent" if (REPO / "agent").exists() else services_src / "agent"
        if agent_src.exists():
            shutil.copytree(agent_src, services_dst / "agent", dirs_exist_ok=True, ignore=_IGNORE)
        mcp_src = REPO / "mcp_server" if (REPO / "mcp_server").exists() else services_src / "mcp"
        if mcp_src.exists():
            shutil.copytree(mcp_src, services_dst / "mcp", dirs_exist_ok=True, ignore=_IGNORE)
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


def build(out: Path, skip_wheels: bool, with_python: bool, make_zip: bool) -> dict:
    dst = out / "AI-CAD"
    if dst.exists():
        shutil.rmtree(dst)
    dst.mkdir(parents=True)

    manifest: dict = {"components": {}}

    if HOST_EXE.exists():
        shutil.copy2(HOST_EXE, dst / "AI-CAD.exe")
        manifest["components"]["exe"] = True
    else:
        manifest["components"]["exe"] = False  # build it with: cmake --build host/build --config Release

    # aicad-hostd.exe (+ .pdb when present) — staged next to AI-CAD.exe/libaicad.dll so
    # install.ps1 can stop/replace it and register_mcp can pin AICAD_HOSTD_BIN at it.
    hostd = next((p for p in HOSTD_EXE_CANDIDATES if p.exists()), None)
    if hostd is not None:
        shutil.copy2(hostd, dst / "aicad-hostd.exe")
        pdb = hostd.with_suffix(".pdb")
        if pdb.exists():
            shutil.copy2(pdb, dst / "aicad-hostd.pdb")
        manifest["components"]["hostd"] = True
    else:
        print("warning: aicad-hostd.exe not found (cmake --build core\\build --config Release "
              "--target aicad-hostd) — payload ships without the engine daemon", file=sys.stderr)
        manifest["components"]["hostd"] = False

    # brand + font assets (loaded by the GUI from <exedir>/assets)
    assets_src = REPO / "host" / "assets"
    if assets_src.exists():
        shutil.copytree(assets_src, dst / "assets", dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("Rubik-VF.ttf", "make_logo.py", "__pycache__"))
        manifest["components"]["assets"] = True

    stage_app(dst)
    manifest["components"]["app"] = True
    manifest["components"].update(stage_optional_payloads(dst))

    shutil.copy2(REPO / "installer" / "install.ps1", dst / "install.ps1")
    shutil.copy2(REPO / "installer" / "uninstall.ps1", dst / "uninstall.ps1")
    shutil.copy2(REPO / "requirements.txt", dst / "requirements.txt")

    if skip_wheels:
        manifest["components"]["wheelhouse"] = "skipped"
    else:
        manifest["components"]["wheelhouse"] = stage_wheels(dst)

    if with_python:
        manifest["components"]["python_embed"] = (
            "TODO: drop a python-build-standalone cpython-3.11 x86_64-pc-windows-msvc "
            "'install_only' tree into python-embed/ for a fully self-contained installer")

    (dst / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    if make_zip:
        archive = shutil.make_archive(str(out / "AI-CAD-release"), "zip", root_dir=dst.parent,
                                      base_dir="AI-CAD")
        manifest["zip"] = archive

    manifest["dist"] = str(dst)
    return manifest


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Assemble an AI-CAD release.")
    ap.add_argument("--out", type=Path, default=REPO / "dist")
    ap.add_argument("--skip-wheels", action="store_true")
    ap.add_argument("--with-python", action="store_true")
    ap.add_argument("--zip", action="store_true")
    args = ap.parse_args(argv)
    res = build(args.out, args.skip_wheels, args.with_python, args.zip)
    print(json.dumps(res, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
