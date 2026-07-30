"""Mesh → fitted-primitive extraction for the Reverse Engineering workspace.

``segment_fit`` turns a cleaned scan mesh into fitted analytic primitives
(planes / cylinders / cones / spheres), attaches the feature semantics a
parametric rebuild actually needs (a cylinder is a *hole* or a *boss* or a
*shaft*, a plane is a *pocket* floor or a raised *pad*), recovers cross-section
profile polylines, and emits the frozen ``aicad.reveng_fits/1`` schema
(``contracts/reveng.md`` §3). The output feeds ``reverse_engineer`` (RE-14) as
recon hints — it is what lets a downstream agent author a bore / boss / revolve
/ loft instead of guessing at a triangle soup.

Pipeline
--------
1. Load the mesh and sample a dense oriented point cloud from its surface.
2. Peel the dominant planes with RANSAC (up to ``max_planes``), scoring each as
   ``pocket`` / ``pad`` / ``unknown`` from where the surrounding material sits.
   A patch whose residual curves is rejected rather than emitted: a threshold
   RANSAC plane will happily hug a wide band of any cylinder, cone or sphere.
3. Peel curved primitives off the residual. Cylinder, cone and sphere fitters
   *compete* every round and the one with the best MSAC support — inlier count
   weighted by how tightly it explains those inliers — is peeled, so a sphere is
   never shredded into a stack of spurious cylinder bands and a cone is never
   swallowed by the sphere that nearly covers it. Per-kind budgets:
   ``max_cylinders`` (default 8) / ``max_cones`` / ``max_spheres``.
4. Classify each cylinder from the sign of its surface normals about the axis —
   normals pointing *at* the axis mean the material is outside (a bore), normals
   pointing *away* mean the material is inside (a boss or a shaft) — and emit
   ``role`` / ``through`` / ``depth_mm`` alongside the geometry.
5. Slice the mesh at ``n_sections`` evenly spaced stations along the dominant
   (or caller-supplied) axis and emit the closed profile polylines with areas.

Surface sampling and every RANSAC here are seeded, so the same mesh yields the
same numbers on every run.

Fitter internals
----------------
* **Plane** — RANSAC (``pyransac3d.Plane`` or ``open3d.segment_plane``) plus the
  curvature guard of ``_plane_is_curved``.
* **Cylinder** — a two-point *oriented* RANSAC isolates one cylindrical patch out
  of a mixed residual (axis = ``n_i × n_j``, centre = the intersection of the two
  normal lines projected into the perpendicular plane), then the patch is refined
  with the normal-SVD axis estimate + Kasa algebraic circle fit. Without
  per-point normals it falls back to ``pyransac3d.Cylinder``.
* **Sphere** — RANSAC over 4-point algebraic fits of
  ``x²+y²+z² = 2cx·x + 2cy·y + 2cz·z + k``, then a least-squares refine on the
  inliers, then an optional geometric refine via ``scipy.optimize.least_squares``
  (accepted only when it lowers the RMS residual).
* **Cone** — a three-point RANSAC over normals isolates one conical patch, then
  the axis is the smallest right-singular vector of the *mean-centred* normals
  (a cone's normals lie on the plane ``n·d = −sinα``, so that is the plane's own
  normal). The axis *line* comes from a least-squares intersection of the
  projected normal lines, and apex + half-angle from a trimmed least-squares
  regression of radial distance against axial position.
* **Sections** — ``trimesh.Trimesh.section`` per station when trimesh is
  present, otherwise a manual triangle/plane intersection plus an edge-chaining
  walk over the raw ``(vertices, faces)`` arrays. Areas are shoelace sums with
  nesting parity, so a bore subtracts from its enclosing profile (no shapely).

Backends (imported lazily — none is a hard dependency)
------------------------------------------------------
* **open3d** (MIT) — mesh IO + point sampling + plane RANSAC when present.
* **trimesh** (MIT) — mesh IO + surface sampling + cross-sections.
* **pyransac3d** (MIT) — plane RANSAC and the no-normals cylinder fallback.
* **scipy** (BSD) — optional sphere refine and k-NN normal estimation for point
  clouds that arrive without normals.

A missing optional dependency never raises and never fabricates a fit: the
affected fitter is skipped and the reason is recorded in the additive ``notes``
list of the result. If *no* mesh backend at all is provisioned every entry point
returns the degradation envelope
``{"ok": false, "reason": "backend_not_provisioned", "hint": "pip install open3d"}``
per contract §0 — never a traceback, never a silent success.
"""
from __future__ import annotations

import math
import random
from collections import defaultdict
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

try:  # pragma: no cover - trivial import guard
    from scipy.optimize import least_squares as _scipy_least_squares  # type: ignore
    from scipy.spatial import cKDTree as _ScipyKDTree  # type: ignore

    HAVE_SCIPY = True
except Exception:  # noqa: BLE001
    _scipy_least_squares = None  # type: ignore
    _ScipyKDTree = None  # type: ignore
    HAVE_SCIPY = False

_BACKEND_HINT = "pip install open3d"

#: Schema tag for the emitted document (contract §3, frozen — append-only).
FITS_SCHEMA = "aicad.reveng_fits/1"

#: Capability revision of the *producer*, stamped additively next to the frozen
#: schema tag. 1 = planes + one cylinder. 2 = multi-cylinder / cone / sphere /
#: feature roles / real cross-sections. Consumers that only read v1 keys keep
#: working; the marker just tells a reader which fit kinds it may encounter.
FITS_REVISION = 2

#: Fixed RANSAC seed — identical input must yield identical fits run to run.
_RNG_SEED = 20260707

#: |n · axis| above which a surface normal is too axial to belong to a cylinder.
_CYL_AXIAL_REJECT = 0.4

#: Inlier-normal agreement bands for the hole/boss decision.
_ROLE_INWARD_MAX = 0.2
_ROLE_OUTWARD_MIN = 0.8

#: How many curved "planes" the peel may discard before it gives up on planes.
_MAX_CURVED_PLANE_REJECTS = 4

#: Cone normals sit at a fixed polar angle to the axis; |n·axis| = sin(half-angle).
#: The bounds exclude cylinders (0) and flat annuli (1); the tolerance is ~3°.
_CONE_SIN_MIN = 0.052
_CONE_SIN_MAX = 0.996
_CONE_NORMAL_TOL = 0.05


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


def _note(stage: str, reason: str, hint: str) -> dict:
    """One honest 'this capability was skipped, and why' record."""
    return {"stage": stage, "reason": reason, "hint": hint}


# ---------------------------------------------------------------------------
# Point-cloud helpers
# ---------------------------------------------------------------------------
def _unit_rows(v: np.ndarray) -> np.ndarray:
    """Row-wise normalize an (N, 3) array."""
    v = np.asarray(v, dtype=float)
    return v / (np.linalg.norm(v, axis=1, keepdims=True) + 1e-12)


