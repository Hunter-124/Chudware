"""services.plm.doctypes — the document-type taxonomy (2026-07 extension).

The frozen ``manifest.ITEM_KINDS`` enum (part/assembly/drawing/ecad/cam/
requirements/deliverable) stays untouched (contract §4). To track everything an
engineering team produces — specs, reports, datasheets, spreadsheets, images,
simulation studies, source code, and linked external repositories — every item
carries an OPTIONAL ``doc_type`` that refines the coarse ``kind`` without
widening it. An item with no ``doc_type`` is a legacy CAD item, read from its
``kind`` exactly as before (forward-tolerance, contract §4).

``doc_type`` is a *suggestion*, never a hard classification: :func:`detect_doc_type`
maps a path to its best guess and deliberately falls back to the generic
``document`` type rather than ever erroring, and the user can always retag via
``plm_item_set`` (contract §7). This mirrors the ease-of-use law — the system
never blocks on a taxonomy decision.

Storage form per doc_type (how :mod:`generic_snapshot` lands it in the shadow
tree — contract §2 sibling paths):

* ``container`` — the existing ``.step+`` / ``.ecad+`` explode path (CAD docs).
* ``file``      — a single working file stored verbatim under ``file/<name>``.
* ``tree``      — a folder walked (with an exclusion list) under ``tree/<rel>``.
* ``link``      — a reference to an external repo (``link.json``); no content.

No user-facing git vocabulary anywhere (contract §8.1).
"""
from __future__ import annotations

import os
from typing import Any

# doc_type → descriptor. ``kind`` is the frozen manifest kind it maps onto;
# ``store`` is the shadow-tree storage form; ``icon`` is a GUI glyph name
# (resolved by the C++ hub's doc_type icon map); ``fields`` are the extra
# metadata keys the Files page offers for that type (all optional, free-form).
DOC_TYPES: dict[str, dict[str, Any]] = {
    # --- CAD documents (container-backed; doc_type usually left implicit) ---
    "part":        {"label": "Part",        "kind": "part",         "store": "container", "icon": "part"},
    "assembly":    {"label": "Assembly",    "kind": "assembly",     "store": "container", "icon": "assembly"},
    "drawing":     {"label": "Drawing",     "kind": "drawing",      "store": "container", "icon": "drawing"},
    "ecad":        {"label": "Electronics", "kind": "ecad",         "store": "container", "icon": "ecad"},
    "cam":         {"label": "Manufacturing","kind": "cam",         "store": "container", "icon": "cam"},
    # --- engineering documents (single files, stored verbatim) -------------
    "spec":        {"label": "Specification", "kind": "requirements", "store": "file", "icon": "spec",
                    "fields": ["standard", "owner"]},
    "report":      {"label": "Report",      "kind": "deliverable",  "store": "file", "icon": "report",
                    "fields": ["author"]},
    "datasheet":   {"label": "Datasheet",   "kind": "deliverable",  "store": "file", "icon": "datasheet",
                    "fields": ["manufacturer", "part_number"]},
    "spreadsheet": {"label": "Spreadsheet", "kind": "deliverable",  "store": "file", "icon": "spreadsheet"},
    "dataset":     {"label": "Data",        "kind": "deliverable",  "store": "file", "icon": "dataset"},
    "image":       {"label": "Image",       "kind": "deliverable",  "store": "file", "icon": "image",
                    "previewable": True},
    "document":    {"label": "Document",    "kind": "deliverable",  "store": "file", "icon": "document"},
    # --- folders / trees ---------------------------------------------------
    "sim_study":   {"label": "Simulation",  "kind": "deliverable",  "store": "tree", "icon": "sim"},
    "software":    {"label": "Software",    "kind": "deliverable",  "store": "tree", "icon": "software"},
    # --- references --------------------------------------------------------
    "linked_repo": {"label": "Linked code", "kind": "deliverable",  "store": "link", "icon": "link"},
}

