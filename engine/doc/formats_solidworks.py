"""SolidWorks (.sldprt / .sldasm) + Parasolid (.x_t / .x_b) — experimental, opportunistic import.

These are proprietary closed formats; there is **no** free standalone B-rep reader (see
FORMATS_PLAN.md §2 D3 for the honest survey). The strategy here is a graceful fallback chain, none
of it on the critical path — every entry point degrades cleanly when SolidWorks / pywin32 / olefile
are absent:

  1. **PRIMARY — SolidWorks COM**: if SolidWorks is installed + licensed on this machine, drive it
     via COM (pywin32) to export a neutral **STEP**, then import that through the B-rep lane. Best
     fidelity, free when SW is present, Windows-only. Used for .sldprt/.sldasm AND .x_t/.x_b
     (SolidWorks imports Parasolid natively).
  2. **EXPERIMENTAL — OLE2 probe**: *legacy* (pre-~2015) .sldprt/.sldasm are Microsoft Compound-File
     (OLE2) containers; we can enumerate their streams and pull the embedded preview.
     **Reverse-engineering verdict (SW 2025, measured):** the modern container is a fully
     COMPRESSED/ENCRYPTED opaque blob — per-file-varying magic (``5e4bf6ca``/``1bd70589``/… not
     ``d0cf11e0``), ~7.9 bits/byte Shannon entropy across the whole file, no plaintext stream names,
     no PNG/BMP preview, no recognizable geometry markers. There is **no standalone way to extract
     geometry or even a preview** without SolidWorks's proprietary codec, so :func:`ole2_streams`
     returns [] and we rely on COM. (Confirmed against the owner's "sldprt reversing" test parts.)
  3. **FALLBACK**: a clear, actionable error telling the user to export STEP from SolidWorks.

Reverse-engineering note: to advance the OLE2 mesh path we need real sample files. See
``docs/FORMATS_STATUS.md`` for the "test parts to create" checklist.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Capability detection
# ---------------------------------------------------------------------------
def _win32com_available() -> bool:
    try:
        import win32com.client  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


def solidworks_available() -> bool:
    """True iff this machine looks like it can COM-automate SolidWorks (pywin32 + SW registered).

    Does NOT launch SolidWorks — just checks pywin32 import and the SldWorks COM registration in the
    registry, so it is cheap to call as a feature gate."""
    if not _win32com_available():
        return False
    try:
        import winreg
        for hive in (winreg.HKEY_CLASSES_ROOT,):
            try:
                winreg.OpenKey(hive, r"SldWorks.Application").Close()
                return True
            except OSError:
                continue
    except Exception:  # noqa: BLE001
        pass
    return False


# ---------------------------------------------------------------------------
# Primary path — SolidWorks COM → STEP
# ---------------------------------------------------------------------------
# SolidWorks document type ids (swDocumentTypes_e).
_SW_DOCTYPE = {".sldprt": 1, ".sldasm": 2, ".slddrw": 3}  # PART / ASSEMBLY / DRAWING


def sw_com_export_step(src_path: str | os.PathLike, out_step: str | None = None) -> str:
    """Open *src_path* in SolidWorks via COM and export it to a STEP file; return the STEP path.

    Raises RuntimeError with an actionable message if SolidWorks/pywin32 are unavailable or the
    automation fails. SolidWorks is opened **invisibly** and the document is closed afterwards."""
    if not _win32com_available():
        raise RuntimeError("SolidWorks COM export needs pywin32 (win32com), which is not installed.")

    import pythoncom  # noqa: F401  (ensures COM is initialised on this thread)
    import win32com.client as win32

    src = str(Path(src_path).resolve())
    ext = Path(src).suffix.lower()
    doctype = _SW_DOCTYPE.get(ext, 1)
    if out_step is None:
        out_step = str(Path(tempfile.mkdtemp(prefix="sw_")) / (Path(src).stem + ".step"))

    swApp = None
    opened = False
    try:
        swApp = win32.Dispatch("SldWorks.Application")
        swApp.Visible = False
        # OpenDoc6(filename, type, options, config, errors, warnings) — use the simpler OpenDoc.
        errors = win32.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0) if hasattr(win32, "VARIANT") else 0
        try:
            model = swApp.OpenDoc6(src, doctype, 1, "", 0, 0)  # 1 = swOpenDocOptions_Silent
        except Exception:
            model = swApp.OpenDoc(src, doctype)
        if model is None:
            raise RuntimeError(f"SolidWorks could not open '{src}'")
        opened = True
        ok = model.SaveAs3(out_step, 0, 0) if hasattr(model, "SaveAs3") else model.SaveAs(out_step)
        # SaveAs* returns nonzero/True-ish on success across versions; verify by file presence.
        if not os.path.exists(out_step) or os.path.getsize(out_step) == 0:
            raise RuntimeError(f"SolidWorks STEP export produced no file (SaveAs returned {ok!r}).")
        return out_step
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"SolidWorks COM export failed: {exc}") from exc
    finally:
        try:
            if swApp is not None and opened:
                swApp.CloseDoc(src)
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# Experimental path — OLE2 container probe
# ---------------------------------------------------------------------------
def ole2_streams(path: str | os.PathLike) -> list[str]:
    """List the OLE2/Compound-File stream paths inside a .sldprt/.sldasm (reverse-engineering aid).

    Returns [] if olefile is unavailable or the file is not an OLE2 container."""
    try:
        import olefile
    except Exception:  # noqa: BLE001
        return []
    if not olefile.isOleFile(str(path)):
        return []
    with olefile.OleFileIO(str(path)) as ole:
        return ["/".join(parts) for parts in ole.listdir()]


def sldprt_extract_thumbnail(path: str | os.PathLike) -> bytes | None:
    """Return the embedded preview image bytes (PNG/BMP) from a .sldprt/.sldasm, or None.

    SolidWorks stores a preview under a 'PreviewPNG' / 'Preview Bitmap' style stream. This is the
    most reliably extractable artifact and is useful for shell thumbnails even when geometry can't
    be read."""
    try:
        import olefile
    except Exception:  # noqa: BLE001
        return None
    if not olefile.isOleFile(str(path)):
        return None
    with olefile.OleFileIO(str(path)) as ole:
        candidates = []
        for parts in ole.listdir():
            name = "/".join(parts).lower()
            if "preview" in name or "thumbnail" in name or name.endswith("png"):
                candidates.append(parts)
        for parts in candidates:
            try:
                data = ole.openstream(parts).read()
                if data and len(data) > 16:
                    return data
            except Exception:  # noqa: BLE001
                continue
    return None


def sldprt_extract_preview_mesh(path: str | os.PathLike):  # -> MeshBody | None
    """Extract an embedded tessellated graphics body as a MeshBody — only feasible for LEGACY
    (OLE2) SolidWorks files.

    For modern SolidWorks (≈2015+, incl. SW 2025) the container is fully compressed/encrypted
    (~7.9 bits/byte entropy, no plaintext markers — see the module docstring), so there is nothing
    to parse and this returns None. Geometry from modern parts must come through the COM→STEP path.
    Kept as the hook for legacy files / future codec work."""
    return None


# ---------------------------------------------------------------------------
# Public import entry points (called from engine.doc.formats.import_path)
# ---------------------------------------------------------------------------
def _via_solidworks_com(path: str | os.PathLike) -> list[tuple[str, Any]] | None:
    """Try the COM→STEP path; return imported bodies, or None if SolidWorks isn't usable."""
    if not solidworks_available():
        return None
    try:
        step = sw_com_export_step(path)
    except Exception:  # noqa: BLE001
        return None
    from engine.doc.formats import import_path
    return import_path(step, "step")


def import_solidworks(path: str | os.PathLike) -> list[tuple[str, Any]]:
    """Import a .sldprt/.sldasm: COM→STEP, else embedded preview mesh, else a clear error."""
    bodies = _via_solidworks_com(path)
    if bodies:
        return bodies

    mesh = sldprt_extract_preview_mesh(path)
    if mesh is not None:
        return [(Path(path).stem, mesh)]

    raise ValueError(
        f"cannot read SolidWorks file '{Path(path).name}' directly. SolidWorks/Parasolid are "
        "proprietary — open the part in SolidWorks and 'Save As' STEP (.step), then import that. "
        "(If SolidWorks is installed, automatic STEP export needs pywin32 in the engine runtime.)")


def import_parasolid(path: str | os.PathLike) -> list[tuple[str, Any]]:
    """Import a Parasolid .x_t/.x_b: only via SolidWorks COM (SW imports Parasolid), else error."""
    bodies = _via_solidworks_com(path)
    if bodies:
        return bodies
    raise ValueError(
        f"cannot read Parasolid file '{Path(path).name}' — no free Parasolid kernel. Open it in "
        "SolidWorks/NX and export STEP (.step), then import that.")
