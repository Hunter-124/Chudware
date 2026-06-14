"""2D export RPCs — drawing sheets and sheet-metal flat patterns to DXF / PDF (W14).

Two worker methods, both writing the 2D geometry the engine already produces to a manufacturing
file (DXF, priority) or a vector PDF:

``export_drawing {doc_id, path, format:"dxf"|"pdf"}``
    Flatten every sheet/view of an OPEN drawing (``drawing_get`` geometry) to layers VISIBLE
    (solid) + HIDDEN (dashed) and write the file. ``doc_id`` is a DRAWING registry id (from
    ``drawing_new``), not a part doc id.

``export_flat_dxf {doc_id, path, format?:"dxf"|"pdf"}``
    The active sheet-metal part's FLAT PATTERN: the blank outline on layer OUTLINE and the bend
    lines on a SEPARATE layer BEND (the key sheet-metal requirement). ``doc_id`` is a part doc id.
    Requires a sheet-metal part with a flat pattern; a clean error otherwise.

Both contain their errors (bad path/format/no geometry) via ``OpError`` so the worker emits the
structured envelope. Format is taken from the explicit ``format`` param, else the path extension.

The flat-pattern geometry comes from the ``CompileResult.sheet_metal_flat`` side-channel the
``sm_flat_pattern`` compiler populates (``engine/doc/features/sheetmetal_flat.py``). Because a
cache-hit does NOT repopulate the side-channel (same caveat as the weldment cut list), we compile
against a FRESH cache here, exactly like ``engine.doc.cutlist.cut_list_for_session``.
"""

from __future__ import annotations

import math
import os
from pathlib import Path
from typing import Any, Callable

from engine.doc.ops import OpError

EmitFn = Callable[[dict], None]

_EXT_TO_FORMAT = {".dxf": "dxf", ".pdf": "pdf"}


def _detect_2d_format(path: str | os.PathLike) -> str | None:
    return _EXT_TO_FORMAT.get(Path(path).suffix.lower())


def _resolve_format(params: dict, path: str) -> str:
    """Resolve the 2D output format from an explicit param or the path extension; validate it."""
    fmt = (params.get("format") or _detect_2d_format(path) or "").lower()
    if fmt not in ("dxf", "pdf"):
        raise OpError("invalid_op",
                      f"format must be 'dxf' or 'pdf' (got {fmt or 'none'}; "
                      f"path '{path}' has no .dxf/.pdf extension)",
                      {"reason": "bad format"})
    return fmt


def _validate_path(params: dict) -> str:
    path = params.get("path")
    if not path or not isinstance(path, str):
        raise OpError("invalid_op", "'path' is required", {"reason": "missing path"})
    return path


# --------------------------------------------------------------------------- drawing export


def _drawing_layers(drawing_get_result: dict) -> tuple[dict, int]:
    """Flatten all sheets/views' visible+hidden polylines into {VISIBLE:[…], HIDDEN:[…]}.

    The view geometry is already in absolute sheet-mm (the engine scaled + positioned each polyline),
    so we just pool them per layer. Returns (layers, entity_count).
    """
    visible: list = []
    hidden: list = []
    for sheet in drawing_get_result.get("sheets", []) or []:
        for view in sheet.get("views", []) or []:
            for pl in view.get("visible", []) or []:
                if isinstance(pl, list) and len(pl) >= 2:
                    visible.append(pl)
            for pl in view.get("hidden", []) or []:
                if isinstance(pl, list) and len(pl) >= 2:
                    hidden.append(pl)
    layers = {"VISIBLE": visible, "HIDDEN": hidden}
    return layers, len(visible) + len(hidden)


