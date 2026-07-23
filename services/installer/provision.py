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
import os
import shutil
import subprocess
import sys
from pathlib import Path

from services import paths

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
# Top-level packages under the staged app/ dir (import as engine, agent, …).
APP_PACKAGES = ("engine", "mcp_server", "agent", "installer")
# Nested packages under services/ that the engine worker imports as services.X
# (e.g. services.plm.worker_rpc for the PLM hub). Without these, plm_*/git_*
# methods never register and the PLM tab sits on "Loading…".
SERVICE_PACKAGES = ("plm", "dag", "browser", "deliverables", "ipc", "serving", "verify")
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
    # dulwich backs the PLM vault (Pillar 19 version history); a venv missing it silently breaks
    # every plm_* call with a no_vault envelope, so probe it too — a venv that predates the PLM
    # dependency is then flagged not-ready and rebuilt instead of being reused as-is.
    probe = ("import build123d, pyvista, mcp, anthropic, openai; "
             "import pptx, docx, openpyxl, matplotlib, reportlab, pypdfium2; "
             "import playwright, dulwich")
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
# table + offline readiness probe live in services/vision/reveng_provision.py
# (kept there so services/mcp/reveng_tools.reveng_status() can consume it without
# importing the installer). OpenMVS is AGPL -> subprocess-only via OPENMVS_BIN,
# never bundled/linked/auto-downloaded. This installer-facing wrapper reports
# availability for --check; it installs nothing and never touches the network.
def reveng_provisioning_status() -> dict:
    """Offline readiness of the Reverse Engineering toolchain (contract §8.1).

    Delegates to services.vision.reveng_provision.reveng_status(); returns a
    contract-shaped ``{ok, missing, ...}`` even if that module is unavailable, so
    the installer --check never raises on a partial tree.
    """
    try:
        from services.vision import reveng_provision
    except Exception as exc:  # noqa: BLE001 - optional subsystem, never fatal
        return {"ok": False, "reason": "reveng_provision unavailable",
                "hint": str(exc), "missing": [], "capabilities": {}}
    return reveng_provision.reveng_status()


def ngspice_provisioning_status() -> dict:
    """Availability of the OPTIONAL ngspice SPICE backend (T-FIN14) — never installs.

    ngspice is subprocess-only: discovered via the ``AI_CAD_NGSPICE`` env var or PATH,
    never bundled. Its absence is not an error — ecad_simulate/ecad_tf degrade honestly
    to the internal MNA solver (backend='mna', the default) with a ``backend_note``.
    Delegates to engine.sim.ngspice_backend.ngspice_status(); never raises.
    """
    try:
        from services.installer.ngspice_backend import ngspice_status
    except Exception as exc:  # noqa: BLE001 - optional subsystem, never fatal
        return {"tool": "ngspice", "installed": False,
                "hint": f"ngspice_backend unavailable: {exc}"}
    return ngspice_status()


def renode_provisioning_status() -> dict:
    """Availability of the OPTIONAL Renode firmware emulator — never raises.

    Renode is MIT, subprocess-only (PATH / known install roots). Absence is not
    an error — firmware emulation degrades honestly. Delegates to
    ``tools.provision_renode.status()`` when importable.
    """
    try:
        # tools/ is a namespace package at the repo root; ensure REPO is on path.
        if str(REPO) not in sys.path:
            sys.path.insert(0, str(REPO))
        from tools.provision_renode import status as _renode_status
        return _renode_status()
    except Exception as exc:  # noqa: BLE001 - optional subsystem, never fatal
        which = shutil.which("renode") or shutil.which("renode.exe")
        return {
            "tool": "renode",
            "installed": bool(which),
            "path": which,
            "version": None,
            "hint": None if which else f"provision_renode unavailable: {exc}",
        }


def openems_provisioning_status() -> dict:
    """Availability of the OPTIONAL openEMS FDTD backend — never raises.

    openEMS is GPLv3, python-module only (``import openEMS`` / ``CSXCAD``).
    Absence is not an error — em_* adapters degrade to builtin-FDFD/analytic.
    Delegates to ``tools.provision_em.status()`` when importable.
    """
    try:
        if str(REPO) not in sys.path:
            sys.path.insert(0, str(REPO))
        from tools.provision_em import status as _openems_status
        return _openems_status()
    except Exception as exc:  # noqa: BLE001 - optional subsystem, never fatal
        return {
            "tool": "openems",
            "installed": False,
            "python": None,
            "version": None,
            "detail": f"provision_em unavailable: {exc}",
            "hint": "python -m tools.provision_em --install",
        }