# doc_types whose content lives outside the container explode path (the
# "generic" family that :mod:`generic_snapshot` owns).
GENERIC_DOC_TYPES = frozenset(
    dt for dt, d in DOC_TYPES.items() if d["store"] != "container"
)
CONTAINER_DOC_TYPES = frozenset(
    dt for dt, d in DOC_TYPES.items() if d["store"] == "container"
)

# extension → doc_type (single files). Detection is best-effort; the user can
# always retag. Unknown extensions fall through to the generic ``document``.
_EXT_DOC_TYPE = {
    ".md": "spec", ".rst": "spec",
    ".pdf": "report", ".docx": "report", ".doc": "report", ".odt": "report",
    ".rtf": "report", ".html": "report", ".htm": "report",
    ".xlsx": "spreadsheet", ".xls": "spreadsheet", ".ods": "spreadsheet",
    ".csv": "dataset", ".tsv": "dataset", ".parquet": "dataset", ".dat": "dataset",
    ".png": "image", ".jpg": "image", ".jpeg": "image", ".gif": "image",
    ".svg": "image", ".webp": "image", ".bmp": "image", ".tif": "image", ".tiff": "image",
    ".txt": "document",
}

# filename stems that promote a .md/.txt to a specification.
_SPEC_HINTS = ("spec", "requirement", "req", "sow", "statement-of-work")

# markers that make a folder look like source code (→ software).
_CODE_MARKERS = frozenset({
    ".git", "src", "CMakeLists.txt", "Makefile", "package.json", "pyproject.toml",
    "Cargo.toml", "go.mod", "platformio.ini", "requirements.txt", "setup.py",
})
# markers that make a folder look like a simulation study.
_SIM_MARKERS = frozenset({
    "system", "constant", "0", "polyMesh",  # OpenFOAM
    "study.json", "results", "mesh",
})


def descriptor(doc_type: str | None) -> dict[str, Any]:
    """Return the descriptor for *doc_type* (the generic ``document`` fallback
    for an unknown or absent type — never raises)."""
    if not doc_type:
        return DOC_TYPES["document"]
    return DOC_TYPES.get(doc_type, DOC_TYPES["document"])


def kind_for(doc_type: str | None) -> str:
    """The frozen manifest ``kind`` a *doc_type* maps onto (contract §4)."""
    return descriptor(doc_type).get("kind", "deliverable")


def storage_form(doc_type: str | None) -> str:
    """``container`` | ``file`` | ``tree`` | ``link`` for *doc_type*."""
    return descriptor(doc_type).get("store", "file")


def detect_doc_type(path: str, head: bytes = b"") -> str:
    """Best-effort doc_type for a working *path* (file or folder).

    A suggestion only: falls back to the generic ``document`` (files) or a
    folder heuristic (``software`` / ``sim_study`` / ``document``) and never
    raises. The user retags via ``plm_item_set`` if the guess is wrong.
    """
    name = os.path.basename(str(path).rstrip("/\\"))
    lower = name.lower()
    # folders first (a path that exists as a directory, or has no extension)
    if os.path.isdir(path):
        return _detect_folder(path)
    ext = os.path.splitext(lower)[1]
    if ext in (".step+", ".ecad+") or lower.endswith(".step+") or lower.endswith(".ecad+"):
        # container — caller should use the schema-sniff path, not this
        return "part" if lower.endswith(".step+") else "ecad"
    dt = _EXT_DOC_TYPE.get(ext, "document")
    if dt in ("document", "spec") and ext in (".md", ".rst", ".txt"):
        stem = os.path.splitext(lower)[0]
        if any(h in stem for h in _SPEC_HINTS):
            return "spec"
        return "spec" if ext in (".md", ".rst") else "document"
    return dt


def _detect_folder(path: str) -> str:
    try:
        entries = set(os.listdir(path))
    except OSError:
        return "document"
    if entries & _CODE_MARKERS:
        return "software"
    if entries & _SIM_MARKERS:
        return "sim_study"
    return "document"
