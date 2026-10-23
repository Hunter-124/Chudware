"""Provision the Chudware runtime into %APPDATA%/Chudware on first launch.

Ensures an isolated Python venv with build123d/OCCT/pyvista/mcp/etc. exists, plus a copy
of the app's canonical Python packages under ``services`` so the MCP server and worker
can run independent of the source tree. Idempotent and version-stamped: a ready
runtime is detected and reused without re-installing.

This provisioner installs only a reviewed, complete offline wheelhouse matching
vendor/python/RUNTIME_WHEELS.lock (prepared by scripts/prepare_runtime_wheels.py) and
never consults a package index. Source checkouts without that lock use a plain venv
instead (scripts/provision_venv.sh, or ``pip install -r requirements.txt``). Release
installers ship compiled sidecars and do not run this provisioner.

    python -m services.installer.provision [--check] [--force]
          [--base-python <exe>] [--wheelhouse <dir>] [--venv-dir <dir>]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

from services import paths
from services.installer.wheelhouse import (
    WheelhouseError, _packaging, install_command, pip_environment, verified_wheelhouse,
)

def run_python_entry(source_root: str, generated_root: str, arguments: list[str]) -> None:
    """Run a builder command after its independent full runtime proof.

    Caller launches this authenticated interpreter with -I and a scrubbed loader
    environment. Only authored/generated builder paths are added, never ambient
    PYTHONPATH. The builder must prove the runtime before its first invocation.
    """
    import runpy
    root = Path(source_root).resolve(strict=True)
    explicit = [str(root)]
    if generated_root and Path(generated_root).is_dir():
        explicit.insert(0, str(Path(generated_root).resolve(strict=True)))
    if not arguments:
        raise ValueError("a Python builder command is required")
    if arguments[0] not in ("-c", "-m"):
        script = Path(arguments[0]).resolve(strict=True)
        if not script.is_relative_to(root):
            raise ValueError("builder scripts must belong to the trusted source root")
        explicit.append(str(script.parent))
    sys.path[:0] = explicit
    if arguments[0] == "-c":
        sys.argv = ["-c", *arguments[2:]]
        exec(compile(arguments[1], "<builder command>", "exec", dont_inherit=True),
             {"__name__": "__main__", "__file__": "<builder command>"})
    elif arguments[0] == "-m":
        sys.argv = [arguments[1], *arguments[2:]]
        runpy.run_module(arguments[1], run_name="__main__", alter_sys=True)
    else:
        sys.argv = arguments
        runpy.run_path(arguments[0], run_name="__main__")


def _find_repo() -> Path:
    """Locate the repo root (the directory holding requirements.txt).

    Package code always lives at ``services/installer``; walking to the marker keeps
    source and staged layouts explicit without depending on a compatibility alias.
    """
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "requirements.txt").is_file():
            return parent
    return here.parents[2]


REPO = _find_repo()
REQUIREMENTS = REPO / "requirements.txt"
WHEEL_LOCK = REPO / "vendor" / "python" / "RUNTIME_WHEELS.lock"
CHUDCODE_LOCK = REPO / "vendor" / "chudcode" / "CHUDCODE_ARTIFACT.lock"
# Top-level packages under the staged app/ directory.
APP_PACKAGES = ("engine",)
# Canonical ``services.X`` packages used by the worker and packaged tools.
SERVICE_PACKAGES = ("agent", "mcp", "installer", "plm", "dag", "browser",
                    "deliverables", "ipc", "serving", "verify", "parts",
                    "reveng", "sim", "vision")
# Top-level modules the worker needs at import time: worker_main itself (the
# spawned module), paths (hard import), kernel_exec (run_model).
SERVICE_MODULES = ("worker_main.py", "paths.py", "kernel_exec.py")
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
    # office_oxide (pip office-oxide) + pypdf back the office_* MCP family; missing
    # oxide still degrades at call time, but a fresh provision should install it from requirements.
    # dulwich backs the PLM vault (version history); a venv missing it silently breaks
    # every plm_* call with a no_vault envelope, so probe it too — a venv that predates the PLM
    # dependency is then flagged not-ready and rebuilt instead of being reused as-is.
    probe = ("import build123d, pyvista, mcp, anthropic, openai; "
             "import pptx, docx, openpyxl, matplotlib, reportlab, pypdfium2, pypdf; "
             "import office_oxide; "
             "import playwright, dulwich, chudcode_rpc")
    r = subprocess.run([str(py), "-I", "-c", probe], capture_output=True, text=True,
                       env=pip_environment())
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
    """Availability of the OPTIONAL ngspice SPICE backend — never installs.

    ngspice is subprocess-only: discovered via the ``CHUDWARE_NGSPICE`` env var or PATH,
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


