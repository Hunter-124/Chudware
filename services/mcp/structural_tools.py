"""Structural analysis MCP tools (T-D2 + T-D3) — member forces, deflections, D/C ratios.

  struct_analyze(doc_path, load_combo)  -> member forces + deflections for an aicad.structure/1 doc
  struct_check(doc_path, load_combo)    -> D/C ratio checks (axial yield + bending) per member
  struct_estimate(doc_path)             -> line-item cost estimate (ENR rate table)

All three are dispatched to the C++ core (libaicad ``struct_*`` RPCs, ``core/structural/``)
via ``core_bridge`` — the legacy Python oracle (``engine_oracle/structural/``) is no longer
imported here. The RPC params take ``doc`` (inline aicad.structure/1 object) or ``doc_path``;
results mirror these handlers' payloads minus the transport ``ok`` wrapper.

Soft solver/schema failures ride in-band at rc 0 as ``{code, message, detail}`` (see
``core/structural/structural_rpc.cpp``) and are surfaced as the normal ``{ok:False, ...}``
error envelope. A missing engine / stale lib without the ``struct_*`` dispatch raises
``CoreError`` — reported as ``solver_unavailable`` (the analogue of the old
PyNite-absent path).
"""
from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from . import core_bridge as cb
from .core_bridge import CoreError

from mcp_server.cad_tools import _dumps, _err


def _struct_rpc(method: str, params: dict) -> str:
    """One ``struct_*`` RPC wrapped in the MCP ``ok`` envelope.

    The C++ result IS the handler payload minus ``ok``; an in-band
    ``{code, message, detail}`` result is a soft failure, a ``CoreError`` means the
    engine itself is unavailable (not staged / stale lib without the dispatch).
    """
    try:
        r = cb.call(method, params)
    except CoreError as e:
        return _dumps({
            "ok": False,
            "error": f"structural solver unavailable ({e})",
            "code": "solver_unavailable",
            "detail": getattr(e, "detail", None),
        })
    except Exception as e:  # noqa: BLE001 - surface as a fixable result, not a transport crash
        return _err(e)
    if isinstance(r, dict) and r.get("code") and r.get("message"):
        return _dumps({
            "ok": False,
            "error": r["message"],
            "code": r["code"],
            "detail": r.get("detail"),
        })
    return _dumps({"ok": True, **r})


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
        moments in N·mm, deflections in mm). ``dc_ratio``/``pass`` are null here; struct_check
        fills them.
        """
        return _struct_rpc("struct_analyze", {"doc_path": doc_path, "load_combo": load_combo})

    @mcp.tool()
    def struct_check(doc_path: str, load_combo: str = "DEAD_LIVE") -> str:
        """Demand/capacity ratio check for every member in a structural document.

        Runs the ``struct_check`` RPC (C++ core, ``core/structural/``) under the requested
        ASCE 7 load combination: member-force analysis, then simplified AISC LRFD (steel)
        or NDS ASD (wood) D/C checks per member.

        Args:
            doc_path:   Path to a JSON ``aicad.structure/1`` document.
            load_combo: ASCE 7 combo — ``DEAD_ONLY``, ``DEAD_LIVE`` (default), or
                        ``DEAD_LIVE_WIND``.

        Returns
        -------
        JSON ``{ok, load_combo, disclaimer, results:[{element_id, combo, dc_ratio, passed,
        governing_limit, dc_axial, dc_bending, disclaimer}, ...]}``.

        * ``dc_ratio`` ≤ 1.0 → ``passed=true``.
        * ``governing_limit`` is ``axial_yield_LRFD`` / ``bending_yield_LRFD`` (steel) or
          ``axial_compression_ASD_NDS`` / ``bending_ASD_NDS`` (wood).
        * Every result row and the top-level response carry the mandatory disclaimer:
          "Preliminary check — not sealed engineering. Verify with a licensed structural engineer
          before construction."

        Steel check uses AISC LRFD elastic (φFy, φ=0.90); wood uses NDS ASD allowable stresses
        (Douglas Fir-Larch No.2 defaults; no load-duration / wet-service adjustments).  These are
        *preliminary estimates only* — see disclaimer.
        """
        return _struct_rpc("struct_check", {"doc_path": doc_path, "load_combo": load_combo})

    @mcp.tool()
    def struct_estimate(doc_path: str) -> str:
        """Line-item cost estimate for a structural document (T-D5).

        Runs quantity takeoff (board-feet for lumber, weight for steel, fastener count) and
        multiplies by the ENR Q4-2025 national average rate table (user-overridable).

        All costs are **preliminary budget figures from a rate table** — not vendor prices.
        Costs for vendor-sourced parts come from ``bom_price``; do not mix the two.

        Args:
            doc_path: Path to a JSON ``aicad.structure/1`` document.

        Returns JSON ``{ok, line_items:[{element_id, element_type, section_name, material,
        quantity, unit, unit_rate, material_cost, labor_hours, labor_cost, description}],
        fastener_line:{...}, total_material_cost, total_labor_cost, total_cost, currency,
        rate_table_date, disclaimer}``.

        Raises
        ------
        Returns ``{ok:False, error, code}`` when the C++ core is unavailable (libaicad not
        staged / stale lib without the ``struct_estimate`` dispatch) or on invalid doc
        path/schema.

        Note: registration is in ``structural_tools.py`` (not server.py) to keep all
        ``struct_*`` tools co-located.  This is consistent with the existing pattern —
        deviation from the task's literal "Files touched: server.py" is intentional and
        recorded in work/tasks/T-D5.md.
        """
        return _struct_rpc("struct_estimate", {"doc_path": doc_path})
