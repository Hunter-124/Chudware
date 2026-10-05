"""AI-Piping — Python side of routed pipe assemblies (pipe runs + fittings).

The C++ core (``core/piping``, served by libchudware and reached through
:mod:`services.mcp.core_bridge`) owns the ``aicad.piping/1`` validator, fluid properties,
the steady-state 1-D network solve / K-factor pressure-drop report / pump operating point,
and the CFD boundary + 1D-vs-3D verify ports (``piping_solve``, ``piping_report``,
``piping_operating_point``, ``piping_fluid``, ``piping_cfd_boundary``, ``piping_verify_cfd``).

This package keeps the kernel-side and data legs:

* :mod:`services.piping.schema` — Python structural validator + factories for
  ``contracts/piping_schema.md`` documents; the kernel legs below share its constants.
* :mod:`services.piping.catalog` — NPS/schedule pipe lookup via the MIT-licensed ``fluids``
  library + the bundled B16 fitting CSV (also read by the parts-library fittings adapter).
* :mod:`services.piping.threed` — build123d/OCCT sweep of runs + fittings to STEP and an
  ``aicad.part/1`` import doc for the viewport.
* :mod:`services.piping.cfd_handoff` — bore fluid-domain extraction (``internal_volume``,
  driven by the MCP ``piping_internal_volume`` / ``piping_cfd_handoff`` tools) plus Python
  ``cfd_boundary_from_solve`` / ``verify_pressure_drop``.

Canonical units are **mm internally** everywhere (matching the part and ECAD documents);
angles in degrees. See ``contracts/piping_schema.md``.
"""

from __future__ import annotations

__all__ = ["schema", "catalog", "threed", "cfd_handoff"]
