"""Open3D-based mesh cleanup and metric scaling.

Scaling is dimension-critical. A shared ``recon_transform`` represents the
full reconstruction-to-millimetres similarity, including orientation and
translation. Two scale-only alternatives remain explicitly named:

* ``mm_per_unit`` — a **ratio**: multiply reconstruction-space lengths by this to get
  millimetres (``factor = mm_per_unit``). This is what a solved
  ``aicad.reveng_session/1`` ``session.scale.mm_per_unit`` carries.
* ``target_longest_mm`` — an **absolute length**: stretch the mesh's longest bounding-box
  dimension to exactly this many millimetres (``factor = target_longest_mm / max_extent``).
  Only valid when the caller genuinely knows the part's overall size.

These options are mutually exclusive. Supplying none leaves the mesh in input
units — reported as ``scale_applied: False`` so no caller can mistake it for metric.
Scale-only transforms are always about the reconstruction origin, not the mesh centre.

Open3D (MIT) is imported in-process — ok per license guardrails.
"""
from __future__ import annotations

import math
from typing import Optional

import numpy as np

from .recon_transform import ReconTransform

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


def _as_transform(value) -> ReconTransform:
    if isinstance(value, ReconTransform):
        return value
    if isinstance(value, dict):
        return ReconTransform.from_json(value)
    raise ValueError("expected a ReconTransform or its JSON representation")


def _matrix(transform: ReconTransform):
    matrix = np.eye(4, dtype=np.float64)
    matrix[:3, :3] = transform.mm_per_unit * np.asarray(transform.rotation)
    matrix[:3, 3] = transform.translation_mm
    return matrix


def _transform_geometry(geometry, transform, *, points=False):
    geometry.transform(_matrix(transform))
    # Open3D transforms normals with the full scaled matrix. Similarity
    # transforms rotate normals but must not change their lengths.
    if transform.mm_per_unit != 1:
        names = ("normals",) if points else ("vertex_normals", "triangle_normals")
        for name in names:
            normals = np.asarray(getattr(geometry, name))
            normals /= transform.mm_per_unit


def _apply_to_file(in_path, transform, out_path, *, points):
    if not HAVE_OPEN3D:
        return {"ok": False, "reason": "open3d not installed", "hint": _OPEN3D_HINT}
    try:
        transform = _as_transform(transform)
        if points:
            geometry = o3d.io.read_point_cloud(str(in_path))
            if len(geometry.points) == 0:
                raise ValueError("point cloud contains no points")
            _transform_geometry(geometry, transform, points=True)
            written = o3d.io.write_point_cloud(str(out_path), geometry)
        else:
            geometry = o3d.io.read_triangle_mesh(str(in_path))
            if len(geometry.vertices) == 0 or len(geometry.triangles) == 0:
                raise ValueError("mesh contains no vertices or triangles")
            _transform_geometry(geometry, transform)
            written = o3d.io.write_triangle_mesh(str(out_path), geometry)
        if written is False:
            raise OSError("Open3D could not write the output")
        return {"ok": True, "point_path" if points else "mesh_path": str(out_path),
                "recon_transform": transform.to_json()}
    except (OSError, ValueError, RuntimeError) as exc:
        return {"ok": False, "reason": "transform_failed", "hint": str(exc)}


def apply_transform_to_mesh(mesh_path, transform, out_path):
    """Write a mesh transformed from its native gauge to millimetres."""
    return _apply_to_file(mesh_path, transform, out_path, points=False)


def apply_transform_to_ply_points(points_path, transform, out_path):
    """Write an ordinary point-cloud PLY in millimetres, retaining colors/normals.

    Gaussian PLYs have additional properties; use the Gaussian codec for them.
    """
    return _apply_to_file(points_path, transform, out_path, points=True)



def cleanup_mesh(
    mesh_path: str,
    out_path: str,
    *,
    mm_per_unit: Optional[float] = None,
    target_longest_mm: Optional[float] = None,
    recon_transform: Optional[ReconTransform] = None,
) -> dict:
    """Load, clean, optionally scale, and save a mesh using Open3D.

    Steps:
    - Remove degenerate triangles
    - Remove duplicated vertices
    - Remove unreferenced vertices
    - Apply the supplied similarity transform or one of the scale ratios

    ``mm_per_unit`` multiplies every length by that ratio (reconstruction units → mm).
    ``target_longest_mm`` instead normalises the longest bbox extent to that many mm.
    ``recon_transform`` additionally supports rotation and translation. All three
    are mutually exclusive, and all transform about the reconstruction origin.
    Pass none to skip scaling and keep the mesh in its input units.

    Returns ``{ok, mesh_path, n_vertices, n_triangles, scale_applied, scale_mode, notes}``
    or ``{ok: False, reason, hint}`` when open3d is unavailable or the units are ambiguous.
    """
    if sum(value is not None for value in (mm_per_unit, target_longest_mm, recon_transform)) > 1:
        return {
            "ok": False,
            "reason": "ambiguous scale: mm_per_unit, target_longest_mm and recon_transform are mutually exclusive",
            "hint": "pass one reconstruction-to-millimetres transform or scale",
        }
    for _name, _value in (("mm_per_unit", mm_per_unit), ("target_longest_mm", target_longest_mm)):
        if _value is None:
            continue
        try:
            _value = float(_value)
        except (TypeError, ValueError):
            return {
                "ok": False,
                "reason": f"{_name} must be a number, got {_value!r}",
                "hint": f"pass a positive float for {_name}, or None to skip scaling",
            }
        if not math.isfinite(_value) or not _value > 0.0:
            return {
                "ok": False,
                "reason": f"{_name} must be positive, got {_value}",
                "hint": "a zero or negative factor would collapse or mirror the mesh; "
                        "pass None to leave the mesh in its input units",
            }

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
    scale_mode = None
    if mm_per_unit is not None or target_longest_mm is not None or recon_transform is not None:
        try:
            if recon_transform is not None:
                transform = _as_transform(recon_transform)
                scale_mode = "recon_transform"
            else:
                if mm_per_unit is not None:
                    factor = float(mm_per_unit)
                    scale_mode = "mm_per_unit"
                else:
                    extent = mesh.get_axis_aligned_bounding_box().get_extent()
                    max_dim = max(float(extent[0]), float(extent[1]), float(extent[2]))
                    if not math.isfinite(max_dim) or max_dim <= 1e-9:
                        raise ValueError("mesh bounding box is degenerate (zero extent)")
                    factor = float(target_longest_mm) / max_dim
                    scale_mode = "target_longest_mm"
                identity = ReconTransform.identity()
                transform = ReconTransform(factor, identity.rotation, identity.translation_mm,
                                           source=scale_mode)
            _transform_geometry(mesh, transform)
            scale_applied = True
        except (ValueError, RuntimeError) as exc:
            return {"ok": False, "reason": "transform_failed", "hint": str(exc)}
    else:
        notes.append(
            "mesh left in input units: neither mm_per_unit nor target_longest_mm was supplied"
        )

    try:
        if o3d.io.write_triangle_mesh(out_path, mesh) is False:
            raise OSError("Open3D could not write the output")
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
        "scale_mode": scale_mode,
    }
    if notes:
        result["notes"] = notes
    return result
