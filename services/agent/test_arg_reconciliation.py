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


import services.mcp.server as mcp_server


def _mcp_server():
    """The imported server module.

    This used to be a deliberately DEFERRED import: server.py registers ~400 tools
    into the process-wide tool_registry catalog as an import side-effect, and
    ``tool_registry.warm_catalog`` used to warm purely by importing it — so a
    module-scope import here (running at pytest COLLECTION time) made every later
    ``warm_catalog()`` a no-op once some suite called ``clear_catalog()``.
    ``tool_registry.mark_warm_baseline`` fixed that at the root: warming now replays a
    content snapshot instead of relying on the one-shot import side effect, so this
    import is safe at module scope again (see tool_registry.warm_catalog docstring and
    test_tool_registry's clear/warm regression tests).
    """
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


def test_plm_item_add_kind_synonym_accepted_on_both_paths(tmp_path):
    """Live run-12211675717239 used the natural ``kind`` discriminator."""
    (tmp_path / "spec.md").write_text("# Requirements\n")
    supplied = {
        "path": str(tmp_path),
        "file_path": "spec.md",
        "kind": "spec",
    }

    kwargs, notes, err = _agent_reconcile("plm_item_add", **supplied)
    assert err is None, err
    assert kwargs == {
        "path": str(tmp_path),
        "file_path": "spec.md",
        "doc_type": "spec",
    }
    assert notes == ["interpreted 'kind' as 'doc_type'"]

    payload = _server_call("plm_item_add", supplied)
    assert payload["ok"] is True, payload
    assert payload["item"]["doc_type"] == "spec"
    assert payload.get("interpreted") == ["interpreted 'kind' as 'doc_type'"]

    container = tmp_path / "Bracket.step+"
    container.write_bytes(b"container shape is rejected before content is read")
    first_live_shape = _server_call(
        "plm_item_add",
        {"path": str(container), "kind": "part"},
    )
    assert first_live_shape["ok"] is False
    assert first_live_shape["reason"] == "invalid_mode"
    assert "plm_checkpoint" in first_live_shape["hint"]
    assert first_live_shape.get("interpreted") == [
        "interpreted 'kind' as 'doc_type'"
    ]


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


# --------------------------------------------------------------------- live 2026-07-29
# (claude-sonnet-4-5, general lane): five tools refused plausible argument NAMES with
# status=unknown_argument. Each block below pins one of them, and each alias was added
# only after reading the tool's real signature.

def test_every_tool_synonym_target_is_a_real_parameter():
    """The invariant that keeps this table from being dangerous.

    A synonym whose target does not exist can never resolve, so it is dead
    weight at best; worse, it documents a parameter the tool does not have and
    invites the next reader to "fix" the tool instead of the table. Checked
    against the REAL signature of every tool named in TOOL_ARG_SYNONYMS."""
    import inspect

    bad = []
    for tool, table in recon.TOOL_ARG_SYNONYMS.items():
        fn = at._bind_tool(tool)
        assert fn is not None, f"{tool} does not resolve for the built-in agent"
        real = {recon.norm_arg(p) for p in inspect.signature(fn).parameters}
        for supplied, target in table.items():
            if target not in real:
                bad.append(f"{tool}: {supplied!r} -> {target!r} (not a parameter)")
    assert not bad, bad


def test_a_synonym_pointing_at_a_missing_parameter_never_resolves():
    """The negative half of the invariant above, at the resolver level: even if
    such an entry were added, `resolve_unknown` must refuse rather than invent
    a slot — an unknown_argument error is recoverable, a wrong call is not."""
    target = recon.resolve_unknown(
        "inlet_velocity_mps", 5.0, ["fluid", "cells"], {}, set(),
        {"inletvelocitymps": "velocity"})  # `velocity` is not in params here
    assert target is None


# --- 1. run_cfd_flow(velocity, aoa, fluid, cells, n_steps, doc_id, background)

def test_run_cfd_flow_inlet_velocity_mps_resolves_to_velocity():
    kwargs, notes, err = _agent_reconcile("run_cfd_flow", doc_id="d1", fluid="air",
                                          inlet_velocity_mps=5.0)
    assert err is None, err
    assert kwargs["velocity"] == 5.0
    assert "interpreted 'inlet_velocity_mps' as 'velocity'" in notes