def _stride_idx(n: int, cap: int) -> np.ndarray:
    """Deterministic ≤``cap`` subsample of ``range(n)`` (used for RANSAC scoring)."""
    if n <= cap:
        return np.arange(n)
    step = max(1, n // cap)
    return np.arange(0, n, step)[:cap]


def _load_mesh(mesh_path: str) -> Optional[dict]:
    """Load a mesh once, keeping the library object plus raw vertices/faces.

    Returns ``{"obj", "vertices", "faces", "backend"}`` or ``None`` when the file
    cannot be read. ``faces`` may be ``None`` for point-cloud-ish inputs; the
    library object is what the trimesh cross-section path needs.
    """
    if HAVE_TRIMESH:
        try:
            mesh = trimesh.load(mesh_path, force="mesh")
        except Exception:  # noqa: BLE001
            mesh = None
        # ``force="mesh"`` turns a vertices-only file into an EMPTY Trimesh rather than
        # raising: a 20k-point scan PLY loaded as 0 vertices / 0 faces, so segment_fit
        # refused the whole cloud with ``mesh_unreadable`` even though a point cloud is a
        # first-class scan input here (and the ``faces=None`` branch below exists for it).
        # Re-read unforced and keep the cloud; only the cross-sections need real faces.
        if mesh is None or len(np.asarray(getattr(mesh, "vertices", []), dtype=float)) == 0:
            try:
                mesh = trimesh.load(mesh_path)
            except Exception:  # noqa: BLE001
                return None
        if mesh is None or getattr(mesh, "vertices", None) is None:
            return None
        verts = np.asarray(mesh.vertices, dtype=float)
        if len(verts) == 0:
            return None
        raw_faces = getattr(mesh, "faces", None)
        faces = np.asarray(raw_faces, dtype=int) if raw_faces is not None and len(raw_faces) else None
        return {"obj": mesh, "vertices": verts, "faces": faces, "backend": "trimesh"}
    if HAVE_OPEN3D:
        try:
            mesh = o3d.io.read_triangle_mesh(mesh_path)
        except Exception:  # noqa: BLE001
            return None
        verts = np.asarray(mesh.vertices, dtype=float)
        if len(verts) == 0:
            return None
        tris = np.asarray(mesh.triangles, dtype=int)
        return {
            "obj": mesh,
            "vertices": verts,
            "faces": tris if len(tris) else None,
            "backend": "open3d",
        }
    return None


def _sample_mesh(handle: dict, n_samples: int):
    """Sample ``n_samples`` oriented surface points from a loaded mesh handle.

    Sampling is seeded: a scan pipeline that reported a different bore diameter on
    every run of the same mesh would be untrustworthy. Backends too old to accept a
    seed fall back to their unseeded call rather than failing.
    """
    mesh = handle["obj"]
    if handle["backend"] == "trimesh":
        try:
            try:
                pts, face_idx = trimesh.sample.sample_surface(mesh, n_samples, seed=_RNG_SEED)
            except TypeError:  # pragma: no cover - older trimesh without ``seed``
                pts, face_idx = trimesh.sample.sample_surface(mesh, n_samples)
            normals = np.asarray(mesh.face_normals[face_idx], dtype=float)
            return np.asarray(pts, dtype=float), normals
        except Exception:  # noqa: BLE001
            return handle["vertices"], None
    try:
        try:
            pcd = mesh.sample_points_uniformly(number_of_points=n_samples, seed=_RNG_SEED)
        except TypeError:  # pragma: no cover - older open3d without ``seed``
            pcd = mesh.sample_points_uniformly(number_of_points=n_samples)
        pcd.estimate_normals()
        pts = np.asarray(pcd.points, dtype=float)
        normals = np.asarray(pcd.normals, dtype=float) if pcd.has_normals() else None
        return pts, normals
    except Exception:  # noqa: BLE001
        return handle["vertices"], None


def _load_points(mesh_path: str, n_samples: int):
    """Load a mesh and sample ``n_samples`` oriented surface points.

    Returns ``(points, normals)`` as ``(N, 3)`` float arrays (``normals`` may be
    ``None`` if unavailable), or ``(None, None)`` when the mesh cannot be read.
    Per-point normals sharpen the cylinder axis estimate (see ``_fit_cylinder``)
    and are what makes the hole-vs-boss classification possible at all.
    """
    handle = _load_mesh(mesh_path)
    if handle is None:
        return None, None
    return _sample_mesh(handle, n_samples)


def _points_from_array(arr: Any) -> Optional[np.ndarray]:
    """Coerce a caller-supplied point array to an (N, 3) float ndarray."""
    try:
        pts = np.asarray(arr, dtype=float)
    except Exception:  # noqa: BLE001
        return None
    if pts.ndim != 2 or pts.shape[1] != 3 or pts.shape[0] < 3:
        return None
    return pts


def _estimate_normals(pts: np.ndarray, k: int = 16) -> Optional[np.ndarray]:
    """k-NN PCA surface normals for a bare point cloud (needs scipy's KD-tree).

    The result is *unoriented* — the sign of each normal is arbitrary — which is
    enough for axis estimation (every axis estimator here is sign-invariant) but
    not for the hole-vs-boss decision, so callers must keep them flagged.
    Returns ``None`` when scipy is absent or the cloud is too small.
    """
    if not HAVE_SCIPY or len(pts) < k:
        return None
    try:
        tree = _ScipyKDTree(pts)
        _, idx = tree.query(pts, k=k)
        nbr = pts[idx]
        nbr = nbr - nbr.mean(axis=1, keepdims=True)
        cov = np.einsum("nki,nkj->nij", nbr, nbr)
        _, vecs = np.linalg.eigh(cov)
    except Exception:  # noqa: BLE001
        return None
    return np.asarray(vecs[:, :, 0], dtype=float)


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


def _sphere_residual(pts: np.ndarray, center: np.ndarray, radius: float) -> float:
    """RMS |distance-from-centre − radius| (mm)."""
    d = np.linalg.norm(pts - center, axis=1) - radius
    return float(np.sqrt(np.mean(d * d)))


def _extent_along(pts: np.ndarray, origin: np.ndarray, axis: np.ndarray) -> float:
    """Length of the point set projected onto ``axis`` (mm)."""
    axis = axis / (np.linalg.norm(axis) + 1e-12)
    t = (pts - origin) @ axis
    return float(t.max() - t.min())


def _fit_plane(pts: np.ndarray, thresh: float):
    """RANSAC plane fit. Returns (equation, inlier_idx) or (None, None).

    ``pyransac3d`` samples from Python's *global* ``random``, so the fit would
    otherwise vary run to run for identical input. The global state is seeded and
    restored around the call to keep the emitted fits reproducible without
    trampling on the caller's RNG.
    """
    if HAVE_PYRANSAC3D:
        state = random.getstate()
        random.seed(_RNG_SEED)
        try:
            eq, inliers = pyrsc.Plane().fit(
                pts, thresh=thresh, minPoints=max(3, len(pts) // 20)
            )
        finally:
            random.setstate(state)
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


def _plane_is_curved(patch_pts: np.ndarray, eq: np.ndarray, thresh: float) -> bool:
    """True when a "plane" patch is really a curved strip of a cylinder/cone/sphere.

    A distance-threshold RANSAC plane happily hugs a wide band of any curved
    surface — the band stays inside the threshold, so inlier count alone cannot
    tell the two apart, and on a cone those bands outscore the genuine faces and
    shred the surface before the cone fitter ever sees it.

    Curvature can tell them apart, and scale-free: regress the signed
    point-to-plane residual on the in-plane coordinates, once with a linear model
    (absorbing any tilt in the RANSAC plane) and once with a quadratic. Sensor
    noise is not explainable by a quadratic; systematic curvature is explained by
    one almost entirely. The fraction of the linear model's residual variance that
    the quadratic term removes therefore separates the two regardless of how noisy
    the scan is — measured on this repo's fixtures, ≤0.12 for genuine faces and
    ≥0.75 for curved bands.
    """
    if len(patch_pts) < 12:
        return False
    n = eq[:3]
    norm = float(np.linalg.norm(n))
    if norm < 1e-12:
        return False
    n = n / norm
    d = patch_pts @ n + eq[3] / norm
    e1, e2 = _axis_perp_basis(n)
    u = patch_pts @ e1
    v = patch_pts @ e2
    u = (u - u.mean()) / (u.std() + 1e-12)
    v = (v - v.mean()) / (v.std() + 1e-12)
    ones = np.ones(len(u))
    linear = np.c_[u, v, ones]
    quadratic = np.c_[u * u, u * v, v * v, u, v, ones]
    res_lin = d - linear @ np.linalg.lstsq(linear, d, rcond=None)[0]
    var_lin = float(res_lin.var())
    # Already flat to well inside the threshold — no curvature to detect.
    if var_lin <= (0.1 * thresh) ** 2:
        return False
    res_quad = d - quadratic @ np.linalg.lstsq(quadratic, d, rcond=None)[0]
    return bool(1.0 - float(res_quad.var()) / var_lin > 0.5)


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

    This assumes ``pts`` already belongs to a *single* cylindrical patch — see
    ``_segment_cylinder`` for the step that isolates one patch out of a residual
    holding several.
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


def _segment_cylinder(
    pts: np.ndarray,
    normals: np.ndarray,
    thresh: float,
    rng: np.random.Generator,
    max_iter: int = 1200,
    sample_cap: int = 2000,
) -> Optional[np.ndarray]:
    """Isolate ONE cylindrical patch from a residual holding several.

    Two-point *oriented* RANSAC: two surface normals of the same cylinder span the
    plane perpendicular to its axis, so ``axis = n_i × n_j``; projected into that
    plane the axis point must lie on both normal lines, so intersecting them gives
    the centre and — as the distance travelled along the line — the radius. No
    radius search, two samples per hypothesis, and points from a *different*
    cylinder simply score as outliers.

    Returns a boolean inlier mask over ``pts``, or ``None`` when nothing scored.
    """
    n_pts = len(pts)
    if n_pts < 16 or normals is None or len(normals) != n_pts:
        return None
    nrm = _unit_rows(normals)
    sub = _stride_idx(n_pts, sample_cap)
    sp, sn = pts[sub], nrm[sub]
    span = pts.max(axis=0) - pts.min(axis=0)
    r_max = float(np.linalg.norm(span)) + 1e-9

    ii = rng.integers(0, n_pts, size=max_iter)
    jj = rng.integers(0, n_pts, size=max_iter)
    best: Optional[tuple] = None
    for a_i, b_i in zip(ii, jj):
        if a_i == b_i:
            continue
        n1 = nrm[a_i]
        n2 = nrm[b_i]
        axis = np.cross(n1, n2)
        na = float(np.linalg.norm(axis))
        if na < 0.15:  # near-parallel normals → the axis is numerically useless
            continue
        axis = axis / na
        e1, e2 = _axis_perp_basis(axis)
        q1 = np.array([pts[a_i] @ e1, pts[a_i] @ e2])
        q2 = np.array([pts[b_i] @ e1, pts[b_i] @ e2])
        m1 = np.array([n1 @ e1, n1 @ e2])
        m2 = np.array([n2 @ e1, n2 @ e2])
        l1 = float(np.linalg.norm(m1))
        l2 = float(np.linalg.norm(m2))
        if l1 < 1e-6 or l2 < 1e-6:
            continue
        m1 /= l1
        m2 /= l2
        den = m1[0] * m2[1] - m1[1] * m2[0]
        if abs(den) < 1e-6:
            continue
        dq = q2 - q1
        s = (dq[0] * m2[1] - dq[1] * m2[0]) / den
        radius = abs(float(s))
        if radius < 1e-3 or radius > r_max:
            continue
        c2 = q1 + s * m1
        x = (sp @ e1) - c2[0]
        y = (sp @ e2) - c2[1]
        rr = np.sqrt(x * x + y * y)
        hit = (np.abs(rr - radius) < thresh) & (np.abs(sn @ axis) < _CYL_AXIAL_REJECT)
        count = int(np.count_nonzero(hit))
        if best is None or count > best[0]:
            best = (count, axis, c2[0] * e1 + c2[1] * e2, radius)

    if best is None or best[0] < 8:
        return None
    _, axis, center, radius = best
    v = pts - center
    t = v @ axis
    rr = np.linalg.norm(v - np.outer(t, axis), axis=1)
    mask = (np.abs(rr - radius) < thresh) & (np.abs(nrm @ axis) < _CYL_AXIAL_REJECT)
    if np.count_nonzero(mask) < 10:
        return None
    return mask


def _fit_cylinder_patch(
    pts: np.ndarray,
    normals: Optional[np.ndarray],
    thresh: float,
    rng: np.random.Generator,
) -> Optional[dict]:
    """One cylinder candidate: segment a patch, refine it, re-select its inliers."""
    sub_pts = pts
    sub_normals = normals
    if normals is not None:
        mask = _segment_cylinder(pts, normals, thresh, rng)
        if mask is not None:
            sub_pts = pts[mask]
            sub_normals = normals[mask]

    cyl = _fit_cylinder(sub_pts, sub_normals, thresh)
    if cyl is None:
        return None
    axis = cyl["axis"] / (np.linalg.norm(cyl["axis"]) + 1e-12)
    center = np.asarray(cyl["center"], dtype=float)
    radius = float(cyl["radius"])
    if radius <= 0:
        return None

    # Re-select inliers against the refined cylinder over the whole working cloud
    # so a patch split by the segmentation step is re-absorbed in one go.
    v = pts - center
    t = v @ axis
    rr = np.linalg.norm(v - np.outer(t, axis), axis=1)
    keep = np.abs(rr - radius) < thresh
    if normals is not None:
        keep &= np.abs(_unit_rows(normals) @ axis) < _CYL_AXIAL_REJECT
    idx = np.flatnonzero(keep)
    if len(idx) < 10:
        return None
    return {
        "kind": "cylinder",
        "inliers": idx,
        "residual": _cylinder_residual(pts[idx], center, axis, radius),
        "source": "ransac",
        "center": center,
        "axis": axis,
        "radius": radius,
    }


def _algebraic_sphere(pts: np.ndarray):
    """Linear sphere fit: x²+y²+z² = 2cx·x + 2cy·y + 2cz·z + k, k = r² − |c|².

    Returns ``(center, radius)`` or ``None`` for a degenerate (near-coplanar) set.
    """
    if len(pts) < 4:
        return None
    A = np.c_[2.0 * pts, np.ones(len(pts))]
    b = np.einsum("ij,ij->i", pts, pts)
    try:
        sol, *_ = np.linalg.lstsq(A, b, rcond=None)
    except np.linalg.LinAlgError:  # pragma: no cover - numpy fallback path
        return None
    center = np.asarray(sol[:3], dtype=float)
    r2 = float(sol[3]) + float(center @ center)
    if not np.isfinite(r2) or r2 <= 0.0:
        return None
    return center, math.sqrt(r2)


def _refine_sphere(pts: np.ndarray, center: np.ndarray, radius: float):
    """Optional geometric (scipy) sphere refine; ``None`` when it cannot help."""
    if not HAVE_SCIPY or len(pts) < 8:
        return None

    def _resid(x):
        return np.linalg.norm(pts - x[:3], axis=1) - x[3]

    try:
        sol = _scipy_least_squares(_resid, np.r_[center, radius], max_nfev=200)
    except Exception:  # noqa: BLE001
        return None
    if not getattr(sol, "success", False):
        return None
    c_new = np.asarray(sol.x[:3], dtype=float)
    r_new = float(sol.x[3])
    if r_new <= 0 or not np.all(np.isfinite(c_new)):
        return None
    if _sphere_residual(pts, c_new, r_new) >= _sphere_residual(pts, center, radius):
        return None
    return c_new, r_new


def _fit_sphere(
    pts: np.ndarray,
    thresh: float,
    rng: np.random.Generator,
    max_iter: int = 400,
    sample_cap: int = 2000,
) -> Optional[dict]:
    """RANSAC + algebraic + least-squares sphere fit. Returns a candidate or None."""
    n_pts = len(pts)
    if n_pts < 16:
        return None
    sub = _stride_idx(n_pts, sample_cap)
    sp = pts[sub]
    span = pts.max(axis=0) - pts.min(axis=0)
    r_max = float(np.linalg.norm(span)) + 1e-9

    best: Optional[tuple] = None
    picks = rng.integers(0, n_pts, size=(max_iter, 4))
    for quad in picks:
        if len(np.unique(quad)) < 4:
            continue
        fit = _algebraic_sphere(pts[quad])
        if fit is None:
            continue
        center, radius = fit
        if radius <= 1e-3 or radius > r_max:
            continue
        dev = np.abs(np.linalg.norm(sp - center, axis=1) - radius)
        count = int(np.count_nonzero(dev < thresh))
        if best is None or count > best[0]:
            best = (count, center, radius)
    if best is None or best[0] < 8:
        return None

    _, center, radius = best
    # Least-squares refine on the inliers of the winning hypothesis.
    for _ in range(4):
        dev = np.abs(np.linalg.norm(pts - center, axis=1) - radius)
        keep = dev < thresh
        if int(np.count_nonzero(keep)) < 8:
            break
        fit = _algebraic_sphere(pts[keep])
        if fit is None:
            break
        center, radius = fit
    if radius <= 1e-3 or radius > r_max:
        return None

    dev = np.abs(np.linalg.norm(pts - center, axis=1) - radius)
    idx = np.flatnonzero(dev < thresh)
    if len(idx) < 8:
        return None
    refined = _refine_sphere(pts[idx], center, radius)
    source = "ransac"
    if refined is not None:
        center, radius = refined
        source = "ransac+scipy"
        dev = np.abs(np.linalg.norm(pts - center, axis=1) - radius)
        idx = np.flatnonzero(dev < thresh)
        if len(idx) < 8:
            return None
    return {
        "kind": "sphere",
        "inliers": idx,
        "residual": _sphere_residual(pts[idx], center, radius),
        "source": source,
        "center": np.asarray(center, dtype=float),
        "radius": float(radius),
    }


def _cone_from_axis(
    pts: np.ndarray, nrm: np.ndarray, axis: np.ndarray, thresh: float
) -> Optional[dict]:
    """Solve apex + half-angle for one candidate cone axis direction.

    With the direction fixed, the axis *line* is the least-squares intersection of
    the projected normal lines (every surface normal of a cone points radially away
    from the axis once the axial component is dropped). Then radial distance is an
    affine function of axial position — ``ρ = tanα·t + b`` — so a trimmed linear
    regression yields the half-angle and, from its root, the apex.
    """
    axis = axis / (np.linalg.norm(axis) + 1e-12)
    e1, e2 = _axis_perp_basis(axis)
    qx = pts @ e1
    qy = pts @ e2
    mx = nrm @ e1
    my = nrm @ e2
    ml = np.sqrt(mx * mx + my * my)
    ok = ml > 1e-3
    if int(np.count_nonzero(ok)) < 16:
        return None
    ux = mx[ok] / ml[ok]
    uy = my[ok] / ml[ok]
    pxx = 1.0 - ux * ux
    pxy = -ux * uy
    pyy = 1.0 - uy * uy
    A = np.array([[pxx.sum(), pxy.sum()], [pxy.sum(), pyy.sum()]])
    if abs(float(np.linalg.det(A))) < 1e-9:
        return None
    rhs = np.array(
        [
            float((pxx * qx[ok] + pxy * qy[ok]).sum()),
            float((pxy * qx[ok] + pyy * qy[ok]).sum()),
        ]
    )
    try:
        c2 = np.linalg.solve(A, rhs)
    except np.linalg.LinAlgError:  # pragma: no cover - guarded by det() above
        return None

    origin = c2[0] * e1 + c2[1] * e2
    v = pts - origin
    t = v @ axis
    rho = np.linalg.norm(v - np.outer(t, axis), axis=1)
    # Orient the axis so radius grows with t (apex behind, base ahead).
    if float((t - t.mean()) @ (rho - rho.mean())) < 0.0:
        axis = -axis
        t = -t

    keep = np.ones(len(pts), dtype=bool)
    slope = 0.0
    intercept = 0.0
    for _ in range(4):
        if int(np.count_nonzero(keep)) < 16:
            return None
        M = np.c_[t[keep], np.ones(int(np.count_nonzero(keep)))]
        sol, *_ = np.linalg.lstsq(M, rho[keep], rcond=None)
        slope = float(sol[0])
        intercept = float(sol[1])
        if slope <= 1e-6:
            return None
        cos_a = 1.0 / math.sqrt(1.0 + slope * slope)
        dev = np.abs(rho - (slope * t + intercept)) * cos_a
        nxt = dev < thresh
        if int(np.count_nonzero(nxt)) < 16 or np.array_equal(nxt, keep):
            break
        keep = nxt

    alpha = math.atan(slope)
    if not (math.radians(3.0) <= alpha <= math.radians(85.0)):
        return None
    t_apex = -intercept / slope
    apex = origin + t_apex * axis
    cos_a = 1.0 / math.sqrt(1.0 + slope * slope)
    dev = np.abs(rho - (slope * t + intercept)) * cos_a
    idx = np.flatnonzero(dev < thresh)
    if len(idx) < 16:
        return None
    tk = t[idx] - t_apex
    top = float(tk.max())
    if top <= 1e-6:
        return None
    return {
        "kind": "cone",
        "inliers": idx,
        "residual": float(np.sqrt(np.mean(dev[idx] ** 2))),
        "source": "lsq",
        "apex": np.asarray(apex, dtype=float),
        "axis": np.asarray(axis, dtype=float),
        "half_angle_rad": float(alpha),
        "radius_at_base": float(slope * top),
        "height": top,
    }


def _cone_deviation(
    pts: np.ndarray, apex: np.ndarray, axis: np.ndarray, half_angle: float
) -> np.ndarray:
    """Perpendicular distance from each point to the cone surface (mm)."""
    v = pts - apex
    t = v @ axis
    rho = np.linalg.norm(v - np.outer(t, axis), axis=1)
    slope = math.tan(half_angle)
    return np.abs(rho - slope * t) / math.sqrt(1.0 + slope * slope)


def _segment_cone(
    pts: np.ndarray,
    nrm: np.ndarray,
    rng: np.random.Generator,
    max_iter: int = 800,
    sample_cap: int = 2000,
):
    """Isolate ONE conical patch from a residual holding several surfaces.

    Every surface normal of a cone makes the same angle with its axis, so for three
    points on one cone both ``n1 − n2`` and ``n1 − n3`` are perpendicular to the
    axis and their cross product recovers it. Scoring then happens purely in normal
    space — how many normals share that polar angle — needing no radius, no apex
    and no distance threshold, and cleanly ignoring the planes, cylinders and
    spheres sharing the residual. Without this the axis estimate is taken over the
    whole mixed cloud and the cone fit is meaningless on any real part.
    """
    n_pts = len(pts)
    if n_pts < 32:
        return None
    sn = nrm[_stride_idx(n_pts, sample_cap)]
    trials = rng.integers(0, n_pts, size=(max_iter, 3))
    best: Optional[tuple] = None
    for a, b, c in trials:
        if a == b or b == c or a == c:
            continue
        axis = np.cross(nrm[a] - nrm[b], nrm[a] - nrm[c])
        na = float(np.linalg.norm(axis))
        if na < 1e-3:  # collinear normal differences (a plane, or too close together)
            continue
        axis = axis / na
        polar = float(nrm[a] @ axis)
        # |n · axis| is sin(half-angle): 0 would be a cylinder, 1 a flat annulus.
        if not (_CONE_SIN_MIN <= abs(polar) <= _CONE_SIN_MAX):
            continue
        count = int(np.count_nonzero(np.abs(sn @ axis - polar) < _CONE_NORMAL_TOL))
        if best is None or count > best[0]:
            best = (count, axis, polar)
    if best is None or best[0] < 16:
        return None
    _, axis, polar = best
    mask = np.abs(nrm @ axis - polar) < _CONE_NORMAL_TOL
    if int(np.count_nonzero(mask)) < 32:
        return None
    return mask, axis


def _fit_cone(
    pts: np.ndarray,
    normals: Optional[np.ndarray],
    thresh: float,
    rng: np.random.Generator,
) -> Optional[dict]:
    """Fit a cone from the normal distribution. Returns a candidate or None.

    The outward normal of a cone of half-angle α is ``n = cosα·u(θ) − sinα·d``, so
    as θ sweeps the cone the normals trace a circle lying exactly on the plane
    ``n · d = −sinα`` in normal space. The axis is therefore the normal of the
    best-fit plane through the normals — the smallest right-singular vector of the
    *mean-centred* normals — which stays well defined at every half-angle.

    The textbook uncentred form ``M = Σnnᵀ/N`` has eigenvalue ``sin²α`` along the
    axis and ``cos²α/2`` twice across it, so the axis is its "odd one out"
    eigenvector — but those coincide at α = 35.26°, where *no* eigenvector of ``M``
    is the axis. They are kept only as extra candidates for patches whose normal
    arc is too short to define a plane; every candidate is fitted and the best
    scoring one wins. None of this means anything over a mixed cloud, hence the
    ``_segment_cone`` pre-step.
    """
    if len(pts) < 32 or normals is None or len(normals) != len(pts):
        return None
    nrm = _unit_rows(normals)
    segmented = _segment_cone(pts, nrm, rng)
    axis_candidates: list = []
    if segmented is not None:
        mask, seed_axis = segmented
        sub_pts = pts[mask]
        sub_nrm = nrm[mask]
        axis_candidates.append(seed_axis)
    else:
        sub_pts = pts
        sub_nrm = nrm
    try:
        _, _, vt = np.linalg.svd(sub_nrm - sub_nrm.mean(axis=0), full_matrices=False)
        axis_candidates.insert(0, np.asarray(vt[-1], dtype=float))
        _, vecs = np.linalg.eigh(sub_nrm.T @ sub_nrm / float(len(sub_nrm)))
        axis_candidates.extend(np.asarray(vecs[:, k], dtype=float) for k in range(3))
    except np.linalg.LinAlgError:  # pragma: no cover - numpy fallback path
        if not axis_candidates:
            return None
    best: Optional[dict] = None
    for axis in axis_candidates:
        cand = _cone_from_axis(sub_pts, sub_nrm, axis, thresh)
        if cand is None:
            continue
        if best is None or (len(cand["inliers"]), -cand["residual"]) > (
            len(best["inliers"]),
            -best["residual"],
        ):
            best = cand
    if best is None:
        return None

    # Re-select inliers against the refined cone over the whole working cloud, so a
    # patch the segmentation clipped is taken back in one go.
    dev = _cone_deviation(pts, best["apex"], best["axis"], best["half_angle_rad"])
    idx = np.flatnonzero(dev < thresh)
    if len(idx) < 32:
        return None
    top = float(((pts[idx] - best["apex"]) @ best["axis"]).max())
    if top <= 1e-6:
        return None
    best["inliers"] = idx
    best["residual"] = float(np.sqrt(np.mean(dev[idx] ** 2)))
    best["height"] = top
    best["radius_at_base"] = float(math.tan(best["half_angle_rad"]) * top)
    return best


def _candidate_score(cand: dict, thresh: float) -> float:
    """MSAC-style score for the per-round cylinder/cone/sphere competition.

    Raw inlier count alone ties too easily: a sphere thrown at a cone claims almost
    the same points as the correct cone fit, just at ten times the residual.
    Weighting each candidate's support by how tightly it actually explains those
    points breaks that tie the right way, and costs nothing.
    """
    if thresh <= 0.0:
        return float(len(cand["inliers"]))
    tightness = 1.0 - min(float(cand["residual"]) / thresh, 1.0)
    return float(len(cand["inliers"])) * tightness


# ---------------------------------------------------------------------------
# Feature semantics — what a downstream parametric rebuild actually needs
# ---------------------------------------------------------------------------
def _outward_fraction(
    pts: np.ndarray, normals: np.ndarray, center: np.ndarray, axis: np.ndarray
) -> Optional[float]:
    """Fraction of surface normals pointing *away* from the cylinder axis.

    ≈1 → the material is inside the cylinder (boss / shaft).
    ≈0 → the material is outside it (hole / bore).
    """
    v = pts - center
    t = v @ axis
    perp = v - np.outer(t, axis)
    rr = np.linalg.norm(perp, axis=1)
    ok = rr > 1e-9
    if int(np.count_nonzero(ok)) < 8:
        return None
    radial = perp[ok] / rr[ok, None]
    dots = np.einsum("ij,ij->i", _unit_rows(normals[ok]), radial)
    return float(np.count_nonzero(dots > 0.0)) / float(len(dots))


def _classify_cylinder(
    inlier_pts: np.ndarray,
    inlier_normals: Optional[np.ndarray],
    all_pts: np.ndarray,
    center: np.ndarray,
    axis: np.ndarray,
    radius: float,
    height: float,
    oriented: bool,
    tol_mm: float,
):
    """Return ``(role, through, depth_mm)`` for a fitted cylinder.

    ``role`` is ``hole`` when the mesh material lies outside the cylinder,
    ``shaft`` when the cylinder *is* the body (it spans the part both axially and
    radially), ``boss`` when it is an external protrusion off a larger body, and
    ``unknown`` when the normals are missing, unoriented, or contradictory.

    ``through`` is True only for a bore whose axial run covers the full local wall.
    That comparison has to be threshold-aware, not a bare percentage: peeling the
    end faces first claims every bore point within ``tol_mm`` of them, so a genuine
    through bore always measures a couple of thresholds short of the wall.

    ``depth_mm`` is the usable feature depth — the fitted axial run for a blind
    bore or a protrusion, but the full local wall thickness for a through bore,
    which by definition spans it and would otherwise inherit that same short bite.
    """
    depth = float(height)
    if not oriented or inlier_normals is None or len(inlier_normals) != len(inlier_pts):
        return "unknown", False, depth
    outward = _outward_fraction(inlier_pts, inlier_normals, center, axis)
    if outward is None:
        return "unknown", False, depth

    v_all = all_pts - center
    t_all = v_all @ axis
    r_all = np.linalg.norm(v_all - np.outer(t_all, axis), axis=1)

    if outward <= _ROLE_INWARD_MAX:
        column = r_all <= max(radius * 2.0, radius + 2.0)
        through = False
        if int(np.count_nonzero(column)) >= 8:
            local = float(t_all[column].max() - t_all[column].min())
            slack = max(0.05 * local, 4.0 * tol_mm)
            through = bool(local > 1e-9 and height >= local - slack)
            if through:
                depth = local
        return "hole", through, depth
    if outward >= _ROLE_OUTWARD_MIN:
        part_axial = float(t_all.max() - t_all.min())
        part_radial = float(r_all.max()) * 2.0
        axial_cover = height / part_axial if part_axial > 1e-9 else 0.0
        radial_cover = (2.0 * radius) / part_radial if part_radial > 1e-9 else 0.0
        role = "shaft" if (axial_cover >= 0.8 and radial_cover >= 0.9) else "boss"
        return role, False, depth
    return "unknown", False, depth


def _plane_is_pocket(
    patch_pts: np.ndarray,
    outward: np.ndarray,
    all_pts: np.ndarray,
    thresh: float,
) -> bool:
    """True when material rises above the patch all the way round its rim.

    That is the one unambiguous, cheap pocket signature: a pocket floor is
    *enclosed* by taller material. A plain face carrying a central boss has taller
    material too, but only in the middle — hence the rim + azimuth-coverage test.
    """
    if len(patch_pts) < 16:
        return False
    e1, e2 = _axis_perp_basis(outward)
    centroid = patch_pts.mean(axis=0)
    rel = patch_pts - centroid
    px = rel @ e1
    py = rel @ e2
    patch_r = float(np.percentile(np.sqrt(px * px + py * py), 95.0))
    if patch_r < 1e-6:
        return False
    rel_all = all_pts - centroid
    above = (rel_all @ outward) > max(4.0 * thresh, 0.5)
    if not bool(np.any(above)):
        return False
    bx = rel_all[above] @ e1
    by = rel_all[above] @ e2
    br = np.sqrt(bx * bx + by * by)
    rim = br > 0.8 * patch_r
    if int(np.count_nonzero(rim)) < 8:
        return False
    ang = np.arctan2(by[rim], bx[rim])
    bins = np.unique(np.clip(((ang + math.pi) / (2.0 * math.pi) * 8.0).astype(int), 0, 7))
    return bool(len(bins) >= 7)


def _assign_plane_pads(records: list, thresh: float) -> None:
    """Label still-``unknown`` planes that stand proud of a much larger parallel face.

    ``records`` are ``(entry, outward_normal, centroid, inlier_count)`` tuples; the
    entries are mutated in place. A raised land only counts as a ``pad`` when its
    reference face faces the same way and is at least 3× bigger — anything more
    speculative stays ``unknown``.
    """
    for entry, n_i, c_i, k_i in records:
        if entry["role"] != "unknown":
            continue
        for other, n_j, c_j, k_j in records:
            if other is entry:
                continue
            if float(n_i @ n_j) < 0.99 or k_j < 3 * k_i:
                continue
            if float((c_i - c_j) @ n_j) > max(4.0 * thresh, 0.5):
                entry["role"] = "pad"
                break


# ---------------------------------------------------------------------------
# Cross-sections
# ---------------------------------------------------------------------------
def _dominant_axis(pts: np.ndarray) -> np.ndarray:
    """Principal direction with the largest projected extent."""
    centred = pts - pts.mean(axis=0)
    try:
        _, _, vt = np.linalg.svd(centred, full_matrices=False)
    except np.linalg.LinAlgError:  # pragma: no cover - numpy fallback path
        return np.array([0.0, 0.0, 1.0])
    best_axis = vt[0]
    best_extent = -1.0
    for k in range(vt.shape[0]):
        proj = centred @ vt[k]
        extent = float(proj.max() - proj.min())
        if extent > best_extent:
            best_extent = extent
            best_axis = vt[k]
    axis = np.asarray(best_axis, dtype=float)
    return axis / (np.linalg.norm(axis) + 1e-12)


def _resolve_section_axis(section_axis: Any, pts: np.ndarray) -> Optional[np.ndarray]:
    """Coerce the ``section_axis`` argument to a unit vector (None → dominant)."""
    if section_axis is None:
        return _dominant_axis(pts)
    if isinstance(section_axis, str):
        named = {
            "x": (1.0, 0.0, 0.0),
            "y": (0.0, 1.0, 0.0),
            "z": (0.0, 0.0, 1.0),
        }.get(section_axis.strip().lower())
        if named is None:
            return None
        return np.asarray(named, dtype=float)
    try:
        axis = np.asarray(section_axis, dtype=float).reshape(3)
    except Exception:  # noqa: BLE001
        return None
    norm = float(np.linalg.norm(axis))
    if norm < 1e-9:
        return None
    return axis / norm


def _polyline_closed(poly: np.ndarray) -> bool:
    """True when the polyline's last vertex repeats its first."""
    return bool(len(poly) > 2 and np.allclose(poly[0], poly[-1], atol=1e-6))


def _open_ring(poly: np.ndarray) -> np.ndarray:
    """Drop the duplicated closing vertex, if present."""
    return poly[:-1] if _polyline_closed(poly) else poly


def _signed_area(poly: np.ndarray) -> float:
    """Shoelace signed area of a 2D polyline (mm²)."""
    ring = _open_ring(poly)
    if len(ring) < 3:
        return 0.0
    x = ring[:, 0]
    y = ring[:, 1]
    return 0.5 * float(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1)))


