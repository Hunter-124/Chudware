"""ECAD → 3D bridge: turn a ``BoardDocument`` into a STEP solid + a CAD ``aicad.part/1`` doc.

This is *the* headline integration (docs/contracts/ecad/fab_outputs.md §5): a finished PCB drops
straight into the existing AI-CAD 3D workspace so the user can model an enclosure around it.

Public surface
--------------
``board_to_step(board, out_path, opts=None) -> {"step_path", "summary"}``
    Build the board substrate (Edge.Cuts outline extruded by ``stackup.thickness``, minus drilled
    holes), optional thin copper, and a component for every ``on_board`` footprint — its real
    ``model_3d`` STEP when one loads, else a courtyard-sized box *proxy* so the enclosure designer
    always has a collision volume. Everything is assembled into a build123d ``Compound`` and
    exported to ``out_path`` as a STEP. Units mm.

``board_to_part_doc(board, step_path) -> dict``
    A minimal ``aicad.part/1`` document whose single ``import`` feature references ``step_path`` —
    matching the envelope the engine.doc compiler expects (see engine/doc/features/import_feature.py
    and engine/doc/schema.py), so ``ecad_to_3d`` can open it directly in the CAD viewport.

The kernel here is build123d / OCP — the same one engine.kernel + engine.doc use — so the produced
geometry round-trips through ``engine.doc.formats.import_path`` and the part-doc compile pipeline.
"""

from __future__ import annotations

import math
import os
import uuid
from dataclasses import dataclass, field
from typing import Any

from engine.ecad.schema import pad_world

# build123d / OCP are imported lazily inside functions so importing this module stays cheap and
# OCP-free for callers that only want ``board_to_part_doc`` (no geometry kernel needed).

# Endpoint-chaining tolerance for stitching outline segments into a closed wire (mm).
_OUTLINE_TOL = 1e-3
# Tessellation count for arc/circle outline edges when we fall back to a polyline.
_ARC_SEGS = 64

# Default per-attr proxy heights (mm) when a footprint has no model_3d and no explicit height.
_PROXY_HEIGHT = {"smd": 1.0, "tht": 5.0, "np_tht": 5.0, "virtual": 1.0}
_PROXY_HEIGHT_DEFAULT = 1.5
# Minimum proxy footprint span so a degenerate (single-tiny-pad) part still gets a visible volume.
_PROXY_MIN_SPAN = 0.5
# Pad extent padding (mm) added around the pad bounding box when no courtyard is present.
_PROXY_PAD_MARGIN = 0.5


# --------------------------------------------------------------------------- options


@dataclass
class BoardToStepOpts:
    """Knobs for :func:`board_to_step` (fab_outputs.md §5)."""

    components: bool = True          # place component models / proxies
    copper: bool = False             # thin copper extrusions (heavy; off by default)
    z_origin: str = "bottom"         # "bottom" -> board sits 0..t; "center" -> -t/2..t/2
    mask_color: tuple = (0.0, 0.45, 0.20)   # solder-mask green-ish
    copper_color: tuple = (0.85, 0.65, 0.20)  # gold/HASL
    copper_thickness: float = 0.035  # mm

    @classmethod
    def coerce(cls, opts: Any) -> "BoardToStepOpts":
        if opts is None:
            return cls()
        if isinstance(opts, cls):
            return opts
        if isinstance(opts, dict):
            known = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
            return cls(**{k: v for k, v in opts.items() if k in known})
        raise TypeError(f"opts must be None, dict, or BoardToStepOpts, got {type(opts)!r}")


@dataclass
class _Summary:
    outline_area: float = 0.0
    thickness: float = 0.0
    board_volume: float = 0.0
    n_holes: int = 0
    n_components: int = 0
    n_proxies: int = 0
    n_models: int = 0
    n_skipped: int = 0
    warnings: list = field(default_factory=list)

    def asdict(self) -> dict:
        return {
            "outline_area": self.outline_area,
            "thickness": self.thickness,
            "board_volume": self.board_volume,
            "n_holes": self.n_holes,
            "n_components": self.n_components,
            "n_proxies": self.n_proxies,
            "n_models": self.n_models,
            "n_skipped": self.n_skipped,
            "warnings": list(self.warnings),
        }