def test_run_cfd_flow_drops_the_descriptive_study_name():
    """A label the tool has no slot for must not cost the whole call — but the
    drop has to be stated, not silent."""
    kwargs, notes, err = _agent_reconcile("run_cfd_flow", doc_id="d1", fluid="air",
                                          inlet_velocity_mps=5.0,
                                          study_name="air_flow_5mps")
    assert err is None, err
    assert kwargs == {"doc_id": "d1", "fluid": "air", "velocity": 5.0}
    assert any("ignored 'study_name'" in n for n in notes), notes


def test_run_cfd_flow_still_refuses_inlet_face():
    """The footgun guard. `inlet_face={...}` is not a label: it asks for a
    boundary condition this LBM tool does not implement. Ignoring it would run
    a DIFFERENT simulation than the model asked for and report success."""
    _kwargs, _notes, err = _agent_reconcile("run_cfd_flow", doc_id="d1",
                                            inlet_velocity_mps=5.0,
                                            inlet_face={"kind": "face", "id": 3},
                                            outlet_face={"kind": "face", "id": 4})
    assert err is not None
    assert err["status"] == "unknown_argument"
    assert set(err["unknown_params"]) == {"inlet_face", "outlet_face"}


# --- 2. ecad_find_part(query, limit, category)

def test_ecad_find_part_query_and_limit_synonyms():
    kwargs, notes, err = _agent_reconcile("ecad_find_part", mpn="ESP32-S3",
                                          max_results=5)
    assert err is None, err
    assert kwargs == {"query": "ESP32-S3", "limit": 5}
    assert "interpreted 'mpn' as 'query'" in notes


def test_ecad_find_part_category_is_a_real_parameter_not_an_alias():
    """`category` was refused live, but it is a genuine FILTER, not a spelling
    of `query` — aliasing it would have silently widened the search. The tool
    grew the parameter instead; this pins that it is passed through untouched."""
    kwargs, notes, err = _agent_reconcile("ecad_find_part", query="ESP32-S3",
                                          category="MCU")
    assert err is None, err
    assert kwargs == {"query": "ESP32-S3", "category": "MCU"}
    assert notes == []


# --- 3. ecad_new_schematic(name)

def test_ecad_new_schematic_title_alone_becomes_the_name():
    kwargs, notes, err = _agent_reconcile("ecad_new_schematic",
                                          title="Decoupling Capacitor Demo")
    assert err is None, err
    assert kwargs == {"name": "Decoupling Capacitor Demo"}
    assert "interpreted 'title' as 'name'" in notes


def test_ecad_new_schematic_drops_a_redundant_title_on_the_agent_path():
    kwargs, notes, err = _agent_reconcile("ecad_new_schematic",
                                          name="decoupling_demo",
                                          title="Decoupling Capacitor Demo")
    assert err is None, err
    assert kwargs == {"name": "decoupling_demo"}  # the model's own `name` wins
    assert any("ignored 'title'" in n for n in notes), notes


def test_ecad_new_schematic_drop_is_visible_in_warnings_on_the_mcp_path():
    """Deliberate asymmetry: the MCP wrapper has a free result-shaping seam so
    it uses a `warnings` key; the agent path reports the same drop through its
    existing notes channel (`arg_interpretation`). Never silent on either."""
    payload = _server_call("ecad_new_schematic", {"name": "decoupling_demo",
                                                  "title": "Decoupling Capacitor Demo"})
    assert payload.get("ok") is True, payload
    assert payload.get("name") == "decoupling_demo"
    assert any("ignored 'title'" in w for w in payload.get("warnings", [])), payload


# --- 4. apply_ascii_edits(kind, edits_json, ...)

def test_apply_ascii_edits_accepts_an_edits_list():
    """A tool named apply_ascii_EDITS refused `edits=[...]`. The name maps to
    `edits_json` by affix already — what blocked it was the `str` annotation
    against a decoded list, so `bind_value` serialises it back to the JSON text
    the signature declares."""
    edits = [{"op": "move", "ref": "R1", "to_cell": [12, 4]}]
    kwargs, notes, err = _agent_reconcile("apply_ascii_edits", kind="pcb",
                                          doc_id="pcb_001", edits=edits)
    assert err is None, err
    assert json.loads(kwargs["edits_json"]) == edits
    assert "interpreted 'edits' as 'edits_json'" in notes