def _point_in_poly(pt: np.ndarray, poly: np.ndarray) -> bool:
    """Even-odd ray cast — is the 2D point inside the polyline?"""
    ring = _open_ring(poly)
    if len(ring) < 3:
        return False
    x = float(pt[0])
    y = float(pt[1])
    x0 = ring[:, 0]
    y0 = ring[:, 1]
    x1 = np.roll(x0, -1)
    y1 = np.roll(y0, -1)
    straddle = (y0 > y) != (y1 > y)
    with np.errstate(divide="ignore", invalid="ignore"):
        xint = x0 + (y - y0) * (x1 - x0) / (y1 - y0)
    hits = straddle & np.isfinite(xint) & (xint > x)
    return bool(int(np.count_nonzero(hits)) % 2 == 1)


def _inside_of(inner: np.ndarray, outer: np.ndarray) -> bool:
    """Majority vote over up to 8 vertices — robust for non-convex rings."""
    ring = _open_ring(inner)
    if len(ring) == 0:
        return False
    probe = ring[_stride_idx(len(ring), 8)]
    inside = sum(1 for p in probe if _point_in_poly(p, outer))
    return inside * 2 > len(probe)


def _net_area(polys: list) -> float:
    """Enclosed area of a section: nested rings alternate sign, so a bore subtracts."""
    areas = [abs(_signed_area(p)) for p in polys]
    total = 0.0
    for i, poly in enumerate(polys):
        if areas[i] <= 0.0:
            continue
        depth = 0
        for j, other in enumerate(polys):
            if i == j or areas[j] <= areas[i]:
                continue
            if _inside_of(poly, other):
                depth += 1
        total += -areas[i] if depth % 2 else areas[i]
    return max(total, 0.0)


