"""Cost-out MCP tools: made-part cost estimate + on-demand manufacturing quotes (workstream B).

Thin MCP adapters over :mod:`engine.cost_model` and :mod:`engine.quote_providers`.

- ``cam_cost_estimate(doc_path)`` opens a doc that carries CAM operations, reads its solid volume,
  CAM setup material, and simulated ``cycle_time_s``, and returns the ``aicad.sourcing/1`` §3
  ``cost_model`` object (material + machining + amortized setup). A missing input degrades to the
  §0 contract shape (``{"ok": false, "reason", "hint", "unit_cost": null}``) — never a guess.
- ``request_quote(doc_path, vendor, quantity)`` exports the geometry (STEP for mechanical, the
  Gerber bundle for PCB) and dispatches to the approved instant-quote API (Xometry / JLCPCB). With
  no API key it degrades gracefully (§0) without a network call; with a key it returns a real §4
  ``QuoteResult``.

Registered onto the FastMCP instance by ``register_cost_tools(mcp)``. No fabricated prices, no
scraping, keys are configuration only (contracts/sourcing_schema.md §0/§4).
"""
from __future__ import annotations

import os
import tempfile
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP

from mcp_server.cad_tools import _dumps, _err
from mcp_server import core_bridge as cb


def _cam_setup_material(tree: Any) -> Optional[str]:
    """Best-effort material from the first CAM setup's stock (``setup.stock.material``)."""
    if not isinstance(tree, dict):
        return None
    setups = tree.get("setups")
    if isinstance(setups, list):
        for s in setups:
            if not isinstance(s, dict):
                continue
            stock = s.get("stock")
            mat = stock.get("material") if isinstance(stock, dict) else None
            mat = mat or s.get("material")
            if mat:
                return str(mat)
    return None


def _total_cycle_time_s(sim: Any) -> Optional[float]:
    """Sum ``cycle_time_s`` across all simulated setups; ``None`` if the sim carried none."""
    if not isinstance(sim, dict):
        return None
    results = sim.get("results")
    if not isinstance(results, list):
        return None
    total = 0.0
    seen = False
    for r in results:
        if isinstance(r, dict) and isinstance(r.get("cycle_time_s"), (int, float)):
            total += float(r["cycle_time_s"])
            seen = True
    return total if seen else None


def register_cost_tools(mcp: FastMCP) -> None:
    """Attach the cost-estimate + quote-out tools to the FastMCP server."""

    @mcp.tool()
    def cam_cost_estimate(doc_path: str, material: Optional[str] = None,
                          quantity: int = 1, machine_rate_per_hr: Optional[float] = None) -> str:
        """Estimate the made-part cost of a doc that has CAM operations.

        Opens `doc_path`, reads its solid volume, the CAM setup's material (override with
        `material`), and the simulated total `cycle_time_s`, then returns the aicad.sourcing/1 §3
        cost_model object: material_cost + machine_cost + amortized setup_cost -> unit_cost, with a
        provenance block (rate $/kg, density, citation). Rates come from a seeded, config-overridable
        table — an unknown material or a missing input degrades to {"ok": false, "reason", "hint",
        "unit_cost": null} (price unknown), never a fabricated number. Returns {ok, cost}.
        """
        try:
            from engine import cost_model
            opened = cb.call("doc_open", {"path": str(doc_path)})
            doc_id = opened.get("doc_id") if isinstance(opened, dict) else None
            volume = opened.get("volume") if isinstance(opened, dict) else None
            volume_mm3 = float(volume) if isinstance(volume, (int, float)) else None

            cycle_time_s: Optional[float] = None
            mat = material
            if doc_id:
                try:
                    sim = cb.call("cam_simulate", {"doc_id": doc_id})
                    cycle_time_s = _total_cycle_time_s(sim)
                except Exception:  # noqa: BLE001 (no CAM ops / sim unavailable -> unknown input)
                    cycle_time_s = None
                if mat is None:
                    try:
                        mat = _cam_setup_material(cb.call("cam_tree", {"doc_id": doc_id}))
                    except Exception:  # noqa: BLE001
                        mat = None

            cost = cost_model.estimate_made_cost(
                volume_mm3=volume_mm3, material=mat, cycle_time_s=cycle_time_s,
                quantity=quantity, machine_rate_per_hr=machine_rate_per_hr,
            )
            return _dumps({"ok": bool(cost.get("ok")), "cost": cost})
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def request_quote(doc_path: str, vendor: str, quantity: int = 1,
                      material: Optional[str] = None, layers: int = 2,
                      finish: Optional[str] = None) -> str:
        """Request an on-demand manufacturing quote for a doc from an approved supplier API.

        `vendor`: "xometry" (mechanical — the part is exported to STEP) or "jlcpcb" (PCB — the doc
        path is the Gerber bundle). Dispatches to the approved instant-quote API using a config-only
        key (quote_providers.<vendor>.api_key, or XOMETRY_API_KEY / JLCPCB_KEY). With NO key it
        returns the contract §0 shape {"ok": false, "reason", "hint"} without any network call; with
        a key it returns a real aicad.sourcing/1 §4 QuoteResult (unit_price/ext_price/lead_time/
        price_breaks/quote_id). No scraping. Returns {ok, quote}.
        """
        try:
            from engine import quote_providers
            part_file = str(doc_path)
            v = str(vendor or "").strip().lower()
            if v in ("xometry", "xom", "cnc", "mechanical"):
                # Export the geometry to STEP for a mechanical quote (best-effort; a failure still
                # lets the provider degrade gracefully on a missing key).
                try:
                    opened = cb.call("doc_open", {"path": str(doc_path)})
                    doc_id = opened.get("doc_id") if isinstance(opened, dict) else None
                    if doc_id:
                        step = os.path.join(tempfile.gettempdir(),
                                            f"aicad_quote_{os.getpid()}.step")
                        cb.call("export_file", {"doc_id": doc_id, "path": step, "format": "step"})
                        part_file = step
                except Exception:  # noqa: BLE001 (export unavailable -> pass the doc path through)
                    part_file = str(doc_path)
            quote = quote_providers.request_quote(
                part_file, vendor, quantity=quantity, material=material,
                layers=layers, finish=finish,
            )
            return _dumps({"ok": bool(quote.get("ok")), "quote": quote})
        except Exception as e:  # noqa: BLE001
            return _err(e)
