"""Structural analysis MCP tools — member forces, deflections, D/C ratios.

  struct_analyze(doc_path, load_combo)          -> member forces, deflections, reactions
  struct_check(doc_path, load_combo)            -> strength + deflection utilization
  struct_estimate(doc_path, rates_path="")      -> completeness-aware cost estimate

All three are dispatched to the C++ core (libchudware ``struct_*`` RPCs, ``core/structural/``)
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

from services.mcp.cad_tools import _dumps, _err


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
    if not isinstance(r, dict):
        return _dumps({"ok": False, "error": f"{method} returned a non-object payload"})
    if method in ("struct_check", "struct_estimate"):
        # ``complete`` is the core's authoritative coverage verdict.  Keep every
        # partial row/total and its reason, but do not let a transport-level wrapper
        # turn unavailable checks or a partial estimate into a successful result.
        return _dumps({**r, "ok": r.get("complete") is True})
    return _dumps({"ok": True, **r})


def register_structural_tools(mcp: FastMCP) -> None:
    @mcp.tool()
    def struct_analyze(doc_path: str, load_combo: str = "DEAD_LIVE") -> str:
        """Run member-force and deflection analysis on a structural document.

        Args:
            doc_path: Path to a JSON ``aicad.structure/1`` document.
            load_combo: ``DEAD_ONLY`` (1.4D), ``DEAD_LIVE`` (1.2D+1.6L), or
                ``DEAD_LIVE_WIND`` (1.2D+1.6L+0.5W).

        Returns JSON ``{ok, load_combo, units, results:[...], reactions:[...]}`` with
        legacy governing force/deflection channels plus additive local-axis components
        and support reactions. Forces are N, moments N·mm, and deflections mm.
        ``dc_ratio``/``pass`` stay null here.
        """
        return _struct_rpc("struct_analyze", {"doc_path": doc_path, "load_combo": load_combo})

    @mcp.tool()
    def struct_check(doc_path: str, load_combo: str = "DEAD_LIVE") -> str:
        """Return preliminary strength and serviceability utilization by member.

        The C++ core evaluates AISC LRFD (steel) or NDS ASD (wood)
        axial/biaxial-bending interaction and an explicitly reported total-load L/240
        deflection check. Unsupported checks, such as a footing without soil and
        reinforcement inputs, return ``status:"unavailable"`` and ``passed:false``.

        Args:
            doc_path: Path to a JSON ``aicad.structure/1`` document.
            load_combo: ``DEAD_ONLY``, ``DEAD_LIVE`` (default), or
                ``DEAD_LIVE_WIND``.

        Returns JSON ``{ok, load_combo, units, complete, disclaimer, results:[...]}``.
        ``complete`` is false, and therefore common ``ok`` is false, when any requested
        capacity check is unavailable. Partial results and their reasons are retained.
        Results are preliminary and retain the mandatory licensed-engineer disclaimer.
        """
        return _struct_rpc("struct_check", {"doc_path": doc_path, "load_combo": load_combo})

    @mcp.tool()
    def struct_estimate(doc_path: str, rates_path: str = "") -> str:
        """Estimate structural material and installation costs from a dated rate table.

        Quantities are board-feet for lumber, net-section weight for steel, concrete
        volume for footings, and explicit fastener assumptions. Costs are preliminary
        budget figures, not vendor prices.

        Args:
            doc_path: Path to a JSON ``aicad.structure/1`` document.
            rates_path: Optional JSON rate-table override. Unreadable, malformed,
                negative, or non-finite overrides fail rather than silently falling back.

        Returns JSON ``{ok, complete, total_is_partial, unestimated_element_ids,
        line_items, fastener_line, total_material_cost, total_labor_cost, total_cost,
        currency, rate_table_date, disclaimer}``. Common ``ok`` follows ``complete``;
        incomplete estimates retain useful partial line items, totals, and reasons.
        """
        return _struct_rpc("struct_estimate", {"doc_path": doc_path, "rates_path": rates_path})
