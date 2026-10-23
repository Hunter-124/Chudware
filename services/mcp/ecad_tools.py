"""ECAD MCP tools — COMPAT FACADE.

The tool implementations now live in per-family modules (ecad_sch_tools, ecad_pcb_tools,
ecad_sim_tools) over the shared ecad_common session/helper layer. This module re-exports
the historical ecad_tools namespace (_ECAD, _get_pcb, register_ecad_tools, autosave
entry points, ...) so existing importers (server.py, ecad_thermal, ecad_reliability,
scripts, tests) keep working unchanged."""
from __future__ import annotations

from mcp.server.fastmcp import FastMCP  # noqa: F401 — register signature

from .ecad_common import (  # noqa: F401 — re-export compat facade
    OpError,
    _ECAD,
    _ECAD_HEAVY_BUDGET_S,
    _ECAD_SESSION_ID,
    _PROVIDER_PLAN_BUSY,
    _PROVIDER_PLAN_LOCK,
    _active_pcb_id,
    _active_sch_id,
    _apply,
    _apply_sim_source_params,
    _attach_board_ascii_readback,
    _attach_schematic_ascii_readback,
    _autosave_all_open_ecad,
    _budget_timeout_err,
    _build_analysis,
    _component_eng,
    _default_out_dir,
    _default_outline_shapes,
    _dumps,
    _eng,
    _eng_metadata,
    _erc_lines,
    _erc_summary,
    _err,
    _finite,
    _fp_overrides,
    _get_pcb,
    _get_sch,
    _net_size,
    _outline_bbox,
    _parts_extents,
    _placed_pins,
    _power_totals,
    _pretty_pin,
    _provider_reachable,
    _resolve_ref_or_id,
    _round,
    _round_sig,
    _rs,
    _run_with_budget,
    _simulate_advanced,
    _summarize_ac,
    _summarize_sweep,
    _summarize_sweep_steps,
    _summarize_tran,
    _sym_by_ref,
    _threading,
    _unconnected_pins,
    _uuid_ecad,
    _zone_polygon,
    ecad_session,
    enable_ecad_autosave,
    lib_search,
    netlist_mod,
    recover_ecad_autosaved,
    schema,
)

from .ecad_sch_tools import register_ecad_sch_tools  # noqa: F401
from .ecad_pcb_tools import register_ecad_pcb_tools  # noqa: F401
from .ecad_sim_tools import register_ecad_sim_tools  # noqa: F401


def register_ecad_tools(mcp: FastMCP) -> None:
    """Register every ECAD tool on the given FastMCP instance."""
    register_ecad_sch_tools(mcp)
    register_ecad_pcb_tools(mcp)
    register_ecad_sim_tools(mcp)
