# AGPL NOTICE: OpenMVS is AGPL-3.0. Chudware (MIT) fetches the pinned upstream release
# on demand (scripts/provision_reveng.py) but only ever runs it as a subprocess; never
# import or link against OpenMVS libraries. See contracts/reveng.md §0.1.
#
# Brush (3DGS trainer) is Apache-2.0 — subprocess only for consistency and isolation.
"""Dense reconstruction and mesh export backends for the RE pipeline.

Two backends, both subprocess-only:

  OpenMVS  — InterfaceCOLMAP → DensifyPointCloud → ReconstructMesh →
             RefineMesh → TextureMesh, from OPENMVS_BIN, PATH, or the managed install.
             Input: shared undistorted COLMAP dataset root with images/ and sparse/.
             Output: isolated dense/ and mesh/ under the session root's dense/.

  Brush    — trains 3D Gaussians using pinned Brush v0.3.0 (no mesh export).
             Managed install or explicit BRUSH_BIN executable.
             Input:  standalone undistorted COLMAP dataset with model + images.
             Output: immutable, validated Gaussian PLY exports under splat/.

When OpenMVS is unavailable the result names the approximate splat_to_mesh
fallback; Brush reports backend_not_provisioned when its executable is absent.
"""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Optional

from .reveng_provision import OPENMVS_TOOLS, brush_executable, openmvs_install_dir


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _not_provisioned(hint: str) -> dict:
    return {"ok": False, "reason": "backend_not_provisioned", "hint": hint}


def _run(cmd: list[str], *, cwd: Optional[str] = None) -> tuple[bool, str]:
    """Run a subprocess and return (success, stderr_or_empty)."""
    try:
        result = subprocess.run(
            cmd,
            check=True,
            capture_output=True,
            text=True,
            cwd=cwd,
        )
        return True, ""
    except subprocess.CalledProcessError as exc:
        return False, exc.stderr or ""
    except FileNotFoundError as exc:
        return False, str(exc)


# ---------------------------------------------------------------------------
# OpenMVS backend
# ---------------------------------------------------------------------------

def _openmvs_unavailable(missing=None):
    return {"ok": False, "reason": "dense_backend_unavailable", "backend": "openmvs",
            "fallback": "splat_to_mesh",
            "fallback_function": "services.formats.splat_io.splat_to_mesh",
            "hint": "Call reveng_install_tools (or use Install missing tools in the Reverse Engineering panel) to install OpenMVS, or set OPENMVS_BIN / add its tools to PATH."
                    + (f" Missing: {', '.join(missing)}." if missing else "")}


def _openmvs_bins(*, check):
    """An explicit OPENMVS_BIN is authoritative; otherwise PATH, then the managed install."""
    directory = os.environ.get("OPENMVS_BIN", "")
    managed = "" if directory else str(openmvs_install_dir())
    bins, missing = {}, []
    for name in OPENMVS_TOOLS:
        candidate = os.path.join(directory, name) if directory else name
        resolved = shutil.which(candidate) or (shutil.which(os.path.join(managed, name)) if managed else None)
        if resolved:
            bins[name] = resolved
        elif not check and directory:
            bins[name] = candidate
        else:
            missing.append(name)
    return bins, missing