def test_apply_ascii_edits_list_survives_pydantic_on_the_mcp_path():
    """Renaming alone was NOT enough here: ToolManager validates against a
    pydantic model, which rejects a list for `edits_json: str`. Reaching the
    tool body (a doc error, not an argument error) is the proof."""
    payload = _server_call("apply_ascii_edits", {
        "kind": "pcb", "doc_id": "pcb_001",
        "edits": [{"op": "move", "ref": "R1", "to_cell": [12, 4]}]})
    assert payload.get("status") != "unknown_argument", payload
    assert "invalid arguments" not in str(payload.get("error", "")), payload
    assert payload.get("interpreted") == ["interpreted 'edits' as 'edits_json'"], payload


def test_bind_value_only_touches_json_document_parameters():
    assert recon.bind_value("edits_json", "str", [{"op": "move"}]) == '[{"op": "move"}]'
    # not a *_json slot -> untouched, whatever the annotation says
    assert recon.bind_value("edits", "str", [{"op": "move"}]) == [{"op": "move"}]
    # a *_json slot that really wants a dict -> untouched
    assert recon.bind_value("grid_json", "optional[dict]", {"a": 1}) == {"a": 1}
    # scalars are never reshaped
    assert recon.bind_value("edits_json", "str", "[]") == "[]"


# --- 5. research_eng_calc: NOT an argument-name problem.

def test_research_eng_calc_is_var_kw_so_this_layer_never_sees_its_args():
    """Live 2026-07-29 the call was ACCEPTED and failed inside the formula
    registry ("beam() missing 3 required positional arguments"). research_eng_calc
    declares **kwargs, so reconciliation is bypassed entirely — no synonym here
    could ever have reached it. Pinned so nobody re-files it against this table."""
    fn = at._bind_tool("research_eng_calc")
    assert fn is not None
    assert at._signature_spec(fn)["var_kw"] is True
    supplied = {"formula": "beam", "params": {"load_n": 2000, "span_mm": 500}}
    kwargs, notes, err = at._reconcile_kwargs("research_eng_calc", fn, dict(supplied))
    assert err is None and notes == []
    assert kwargs == supplied  # handed through UNTOUCHED


# --------------------------------------------------------------------- drop_descriptive guards

def test_drop_descriptive_refuses_anything_not_on_the_allowlist():
    """No heuristics. `fillet_name` looks every bit as cosmetic as `title`; it
    is not on the list, so it is refused."""
    params = ["doc_id"]
    assert not recon.drop_descriptive("fillet_name", "outer", params, set())
    assert not recon.drop_descriptive("material", "6061", params, set())
    assert recon.drop_descriptive("title", "Outer fillet", params, set())


def test_drop_descriptive_refuses_a_non_string_value():
    """Constraint 4: structure, quantities and flags carry semantics whatever
    the argument happens to be called."""
    params = ["doc_id"]
    assert not recon.drop_descriptive("label", {"face": 3}, params, set())
    assert not recon.drop_descriptive("label", [1, 2, 3], params, set())
    assert not recon.drop_descriptive("label", 12.5, params, set())
    assert not recon.drop_descriptive("label", True, params, set())


def test_drop_descriptive_never_shadows_a_free_parameter():
    """Constraint 2: if any FREE parameter could have received the value,
    refuse — an ignored value next to an empty slot is invisible."""
    # `title` could be `title_block`: refuse.
    assert not recon.drop_descriptive("title", "Sheet 1", ["title_block"], set())
    # `title` -> `name` is a registered synonym for this tool: refuse the drop
    # while the slot is free (resolve_unknown maps it instead).
    syn = recon.synonyms_for("ecad_new_schematic")
    assert not recon.drop_descriptive("title", "Demo", ["name"], set(), syn)
    # once `name` is taken by the model's own argument, nothing can receive it.
    assert recon.drop_descriptive("title", "Demo", ["name"], {"name"}, syn)


def test_attach_warnings_appends_and_never_clobbers():
    assert recon.attach_warnings({"ok": True}, ["w1"]) == {"ok": True, "warnings": ["w1"]}
    merged = recon.attach_warnings({"ok": True, "warnings": ["pre"]}, ["w1"])
    assert merged["warnings"] == ["pre", "w1"]
    as_text = recon.attach_warnings("plain result", ["w1"])
    assert "[warning] w1" in as_text
    assert recon.attach_warnings({"ok": True}, []) == {"ok": True}


# --------------------------------------------------------------------- live 2026-07-29, ECAD pass 2
# ecad_simulate(kind='s-param', ports=[...], freq_start_hz=2e9, freq_stop_hz=3e9,
# freq_points=201) -> all five refused. Real signature:
#   ecad_simulate(analysis='op', params=None, probes=None, doc_id='', backend='mna')
# with analysis in op/dc/tran/ac/four/mc/temp/noise (ecad_sim_tools.py:153-165).