class BoardGeometryError(ValueError):
    """Raised for a board whose geometry cannot be turned into a solid (open/empty outline)."""


# --------------------------------------------------------------------------- outline -> wire


def _arc_points(center, radius, a0_deg, a1_deg, segs=_ARC_SEGS):
    """Polyline points along an arc from ``a0`` to ``a1`` (deg, CCW), inclusive of both ends."""
    cx, cy = float(center[0]), float(center[1])
    a0 = math.radians(float(a0_deg))
    a1 = math.radians(float(a1_deg))
    # Sweep CCW; if a1 <= a0 treat as the short forward sweep.
    if a1 <= a0:
        a1 += 2 * math.pi
    n = max(2, int(segs * (a1 - a0) / (2 * math.pi)) + 1)
    return [(cx + radius * math.cos(a0 + (a1 - a0) * i / n),
             cy + radius * math.sin(a0 + (a1 - a0) * i / n)) for i in range(n + 1)]


def _outline_segments(outline: dict) -> tuple[list, list]:
    """Return ``(circle_shapes, segment_chains)``.

    ``circle_shapes`` are full ``circle`` outline shapes (each is its own closed loop / a face on
    its own). ``segment_chains`` is a flat list of ``(p1, p2)`` straight chords obtained by
    flattening every ``line``/``arc`` shape into endpoints — these are stitched into closed loops
    downstream. Flattening arcs to chords keeps the wire build robust (no tangency/orientation
    bookkeeping) at the cost of a faceted edge, which is fine for a board substrate.
    """
    shapes = outline.get("shapes", []) or []
    circles: list = []
    chords: list = []
    for sh in shapes:
        stype = sh.get("type", "line")
        if stype == "line":
            chords.append((tuple(sh["p1"]), tuple(sh["p2"])))
        elif stype == "arc":
            pts = _arc_points(sh["center"], float(sh["radius"]),
                              float(sh.get("a0", 0.0)), float(sh.get("a1", 360.0)))
            for a, b in zip(pts, pts[1:]):
                chords.append((a, b))
        elif stype == "circle":
            circles.append((tuple(sh["center"]), float(sh["radius"])))
        else:
            raise BoardGeometryError(f"unsupported outline shape type {stype!r}")
    return circles, chords


def _chain_loops(chords: list) -> list[list]:
    """Stitch ``(p1, p2)`` chords into ordered closed point loops by endpoint matching.

    Raises :class:`BoardGeometryError` when the chords do not form at least one closed loop.
    """
    if not chords:
        return []

    def close(a, b):
        return abs(a[0] - b[0]) <= _OUTLINE_TOL and abs(a[1] - b[1]) <= _OUTLINE_TOL

    remaining = list(chords)
    loops: list[list] = []
    while remaining:
        p1, p2 = remaining.pop(0)
        loop = [p1, p2]
        progressed = True
        while not close(loop[0], loop[-1]) and progressed:
            progressed = False
            for i, (a, b) in enumerate(remaining):
                if close(a, loop[-1]):
                    loop.append(b)
                    remaining.pop(i)
                    progressed = True
                    break
                if close(b, loop[-1]):
                    loop.append(a)
                    remaining.pop(i)
                    progressed = True
                    break
        if not close(loop[0], loop[-1]):
            raise BoardGeometryError(
                "Edge.Cuts outline does not form a closed loop (open chain near "
                f"{loop[-1]}). The board outline must be one or more closed contours.")
        loops.append(loop[:-1])  # drop the duplicated closing point
    return loops


def _loop_area(loop: list) -> float:
    """Signed shoelace area of a closed point loop (mm^2)."""
    a = 0.0
    n = len(loop)
    for i in range(n):
        x1, y1 = loop[i]
        x2, y2 = loop[(i + 1) % n]
        a += x1 * y2 - x2 * y1
    return a / 2.0


