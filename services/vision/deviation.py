"""Sampled surface deviation, optionally aligning a scan to a CAD reference.

Reference documents are tessellated through the same core bridge export used by
``export_part(fmt="stl")``. Alignment is opt-in: ``world`` preserves placement;
``icp`` uses PCA initialisations followed by Open3D point-to-point ICP. Returned
distances approximate surface distance using sampled points, not exact B-rep
distances or a manufacturing tolerance.
"""
from __future__ import annotations

import itertools
import math
import tempfile
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

try:
    import open3d as o3d  # type: ignore
    _HAVE_OPEN3D = True
except Exception:  # noqa: BLE001
    o3d = None  # type: ignore
    _HAVE_OPEN3D = False

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


def _document_mesh_reference(doc_id: str, reference_id: str):
    """Read transformed reference triangles without exporting solids."""
    from services.mcp import core_bridge
    vertices, faces = [], []
    offset, vertex_base = 0, 0
    while True:
        result = core_bridge.call("doc_mesh_ref_mesh", {
            "doc_id": doc_id, "id": reference_id, "offset_tris": offset, "max_tris": 10000})
        if not result.get("ok"):
            raise ValueError(result.get("reason") or result.get("error") or "mesh reference unavailable")
        page_vertices = np.asarray(result["vertices_mm"], dtype=np.float64).reshape((-1, 3))
        page_faces = np.asarray(result["indices"], dtype=np.int64).reshape((-1, 3))
        vertices.append(page_vertices)
        faces.append(page_faces + vertex_base)
        vertex_base += len(page_vertices)
        next_offset = result.get("next_offset_tris")
        if next_offset is None:
            break
        if next_offset <= offset:
            raise ValueError("mesh reference returned an invalid page offset")
        offset = next_offset
    return trimesh.Trimesh(vertices=np.concatenate(vertices), faces=np.concatenate(faces), process=False)


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


def _validate_cloud(points):
    pts = np.asarray(points, dtype=np.float64)
    if pts.ndim != 2 or pts.shape[1] != 3 or len(pts) < 3 or not np.isfinite(pts).all():
        raise ValueError("point cloud must contain at least three finite 3D points")
    return pts