def _sections_trimesh(mesh_obj, axis: np.ndarray, e1: np.ndarray, e2: np.ndarray, stations):
    """Cross-sections via ``trimesh.Trimesh.section``: (station, [2D polylines])."""
    out = []
    for station in stations:
        try:
            path = mesh_obj.section(plane_origin=axis * station, plane_normal=axis)
        except Exception:  # noqa: BLE001
            continue
        if path is None:
            continue
        try:
            loops = [np.asarray(d, dtype=float) for d in path.discrete]
        except Exception:  # noqa: BLE001
            continue
        polys = [
            np.c_[loop @ e1, loop @ e2]
            for loop in loops
            if loop.ndim == 2 and loop.shape[0] >= 3
        ]
        if polys:
            out.append((float(station), polys))
    return out


def _slice_triangles(
    verts: np.ndarray, faces: np.ndarray, axis: np.ndarray, station: float
) -> Optional[np.ndarray]:
    """Intersect every triangle with the plane ``p·axis = station``.

    Returns an ``(M, 2, 3)`` array of 3D segments, or ``None`` when the plane
    misses the mesh. Vertices landing exactly on the plane are nudged to its
    positive side so every crossing triangle contributes exactly two edge hits and
    the chained loops never develop a gap.
    """
    d = verts @ axis - station
    eps = 1e-9 * (float(np.abs(d).max()) + 1.0)
    d = np.where(np.abs(d) < eps, eps, d)
    dt = d[faces]
    pos = dt > 0.0
    hits = pos.sum(axis=1)
    sel = np.flatnonzero((hits == 1) | (hits == 2))
    if sel.size == 0:
        return None
    tri = verts[faces[sel]]
    dtr = dt[sel]
    head = np.array([0, 1, 2])
    tail = np.array([1, 2, 0])
    da = dtr[:, head]
    db = dtr[:, tail]
    crossing = (da > 0.0) != (db > 0.0)
    # Non-crossing edges get a dummy denominator: their interpolation is finite
    # garbage that ``pick`` discards, and no inf/nan ever enters the arithmetic.
    w = da / np.where(crossing, da - db, 1.0)
    pa = tri[:, head, :]
    pb = tri[:, tail, :]
    ip = pa + w[..., None] * (pb - pa)
    # Exactly two of the three edges cross; stable-sort the crossing ones first.
    pick = np.argsort(~crossing, axis=1, kind="stable")[:, :2]
    return np.take_along_axis(ip, pick[..., None], axis=1)


