"""Wire-harness / cable design — Python side of the ``aicad.harness/1`` study.

* :mod:`services.harness.model` — the schema (connectors, wires, a routed segment tree),
  the connector-family table, bundle sizing, derived routing state, document-study
  persistence (``load_harness`` / ``save_harness``) and the cut-list / BOM report.
* :mod:`services.harness.formboard` — deterministic flattening of the harness tree into a
  true-length 2D formboard (SVG / DXF / PDF) with an overlap check.

The routing and path checks run in the C++ engine (``harness_route`` /
``harness_path_check`` RPCs, reached through :mod:`services.mcp.core_bridge`); the MCP tools
live in :mod:`services.mcp.harness_tools`. Units are mm and degrees everywhere.
"""

from __future__ import annotations

__all__ = ["model", "formboard"]