def _outline_face(outline: dict):
    """Build a build123d ``Face`` from the Edge.Cuts outline (largest loop = outer, rest = holes).

    Returns ``(face, area_mm2)``. Raises :class:`BoardGeometryError` for an empty / open outline.
    """
    from build123d import Polygon, Vector

    if not outline or not outline.get("shapes"):
        raise BoardGeometryError("board has no Edge.Cuts outline shapes; cannot build a substrate.")

    circles, chords = _outline_segments(outline)
    poly_loops = _chain_loops(chords)

    # Candidate faces: each polygonal loop + each full circle. Pick the largest-area loop as the
    # board outer; everything strictly inside it is treated as a cut-out (board hole / slot).
    cand: list[tuple[float, Any]] = []
    for loop in poly_loops:
        area = abs(_loop_area(loop))
        if area <= 1e-9:
            continue
        pts = [Vector(x, y, 0) for (x, y) in loop]
        cand.append((area, Polygon(*pts, align=None)))
    for (cx, cy), r in circles:
        area = math.pi * r * r
        cand.append((area, _circle_face(cx, cy, r)))

    if not cand:
        raise BoardGeometryError("Edge.Cuts outline produced no usable closed region.")

    cand.sort(key=lambda t: t[0], reverse=True)
    outer_area, outer = cand[0]
    face = outer
    for _area, inner in cand[1:]:
        try:
            face = face - inner
        except Exception:  # noqa: BLE001 — a non-overlapping extra loop is simply ignored
            pass
    # ``face`` may be a Sketch/ShapeList after subtraction; normalize to a single Face.
    face = _as_face(face)
    return face, outer_area


def _circle_face(cx: float, cy: float, r: float):
    from build123d import Circle, Pos
    return _as_face(Pos(cx, cy, 0) * Circle(r))


def _as_face(obj):
    """Coerce a build123d Sketch / ShapeList / Face into a single Face."""
    from build123d import Face
    if isinstance(obj, Face):
        return obj
    faces = getattr(obj, "faces", None)
    if callable(faces):
        fl = obj.faces()
        if fl:
            # Largest-area face (subtraction can leave slivers).
            return max(fl, key=lambda f: f.area)
    if isinstance(obj, Face):
        return obj
    raise BoardGeometryError(f"could not coerce {type(obj)!r} to a Face")


# --------------------------------------------------------------------------- substrate


def _z_base(opts: BoardToStepOpts, thickness: float) -> float:
    """Z of the board's bottom face given the z-origin convention."""
    return -thickness / 2.0 if opts.z_origin == "center" else 0.0


def _iter_drills(board: dict):
    """Yield ``(x, y, drill_diameter)`` for every plated/NPTH drill: tht/np_tht pads + vias."""
    for fp in board.get("footprints", []):
        for pad in fp.get("pads", []):
            drill = pad.get("drill")
            if not drill:
                continue
            dia = float(drill.get("diameter", 0.0))
            if dia <= 0.0:
                continue
            wx, wy = pad_world(fp, pad)
            # drill offset is in the pad-local frame; for a board hole the pad-center hole is the
            # common case — offset handling kept simple (added in board axes).
            off = drill.get("offset") or [0.0, 0.0]
            yield (wx + float(off[0]), wy + float(off[1]), dia)
    for via in board.get("vias", []):
        dia = float(via.get("drill", 0.0))
        if dia <= 0.0:
            continue
        at = via.get("at", [0.0, 0.0])
        yield (float(at[0]), float(at[1]), dia)


def _build_substrate(board: dict, opts: BoardToStepOpts, summary: _Summary):
    """Extrude the outline to thickness, subtract drill cylinders. Returns a build123d Part/Solid."""
    from build123d import Pos, Cylinder, Plane, extrude

    stack = board.get("stackup", {}) or {}
    thickness = float(stack.get("thickness", 1.6))
    if thickness <= 0.0:
        raise BoardGeometryError(f"stackup.thickness must be > 0, got {thickness}")

    face, area = _outline_face(board.get("outline", {}) or {})
    summary.outline_area = area
    summary.thickness = thickness

    zb = _z_base(opts, thickness)
    # Place the face at the board bottom and extrude up by thickness.
    solid = extrude(Plane.XY.offset(zb) * face, amount=thickness)

    # Subtract drill cylinders (slightly over-tall so the cut is clean through both faces).
    holes = list(_iter_drills(board))
    summary.n_holes = len(holes)
    overshoot = thickness * 0.1 + 0.1
    for (x, y, dia) in holes:
        cyl = Pos(x, y, zb + thickness / 2.0) * Cylinder(radius=dia / 2.0,
                                                         height=thickness + 2 * overshoot)
        try:
            solid = solid - cyl
        except Exception as exc:  # noqa: BLE001
            summary.warnings.append(f"drill subtraction failed at ({x:.2f},{y:.2f}): {exc}")
    try:
        solid.color = _color(opts.mask_color)
        solid.label = "PCB_Substrate"
    except Exception:  # noqa: BLE001
        pass
    return solid, thickness, zb


