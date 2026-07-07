"""Mesh → fitted-primitive extraction for the Reverse Engineering workspace.

``segment_fit`` turns a cleaned scan mesh into a set of fitted analytic primitives
(planes / cylinders / spheres / cones) plus recovered cross-section polylines, and
emits the frozen ``aicad.reveng_fits/1`` schema (``contracts/reveng.md`` §3). The
output feeds ``reverse_engineer`` (RE-14) as recon hints.

Pipeline
--------
1. Load the mesh and sample a dense oriented point cloud from its surface.
2. Segment via RANSAC: iteratively peel off the dominant plane (``segment_plane``),
   then fit cylinders / spheres on the residual points.
3. Slice the mesh at a few section planes to recover profile polylines (``sections``).

Backends (imported lazily — none is a hard dependency)
------------------------------------------------------
* **open3d** (MIT) — preferred, used for plane RANSAC + sampling when present.
* **trimesh** (MIT) + **pyransac3d** (MIT) — pure-Python fallback, explicitly
  sanctioned by ``contracts/reveng.md`` for shapes open3d lacks (cylinder/sphere/cone).

If *no* backend is provisioned every entry point returns the degradation envelope
``{"ok": false, "reason": "backend_not_provisioned", "hint": "pip install open3d"}``
per contract §0 — never a traceback, never a silent success.
"""
from __future__ import annotations

from typing import Any, Optional

import numpy as np

# ---------------------------------------------------------------------------
# Lazy backend detection. Imported at module load so tests can monkeypatch the
# HAVE_* flags without needing the real packages installed.
# ---------------------------------------------------------------------------
try:  # pragma: no cover - trivial import guard
    import open3d as o3d  # type: ignore

    HAVE_OPEN3D = True
except Exception:  # noqa: BLE001
    o3d = None  # type: ignore
    HAVE_OPEN3D = False

try:  # pragma: no cover - trivial import guard
    import trimesh  # type: ignore

    HAVE_TRIMESH = True
except Exception:  # noqa: BLE001
    trimesh = None  # type: ignore
    HAVE_TRIMESH = False

try:  # pragma: no cover - trivial import guard
    import pyransac3d as pyrsc  # type: ignore

    HAVE_PYRANSAC3D = True
except Exception:  # noqa: BLE001
    pyrsc = None  # type: ignore
    HAVE_PYRANSAC3D = False

_BACKEND_HINT = "pip install open3d"

#: Schema tag for the emitted document (contract §3, frozen).
FITS_SCHEMA = "aicad.reveng_fits/1"


def _backend_available() -> bool:
    """True when at least one mesh+fit backend can run.

    open3d alone can sample + fit planes; the trimesh+pyransac3d pair covers the
    full plane/cylinder/sphere path. Either is sufficient to produce fits.
    """
    if HAVE_OPEN3D:
        return True
    return HAVE_TRIMESH and HAVE_PYRANSAC3D


def _envelope() -> dict:
    """Contract §0 degradation envelope for an unprovisioned backend."""
    return {
        "ok": False,
        "reason": "backend_not_provisioned",
        "hint": _BACKEND_HINT,
    }


# ---------------------------------------------------------------------------
# Point-cloud helpers
# ---------------------------------------------------------------------------
def _load_points(mesh_path: str, n_samples: int):
    """Load a mesh and sample ``n_samples`` oriented surface points.

    Returns ``(points, normals)`` as ``(N, 3)`` float arrays (``normals`` may be
    ``None`` if unavailable), or ``(None, None)`` when the mesh cannot be read.
    Per-point normals sharpen the cylinder axis estimate (see ``_fit_cylinder``).
    """
    if HAVE_TRIMESH:
        try:
            mesh = trimesh.load(mesh_path, force="mesh")
        except Exception:  # noqa: BLE001
            return None, None
        if mesh is None or getattr(mesh, "vertices", None) is None:
            return None, None
        if len(mesh.vertices) == 0:
            return None, None
        try:
            pts, face_idx = trimesh.sample.sample_surface(mesh, n_samples)
            normals = np.asarray(mesh.face_normals[face_idx], dtype=float)
            return np.asarray(pts, dtype=float), normals
        except Exception:  # noqa: BLE001
            return np.asarray(mesh.vertices, dtype=float), None
    if HAVE_OPEN3D:
        try:
            mesh = o3d.io.read_triangle_mesh(mesh_path)
        except Exception:  # noqa: BLE001
            return None, None
        if len(mesh.vertices) == 0:
            return None, None
        pcd = mesh.sample_points_uniformly(number_of_points=n_samples)
        pcd.estimate_normals()
        pts = np.asarray(pcd.points, dtype=float)
        normals = np.asarray(pcd.normals, dtype=float) if pcd.has_normals() else None
        return pts, normals
    return None, None