def _align_clouds(query, reference, alignment: str):
    """Return transformed query points and the query-to-reference alignment record."""
    if alignment == "world":
        return query, {"mode": "world", "transform_query_to_reference": np.eye(4).tolist()}
    if alignment != "icp":
        raise ValueError("alignment must be 'world' or 'icp'")
    if not _HAVE_OPEN3D:
        raise ImportError("ICP alignment needs Open3D; pip install open3d")

    # Try all proper sign/permutation choices of the PCA frames; otherwise an
    # asymmetric object can land in the wrong 180-degree orientation. Bound the
    # registration clouds and score before running ICP on the best four candidates.
    q = query[np.linspace(0, len(query) - 1, min(len(query), 4000), dtype=int)]
    r = reference[np.linspace(0, len(reference) - 1, min(len(reference), 4000), dtype=int)]
    cq, cr = q.mean(axis=0), r.mean(axis=0)
    _, vq = np.linalg.eigh((q - cq).T @ (q - cq))
    _, vr = np.linalg.eigh((r - cr).T @ (r - cr))
    vq, vr = vq[:, ::-1], vr[:, ::-1]
    tree_q, tree_r = cKDTree(q), cKDTree(r)

    def score(rotation, translation):
        qt = q @ rotation.T + translation
        # Reverse distances are evaluated in the original query frame.
        rb = (r - translation) @ rotation
        a = tree_r.query(qt)[0]
        b = tree_q.query(rb)[0]
        return float((np.mean(a * a) + np.mean(b * b)) / 2.0)

    candidates = []
    for perm in itertools.permutations(range(3)):
        for signs in itertools.product((-1.0, 1.0), repeat=3):
            permutation = np.eye(3)[:, perm] @ np.diag(signs)
            rotation = vr @ permutation @ vq.T
            if np.linalg.det(rotation) < 0:
                continue
            translation = cr - rotation @ cq
            matrix = np.eye(4)
            matrix[:3, :3], matrix[:3, 3] = rotation, translation
            candidates.append((score(rotation, translation), matrix))
    candidates.sort(key=lambda item: item[0])
    extent = float(np.linalg.norm(np.ptp(r, axis=0)))
    if extent <= 1e-12:
        raise ValueError("reference cloud has no spatial extent for alignment")
    threshold = max(0.15 * extent, 1e-6)
    source, target = o3d.geometry.PointCloud(), o3d.geometry.PointCloud()
    source.points = o3d.utility.Vector3dVector(q)
    target.points = o3d.utility.Vector3dVector(r)
    reg = o3d.pipelines.registration
    best = None
    for _, initial in candidates[:4]:
        result = reg.registration_icp(
            source, target, threshold, initial,
            reg.TransformationEstimationPointToPoint(),
            reg.ICPConvergenceCriteria(max_iteration=80),
        )
        matrix = np.asarray(result.transformation)
        if result.fitness <= 0 or not np.isfinite(matrix).all():
            continue
        final_score = score(matrix[:3, :3], matrix[:3, 3])
        if best is None or final_score < best[0]:
            best = (final_score, matrix, result)
    if best is None:
        raise ValueError("ICP found no correspondences; check scale and capture overlap")
    _, matrix, result = best
    aligned = query @ matrix[:3, :3].T + matrix[:3, 3]
    return aligned, {
        "mode": "icp",
        "method": "pca_multistart_point_to_point_icp",
        "transform_query_to_reference": matrix.tolist(),
        "fitness": float(result.fitness),
        "inlier_rmse_mm": float(result.inlier_rmse),
        "correspondence_distance_mm": threshold,
        "note": "Rigid alignment can be ambiguous for symmetric or incomplete surfaces; "
                "inspect the transform and overlap before interpreting deviation.",
    }


def _tessellate_document(doc_id: str, destination: str):
    """Export a temporary reference STL using the CAD document RPC."""
    from services.mcp import core_bridge

    result = core_bridge.call("export_file", {
        "doc_id": doc_id, "path": destination, "format": "stl",
    })
    if result.get("ok") is False:
        raise ValueError(result.get("reason") or result.get("error") or "STL export failed")
    return result.get("path", destination)