def run_openmvs(
    dataset_dir: str,
    session_dir: str,
    *,
    working_resolution: int = 0,
    texture_size: int = 4096,
) -> dict:
    """Run user-installed OpenMVS on the shared, undistorted COLMAP dataset.

    ``dataset_dir`` contains ``images/`` and ``sparse/``.  The result remains
    in the dataset's native gauge; metric placement belongs to ReconTransform.
    ``working_resolution`` is the number of image downsampling levels.
    """
    _bins, missing = _openmvs_bins(check=True)
    if missing:
        return _openmvs_unavailable(missing)
    dataset = Path(dataset_dir).expanduser().resolve()

    def complete(model):
        return any(all((model / (name + ext)).is_file()
                       for name in ("cameras", "images", "points3D"))
                   for ext in (".bin", ".txt"))

    sparse = dataset / "sparse"
    model = sparse if complete(sparse) else sparse / "0"
    if not (dataset / "images").is_dir() or not complete(model):
        return {"ok": False, "reason": "invalid_dataset", "backend": "openmvs",
                "hint": "OpenMVS needs the shared undistorted dataset root with images/ and sparse/ model files."}
    # Keep every attempt in its own directory: a failed refresh must never
    # publish a prior run's scene or mesh as its new output.
    parent = Path(session_dir).expanduser().resolve() / "dense"
    parent.mkdir(parents=True, exist_ok=True)
    run_root = Path(tempfile.mkdtemp(prefix="openmvs-", dir=parent))
    dense_dir, mesh_dir = run_root / "dense", run_root / "mesh"
    dense_dir.mkdir()
    mesh_dir.mkdir()
    if model != sparse:
        # InterfaceCOLMAP requires files directly in sparse/. Keep a durable
        # view of sparse/0 and images/ for its scene's later image references.
        view = run_root / "dataset"
        view.mkdir()
        for source, target in ((model, view / "sparse"), (dataset / "images", view / "images")):
            try:
                target.symlink_to(source, target_is_directory=True)
            except OSError:
                shutil.copytree(source, target)
        dataset = view
    result = openmvs_commands(str(dataset), str(run_root), working_resolution=working_resolution,
                              texture_size=texture_size)
    if not result["ok"]:
        return result
    commands = result["commands"]
    for command in commands:
        ok, err = _run(command)
        if not ok:
            return {"ok": False, "reason": "openmvs_failed", "backend": "openmvs",
                    "hint": f"{Path(command[0]).name} failed: {err}"}
        expected = Path(command[command.index("--output-file") + 1])
        if not expected.is_file() or expected.stat().st_size == 0:
            return {"ok": False, "reason": "openmvs_output_missing", "backend": "openmvs",
                    "hint": f"{Path(command[0]).name} did not produce {expected}."}
    mesh_path = mesh_dir / "mesh_texture.obj"
    if not mesh_path.is_file() or mesh_path.stat().st_size == 0:
        return {"ok": False, "reason": "openmvs_output_missing", "backend": "openmvs",
                "hint": f"TextureMesh did not produce {mesh_path}."}
    return {"ok": True, "mesh_path": str(mesh_path), "backend": "openmvs",
            "units": "reconstruction"}


def openmvs_commands(
    dataset_dir: str,
    session_dir: str,
    *,
    working_resolution: int = 0,
    texture_size: int = 4096,
) -> dict:
    """Return the exact subprocess command lines for the OpenMVS pipeline without running them.

    Useful for inspection, testing, and dry-run displays.

    Returns:
        {"ok": True, "commands": [list-of-args, ...], "backend": "openmvs"}
        {"ok": False, "reason": "dense_backend_unavailable", ...} if absent.
    """
    bins, missing = _openmvs_bins(check=False)
    if missing:
        return _openmvs_unavailable(missing)

    dense_dir = os.path.join(session_dir, "dense")
    mesh_dir = os.path.join(session_dir, "mesh")
    scene_mvs = os.path.join(dense_dir, "scene.mvs")
    dense_mvs = os.path.join(dense_dir, "dense.mvs")
    mesh_mvs = os.path.join(dense_dir, "mesh.mvs")
    refined_mvs = os.path.join(dense_dir, "mesh_refine.mvs")
    textured_mvs = os.path.join(mesh_dir, "mesh_texture.mvs")

    commands = [
        [bins["InterfaceCOLMAP"], "--input-file", dataset_dir, "--output-file", scene_mvs],
        [bins["DensifyPointCloud"], "--input-file", scene_mvs, "--output-file", dense_mvs,
         "--working-folder", dense_dir, "--resolution-level", str(working_resolution),
         "--normalize-coordinates", "0", "--tower-mode", "0"],
        [bins["ReconstructMesh"], "--input-file", dense_mvs, "--output-file", mesh_mvs,
         "--working-folder", dense_dir],
        [bins["RefineMesh"], "--input-file", mesh_mvs, "--output-file", refined_mvs,
         "--working-folder", dense_dir],
        [bins["TextureMesh"], "--input-file", refined_mvs, "--output-file", textured_mvs,
         "--working-folder", dense_dir, "--export-type", "obj",
         "--max-texture-size", str(texture_size)],
    ]
    return {"ok": True, "commands": commands, "backend": "openmvs"}