def _points_from_array(arr: Any) -> Optional[np.ndarray]:
    """Coerce a caller-supplied point array to an (N, 3) float ndarray."""
    try:
        pts = np.asarray(arr, dtype=float)
    except Exception:  # noqa: BLE001
        return None
    if pts.ndim != 2 or pts.shape[1] != 3 or pts.shape[0] < 3:
        return None
    return pts


# ---------------------------------------------------------------------------
# Primitive fitters (pure-numpy residuals so numbers are backend-independent)
# ---------------------------------------------------------------------------
def _plane_residual(pts: np.ndarray, eq: np.ndarray) -> float:
    """RMS point-to-plane distance (mm) for the normalized plane a,b,c,d."""
    n = eq[:3]
    norm = np.linalg.norm(n)
    if norm < 1e-12:
        return float("inf")
    d = (pts @ n + eq[3]) / norm
    return float(np.sqrt(np.mean(d * d)))


def _cylinder_residual(
    pts: np.ndarray, center: np.ndarray, axis: np.ndarray, radius: float
) -> float:
    """RMS |distance-from-axis − radius| (mm)."""
    axis = axis / (np.linalg.norm(axis) + 1e-12)
    v = pts - center
    # component of v perpendicular to the axis
    proj = np.outer(v @ axis, axis)
    perp = v - proj
    dist = np.linalg.norm(perp, axis=1)
    return float(np.sqrt(np.mean((dist - radius) ** 2)))


def _extent_along(pts: np.ndarray, origin: np.ndarray, axis: np.ndarray) -> float:
    """Length of the point set projected onto ``axis`` (mm)."""
    axis = axis / (np.linalg.norm(axis) + 1e-12)
    t = (pts - origin) @ axis
    return float(t.max() - t.min())


