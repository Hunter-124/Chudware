"""Printable ArUco / ChArUco *scale mats* — the known-size fiducial the user lays beside a part.

The standard mat (:data:`MAT_BOARD`, printed by ``reveng_print_scale_mat``) is a full-page ChArUco
board: every chessboard corner it resolves is triangulated against the reconstruction, and
:func:`~services.vision.scale_solver.solve_scale_charuco` fits the board's known layout to them to
recover ``mm_per_unit``. The legacy single-marker mat (:class:`MarkerSpec`, edge ``marker_len_mm``)
is still solved by :func:`~services.vision.scale_solver.solve_scale_aruco` for mats already printed.

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
            hint="pip install Pillow (already an Chudware runtime dep)",
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


#: The printed scale mat: 7 × 10 squares of 25 mm (175 × 250 mm) fills Letter and A4 alike.
#: Every chessboard corner is a scale observation, so one sheet spans ~300 mm of corners.
MAT_BOARD = BoardSpec(squares_x=7, squares_y=10, square_len_mm=25.0, marker_len_mm=18.0)


def generate_charuco_mat_pdf(spec: BoardSpec = MAT_BOARD, *, out_path: str,
                             size: str = "A4", dpi: int = 300) -> dict[str, Any]:
    """Render a full ChArUco board as a print-ready PDF scale mat, plus a PNG preview.

    The board raster is exactly ``spec.width_mm × spec.height_mm`` with no internal margin
    (OpenCV shrinks a board to fit any ``marginSize``, which would change the printed pitch);
    the page margin is the quiet zone. Printed at 100 % the chessboard pitch is
    ``spec.square_len_mm``, and the caption states it so the print can be checked with a ruler.
    """
    if size not in PAGE_SIZES_MM:
        raise ValueError(f"unknown page size: {size!r} (expected one of {sorted(PAGE_SIZES_MM)})")
    page_mm = PAGE_SIZES_MM[size]
    if spec.width_mm > page_mm[0] - 10 or spec.height_mm > page_mm[1] - 16:
        raise ValueError(f"{spec.width_mm:g} x {spec.height_mm:g} mm board does not fit {size} with a quiet zone")
    px_per_mm = dpi / _MM_PER_INCH
    w_px = round(spec.width_mm * px_per_mm)
    h_px = round(spec.height_mm * px_per_mm)
    board_img = spec.cv_board().generateImage((w_px, h_px), marginSize=0, borderBits=1)
    page = _center_on_page(_cv_gray_to_pil(board_img), page_mm, dpi)
    from PIL import ImageDraw, ImageFont
    caption = (f"ChArUco {spec.squares_x}x{spec.squares_y} {spec.dict_name} - print at 100%: "
               f"square = {spec.square_len_mm:g} mm, {spec.squares_y} squares = "
               f"{spec.height_mm:g} mm")
    font = ImageFont.load_default(size=max(10, round(2.6 * px_per_mm)))
    bottom = (page.height + h_px) // 2
    ImageDraw.Draw(page).text((page.width // 2, bottom + round(2 * px_per_mm)), caption,
                              fill=0, font=font, anchor="mt")
    page.save(out_path, "PDF", resolution=float(dpi))
    preview_path = out_path.rsplit(".", 1)[0] + ".png"
    page.resize((page.width // 4, page.height // 4)).save(preview_path)
    return {
        "path": out_path,
        "preview_path": preview_path,
        "kind": "charuco",
        "spec": spec.to_dict(),
        "size": size,
        "dpi": dpi,
        "page_mm": list(page_mm),
        "square_len_mm": spec.square_len_mm,
        "print_note": "Print at 100% scale (no fit-to-page) and check one printed square against "
                      f"{spec.square_len_mm:g} mm. Tape the sheet flat and keep it fully in view; "
                      "the chessboard pitch is the scale reference.",
    }


def generate_scale_mat(size: str, out_dir: str) -> dict[str, Any]:
    """Write the standard :data:`MAT_BOARD` scale mat for ``size`` into ``out_dir``."""
    import os

    os.makedirs(out_dir, exist_ok=True)
    spec = MAT_BOARD
    name = f"charuco_{spec.squares_x}x{spec.squares_y}_{spec.square_len_mm:g}mm_{size}.pdf"
    return generate_charuco_mat_pdf(spec, out_path=os.path.join(out_dir, name), size=size)
