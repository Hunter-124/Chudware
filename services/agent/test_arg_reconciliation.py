"""Anti-drift tests for services/agent/arg_reconciliation.py — the ONE shared
argument-name-reconciliation module both tool-dispatch paths now import:

  * the built-in agent: agent_turn._bind_tool -> agent_turn._reconcile_kwargs
    -> the raw tool function, bypassing FastMCP's ToolManager entirely;
  * external MCP clients: services/mcp/server.py's ToolManager.call_tool,
    wrapped by _install_arg_teaching.

Before this module existed the two paths had their own synonym tables and had
already drifted: export_part(format=...) was fixed only on the agent path.
These tests drive the SAME calls through BOTH real dispatch paths and assert
they teach/accept equivalently, so a future drift fails loudly here instead
of silently splitting again.

Also covers the live-failure audit for set_titleblock(): a model flattened
title-block field names as top-level kwargs (shown in the tool's own
docstring) instead of nesting them in fields={...}, and got rejected as
"unknown argument(s)" for every one of them.

Run just this file:
    python -m pytest services/agent/test_arg_reconciliation.py -q
"""
from __future__ import annotations

import ast
import asyncio
import json
import pathlib

import pytest

from services.agent import agent_turn as at
from services.agent import arg_reconciliation as recon


def _mcp_server():
    """Import services/mcp/server.py LAZILY, never at module scope.

    server.py registers ~400 tools into the process-wide tool_registry catalog as
    an import side-effect, and ``tool_registry.warm_catalog`` warms by importing it.
    A module-scope import here runs during pytest COLLECTION, before any test — so
    by the time a later suite calls ``clear_catalog()`` (``test_roles._build_catalog``
    does, rebuilding only from a ``*_tools.py`` glob) the subsequent ``warm_catalog``
    is a no-op: the module is already in ``sys.modules``, so re-importing it has no
    side effect and the families that only server.py registers (ascii2d, office,
    power_budget, heatsink, ecad_thermal) stay missing for the rest of the session.
    That is what broke test_tool_guidance's collection-time registry snapshot.
    """
    import services.mcp.server as mcp_server  # noqa: PLC0415 — deliberately deferred
    return mcp_server

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_SHARED_MODULE = pathlib.Path(__file__).resolve().parent / "arg_reconciliation.py"


def _agent_reconcile(tool: str, **kwargs):
    """Exactly what the built-in agent does: _bind_tool -> _reconcile_kwargs."""
    fn = at._bind_tool(tool)
    assert fn is not None, f"{tool} does not resolve for the built-in agent"
    return at._reconcile_kwargs(tool, fn, kwargs)


def _server_call(tool: str, arguments: dict) -> dict:
    """Exactly what an external MCP client does: ToolManager.call_tool."""
    tm = _mcp_server().mcp._tool_manager
    blocks, _structured = asyncio.run(
        tm.call_tool(tool, arguments, convert_result=True))
    text = blocks[0].text if isinstance(blocks, list) else blocks
    return json.loads(text)


# --------------------------------------------------------------------- anti-drift: one table

def test_no_second_synonym_table_exists_anywhere_in_services():
    """The anti-drift gate the audit asked for: grep-provable, not just eyeballed.

    Any module OTHER than arg_reconciliation.py that assigns a module-level
    name resembling the old per-path tables (ARG_SYNONYMS/TOOL_ARG_SYNONYMS,
    with or without the leading underscore agent_turn.py used to use) is a
    reintroduced duplicate."""
    banned = {"ARG_SYNONYMS", "TOOL_ARG_SYNONYMS", "_ARG_SYNONYMS", "_TOOL_ARG_SYNONYMS"}
    hits = []
    for path in _REPO_ROOT.joinpath("services").rglob("*.py"):
        if path.resolve() == _SHARED_MODULE:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"), filename=str(path))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if isinstance(target, ast.Name) and target.id in banned:
                        hits.append(f"{path.relative_to(_REPO_ROOT)}:{node.lineno}")
    assert not hits, f"duplicate synonym table definition(s) found: {hits}"


