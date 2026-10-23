"""Reverse Engineering MCP tools — COMPAT FACADE. Family ``reveng``
(contracts/reveng.md §8).

The tool implementations now live in reveng_session_tools + reveng_build_tools over the
shared reveng_common session/pipeline layer. This module re-exports the historical
reveng_tools namespace (_ingest_pdf, _make_pipeline_agent_fn, register_reveng_tools, ...)
so existing importers (services/dag, services/reveng/worker_rpc, tests) keep working
unchanged."""
from __future__ import annotations

from mcp.server.fastmcp import FastMCP  # noqa: F401 — register signature

from .reveng_common import (  # noqa: F401 — re-export compat facade
    _DATASHEET_NODE_STAGE,
    _SCAN_NODE_STAGE,
    _TEMPLATES_DIR,
    _autosolve_scale,
    _call_reverse_engineer,
    _dumps,
    _err,
    _extract_frames_safe,
    _find_session_run,
    _ingest_datasheet,
    _ingest_pdf,
    _launch_reveng_mission,
    _load_session,
    _load_template,
    _make_pipeline_agent_fn,
    _make_session,
    _mission_running,
    _new_session_id,
    _not_provisioned,
    _now_iso,
    _read_run_record,
    _registration_ratio,
    _reject_reveng_gate,
    _research_and_ingest,
    _research_ingest,
    _resume_reveng_mission,
    _reveng_dir,
    _reveng_runs_dir,
    _reveng_status,
    _run_record_path,
    _runs_base_dir,
    _save_session,
    _session_image_paths,
    _session_json_path,
    _session_path,
    _session_summary,
    make_reveng_emit,
    make_reveng_stage_emitter,
)

from .reveng_session_tools import register_reveng_session_tools  # noqa: F401
from .reveng_build_tools import register_reveng_build_tools  # noqa: F401


def register_reveng_tools(mcp: FastMCP) -> None:
    """Register the ``reveng`` MCP tool family (contracts/reveng.md §8)."""
    register_reveng_session_tools(mcp)
    register_reveng_build_tools(mcp)
