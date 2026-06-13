"""Minimal DXF R12 ASCII writer — the manufacturing 2D format (W14).

DXF R12 is the lowest-common-denominator interchange format every CAD/CAM tool reads. This module
writes the *small* subset we need for 2D line art: a HEADER, a TABLES section declaring named LAYERS
(each with a color + line type), and an ENTITIES section of polylines. No blocks, no splines — every
curve the engine hands us is already discretized to a polyline of ``[x, y]`` points (drawing HLR
output, sheet-metal flat outlines/bend lines), so a polyline is all we emit.

Public API
----------
``write_dxf(path, layers, *, polyline_as="polyline")``
    *layers* is ``{layer_name: [polyline, …]}`` where a polyline is ``list[[x, y]]`` (mm). Each
    layer gets a TABLES entry with a sensible color + linetype (see ``_LAYER_STYLE``); each polyline
    is written as a native R12 ``POLYLINE``/``VERTEX``/``SEQEND`` entity on that layer.
    ``polyline_as="line"`` instead emits one ``LINE`` per segment (equally valid, maximally portable).

Validity
--------
The output is hand-written group-code/value pairs (the DXF "tag" format: an integer code line then a
value line). It opens in LibreCAD / AutoCAD / Fusion / ezdxf / any DXF reader. We declare the LTYPE
table (CONTINUOUS + DASHED) and the LAYER table up front so layer colors/linetypes resolve; entities
then reference their layer by name.

R12 note: we emit the CANONICAL R12 ``POLYLINE`` (with the 66=1 "vertices follow" flag, one VERTEX
per point, then SEQEND) rather than ``LWPOLYLINE`` — LWPOLYLINE is an R13+ entity that strict R12
readers (e.g. ezdxf) reject in an AC1009 file. ``polyline_as="line"`` is the bulletproof fallback
(LINE is valid in every DXF version ever shipped, with no subclass markers).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable

# Layer styling: name -> (aci_color, linetype). ACI (AutoCAD Color Index): 7=white/black,
# 1=red, 3=green, 5=blue. Hidden lines use the DASHED linetype; everything else CONTINUOUS.
_LAYER_STYLE: dict[str, tuple[int, str]] = {
    "VISIBLE": (7, "CONTINUOUS"),   # solid object lines — white/black (7 renders black on white)
    "HIDDEN": (1, "DASHED"),        # hidden lines — red + dashed
    "OUTLINE": (7, "CONTINUOUS"),   # flat-pattern blank boundary — white/black
    "BEND": (3, "CONTINUOUS"),      # sheet-metal bend lines — green (the key SM layer)
}
_DEFAULT_STYLE = (7, "CONTINUOUS")


def _tag(out: list[str], code: int, value) -> None:
    """Append one DXF group-code / value pair (two physical lines)."""
    out.append(str(code))
    if isinstance(value, float):
        # Trim to a clean decimal; DXF readers accept plain decimal floats.
        out.append(f"{value:.6f}".rstrip("0").rstrip(".") if value == value else "0")
    else:
        out.append(str(value))


def _header(out: list[str]) -> None:
    _tag(out, 0, "SECTION")
    _tag(out, 2, "HEADER")
    _tag(out, 9, "$ACADVER")
    _tag(out, 1, "AC1009")  # R12
    _tag(out, 0, "ENDSEC")


def _tables(out: list[str], layer_names: Iterable[str]) -> None:
    names = list(layer_names)
    _tag(out, 0, "SECTION")
    _tag(out, 2, "TABLES")

    # --- LTYPE table: CONTINUOUS + DASHED so hidden/dashed layers resolve. ---
    _tag(out, 0, "TABLE")
    _tag(out, 2, "LTYPE")
    _tag(out, 70, 2)
    # CONTINUOUS (no dashes)
    _tag(out, 0, "LTYPE")
    _tag(out, 2, "CONTINUOUS")
    _tag(out, 70, 0)
    _tag(out, 3, "Solid line")
    _tag(out, 72, 65)
    _tag(out, 73, 0)
    _tag(out, 40, 0.0)
    # DASHED: dash 5, gap 5 (pattern total length 10).
    _tag(out, 0, "LTYPE")
    _tag(out, 2, "DASHED")
    _tag(out, 70, 0)
    _tag(out, 3, "Dashed __ __ __")
    _tag(out, 72, 65)
    _tag(out, 73, 2)
    _tag(out, 40, 10.0)
    _tag(out, 49, 5.0)   # dash
    _tag(out, 49, -5.0)  # gap (negative = pen up)
    _tag(out, 0, "ENDTAB")

    # --- LAYER table: one entry per layer we actually write. ---
    _tag(out, 0, "TABLE")
    _tag(out, 2, "LAYER")
    _tag(out, 70, len(names))
    for name in names:
        color, ltype = _LAYER_STYLE.get(name, _DEFAULT_STYLE)
        _tag(out, 0, "LAYER")
        _tag(out, 2, name)
        _tag(out, 70, 0)
        _tag(out, 62, color)
        _tag(out, 6, ltype)
    _tag(out, 0, "ENDTAB")

    _tag(out, 0, "ENDSEC")


def _polyline(out: list[str], layer: str, pts: list) -> None:
    """A native R12 POLYLINE: a POLYLINE header, one VERTEX per point, then SEQEND.

    This is the canonical R12 (AC1009) polyline — it loads in every DXF reader, including strict
    ones (ezdxf), unlike LWPOLYLINE which is an R13+ entity. Each edge stays ONE logical entity
    (vs. exploding to per-segment LINEs), which is what downstream CAM expects.
    """
    _tag(out, 0, "POLYLINE")
    _tag(out, 8, layer)
    _tag(out, 66, 1)          # "vertices follow" flag (required for R12 POLYLINE)
    _tag(out, 70, 0)          # polyline flags: 0 = open 2D polyline
    # A dummy elevation point (R12 POLYLINE carries a 10/20/30 default position).
    _tag(out, 10, 0.0)
    _tag(out, 20, 0.0)
    _tag(out, 30, 0.0)
    for p in pts:
        _tag(out, 0, "VERTEX")
        _tag(out, 8, layer)
        _tag(out, 10, float(p[0]))
        _tag(out, 20, float(p[1]))
        _tag(out, 30, 0.0)
    _tag(out, 0, "SEQEND")
    _tag(out, 8, layer)


def _line(out: list[str], layer: str, a, b) -> None:
    _tag(out, 0, "LINE")
    _tag(out, 8, layer)
    _tag(out, 10, float(a[0]))
    _tag(out, 20, float(a[1]))
    _tag(out, 11, float(b[0]))
    _tag(out, 21, float(b[1]))


def _entities(out: list[str], layers: dict, polyline_as: str) -> int:
    """Write the ENTITIES section. Returns the number of polyline/line entities emitted."""
    _tag(out, 0, "SECTION")
    _tag(out, 2, "ENTITIES")
    n = 0
    for layer, polylines in layers.items():
        for pl in polylines or []:
            pts = [p for p in pl if p is not None and len(p) >= 2]
            if len(pts) < 2:
                continue
            if polyline_as == "line":
                for i in range(1, len(pts)):
                    _line(out, layer, pts[i - 1], pts[i])
                    n += 1
            else:
                _polyline(out, layer, pts)
                n += 1
    _tag(out, 0, "ENDSEC")
    return n


def write_dxf(path: str | os.PathLike, layers: dict, *,
              polyline_as: str = "polyline") -> str:
    """Write *layers* to a DXF R12 file at *path*.

    Parameters
    ----------
    path:
        Destination ``.dxf`` file (parent dirs created).
    layers:
        ``{layer_name: [polyline, …]}`` where a polyline is ``list[[x, y]]`` in millimetres. Empty
        layers are still declared in the TABLES section (so the file carries the expected layer set)
        but contribute no entities. A polyline with <2 points is skipped.
    polyline_as:
        ``"polyline"`` (default — native R12 POLYLINE, one entity per edge) or ``"line"`` (one LINE
        per segment — maximally portable).

    Returns
    -------
    str
        The resolved path written.

    Raises
    ------
    ValueError
        When *layers* contains no drawable geometry at all (every layer empty) — the caller should
        surface a clean "no geometry" error rather than write an empty DXF.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)

    out: list[str] = []
    _header(out)
    _tables(out, layers.keys())
    n = _entities(out, layers, polyline_as)
    _tag(out, 0, "EOF")

    if n == 0:
        raise ValueError("write_dxf: no polyline geometry to export (all layers empty)")

    # DXF is CRLF by convention; readers accept LF too. Write BYTES (not text mode) so the platform
    # newline translation can't turn our explicit "\r\n" into "\r\r\n" on Windows.
    data = ("\r\n".join(out) + "\r\n").encode("ascii")
    p.write_bytes(data)
    return str(p)
