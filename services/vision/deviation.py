"""Mesh-vs-mesh geometric deviation for the reverse-engineering verification loop.

Computes max deviation, RMS, per-region heat buckets, and per-vertex distances
(for colour-coding in the GUI).  Implements the deviation schema from
``contracts/reveng.md §5``.

Heavy deps (scipy, numpy, trimesh) are imported lazily — every public function
degrades gracefully when they are absent, returning ``{"ok": False, ...}``.

Typical usage::

    result = compute_deviation(mesh_path="mesh/cleaned.ply",
                               ref_path="mesh/reference.ply")
    # result["ok"] is True, result["max_mm"], result["rms_mm"], ...

MCP wiring lives in ``services/mcp/reveng_tools.py`` (RE-02).
"""
from __future__ import annotations

import math
from typing import Optional

# ---------------------------------------------------------------------------
# Lazy heavy imports
# ---------------------------------------------------------------------------

try:
    import numpy as np  # type: ignore
    _HAVE_NP = True
except Exception:  # noqa: BLE001
    np = None  # type: ignore
    _HAVE_NP = False

try:
    from scipy.spatial import cKDTree  # type: ignore
    _HAVE_SCIPY = True
except Exception:  # noqa: BLE001
    cKDTree = None  # type: ignore
    _HAVE_SCIPY = False

try:
    import trimesh  # type: ignore
    _HAVE_TRIMESH = True
except Exception:  # noqa: BLE001
    trimesh = None  # type: ignore
    _HAVE_TRIMESH = False

_MISSING_HINT = (
    "pip install numpy scipy trimesh"
)

# Default heat-map bucket boundaries (mm)
DEFAULT_BUCKETS_MM = [
    (0.0, 0.1, "#00ff00"),   # green  — within tight tolerance
    (0.1, 0.5, "#ffff00"),   # yellow — moderate deviation
    (0.5, None, "#ff0000"),  # red    — out-of-tolerance
]

# Number of surface sample points for the KD-tree probe
_DEFAULT_SAMPLES = 20_000


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _load_mesh(path: str):
    """Load a mesh from *path* using trimesh.  Returns a trimesh.Trimesh."""
    mesh = trimesh.load(path, force="mesh", process=False)
    if hasattr(mesh, "geometry"):  # Scene → pick largest geometry
        geom = max(mesh.geometry.values(), key=lambda g: len(g.vertices))
        mesh = geom
    return mesh


def _sample_surface(mesh, n_samples: int):
    """Return (n_samples, 3) float64 array of points uniformly on the surface."""
    pts, _ = trimesh.sample.sample_surface(mesh, n_samples)
    return pts.astype(np.float64)


def _kdtree_distances(query_pts, ref_pts):
    """Nearest-neighbour distances from *query_pts* to *ref_pts* via cKDTree."""
    tree = cKDTree(ref_pts)
    dists, _ = tree.query(query_pts, workers=-1)
    return dists.astype(np.float64)


