"""OCCT B-rep generation for structural elements (structural_schema.md §4).

``section_profile(name)`` parses a section spec into a closed OCCT wire (centred on the local
origin, in the XY plane); ``element_to_brep(element)`` faces that wire and extrudes it along +Z by
``length_mm`` with ``BRepPrimAPI_MakePrism``, then applies the element's 4×4 ``transform``. The
kernel is OCP (cadquery-ocp) — the same OCCT binding ``engine.kernel`` / ``engine.ecad.threed`` use.
OCP is imported lazily inside functions so importing this module stays cheap when only the schema
(``engine.structural.elements``) is needed.

Section forms parsed (see the §4 table): ``WxH`` rectangle (lumber/generic), ``RHS WxHxT`` /
3-dim ``WxHxT`` (rectangular hollow → outer rectangle for now), and ``I HxBxTWxTF`` / a few named
AISC ``W`` shapes (I-profile). Unknown names fall back to a per-element-type default rectangle so a
valid solid is always produced.
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


def section_profile(name: str, *, element_type: Optional[str] = None):
    """Parse a section spec into a closed OCCT ``TopoDS_Wire`` (structural_schema.md §4).

    Falls back to the element type's default section (or a 100×100 square) when *name* is empty or
    unparseable, so a caller always gets a valid wire.
    """
    raw = (name or "").strip()
    key = raw.upper().replace(" ", "")

    # Named I-shape (AISC W-series subset)
    if key in _NAMED_I_SHAPES:
        return _i_wire(*_NAMED_I_SHAPES[key])

    # Explicit I-shape: I<h>x<b>x<tw>x<tf>
    if key.startswith("I"):
        d = _dims(key)
        if len(d) >= 4:
            return _i_wire(d[0], d[1], d[2], d[3])

    # Rectangular hollow section: RHS<w>x<h>x<t> or a bare 3-dim WxHxT → outer rectangle
    if key.startswith("RHS"):
        d = _dims(key)
        if len(d) >= 2:
            return _rect_wire(d[0], d[1])

    d = _dims(key)
    if len(d) >= 2:            # WxH (or WxHxT: use the first two as the outer rectangle)
        return _rect_wire(d[0], d[1])
    if len(d) == 1:            # square nominal, e.g. "100"
        return _rect_wire(d[0], d[0])

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


def element_to_brep(element: StructuralElement):
    """Compile a structural element to an OCCT ``TopoDS_Shape`` solid: extrude its section profile
    along +Z by ``length_mm``, then place it with the element's ``transform``."""
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace
    from OCP.BRepPrimAPI import BRepPrimAPI_MakePrism
    from OCP.gp import gp_Vec

    if element.length_mm <= 0:
        raise ValueError(f"element {element.id} has non-positive length_mm={element.length_mm}")

    wire = section_profile(element.section_name, element_type=element.element_type)
    face = BRepBuilderAPI_MakeFace(wire).Face()
    solid = BRepPrimAPI_MakePrism(face, gp_Vec(0.0, 0.0, float(element.length_mm))).Shape()
    return _apply_transform(solid, element.transform)


def volume_mm3(shape) -> float:
    """Volume of a solid in mm³ (GProp) — used by tests and the takeoff downstream."""
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps

    props = GProp_GProps()
    BRepGProp.VolumeProperties_s(shape, props)
    return props.Mass()