def ensure_optional_sim_tools(*, install: bool = False) -> dict:
    """Optionally provision Renode + openEMS. Always non-fatal / best-effort.

    Returns a dict of per-tool status snapshots. When *install* is False this is
    detection-only (used by ``--check``). When True, each provisioner's
    ``--install`` path is invoked if the tool is absent; failures are recorded
    but never raised so a normal install still succeeds.
    """
    out: dict = {"renode": renode_provisioning_status(), "openems": openems_provisioning_status()}
    if not install:
        return out

    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))

    if not out["renode"].get("installed"):
        try:
            from tools import provision_renode as _pr
            print("[provision] optional: installing Renode ...")
            _pr.install()
        except Exception as exc:  # noqa: BLE001
            print(f"[provision] optional Renode install skipped: {exc}")
        out["renode"] = renode_provisioning_status()

    if not out["openems"].get("installed"):
        try:
            from tools import provision_em as _pe
            print("[provision] optional: installing openEMS ...")
            _pe.install()
        except Exception as exc:  # noqa: BLE001
            print(f"[provision] optional openEMS install skipped: {exc}")
        out["openems"] = openems_provisioning_status()

    return out


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
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc", "build", "test_*", "tests")
    for pkg in APP_PACKAGES:
        s = src / pkg
        if s.exists():
            shutil.copytree(s, app / pkg, dirs_exist_ok=True, ignore=ignore)
    # services.X packages — worker imports `from services.plm.worker_rpc …` etc.
    # Stage under app/services/ so cwd=appDir() on the worker process can resolve them.
    services_src = src / "services"
    if services_src.is_dir():
        services_dst = app / "services"
        services_dst.mkdir(parents=True, exist_ok=True)
        init_py = services_src / "__init__.py"
        if init_py.exists():
            shutil.copy2(init_py, services_dst / "__init__.py")
        else:
            (services_dst / "__init__.py").touch()
        for name in SERVICE_PACKAGES:
            s = services_src / name
            if s.exists():
                shutil.copytree(s, services_dst / name, dirs_exist_ok=True, ignore=ignore)
        # services.agent is also the top-level `agent` package (junction). Mirror it
        # under services/ so `from services.agent…` works without monorepo layout.
        agent_src = src / "agent" if (src / "agent").exists() else services_src / "agent"
        if agent_src.exists():
            shutil.copytree(agent_src, services_dst / "agent", dirs_exist_ok=True, ignore=ignore)
        # services.mcp mirrors mcp_server for `from services.mcp…` imports.
        mcp_src = src / "mcp_server" if (src / "mcp_server").exists() else services_src / "mcp"
        if mcp_src.exists():
            shutil.copytree(mcp_src, services_dst / "mcp", dirs_exist_ok=True, ignore=ignore)
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

    # Optional sim/firmware backends (Renode, openEMS). Best-effort, non-fatal,
    # idempotent. Controlled by AICAD_PROVISION_OPTIONAL_SIM (default: "1" = try).
    # Set to "0"/"false" to skip network/source builds during a minimal install.
    opt_flag = os.environ.get("AICAD_PROVISION_OPTIONAL_SIM", "1").strip().lower()
    if opt_flag not in ("0", "false", "no", "off"):
        try:
            ensure_optional_sim_tools(install=True)
        except Exception as exc:  # noqa: BLE001
            print(f"[provision] optional sim tools skipped: {exc}")

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
                          "ngspice": ngspice_provisioning_status(),
                          "reveng": reveng_provisioning_status(),
                          "renode": renode_provisioning_status(),
                          "openems": openems_provisioning_status()}, indent=2))
        return 0

    res = ensure_runtime(args.base_python, args.wheelhouse, args.force)
    if args.with_app_code:
        res["app_code"] = str(ensure_app_code())
    print(json.dumps(res, indent=2))
    return 0 if res.get("status") == "ready" else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
