"""AI-ECAD — electronics design (schematic capture, PCB layout, simulation, fabrication).

A sibling of :mod:`engine.doc` that adds KiCad-class EDA to AI-CAD. Python is the authority; the
C++ host mirrors and submits ops. See ``docs/ECAD_PLAN.md`` and ``docs/contracts/ecad/``.

Canonical units are **mm internally** everywhere (matching engine.doc); angles in degrees CCW.
"""

from __future__ import annotations

__all__ = ["schema"]
