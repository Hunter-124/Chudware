"""Recover metric scale for a COLMAP reconstruction — meyerls-style ArUco auto-scale + manual 2-point.

A structure-from-motion reconstruction (COLMAP sparse model) is metric *up to an unknown global
scale*: geometry is correct in shape but its unit is arbitrary. To turn it into millimetres we need
one known length in the scene.

Two solvers, both landing on the same output ``ScaleResult`` (``mm_per_unit`` + a residual %):

* :func:`solve_scale_aruco` — **automatic**, meyerls-style
  (github.com/meyerls/aruco-estimator): a printed marker of known ``marker_len_mm`` sits in the
  captured scene. We detect it in the registered images, back-project each detection ray, and
  triangulate the marker's four corners into the reconstruction's own 3D frame. The corner-to-corner
  edge length there is in *reconstruction units*; ``marker_len_mm / edge_units`` is the metric scale.
  Averaging the four edges (and the count of contributing views) gives a residual we surface honestly.

* :func:`solve_scale_two_point` — **manual** fallback (``contracts/reveng.md`` ``two_point``): the
  user clicks two pixels in one image whose real-world separation they know. With that image's camera
  pose + the reconstruction we ray-cast both pixels onto the nearest sparse geometry, measure the unit
  distance, and scale ``distance_mm / dist_units``.

The heavy detection front-ends need ``cv2`` (Apache-2.0; ArUco/AprilTag in core ``objdetect``). The
**arithmetic core** (:func:`scale_from_corner_edges`, :func:`triangulate_point`) is pure NumPy so the
meyerls math is unit-testable without any images, and the image path is tested on a synthetic ArUco set.
Every entry point degrades to a contract-shaped ``{ok: False, reason, hint}`` via
:class:`~services.vision.fiducial.VisionUnavailable` — never a traceback.
"""
from __future__ import annotations

import glob
import os
import statistics
from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

try:  # NumPy rides with OpenCV; keep import optional so the module never fails to import.
    import numpy as np  # type: ignore
    _HAVE_NP = True
except Exception:  # noqa: BLE001
    np = None  # type: ignore
    _HAVE_NP = False

from .fiducial import HAVE_CV2, VisionUnavailable, require_cv2


def _require_np():
    if not _HAVE_NP:
        raise VisionUnavailable(
            reason="NumPy is not installed",
            hint="pip install numpy (rides with opencv-python-headless)",
        )
    return np


# ---------------------------------------------------------------------------------- result shape


@dataclass
class ScaleResult:
    """Solved metric scale, contract-shaped for ``reveng_set_scale`` -> ``session.scale``."""

    method: str                              # "aruco_mat" | "two_point"
    mm_per_unit: float                       # multiply reconstruction lengths by this to get mm
    residual_pct: float                      # spread of the measurements used (honest uncertainty)
    n_views: int = 0                         # images that contributed a marker detection
    detail: dict[str, Any] = field(default_factory=dict)

    def to_scale_dict(self, *, confirmed: bool = False) -> dict[str, Any]:
        """Shape as ``aicad.reveng_session/1`` ``scale`` block."""
        return {
            "method": self.method,
            "mm_per_unit": float(self.mm_per_unit),
            "residual_pct": float(self.residual_pct),
            "confirmed": bool(confirmed),
        }


# ---------------------------------------------------------------------------------- COLMAP camera


@dataclass
class ColmapCamera:
    """One registered view: intrinsics (pinhole) + world→camera pose (COLMAP quaternion+t)."""

    image_name: str
    K: Any                                   # 3×3 intrinsic matrix
    R: Any                                   # 3×3 world→camera rotation
    t: Any                                   # 3, world→camera translation
    width: int = 0
    height: int = 0

    @property
    def center(self):
        """Camera centre in world (reconstruction) coordinates: C = -Rᵀ t."""
        return -self.R.T @ self.t

    def ray_dir(self, px: Sequence[float]):
        """Unit ray direction in world coords for pixel ``px`` (through the pinhole)."""
        _require_np()
        Kinv = np.linalg.inv(self.K)
        d_cam = Kinv @ np.array([px[0], px[1], 1.0], dtype="float64")
        d_world = self.R.T @ d_cam
        n = np.linalg.norm(d_world)
        return d_world / n if n > 0 else d_world


# ---------------------------------------------------------------------------------- math core