def _chain_segments(segs: np.ndarray, quantum: float) -> list:
    """Walk unordered 3D segments into polylines, closing loops where they close."""
    keys = np.round(segs / quantum).astype(np.int64)
    ends = [(tuple(keys[i, 0]), tuple(keys[i, 1])) for i in range(len(segs))]
    adjacency: dict = defaultdict(list)
    for i, (k0, k1) in enumerate(ends):
        adjacency[k0].append(i)
        adjacency[k1].append(i)
    used = np.zeros(len(segs), dtype=bool)
    loops: list = []

    def _step(current, chain, keychain, append: bool):
        while True:
            nxt = -1
            for cand in adjacency.get(current, ()):
                if not used[cand]:
                    nxt = cand
                    break
            if nxt < 0:
                return
            used[nxt] = True
            k0, k1 = ends[nxt]
            if k0 == current:
                point, current = segs[nxt, 1], k1
            else:
                point, current = segs[nxt, 0], k0
            if append:
                chain.append(point)
                keychain.append(current)
            else:
                chain.insert(0, point)
                keychain.insert(0, current)
            if keychain[0] == keychain[-1]:
                return

    for start in range(len(segs)):
        if used[start]:
            continue
        used[start] = True
        chain = [segs[start, 0], segs[start, 1]]
        keychain = [ends[start][0], ends[start][1]]
        _step(keychain[-1], chain, keychain, True)
        if keychain[0] != keychain[-1]:
            _step(keychain[0], chain, keychain, False)
        if len(chain) >= 3:
            loops.append(np.asarray(chain, dtype=float))
    return loops


