"""Civil / site / utility infrastructure engineering (contracts/civil_schema.md).

Pure-Python engines over the ``aicad.civil/1`` site model: CRS-aware GIS and LandXML/IFC
exchange, terrain (TIN/DEM), alignments and earthwork, pressurized water distribution,
gravity sewer/storm networks, hydrology and detention, culverts, buried-pipe and
pipe-support structural checks, quantity takeoff/cost/schedule, and the tie-in coupling to
process piping (``aicad.piping/1``) through ``aicad.simhandoff/1``.

Canonical units are SI metres (see the contract §1); every analysis returns
``{ok, ..., warnings, assumptions, method}`` and refuses with
:class:`services.civil.errors.CivilError`.
"""

from __future__ import annotations

__all__ = [
    "errors", "schema", "crs", "gis_io", "criteria", "landxml", "ifc_export", "terrain",
    "dem", "alignment", "water", "epanet_inp", "gravity", "swmm_inp", "hydrology",
    "culvert", "buried", "supports", "planning", "piping_link",
]