def _color(rgb):
    from build123d import Color
    return Color(float(rgb[0]), float(rgb[1]), float(rgb[2]))


# --------------------------------------------------------------------------- copper (optional)


def _build_copper(board: dict, opts: BoardToStepOpts, thickness: float, zb: float, summary: _Summary):
    """Thin copper extrusions for tracks on F.Cu / B.Cu. Pads/zones are deferred (bounding realism
    only, per spec); tracks give the enclosure designer a sense of routed thickness. Returns a list
    of build123d solids (possibly empty)."""
    from build123d import Pos, Plane, Line, extrude, Circle

    t_cu = float(opts.copper_thickness)
    out: list = []
    # Top copper sits on top face (z = zb + thickness); bottom copper under the bottom face.
    z_top = zb + thickness
    z_bot = zb - t_cu
    for trk in board.get("tracks", []):
        layer = trk.get("layer")
        if layer not in ("F.Cu", "B.Cu"):
            continue
        if trk.get("kind", "segment") != "segment":
            continue  # arc tracks deferred — bounding realism only
        try:
            p1 = trk["p1"]
            p2 = trk["p2"]
            width = float(trk.get("width", 0.25))
            z = z_top if layer == "F.Cu" else z_bot
            seg = _track_solid(p1, p2, width, t_cu, z)
            if seg is not None:
                seg.color = _color(opts.copper_color)
                out.append(seg)
        except Exception as exc:  # noqa: BLE001
            summary.warnings.append(f"copper track skipped: {exc}")
    return out


def _track_solid(p1, p2, width: float, t_cu: float, z: float):
    """A rounded-end track segment extruded by ``t_cu``, sitting with its bottom at z."""
    from build123d import Pos, Plane, extrude, Rectangle, Circle, Rot
    x1, y1 = float(p1[0]), float(p1[1])
    x2, y2 = float(p2[0]), float(p2[1])
    dx, dy = x2 - x1, y2 - y1
    length = math.hypot(dx, dy)
    if length <= 1e-9:
        return None
    ang = math.degrees(math.atan2(dy, dx))
    cx, cy = (x1 + x2) / 2.0, (y1 + y2) / 2.0
    # Body rectangle (length x width) + two end caps as circles, centered at the segment midpoint.
    body = Rectangle(length, width)
    cap = Circle(width / 2.0)
    sketch = body + Pos(length / 2.0, 0) * cap + Pos(-length / 2.0, 0) * cap
    placed = Pos(cx, cy, z) * Rot(0, 0, ang) * sketch
    return extrude(placed, amount=t_cu)


# --------------------------------------------------------------------------- components