def _sections_manual(
    verts: np.ndarray,
    faces: np.ndarray,
    axis: np.ndarray,
    e1: np.ndarray,
    e2: np.ndarray,
    stations,
):
    """trimesh-free cross-sections: triangle/plane intersection + edge chaining."""
    span = verts.max(axis=0) - verts.min(axis=0)
    quantum = max(float(np.linalg.norm(span)) * 1e-7, 1e-12)
    out = []
    for station in stations:
        segs = _slice_triangles(verts, faces, axis, float(station))
        if segs is None or len(segs) == 0:
            continue
        polys = [
            np.c_[loop @ e1, loop @ e2] for loop in _chain_segments(segs, quantum)
        ]
        if polys:
            out.append((float(station), polys))
    return out


def _extract_sections(
    handle: Optional[dict],
    pts: np.ndarray,
    section_axis: Any,
    n_sections: int,
):
    """Recover ``n_sections`` cross-section profiles. Returns ``(sections, note)``."""
    if n_sections <= 0:
        return [], None
    if handle is None:
        return [], _note(
            "sections",
            "no_mesh",
            "pass mesh_path (not points=) so the mesh can be sliced",
        )
    axis = _resolve_section_axis(section_axis, pts)
    if axis is None:
        return [], _note(
            "sections", "bad_section_axis", "section_axis must be 'x'/'y'/'z' or a 3-vector"
        )
    proj = pts @ axis
    lo = float(proj.min())
    hi = float(proj.max())
    if hi - lo < 1e-6:
        return [], _note("sections", "degenerate_extent", "mesh is flat along the section axis")
    step = (hi - lo) / float(n_sections)
    stations = lo + (np.arange(n_sections, dtype=float) + 0.5) * step
    e1, e2 = _axis_perp_basis(axis)

    raw = []
    note = None
    if handle.get("backend") == "trimesh" and handle.get("obj") is not None:
        raw = _sections_trimesh(handle["obj"], axis, e1, e2, stations)
    elif handle.get("faces") is not None:
        raw = _sections_manual(handle["vertices"], handle["faces"], axis, e1, e2, stations)
    else:
        note = _note(
            "sections",
            "no_faces",
            "mesh carries no triangles; cross-sections need a surface",
        )

    axis_out = [round(float(x), 6) for x in axis]
    frame = {
        "e1": [round(float(x), 6) for x in e1],
        "e2": [round(float(x), 6) for x in e2],
    }
    sections = []
    for station, polys in raw:
        sections.append(
            {
                "station_mm": round(station, 6),
                "axis": axis_out,
                "polylines": [
                    [[round(float(x), 6), round(float(y), 6)] for x, y in poly]
                    for poly in polys
                ],
                "area_mm2": round(_net_area(polys), 6),
                "closed": bool(all(_polyline_closed(p) for p in polys)),
                "frame": dict(frame, origin=[round(float(x * station), 6) for x in axis]),
            }
        )
    if not sections and note is None:
        note = _note("sections", "no_intersection", "no station plane intersected the mesh")
    return sections, note