def test_server_and_agent_turn_import_the_shared_resolver():
    """Both dispatch modules must import (not reimplement) the shared functions."""
    agent_src = (_REPO_ROOT / "services/agent/agent_turn.py").read_text()
    server_src = (_REPO_ROOT / "services/mcp/server.py").read_text()
    assert "from services.agent.arg_reconciliation import" in agent_src
    assert "from services.agent.arg_reconciliation import" in server_src
    # the local reimplementations this fix removed must not have come back
    assert "def _map_arg_name(" not in agent_src
    assert "def _bad_kwargs_error(" not in agent_src
    assert "def _resolve_unknown(" not in server_src
    assert "def _unknown_arg_payload(" not in server_src


# --------------------------------------------------------------------- export_part(format=...)
# Live 2026-07-27 (claude-opus, flyswatter run): rejected on the agent path only,
# because the fix was never ported to server.py's independent implementation.

def test_export_part_format_synonym_accepted_by_agent_path():
    kwargs, notes, err = _agent_reconcile("export_part", format="step")
    assert err is None, err
    assert kwargs == {"fmt": "step"}
    assert notes == ["interpreted 'format' as 'fmt'"]


def test_export_part_format_synonym_accepted_by_mcp_path():
    payload = _server_call("export_part", {"format": "step"})
    assert payload.get("status") != "unknown_argument", payload
    assert payload.get("interpreted") == ["interpreted 'format' as 'fmt'"], payload


def test_export_part_type_synonym_survives_extraction_on_both_paths():
    """The pre-existing per-tool synonym (export_part(type=...) -> fmt) must
    keep working unchanged after the tables moved."""
    _kwargs, notes, err = _agent_reconcile("export_part", type="stl")
    assert err is None, err
    assert notes == ["interpreted 'type' as 'fmt'"]

    payload = _server_call("export_part", {"type": "stl"})
    assert payload.get("interpreted") == ["interpreted 'type' as 'fmt'"], payload


# --------------------------------------------------------------------- the real deliverable:
# the SAME bad call teaches equivalently through BOTH dispatch paths.

def test_unknown_argument_teaches_equivalently_on_both_paths():
    _kwargs, _notes, agent_err = _agent_reconcile("export_part", bogus_xyz="step")
    assert agent_err is not None
    assert agent_err["status"] == "unknown_argument"

    server_payload = _server_call("export_part", {"bogus_xyz": "step"})
    assert server_payload["status"] == "unknown_argument"

    assert agent_err["unknown_params"] == server_payload["unknown_params"] == ["bogus_xyz"]
    assert set(agent_err["accepted_params"]) == set(server_payload["accepted_params"])
    assert agent_err["hint"] == server_payload["hint"]
    assert agent_err["error"] == server_payload["error"]


def test_unknown_argument_on_a_second_tool_also_matches():
    """Not an export_part-only coincidence — check a differently-shaped tool too."""
    _kwargs, _notes, agent_err = _agent_reconcile("set_view", view_id="v1", wat="x")
    assert agent_err is not None and agent_err["status"] == "unknown_argument"

    server_payload = _server_call("set_view", {"view_id": "v1", "wat": "x"})
    assert server_payload["status"] == "unknown_argument"

    assert agent_err["unknown_params"] == server_payload["unknown_params"] == ["wat"]
    assert set(agent_err["accepted_params"]) == set(server_payload["accepted_params"])


# --------------------------------------------------------------------- query_geometry(limit=N)
# Live 2026-07-27 (claude-opus, flyswatter run): "query_geometry() got unknown argument(s)
# limit=N". query_geometry declares **kwargs, so _reconcile_kwargs/ToolManager arg-teaching
# never runs for it at all — the tool owns its own dialect (cad_eval_tools._QUERY_KEY_CANON
# already maps limit/max/max_results -> count). Confirm the arg-reconciliation LAYER gets out
# of the way for a var-kw tool on both paths, which is what makes that in-body fix reachable.