def _footprint_extent(fp: dict) -> tuple[float, float]:
    """Footprint courtyard / pad-extent (width, height) in the footprint-local frame (mm).

    Prefers a F.CrtYd / B.CrtYd courtyard graphic bbox; falls back to the bounding box of all pad
    extents; finally a small default. Returns local-axes (w, h) before rotation/mirror.
    """
    xs: list[float] = []
    ys: list[float] = []

    def _accumulate_rect(cx, cy, w, h):
        xs.extend([cx - w / 2.0, cx + w / 2.0])
        ys.extend([cy - h / 2.0, cy + h / 2.0])

    # Courtyard graphics (most accurate collision extent).
    crtyd_pts: list = []
    for g in fp.get("graphics", []) or []:
        if g.get("layer") not in ("F.CrtYd", "B.CrtYd"):
            continue
        gtype = g.get("type")
        if gtype == "line":
            crtyd_pts.extend([tuple(g["p1"]), tuple(g["p2"])])
        elif gtype == "rect":
            p1 = g.get("p1") or g.get("start")
            p2 = g.get("p2") or g.get("end")
            if p1 and p2:
                crtyd_pts.extend([tuple(p1), tuple(p2)])
        elif gtype == "circle":
            cx, cy = g["center"]
            r = float(g["radius"])
            crtyd_pts.extend([(cx - r, cy - r), (cx + r, cy + r)])
        elif gtype == "poly" and g.get("points"):
            crtyd_pts.extend(tuple(p) for p in g["points"])
    if crtyd_pts:
        xs = [p[0] for p in crtyd_pts]
        ys = [p[1] for p in crtyd_pts]
    else:
        # Pad extent fallback.
        for pad in fp.get("pads", []):
            px, py = float(pad["pos"][0]), float(pad["pos"][1])
            size = pad.get("size") or [0.5, 0.5]
            w, h = float(size[0]), float(size[1])
            _accumulate_rect(px, py, w, h)
        if xs:
            # margin so the proxy fully encloses the pads.
            xs = [min(xs) - _PROXY_PAD_MARGIN, max(xs) + _PROXY_PAD_MARGIN]
            ys = [min(ys) - _PROXY_PAD_MARGIN, max(ys) + _PROXY_PAD_MARGIN]

    if not xs or not ys:
        return (_PROXY_MIN_SPAN, _PROXY_MIN_SPAN)
    w = max(max(xs) - min(xs), _PROXY_MIN_SPAN)
    h = max(max(ys) - min(ys), _PROXY_MIN_SPAN)
    return (w, h)


def _proxy_height(fp: dict) -> float:
    """Per-footprint proxy height: explicit ``fields.height`` if present, else per-attr default."""
    fields = fp.get("fields") or {}
    h = fields.get("height")
    if h is not None:
        try:
            hv = float(h)
            if hv > 0:
                return hv
        except (TypeError, ValueError):
            pass
    return _PROXY_HEIGHT.get(fp.get("attr", "smd"), _PROXY_HEIGHT_DEFAULT)


def _place_component(shape, fp: dict, thickness: float, zb: float):
    """Place a (kernel-local, z≥0, sitting-on-board) component shape at the footprint's world pose.

    Mirror/rotate/translate per ``(pos, angle, side)``. Top parts sit on the top face
    (z = zb + thickness); bottom parts are flipped (mirrored across z) to sit under the bottom face
    (z = zb), so their body grows in -z. Returns the world-placed shape.
    """
    from build123d import Pos, Rot

    px, py = float(fp["pos"][0]), float(fp["pos"][1])
    angle = float(fp.get("angle", 0.0))
    side = fp.get("side", "top")

    if side == "bottom":
        # Flip the part upside-down (mirror Z about z=0) then drop it under the bottom face.
        flipped = Rot(180, 0, 0) * shape           # body now grows downward (-z)
        placed = Pos(px, py, zb) * Rot(0, 0, -angle) * flipped
    else:
        placed = Pos(px, py, zb + thickness) * Rot(0, 0, angle) * shape
    return placed


def _proxy_shape(fp: dict):
    """An axis-aligned box proxy sitting on z=0, sized to the footprint extent × proxy height."""
    from build123d import Pos, Box
    w, h = _footprint_extent(fp)
    ht = _proxy_height(fp)
    # Box centered at origin; lift so it sits on z=0 (its base at z=0, growing +z).
    return Pos(0, 0, ht / 2.0) * Box(w, h, ht)


def _is_on_board(fp: dict) -> bool:
    """A footprint contributes a 3D body unless explicitly excluded (virtual / on_board=False)."""
    if fp.get("on_board") is False:
        return False
    return True


