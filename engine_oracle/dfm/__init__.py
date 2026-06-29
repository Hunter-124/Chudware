"""AI-CAD design-for-manufacture (DfM) helpers.

Process-aware DfM math shared by the MCP tools and the in-app agent. The first member,
:mod:`engine.dfm.process_profiles`, owns the per-process parameter table (FDM-default),
the rule-based process resolver + ambiguity gate, and the fit-clearance math that lets the
modeler build a *receptacle that actually fits* on the first print.

Stdlib-only and dependency-free so it can be imported anywhere (engine worker, MCP server,
the C++ ``core_bridge`` seam) without dragging in OCP/build123d.
"""
from __future__ import annotations

from . import process_profiles

__all__ = ["process_profiles"]