def triangulate_point(cameras: Sequence[ColmapCamera], pixels: Sequence[Sequence[float]]):
    """Least-squares triangulate one 3D point from ``>=2`` (camera, pixel) observations.

    Classic linear DLT: each view contributes two rows of ``A x = 0`` from ``x_px × (P X) = 0``.
    Returns the world-frame point (reconstruction units).
    """
    npy = _require_np()
    if len(cameras) != len(pixels):
        raise ValueError("cameras and pixels must be the same length")
    if len(cameras) < 2:
        raise ValueError("need at least 2 views to triangulate")
    rows = []
    for cam, px in zip(cameras, pixels):
        P = cam.K @ npy.hstack([cam.R, cam.t.reshape(3, 1)])  # 3×4 projection
        x, y = float(px[0]), float(px[1])
        rows.append(x * P[2] - P[0])
        rows.append(y * P[2] - P[1])
    A = npy.asarray(rows, dtype="float64")
    _u, _s, vt = npy.linalg.svd(A)
    Xh = vt[-1]
    if abs(Xh[3]) < 1e-12:
        raise ValueError("degenerate triangulation (point at infinity)")
    return Xh[:3] / Xh[3]


def scale_from_corner_edges(corners_3d, marker_len_mm: float) -> ScaleResult:
    """meyerls arithmetic: 4 triangulated marker corners (units) + known edge (mm) → scale.

    ``corners_3d`` is ordered TL, TR, BR, BL (OpenCV ArUco order). We measure the four edge
    lengths in reconstruction units, take their mean as the scale basis, and report the
    coefficient of variation as ``residual_pct`` — a real, non-cosmetic spread of how square the
    triangulation came out.
    """
    npy = _require_np()
    pts = npy.asarray(corners_3d, dtype="float64")
    if pts.shape != (4, 3):
        raise ValueError("need exactly 4 corner points of shape (4, 3)")
    if marker_len_mm <= 0:
        raise ValueError("marker_len_mm must be positive")
    edges = [
        float(npy.linalg.norm(pts[(i + 1) % 4] - pts[i]))
        for i in range(4)
    ]
    if any(e <= 0 for e in edges):
        raise ValueError("degenerate marker (zero-length edge)")
    mean_edge = sum(edges) / len(edges)
    mm_per_unit = marker_len_mm / mean_edge
    cov = statistics.pstdev(edges) / mean_edge if mean_edge > 0 else 0.0
    return ScaleResult(
        method="aruco_mat",
        mm_per_unit=mm_per_unit,
        residual_pct=cov * 100.0,
        detail={"edges_units": edges, "mean_edge_units": mean_edge},
    )


# ---------------------------------------------------------------------------------- COLMAP reader


def _read_colmap_cameras_txt(path: str) -> dict[int, Any]:
    """Parse COLMAP ``cameras.txt`` → {camera_id: (model, w, h, params[])}. Text format only."""
    out: dict[int, Any] = {}
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            cam_id = int(parts[0])
            model = parts[1]
            w, h = int(parts[2]), int(parts[3])
            params = [float(v) for v in parts[4:]]
            out[cam_id] = (model, w, h, params)
    return out


def _intrinsic_matrix(model: str, params: Sequence[float]):
    """Build a 3×3 K from a COLMAP camera model's params (pinhole family)."""
    npy = _require_np()
    if model in ("SIMPLE_PINHOLE", "SIMPLE_RADIAL", "RADIAL"):
        f, cx, cy = params[0], params[1], params[2]
        fx = fy = f
    elif model in ("PINHOLE", "OPENCV", "FULL_OPENCV", "OPENCV_FISHEYE"):
        fx, fy, cx, cy = params[0], params[1], params[2], params[3]
    else:
        raise ValueError(f"unsupported COLMAP camera model: {model}")
    return npy.array([[fx, 0.0, cx], [0.0, fy, cy], [0.0, 0.0, 1.0]], dtype="float64")


def _quat_to_rot(qw: float, qx: float, qy: float, qz: float):
    """COLMAP (w, x, y, z) quaternion → 3×3 rotation matrix."""
    npy = _require_np()
    n = (qw * qw + qx * qx + qy * qy + qz * qz) ** 0.5
    if n == 0:
        raise ValueError("zero quaternion")
    qw, qx, qy, qz = qw / n, qx / n, qy / n, qz / n
    return npy.array([
        [1 - 2 * (qy * qy + qz * qz), 2 * (qx * qy - qz * qw), 2 * (qx * qz + qy * qw)],
        [2 * (qx * qy + qz * qw), 1 - 2 * (qx * qx + qz * qz), 2 * (qy * qz - qx * qw)],
        [2 * (qx * qz - qy * qw), 2 * (qy * qz + qx * qw), 1 - 2 * (qx * qx + qy * qy)],
    ], dtype="float64")


