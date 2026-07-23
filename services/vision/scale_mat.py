"""Printable ArUco / AprilTag *scale mat* — the known-size fiducial the user lays beside a part.

Where :mod:`~services.vision.calib_sheet` renders a full ChArUco board for the pixel→mm homography,
a **scale mat** is the simplest possible reference for the reverse-engineering (scan) pipeline:
one or more single markers whose printed edge length is exactly ``marker_len_mm``. Dropped in the
photo/video set, the markers get reconstructed alongside the part; :mod:`~services.vision.scale_solver`
then reads their edge length in reconstruction units and recovers ``mm_per_unit`` (meyerls-style).

The output is a **print-ready PDF** (``contracts/reveng.md`` §8.2 ``reveng_print_scale_mat``): the
board/marker raster is rendered with OpenCV — reusing :mod:`~services.vision.calib_sheet` — then wrapped
to PDF via Pillow at the exact DPI so that, printed at 100 %, every square/marker is physically
``*_len_mm`` on paper. Print-at-100 % is the load-bearing invariant; a "fit to page" reprint silently
destroys the scale.

Both ArUco and AprilTag dictionaries live in **core OpenCV ``objdetect``** (Apache-2.0) — no
``opencv-contrib``, no GPU. ``cv2``/Pillow are imported lazily so importing this module never fails on
a box without them; callers hit :class:`~services.vision.fiducial.VisionUnavailable` (cv2) only when they
actually render.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from .calib_sheet import BoardSpec, DEFAULT_BOARD, _MM_PER_INCH
from .fiducial import require_cv2

#: ISO / US paper sizes in millimetres (portrait, w × h).
PAGE_SIZES_MM: dict[str, tuple[float, float]] = {
    "A4": (210.0, 297.0),
    "Letter": (215.9, 279.4),
}


@dataclass
class MarkerSpec:
    """A single printed fiducial marker whose edge is the physical scale reference.

    ``dict_name`` is a ``cv2.aruco.DICT_*`` constant name (ArUco *or* AprilTag, e.g.
    ``DICT_APRILTAG_36h11``) so the spec stays plain-JSON and version independent.
    """

    dict_name: str = "DICT_5X5_100"
    marker_id: int = 0
    marker_len_mm: float = 40.0

    def cv_image(self, *, dpi: int, border_bits: int = 1):
        """Render this marker as a numpy image at ``dpi`` (edge = ``marker_len_mm`` on paper)."""
        cv2 = require_cv2()
        const = getattr(cv2.aruco, self.dict_name, None)
        if const is None:
            raise ValueError(f"unknown ArUco/AprilTag dictionary: {self.dict_name}")
        dictionary = cv2.aruco.getPredefinedDictionary(const)
        side_px = max(1, round(self.marker_len_mm * dpi / _MM_PER_INCH))
        return cv2.aruco.generateImageMarker(dictionary, self.marker_id, side_px, borderBits=border_bits)

    def to_dict(self) -> dict[str, Any]:
        return {
            "dict_name": self.dict_name,
            "marker_id": self.marker_id,
            "marker_len_mm": self.marker_len_mm,
        }

    @classmethod
    def from_dict(cls, d: Optional[dict[str, Any]]) -> "MarkerSpec":
        d = d or {}
        return cls(
            dict_name=d.get("dict_name", "DICT_5X5_100"),
            marker_id=int(d.get("marker_id", 0)),
            marker_len_mm=float(d.get("marker_len_mm", 40.0)),
        )


#: Default scale mat: a single 40 mm ArUco marker (id 0) — big enough to reconstruct robustly.
DEFAULT_MARKER = MarkerSpec()


def _require_pillow():
    try:
        from PIL import Image  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        from .fiducial import VisionUnavailable

        raise VisionUnavailable(
            reason="Pillow (PIL) is not installed",
            hint="pip install Pillow (already an AI-CAD runtime dep)",
        ) from exc
    from PIL import Image

    return Image


def _cv_gray_to_pil(cv_img):
    """OpenCV single-channel uint8 → Pillow ``L`` image."""
    Image = _require_pillow()
    return Image.fromarray(cv_img).convert("L")


def _center_on_page(marker_pil, page_mm: tuple[float, float], dpi: int):
    """Paste ``marker_pil`` centred on a white page of ``page_mm`` at ``dpi`` (returns Pillow ``L``)."""
    Image = _require_pillow()
    page_w_px = max(1, round(page_mm[0] * dpi / _MM_PER_INCH))
    page_h_px = max(1, round(page_mm[1] * dpi / _MM_PER_INCH))
    page = Image.new("L", (page_w_px, page_h_px), color=255)
    off_x = (page_w_px - marker_pil.width) // 2
    off_y = (page_h_px - marker_pil.height) // 2
    page.paste(marker_pil, (max(0, off_x), max(0, off_y)))
    return page


def generate_scale_mat_pdf(spec: MarkerSpec = DEFAULT_MARKER, *, out_path: str,
                           size: str = "A4", dpi: int = 300) -> dict[str, Any]:
    """Render a single-marker scale mat to a print-ready PDF at ``out_path``.

    The marker is centred on the chosen page; printed at 100 % its edge is exactly
    ``spec.marker_len_mm``. Returns the geometry + the print note the caller shows the user.
    """
    if size not in PAGE_SIZES_MM:
        raise ValueError(f"unknown page size: {size!r} (expected one of {sorted(PAGE_SIZES_MM)})")
    page_mm = PAGE_SIZES_MM[size]
    marker = _cv_gray_to_pil(spec.cv_image(dpi=dpi))
    page = _center_on_page(marker, page_mm, dpi)
    # Pillow embeds `resolution` as the PDF DPI so a 100 %-scale print reproduces the mm edge.
    page.save(out_path, "PDF", resolution=float(dpi))
    return {
        "path": out_path,
        "kind": "marker",
        "spec": spec.to_dict(),
        "size": size,
        "dpi": dpi,
        "page_mm": list(page_mm),
        "marker_len_mm": spec.marker_len_mm,
        "print_note": "Print at 100% scale (no fit-to-page); lay the mat flat, in view, and keep "
                      "it rigid during capture. The marker edge is the scale reference.",
    }


def generate_charuco_mat_pdf(spec: BoardSpec = DEFAULT_BOARD, *, out_path: str,
                             size: str = "A4", dpi: int = 300, margin_mm: float = 10.0) -> dict[str, Any]:
    """Render a full ChArUco board as a print-ready PDF scale mat (reuses the board raster path).

    Same physical invariant as :func:`~services.vision.calib_sheet.generate_charuco_sheet`: printed at
    100 % the chessboard pitch is ``spec.square_len_mm``. Wrapping to PDF (vs. the calib-sheet PNG)
    is what makes it a one-click printable in the reverse-engineering deliverables flow.
    """
    if size not in PAGE_SIZES_MM:
        raise ValueError(f"unknown page size: {size!r} (expected one of {sorted(PAGE_SIZES_MM)})")
    cv2 = require_cv2()
    page_mm = PAGE_SIZES_MM[size]
    px_per_mm = dpi / _MM_PER_INCH
    w_px = round(spec.width_mm * px_per_mm)
    h_px = round(spec.height_mm * px_per_mm)
    margin_px = max(0, round(margin_mm * px_per_mm))
    board_img = spec.cv_board().generateImage((w_px, h_px), marginSize=margin_px, borderBits=1)
    board_pil = _cv_gray_to_pil(board_img)
    page = _center_on_page(board_pil, page_mm, dpi)
    page.save(out_path, "PDF", resolution=float(dpi))
    return {
        "path": out_path,
        "kind": "charuco",
        "spec": spec.to_dict(),
        "size": size,
        "dpi": dpi,
        "page_mm": list(page_mm),
        "square_len_mm": spec.square_len_mm,
        "print_note": "Print at 100% scale (no fit-to-page); keep the sheet flat and rigid, fully "
                      "in view. The chessboard pitch is the scale reference.",
    }
