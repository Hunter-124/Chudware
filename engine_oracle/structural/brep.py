"""OCCT B-rep generation for structural elements (structural_schema.md §4).

``section_profile(name)`` parses a section spec into a closed OCCT wire (centred on the local
origin, in the XY plane) describing the section's OUTER boundary only; ``element_to_brep(element)``
builds the full section FACE (voiding the wall for hollow sections — see below) and extrudes it
along +Z by ``length_mm`` with ``BRepPrimAPI_MakePrism``, then applies the element's 4×4
``transform``. The kernel is OCP (cadquery-ocp) — the same OCCT binding ``engine.kernel`` /
``engine.ecad.threed`` use. OCP is imported lazily inside functions so importing this module stays
cheap when only the schema (``engine.structural.elements``) is needed.

Section forms parsed (see the §4 table): ``WxH`` rectangle (lumber/generic), ``RHS`` / ``HSS``
``WxHxT`` or a bare 3-dim ``WxHxT`` (rectangular hollow — outer-minus-inner prism, wall thickness
``T``), ``CHS`` / ``PIPE`` ``ODxT`` (circular hollow — true annulus prism), and ``I HxBxTWxTF`` / a
few named AISC ``W`` shapes (I-profile, still a solid open-thin-wall wire — no void). Unknown names
fall back to a per-element-type default rectangle so a valid solid is always produced. A hollow spec
whose wall thickness doesn't fit the outer envelope (``T <= 0`` or ``T >= half the smaller outer
dimension``) degrades to its solid outer shape rather than producing a self-intersecting face.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Optional

from engine.structural.elements import IDENTITY_4X4, StructuralElement

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids importing OCP at module load
    from OCP.TopoDS import TopoDS_Shape, TopoDS_Wire

# A tiny AISC-ish table of named I-shapes → (height, width, web_t, flange_t) in mm. Enough for the
# scaffold; the full library is a later slice.
_NAMED_I_SHAPES: dict[str, tuple[float, float, float, float]] = {
    "W150X100": (148.0, 100.0, 4.3, 7.0),
    "W200X100": (200.0, 100.0, 5.5, 8.0),
    "W250X150": (250.0, 150.0, 6.5, 9.0),
    "W310X165": (310.0, 165.0, 7.5, 11.8),
}

# Per-element-type default section when a name cannot be parsed (mm). Keeps element_to_brep total.
_DEFAULT_SECTION: dict[str, str] = {
    "beam": "W200x100",
    "column": "W200x100",
    "post": "100x100",
    "joist": "38x235",
    "stud_wall": "89x2400",
    "footing": "600x600",
}

_DIMS_RE = re.compile(r"(\d+(?:\.\d+)?)")


def _dims(name: str) -> list[float]:
    return [float(x) for x in _DIMS_RE.findall(name)]


# --------------------------------------------------------------------------- wire builders


def _rect_wire(width: float, height: float):
    """A closed rectangular wire (``width`` × ``height``) centred on the origin in XY."""
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakePolygon
    from OCP.gp import gp_Pnt

    w, h = width / 2.0, height / 2.0
    poly = BRepBuilderAPI_MakePolygon()
    for x, y in ((-w, -h), (w, -h), (w, h), (-w, h)):
        poly.Add(gp_Pnt(x, y, 0.0))
    poly.Close()
    return poly.Wire()


def _i_wire(h: float, b: float, tw: float, tf: float):
    """A closed 12-point I-shape wire (total height ``h``, flange width ``b``, web ``tw``, flange
    ``tf``) centred on the origin in XY."""
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakePolygon
    from OCP.gp import gp_Pnt

    hy, by, wx = h / 2.0, b / 2.0, tw / 2.0
    pts = [
        (-by, -hy), (by, -hy), (by, -hy + tf), (wx, -hy + tf),
        (wx, hy - tf), (by, hy - tf), (by, hy), (-by, hy),
        (-by, hy - tf), (-wx, hy - tf), (-wx, -hy + tf), (-by, -hy + tf),
    ]
    poly = BRepBuilderAPI_MakePolygon()
    for x, y in pts:
        poly.Add(gp_Pnt(x, y, 0.0))
    poly.Close()
    return poly.Wire()


def _circle_wire(diameter: float):
    """A closed circular wire of the given ``diameter``, centred on the origin in XY."""
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeEdge, BRepBuilderAPI_MakeWire
    from OCP.gp import gp_Ax2, gp_Circ, gp_Dir, gp_Pnt

    ax = gp_Ax2(gp_Pnt(0.0, 0.0, 0.0), gp_Dir(0.0, 0.0, 1.0))
    edge = BRepBuilderAPI_MakeEdge(gp_Circ(ax, diameter / 2.0)).Edge()
    return BRepBuilderAPI_MakeWire(edge).Wire()


# --------------------------------------------------------------------------- section spec parsing


def _parse_section(key: str) -> Optional[dict]:
    """Parse an upper-cased, space-stripped section ``key`` into a shape spec dict, or ``None`` if
    unparseable. ``kind`` is one of ``"i"``, ``"rect"``, ``"rect_hollow"``, ``"circle_hollow"``.

    A hollow spec degrades to its solid ``"rect"``/outer-only form when the wall thickness doesn't
    physically fit (``t <= 0`` or ``t >= half the smaller outer dimension``).
    """
    if "/" in key:
        # A fractional-inch AISC catalog designation (e.g. "HSS4X4X1/4", the actual format in
        # estimate._AISC_LBS_PER_FT) isn't parseable by this mm-based digit grammar: the digit
        # regex would silently mis-tokenize "1/4" into dims [1, 4], producing a plausible-but-wrong
        # tiny hollow section instead of an error. Bail out to the caller's safe per-type default
        # rather than fabricate geometry (work/questions/T-BL21.md).
        return None

    # Named I-shape (AISC W-series subset)
    if key in _NAMED_I_SHAPES:
        h, b, tw, tf = _NAMED_I_SHAPES[key]
        return {"kind": "i", "h": h, "b": b, "tw": tw, "tf": tf}

    # Explicit I-shape: I<h>x<b>x<tw>x<tf>
    if key.startswith("I"):
        d = _dims(key)
        if len(d) >= 4:
            return {"kind": "i", "h": d[0], "b": d[1], "tw": d[2], "tf": d[3]}

    # Circular hollow / pipe: CHS<od>x<t> or PIPE<od>x<t>
    if key.startswith("CHS") or key.startswith("PIPE"):
        d = _dims(key)
        if len(d) >= 2 and 0.0 < d[1] < d[0] / 2.0:
            return {"kind": "circle_hollow", "od": d[0], "t": d[1]}

    # Rectangular hollow section: RHS<w>x<h>x<t> / HSS<w>x<h>x<t>, or a bare 3-dim WxHxT
    d = _dims(key)
    if len(d) >= 3 and 0.0 < d[2] < min(d[0], d[1]) / 2.0:
        return {"kind": "rect_hollow", "w": d[0], "h": d[1], "t": d[2]}
    if len(d) >= 2:             # WxH (or an out-of-range WxHxT wall: solid outer rectangle)
        return {"kind": "rect", "w": d[0], "h": d[1]}
    if len(d) == 1:             # square nominal, e.g. "100"
        return {"kind": "rect", "w": d[0], "h": d[0]}
    return None


def _outer_wire(spec: dict):
    """The OUTER boundary wire for a parsed ``spec`` (no hole — hollow kinds return just the
    envelope, matching :func:`section_profile`'s single-wire contract)."""
    kind = spec["kind"]
    if kind == "i":
        return _i_wire(spec["h"], spec["b"], spec["tw"], spec["tf"])
    if kind == "circle_hollow":
        return _circle_wire(spec["od"])
    return _rect_wire(spec["w"], spec["h"])          # "rect" / "rect_hollow"


def section_profile(name: str, *, element_type: Optional[str] = None):
    """Parse a section spec into a closed OCCT ``TopoDS_Wire`` — the section's OUTER boundary
    (structural_schema.md §4). Hollow sections (RHS/HSS/CHS/PIPE) are voided in the solid built by
    :func:`element_to_brep`, but this single-wire API only ever describes the outer envelope.

    Falls back to the element type's default section (or a 100×100 square) when *name* is empty or
    unparseable, so a caller always gets a valid wire.
    """
    raw = (name or "").strip()
    key = raw.upper().replace(" ", "")
    spec = _parse_section(key)
    if spec is not None:
        return _outer_wire(spec)

    # Unparseable → per-type default
    default = _DEFAULT_SECTION.get(element_type or "", "100x100")
    if default != raw:         # guard against infinite recursion if a default were itself bad
        return section_profile(default, element_type=None)
    return _rect_wire(100.0, 100.0)


# --------------------------------------------------------------------------- transform


def _apply_transform(shape, transform: list[float]):
    """Apply a row-major 4×4 rigid ``transform`` to ``shape`` via ``gp_Trsf``. A non-rigid or
    invalid matrix (OCCT rejects scale/shear in gp_Trsf) leaves the shape in local space."""
    if not transform or list(transform) == list(IDENTITY_4X4):
        return shape
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform
    from OCP.gp import gp_Trsf

    m = transform
    trsf = gp_Trsf()
    try:
        trsf.SetValues(
            m[0], m[1], m[2], m[3],
            m[4], m[5], m[6], m[7],
            m[8], m[9], m[10], m[11],
        )
    except Exception:  # noqa: BLE001 - non-rigid matrix: keep local placement rather than crash
        return shape
    return BRepBuilderAPI_Transform(shape, trsf, True).Shape()


# --------------------------------------------------------------------------- element → solid


def _hollow_face(outer, inner):
    """A planar face bounded by ``outer`` with ``inner`` cut out as a hole (OCCT requires the hole
    wire's orientation reversed relative to the outer wire)."""
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace
    from OCP.TopoDS import TopoDS

    mk = BRepBuilderAPI_MakeFace(outer)
    mk.Add(TopoDS.Wire_s(inner.Reversed()))
    return mk.Face()


def _section_face(spec: dict):
    """The full section FACE for a parsed ``spec`` — voided (outer-minus-inner) for hollow kinds,
    a plain faced wire otherwise."""
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace

    kind = spec["kind"]
    if kind == "rect_hollow":
        outer = _rect_wire(spec["w"], spec["h"])
        inner = _rect_wire(spec["w"] - 2.0 * spec["t"], spec["h"] - 2.0 * spec["t"])
        return _hollow_face(outer, inner)
    if kind == "circle_hollow":
        outer = _circle_wire(spec["od"])
        inner = _circle_wire(spec["od"] - 2.0 * spec["t"])
        return _hollow_face(outer, inner)
    return BRepBuilderAPI_MakeFace(_outer_wire(spec)).Face()


def element_to_brep(element: StructuralElement):
    """Compile a structural element to an OCCT ``TopoDS_Shape`` solid: extrude its section face
    (voided for hollow RHS/HSS/CHS/PIPE sections — a true outer-minus-inner prism, not just the
    outer envelope) along +Z by ``length_mm``, then place it with the element's ``transform``."""
    from OCP.BRepPrimAPI import BRepPrimAPI_MakePrism
    from OCP.gp import gp_Vec

    if element.length_mm <= 0:
        raise ValueError(f"element {element.id} has non-positive length_mm={element.length_mm}")

    raw = (element.section_name or "").strip()
    key = raw.upper().replace(" ", "")
    spec = _parse_section(key)
    if spec is None:
        default = _DEFAULT_SECTION.get(element.element_type or "", "100x100")
        spec = _parse_section(default.upper().replace(" ", "")) or {"kind": "rect", "w": 100.0, "h": 100.0}

    face = _section_face(spec)
    solid = BRepPrimAPI_MakePrism(face, gp_Vec(0.0, 0.0, float(element.length_mm))).Shape()
    return _apply_transform(solid, element.transform)


def volume_mm3(shape) -> float:
    """Volume of a solid in mm³ (GProp) — used by tests and the takeoff downstream."""
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps

    props = GProp_GProps()
    BRepGProp.VolumeProperties_s(shape, props)
    return props.Mass()