def _build_buckets(dists, bucket_spec, n_vertices: int):
    """Aggregate per-vertex distances into heat buckets."""
    buckets = []
    for lo, hi, color in bucket_spec:
        if hi is None:
            mask = dists >= lo
        else:
            mask = (dists >= lo) & (dists < hi)
        buckets.append({
            "range_mm": [lo, hi],
            "face_count": int(mask.sum()),
            "color": color,
        })
    return buckets


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def compute_deviation(
    mesh_path: str,
    ref_path: str,
    *,
    session_id: str = "",
    brep_doc_id: Optional[str] = None,
    n_samples: int = _DEFAULT_SAMPLES,
    bucket_spec: Optional[list] = None,
) -> dict:
    """Compute geometric deviation between two mesh files.

    Parameters
    ----------
    mesh_path:
        Path to the query mesh (the scanned / reconstructed mesh).
    ref_path:
        Path to the reference mesh (ground-truth or parametric-exported tessellation).
    session_id:
        RE session identifier — echoed back into the result dict.
    brep_doc_id:
        Optional B-Rep doc id (when ref was tessellated from a parametric model).
    n_samples:
        Number of surface samples used for KD-tree distance probing.
    bucket_spec:
        List of ``(lo_mm, hi_mm_or_None, hex_color)`` tuples.
        Defaults to ``DEFAULT_BUCKETS_MM``.

    Returns
    -------
    dict
        Conforms to ``contracts/reveng.md §5`` deviation schema plus
        ``vertex_distances_mm`` (list[float]) for colour-coding.
        On failure returns ``{"ok": False, "reason": ..., "hint": ...}``.
    """
    if bucket_spec is None:
        bucket_spec = DEFAULT_BUCKETS_MM

    if not _HAVE_NP:
        return {"ok": False, "reason": "numpy not installed", "hint": _MISSING_HINT}
    if not _HAVE_SCIPY:
        return {"ok": False, "reason": "scipy not installed", "hint": _MISSING_HINT}
    if not _HAVE_TRIMESH:
        return {"ok": False, "reason": "trimesh not installed", "hint": _MISSING_HINT}

    try:
        mesh_q = _load_mesh(mesh_path)
        mesh_r = _load_mesh(ref_path)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"mesh load error: {exc}", "hint": "check file paths"}

    try:
        query_pts = _sample_surface(mesh_q, n_samples)
        # Sample the reference surface densely for a faithful KD-tree proxy;
        # use 4× samples so the tree is much denser than query to minimise
        # the nearest-point vs nearest-surface approximation error.
        ref_pts = _sample_surface(mesh_r, n_samples * 4)
        dists = _kdtree_distances(query_pts, ref_pts)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"distance computation error: {exc}", "hint": ""}

    max_mm = float(dists.max())
    rms_mm = float(math.sqrt((dists ** 2).mean()))
    buckets = _build_buckets(dists, bucket_spec, len(query_pts))

    return {
        "ok": True,
        "session_id": session_id,
        "max_mm": max_mm,
        "rms_mm": rms_mm,
        "buckets": buckets,
        "mesh_path": mesh_path,
        "brep_doc_id": brep_doc_id,
        "vertex_distances_mm": dists.tolist(),
    }


def compute_deviation_from_arrays(
    query_vertices,
    ref_vertices,
    *,
    session_id: str = "",
    brep_doc_id: Optional[str] = None,
    bucket_spec: Optional[list] = None,
) -> dict:
    """Compute deviation directly from vertex arrays (numpy (N,3) float64).

    Useful when meshes are already loaded in memory (e.g. from Open3D or
    trimesh objects).  Both arrays are treated as *unordered point clouds*;
    nearest-surface distance is approximated by nearest-point distance.

    Same return schema as :func:`compute_deviation`.
    """
    if bucket_spec is None:
        bucket_spec = DEFAULT_BUCKETS_MM

    if not _HAVE_NP:
        return {"ok": False, "reason": "numpy not installed", "hint": _MISSING_HINT}
    if not _HAVE_SCIPY:
        return {"ok": False, "reason": "scipy not installed", "hint": _MISSING_HINT}

    try:
        q = np.asarray(query_vertices, dtype=np.float64)
        r = np.asarray(ref_vertices, dtype=np.float64)
        dists = _kdtree_distances(q, r)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"distance computation error: {exc}", "hint": ""}

    max_mm = float(dists.max())
    rms_mm = float(math.sqrt((dists ** 2).mean()))
    buckets = _build_buckets(dists, bucket_spec, len(q))

    return {
        "ok": True,
        "session_id": session_id,
        "max_mm": max_mm,
        "rms_mm": rms_mm,
        "buckets": buckets,
        "mesh_path": None,
        "brep_doc_id": brep_doc_id,
        "vertex_distances_mm": dists.tolist(),
    }
