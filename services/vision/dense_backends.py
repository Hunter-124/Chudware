# AGPL NOTICE: OpenMVS is AGPL-3.0. It MUST be invoked via subprocess only.
# Never import or link against OpenMVS libraries. See contracts/reveng.md §0.1
# and CONTRIBUTING.md license guardrails. RE-26 owns the license_debt.md entry.
#
# Brush (3DGS trainer) is Apache-2.0 — subprocess only for consistency and isolation.
"""Dense reconstruction and mesh export backends for the RE pipeline.

Two backends, both subprocess-only:

  OpenMVS  — DensifyPointCloud → ReconstructMesh → RefineMesh → TextureMesh
             Triggered by OPENMVS_BIN env var (path to the OpenMVS bin directory
             containing DensifyPointCloud, ReconstructMesh, etc.).
             Input:  COLMAP sparse model dir (from RE-09 colmap_pipeline.py)
             Output: dense/ and mesh/ under the session root.

  Brush    — trains 3D Gaussians using pinned Brush v0.3.0 (no mesh export).
             Managed install or explicit BRUSH_BIN executable.
             Input:  standalone undistorted COLMAP dataset with model + images.
             Output: immutable, validated Gaussian PLY exports under splat/.

When the required binary env/path is absent, both functions return the degradation
envelope  {"ok": false, "reason": "backend_not_provisioned", ...}  — never a traceback.
"""
from __future__ import annotations

import os
import subprocess
import shutil
import signal
import tempfile
import time
from pathlib import Path

from .reveng_provision import brush_executable
from typing import Optional


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

def run_openmvs(
    sparse_dir: str,
    session_dir: str,
    *,
    working_resolution: int = 0,
    texture_size: int = 4096,
) -> dict:
    """Run the OpenMVS dense pipeline on a COLMAP sparse model.

    Args:
        sparse_dir:   Path to the COLMAP sparse model directory (contains cameras.bin
                      / images.bin / points3D.bin, as produced by RE-09
                      colmap_pipeline.py).
        session_dir:  Session root.  Outputs go to <session_dir>/dense/ and
                      <session_dir>/mesh/.
        working_resolution: Passed as --working-resolution to DensifyPointCloud
                      (0 = full, positive = cap in megapixels).
        texture_size: Max texture atlas size for TextureMesh.

    Returns:
        On success:  {"ok": True, "mesh_path": <str>, "backend": "openmvs"}
        On failure:  {"ok": False, "reason": ..., "hint": ..., "backend": "openmvs"}
    """
    bin_dir = os.environ.get("OPENMVS_BIN", "")
    if not bin_dir:
        return {**_not_provisioned("set OPENMVS_BIN to the OpenMVS bin directory"), "backend": "openmvs"}

    dense_dir = os.path.join(session_dir, "dense")
    mesh_dir = os.path.join(session_dir, "mesh")
    os.makedirs(dense_dir, exist_ok=True)
    os.makedirs(mesh_dir, exist_ok=True)

    # Intermediate file paths
    dense_mvs = os.path.join(dense_dir, "dense.mvs")
    dense_ply = os.path.join(dense_dir, "dense_dense.ply")
    mesh_mvs = os.path.join(dense_dir, "mesh.mvs")
    refined_mvs = os.path.join(dense_dir, "mesh_refine.mvs")
    textured_obj = os.path.join(mesh_dir, "mesh_texture.obj")

    def _bin(name: str) -> str:
        return os.path.join(bin_dir, name)

    # Step 1: DensifyPointCloud
    # InterfaceCOLMAP first converts the COLMAP sparse model to .mvs
    interface_cmd = [
        _bin("InterfaceCOLMAP"),
        "--input-path", sparse_dir,
        "--output-file", dense_mvs,
    ]
    ok, err = _run(interface_cmd)
    if not ok:
        return {"ok": False, "reason": f"InterfaceCOLMAP failed: {err}", "hint": "check OPENMVS_BIN and sparse_dir", "backend": "openmvs"}

    densify_cmd = [
        _bin("DensifyPointCloud"),
        "--input-file", dense_mvs,
        "--output-file", dense_ply,
        "--working-folder", dense_dir,
        "--working-resolution", str(working_resolution),
    ]
    ok, err = _run(densify_cmd)
    if not ok:
        return {"ok": False, "reason": f"DensifyPointCloud failed: {err}", "hint": "check OpenMVS GPU/CPU requirements", "backend": "openmvs"}

    # Step 2: ReconstructMesh
    reconstruct_cmd = [
        _bin("ReconstructMesh"),
        "--input-file", dense_ply,
        "--output-file", mesh_mvs,
        "--working-folder", dense_dir,
    ]
    ok, err = _run(reconstruct_cmd)
    if not ok:
        return {"ok": False, "reason": f"ReconstructMesh failed: {err}", "hint": "check input dense point cloud", "backend": "openmvs"}

    # Step 3: RefineMesh
    refine_cmd = [
        _bin("RefineMesh"),
        "--input-file", mesh_mvs,
        "--output-file", refined_mvs,
        "--working-folder", dense_dir,
    ]
    ok, err = _run(refine_cmd)
    if not ok:
        return {"ok": False, "reason": f"RefineMesh failed: {err}", "hint": "check mesh quality from ReconstructMesh", "backend": "openmvs"}

    # Step 4: TextureMesh
    texture_cmd = [
        _bin("TextureMesh"),
        "--input-file", refined_mvs,
        "--output-file", textured_obj,
        "--working-folder", dense_dir,
        "--export-type", "obj",
        "--texture-size-multiple", str(texture_size),
    ]
    ok, err = _run(texture_cmd)
    if not ok:
        return {"ok": False, "reason": f"TextureMesh failed: {err}", "hint": "check mesh and images", "backend": "openmvs"}

    return {"ok": True, "mesh_path": textured_obj, "backend": "openmvs"}


