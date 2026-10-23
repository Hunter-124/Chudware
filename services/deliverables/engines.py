"""Engine wrappers (contract §7): in-process rasterization + subprocess converter discovery.

Split exactly along the license guardrail:

- **In-process** (permissive MIT/BSD/PSF): pypdfium2 (PDF→PNG), pillow (image downscale),
  matplotlib (text/csv/md → PNG). Always available.
- **Subprocess-only** (GPL pandoc, MPL LibreOffice): *discovered on PATH*, invoked as external
  processes, never linked/bundled. Absent → :class:`DeliverableError` ``converter_missing`` so
  CI passes with neither installed.

Never PyMuPDF/pymupdf4llm (AGPL) — the PDF→PNG path is pypdfium2, full stop.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from .errors import DeliverableError

# Preview caps (contract §5.4): ≤ 1024 px long edge, < 1 MB.
MAX_LONG_EDGE = 1024
MAX_BYTES = 1_000_000

_OFFICE_EXTS = {
    "pptx", "ppt", "odp", "docx", "doc", "odt", "xlsx", "xlsm", "xls", "ods", "rtf",
}
_IMAGE_EXTS = {"png", "jpg", "jpeg", "bmp", "gif", "tif", "tiff"}
_TEXT_EXTS = {"csv", "html", "md", "markdown", "txt"}


# --------------------------------------------------------------------------- discovery

def soffice_bin() -> str | None:
    """LibreOffice headless binary on PATH, or None (patched to None in the CI degradation test)."""
    return shutil.which("soffice") or shutil.which("libreoffice")


def pandoc_bin() -> str | None:
    return shutil.which("pandoc")


# --------------------------------------------------------------------------- subprocess

def office_to_pdf(src: Path, outdir: Path) -> Path:
    """Convert an office file to PDF via LibreOffice headless (subprocess). Never bundled.

    Raises ``converter_missing`` when LibreOffice is not on PATH (the office file itself is
    untouched — only the preview degrades, contract §5.4).
    """
    binary = soffice_bin()
    if not binary:
        raise DeliverableError("converter_missing", "install libreoffice")
    outdir.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [binary, "--headless", "--convert-to", "pdf", "--outdir", str(outdir), str(src)],
        capture_output=True, text=True, timeout=120,
    )
    pdf = outdir / (src.stem + ".pdf")
    if proc.returncode != 0 or not pdf.exists():
        raise DeliverableError(
            "converter_missing",
            f"libreoffice could not convert {src.name}: {(proc.stderr or '').strip()[:200]}")
    return pdf


# --------------------------------------------------------------------------- rasterize

def _save_capped(pil_img, dest: Path) -> dict:
    """Save a PIL image as PNG, downscaled to ≤ MAX_LONG_EDGE and < MAX_BYTES. Returns image dict."""
    from PIL import Image  # noqa: PLC0415

    img = pil_img
    if img.mode not in ("RGB", "RGBA", "L"):
        img = img.convert("RGB")
    long_edge = max(img.size)
    if long_edge > MAX_LONG_EDGE:
        scale = MAX_LONG_EDGE / long_edge
        img = img.resize((max(1, int(img.width * scale)), max(1, int(img.height * scale))),
                         Image.LANCZOS)
    dest.parent.mkdir(parents=True, exist_ok=True)
    img.save(dest, format="PNG", optimize=True)
    # Byte cap: shrink until under MAX_BYTES (a handful of steps at most).
    for _ in range(6):
        if dest.stat().st_size < MAX_BYTES:
            break
        img = img.resize((max(1, int(img.width * 0.8)), max(1, int(img.height * 0.8))),
                         Image.LANCZOS)
        img.save(dest, format="PNG", optimize=True)
    return {"w": img.width, "h": img.height, "bytes": dest.stat().st_size}


def pdf_to_pngs(pdf: Path, dest_dir: Path, stem: str) -> list[dict]:
    """Rasterize every PDF page to a capped PNG via pypdfium2 (in-process). Returns page dicts."""
    import pypdfium2 as pdfium  # noqa: PLC0415

    doc = pdfium.PdfDocument(str(pdf))
    pages: list[dict] = []
    try:
        for i in range(len(doc)):
            page = doc[i]
            w_pt, h_pt = page.get_size()
            scale = MAX_LONG_EDGE / max(w_pt, h_pt) if max(w_pt, h_pt) else 1.0
            bitmap = page.render(scale=scale)
            pil = bitmap.to_pil()
            out = dest_dir / f"{stem}_p{i + 1}.png"
            info = _save_capped(pil, out)
            pages.append({"index": i + 1, "_abs": out, "image": info})
    finally:
        doc.close()
    return pages


def image_to_png(src: Path, dest_dir: Path, stem: str) -> list[dict]:
    from PIL import Image  # noqa: PLC0415

    with Image.open(src) as im:
        im.load()
        out = dest_dir / f"{stem}_p1.png"
        info = _save_capped(im, out)
    return [{"index": 1, "_abs": out, "image": info}]


def text_to_png(src: Path, dest_dir: Path, stem: str) -> list[dict]:
    """Render a text/csv/md file to a preview PNG in-process via matplotlib (no subprocess)."""
    import matplotlib  # noqa: PLC0415

    matplotlib.use("Agg", force=True)
    import matplotlib.pyplot as plt  # noqa: PLC0415

    text = src.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines() or [""]
    lines = lines[:60]  # a preview, not the whole file
    fig = plt.figure(figsize=(8.5, 11), dpi=96)
    fig.text(0.02, 0.98, "\n".join(lines), va="top", ha="left", family="monospace", fontsize=8)
    out = dest_dir / f"{stem}_p1.png"
    fig.savefig(out, format="png")
    plt.close(fig)
    from PIL import Image  # noqa: PLC0415

    with Image.open(out) as im:
        im.load()
        info = _save_capped(im, out)
    return [{"index": 1, "_abs": out, "image": info}]


def svg_to_pngs(src: Path, dest_dir: Path, stem: str) -> list[dict]:
    """Best-effort SVG preview: cairosvg if importable, else LibreOffice office path.

    The frozen §7 engine set has no permissive in-process SVG rasterizer; this
    degrades to ``converter_missing`` when neither an
    optional cairosvg nor LibreOffice is available, so CI stays green.
    """
    try:  # optional, not a declared dep — used only if the environment already has it.
        import cairosvg  # noqa: PLC0415

        png_bytes = cairosvg.svg2png(url=str(src), output_width=MAX_LONG_EDGE)
        from io import BytesIO  # noqa: PLC0415
        from PIL import Image  # noqa: PLC0415

        with Image.open(BytesIO(png_bytes)) as im:
            im.load()
            out = dest_dir / f"{stem}_p1.png"
            info = _save_capped(im, out)
        return [{"index": 1, "_abs": out, "image": info}]
    except Exception:  # noqa: BLE001 - fall through to the office path
        pass
    pdf = office_to_pdf(src, dest_dir)  # raises converter_missing if no LibreOffice
    return pdf_to_pngs(pdf, dest_dir, stem)


def classify_ext(ext: str) -> str:
    ext = ext.lower().lstrip(".")
    if ext == "pdf":
        return "pdf"
    if ext == "svg":
        return "svg"
    if ext in _IMAGE_EXTS:
        return "image"
    if ext in _TEXT_EXTS:
        return "text"
    if ext in _OFFICE_EXTS:
        return "office"
    return "unsupported"