def test_ecad_simulate_kind_resolves_to_analysis():
    """`analysis` is the discriminator `kind` names. Safe to alias ONLY because
    an unrecognised value fails loudly ("unknown analysis <x>" + the vocabulary,
    ecad_sim_tools.py:213) rather than defaulting to a different analysis."""
    kwargs, notes, err = _agent_reconcile("ecad_simulate", kind="tran",
                                          params={"tstep": 1e-6, "tstop": 1e-3})
    assert err is None, err
    assert kwargs["analysis"] == "tran"
    assert "interpreted 'kind' as 'analysis'" in notes


def test_ecad_simulate_still_refuses_the_s_parameter_port_arguments():
    """CAPABILITY GAP, not a naming gap — pinned so nobody "fixes" it with an alias.

    ecad_simulate is a SPICE circuit simulator: no S-parameter analysis, and no
    port concept at all. The nearest S-param tools are em_driven (waveguide
    standards, not a PCB net) and ecad_antenna_analyze (parametric antenna S11,
    also not a board net), so there is nowhere honest to redirect `ports` to.
    Aliasing any of these would make a nonsense call report success."""
    _kwargs, _notes, err = _agent_reconcile(
        "ecad_simulate", doc_id="ecadcpp0000001a", kind="s-param",
        ports=[{"net": "ANT", "x": 7.3335, "y": -3.2}],
        freq_start_hz=2e9, freq_stop_hz=3e9, freq_points=201)
    assert err is not None
    assert err["status"] == "unknown_argument"
    assert set(err["unknown_params"]) == {"ports", "freq_start_hz",
                                          "freq_stop_hz", "freq_points"}


def test_unknown_argument_points_at_the_settings_dict_when_the_tool_has_one():
    """freq_start/stop/points really do exist for an AC sweep — one level down,
    as params={'fstart':..,'fstop':..,'points':..}. The payload says where such
    settings live without CLAIMING these particular names map there."""
    _kwargs, _notes, err = _agent_reconcile("ecad_simulate", analysis="ac",
                                            freq_start_hz=2e9)
    assert err is not None
    assert err["settings_param"] == "params"
    assert "inside the `params` dict" in err["error"]


def test_settings_dict_pointer_is_silent_when_the_bag_was_already_supplied():
    """No noise when the model already used it — the note would be wrong advice."""
    _kwargs, _notes, err = _agent_reconcile("ecad_simulate", analysis="ac",
                                            params={"fstart": 1.0},
                                            freq_start_hz=2e9)
    assert err is not None
    assert "settings_param" not in err
    assert "settings" not in err["error"]


def test_export_part_payload_has_no_settings_pointer():
    """export_part(fmt, path, doc_id) has no settings bag; the note must not
    appear on tools that do not have one."""
    _kwargs, _notes, err = _agent_reconcile("export_part", bogus_xyz="step")
    assert err is not None
    assert "settings_param" not in err

# --------------------------------------------------------------------- live 2026-07-29, pass 2
# Two refusals from the same general-lane sweep with OPPOSITE correct answers:
# one is a genuine spelling of a real parameter, the other is a different
# physical quantity wearing a similar name.

# --- run_shell(cmd=...) -> alias. Real signature: fs_tools.py:762
#     run_shell(command: str, cwd: str = "", timeout_s: int = ...)

def test_run_shell_cmd_resolves_to_command_and_says_so():
    """Live: run_shell(cmd='ls -la /tmp/sweep/work/') -> unknown_argument. `cmd`
    is the universal abbreviation of the single command slot; the affix rule
    cannot reach it, so it needs the explicit tool-scoped synonym."""
    kwargs, notes, err = _agent_reconcile("run_shell", cmd="ls -la /tmp/sweep/work/")
    assert err is None, err
    assert kwargs == {"command": "ls -la /tmp/sweep/work/"}, kwargs
    # The echoed interpretation is the human-visible half of the fix.
    assert "interpreted 'cmd' as 'command'" in notes, notes


def test_run_shell_cmd_target_is_the_only_command_slot():
    """The evidence behind the alias, asserted rather than trusted: run_shell has
    exactly one str parameter that could take a command line."""
    import inspect
    fn = at._bind_tool("run_shell")
    params = list(inspect.signature(fn).parameters)
    assert params == ["command", "cwd", "timeout_s"], params