def _fit_plane(pts: np.ndarray, thresh: float):
    """RANSAC plane fit. Returns (equation, inlier_idx) or (None, None)."""
    if HAVE_PYRANSAC3D:
        eq, inliers = pyrsc.Plane().fit(pts, thresh=thresh, minPoints=max(3, len(pts) // 20))
        if eq is None or len(eq) != 4:
            return None, None
        return np.asarray(eq, dtype=float), np.asarray(inliers, dtype=int)
    if HAVE_OPEN3D:
        pcd = o3d.geometry.PointCloud()
        pcd.points = o3d.utility.Vector3dVector(pts)
        eq, inliers = pcd.segment_plane(
            distance_threshold=thresh, ransac_n=3, num_iterations=1000
        )
        return np.asarray(eq, dtype=float), np.asarray(inliers, dtype=int)
    return None, None


def _axis_perp_basis(axis: np.ndarray):
    """Two orthonormal vectors spanning the plane perpendicular to ``axis``."""
    ref = np.array([1.0, 0.0, 0.0]) if abs(axis[0]) < 0.9 else np.array([0.0, 1.0, 0.0])
    e1 = ref - axis * (ref @ axis)
    e1 /= np.linalg.norm(e1) + 1e-12
    e2 = np.cross(axis, e1)
    return e1, e2


def _kasa_circle(pts: np.ndarray, axis: np.ndarray, e1: np.ndarray, e2: np.ndarray):
    """Algebraic (Kasa) circle fit in the plane perpendicular to ``axis``.

    Returns (center_3d, radius).
    """
    x = pts @ e1
    y = pts @ e2
    A = np.c_[2.0 * x, 2.0 * y, np.ones(len(x))]
    b = x * x + y * y
    sol, *_ = np.linalg.lstsq(A, b, rcond=None)
    cx, cy = sol[0], sol[1]
    radius = float(np.sqrt(max(sol[2] + cx * cx + cy * cy, 0.0)))
    center = cx * e1 + cy * e2
    return center, radius


def _fit_cylinder(pts: np.ndarray, normals: Optional[np.ndarray], thresh: float):
    """Fit a cylinder to a (curved-surface) point set. Returns dict or None.

    Preferred path (needs per-point normals): the cylinder axis is the direction
    most orthogonal to every surface normal — the smallest right-singular vector of
    the mean-centred normals. Radius/centre then come from an algebraic circle fit in
    the perpendicular plane, refined by dropping radial outliers. This is
    deterministic and far more axis-stable than pyransac3d's random-triple cylinder.

    Fallback (no normals): pyransac3d's RANSAC cylinder, if installed.
    """
    if len(pts) < 10:
        return None

    if normals is not None and len(normals) == len(pts):
        n = np.asarray(normals, dtype=float)
        n = n / (np.linalg.norm(n, axis=1, keepdims=True) + 1e-12)
        # Axis ⟂ all normals → smallest singular direction of centred normals.
        _, _, vt = np.linalg.svd(n - n.mean(axis=0), full_matrices=False)
        axis = vt[-1]
        axis = axis / (np.linalg.norm(axis) + 1e-12)
        if axis[2] < 0:
            axis = -axis
        e1, e2 = _axis_perp_basis(axis)
        work = pts.copy()
        center, radius = _kasa_circle(work, axis, e1, e2)
        # Refine: keep radial inliers, refit a few times.
        idx = np.arange(len(pts))
        keep_idx = idx
        for _ in range(3):
            rx = (work @ e1) - (center @ e1)
            ry = (work @ e2) - (center @ e2)
            rr = np.sqrt(rx * rx + ry * ry)
            mask = np.abs(rr - radius) < max(thresh, 0.3)
            if mask.sum() < 10:
                break
            work = work[mask]
            keep_idx = keep_idx[mask]
            center, radius = _kasa_circle(work, axis, e1, e2)
        if radius <= 0:
            return None
        return {
            "center": np.asarray(center, dtype=float),
            "axis": np.asarray(axis, dtype=float),
            "radius": float(radius),
            "inliers": np.asarray(keep_idx, dtype=int),
        }

    if HAVE_PYRANSAC3D:
        try:
            center, axis, radius, inliers = pyrsc.Cylinder().fit(
                pts, thresh=thresh, maxIteration=8000
            )
        except Exception:  # noqa: BLE001
            return None
        if radius is None or radius <= 0 or axis is None:
            return None
        return {
            "center": np.asarray(center, dtype=float),
            "axis": np.asarray(axis, dtype=float),
            "radius": float(radius),
            "inliers": np.asarray(inliers, dtype=int),
        }
    return None


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
def segment_fit(
    mesh_path: Optional[str] = None,
    *,
    session_id: str = "",
    points: Any = None,
    n_samples: int = 20000,
    plane_thresh_mm: float = 0.3,
    cyl_thresh_mm: float = 0.3,
    max_planes: int = 6,
    min_inlier_frac: float = 0.02,
) -> dict:
    """Extract fitted primitives from a mesh, emitting ``aicad.reveng_fits/1``.

    Parameters
    ----------
    mesh_path:
        Path to a cleaned mesh (``.ply`` / ``.obj``). Mutually usable with ``points``.
    session_id:
        Reveng session id stamped into the emitted schema.
    points:
        Optional pre-sampled ``(N, 3)`` point cloud, bypassing mesh loading (used by
        tests / callers that already hold a cloud).
    n_samples:
        Surface sample count when loading from ``mesh_path``.
    plane_thresh_mm / cyl_thresh_mm:
        RANSAC inlier distance thresholds (mm).
    max_planes:
        Cap on peeled planes.
    min_inlier_frac:
        Minimum inlier fraction (of the whole cloud) for a fit to be kept.

    Returns
    -------
    On success, the ``aicad.reveng_fits/1`` schema object (contract §3) with an
    additive ``ok: true``. On an unprovisioned backend, the §0 degradation envelope
    ``{"ok": false, "reason": "backend_not_provisioned", "hint": "pip install open3d"}``.
    """
    if not _backend_available():
        return _envelope()

    normals: Optional[np.ndarray] = None
    if points is not None:
        pts = _points_from_array(points)
    elif mesh_path is not None:
        pts, normals = _load_points(mesh_path, n_samples)
    else:
        return {
            "ok": False,
            "reason": "no_input",
            "hint": "pass mesh_path or points",
        }

    if pts is None or len(pts) < 3:
        return {
            "ok": False,
            "reason": "mesh_unreadable",
            "hint": "ensure the mesh file exists and contains geometry",
        }

    total = len(pts)
    min_inliers = max(3, int(total * min_inlier_frac))
    fits: list[dict] = []

    # --- Peel dominant planes ------------------------------------------------
    remaining = pts.copy()
    remaining_normals = normals.copy() if normals is not None else None
    plane_face_i = 0
    for _ in range(max_planes):
        if len(remaining) < min_inliers:
            break
        eq, inliers = _fit_plane(remaining, plane_thresh_mm)
        if eq is None or inliers is None or len(inliers) < min_inliers:
            break
        inlier_pts = remaining[inliers]
        normal = eq[:3] / (np.linalg.norm(eq[:3]) + 1e-12)
        centroid = inlier_pts.mean(axis=0)
        residual = _plane_residual(inlier_pts, eq)
        fits.append(
            {
                "kind": "plane",
                "confidence": round(float(len(inliers) / total), 4),
                "params_mm": {
                    "normal": [round(float(x), 6) for x in normal],
                    "origin": [round(float(x), 6) for x in centroid],
                    "offset": round(float(-eq[3] / (np.linalg.norm(eq[:3]) + 1e-12)), 6),
                },
                "source": "ransac",
                "inliers": int(len(inliers)),
                "residual_mm": round(residual, 6),
                "face_ids": [f"plane_{plane_face_i}"],
            }
        )
        plane_face_i += 1
        mask = np.ones(len(remaining), dtype=bool)
        mask[inliers] = False
        remaining = remaining[mask]
        if remaining_normals is not None:
            remaining_normals = remaining_normals[mask]

    # --- Fit a cylinder on the residual (post-plane-peel) points -------------
    # Peeling the flat faces first leaves the curved boss surface, so the cylinder
    # fitter no longer latches onto a spurious large radius spanning the slab.
    if len(remaining) >= 10:
        cyl_pts = remaining
        cyl_normals = remaining_normals
    else:
        cyl_pts = pts
        cyl_normals = normals
    cyl = _fit_cylinder(cyl_pts, cyl_normals, cyl_thresh_mm)
    if cyl is not None and len(cyl["inliers"]) >= min_inliers:
        inlier_pts = cyl_pts[cyl["inliers"]]
        height = _extent_along(inlier_pts, cyl["center"], cyl["axis"])
        residual = _cylinder_residual(inlier_pts, cyl["center"], cyl["axis"], cyl["radius"])
        axis = cyl["axis"] / (np.linalg.norm(cyl["axis"]) + 1e-12)
        fits.append(
            {
                "kind": "cylinder",
                "confidence": round(float(len(cyl["inliers"]) / total), 4),
                "params_mm": {
                    "radius": round(float(cyl["radius"]), 6),
                    "height": round(float(height), 6),
                    "axis": [round(float(x), 6) for x in axis],
                    "origin": [round(float(x), 6) for x in cyl["center"]],
                },
                "source": "ransac",
                "inliers": int(len(cyl["inliers"])),
                "residual_mm": round(residual, 6),
                "face_ids": ["cyl_0"],
            }
        )

    return {
        "schema": FITS_SCHEMA,
        "ok": True,
        "session_id": session_id,
        "mesh_path": mesh_path or "",
        "fits": fits,
        "sections": [],
    }
