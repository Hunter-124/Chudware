"""Surviving document-layer helpers.

The Python parametric document engine (compiler / session / sketch+mate solvers / schema /
evaluate / drawings / formats handlers / …) was rewritten in the C++ core (libaicad) and deleted.
What remains in this package is closure-free and still used by the kept Python surfaces:

- ``tessellate`` / ``mesh_body`` — AICM mesh interchange (engine.sim worker + overlay)
- ``formats`` — pure import/export helpers (``import_path`` used by engine.ecad.threed)
- ``formats_solidworks`` — COM->STEP bridge (referenced by ``formats``)

This package no longer re-exports a modeling API; import the specific helper module you need.
"""