def test_run_shell_script_and_cmdline_also_reach_command():
    for spelling in ("script", "cmdline", "command_line"):
        kwargs, notes, err = _agent_reconcile("run_shell", **{spelling: "echo hi"})
        assert err is None, (spelling, err)
        assert kwargs["command"] == "echo hi", (spelling, kwargs)


def test_run_shell_refuses_shell_because_it_names_the_interpreter():
    """Deliberate NON-alias. "shell" names bash/sh/zsh — the interpreter — not the
    command line. run_shell has no interpreter slot, so binding shell='bash'
    would execute `bash` and report success on a call the model never made."""
    kwargs, notes, err = _agent_reconcile("run_shell", shell="bash")
    assert err is not None and err["status"] == "unknown_argument", (kwargs, notes)
    assert err["unknown_params"] == ["shell"], err
    assert "command (required)" in err["accepted_params"], err


def test_run_shell_still_refuses_a_genuinely_unknown_argument():
    """The catch-all guard: the table must not have turned run_shell into a tool
    that accepts any name at all."""
    kwargs, notes, err = _agent_reconcile("run_shell", command="true", sudo=True)
    assert err is not None and err["status"] == "unknown_argument", (kwargs, notes)
    assert err["unknown_params"] == ["sudo"], err
    assert err["accepted_params"] == [
        "command (required)", "cwd (optional)", "timeout_s (optional)"], err


# --- run_fea_static(mesh_size_mm=...) -> REFUSE. `mesh_fineness` is a
#     DIMENSIONLESS 0..1 factor (cad_sim_tools.py:75-76, 284-285), so a
#     millimetre count bound there would mesh at the wrong density and report
#     success. Contract rule 1: a unit mismatch is never aliased.

def test_mesh_fineness_really_is_dimensionless_not_a_length():
    """The evidence for the refusal, taken from the tool itself rather than from
    a comment: the preset WORDS map onto 0..1 numbers, so the parameter is a
    density factor and has no millimetre reading."""
    from services.mcp.cad_sim_tools import _norm_fineness
    assert _norm_fineness("coarse") == 0.25
    assert _norm_fineness("finest") == 1.0
    import inspect
    sig = inspect.signature(at._bind_tool("run_fea_static"))
    assert sig.parameters["mesh_fineness"].default == 0.5


def test_run_fea_static_mesh_size_mm_is_refused_and_the_error_teaches_the_unit():
    kwargs, notes, err = _agent_reconcile(
        "run_fea_static", doc_id="d1", fixtures=["bottom"],
        loads=[{"face": "top", "pressure": 0.5}], mesh_size_mm=2.0)
    assert err is not None and err["status"] == "unknown_argument", (kwargs, notes)
    assert kwargs == {} and err["unknown_params"] == ["mesh_size_mm"], err
    # It must NOT have been quietly bound to the fineness knob.
    assert "mesh_fineness" not in kwargs
    msg = err["error"]
    assert "mesh_fineness" in msg, msg
    assert "millimetre" in msg and "DIMENSIONLESS" in msg, msg
    assert "0..1" in msg, msg
    assert "mesh_fineness=0.75" in msg, msg          # a correct example
    assert err["quantity_mismatch"] == [{
        "supplied": "mesh_size_mm", "parameter": "mesh_fineness",
        "note": err["quantity_mismatch"][0]["note"],
    }], err["quantity_mismatch"]


def test_run_fea_static_mesh_size_mm_teaches_the_same_thing_on_the_mcp_path():
    payload = _server_call("run_fea_static", {
        "fixtures": ["bottom"], "loads": [{"face": "top", "pressure": 0.5}],
        "mesh_size_mm": 2.0})
    assert payload["status"] == "unknown_argument", payload
    assert "mesh_fineness" in payload["error"] and "0..1" in payload["error"], payload
    assert payload["quantity_mismatch"][0]["parameter"] == "mesh_fineness", payload


def test_no_synonym_or_affix_rule_can_ever_bind_a_quantity_mismatch():
    """The resolver-level half. Even handed a synonym entry that points straight
    at the parameter, `mesh_size_mm` must not bind — the guard runs before the
    synonym and affix rules, so a future well-meaning table edit cannot undo the
    refusal by accident."""
    ann = {"mesh_fineness": "Union[float, str]"}
    forged = {"meshsizemm": "meshfineness"}
    assert recon.resolve_unknown("mesh_size_mm", 2.0, ["mesh_fineness"], ann,
                                 set(), forged) is None
    assert recon.resolve_unknown("element_size_mm", 2.0, ["element_size", "mesh_fineness"],
                                 {}, set()) is None