# ---------------------------------------------------------------------------
# Fit-entry builders (contract §3 shape: kind / confidence / params_mm / source)
# ---------------------------------------------------------------------------
def _emit_cylinder(
    cand: dict,
    work: np.ndarray,
    work_normals: Optional[np.ndarray],
    all_pts: np.ndarray,
    total: int,
    index: int,
    oriented: bool,
    tol_mm: float,
) -> dict:
    idx = cand["inliers"]
    inlier_pts = work[idx]
    center = cand["center"]
    axis = cand["axis"]
    radius = cand["radius"]
    height = _extent_along(inlier_pts, center, axis)
    base = center + float(((inlier_pts - center) @ axis).min()) * axis
    role, through, depth = _classify_cylinder(
        inlier_pts,
        work_normals[idx] if work_normals is not None else None,
        all_pts,
        center,
        axis,
        radius,
        height,
        oriented,
        tol_mm,
    )
    return {
        "kind": "cylinder",
        "confidence": round(float(len(idx) / total), 4),
        "params_mm": {
            "radius": round(float(radius), 6),
            "height": round(float(height), 6),
            "axis": [round(float(x), 6) for x in axis],
            "origin": [round(float(x), 6) for x in center],
            "base": [round(float(x), 6) for x in base],
        },
        "source": "ransac",
        "inliers": int(len(idx)),
        "residual_mm": round(float(cand["residual"]), 6),
        "face_ids": [f"cyl_{index}"],
        "role": role,
        "through": bool(through),
        "depth_mm": round(float(depth), 6),
    }


def _emit_sphere(cand: dict, total: int, index: int) -> dict:
    center = cand["center"]
    return {
        "kind": "sphere",
        "confidence": round(float(len(cand["inliers"]) / total), 4),
        "params_mm": {
            "center": [round(float(x), 6) for x in center],
            "radius": round(float(cand["radius"]), 6),
            "origin": [round(float(x), 6) for x in center],
        },
        "source": cand["source"],
        "inliers": int(len(cand["inliers"])),
        "residual_mm": round(float(cand["residual"]), 6),
        "face_ids": [f"sphere_{index}"],
    }


