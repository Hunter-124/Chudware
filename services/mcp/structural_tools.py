"""Structural analysis MCP tools (T-D2) — PyNite member forces & deflections.

  struct_analyze(doc_path, load_combo)  -> member forces + deflections for an aicad.structure/1 doc
                                           under an ASCE 7 load combination (structural_schema.md §3).

Day 3 (T-D3/T-D4) adds ``struct_check`` (D/C ratio + code compliance); a placeholder is registered
here so the tool surface is discoverable early. PyNite (MIT) is imported **lazily inside the
handler** — the engine's structural stack (scipy) may be absent in some MCP runtime venvs, and a
top-level import would take the whole server's module load down with it (server.py is a shared
hot-spot). On absence the handler returns the normal ``{ok:False, code, detail}`` envelope.
"""
from __future__ import annotations

import json

from mcp.server.fastmcp import FastMCP

from mcp_server.cad_tools import _dumps, _err


def _load_structure(doc_path: str):
    """Read a JSON ``aicad.structure/1`` document from disk into a StructureDoc."""
    from engine.structural.elements import StructureDoc

    with open(doc_path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    return StructureDoc.from_dict(data)


def register_structural_tools(mcp: FastMCP) -> None:
    @mcp.tool()
    def struct_analyze(doc_path: str, load_combo: str = "DEAD_LIVE") -> str:
        """Run member-force & deflection analysis on a structural document.

        Args:
            doc_path: path to a JSON ``aicad.structure/1`` document (elements + load_cases).
            load_combo: ASCE 7 combo name — ``DEAD_ONLY`` (1.4D), ``DEAD_LIVE`` (1.2D+1.6L),
                or ``DEAD_LIVE_WIND`` (1.2D+1.6L+0.5W).

        Returns JSON ``{ok, load_combo, results:[{element_id, axial_n, shear_n, moment_nmm,
        deflection_mm, dc_ratio, pass}, ...]}`` — one row per member (axial +tension, forces in N,
        moments in N·mm, deflections in mm). ``dc_ratio``/``pass`` are null until T-D3.
        """
        try:
            from engine.structural.analysis import analyze_doc
        except ImportError as e:  # scipy/PyNite not provisioned in this runtime venv
            return _dumps({
                "ok": False,
                "error": "structural solver unavailable (PyNite/scipy not installed)",
                "code": "solver_unavailable",
                "detail": str(e),
            })
        try:
            doc = _load_structure(doc_path)
            out = analyze_doc(doc, load_combo)
            return _dumps({"ok": True, "load_combo": load_combo, "results": out["results"]})
        except Exception as e:  # noqa: BLE001 - surface as a fixable result, not a transport crash
            return _err(e)

    @mcp.tool()
    def struct_check(doc_path: str, load_combo: str = "DEAD_LIVE") -> str:
        """[Day 3 / T-D3] Demand-capacity ratio + code compliance. Not yet implemented."""
        return _dumps({
            "ok": False,
            "error": "struct_check lands in T-D3 (D/C ratio + code compliance)",
            "code": "not_implemented",
        })
