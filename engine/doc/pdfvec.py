"""Minimal vector PDF writer — line art on a single page at 1:1 mm scale (W14).

No PDF library is in the frozen runtime (no reportlab / fpdf), so this hand-writes a SMALL,
spec-valid PDF 1.4 file: the five required objects (Catalog, Pages, Page, the page's content
stream, and the cross-reference table + trailer). The page content is a sequence of line-drawing
operators in the PDF graphics model.

Scale & coordinates
--------------------
PDF user space is points (1 pt = 1/72 inch). We draw at 1:1 millimetre scale by converting every
mm coordinate to points with ``MM_TO_PT = 72 / 25.4``. The PDF origin is bottom-left with +y up —
which already matches the engine's sheet/flat coordinate convention (+y up), so no Y flip is needed.
A small margin is added and the whole drawing is translated so its min corner sits at the margin.

Public API
----------
``write_pdf(path, lines, page_mm=None)``
    *lines* is a list of ``(polyline, dashed)`` where polyline is ``list[[x, y]]`` mm and *dashed*
    marks hidden lines (drawn with a dash pattern). ``page_mm=(w, h)`` sets the page size in mm;
    when ``None`` the page is sized to the drawing's bounding box plus a margin.

The output is a valid ``%PDF-1.4`` file that opens in any viewer; solid strokes for visible lines,
a dashed stroke for hidden lines.
"""

from __future__ import annotations

import os
from pathlib import Path

MM_TO_PT = 72.0 / 25.4
_MARGIN_MM = 10.0


def _bbox(lines: list) -> tuple[float, float, float, float] | None:
    xs: list[float] = []
    ys: list[float] = []
    for pl, _dashed in lines:
        for p in pl:
            if p is None or len(p) < 2:
                continue
            xs.append(float(p[0]))
            ys.append(float(p[1]))
    if not xs:
        return None
    return min(xs), min(ys), max(xs), max(ys)


def _content_stream(lines: list, dx_mm: float, dy_mm: float) -> bytes:
    """Build the page content stream: stroke each polyline, dashed when flagged.

    ``dx_mm/dy_mm`` translate drawing-mm so the min corner lands at the margin. Everything is then
    scaled to points. We set a thin line width (0.35 mm ~ a fine pen) and toggle the dash pattern
    per polyline.
    """
    ops: list[str] = []
    ops.append(f"{0.35 * MM_TO_PT:.3f} w")  # line width in pt
    last_dashed: bool | None = None
    for pl, dashed in lines:
        pts = [p for p in pl if p is not None and len(p) >= 2]
        if len(pts) < 2:
            continue
        if dashed != last_dashed:
            if dashed:
                d = 2.5 * MM_TO_PT
                ops.append(f"[{d:.3f} {d:.3f}] 0 d")  # dash on/off
            else:
                ops.append("[] 0 d")                  # solid
            last_dashed = dashed
        x0 = (float(pts[0][0]) + dx_mm) * MM_TO_PT
        y0 = (float(pts[0][1]) + dy_mm) * MM_TO_PT
        ops.append(f"{x0:.3f} {y0:.3f} m")
        for p in pts[1:]:
            x = (float(p[0]) + dx_mm) * MM_TO_PT
            y = (float(p[1]) + dy_mm) * MM_TO_PT
            ops.append(f"{x:.3f} {y:.3f} l")
        ops.append("S")  # stroke the current path
    return ("\n".join(ops) + "\n").encode("latin-1")


def write_pdf(path: str | os.PathLike, lines: list,
              page_mm: tuple[float, float] | None = None) -> str:
    """Write *lines* to a single-page vector PDF at *path*.

    Parameters
    ----------
    path:
        Destination ``.pdf`` file (parent dirs created).
    lines:
        List of ``(polyline, dashed)``; polyline is ``list[[x, y]]`` mm, *dashed* True for hidden
        lines. Polylines with <2 points are skipped.
    page_mm:
        ``(width_mm, height_mm)`` page size. When ``None`` the page is sized to the drawing bbox +
        a 10 mm margin all round.

    Returns
    -------
    str
        The resolved path written.

    Raises
    ------
    ValueError
        When *lines* has no drawable geometry.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)

    bb = _bbox(lines)
    if bb is None:
        raise ValueError("write_pdf: no line geometry to export")
    minx, miny, maxx, maxy = bb

    # Translate so the drawing's min corner sits at the margin, then choose a page that contains it.
    dx_mm = _MARGIN_MM - minx
    dy_mm = _MARGIN_MM - miny
    if page_mm is not None:
        page_w_mm, page_h_mm = float(page_mm[0]), float(page_mm[1])
        # When the caller fixes the page, keep the drawing in its native position (just margin-shift
        # only if it would fall off the bottom-left); a fixed page is the sheet itself.
        dx_mm = 0.0
        dy_mm = 0.0
    else:
        page_w_mm = (maxx - minx) + 2 * _MARGIN_MM
        page_h_mm = (maxy - miny) + 2 * _MARGIN_MM

    page_w_pt = page_w_mm * MM_TO_PT
    page_h_pt = page_h_mm * MM_TO_PT

    content = _content_stream(lines, dx_mm, dy_mm)

    # --- assemble the PDF objects ---
    objects: list[bytes] = []
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>")
    objects.append(
        (f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {page_w_pt:.3f} {page_h_pt:.3f}] "
         f"/Contents 4 0 R /Resources << >> >>").encode("latin-1"))
    objects.append(
        (f"<< /Length {len(content)} >>\nstream\n").encode("latin-1")
        + content + b"endstream")

    # --- serialize with a byte-accurate xref table ---
    buf = bytearray()
    buf += b"%PDF-1.4\n"
    buf += b"%\xe2\xe3\xcf\xd3\n"  # binary marker so tools treat the file as binary
    offsets: list[int] = []
    for i, obj in enumerate(objects, start=1):
        offsets.append(len(buf))
        buf += f"{i} 0 obj\n".encode("latin-1")
        buf += obj
        buf += b"\nendobj\n"

    xref_pos = len(buf)
    n = len(objects)
    buf += f"xref\n0 {n + 1}\n".encode("latin-1")
    buf += b"0000000000 65535 f \n"
    for off in offsets:
        buf += f"{off:010d} 00000 n \n".encode("latin-1")
    buf += b"trailer\n"
    buf += f"<< /Size {n + 1} /Root 1 0 R >>\n".encode("latin-1")
    buf += b"startxref\n"
    buf += f"{xref_pos}\n".encode("latin-1")
    buf += b"%%EOF\n"

    p.write_bytes(bytes(buf))
    return str(p)