def export_drawing(doc_id: str, path: str, fmt: str) -> dict:
    """Write an open drawing's 2D geometry to DXF/PDF. ``doc_id`` is a drawing registry id."""
    from engine.doc import drawing as drawingmod

    drawingmod.get_drawing(doc_id)  # validates the drawing is open (raises OpError otherwise)
    result = drawingmod.get_drawing_full(doc_id)

    layers, n = _drawing_layers(result)
    if n == 0:
        raise OpError("export_failed",
                      "export_drawing: the drawing has no view geometry to export "
                      "(add views first, then Rebuild)",
                      {"doc_id": doc_id})

    try:
        if fmt == "dxf":
            from engine.doc.dxf import write_dxf
            out = write_dxf(path, layers)
        else:
            from engine.doc.pdfvec import write_pdf
            lines = [(pl, False) for pl in layers["VISIBLE"]] + \
                    [(pl, True) for pl in layers["HIDDEN"]]
            out = write_pdf(path, lines)
    except OpError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise OpError("export_failed", f"export_drawing: {exc}",
                      {"doc_id": doc_id, "path": path, "format": fmt}) from exc

    return {"path": out, "format": fmt, "visible": len(layers["VISIBLE"]),
            "hidden": len(layers["HIDDEN"]), "entities": n}


# --------------------------------------------------------------------------- flat-pattern export


def _basis_from_normal(n: list[float]) -> tuple[tuple, tuple, tuple]:
    """An orthonormal (u, v, n) frame for projecting base-plane 3D points to 2D.

    ``n`` is the flat sheet's broad-face normal. ``u`` is any unit vector ⟂ n, ``v = n × u``. The
    flat geometry lies in the base plane (⟂ n), so (point·u, point·v) is its 2D coordinate.
    """
    nx, ny, nz = float(n[0]), float(n[1]), float(n[2])
    ln = math.sqrt(nx * nx + ny * ny + nz * nz) or 1.0
    nx, ny, nz = nx / ln, ny / ln, nz / ln
    # Pick the world axis least aligned with n as the seed for u.
    seed = (1.0, 0.0, 0.0) if abs(nx) < 0.9 else (0.0, 1.0, 0.0)
    ux = seed[1] * nz - seed[2] * ny
    uy = seed[2] * nx - seed[0] * nz
    uz = seed[0] * ny - seed[1] * nx
    lu = math.sqrt(ux * ux + uy * uy + uz * uz) or 1.0
    ux, uy, uz = ux / lu, uy / lu, uz / lu
    vx = ny * uz - nz * uy
    vy = nz * ux - nx * uz
    vz = nx * uy - ny * ux
    return (ux, uy, uz), (vx, vy, vz), (nx, ny, nz)


def _to_2d(pt, u, v) -> list[float]:
    x, y, z = float(pt[0]), float(pt[1]), float(pt[2])
    return [x * u[0] + y * u[1] + z * u[2], x * v[0] + y * v[1] + z * v[2]]


def flat_pattern_layers(flat_record: dict) -> tuple[dict, int]:
    """Project one ``_sheet_metal_flat`` record to {OUTLINE:[poly], BEND:[seg, …]} in 2D mm.

    The outline (a 3-D boundary polyline in the base plane) becomes a single closed OUTLINE polyline;
    each bend line's start/end 3-D segments go on the BEND layer. The 2-D frame is built from the
    record's stored ``n_base`` so the projection is the natural in-plane (u, v) coordinate.
    """
    n_base = flat_record.get("n_base") or [0.0, 0.0, 1.0]
    u, v, _n = _basis_from_normal(n_base)

    outline3d = flat_record.get("outline", []) or []
    outline2d = [_to_2d(p, u, v) for p in outline3d if isinstance(p, list) and len(p) >= 3]
    # Close the outline polyline so the blank boundary reads as a closed loop.
    if len(outline2d) >= 2 and outline2d[0] != outline2d[-1]:
        outline2d = outline2d + [outline2d[0]]

    bend_segs: list = []
    for bl in flat_record.get("bend_lines", []) or []:
        for key in ("line_start", "line_end"):
            seg = bl.get(key)
            if isinstance(seg, list) and len(seg) == 2 and \
               all(isinstance(pt, list) and len(pt) >= 3 for pt in seg):
                bend_segs.append([_to_2d(seg[0], u, v), _to_2d(seg[1], u, v)])

    layers: dict = {"OUTLINE": [outline2d] if len(outline2d) >= 2 else [], "BEND": bend_segs}
    n = len(layers["OUTLINE"]) + len(bend_segs)
    return layers, n


