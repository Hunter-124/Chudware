"""Fiducial detection + pixel→mm homography — the deterministic verifier.

Detect a ChArUco board, fit the homography that maps image pixels to the board's physical
millimetre plane, and read out the two signals the confidence tier is built from:

* **resolution** — ``px_per_mm`` from the median detected marker's pixel size. More pixels per
  millimetre ⇒ a tighter measurement.
* **planarity** — the coefficient of variation of the marker pixel *areas*. On a fronto-parallel
  flat sheet every marker images at ~the same area; under tilt the near markers swell and the far
  ones shrink, so a high CoV is a direct, intrinsic "the board is out of plane" flag — no camera
  intrinsics required.

``cv2`` (Apache-2.0, core ``objdetect``) is optional: this module imports without it and exposes
:data:`HAVE_CV2` / :func:`require_cv2` so callers degrade to a contract-shaped error.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Any, Optional

try:  # OpenCV is an optional runtime dep — never fail import if it is not provisioned.
    import cv2  # type: ignore
    import numpy as np  # type: ignore
    HAVE_CV2 = True
except Exception:  # noqa: BLE001  (ImportError or a broken partial install)
    cv2 = None  # type: ignore
    np = None  # type: ignore
    HAVE_CV2 = False

_CV2_HINT = "pip install opencv-python-headless (Apache-2.0; ArUco is in core objdetect)"


class VisionUnavailable(RuntimeError):
    """Raised when a fiducial op needs OpenCV but it is not installed.

    Carries ``reason`` + ``hint`` so the MCP adapter can surface the contract-shaped
    ``{ok: False, reason, hint}`` degradation instead of a traceback.
    """

    def __init__(self, reason: str = "OpenCV (cv2) is not installed", hint: str = _CV2_HINT):
        super().__init__(reason)
        self.reason = reason
        self.hint = hint


def require_cv2():
    """Return the ``cv2`` module or raise :class:`VisionUnavailable`."""
    if not HAVE_CV2:
        raise VisionUnavailable()
    return cv2


@dataclass
class Detection:
    """The fitted board: everything :mod:`~services.vision.measure` needs to measure + score."""

    homography_px_to_mm: Any                 # 3×3 numpy array mapping image px → board mm
    n_corners: int
    n_markers: int
    px_per_mm: float
    planarity_cov: float                     # CoV of marker pixel areas (0 = perfectly flat/frontal)
    image_size: tuple[int, int]              # (w, h) px
    marker_areas_px: list[float] = field(default_factory=list)

    def px_to_mm(self, px_a, px_b) -> tuple[tuple[float, float], tuple[float, float]]:
        pts = np.array([[list(px_a)], [list(px_b)]], dtype="float64")
        out = cv2.perspectiveTransform(pts, self.homography_px_to_mm)
        a = (float(out[0, 0, 0]), float(out[0, 0, 1]))
        b = (float(out[1, 0, 0]), float(out[1, 0, 1]))
        return a, b


def load_image(image_path: str):
    """Read an image to BGR ndarray, or raise a clear error the adapter can wrap."""
    cv = require_cv2()
    img = cv.imread(str(image_path), cv.IMREAD_COLOR)
    if img is None:
        raise OSError(f"could not read image: {image_path}")
    return img


def detect_board(image, spec) -> Optional[Detection]:
    """Detect ``spec`` in ``image`` (BGR ndarray) and fit the px→mm homography.

    Returns ``None`` when too few chessboard corners are found to fit a homography (the caller
    turns that into ``{ok: False, reason: "no fiducial detected"}``).
    """
    cv = require_cv2()
    board = spec.cv_board()
    detector = cv.aruco.CharucoDetector(board)
    charuco_corners, charuco_ids, marker_corners, _marker_ids = detector.detectBoard(image)

    if charuco_corners is None or charuco_ids is None or len(charuco_ids) < 4:
        return None

    # Board-plane mm coords for each detected chessboard corner (getChessboardCorners is in the
    # same units the board was built with — millimetres).
    board_pts_mm = board.getChessboardCorners()  # (M, 3) in mm
    ids = charuco_ids.flatten()
    dst_mm = np.array([board_pts_mm[i][:2] for i in ids], dtype="float64")
    src_px = charuco_corners.reshape(-1, 2).astype("float64")

    homography, _mask = cv.findHomography(src_px, dst_mm, cv.RANSAC, 3.0)
    if homography is None:
        return None

    areas: list[float] = []
    if marker_corners is not None:
        for mc in marker_corners:
            quad = np.asarray(mc, dtype="float64").reshape(-1, 2)
            if quad.shape[0] == 4:
                areas.append(abs(float(cv.contourArea(quad.astype("float32")))))

    if areas:
        median_area = statistics.median(areas)
        px_per_mm = (median_area ** 0.5) / float(spec.marker_len_mm) if median_area > 0 else 0.0
        mean_area = sum(areas) / len(areas)
        planarity_cov = (statistics.pstdev(areas) / mean_area) if mean_area > 0 and len(areas) > 1 else 0.0
    else:
        # No full markers resolved but corners fit — derive px/mm straight from the homography
        # (map a 1 mm board vector back to pixels via the inverse) and treat planarity as unknown.
        inv = np.linalg.inv(homography)
        o = cv.perspectiveTransform(np.array([[[0.0, 0.0]]]), inv)[0, 0]
        e = cv.perspectiveTransform(np.array([[[1.0, 0.0]]]), inv)[0, 0]
        px_per_mm = float(np.hypot(e[0] - o[0], e[1] - o[1]))
        planarity_cov = 0.0

    h, w = image.shape[:2]
    return Detection(
        homography_px_to_mm=homography,
        n_corners=int(len(ids)),
        n_markers=len(areas),
        px_per_mm=float(px_per_mm),
        planarity_cov=float(planarity_cov),
        image_size=(int(w), int(h)),
        marker_areas_px=areas,
    )