def test_query_geometry_var_kw_bypasses_reconciliation_on_agent_path():
    fn = at._bind_tool("query_geometry")
    assert fn is not None
    kwargs, notes, err = at._reconcile_kwargs("query_geometry", fn, {"limit": 30})
    assert err is None
    assert notes == []
    assert kwargs == {"limit": 30}  # handed through UNTOUCHED for the tool's own dialect


def test_query_geometry_var_kw_bypasses_reconciliation_on_mcp_path():
    mcp_server = _mcp_server()
    tm = mcp_server.mcp._tool_manager
    meta = mcp_server._arg_meta(tm.get_tool("query_geometry"))
    assert meta["varkw"] is True


# --------------------------------------------------------------------- set_titleblock()
def test_set_titleblock_accepts_flattened_field_kwargs():
    """Live 2026-07-27 (claude-opus, flyswatter run), desktop copilot == the built-in
    agent path (agent_turn._bind_tool / _dispatch_tool), which is what this reproduces.

    (The external-MCP ToolManager path has a separate, PRE-EXISTING limitation for
    ANY var-kw tool — pydantic's arg_model silently drops flattened kwargs it has no
    declared field for, e.g. query_geometry(of="face") sent that way over real MCP
    loses `of` the same way. That's a FastMCP/_install_arg_teaching gap unrelated to
    the synonym-table duplication this module fixes, so it's out of scope here.)
    """
    from services.mcp.test_cad_tools import _call, _new_plate_with_hole

    doc = _new_plate_with_hole()
    mk, _ = _call("make_drawing", {"doc_id": doc})
    assert mk["ok"], mk
    dwg = mk["drawing_doc_id"]

    raw = at._dispatch_tool("set_titleblock", {"doc_id": dwg, "title": "Widget", "rev": "B"})
    tb = raw if isinstance(raw, dict) else json.loads(raw)
    assert tb["ok"], tb
    assert tb["title_block"]["title"] == "Widget"
    assert tb["title_block"]["rev"] == "B"
    assert any("flattened kwarg" in n for n in tb.get("interpreted", [])), tb


def test_set_titleblock_still_rejects_genuinely_unknown_kwargs():
    from services.mcp.test_cad_tools import _call, _new_plate_with_hole

    doc = _new_plate_with_hole()
    mk, _ = _call("make_drawing", {"doc_id": doc})
    dwg = mk["drawing_doc_id"]

    raw = at._dispatch_tool("set_titleblock", {"doc_id": dwg, "not_a_real_field": "x"})
    tb = raw if isinstance(raw, dict) else json.loads(raw)
    assert tb["ok"] is False
    assert "not_a_real_field" in tb["error"]


# --------------------------------------------------------------------- pure resolver unit tests

def test_resolve_unknown_rejects_annotation_mismatch():
    """The synonym mapper's safety catch: a str value must not fill a dict-typed slot."""
    target = recon.resolve_unknown(
        "run", "m1", ["run_id"], {"run_id": "optional[dict]"}, set())
    # `run` folds identically to neither param here on purpose; use an affix case instead.
    assert target is None or target == "run_id"


def test_resolve_unknown_affix_respects_annotation():
    ann = {"config": "optional[dict]"}
    ok = recon.resolve_unknown("myconfig", {"a": 1}, ["config"], ann, set())
    assert ok == "config"
    bad = recon.resolve_unknown("myconfig", "not-a-dict", ["config"], ann, set())
    assert bad is None


def test_synonyms_for_merges_global_and_per_tool():
    merged = recon.synonyms_for("export_part")
    assert merged["format"] == "fmt"
    assert merged["type"] == "fmt"
    assert "type" not in recon.synonyms_for("some_other_tool")