def flat_pattern_for_session(session) -> dict:
    """Compute (fresh-cache) the active part's flat-pattern record, or raise a clean OpError.

    Mirrors ``cutlist.cut_list_for_session``: a fresh compile guarantees ``sm_flat_pattern`` actually
    runs and repopulates the ``_sheet_metal_flat`` side-channel. Returns the LAST flat record (the
    most recent flat pattern in the feature tree). Raises ``OpError`` when the part has no flat
    pattern (the user must add a Flatten feature first).
    """
    from engine.doc.compiler import compile_document, CompileCache

    if getattr(session, "is_assembly", False):
        raise OpError("invalid_op",
                      "export_flat_dxf: an assembly has no flat pattern — open a sheet-metal part",
                      {"reason": "assembly"})

    result = compile_document(session.doc, CompileCache())
    flat = result.sheet_metal_flat or {}
    if not flat:
        raise OpError("invalid_op",
                      "export_flat_dxf: the part has no flat pattern — add a Flatten "
                      "(sm_flat_pattern) feature to a sheet-metal part first",
                      {"reason": "no flat pattern"})
    # Use the last flat record (most recent Flatten). dict preserves insertion order.
    fid = list(flat.keys())[-1]
    return flat[fid]


def export_flat_dxf(doc_id: str, path: str, fmt: str) -> dict:
    """Write the active part's flat-pattern outline + bend lines to DXF/PDF on separate layers."""
    from engine.doc import session as docsess

    session = docsess.get_session(doc_id)  # raises unknown_doc if absent
    flat_record = flat_pattern_for_session(session)

    layers, n = flat_pattern_layers(flat_record)
    if n == 0 or not layers["OUTLINE"]:
        raise OpError("export_failed",
                      "export_flat_dxf: the flat pattern produced no outline geometry",
                      {"doc_id": doc_id})

    try:
        if fmt == "dxf":
            from engine.doc.dxf import write_dxf
            out = write_dxf(path, layers)
        else:
            from engine.doc.pdfvec import write_pdf
            # Outline solid; bend lines dashed (so a PDF still distinguishes them — the DXF keeps
            # them on a named layer, the canonical separation).
            lines = [(pl, False) for pl in layers["OUTLINE"]] + \
                    [(seg, True) for seg in layers["BEND"]]
            out = write_pdf(path, lines)
    except OpError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise OpError("export_failed", f"export_flat_dxf: {exc}",
                      {"doc_id": doc_id, "path": path, "format": fmt}) from exc

    return {"path": out, "format": fmt, "bend_lines": len(layers["BEND"]),
            "outline_points": len(layers["OUTLINE"][0]) if layers["OUTLINE"] else 0,
            "flat_area": flat_record.get("flat_area"), "entities": n}


# --------------------------------------------------------------------------- worker RPC handlers


def _handler_export_drawing(params: dict, _emit: EmitFn) -> dict:
    doc_id = params.get("doc_id")
    if not doc_id:
        raise OpError("invalid_op", "export_drawing: 'doc_id' is required",
                      {"reason": "missing doc_id"})
    path = _validate_path(params)
    fmt = _resolve_format(params, path)
    return export_drawing(doc_id, path, fmt)


def _handler_export_flat_dxf(params: dict, _emit: EmitFn) -> dict:
    doc_id = params.get("doc_id")
    if not doc_id:
        raise OpError("invalid_op", "export_flat_dxf: 'doc_id' is required",
                      {"reason": "missing doc_id"})
    path = _validate_path(params)
    fmt = _resolve_format(params, path)
    return export_flat_dxf(doc_id, path, fmt)


WORKER_METHODS: dict[str, Callable] = {
    "export_drawing": _handler_export_drawing,
    "export_flat_dxf": _handler_export_flat_dxf,
}