def _load_model(model_ref: str, summary: _Summary):
    """Try to load a footprint ``model_3d`` via engine.doc.formats.import_path.

    Returns a build123d shape on success, or ``None`` (with a recorded warning) on any failure —
    including WRL (mesh) sources and missing files. The caller then uses the box-proxy fallback.
    """
    if not model_ref:
        return None
    ext = os.path.splitext(model_ref)[1].lower()
    if ext in (".wrl", ".vrml"):
        summary.warnings.append(f"model_3d {model_ref!r} is WRL (mesh) — using box proxy instead.")
        return None
    if not os.path.isfile(model_ref):
        summary.warnings.append(f"model_3d {model_ref!r} not found — using box proxy.")
        return None
    try:
        from engine.doc.formats import import_path
        bodies = import_path(model_ref)
    except Exception as exc:  # noqa: BLE001
        summary.warnings.append(f"model_3d {model_ref!r} failed to load ({exc}) — using box proxy.")
        return None
    shapes = [s for _n, s in bodies if getattr(s, "wrapped", None) is not None]
    if not shapes:
        summary.warnings.append(f"model_3d {model_ref!r} loaded no solid bodies — using box proxy.")
        return None
    if len(shapes) == 1:
        return shapes[0]
    from build123d import Compound
    return Compound(children=list(shapes))


def _build_components(board: dict, opts: BoardToStepOpts, thickness: float, zb: float,
                      summary: _Summary):
    """One world-placed body per on_board footprint (real model when loadable, else box proxy)."""
    out: list = []
    for fp in board.get("footprints", []):
        if not _is_on_board(fp):
            continue
        if not fp.get("pads"):
            summary.warnings.append(f"footprint {fp.get('ref', fp.get('id'))!r} has no pads — skipped.")
            summary.n_skipped += 1
            continue
        model = _load_model(fp.get("model_3d"), summary)
        if model is not None:
            shape = model
            summary.n_models += 1
        else:
            shape = _proxy_shape(fp)
            summary.n_proxies += 1
        try:
            placed = _place_component(shape, fp, thickness, zb)
            placed.label = str(fp.get("ref") or fp.get("id") or "CMP")
            out.append(placed)
            summary.n_components += 1
        except Exception as exc:  # noqa: BLE001
            summary.warnings.append(
                f"component {fp.get('ref', fp.get('id'))!r} placement failed: {exc}")
            summary.n_skipped += 1
    return out


# --------------------------------------------------------------------------- public API


def board_to_step(board: dict, out_path: str, opts: Any = None) -> dict:
    """Convert ``board`` (an ``aicad.pcb/1`` BoardDocument) into a STEP solid at ``out_path``.

    Returns ``{"step_path": <abs path>, "summary": {...}}``. Raises :class:`BoardGeometryError`
    for a board whose outline is empty or does not close.
    """
    from build123d import Compound, export_step

    opts = BoardToStepOpts.coerce(opts)
    summary = _Summary()

    substrate, thickness, zb = _build_substrate(board, opts, summary)

    children: list = [substrate]

    if opts.copper:
        children.extend(_build_copper(board, opts, thickness, zb, summary))

    if opts.components:
        children.extend(_build_components(board, opts, thickness, zb, summary))

    try:
        summary.board_volume = float(substrate.volume)
    except Exception:  # noqa: BLE001
        summary.board_volume = 0.0

    assembly = Compound(children=children)
    assembly.label = str(board.get("name") or "PCB")

    out_path = os.path.abspath(out_path)
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    export_step(assembly, out_path)

    return {"step_path": out_path, "summary": summary.asdict()}


def board_to_part_doc(board: dict, step_path: str) -> dict:
    """A minimal ``aicad.part/1`` document whose single ``import`` feature references ``step_path``.

    Matches engine/doc/schema.py's ``import`` feature envelope (params: source_path/format/
    original_name; refs: {}) exactly, so it round-trips through the engine.doc compiler and the host
    can open it directly in the CAD viewport (fab_outputs.md §5 step 4).
    """
    name = str(board.get("name") or "PCB")
    return {
        "schema": "aicad.part/1",
        "id": "doc-" + uuid.uuid4().hex[:8],
        "name": name,
        "units": {"system": "MMGS"},
        "material": None,
        "equations": {"globals": []},
        "features": [
            {
                "id": "f1",
                "type": "import",
                "name": "PCB",
                "suppressed": False,
                "params": {
                    "source_path": os.path.abspath(step_path),
                    "format": "step",
                    "original_name": name,
                },
                "refs": {},
            }
        ],
    }