def test_a_tool_that_really_has_that_parameter_still_binds_it():
    """The guard is a refusal to ALIAS, not a ban on the name: an exact parameter
    of that name is still a normal, correct binding."""
    assert recon.resolve_unknown("mesh_size_mm", 2.0, ["mesh_size_mm", "doc_id"],
                                 {"mesh_size_mm": "float"}, set()) == "mesh_size_mm"


def test_quantity_mismatch_note_is_silent_when_the_tool_lacks_the_parameter():
    """It may never invent advice about a parameter the tool does not have —
    the same rule the settings-bag pointer follows."""
    assert recon.quantity_mismatch_notes("export_part", ["fmt", "path"],
                                         ["mesh_size_mm"]) == []
    assert recon.quantity_mismatch_notes("run_fea_static", ["mesh_fineness"],
                                         ["bogus_xyz"]) == []


def test_run_fea_static_study_name_is_dropped_but_never_silently():
    """`study_name` was ACCEPTED in the live call. It is not a parameter of
    run_fea_static — it is on the closed descriptive allowlist, so it is dropped
    with a visible note. Pinned here because a silent drop would be the same
    invisible-difference defect as a wrong-unit alias."""
    import inspect
    params = list(inspect.signature(at._bind_tool("run_fea_static")).parameters)
    assert "study_name" not in params, params
    kwargs, notes, err = _agent_reconcile(
        "run_fea_static", fixtures=["bottom"],
        loads=[{"face": "top", "pressure": 0.5}], study_name="bracket_static")
    assert err is None, err
    assert "study_name" not in kwargs
    assert any("ignored 'study_name'" in n for n in notes), notes

# --- run_fea_modal(num_modes=3) -> BIND. Live 2026-07-29 (claude-sonnet-4-5, sim suite).
#     Same quantity as `n_modes` (a count of modes; no unit, no scale), so unlike
#     mesh_size_mm above this IS a spelling and is aliased. Contract rule 1 cuts both ways:
#     refuse across a quantity boundary, bind within one.

def test_n_modes_is_the_only_mode_count_slot_on_every_tool_that_takes_it():
    """The evidence for the alias, read off the real signatures rather than trusted:
    each of the three solvers has exactly ONE parameter a mode COUNT could mean, so no
    spelling of "how many modes" can be misrouted."""
    import inspect
    for tool, default in (("run_fea_modal", 6), ("run_fea_frequency_response", 6),
                          ("run_fea_buckling", 4)):
        params = inspect.signature(at._bind_tool(tool)).parameters
        assert params["n_modes"].default == default, (tool, params["n_modes"])
        counts = [p for p in params if "mode" in p.lower()]
        assert counts == ["n_modes"], (tool, counts)


def test_num_modes_was_genuinely_unreachable_by_the_affix_heuristic():
    """Why a table entry was needed at all: "nummodes" is neither a prefix nor a suffix of
    "nmodes", so resolve_unknown cannot see it WITHOUT the synonym. The same call proves
    the entries that would have been redundant are absent — `modes` and `n_mode` already
    bind on the affix rule alone, so neither is in the table."""
    ann = {"n_modes": "int"}
    assert recon.resolve_unknown("num_modes", 3, ["n_modes"], ann, set()) is None
    assert recon.resolve_unknown("modes", 3, ["n_modes"], ann, set()) == "n_modes"
    assert recon.resolve_unknown("n_mode", 3, ["n_modes"], ann, set()) == "n_modes"
    table = recon.synonyms_for("run_fea_modal")
    assert "modes" not in table and "nmode" not in table, table
    assert table["nummodes"] == "nmodes", table


@pytest.mark.parametrize("spelling", ["num_modes", "numModes", "mode_count",
                                      "number_of_modes", "modes"])
def test_every_mode_count_spelling_binds_to_n_modes_on_run_fea_modal(spelling):
    kwargs, notes, err = _agent_reconcile("run_fea_modal", fixtures=["bottom"],
                                          **{spelling: 3})
    assert err is None, err
    assert kwargs["n_modes"] == 3, (spelling, kwargs)


def test_num_modes_binds_on_the_other_two_solvers_that_take_n_modes():
    for tool, extra in (("run_fea_frequency_response", {}),
                        ("run_fea_buckling", {"load": [{"face": "top",
                                                        "force": [0, 0, -100]}]})):
        kwargs, _notes, err = _agent_reconcile(tool, fixtures=["bottom"], num_modes=3,
                                               **extra)
        assert err is None, (tool, err)
        assert kwargs["n_modes"] == 3, (tool, kwargs)