def _emit_cone(cand: dict, total: int, index: int) -> dict:
    apex = cand["apex"]
    axis = cand["axis"]
    return {
        "kind": "cone",
        "confidence": round(float(len(cand["inliers"]) / total), 4),
        "params_mm": {
            "apex": [round(float(x), 6) for x in apex],
            "axis": [round(float(x), 6) for x in axis],
            "half_angle_rad": round(float(cand["half_angle_rad"]), 6),
            "radius_at_base": round(float(cand["radius_at_base"]), 6),
            "height": round(float(cand["height"]), 6),
            "origin": [round(float(x), 6) for x in apex],
        },
        "source": cand["source"],
        "inliers": int(len(cand["inliers"])),
        "residual_mm": round(float(cand["residual"]), 6),
        "face_ids": [f"cone_{index}"],
    }


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
    max_cylinders: int = 8,
    max_cones: int = 4,
    max_spheres: int = 4,
    min_inlier_frac: float = 0.02,
    n_sections: int = 12,
    section_axis: Any = None,
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
        tests / callers that already hold a cloud). Cross-sections need the mesh, so
        this path records a ``sections`` note instead of slicing.
    n_samples:
        Surface sample count when loading from ``mesh_path``.
    plane_thresh_mm / cyl_thresh_mm:
        RANSAC inlier distance thresholds (mm). ``cyl_thresh_mm`` also gates the
        cone and sphere fitters.
    max_planes:
        Cap on peeled planes.
    max_cylinders / max_cones / max_spheres:
        Per-kind caps on peeled curved primitives. The three fitters compete each
        round and the best-supported one is peeled, so these are budgets, not an
        execution order.
    min_inlier_frac:
        Minimum inlier fraction (of the whole cloud) for a fit to be kept.
    n_sections:
        Number of evenly spaced cross-section stations (0 disables sectioning).
    section_axis:
        ``'x'`` / ``'y'`` / ``'z'`` or an explicit 3-vector. Defaults to the
        largest-extent principal axis of the cloud.

    Returns
    -------
    On success, the ``aicad.reveng_fits/1`` schema object (contract §3) with an
    additive ``ok: true``, a ``fits_revision`` capability marker and a ``notes``
    list naming every fitter that had to be skipped and why. On an unprovisioned
    backend, the §0 degradation envelope
    ``{"ok": false, "reason": "backend_not_provisioned", "hint": "pip install open3d"}``.
    """
    if not _backend_available():
        return _envelope()

    notes: list[dict] = []
    handle: Optional[dict] = None
    normals: Optional[np.ndarray] = None
    normals_oriented = False
    if points is not None:
        pts = _points_from_array(points)
    elif mesh_path is not None:
        handle = _load_mesh(mesh_path)
        if handle is None:
            pts = None
        else:
            pts, normals = _sample_mesh(handle, n_samples)
            normals_oriented = normals is not None
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

    if normals is None:
        normals = _estimate_normals(pts)
        if normals is None:
            notes.append(
                _note(
                    "normals",
                    "unavailable",
                    "pip install scipy to estimate normals from a bare point cloud;"
                    " without them cones are skipped and roles stay 'unknown'",
                )
            )
        else:
            notes.append(
                _note(
                    "normals",
                    "estimated_unoriented",
                    "normals came from k-NN PCA, so their sign is unknown and"
                    " hole-vs-boss roles stay 'unknown'",
                )
            )

    total = len(pts)
    min_inliers = max(3, int(total * min_inlier_frac))
    rng = np.random.default_rng(_RNG_SEED)
    fits: list[dict] = []

    # --- Peel dominant planes ------------------------------------------------
    # ``alive`` tracks points not yet claimed by a fit; ``pool`` narrows what the
    # plane RANSAC may still sample. A patch rejected as curved leaves the pool but
    # stays alive, so a small genuine face behind it is still findable and the
    # curved surface itself reaches the cone/sphere fitters intact.
    alive = np.ones(total, dtype=bool)
    pool = np.ones(total, dtype=bool)
    plane_face_i = 0
    plane_records: list = []
    curved_rejects = 0
    for _ in range(max_planes + _MAX_CURVED_PLANE_REJECTS):
        if plane_face_i >= max_planes or curved_rejects >= _MAX_CURVED_PLANE_REJECTS:
            break
        pool_idx = np.flatnonzero(alive & pool)
        if len(pool_idx) < min_inliers:
            break
        eq, inliers = _fit_plane(pts[pool_idx], plane_thresh_mm)
        if eq is None or inliers is None or len(inliers) < min_inliers:
            break
        claimed = pool_idx[inliers]
        inlier_pts = pts[claimed]
        if _plane_is_curved(inlier_pts, eq, plane_thresh_mm):
            pool[claimed] = False
            curved_rejects += 1
            continue
        normal = eq[:3] / (np.linalg.norm(eq[:3]) + 1e-12)
        centroid = inlier_pts.mean(axis=0)
        residual = _plane_residual(inlier_pts, eq)
        entry = {
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
            "role": "unknown",
        }
        if normals_oriented and normals is not None:
            outward = _unit_rows(normals[claimed]).mean(axis=0)
            agreement = float(np.linalg.norm(outward))
            if agreement >= 0.9:
                outward = outward / agreement
                if _plane_is_pocket(inlier_pts, outward, pts, plane_thresh_mm):
                    entry["role"] = "pocket"
                plane_records.append((entry, outward, centroid, int(len(inliers))))
        fits.append(entry)
        plane_face_i += 1
        alive[claimed] = False
    remaining = pts[alive]
    remaining_normals = normals[alive] if normals is not None else None
    _assign_plane_pads(plane_records, plane_thresh_mm)

    # --- Peel curved primitives from the residual ----------------------------
    # Peeling the flat faces first leaves the curved surfaces, so the fitters no
    # longer latch onto a spurious large radius spanning a slab. Cylinder, cone and
    # sphere then *compete* each round: whichever explains the most residual points
    # is peeled. Competing rather than running in a fixed order is what stops a
    # sphere being shredded into cylinder bands (its equatorial belt is a perfectly
    # good cylinder) or a cone being clipped into one.
    if len(remaining) >= 10:
        work = remaining
        work_normals = remaining_normals
    else:
        work = pts
        work_normals = normals
    if work_normals is None:
        notes.append(
            _note(
                "cone",
                "no_normals",
                "cone fitting needs surface normals; none available for this input",
            )
        )
        if not HAVE_PYRANSAC3D:
            notes.append(
                _note(
                    "cylinder",
                    "no_normals_no_pyransac3d",
                    "pip install pyransac3d for the no-normals cylinder fallback",
                )
            )

    budget = {
        "cylinder": max(0, int(max_cylinders)),
        "cone": max(0, int(max_cones)),
        "sphere": max(0, int(max_spheres)),
    }
    counters = {"cylinder": 0, "cone": 0, "sphere": 0}
    for _ in range(sum(budget.values())):
        if len(work) < max(10, min_inliers):
            break
        candidates = []
        if budget["cylinder"] > 0:
            cand = _fit_cylinder_patch(work, work_normals, cyl_thresh_mm, rng)
            if cand is not None:
                candidates.append(cand)
        if budget["cone"] > 0 and work_normals is not None:
            cand = _fit_cone(work, work_normals, cyl_thresh_mm, rng)
            if cand is not None:
                candidates.append(cand)
        if budget["sphere"] > 0:
            cand = _fit_sphere(work, cyl_thresh_mm, rng)
            if cand is not None:
                candidates.append(cand)
        candidates = [c for c in candidates if len(c["inliers"]) >= min_inliers]
        if not candidates:
            break
        best = max(candidates, key=lambda c: _candidate_score(c, cyl_thresh_mm))
        kind = best["kind"]
        index = counters[kind]
        if kind == "cylinder":
            fits.append(
                _emit_cylinder(
                    best,
                    work,
                    work_normals,
                    pts,
                    total,
                    index,
                    normals_oriented,
                    max(plane_thresh_mm, cyl_thresh_mm),
                )
            )
        elif kind == "sphere":
            fits.append(_emit_sphere(best, total, index))
        else:
            fits.append(_emit_cone(best, total, index))
        budget[kind] -= 1
        counters[kind] += 1
        mask = np.ones(len(work), dtype=bool)
        mask[best["inliers"]] = False
        work = work[mask]
        if work_normals is not None:
            work_normals = work_normals[mask]

    # --- Cross-section profiles ---------------------------------------------
    sections, section_note = _extract_sections(handle, pts, section_axis, n_sections)
    if section_note is not None:
        notes.append(section_note)

    return {
        "schema": FITS_SCHEMA,
        "fits_revision": FITS_REVISION,
        "ok": True,
        "session_id": session_id,
        "mesh_path": mesh_path or "",
        "fits": fits,
        "sections": sections,
        "notes": notes,
    }
