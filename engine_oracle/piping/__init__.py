"""AI-Piping — routed pipe assemblies (pipe runs + fittings) as a discipline module.

A sibling of :mod:`engine.doc` and :mod:`engine.ecad` that adds mechanical piping to
AI-CAD, following the ECAD precedent: a frozen document schema (``contracts/piping_schema.md``
/ :mod:`engine.piping.schema`), a CSV-backed fitting catalog with NPS/schedule resolution via
the MIT-licensed ``fluids`` library (:mod:`engine.piping.catalog`), and a build123d/OCCT
realization bridge that sweeps runs to B-rep and hands the host an ``aicad.part/1`` import doc
for the viewport (:mod:`engine.piping.threed`).

Canonical units are **mm internally** everywhere (matching engine.doc / engine.ecad); angles
in degrees. See ``contracts/piping_schema.md``.
"""

from __future__ import annotations

__all__ = ["schema", "catalog", "threed"]