def openmvs_commands(
    sparse_dir: str,
    session_dir: str,
    *,
    working_resolution: int = 0,
    texture_size: int = 4096,
) -> dict:
    """Return the exact subprocess command lines for the OpenMVS pipeline without running them.

    Useful for inspection, testing, and dry-run displays.

    Returns:
        {"ok": True, "commands": [list-of-args, ...], "backend": "openmvs"}
        {"ok": False, "reason": "backend_not_provisioned", ...} when OPENMVS_BIN unset.
    """
    bin_dir = os.environ.get("OPENMVS_BIN", "")
    if not bin_dir:
        return {**_not_provisioned("set OPENMVS_BIN to the OpenMVS bin directory"), "backend": "openmvs"}

    dense_dir = os.path.join(session_dir, "dense")
    mesh_dir = os.path.join(session_dir, "mesh")
    dense_mvs = os.path.join(dense_dir, "dense.mvs")
    dense_ply = os.path.join(dense_dir, "dense_dense.ply")
    mesh_mvs = os.path.join(dense_dir, "mesh.mvs")
    refined_mvs = os.path.join(dense_dir, "mesh_refine.mvs")
    textured_obj = os.path.join(mesh_dir, "mesh_texture.obj")

    def _bin(name: str) -> str:
        return os.path.join(bin_dir, name)

    commands = [
        [_bin("InterfaceCOLMAP"), "--input-path", sparse_dir, "--output-file", dense_mvs],
        [_bin("DensifyPointCloud"), "--input-file", dense_mvs, "--output-file", dense_ply,
         "--working-folder", dense_dir, "--working-resolution", str(working_resolution)],
        [_bin("ReconstructMesh"), "--input-file", dense_ply, "--output-file", mesh_mvs,
         "--working-folder", dense_dir],
        [_bin("RefineMesh"), "--input-file", mesh_mvs, "--output-file", refined_mvs,
         "--working-folder", dense_dir],
        [_bin("TextureMesh"), "--input-file", refined_mvs, "--output-file", textured_obj,
         "--working-folder", dense_dir, "--export-type", "obj",
         "--texture-size-multiple", str(texture_size)],
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
        return {**base, **_not_provisioned("Run python scripts/provision_reveng.py or set BRUSH_BIN to an executable.")}
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

        def publish():
            nonlocal latest
            candidates = []
            for path in exports.glob("export_*.ply"):
                try:
                    iteration = int(path.stem.removeprefix("export_"))
                except ValueError:
                    continue
                if iteration not in seen:
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
