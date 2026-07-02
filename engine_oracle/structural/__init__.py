"""AI-Structural — parametric structural framing (beams, columns, posts, joists, walls, footings).

A sibling of :mod:`engine.ecad` that adds the structural pillar to AI-CAD. Doc kind
``aicad.structure/1``: a flat list of structural elements + load cases. Every element compiles to
an OCCT B-rep solid (:func:`engine.structural.brep.element_to_brep`) so structural bodies render in
the normal viewport alongside CAD parts. Python owns the schema + geometry; the C++ host and the MCP
bridge call this engine directly. See ``contracts/structural_schema.md``.

Canonical units are **mm internally** everywhere (matching engine.doc / engine.ecad).
"""

from __future__ import annotations

__all__ = ["elements", "brep"]
