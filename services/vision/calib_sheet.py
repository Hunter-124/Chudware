"""Printable ChArUco / ArUco calibration-sheet generator.

The fiducial the user lays in-frame. ChArUco (a chessboard fused with ArUco markers) gives
sub-pixel corner localisation *and* unambiguous identity, so a single flat sheet fixes the
pixel→mm homography that :mod:`~services.vision.measure` needs. ArUco lives in **core OpenCV
``objdetect``** (Apache-2.0) — no ``opencv-contrib``, no GPU.

``cv2`` is imported lazily inside each function so importing this module never fails on a box
without OpenCV; callers hit :class:`~services.vision.fiducial.VisionUnavailable` only when they
actually try to generate a sheet.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Optional

from .fiducial import require_cv2

_MM_PER_INCH = 25.4


@dataclass
class BoardSpec:
    """A ChArUco board's physical geometry — the ground truth the homography is scaled to.

    Lengths are millimetres. ``square_len_mm`` is the chessboard pitch (what actually sets the
    scale); ``marker_len_mm`` is the ArUco square nested in each white cell. ``dict_name`` is a
    ``cv2.aruco.DICT_*`` constant name so the spec is plain-JSON and version-independent.
    """

    dict_name: str = "DICT_5X5_100"
    squares_x: int = 5
    squares_y: int = 7
    square_len_mm: float = 30.0
    marker_len_mm: float = 22.0

    @property
    def width_mm(self) -> float:
        return self.squares_x * self.square_len_mm

    @property
    def height_mm(self) -> float:
        return self.squares_y * self.square_len_mm

    def cv_dictionary(self):
        cv2 = require_cv2()
        const = getattr(cv2.aruco, self.dict_name, None)
        if const is None:
            raise ValueError(f"unknown ArUco dictionary: {self.dict_name}")
        return cv2.aruco.getPredefinedDictionary(const)

    def cv_board(self):
        cv2 = require_cv2()
        return cv2.aruco.CharucoBoard(
            (self.squares_x, self.squares_y),
            float(self.square_len_mm),
            float(self.marker_len_mm),
            self.cv_dictionary(),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "dict_name": self.dict_name,
            "squares_x": self.squares_x,
            "squares_y": self.squares_y,
            "square_len_mm": self.square_len_mm,
            "marker_len_mm": self.marker_len_mm,
        }

    @classmethod
    def from_dict(cls, d: Optional[dict[str, Any]]) -> "BoardSpec":
        d = d or {}
        return cls(
            dict_name=d.get("dict_name", "DICT_5X5_100"),
            squares_x=int(d.get("squares_x", 5)),
            squares_y=int(d.get("squares_y", 7)),
            square_len_mm=float(d.get("square_len_mm", 30.0)),
            marker_len_mm=float(d.get("marker_len_mm", 22.0)),
        )


#: Default sheet: 5×7 of 30 mm squares (150 × 210 mm — fits A4/Letter with margin).
DEFAULT_BOARD = BoardSpec()


def generate_charuco_sheet(spec: BoardSpec = DEFAULT_BOARD, *, out_path: str,
                           dpi: int = 300, margin_mm: float = 10.0) -> dict[str, Any]:
    """Render ``spec`` to a print-ready PNG at ``dpi`` and write it to ``out_path``.

    The printed square pitch IS the scale reference, so the caller must print at 100 % (no
    "fit to page"). Returns the geometry the user needs on the sheet + measurement.
    """
    cv2 = require_cv2()
    px_per_mm = dpi / _MM_PER_INCH
    w_px = round(spec.width_mm * px_per_mm)
    h_px = round(spec.height_mm * px_per_mm)
    margin_px = max(0, round(margin_mm * px_per_mm))
    board = spec.cv_board()
    img = board.generateImage((w_px, h_px), marginSize=margin_px, borderBits=1)
    if not cv2.imwrite(out_path, img):
        raise OSError(f"could not write calibration sheet to {out_path}")
    return {
        "path": out_path,
        "spec": spec.to_dict(),
        "dpi": dpi,
        "width_mm": spec.width_mm,
        "height_mm": spec.height_mm,
        "square_len_mm": spec.square_len_mm,
        "print_note": "Print at 100% scale (no fit-to-page); keep the sheet flat and in the "
                      "measurement plane.",
    }


def generate_aruco_marker(marker_id: int = 0, *, dict_name: str = "DICT_5X5_100",
                          size_px: int = 240):
    """Return a single ArUco marker as a numpy image (used to composite synthetic test photos)."""
    cv2 = require_cv2()
    const = getattr(cv2.aruco, dict_name, None)
    if const is None:
        raise ValueError(f"unknown ArUco dictionary: {dict_name}")
    dictionary = cv2.aruco.getPredefinedDictionary(const)
    return cv2.aruco.generateImageMarker(dictionary, marker_id, size_px)
