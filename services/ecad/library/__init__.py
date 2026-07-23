"""engine.ecad.library — ECAD symbol/footprint/3D library system.

Public API re-exported at module level so engine.ecad.ops.py can call::

    from services.ecad import library
    library.resolve("Device:R")
    library.resolve_footprint("Resistor_SMD:R_0603")
    library.search("resistor")

Sub-modules:
  sexpr      — S-expression parser/writer (dependency-free)
  kicad_sym  — KiCad .kicad_sym importer
  kicad_mod  — KiCad .kicad_mod importer
  native     — multi-source Library resolver + module-level singletons
  starter    — clean-room starter symbol + footprint definitions
"""

from __future__ import annotations

from .native import (
    Library,
    resolve,
    resolve_footprint,
    search,
    get_default_library,
)
from .starter import (
    SYMBOLS,
    FOOTPRINTS,
    get_symbol,
    get_footprint,
)
from .sexpr import parse as sexpr_parse, dumps as sexpr_dumps
from .kicad_sym import load_kicad_sym
from .kicad_mod import load_kicad_mod, load_pretty

__all__ = [
    # resolver
    "Library",
    "resolve",
    "resolve_footprint",
    "search",
    "get_default_library",
    # starter
    "SYMBOLS",
    "FOOTPRINTS",
    "get_symbol",
    "get_footprint",
    # sexpr
    "sexpr_parse",
    "sexpr_dumps",
    # kicad importers
    "load_kicad_sym",
    "load_kicad_mod",
    "load_pretty",
]
