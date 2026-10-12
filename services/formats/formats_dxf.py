"""DXF (AutoCAD Drawing Exchange Format) import via **ezdxf** (MIT).

DXF is a 2D-centric vector-drawing exchange format (laser/waterjet/plasma profiles, sketch
outlines, 2D details). This importer turns modelspace geometry into build123d planar B-rep so a
DXF profile drops straight into the OCCT lane and can be extruded/referenced like any imported
solid's face.

Pipeline
--------
1. Walk modelspace entities (recursing into ``INSERT`` block references, which are exploded to
   their primitives in world coordinates).
2. Convert each supported entity to one or more build123d ``Edge``:
     * ``LINE``                      -> line edge
     * ``CIRCLE``                    -> full-circle edge
     * ``ARC``                       -> circular-arc edge
     * ``LWPOLYLINE`` / ``POLYLINE`` -> per-segment line/arc edges (bulge -> true arc)
     * ``ELLIPSE`` / ``SPLINE``      -> spline edge through flattened points (approximated;
                                        chord deviation ``_FLATTEN_DEV`` drawing units)
   Text, dimensions, hatches, points and 3D mesh entities are skipped (counted, reported).
3. Connect the loose edges into wires (OCCT ``ShapeAnalysis_FreeBounds``); every *closed* planar
   wire becomes a ``Face``, open wires are kept as reference wires.
4. Return a single ``(stem, Compound)`` body containing all faces + free wires (one file = one
   body, mirroring the mesh importers).

Scope: this Python module **imports only**; DXF writing is the native engine's ``export_file``
(format ``dxf``). Curves are flattened, not preserved as exact NURBS. ``$INSUNITS`` scales geometry
into millimetres; unset/unitless drawings retain their coordinates. rhino3dm/OCCT NURBS fidelity
is out of scope here.

License: ezdxf is MIT — in-process use is inside the CONTRIBUTING.md guardrails (no debt row).
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

# Chord-deviation (in DXF drawing units) used to flatten ELLIPSE / SPLINE into a spline edge.
_FLATTEN_DEV = 0.05
# Tolerance for stitching loose edges into connected wires.
_CONNECT_TOL = 1e-6
# Below this a polyline segment / curve span is treated as degenerate and dropped.
_EPS = 1e-9

# AutoCAD $INSUNITS codes: millimetres per drawing unit (0 = unitless, unchanged).
_MM_PER_INSUNIT = {
    0: 1.0, 1: 25.4, 2: 304.8, 3: 1609344.0, 4: 1.0,
    5: 10.0, 6: 1000.0, 7: 1000000.0, 8: 0.0000254,
    9: 0.0254, 10: 914.4, 11: 1e-7, 12: 1e-6, 13: 0.001,
    14: 100.0, 15: 10000.0, 16: 100000.0, 17: 1e12,
    18: 149597870700000.0, 19: 9.4607304725808e18,
    20: 3.085677581491367e19, 21: 1200.0 / 3937.0 * 1000.0,
    22: 100.0 / 3937.0 * 1000.0, 23: 3600.0 / 3937.0 * 1000.0,
    24: 6336000.0 / 3937.0 * 1000.0,
}


def import_dxf(path: str | Path) -> list[tuple[str, Any]]:
    """Import a DXF file → ``[(name, shape)]`` (a single Compound body of faces + free wires).

    Raises ``ValueError`` when the file cannot be read or carries no importable 2D geometry.
    """
    import ezdxf

    try:
        doc = ezdxf.readfile(str(path))
    except IOError as exc:
        raise ValueError(f"could not read DXF '{path}': {exc}") from exc
    except ezdxf.DXFStructureError as exc:
        raise ValueError(f"'{path}' is not a valid DXF file: {exc}") from exc

    return bodies_from_doc(doc, Path(path).stem or "dxf", source=str(path))


def bodies_from_doc(doc, stem: str, source: str = "") -> list[tuple[str, Any]]:
    """Convert an ezdxf ``Drawing`` → ``[(name, shape)]`` (faces + free wires as one Compound body).

    Shared by the DXF importer and the DWG importer (which obtains the same ezdxf ``Drawing`` from an
    external DWG→DXF converter). ``source`` is only used to make the "no geometry" error legible.
    """
    from build123d import Compound

    unit = int(doc.header.get("$INSUNITS", 0) or 0)
    if unit not in _MM_PER_INSUNIT:
        raise ValueError(f"'{source or stem}' has unsupported $INSUNITS {unit}")
    edges, skipped = _entities_to_edges(doc.modelspace())
    if not edges:
        raise ValueError(
            f"'{source or stem}' has no importable 2D geometry "
            f"({skipped} unsupported/empty entity(ies) skipped — text, dimensions, hatches and "
            "3D mesh entities are not imported).")
    faces, wires = _assemble_wires(edges)
    shapes: list[Any] = [*faces, *wires]
    body = shapes[0] if len(shapes) == 1 else Compound(children=shapes)
    scale = _MM_PER_INSUNIT[unit]
    if scale != 1.0:
        body = body.scale(scale)
    return [(stem, body)]


# --------------------------------------------------------------------------- entity → edges


def _entities_to_edges(entities) -> tuple[list, int]:
    """Convert an iterable of ezdxf entities to a flat list of build123d Edges + a skipped count.

    Recurses through ``INSERT`` block references via ``virtual_entities()`` (world-placed)."""
    from build123d import Edge, Plane, Vector

    edges: list = []
    skipped = 0
    for e in entities:
        etype = e.dxftype()
        try:
            if etype == "LINE":
                s, en = e.dxf.start, e.dxf.end
                edges.append(Edge.make_line(Vector(s.x, s.y, s.z), Vector(en.x, en.y, en.z)))
            elif etype == "CIRCLE":
                c = e.dxf.center
                edges.append(Edge.make_circle(e.dxf.radius, Plane(origin=(c.x, c.y, c.z))))
            elif etype == "ARC":
                c = e.dxf.center
                edges.append(Edge.make_circle(
                    e.dxf.radius, Plane(origin=(c.x, c.y, c.z)),
                    start_angle=e.dxf.start_angle, end_angle=e.dxf.end_angle))
            elif etype in ("LWPOLYLINE", "POLYLINE"):
                edges.extend(_polyline_edges(e))
            elif etype in ("ELLIPSE", "SPLINE"):
                se = _flatten_to_spline(e)
                if se is not None:
                    edges.append(se)
                else:
                    skipped += 1
            elif etype == "INSERT":
                sub, sub_skipped = _entities_to_edges(e.virtual_entities())
                edges.extend(sub)
                skipped += sub_skipped
            else:
                skipped += 1
        except Exception:  # noqa: BLE001 — one bad entity must not abort the whole import
            skipped += 1
    return edges, skipped


def _polyline_edges(e) -> list:
    """LWPOLYLINE / POLYLINE → per-segment line/arc edges (DXF *bulge* → true circular arc)."""
    from build123d import Edge, Vector

    etype = e.dxftype()
    pts: list[tuple[float, float, float, float]] = []  # (x, y, z, bulge)
    if etype == "LWPOLYLINE":
        elev = float(e.dxf.get("elevation", 0.0) or 0.0)
        for x, y, bulge in e.get_points("xyb"):
            pts.append((float(x), float(y), elev, float(bulge)))
        closed = bool(e.closed)
    else:  # POLYLINE (2D or 3D)
        for v in e.vertices:
            loc = v.dxf.location
            bulge = float(getattr(v.dxf, "bulge", 0.0) or 0.0)
            pts.append((loc.x, loc.y, loc.z, bulge))
        closed = bool(e.is_closed)

    out: list = []
    n = len(pts)
    if n < 2:
        return out
    for i in (range(n) if closed else range(n - 1)):
        x1, y1, z1, bulge = pts[i]
        x2, y2, z2, _ = pts[(i + 1) % n]
        if math.hypot(x2 - x1, y2 - y1) < _EPS and abs(z2 - z1) < _EPS:
            continue  # coincident vertices — skip degenerate segment
        if abs(bulge) < 1e-12:
            out.append(Edge.make_line(Vector(x1, y1, z1), Vector(x2, y2, z2)))
        else:
            arc = _bulge_arc(x1, y1, x2, y2, bulge, z1)
            if arc is not None:
                out.append(arc)
            else:
                out.append(Edge.make_line(Vector(x1, y1, z1), Vector(x2, y2, z2)))
    return out


def _bulge_arc(x1: float, y1: float, x2: float, y2: float, bulge: float, z: float):
    """A DXF polyline *bulge* segment → circular-arc edge.

    ``bulge = tan(theta/4)`` for included angle *theta* (positive = CCW). The arc apex is offset
    from the chord midpoint along the left normal by the signed sagitta ``bulge * chord/2``; a
    three-point arc through (p1, apex, p2) reproduces it exactly."""
    from build123d import Edge, Vector

    dx, dy = x2 - x1, y2 - y1
    chord = math.hypot(dx, dy)
    if chord < _EPS:
        return None
    sagitta = bulge * chord / 2.0
    ux, uy = dx / chord, dy / chord
    nx, ny = -uy, ux  # left normal
    mx, my = (x1 + x2) / 2.0, (y1 + y2) / 2.0
    ax, ay = mx + nx * sagitta, my + ny * sagitta
    try:
        return Edge.make_three_point_arc(
            Vector(x1, y1, z), Vector(ax, ay, z), Vector(x2, y2, z))
    except Exception:  # noqa: BLE001
        return None


def _flatten_to_spline(e):
    """ELLIPSE / SPLINE → a spline edge through ezdxf-flattened points (chord dev _FLATTEN_DEV)."""
    from build123d import Edge, Vector

    raw = list(e.flattening(_FLATTEN_DEV))
    if not raw:
        return None
    clean: list = [Vector(raw[0].x, raw[0].y, raw[0].z)]
    for p in raw[1:]:
        v = Vector(p.x, p.y, p.z)
        if (v - clean[-1]).length > _EPS:
            clean.append(v)
    if len(clean) < 2:
        return None
    return Edge.make_spline(clean)


# --------------------------------------------------------------------------- edges → wires/faces


def _assemble_wires(edges) -> tuple[list, list]:
    """Stitch loose edges into connected wires; closed planar wires → Faces, rest → free wires."""
    from build123d import Face, Wire
    from OCP.ShapeAnalysis import ShapeAnalysis_FreeBounds
    from OCP.TopoDS import TopoDS
    from OCP.TopTools import TopTools_HSequenceOfShape

    seq = TopTools_HSequenceOfShape()
    for e in edges:
        seq.Append(e.wrapped)
    connected = TopTools_HSequenceOfShape()
    ShapeAnalysis_FreeBounds.ConnectEdgesToWires_s(seq, _CONNECT_TOL, False, connected)

    faces: list = []
    wires: list = []
    for i in range(1, connected.Length() + 1):
        topo_wire = TopoDS.Wire_s(connected.Value(i))
        wire = Wire(topo_wire)
        try:
            is_closed = wire.is_closed
        except Exception:  # noqa: BLE001
            is_closed = bool(topo_wire.Closed())
        if is_closed:
            try:
                faces.append(Face(wire))
                continue
            except Exception:  # noqa: BLE001 — non-planar / self-intersecting closed wire
                pass
        wires.append(wire)
    return faces, wires
