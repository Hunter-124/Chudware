"""Provision the AI-CAD runtime into %APPDATA%/AI-CAD on first launch.

Ensures an isolated Python venv with build123d/OCCT/pyvista/mcp/etc. exists, plus a copy
of the app's Python packages (engine, mcp_server, agent, installer) so the MCP server and
worker can run independent of the source tree. Idempotent and version-stamped: a ready
runtime is detected and reused without re-installing.

In the shipped product the C++ launcher extracts an embedded payload (portable Python +
offline wheelhouse) here; this module is that setup logic and also works from source.

    python -m installer.provision [--check] [--force]
                                  [--base-python <exe>] [--wheelhouse <dir>]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

from engine import paths

def _find_repo() -> Path:
    """Locate the repo root (the dir holding requirements.txt) by walking up from this file.

    Robust to how ``installer`` is laid out: on a normal checkout ``installer`` is a symlink to
    ``services/installer`` (so ``__file__`` resolves 3 levels below the root), but on Windows —
    where git symlinks materialize as real top-level directory copies — it sits only 2 levels
    below. A fixed ``parent.parent.parent`` overshoots by one there and picks the wrong root
    (the source of the "Could not open requirements file" first-run failure). Anchoring on the
    requirements.txt marker instead is correct under either layout.
    """
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "requirements.txt").is_file():
            return parent
    return here.parent.parent.parent  # historical fallback (services/installer symlink depth)


REPO = _find_repo()
REQUIREMENTS = REPO / "requirements.txt"
APP_PACKAGES = ("engine", "mcp_server", "agent", "installer")
STAMP = paths.RUNTIME / ".ready.json"


def _venv_python() -> Path:
    # Mirrors bootstrap.cpp's runtimePython() layout exactly (both must agree on where the
    # managed venv's interpreter lives, or the C++ readiness check and this provisioner
    # disagree on Linux): Scripts/python.exe on Windows, bin/python everywhere else.
    if sys.platform == "win32":
        return paths.RUNTIME / "venv" / "Scripts" / "python.exe"
    return paths.RUNTIME / "venv" / "bin" / "python"


def _req_hash() -> str:
    data = REQUIREMENTS.read_bytes() if REQUIREMENTS.exists() else b""
    return hashlib.sha256(data).hexdigest()[:16]


def _imports_ok(py: Path) -> bool:
    if not py.exists():
        return False
    # Includes the deliverables/1 in-process engines (contract §7): they are required deps, so a
    # runtime missing python-pptx/docx/openpyxl/matplotlib/reportlab/pypdfium2 is NOT ready.
    probe = ("import build123d, pyvista, mcp, anthropic, openai; "
             "import pptx, docx, openpyxl, matplotlib, reportlab, pypdfium2")
    r = subprocess.run([str(py), "-c", probe], capture_output=True, text=True)
    return r.returncode == 0


def converters_status() -> dict:
    """Discover the OPTIONAL subprocess-only converters on PATH (contract §7) — never installs.

    pandoc (GPL) and LibreOffice headless (MPL) are subprocess-only: discovered on PATH, never
    bundled. Their absence is not an error — the deliverables tools degrade to a contract-shaped
    ``{ok:false, reason:"converter_missing", hint:...}`` and CI passes without them. This helper
    reports availability for the installer/host; it raises nothing.
    """
    lo = shutil.which("soffice") or shutil.which("libreoffice")
    pandoc = shutil.which("pandoc")
    return {
        "libreoffice": {
            "installed": bool(lo), "path": lo,
            "hint": None if lo else "install libreoffice (office->PDF previews in render_document)",
        },
        "pandoc": {
            "installed": bool(pandoc), "path": pandoc,
            "hint": None if pandoc else "install pandoc (optional markup conversion)",
        },
    }


# RE-ANCHOR(reveng): Reverse Engineering download-on-demand descriptors (RE-06).
# The heavy RE toolchain (COLMAP/OpenMVS/Brush binaries, TripoSR/TRELLIS/VGGT
# weights) is NEVER part of the base runtime venv above — it is provisioned on
# demand or discovered from user-provided binaries. The SHA-pinned descriptor
# table + offline readiness probe live in engine_oracle/vision/reveng_provision.py
# (kept there so services/mcp/reveng_tools.reveng_status() can consume it without
# importing the installer). OpenMVS is AGPL -> subprocess-only via OPENMVS_BIN,
# never bundled/linked/auto-downloaded. This installer-facing wrapper reports
# availability for --check; it installs nothing and never touches the network.
def reveng_provisioning_status() -> dict:
    """Offline readiness of the Reverse Engineering toolchain (contract §8.1).

    Delegates to engine_oracle.vision.reveng_provision.reveng_status(); returns a
    contract-shaped ``{ok, missing, ...}`` even if that module is unavailable, so
    the installer --check never raises on a partial tree.
    """
    try:
        from engine_oracle.vision import reveng_provision
    except Exception as exc:  # noqa: BLE001 - optional subsystem, never fatal
        return {"ok": False, "reason": "reveng_provision unavailable",
                "hint": str(exc), "missing": [], "capabilities": {}}
    return reveng_provision.reveng_status()


def is_ready() -> bool:
    if not STAMP.exists():
        return False
    try:
        stamp = json.loads(STAMP.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return False
    if not (stamp.get("req_hash") == _req_hash() and _venv_python().exists()):
        return False
    # Verify that the key packages are actually importable — catches a partial venv that
    # has python.exe but is missing a module (e.g. pyvista/vtk failed to install).
    return _imports_ok(_venv_python())


def _write_stamp() -> None:
    STAMP.parent.mkdir(parents=True, exist_ok=True)
    STAMP.write_text(json.dumps({
        "req_hash": _req_hash(),
        "python": str(_venv_python()),
    }, indent=2), encoding="utf-8")


def ensure_app_code(src: Path = REPO) -> Path:
    """Copy the app's Python packages into %APPDATA%/AI-CAD/app."""
    app = paths.ROOT / "app"
    app.mkdir(parents=True, exist_ok=True)
    for pkg in APP_PACKAGES:
        s = src / pkg
        if s.exists():
            shutil.copytree(s, app / pkg, dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "build", "test_*"))
    if REQUIREMENTS.exists():
        shutil.copy2(REQUIREMENTS, app / "requirements.txt")
    return app


