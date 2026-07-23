"""Open3D-based mesh cleanup and fiducial scale normalization.

Open3D (MIT) is imported in-process — ok per license guardrails.
"""
from __future__ import annotations

from typing import Optional

try:
    import open3d as o3d  # type: ignore
    HAVE_OPEN3D = True
except Exception:  # noqa: BLE001
    o3d = None  # type: ignore
    HAVE_OPEN3D = False

_OPEN3D_HINT = "pip install open3d"

# Threshold below which we warn about low-confidence reconstruction
_LOW_VERTEX_THRESHOLD = 500


class MeshCleanupUnavailable(RuntimeError):
    """Raised when a cleanup op needs open3d but it is not installed."""

    def __init__(
        self,
        reason: str = "open3d not installed",
        hint: str = _OPEN3D_HINT,
    ):
        super().__init__(reason)
        self.reason = reason
        self.hint = hint


def cleanup_mesh(
    mesh_path: str,
    out_path: str,
    *,
    scale_mm: Optional[float] = None,
) -> dict:
    """Load, clean, optionally scale, and save a mesh using Open3D.

    Steps:
    - Remove degenerate triangles
    - Remove duplicated vertices
    - Remove unreferenced vertices
    - If scale_mm provided: scale longest bbox dimension to match

    Returns {ok, mesh_path, n_vertices, n_triangles, scale_applied, notes} or
    {ok: False, reason, hint} when open3d is unavailable.
    """
    if not HAVE_OPEN3D:
        return {
            "ok": False,
            "reason": "open3d not installed",
            "hint": _OPEN3D_HINT,
        }

    notes = []
    try:
        mesh = o3d.io.read_triangle_mesh(mesh_path)
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "reason": f"failed to read mesh: {exc}",
            "hint": "ensure the file is a valid .obj or .ply",
        }

    mesh.remove_degenerate_triangles()
    mesh.remove_duplicated_vertices()
    mesh.remove_unreferenced_vertices()

    scale_applied = False
    if scale_mm is not None:
        try:
            aabb = mesh.get_axis_aligned_bounding_box()
            extent = aabb.get_extent()
            max_dim = max(float(extent[0]), float(extent[1]), float(extent[2]))
            if max_dim > 1e-9:
                factor = scale_mm / max_dim
                mesh.scale(factor, center=aabb.get_center())
                scale_applied = True
        except Exception as exc:  # noqa: BLE001
            notes.append(f"scale normalization skipped: {exc}")

    try:
        o3d.io.write_triangle_mesh(out_path, mesh)
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "reason": f"failed to write cleaned mesh: {exc}",
            "hint": "check that out_path directory exists and is writable",
        }

    n_vertices = len(mesh.vertices)
    n_triangles = len(mesh.triangles)

    if n_vertices < _LOW_VERTEX_THRESHOLD:
        notes.append(
            f"low reconstruction confidence: only {n_vertices} vertices — the mesh may be "
            "sparse; consider capturing more photos or checking lighting conditions "
            "(shiny/reflective surfaces are photogrammetry adversarial)"
        )

    result: dict = {
        "ok": True,
        "mesh_path": out_path,
        "n_vertices": n_vertices,
        "n_triangles": n_triangles,
        "scale_applied": scale_applied,
    }
    if notes:
        result["notes"] = notes
    return result
