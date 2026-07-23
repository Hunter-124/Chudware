"""AI-Piping — routed pipe assemblies (pipe runs + fittings) as a discipline module.

A sibling of :mod:`engine.doc` and :mod:`engine.ecad` that adds mechanical piping to
Chudware, following the ECAD precedent: a frozen document schema (``contracts/piping_schema.md``
/ :mod:`services.piping.schema`), a CSV-backed fitting catalog with NPS/schedule resolution via
the MIT-licensed ``fluids`` library (:mod:`services.piping.catalog`), a build123d/OCCT
realization bridge that sweeps runs to B-rep and hands the host an ``aicad.part/1`` import doc
for the viewport (:mod:`services.piping.threed`), and a native numpy/scipy steady-state 1-D network
hydraulic solver — node pressures/heads + link flows, per-segment pressure-drop reports with
K-factor breakdowns, and pump-curve CSV import with affinity scaling (:mod:`engine.piping.solve`),
the direct hydraulic analogue of the MNA circuit simulator in :mod:`engine.sim.circuit`.

Canonical units are **mm internally** everywhere (matching engine.doc / engine.ecad); angles
in degrees. See ``contracts/piping_schema.md``.
"""

from __future__ import annotations

__all__ = ["schema", "catalog", "threed", "solve"]
