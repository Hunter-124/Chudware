"""Watched "parts library" folder -> auto-register into the vendor-part registry.

Users (or the host) drop CAD files into ``%APPDATA%/AI-CAD/parts_library``; scanning the
folder registers every new or changed file into the same registry that ``vendor_parts``
owns (and that the C++ core reads via ``index.json``). This module is a thin scanner on
top of :mod:`engine.vendor_parts`; that module remains the registry owner.

Layout convention::

    parts_library/
        McMaster-Carr/91251A542.step   -> supplier="McMaster-Carr"
        fasteners/M3x10.step           -> supplier="fasteners"
        loose_part.step                -> supplier="library"   (no top-level subfolder)

Re-scanning is idempotent: a file already registered with the same resolved path and the
same mtime is skipped (``source_mtime`` on the record). A file whose mtime changed is
re-registered (counted as "updated").
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable

from engine import paths, vendor_parts
from engine.vendor_parts import CAD_EXTS, slug


# "M3x10", "M3x0.5x10", "M6-1.0x20" -> thread / pitch / length where obvious.
_THREAD_RE = re.compile(
    r"\b(?P<thread>M\d+(?:\.\d+)?)"
    r"(?:[x\-](?P<pitch>\d+(?:\.\d+)?))?"     # optional metric pitch
    r"(?:[x\-](?P<length>\d+(?:\.\d+)?))\b",  # length
    re.IGNORECASE,
)
# Bare "M3x10" (thread x length, no pitch).
_THREAD_SHORT_RE = re.compile(
    r"\b(?P<thread>M\d+(?:\.\d+)?)[x\-](?P<length>\d+(?:\.\d+)?)\b", re.IGNORECASE)


def parse_specs_from_name(stem: str) -> dict[str, Any]:
    """Best-effort spec extraction from a filename stem.

    Recognizes common fastener shorthand like ``M3x10`` or ``M6-1.0x20``. Returns an empty
    dict when nothing obvious is found (never raises).
    """
    specs: dict[str, Any] = {}
    m = _THREAD_RE.search(stem)
    if m and m.group("length"):
        specs["thread"] = m.group("thread").upper()
        if m.group("pitch"):
            specs["pitch"] = float(m.group("pitch"))
        specs["length"] = float(m.group("length"))
        return specs
    m = _THREAD_SHORT_RE.search(stem)
    if m:
        specs["thread"] = m.group("thread").upper()
        specs["length"] = float(m.group("length"))
    return specs


def _iter_cad_files(root: Path):
    for p in sorted(root.rglob("*")):
        if p.is_file() and p.suffix.lower() in CAD_EXTS:
            yield p


def _supplier_for(path: Path, root: Path) -> str:
    """Infer supplier from the top-level subfolder under ``root``; else "library"."""
    try:
        rel = path.resolve().relative_to(root.resolve())
    except ValueError:
        return "library"
    parts = rel.parts
    if len(parts) >= 2:  # at least one subfolder before the filename
        return parts[0]
    return "library"


def _index_by_path() -> dict[str, dict]:
    """Map resolved cad_path -> record for fast already-registered lookup."""
    out: dict[str, dict] = {}
    for rec in vendor_parts._load_index().values():  # noqa: SLF001 (intentional: same package)
        cp = rec.get("cad_path")
        if cp:
            try:
                out[str(Path(cp).resolve())] = rec
            except OSError:
                out[str(cp)] = rec
    return out


def scan_library_folder(folder: str | None = None) -> dict:
    """Recursively scan ``folder`` (default: the watched parts-library dir) and register
    every new or changed CAD file into the vendor-part registry.

    Idempotent: files already registered with the same path + mtime are skipped. Returns
    ``{"scanned": N, "added": M, "updated": K, "parts": [records...]}``.
    """
    root = Path(folder).expanduser().resolve() if folder else paths.parts_library_dir()
    if not folder:
        root.mkdir(parents=True, exist_ok=True)
    if not root.exists():
        raise FileNotFoundError(f"parts library folder not found: {root}")

    by_path = _index_by_path()
    scanned = added = updated = 0
    touched: list[dict] = []

    for f in _iter_cad_files(root):
        scanned += 1
        resolved = str(f.resolve())
        mtime = f.stat().st_mtime
        existing = by_path.get(resolved)
        if existing is not None and float(existing.get("source_mtime") or 0.0) == mtime:
            continue  # unchanged -> skip (idempotent)

        supplier = _supplier_for(f, root)
        part_number = f.stem
        specs = parse_specs_from_name(f.stem)
        # Register WITHOUT copying: the library folder is itself the canonical store, so we
        # point cad_path at the file in place (keeps scans cheap + avoids duplicate bytes).
        rec = vendor_parts.register_local_cad(
            supplier=supplier,
            part_number=part_number,
            description=f.stem,
            cad_path=resolved,
            specs=specs,
            source="library",
            copy_file=False,
            source_mtime=mtime,
        )
        touched.append(rec)
        if existing is None:
            added += 1
        else:
            updated += 1

    return {"scanned": scanned, "added": added, "updated": updated, "parts": touched}


EmitFn = Callable[[dict], None]


def _vendor_part_scan_library(params: dict, _emit: EmitFn) -> dict:
    folder = params.get("folder")
    return scan_library_folder(str(folder) if folder else None)


WORKER_METHODS = {
    "vendor_part_scan_library": _vendor_part_scan_library,
}
