"""Open3D-based mesh cleanup and metric scaling.

Scaling is the dimension-critical seam here, so the two possible meanings of "scale this
mesh to millimetres" are separate, explicitly-named parameters rather than one ambiguous
number:

* ``mm_per_unit`` — a **ratio**: multiply reconstruction-space lengths by this to get
  millimetres (``factor = mm_per_unit``). This is what a solved
  ``aicad.reveng_session/1`` ``session.scale.mm_per_unit`` carries.
* ``target_longest_mm`` — an **absolute length**: stretch the mesh's longest bounding-box
  dimension to exactly this many millimetres (``factor = target_longest_mm / max_extent``).
  Only valid when the caller genuinely knows the part's overall size.

They are mutually exclusive; supplying both is a caller bug and returns the honest
``{ok: False, reason, hint}`` envelope. Supplying neither leaves the mesh in its input
units — reported as ``scale_applied: False`` so no caller can mistake it for metric.

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
    mm_per_unit: Optional[float] = None,
    target_longest_mm: Optional[float] = None,
) -> dict:
    """Load, clean, optionally scale, and save a mesh using Open3D.

    Steps:
    - Remove degenerate triangles
    - Remove duplicated vertices
    - Remove unreferenced vertices
    - Scale, per exactly one of the two mutually-exclusive units below (or not at all)

    ``mm_per_unit`` multiplies every length by that ratio (reconstruction units → mm).
    ``target_longest_mm`` instead normalises the longest bbox extent to that many mm.
    Pass neither to skip scaling and keep the mesh in its input units.

    Returns ``{ok, mesh_path, n_vertices, n_triangles, scale_applied, scale_mode, notes}``
    or ``{ok: False, reason, hint}`` when open3d is unavailable or the units are ambiguous.
    """
    if mm_per_unit is not None and target_longest_mm is not None:
        return {
            "ok": False,
            "reason": "ambiguous scale: mm_per_unit and target_longest_mm are mutually exclusive",
            "hint": "pass mm_per_unit for a reconstruction-units→mm ratio, or "
                    "target_longest_mm to stretch the longest bbox dimension — never both",
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
        if not _value > 0.0:
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
    if mm_per_unit is not None or target_longest_mm is not None:
        try:
            aabb = mesh.get_axis_aligned_bounding_box()
            if mm_per_unit is not None:
                factor = float(mm_per_unit)
                scale_mode = "mm_per_unit"
            else:
                extent = aabb.get_extent()
                max_dim = max(float(extent[0]), float(extent[1]), float(extent[2]))
                if max_dim <= 1e-9:
                    raise ValueError("mesh bounding box is degenerate (zero extent)")
                factor = float(target_longest_mm) / max_dim
                scale_mode = "target_longest_mm"
            mesh.scale(factor, center=aabb.get_center())
            scale_applied = True
        except Exception as exc:  # noqa: BLE001
            scale_mode = None
            notes.append(f"scale skipped: {exc}")
    else:
        notes.append(
            "mesh left in input units: neither mm_per_unit nor target_longest_mm was supplied"
        )

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
        "scale_mode": scale_mode,
    }
    if notes:
        result["notes"] = notes
    return result