def _temporary_mesh_directory():
    """Use a scoped workspace temp directory in a cell and system temp locally."""
    from services.agent import request_context

    directory = request_context.cell_path("/work") if request_context.in_cell() else None
    return tempfile.TemporaryDirectory(prefix=".chudware_deviation_", dir=directory)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def compute_deviation(
    mesh_path: Optional[str],
    ref_path: Optional[str] = None,
    *,
    session_id: str = "",
    brep_doc_id: Optional[str] = None,
    query_mesh_ref_id: Optional[str] = None,
    ref_mesh_ref_id: Optional[str] = None,
    mesh_ref_doc_id: Optional[str] = None,
    n_samples: int = _DEFAULT_SAMPLES,
    bucket_spec: Optional[list] = None,
    alignment: str = "world",
) -> dict:
    """Compare a mesh with a reference mesh or a tessellated B-rep document.

    Supply ``ref_path`` or, when absent, ``brep_doc_id``. Both must already use
    the same physical units (mm); alignment never estimates scale. ``world``
    compares existing placement, while ``icp`` rigidly aligns query to reference
    first and reports its 4x4 transform and registration quality. Distances are
    query-surface samples to a denser reference sample, not exact surface or
    vertex distances. Sampling and tessellation impose additional uncertainty.
    """
    if bucket_spec is None:
        bucket_spec = DEFAULT_BUCKETS_MM

    if not _HAVE_NP:
        return {"ok": False, "reason": "numpy not installed", "hint": _MISSING_HINT}
    if not _HAVE_SCIPY:
        return {"ok": False, "reason": "scipy not installed", "hint": _MISSING_HINT}
    if not _HAVE_TRIMESH:
        return {"ok": False, "reason": "trimesh not installed", "hint": _MISSING_HINT}

    if (query_mesh_ref_id or ref_mesh_ref_id) and not mesh_ref_doc_id:
        return {"ok": False, "reason": "mesh_ref_doc_id_required",
                "hint": "Supply the document containing the mesh reference."}
    if not mesh_path and not query_mesh_ref_id:
        return {"ok": False, "reason": "no_query", "hint": "Supply mesh_path or query_mesh_ref_id."}
    if not ref_path and not brep_doc_id and not ref_mesh_ref_id:
        return {"ok": False, "reason": "no_reference",
                "hint": "Supply ref_path, brep_doc_id or ref_mesh_ref_id."}
    try:
        mesh_q = (_document_mesh_reference(mesh_ref_doc_id, query_mesh_ref_id)
                  if query_mesh_ref_id else _load_mesh(mesh_path))
        if ref_mesh_ref_id:
            mesh_r = _document_mesh_reference(mesh_ref_doc_id, ref_mesh_ref_id)
        elif ref_path:
            mesh_r = _load_mesh(ref_path)
        else:
            with _temporary_mesh_directory() as temp_dir:
                path = _tessellate_document(brep_doc_id, f"{temp_dir}/reference.stl")
                mesh_r = _load_mesh(path)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"reference or mesh load error: {exc}",
                "hint": "Check the mesh path and that the reference document has geometry."}

    try:
        if int(n_samples) < 3:
            raise ValueError("n_samples must be at least 3")
        query_pts = _sample_surface(mesh_q, int(n_samples))
        # A denser reference reduces nearest-sample error; it does not eliminate it.
        ref_pts = _sample_surface(mesh_r, int(n_samples) * 4)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"surface sampling error: {exc}", "hint": ""}
    result = compute_deviation_from_arrays(
        query_pts, ref_pts, session_id=session_id, brep_doc_id=brep_doc_id,
        bucket_spec=bucket_spec, alignment=alignment,
    )
    if result.get("ok"):
        result["mesh_path"] = mesh_path
        result["distance_basis"] = "query_surface_samples_to_reference_surface_samples"
        result["reference_samples"] = len(ref_pts)
    return result


def compute_deviation_from_arrays(
    query_vertices,
    ref_vertices,
    *,
    session_id: str = "",
    brep_doc_id: Optional[str] = None,
    bucket_spec: Optional[list] = None,
    alignment: str = "world",
) -> dict:
    """Compute deviation directly from vertex arrays (numpy (N,3) float64).

    Useful when meshes are already loaded in memory (e.g. from Open3D or
    trimesh objects).  Both arrays are treated as *unordered point clouds*;
    nearest-surface distance is approximated by nearest-point distance.

    ``alignment`` is ``world`` or ``icp``; the transform maps query to reference.
    Same return schema as :func:`compute_deviation`.
    """
    if bucket_spec is None:
        bucket_spec = DEFAULT_BUCKETS_MM

    if not _HAVE_NP:
        return {"ok": False, "reason": "numpy not installed", "hint": _MISSING_HINT}
    if not _HAVE_SCIPY:
        return {"ok": False, "reason": "scipy not installed", "hint": _MISSING_HINT}

    try:
        q = _validate_cloud(query_vertices)
        r = _validate_cloud(ref_vertices)
        q, alignment_record = _align_clouds(q, r, alignment)
        dists = _kdtree_distances(q, r)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"alignment or distance computation error: {exc}",
                "hint": "Use world or icp alignment with valid, similarly scaled geometry."}

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
        "alignment": alignment_record,
        "distance_basis": "query_points_to_reference_points",
    }