def _trusted_base_path(base_python: str | None = None, candidate_dir: Path | None = None) -> str:
    base = Path(base_python or sys._base_executable)
    if not base.is_absolute() or not base.is_file():
        raise WheelhouseError("select an operator-authenticated absolute base Python executable")
    resolved = base.resolve(strict=True)
    if ((base.parent.parent / "pyvenv.cfg").exists()
            or (resolved.parent.parent / "pyvenv.cfg").exists()):
        raise WheelhouseError("the inspection base must not be a candidate/venv interpreter")
    if candidate_dir is not None and (
            base.absolute().is_relative_to(candidate_dir.absolute())
            or resolved.is_relative_to(candidate_dir.resolve())):
        raise WheelhouseError("a candidate interpreter cannot authenticate itself as the base")
    return str(base)


def _target_context(base: str) -> dict:
    base = _trusted_base_path(base)
    # Only the explicitly selected system/portable interpreter runs here. No
    # wheel/backend or installed product module is executed to discover a target.
    code = (
        "import sys,json;sys.path.insert(0,sys.argv[1]); "
        "from services.installer.wheelhouse import _packaging; "
        "markers,_,_,_,_,tags=_packaging(); "
        "print(json.dumps({'environment':markers.default_environment(),"
        "'tags':[str(tag) for tag in tags.sys_tags()]}))"
    )
    if str(Path(base).resolve()) == str(Path(sys.executable).resolve()):
        markers, _, _, _, _, tags = _packaging()
        return {"environment": markers.default_environment(), "tags": [str(tag) for tag in tags.sys_tags()]}
    result = subprocess.run([base, "-I", "-S", "-c", code, str(REPO)],
                            check=True, capture_output=True, text=True, env=pip_environment())
    return json.loads(result.stdout)


def _installed_identity_ok(py: Path, identity: dict, base_python: str | None = None) -> bool:
    if not py.exists():
        return False
    # Inspect using only the separately trusted base. -S is not authentication
    # of an executable; candidate exe/config/loader identities are checked first.
    probe = """
import json, sys
from pathlib import Path
expected = json.loads(sys.stdin.read())
sys.path.insert(0, expected['source_root'])
from services.installer.wheelhouse import verify_venv_interpreter, verify_installed_runtime
verify_venv_interpreter(Path(expected['venv']))
verify_installed_runtime(Path(expected['venv']), expected)
"""
    expected = {key: identity[key] for key in ("packages", "hashes", "files")}
    expected["venv"] = str(py.absolute().parent.parent)
    expected["source_root"] = str(REPO)
    try:
        base = _trusted_base_path(base_python, py.absolute().parent.parent)
    except WheelhouseError:
        return False
    result = subprocess.run([str(base), "-I", "-S", "-c", probe], input=json.dumps(expected),
                            capture_output=True, text=True, env=pip_environment())
    return result.returncode == 0