def read_colmap_sparse(sparse_dir: str) -> dict[str, ColmapCamera]:
    """Read a COLMAP text sparse model (``cameras.txt`` + ``images.txt``) → {image_name: ColmapCamera}.

    Only the text export is supported (binary ``.bin`` is out of scope for v1); a session that only
    has ``.bin`` should be re-exported with ``colmap model_converter``. Raises ``FileNotFoundError``
    with a clear message the adapter can wrap.
    """
    _require_np()
    cameras_txt = os.path.join(sparse_dir, "cameras.txt")
    images_txt = os.path.join(sparse_dir, "images.txt")
    if not os.path.isfile(cameras_txt) or not os.path.isfile(images_txt):
        raise FileNotFoundError(
            f"COLMAP text model not found in {sparse_dir} (need cameras.txt + images.txt; "
            f"run `colmap model_converter --output_type TXT` if you only have .bin)"
        )
    intr = _read_colmap_cameras_txt(cameras_txt)
    out: dict[str, ColmapCamera] = {}
    with open(images_txt, "r", encoding="utf-8") as fh:
        lines = [ln for ln in fh if ln.strip() and not ln.startswith("#")]
    # images.txt: pose line then a POINTS2D line, alternating. We only need the pose lines.
    for i in range(0, len(lines), 2):
        parts = lines[i].split()
        if len(parts) < 10:
            continue
        qw, qx, qy, qz = (float(parts[1]), float(parts[2]), float(parts[3]), float(parts[4]))
        tx, ty, tz = (float(parts[5]), float(parts[6]), float(parts[7]))
        cam_id = int(parts[8])
        name = parts[9]
        model, w, h, params = intr[cam_id]
        out[name] = ColmapCamera(
            image_name=name,
            K=_intrinsic_matrix(model, params),
            R=_quat_to_rot(qw, qx, qy, qz),
            t=np.array([tx, ty, tz], dtype="float64"),
            width=w,
            height=h,
        )
    return out


# ---------------------------------------------------------------------------------- image front-end


def _detect_marker_corners(image, dict_name: str, marker_id: Optional[int]):
    """Detect ArUco/AprilTag markers; return {id: (4,2) corner array} in OpenCV order (TL,TR,BR,BL)."""
    cv2 = require_cv2()
    const = getattr(cv2.aruco, dict_name, None)
    if const is None:
        raise ValueError(f"unknown ArUco/AprilTag dictionary: {dict_name}")
    dictionary = cv2.aruco.getPredefinedDictionary(const)
    detector = cv2.aruco.ArucoDetector(dictionary, cv2.aruco.DetectorParameters())
    corners, ids, _rej = detector.detectMarkers(image)
    found: dict[int, Any] = {}
    if ids is not None:
        for quad, mid in zip(corners, ids.flatten()):
            mid = int(mid)
            if marker_id is not None and mid != marker_id:
                continue
            found[mid] = np.asarray(quad, dtype="float64").reshape(4, 2)
    return found