def ensure_runtime(base_python: str | None = None, wheelhouse: Path | None = None,
                   force: bool = False) -> dict:
    if is_ready() and not force:
        return {"status": "ready", "reused": True, "python": str(_venv_python())}

    py = _venv_python()
    # Reuse an existing, working venv (e.g. created during development).
    if py.exists() and _imports_ok(py) and not force:
        _write_stamp()
        return {"status": "ready", "reused": True, "python": str(py)}

    base = base_python or _find_base_python()
    paths.RUNTIME.mkdir(parents=True, exist_ok=True)
    venv_dir = paths.RUNTIME / "venv"
    if force and venv_dir.exists():
        # A repair: tear down the broken/partial venv so it is rebuilt cleanly.
        shutil.rmtree(venv_dir, ignore_errors=True)
    subprocess.run([base, "-m", "venv", str(venv_dir)], check=True)
    subprocess.run([str(py), "-m", "pip", "install", "--upgrade", "pip", "--quiet"], check=True)

    install = [str(py), "-m", "pip", "install", "-r", str(REQUIREMENTS)]
    if wheelhouse and Path(wheelhouse).exists():
        # Prefer bundled wheels (fast, deterministic, works offline) but still allow PyPI
        # for any transitive dependency not captured in the wheelhouse.
        install = [str(py), "-m", "pip", "install",
                   "--find-links", str(wheelhouse), "--prefer-binary",
                   "-r", str(REQUIREMENTS)]
    subprocess.run(install, check=True)

    # Install Playwright's Chromium browser binary (separate from the pip package).
    # The browser is ~300 MB and needs network; a failure here is non-fatal — every
    # Playwright call in services/browser/ degrades gracefully with a
    # "playwright not installed" error envelope.
    subprocess.run(
        [str(py), "-m", "playwright", "install", "chromium", "--with-deps"],
        check=False,
    )

    if not _imports_ok(py):
        return {"status": "error", "reason": "imports failed after install"}
    # Optional local-AI payload. This is a no-op unless the release/source tree includes a
    # `llamacpp` folder with llama-server.exe; status remains visible via installer.llamacpp.
    try:
        from installer import llamacpp
        llamacpp.install_from_bundle()
    except Exception:  # noqa: BLE001 - runtime provisioning must not fail because optional AI is absent
        pass
    _write_stamp()
    return {"status": "ready", "reused": False, "python": str(py)}


def _find_base_python() -> str:
    # The pinned requirements.txt wheels (build123d et al.) target Python 3.11 specifically;
    # sys.executable may be a newer interpreter the OS ships as the default `python3` (verified
    # live: a box with python3 -> 3.14 as default but python3.11 also installed failed to
    # resolve any pinned wheel until python3.11 was tried explicitly). Prefer 3.11 if present,
    # falling back to whatever is running this script.
    candidates = ["py", sys.executable] if sys.platform == "win32" else ["python3.11", sys.executable]
    for exe in candidates:
        if shutil.which(exe) or Path(exe).exists():
            return exe
    return sys.executable


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Provision the AI-CAD runtime.")
    ap.add_argument("--check", action="store_true", help="report readiness only")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--base-python", default=None)
    ap.add_argument("--wheelhouse", type=Path, default=None)
    ap.add_argument("--with-app-code", action="store_true",
                    help="also copy app packages into %%APPDATA%%/AI-CAD/app")
    args = ap.parse_args(argv)

    if args.check:
        print(json.dumps({"ready": is_ready(), "python": str(_venv_python()),
                          "exists": _venv_python().exists(),
                          "imports_ok": _imports_ok(_venv_python()),
                          "converters": converters_status(),
                          "reveng": reveng_provisioning_status()}, indent=2))
        return 0

    res = ensure_runtime(args.base_python, args.wheelhouse, args.force)
    if args.with_app_code:
        res["app_code"] = str(ensure_app_code())
    print(json.dumps(res, indent=2))
    return 0 if res.get("status") == "ready" else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