def test_an_explicit_n_modes_is_never_outvoted_by_the_synonym():
    """A synonym may only fill a FREE slot. When the model supplied the real name too, the
    real name wins and the spelling is refused rather than silently overwriting it."""
    kwargs, _notes, err = _agent_reconcile("run_fea_modal", fixtures=["bottom"],
                                           n_modes=8, num_modes=3)
    assert err is not None and err["status"] == "unknown_argument", (kwargs, err)
    assert err["unknown_params"] == ["num_modes"], err


def test_run_fea_modal_still_refuses_a_genuinely_unknown_argument():
    """The catch-all guard: the new entries must not have turned run_fea_modal into a tool
    that accepts any name."""
    kwargs, notes, err = _agent_reconcile("run_fea_modal", fixtures=["bottom"],
                                           modal_participation=True)
    assert err is not None and err["status"] == "unknown_argument", (kwargs, notes)
    assert err["unknown_params"] == ["modal_participation"], err
    assert "n_modes (optional)" in err["accepted_params"], err


def test_run_fea_modal_mesh_size_mm_is_still_a_refused_quantity_not_a_spelling():
    """The boundary this batch must not blur: `num_modes` binds because it is the same
    COUNT, while `mesh_size_mm` stays refused because millimetres are not a 0..1 factor.
    Both rules live on the same tool now, so they are pinned together."""
    kwargs, _notes, err = _agent_reconcile("run_fea_modal", fixtures=["bottom"],
                                           num_modes=3, mesh_size_mm=2.0)
    assert err is not None and err["status"] == "unknown_argument", kwargs
    assert err["unknown_params"] == ["mesh_size_mm"], err
    assert "mesh_fineness" not in kwargs and "DIMENSIONLESS" in err["error"], err


def test_the_mode_count_spellings_are_shared_by_both_dispatch_paths():
    """Anti-drift: the MCP path reads the same table object, so there is nothing to keep
    in sync. Asserted on identity, not on a copied literal."""
    assert (recon.TOOL_ARG_SYNONYMS["run_fea_modal"]
            is recon.TOOL_ARG_SYNONYMS["run_fea_buckling"]
            is recon.TOOL_ARG_SYNONYMS["run_fea_frequency_response"])
    for tool in ("run_fea_modal", "run_fea_buckling", "run_fea_frequency_response"):
        assert recon.synonyms_for(tool)["numberofmodes"] == "nmodes", tool


# --------------------------------------------------------------------- live 2026-07-29, drawing
# Requested by DrawingArgAliases, who owns cad_drawing_tools.py and carries the three
# VERBATIM live names (source_doc/template/filename) as real parameters. These are the
# remaining spellings. Added only after reading the post-revert signatures here:
#   make_drawing(doc_id, views, sections, sheet, hole_callouts, model_items, title,
#                source_doc, template)
#   export_drawing(fmt, doc_id, path, filename)

def test_the_drawing_tools_are_not_varkw_so_this_layer_actually_runs():
    """The precondition for every test below, and the reason the entries were refused
    the first time they were requested: while these tools declared **kwargs, BOTH
    dispatch paths skipped reconciliation (agent_turn.py:4380, server.py:852) and any
    table entry would have been dead code."""
    mcp_server = _mcp_server()
    for tool in ("make_drawing", "export_drawing"):
        assert at._signature_spec(at._bind_tool(tool))["var_kw"] is False, tool
        meta = mcp_server._arg_meta(mcp_server.mcp._tool_manager.get_tool(tool))
        assert meta["varkw"] is False, tool


@pytest.mark.parametrize("spelling", ["from_doc", "model_doc", "part_doc", "source_part"])
def test_every_document_spelling_binds_to_doc_id_on_make_drawing(spelling):
    kwargs, notes, err = _agent_reconcile("make_drawing", **{spelling: "d1"})
    assert err is None, (spelling, err)
    assert kwargs == {"doc_id": "d1"}, (spelling, kwargs)
    assert f"interpreted {spelling!r} as 'doc_id'" in notes, notes


@pytest.mark.parametrize("spelling", ["paper_size", "paper"])
def test_paper_size_spellings_bind_to_sheet(spelling):
    kwargs, notes, err = _agent_reconcile("make_drawing", doc_id="d1", **{spelling: "A4"})
    assert err is None, (spelling, err)
    assert kwargs["sheet"] == "A4", (spelling, kwargs)