def _stamp_matches(stamp_path: Path, identity: dict) -> bool:
    try:
        stamp = json.loads(stamp_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return (stamp.get("req_hash") == _req_hash()
            and stamp.get("wheel_lock_sha256") == identity["lock_sha256"]
            and stamp.get("wheel_target") == identity["target"])


def _runtime_location(venv_dir: Path | None) -> tuple[Path, Path, Path]:
    directory = Path(venv_dir) if venv_dir is not None else paths.RUNTIME / "venv"
    py = directory / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    stamp = STAMP if directory.resolve() == (paths.RUNTIME / "venv").resolve() else directory / ".ready.json"
    return directory, py, stamp


def is_ready(wheelhouse: Path | None = None, venv_dir: Path | None = None,
             base_python: str | None = None) -> bool:
    _, py, stamp = _runtime_location(venv_dir)
    try:
        base = _trusted_base_path(base_python, py.absolute().parent.parent)
        context = _target_context(base)
        with verified_wheelhouse(wheelhouse or REPO / "wheelhouse", REQUIREMENTS,
                                 WHEEL_LOCK, CHUDCODE_LOCK, context["environment"],
                                 set(context["tags"])) as identity:
            return (_stamp_matches(stamp, identity)
                    and _installed_identity_ok(py, identity, base) and _imports_ok(py))
    except (WheelhouseError, OSError, ValueError, subprocess.CalledProcessError):
        return False


def _write_stamp(stamp_path: Path, py: Path, identity: dict) -> None:
    stamp_path.parent.mkdir(parents=True, exist_ok=True)
    stamp_path.write_text(json.dumps({
        "req_hash": _req_hash(),
        "wheel_lock_sha256": identity["lock_sha256"],
        "wheel_target": identity["target"],
        "python": str(py),
    }, indent=2), encoding="utf-8")


def ensure_app_code(src: Path = REPO) -> Path:
    """Copy the app's Python packages into %APPDATA%/Chudware/app."""
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
        for mod in SERVICE_MODULES:
            s = services_src / mod
            if s.exists():
                shutil.copy2(s, services_dst / mod)
    if REQUIREMENTS.exists():
        shutil.copy2(REQUIREMENTS, app / "requirements.txt")
    for identity in (WHEEL_LOCK, CHUDCODE_LOCK):
        if identity.is_file():
            destination = app / identity.relative_to(REPO)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(identity, destination)
    return app


def ensure_runtime(base_python: str | None = None, wheelhouse: Path | None = None,
                   force: bool = False, venv_dir: Path | None = None) -> dict:
    """Install only an authenticated, complete snapshot; never resolve online."""
    if not WHEEL_LOCK.is_file():
        raise WheelhouseError(
            f"reviewed runtime wheel lock unavailable: {WHEEL_LOCK}; prepare it with "
            "scripts/prepare_runtime_wheels.py, or for a source checkout provision a "
            "plain venv with scripts/provision_venv.sh (pip install -r requirements.txt)"
        )
    directory, py, stamp = _runtime_location(venv_dir)
    base = _trusted_base_path(base_python, directory)
    context = _target_context(base)
    with verified_wheelhouse(wheelhouse or REPO / "wheelhouse", REQUIREMENTS,
                             WHEEL_LOCK, CHUDCODE_LOCK, context["environment"],
                             set(context["tags"])) as identity:
        if (not force and _stamp_matches(stamp, identity)
                and _installed_identity_ok(py, identity, base) and _imports_ok(py)):
            return {"status": "ready", "reused": True, "python": str(py)}
        if directory.exists() and not force:
            raise WheelhouseError(
                "existing venv lacks authenticated runtime identity; leave it unchanged "
                "and explicitly request --force with approved inputs to replace it"
            )
        # Validate ALL managed inputs above before removing/creating a venv or
        # executing pip. No upgrade of pip, editable install or build isolation.
        stamp.unlink(missing_ok=True)
        if force and directory.exists():
            shutil.rmtree(directory)
        directory.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run([base, "-I", "-S", "-m", "venv", "--without-pip", str(directory)],
                       check=True, env=pip_environment())
        # The base process proves the generated interpreter before even ensurepip
        # may launch it. No candidate startup or provenance self-attestation.
        proof = (
            "import sys;from pathlib import Path;sys.path.insert(0,sys.argv[1]);"
            "from services.installer.wheelhouse import verify_venv_interpreter;"
            "verify_venv_interpreter(Path(sys.argv[2]))"
        )
        subprocess.run([base, "-I", "-S", "-c", proof, str(REPO), str(directory)],
                       check=True, env=pip_environment())
        subprocess.run([str(py), "-I", "-m", "ensurepip", "--default-pip"],
                       check=True, env=pip_environment())
        bootstrap_identity = {"packages": {}, "hashes": {}, "files": {}}
        if not _installed_identity_ok(py, bootstrap_identity, base):
            raise WheelhouseError("new runtime bootstrap differs from the trusted base payload")
        subprocess.run(install_command(py, identity["requirements"]), check=True, env=pip_environment())
        if not _installed_identity_ok(py, identity, base) or not _imports_ok(py):
            return {"status": "error", "reason": "identity/import checks failed after offline install"}
        # Browser binaries and optional sim/AI tools have independent provisioning
        # interfaces; managed dependency setup must not silently download them.
        _write_stamp(stamp, py, identity)
        return {"status": "ready", "reused": False, "python": str(py)}




def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Provision the Chudware runtime.")
    ap.add_argument("--check", action="store_true", help="report readiness only")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--check-ready", action="store_true",
                    help="return success only for an authenticated ready managed runtime")
    ap.add_argument("--check-inputs", action="store_true",
                    help="authenticate installed runtime wheels before compiling release sidecars")
    ap.add_argument("--base-python", default=None)
    ap.add_argument("--wheelhouse", type=Path, default=None)
    ap.add_argument("--venv-dir", type=Path, default=None,
                    help="provision a source-development venv rather than the managed runtime")
    ap.add_argument("--with-app-code", action="store_true",
                    help="also copy app packages into %%APPDATA%%/Chudware/app")
    args = ap.parse_args(argv)
    if args.check_ready:
        return 0 if is_ready(args.wheelhouse, args.venv_dir, args.base_python) else 1
    if args.check_inputs:
        try:
            if args.venv_dir is None:
                raise WheelhouseError("--check-inputs requires --venv-dir for the candidate; run from a separate trusted base")
            base = _trusted_base_path(args.base_python, args.venv_dir)
            context = _target_context(base)
            with verified_wheelhouse(args.wheelhouse or REPO / "wheelhouse", REQUIREMENTS,
                                     WHEEL_LOCK, CHUDCODE_LOCK, context["environment"], set(context["tags"]),
                                     additional_requirements=REPO / "packaging/requirements-release.txt") as identity:
                _, py, stamp = _runtime_location(args.venv_dir)
                if not _stamp_matches(stamp, identity) or not _installed_identity_ok(py, identity, base):
                    raise WheelhouseError("compiler runtime executable/wheels do not match authenticated base/provisioning identity")
            return 0
        except (WheelhouseError, OSError, ValueError) as exc:
            print(json.dumps({"status": "error", "reason": str(exc)}))
            return 1

    if args.check:
        _, py, _ = _runtime_location(args.venv_dir)
        print(json.dumps({"ready": is_ready(args.wheelhouse, args.venv_dir, args.base_python), "python": str(py),
                          "exists": py.exists(),
                          "converters": converters_status(),
                          "ngspice": ngspice_provisioning_status(),
                          "reveng": reveng_provisioning_status(),
                          "renode": renode_provisioning_status(),
                          "openems": openems_provisioning_status()}, indent=2))
        return 0

    try:
        res = ensure_runtime(args.base_python, args.wheelhouse, args.force, args.venv_dir)
    except (WheelhouseError, OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(json.dumps({"status": "error", "reason": str(exc)}, indent=2))
        return 1
    if args.with_app_code and res.get("status") == "ready":
        res["app_code"] = str(ensure_app_code())
    print(json.dumps(res, indent=2))
    return 0 if res.get("status") == "ready" else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