def solve_scale_aruco(*, sparse_dir: str, images_dir: str, marker_len_mm: float,
                      dict_name: str = "DICT_5X5_100", marker_id: Optional[int] = 0) -> ScaleResult:
    """Auto-scale a COLMAP reconstruction from a known-size marker in the scene (meyerls-style).

    Reads the sparse model, detects the marker in every registered image that resolves it, then
    triangulates each of its four corners across all contributing views and reads the metric scale
    from the corner edge lengths. Raises :class:`VisionUnavailable` if cv2 is absent, ``ValueError``
    on too-few detections (the adapter turns either into a contract-shaped error).
    """
    if not HAVE_CV2:
        raise VisionUnavailable()
    npy = _require_np()
    cv2 = require_cv2()
    cams = read_colmap_sparse(sparse_dir)

    # Gather per-corner (camera, pixel) observations across all images that saw the marker.
    per_corner_cams: list[list[ColmapCamera]] = [[] for _ in range(4)]
    per_corner_px: list[list[Any]] = [[] for _ in range(4)]
    n_views = 0
    for name, cam in cams.items():
        img_path = os.path.join(images_dir, name)
        if not os.path.isfile(img_path):
            # COLMAP may store a subdir-qualified name; try a basename glob.
            hits = glob.glob(os.path.join(images_dir, "**", os.path.basename(name)), recursive=True)
            if not hits:
                continue
            img_path = hits[0]
        img = cv2.imread(img_path, cv2.IMREAD_GRAYSCALE)
        if img is None:
            continue
        found = _detect_marker_corners(img, dict_name, marker_id)
        target = marker_id if marker_id is not None else (next(iter(found)) if found else None)
        if target is None or target not in found:
            continue
        quad = found[target]
        n_views += 1
        for c in range(4):
            per_corner_cams[c].append(cam)
            per_corner_px[c].append(quad[c])

    if n_views < 2:
        raise ValueError(
            f"need the marker resolved + camera-posed in >=2 registered images (got {n_views}); "
            f"check the marker is in frame and the sparse model covers those images"
        )

    corners_3d = npy.zeros((4, 3), dtype="float64")
    for c in range(4):
        corners_3d[c] = triangulate_point(per_corner_cams[c], per_corner_px[c])

    res = scale_from_corner_edges(corners_3d, marker_len_mm)
    res.n_views = n_views
    res.detail.update({"dict_name": dict_name, "marker_id": marker_id, "corners_3d": corners_3d.tolist()})
    return res


# ---------------------------------------------------------------------------------- two-point manual


def solve_scale_two_point(*, cameras: Sequence[ColmapCamera], points3d,
                          p1: Sequence[float], p2: Sequence[float], distance_mm: float,
                          image_name: Optional[str] = None) -> ScaleResult:
    """Manual scale: two pixels of known real separation in one image → ``mm_per_unit``.

    ``cameras`` is the reconstruction's posed views (from :func:`read_colmap_sparse`); ``points3d``
    is an ``(N,3)`` array of sparse world points used to depth each clicked pixel (we take the sparse
    point whose reprojection is nearest the click, then read its world position). ``image_name``
    selects which posed view the clicks came from (defaults to the only/first camera).

    Returns ``method="two_point"``; residual here is the pair's reprojection error in pixels expressed
    against the marker span (a coarse but honest confidence, not a fabricated 0 %).
    """
    npy = _require_np()
    if distance_mm <= 0:
        raise ValueError("distance_mm must be positive")
    cam_list = list(cameras)
    if not cam_list:
        raise ValueError("no cameras provided")
    cam = cam_list[0]
    if image_name is not None:
        match = [c for c in cam_list if c.image_name == image_name]
        if not match:
            raise ValueError(f"image {image_name!r} not among provided cameras")
        cam = match[0]

    pts = npy.asarray(points3d, dtype="float64")
    if pts.ndim != 2 or pts.shape[1] != 3 or pts.shape[0] == 0:
        raise ValueError("points3d must be a non-empty (N, 3) array")

    P = cam.K @ npy.hstack([cam.R, cam.t.reshape(3, 1)])
    homo = npy.hstack([pts, npy.ones((pts.shape[0], 1))])
    proj = (P @ homo.T).T
    front = proj[:, 2] > 1e-9
    proj = proj[front]
    world = pts[front]
    if proj.shape[0] == 0:
        raise ValueError("no sparse points project in front of the selected camera")
    proj2d = proj[:, :2] / proj[:, 2:3]

    def _nearest_world(click, max_err):
        d = npy.linalg.norm(proj2d - npy.asarray(click, dtype="float64"), axis=1)
        j = int(npy.argmin(d))
        return world[j], float(d[j])

    diag = float(npy.hypot(cam.width or 4000, cam.height or 3000))
    w1, e1 = _nearest_world(p1, diag)
    w2, e2 = _nearest_world(p2, diag)
    dist_units = float(npy.linalg.norm(w1 - w2))
    if dist_units <= 0:
        raise ValueError("the two clicks resolved to the same 3D point")
    mm_per_unit = distance_mm / dist_units
    span_px = float(npy.linalg.norm(npy.asarray(p1, dtype="float64") - npy.asarray(p2, dtype="float64")))
    residual_pct = (100.0 * (e1 + e2) / span_px) if span_px > 0 else 0.0
    return ScaleResult(
        method="two_point",
        mm_per_unit=mm_per_unit,
        residual_pct=residual_pct,
        n_views=1,
        detail={"dist_units": dist_units, "reproj_err_px": [e1, e2],
                "world_p1": w1.tolist(), "world_p2": w2.tolist()},
    )
