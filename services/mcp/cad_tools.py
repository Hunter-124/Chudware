"""Mechanical CAD + simulation MCP tools — COMPAT FACADE.

The tool implementations now live in per-family modules (cad_doc_tools, cad_eval_tools,
cad_render_tools, cad_exchange_tools, cad_sim_tools, cad_asm_tools, cad_drawing_tools)
over the shared cad_common helper layer + cad_guide topic texts. This module re-exports
the historical cad_tools namespace (helpers, state, cad_guide_text) and provides
register_cad_tools(mcp), which registers ALL cad families, so existing importers
(server.py, services/verify/battery.py, tests) keep working unchanged."""
from __future__ import annotations

from mcp.server.fastmcp import FastMCP  # noqa: F401 — register signature

from .cad_common import (  # noqa: F401 — used by tool clients and tests
    _CUR,
    _DOC_ALIASES,
    _EMPHASIS_PALETTE,
    _PERIODIC_TIMER,
    _STANDARD_ORTHO_VIEWS,
    _autosave_dir,
    _build_emphasis_palette,
    _camera_dir_for_target,
    _doc,
    _dumps,
    _err,
    _feature_is_subtractive,
    _feature_owned_matches,
    _inspect_caption,
    _normalize_view_list,
    _orbit_view_dirs,
    _parse_inspect_target,
    _publish_current,
    _resolve_alias,
    _resolve_inspect_target,
    _target_bbox,
    _target_is_internal,
    _tile_pngs_grid,
    cb,
    recover_autosaved,
    start_periodic_autosave,
)
from .cad_guide import _read_prompt, cad_guide_text  # noqa: F401

from .cad_doc_tools import register_cad_doc_tools  # noqa: F401
from .cad_eval_tools import register_cad_eval_tools  # noqa: F401
from .cad_render_tools import register_cad_render_tools  # noqa: F401
from .cad_exchange_tools import register_cad_exchange_tools  # noqa: F401
from .cad_sim_tools import register_cad_sim_tools  # noqa: F401
from .cad_asm_tools import register_cad_asm_tools  # noqa: F401
from .cad_drawing_tools import register_cad_drawing_tools  # noqa: F401


def register_cad_tools(mcp: FastMCP) -> None:
    """Attach the C++ feature-engine + simulation tools to the FastMCP server."""
    register_cad_doc_tools(mcp)
    register_cad_eval_tools(mcp)
    register_cad_render_tools(mcp)
    register_cad_exchange_tools(mcp)
    register_cad_sim_tools(mcp)
    register_cad_asm_tools(mcp)
    register_cad_drawing_tools(mcp)
