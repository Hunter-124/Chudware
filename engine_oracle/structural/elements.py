"""Structural element dataclasses + the ``aicad.structure/1`` document (structural_schema.md §1/§2).

A structural document is a flat list of elements (beam/column/post/joist/stud_wall/footing) plus
load cases. Each element carries the fields the BOM/cost takeoff downstream reads (``section_name``,
``material``) — frozen day 1. Geometry lives in :mod:`engine.structural.brep`; this module is
pure-Python data (no OCCT import) so it stays cheap to import and easy to serialize.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import ClassVar, Optional

SCHEMA = "aicad.structure/1"

ELEMENT_TYPES = ("beam", "column", "post", "joist", "stud_wall", "footing")

# Identity 4×4, row-major (16 floats). Elements default to local space (extruded +Z at the origin).
IDENTITY_4X4 = [
    1.0, 0.0, 0.0, 0.0,
    0.0, 1.0, 0.0, 0.0,
    0.0, 0.0, 1.0, 0.0,
    0.0, 0.0, 0.0, 1.0,
]


# --------------------------------------------------------------------------- elements


@dataclass
class StructuralElement:
    """Base for every structural element. ``element_type`` is a per-subclass class constant (a
    ``ClassVar``, not an init field, so subclasses stay valid dataclasses)."""
    element_type: ClassVar[str] = ""
    section_name: str
    material: str
    length_mm: float
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    transform: list[float] = field(default_factory=lambda: list(IDENTITY_4X4))

    def to_dict(self) -> dict:
        return {
            "element_type": self.element_type,
            "id": self.id,
            "section_name": self.section_name,
            "material": self.material,
            "length_mm": self.length_mm,
            "transform": list(self.transform),
        }


@dataclass
class Beam(StructuralElement):
    element_type: ClassVar[str] = "beam"


@dataclass
class Column(StructuralElement):
    element_type: ClassVar[str] = "column"


@dataclass
class Post(StructuralElement):
    element_type: ClassVar[str] = "post"


@dataclass
class Joist(StructuralElement):
    element_type: ClassVar[str] = "joist"


@dataclass
class StudWall(StructuralElement):
    element_type: ClassVar[str] = "stud_wall"


@dataclass
class Footing(StructuralElement):
    element_type: ClassVar[str] = "footing"


_ELEMENT_CLASSES: dict[str, type[StructuralElement]] = {
    "beam": Beam, "column": Column, "post": Post,
    "joist": Joist, "stud_wall": StudWall, "footing": Footing,
}


def element_from_dict(d: dict) -> StructuralElement:
    et = d.get("element_type")
    cls = _ELEMENT_CLASSES.get(et)
    if cls is None:
        raise ValueError(f"unknown element_type {et!r}; expected one of {ELEMENT_TYPES}")
    return cls(
        section_name=d["section_name"],
        material=d["material"],
        length_mm=float(d["length_mm"]),
        id=d.get("id") or uuid.uuid4().hex[:12],
        transform=list(d.get("transform") or IDENTITY_4X4),
    )


def make_element(element_type: str, section_name: str, material: str, length_mm: float,
                 *, transform: Optional[list[float]] = None,
                 id: Optional[str] = None) -> StructuralElement:  # noqa: A002
    return element_from_dict({
        "element_type": element_type, "section_name": section_name, "material": material,
        "length_mm": length_mm, "transform": transform, "id": id,
    })


# --------------------------------------------------------------------------- document


@dataclass
class StructureDoc:
    name: str = "Structure1"
    units: str = "mm"
    elements: list[StructuralElement] = field(default_factory=list)
    load_cases: list[dict] = field(default_factory=list)

    def add(self, element: StructuralElement) -> StructuralElement:
        self.elements.append(element)
        return element

    def get(self, element_id: str) -> StructuralElement:
        for e in self.elements:
            if e.id == element_id:
                return e
        raise KeyError(f"no structural element '{element_id}'")

    def to_dict(self) -> dict:
        return {
            "schema": SCHEMA,
            "name": self.name,
            "units": self.units,
            "elements": [e.to_dict() for e in self.elements],
            "load_cases": list(self.load_cases),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "StructureDoc":
        if not is_structure(d):
            raise ValueError(f"not an aicad.structure document (schema={d.get('schema')!r})")
        doc = cls(name=d.get("name", "Structure1"), units=d.get("units", "mm"),
                  load_cases=list(d.get("load_cases", [])))
        doc.elements = [element_from_dict(e) for e in d.get("elements", [])]
        return doc


# --------------------------------------------------------------------------- doc-kind helpers


def new_structure(name: str = "Structure1") -> dict:
    """A fresh, empty ``aicad.structure/1`` document dict."""
    return StructureDoc(name=name).to_dict()


def is_structure(doc: dict) -> bool:
    return isinstance(doc, dict) and doc.get("schema") == SCHEMA


def doc_kind(doc: dict) -> str:
    """Return ``"structure"`` for an ``aicad.structure/1`` doc, else ``""`` (mirrors
    :func:`engine.ecad.schema.doc_kind`). A doc self-declares its kind via its ``schema`` field —
    there is no separate central Python registry (the parametric doc engine is in the C++ core)."""
    return "structure" if is_structure(doc) else ""
