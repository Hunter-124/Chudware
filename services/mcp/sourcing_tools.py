"""Sourcing MCP tools: unified BOM roll-up + provider-resolved pricing.

Thin MCP adapter over the C++ core (libchudware ``bom_rollup`` / ``bom_price`` RPCs via
:mod:`services.mcp.core_bridge`). ``bom_rollup`` walks an assembly document (and any
linked ECAD board) into a unified made/bought BOM; ``bom_price`` additionally resolves the
bought lines' prices through the approved-provider chain (Nexar→Digi-Key→Mouser). Both return
JSON-serializable BOM lines (``aicad.sourcing/1`` §3). No scraping, no network without configured
credentials — a bought line that cannot be priced comes back with ``unit_cost``/``ext_cost`` null
(price *unknown*), never a fabricated number (contract §0).
"""
from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from services.mcp.cad_tools import _dumps, _err
from . import core_bridge as cb


def register_sourcing_tools(mcp: FastMCP) -> None:

    @mcp.tool()
    def bom_rollup(doc_path: str) -> str:
        """Roll an assembly (+ any linked ECAD board) up into a unified made/bought BOM.

        Walks the assembly document at `doc_path`, classifying each line as "made" (our geometry
        + CAM, priced later by the cost model) or "bought" (a catalog/vendor part resolved via a
        supplier provider). A linked PCB component is expanded into one bought line per grouped
        footprint from its electronic BOM. Prices are NOT resolved here — call bom_price for that.
        Returns {ok, bom: [BomLine...]} where each line follows aicad.sourcing/1 §3.
        """
        try:
            return _dumps(cb.call("bom_rollup", {"doc_path": doc_path}))
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def bom_price(doc_path: str) -> str:
        """Roll up the BOM for `doc_path` AND resolve bought-line prices via the provider chain.

        Same unified BOM as bom_rollup, then each bought line's manufacturer part number is priced
        through the approved-provider chain (Nexar→Digi-Key→Mouser): on a hit the line gains a
        resolved vendor_part plus unit_cost/ext_cost from the matching price-break tier. A line
        that cannot be priced (no credentials / no results) keeps unit_cost/ext_cost null — the
        contract's "price unknown" state, never a guessed number. Made lines are left for the cost
        model. Returns {ok, bom: [BomLine...]}.
        """
        try:
            return _dumps(cb.call("bom_price", {"doc_path": doc_path}))
        except Exception as e:  # noqa: BLE001
            return _err(e)
