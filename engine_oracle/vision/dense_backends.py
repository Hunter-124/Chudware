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

  Brush    — trains a 3D Gaussian Splat, then exports a mesh (TSDF/Poisson).
             Triggered by BRUSH_BIN env var (path to the brush binary).
             Input:  COLMAP sparse model dir
             Output: splat/ (.ply) and mesh/ under the session root.

When the required binary env/path is absent, both functions return the degradation
envelope  {"ok": false, "reason": "backend_not_provisioned", ...}  — never a traceback.
"""
from __future__ import annotations

import os
import subprocess
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
    export_mesh: bool = True,
) -> dict:
    """Run the Brush 3DGS trainer on a COLMAP sparse model and export a mesh.

    Args:
        sparse_dir:   Path to the COLMAP sparse model directory.
        session_dir:  Session root.  Splat goes to <session_dir>/splat/,
                      mesh to <session_dir>/mesh/.
        max_steps:    Training iterations (default 30 000).
        export_mesh:  If True (default) export a mesh via TSDF/Poisson after training.

    Returns:
        On success:  {"ok": True, "splat_path": <str>, "mesh_path": <str|None>, "backend": "brush"}
        On failure:  {"ok": False, "reason": ..., "hint": ..., "backend": "brush"}
    """
    brush_bin = os.environ.get("BRUSH_BIN", "")
    if not brush_bin:
        return {**_not_provisioned("set BRUSH_BIN to the Brush binary path"), "backend": "brush"}

    splat_dir = os.path.join(session_dir, "splat")
    mesh_dir = os.path.join(session_dir, "mesh")
    os.makedirs(splat_dir, exist_ok=True)
    os.makedirs(mesh_dir, exist_ok=True)

    splat_ply = os.path.join(splat_dir, "splat.ply")
    mesh_ply = os.path.join(mesh_dir, "brush_mesh.ply")

    # Train the Gaussian splat
    train_cmd = [
        brush_bin,
        "train",
        "--source-path", sparse_dir,
        "--model-path", splat_dir,
        "--output-ply", splat_ply,
        "--iterations", str(max_steps),
    ]
    ok, err = _run(train_cmd)
    if not ok:
        return {"ok": False, "reason": f"Brush train failed: {err}", "hint": "check BRUSH_BIN and sparse_dir", "backend": "brush"}

    mesh_path: Optional[str] = None
    if export_mesh:
        export_cmd = [
            brush_bin,
            "export-mesh",
            "--splat", splat_ply,
            "--output", mesh_ply,
            "--method", "tsdf",
        ]
        ok, err = _run(export_cmd)
        if not ok:
            # Non-fatal: splat succeeded; mesh export failed
            return {
                "ok": True,
                "splat_path": splat_ply,
                "mesh_path": None,
                "mesh_warning": f"Brush mesh export failed: {err}",
                "backend": "brush",
            }
        mesh_path = mesh_ply

    return {"ok": True, "splat_path": splat_ply, "mesh_path": mesh_path, "backend": "brush"}


def brush_commands(
    sparse_dir: str,
    session_dir: str,
    *,
    max_steps: int = 30_000,
    export_mesh: bool = True,
) -> dict:
    """Return the exact subprocess command lines for the Brush pipeline without running them.

    Returns:
        {"ok": True, "commands": [list-of-args, ...], "backend": "brush"}
        {"ok": False, "reason": "backend_not_provisioned", ...} when BRUSH_BIN unset.
    """
    brush_bin = os.environ.get("BRUSH_BIN", "")
    if not brush_bin:
        return {**_not_provisioned("set BRUSH_BIN to the Brush binary path"), "backend": "brush"}

    splat_dir = os.path.join(session_dir, "splat")
    mesh_dir = os.path.join(session_dir, "mesh")
    splat_ply = os.path.join(splat_dir, "splat.ply")
    mesh_ply = os.path.join(mesh_dir, "brush_mesh.ply")

    commands = [
        [brush_bin, "train",
         "--source-path", sparse_dir,
         "--model-path", splat_dir,
         "--output-ply", splat_ply,
         "--iterations", str(max_steps)],
    ]
    if export_mesh:
        commands.append([
            brush_bin, "export-mesh",
            "--splat", splat_ply,
            "--output", mesh_ply,
            "--method", "tsdf",
        ])
    return {"ok": True, "commands": commands, "backend": "brush"}
