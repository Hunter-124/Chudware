"""Fabrication output writers for aicad.pcb/1 boards.

Re-exports the primary entry points so callers can do::

    from services.ecad.fab import write_gerbers, write_drill, write_bom, write_placement
"""
from __future__ import annotations

from .gerber import write_gerber_layer, write_gerbers
from .excellon import write_drill
from .bom import write_bom
from .placement import write_placement

__all__ = [
    "write_gerber_layer",
    "write_gerbers",
    "write_drill",
    "write_bom",
    "write_placement",
]