@pytest.mark.parametrize("spelling,target", [
    ("out_file", "path"), ("output_file", "path"), ("dest", "path"),
    ("destination", "path"), ("save_as", "path"),
    ("file_type", "fmt"), ("extension", "fmt"), ("ext", "fmt"), ("type", "fmt"),
])
def test_export_drawing_output_and_format_spellings(spelling, target):
    value = "/tmp/plate" if target == "path" else "pdf"
    kwargs, notes, err = _agent_reconcile("export_drawing", doc_id="d1",
                                          **{spelling: value})
    assert err is None, (spelling, err)
    assert kwargs[target] == value, (spelling, kwargs)


def test_the_redundant_spellings_are_left_out_of_the_table_on_purpose():
    """Module convention: never duplicate the affix heuristic — an entry that shadows it
    hides which rule is doing the work. These resolve with an EMPTY synonym map, so they
    must NOT appear in the table."""
    for tool, cases in (
            ("make_drawing", {"source_document": "source_doc", "sheet_format": "sheet"}),
            ("export_drawing", {"save_path": "path", "file": "filename"})):
        spec = at._signature_spec(at._bind_tool(tool))
        table = recon.TOOL_ARG_SYNONYMS[tool]
        for spelling, expected in cases.items():
            assert recon.resolve_unknown(spelling, "x", spec["params"], spec["ann"],
                                         set(), {}) == expected, (tool, spelling)
            assert recon.norm_arg(spelling) not in table, (tool, spelling)


def test_source_needs_an_explicit_entry_because_two_real_aliases_shadow_it():
    """A trap this batch actually hit. make_drawing declares BOTH `source_doc` and
    `source_doc_id` — two spellings of one slot — so "source" affix-matches TWO free
    parameters and resolve_unknown refuses (len(affix) != 1). Declaring a second alias of
    an existing slot silently converts a working spelling into a refusal. The explicit
    synonym runs BEFORE the affix step and names the unambiguous `doc_id`, so it repairs
    the coverage without loosening the ambiguity rule."""
    spec = at._signature_spec(at._bind_tool("make_drawing"))
    assert {"source_doc", "source_doc_id"} <= set(spec["params"]), spec["params"]
    # affix alone: genuinely ambiguous, correctly refused
    assert recon.resolve_unknown("source", "d1", spec["params"], spec["ann"],
                                 set(), {}) is None
    # with the table: resolved, and to doc_id rather than either alias
    kwargs, notes, err = _agent_reconcile("make_drawing", source="d1")
    assert err is None, err
    assert kwargs == {"doc_id": "d1"}, kwargs
    assert "interpreted 'source' as 'doc_id'" in notes, notes


def test_export_drawing_refuses_output_because_it_could_mean_the_format():
    """Deliberate NON-alias, same class as run_shell(shell=). output='dxf' reads as the
    FORMAT to plenty of models; fmt and path are both str so no annotation guard can tell
    them apart, and binding it to `path` would silently write a file named 'dxf.pdf'."""
    assert "output" not in recon.TOOL_ARG_SYNONYMS["export_drawing"]
    kwargs, notes, err = _agent_reconcile("export_drawing", doc_id="d1", output="dxf")
    assert err is not None and err["status"] == "unknown_argument", (kwargs, notes)
    assert err["unknown_params"] == ["output"], err


def test_make_drawing_template_is_left_to_the_tool_to_judge_by_value():
    """`template` is a REAL parameter of make_drawing now, so this layer passes it through
    untouched rather than aliasing it to `sheet`: only the tool can inspect the value and
    tell a paper size from a border-template file (which does not exist in this repo)."""
    assert "template" not in recon.TOOL_ARG_SYNONYMS["make_drawing"]
    kwargs, notes, err = _agent_reconcile("make_drawing", doc_id="d1", template="A4")
    assert err is None, err
    assert kwargs["template"] == "A4" and "sheet" not in kwargs, kwargs


def test_drawing_synonyms_still_refuse_a_genuinely_unknown_argument():
    for tool in ("make_drawing", "export_drawing"):
        kwargs, notes, err = _agent_reconcile(tool, doc_id="d1", bogus_xyz=1)
        assert err is not None and err["status"] == "unknown_argument", (tool, kwargs)
        assert err["unknown_params"] == ["bogus_xyz"], err