# ---------------------------------------------------------------------------
# Brush (3DGS) backend
# ---------------------------------------------------------------------------

def run_brush(
    sparse_dir: str,
    session_dir: str,
    *,
    max_steps: int = 30_000,
    export_mesh: bool = False,
    image_dir: Optional[str] = None,
    export_every: Optional[int] = None,
    progress=None,
    cancel=None,
    own_process_group: bool = True,
) -> dict:
    """Train a complete COLMAP dataset with Brush's positional v0.3.0 CLI.

    ``image_dir`` names the entire undistorted dataset root, not its images
    subfolder. Without it, ``session_dir`` must itself be a complete dataset.
    Each invocation owns a fresh output directory: stale exports cannot turn a
    failed refresh into success. Standalone calls own a new process group and
    cancel that group boundedly. A supervising process-group owner explicitly
    passes ``own_process_group=False`` and is responsible for stopping the tree.
    """
    max_splats = 2_000_000  # Native viewer's explicit training resource budget.
    base = {"backend": "brush", "mesh_path": None, "max_splats": max_splats}

    def failure(reason, hint, **extra):
        return {**base, "ok": False, "reason": reason, "hint": hint, **extra}

    if export_mesh:
        return failure("mesh_export_unsupported", "Brush exports Gaussians, not meshes; use the separate OpenMVS action.")
    executable = brush_executable()
    if not executable:
        return {**base, **_not_provisioned("Call reveng_install_tools (or use Install missing tools in the Reverse Engineering panel), or set BRUSH_BIN to an executable.")}
    if isinstance(max_steps, bool) or not isinstance(max_steps, int) or max_steps < 1:
        return failure("invalid_iterations", "max_steps must be a positive integer.")
    every = min(max_steps, 100) if export_every is None else export_every
    if isinstance(every, bool) or not isinstance(every, int) or every < 1:
        return failure("invalid_export_interval", "export_every must be a positive integer.")
    dataset = Path(image_dir or session_dir).expanduser().resolve()
    model_dirs = (dataset / "sparse/0", dataset / "sparse", dataset)
    model = next((p for p in model_dirs if any(
        all((p / (name + ext)).is_file() for name in ("cameras", "images", "points3D"))
        for ext in (".bin", ".txt"))), None)
    if not (dataset / "images").is_dir() or model is None:
        return failure("invalid_dataset", "Brush needs a complete undistorted COLMAP dataset root with images/ and a sparse model.")
    if not Path(sparse_dir).is_dir():
        return failure("invalid_sparse_model", "sparse_dir must name the reconstructed COLMAP model.")
    if cancel and cancel():
        return failure("cancelled", "Training cancelled before launch.")

    # Import at call time: the pipeline also uses this adapter for training.
    from .gaussian_pipeline import inspect_splat

    proc = None
    latest = {}
    owned_group = os.name == "posix" and own_process_group

    def stop():
        if proc is None:
            return
        if owned_group:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        elif proc.poll() is None:
            proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            if owned_group:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            else:
                proc.kill()
            proc.wait(timeout=3)
        # Descendants can outlive their immediate parent after TERM.
        if owned_group:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    try:
        root = Path(session_dir).resolve() / "splat"
        root.mkdir(parents=True, exist_ok=True)
        run_dir = Path(tempfile.mkdtemp(prefix="brush-", dir=root))
        exports = run_dir / "exports"
        exports.mkdir()
        published = run_dir / "published"
        published.mkdir()
        log_path = run_dir / "brush.log"
        command = [
            executable, str(dataset),
            "--total-steps", str(max_steps), "--export-every", str(every),
            "--max-splats", str(max_splats),
            "--export-path", str(exports), "--export-name", "export_{iter}.ply",
        ]
        seen = set()

        def iteration_of(path):
            try:
                return int(path.stem.removeprefix("export_"))
            except ValueError:
                return None

        def publish():
            nonlocal latest
            candidates = []
            for path in exports.glob("export_*.ply"):
                iteration = iteration_of(path)
                if iteration is not None and iteration not in seen:
                    candidates.append((iteration, path))
            # Only the newest completed snapshot is useful to an interactive UI.
            for iteration, path in sorted(candidates, reverse=True):
                if latest and iteration <= latest["iteration"]:
                    continue
                temporary = published / (path.name + ".tmp")
                try:
                    before = path.stat()
                    shutil.copyfile(path, temporary)
                    info = inspect_splat(str(temporary))
                    if info["count"] > max_splats:
                        continue
                    after = path.stat()
                    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                        continue
                    target = published / path.name
                    os.replace(temporary, target)
                except (ValueError, OSError):
                    # Export writes are not atomic upstream. Retry next poll.
                    continue
                finally:
                    temporary.unlink(missing_ok=True)
                seen.add(iteration)
                latest = {"splat_path": str(target), "iteration": iteration,
                          "total_iterations": max_steps, "bounds": info["bounds"],
                          "max_splats": max_splats}
                # Raw exports are staging copies and older previews are superseded; a
                # long pass would otherwise keep hundreds of full-size PLYs. The previous
                # snapshot stays for a reader that is still loading it.
                for stale in exports.glob("export_*.ply"):
                    stale_iteration = iteration_of(stale)
                    if stale_iteration is not None and stale_iteration <= iteration:
                        stale.unlink(missing_ok=True)
                snapshots = sorted((p for p in published.glob("export_*.ply")
                                    if iteration_of(p) is not None), key=iteration_of)
                for old in snapshots[:-2]:
                    old.unlink(missing_ok=True)
                if progress:
                    progress("training", min(99, iteration * 100 // max_steps),
                             f"Brush exported iteration {iteration}/{max_steps}", dict(latest))
                break

        with log_path.open("wb") as log:
            proc = subprocess.Popen(command, cwd=str(dataset), stdout=log,
                                    stderr=subprocess.STDOUT, start_new_session=owned_group)
            if progress:
                progress("training", 0, "Brush training started", None)
            next_publish = 0.0
            while proc.poll() is None:
                if cancel and cancel():
                    return failure("cancelled", "Training cancelled; last completed preview retained.", **latest)
                now = time.monotonic()
                if now >= next_publish:
                    publish()
                    next_publish = now + 1.0
                time.sleep(0.1)
            publish()
        if cancel and cancel():
            return failure("cancelled", "Training cancelled; last completed preview retained.", **latest)
        if proc.returncode != 0:
            with log_path.open("rb") as log:
                log.seek(max(0, log_path.stat().st_size - 8192))
                detail = log.read().decode("utf-8", errors="replace")
            return failure("brush_failed", detail or f"Brush exited with {proc.returncode}",
                           returncode=proc.returncode, log_path=str(log_path), **latest)
        if not latest or latest["iteration"] != max_steps:
            return failure("invalid_brush_output", "Brush did not produce a complete valid final Gaussian PLY.",
                           log_path=str(log_path), **latest)
        if progress:
            progress("training", 100, "Brush training complete", dict(latest))
        return {**base, "ok": True, **latest, "log_path": str(log_path)}
    except (OSError, ValueError) as exc:
        return failure("brush_failed", str(exc), **latest)
    finally:
        if proc is not None and (owned_group or proc.poll() is None):
            stop()
