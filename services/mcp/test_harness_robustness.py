"""Harness-robustness tests: preflight cost gate, buildgraph input dialect,
buildgraph_status discoverability, and the two "advertised but impossible" tools.

Every case here mirrors a real failure mined from recorded agent-run ledgers.
"""
from __future__ import annotations

import asyncio
import json

import pytest


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

class _StubMCP:
    """Minimal @mcp.tool() host that keeps the raw function."""

    def __init__(self) -> None:
        self.tools: dict = {}

    def tool(self, *args, **kwargs):
        def deco(fn):
            self.tools[kwargs.get("name") or fn.__name__] = fn
            return fn
        return deco


def _buildgraph_tools(tmp_path):
    from services.agent.buildgraph.mcp_tools import register_buildgraph_tools

    mcp = _StubMCP()
    register_buildgraph_tools(mcp, runs_dir=tmp_path)
    return mcp.tools


def _payload(raw):
    return json.loads(raw) if isinstance(raw, str) else raw


# ---------------------------------------------------------------------------
# 1. preflight complexity gate — ~8 engine RPCs on the hottest tool
# ---------------------------------------------------------------------------

_BOX_OP = {"op": "add_feature", "feature": {
    "type": "box", "name": "Block",
    "params": {"length": {"value": 40}, "width": {"value": 30},
               "height": {"value": 20}}}}
_CUT_OP = {"op": "add_feature", "feature": {
    "type": "cut", "name": "Pocket", "params": {"depth": {"value": 5}}}}
_REF_OP = {"op": "add_feature", "feature": {
    "type": "cylinder", "name": "Boss", "params": {"radius": {"value": 5},
                                                   "height": {"value": 10}},
    "refs": {"face": {"of": "face", "where": "top"}}}}
_SKETCH_OP = {"op": "sketch_add_entity", "sketch": "f1",
              "entity": {"type": "line", "id": "e0"}}


@pytest.fixture()
def spy_preflight(monkeypatch):
    """Count real run_preflight invocations without touching the engine."""
    import services.agent.advisor.preflight as pf

    calls: list = []

    def _fake(doc_id, tool, ops):
        calls.append((doc_id, tool, list(ops)))
        return {"schema": "aicad.preflight/1", "id": "pf_fake", "verdict": "pass",
                "checks": [], "predicted": {}, "downstream": [],
                "advisory_items": []}

    monkeypatch.setattr(pf, "run_preflight", _fake)
    monkeypatch.delenv("CHUDWARE_PREFLIGHT", raising=False)
    monkeypatch.delenv("CHUDWARE_PREFLIGHT_MAX_OPS", raising=False)
    return calls


def test_preflight_skipped_for_trivial_batch(spy_preflight):
    from services.mcp.preflight_tools import run_auto_preflight

    out = run_auto_preflight("doc1", "add_features", [_BOX_OP])

    assert spy_preflight == [], "a lone primitive must not pay for a shadow clone"
    assert out["verdict"] == "skipped"
    assert out["skipped"] == "trivial"
    # The reason is observable, never a silent behaviour change.
    assert "no refs" in out["skipped_detail"]


def test_preflight_skipped_for_metadata_only_batch(spy_preflight):
    from services.mcp.preflight_tools import run_auto_preflight

    ops = [{"op": "rename", "id": "f1", "name": "Base"},
           {"op": "set_units", "units": "mm"},
           {"op": "set_material", "material": "6061"}]
    out = run_auto_preflight("doc1", "add_features", ops)

    assert spy_preflight == []
    assert out["verdict"] == "skipped" and out["skipped"] == "trivial"


@pytest.mark.parametrize("ops,why", [
    ([_REF_OP], "refs need resolving"),
    ([_CUT_OP], "boolean/destructive feature"),
    ([_SKETCH_OP], "sketch mutation"),
    ([{"op": "delete_feature", "id": "f2"}], "destructive op"),
    ([{"op": "add_feature", "feature": {"type": "box", "name": "B",
       "params": {"length": {"value": -5}}}}], "params_sane has something to say"),
])
def test_preflight_still_runs_for_non_trivial_batches(spy_preflight, ops, why):
    from services.mcp.preflight_tools import run_auto_preflight

    out = run_auto_preflight("doc1", "add_features", ops)

    assert len(spy_preflight) == 1, f"preflight must still run when {why}"
    assert out["verdict"] == "pass"


def test_preflight_still_runs_for_oversized_batch(spy_preflight):
    from services.mcp.preflight_tools import run_auto_preflight

    ops = [_BOX_OP] * 8  # every op individually trivial, batch is not
    run_auto_preflight("doc1", "add_features", ops)

    assert len(spy_preflight) == 1


def test_preflight_force_on_override_beats_the_gate(spy_preflight, monkeypatch):
    from services.mcp.preflight_tools import run_auto_preflight

    monkeypatch.setenv("CHUDWARE_PREFLIGHT", "always")
    out = run_auto_preflight("doc1", "add_features", [_BOX_OP])

    assert len(spy_preflight) == 1, "CHUDWARE_PREFLIGHT=always must force the clone"
    assert out["verdict"] == "pass"


def test_preflight_disabled_override_still_wins(spy_preflight, monkeypatch):
    from services.mcp.preflight_tools import run_auto_preflight

    monkeypatch.setenv("CHUDWARE_PREFLIGHT", "0")
    out = run_auto_preflight("doc1", "add_features", [_CUT_OP])

    assert spy_preflight == []
    assert out["skipped"] == "disabled"


def test_preflight_decisions_are_recorded(spy_preflight):
    from services.mcp import preflight_tools as pt

    pt.run_auto_preflight("docX", "add_features", [_BOX_OP])
    last = pt.preflight_decisions()[-1]

    assert last["doc_id"] == "docX" and last["ran"] is False
    assert last["n_ops"] == 1 and last["reason"]


# ---------------------------------------------------------------------------
# 2. buildgraph_plan input dialect
# ---------------------------------------------------------------------------

_CANONICAL = {"name": "bracket",
              "parts": [{"name": "bracket",
                         "features": ["base plate 80x60x10", "4x M5 holes"]}]}


def test_buildgraph_plan_canonical_call_is_unchanged(tmp_path):
    out = _payload(_buildgraph_tools(tmp_path)["buildgraph_plan"](plan_json=_CANONICAL))

    assert out["ok"] is True and out["node_count"] == 4
    # Nothing was rewritten, so no echo keys appear on the canonical shape.
    assert "interpreted" not in out and "plan_used" not in out


def test_buildgraph_plan_accepts_a_json_string(tmp_path):
    out = _payload(_buildgraph_tools(tmp_path)["buildgraph_plan"](
        plan_json=json.dumps(_CANONICAL)))

    assert out["ok"] is True and out["node_count"] == 4
    assert any("JSON string" in note for note in out["interpreted"])
    assert out["plan_used"]["parts"][0]["name"] == "bracket"


def test_buildgraph_plan_accepts_a_fenced_json_string(tmp_path):
    fenced = "```json\n" + json.dumps(_CANONICAL) + "\n```"
    out = _payload(_buildgraph_tools(tmp_path)["buildgraph_plan"](plan_json=fenced))

    assert out["ok"] is True and out["node_count"] == 4


def test_buildgraph_plan_malformed_string_teaches_the_fix(tmp_path):
    out = _payload(_buildgraph_tools(tmp_path)["buildgraph_plan"](
        plan_json='{"parts": [oops'))

    assert out["ok"] is False
    assert "failed to parse" in out["reason"]        # quotes the parse error
    assert "Expecting" in out["reason"]
    assert "parts" in out["hint"] and "deliverable" in out["hint"]


def test_buildgraph_plan_alias_kwarg(tmp_path):
    out = _payload(_buildgraph_tools(tmp_path)["buildgraph_plan"](plan=_CANONICAL))

    assert out["ok"] is True and out["node_count"] == 4
    assert any("plan=" in note for note in out["interpreted"])


def test_buildgraph_plan_synthesizes_from_semantic_fields(tmp_path):
    out = _payload(_buildgraph_tools(tmp_path)["buildgraph_plan"](
        deliverable="steel bracket",
        components=["bracket", "pin"],
        constraints=["6061-T6"],
    ))

    assert out["ok"] is True
    plan = out["plan_used"]
    assert plan["name"] == "steel bracket"
    assert [p["name"] for p in plan["parts"]] == ["bracket", "pin"]
    assert plan["constraints"] == ["6061-T6"]
    assert any("synthesized" in note for note in out["interpreted"])


def test_buildgraph_plan_bare_brief_becomes_a_plan(tmp_path):
    out = _payload(_buildgraph_tools(tmp_path)["buildgraph_plan"](
        brief="flanged bushing with a 20mm bore",
        sequence=["revolve the body", "bore the through hole"],
    ))

    assert out["ok"] is True
    assert out["plan_used"]["parts"][0]["features"] == [
        "revolve the body", "bore the through hole"]


def test_buildgraph_plan_no_arguments_teaches_instead_of_raising(tmp_path):
    out = _payload(_buildgraph_tools(tmp_path)["buildgraph_plan"]())

    assert out["ok"] is False
    assert "needs a plan" in out["reason"]
    assert "deliverable" in out["hint"]


def test_buildgraph_plan_accepts_a_bare_list_of_steps(tmp_path):
    out = _payload(_buildgraph_tools(tmp_path)["buildgraph_plan"](
        plan_json=["base plate", "4x M5 holes"], mission="bracket"))

    assert out["ok"] is True
    assert out["plan_used"]["parts"][0]["features"] == ["base plate", "4x M5 holes"]
    assert any("strings" in note for note in out["interpreted"])


def test_buildgraph_plan_survives_dict_features(tmp_path):
    """'cannot use dict as a dict key (unhashable type)' — real ledger crash."""
    plan = {"parts": [{"name": "plate", "features": [
        {"type": "hole", "dia": 5}, {"type": "hole", "dia": 5}]}]}

    out = _payload(_buildgraph_tools(tmp_path)["buildgraph_plan"](plan_json=plan))

    assert out["ok"] is True
    labels = [n["label"] for n in out["graph"]["nodes"] if n["kind"] == "feature"]
    assert len(labels) == 2 and len(set(labels)) == 2


def test_buildgraph_plan_survives_string_parts(tmp_path):
    out = _payload(_buildgraph_tools(tmp_path)["buildgraph_plan"](
        plan_json={"parts": ["bracket", "pin"]}))

    assert out["ok"] is True
    assert {n["label"] for n in out["graph"]["nodes"] if n["kind"] == "part"} == {
        "bracket", "pin"}


def test_buildgraph_plan_canonical_call_works_through_fastmcp(tmp_path):
    """The var-kw parameter must not break the external FastMCP surface."""
    from mcp.server.fastmcp import FastMCP
    from services.agent.buildgraph.mcp_tools import register_buildgraph_tools

    mcp = FastMCP("harness-robustness")
    register_buildgraph_tools(mcp, runs_dir=tmp_path)

    blocks, _ = asyncio.run(
        mcp.call_tool("buildgraph_plan", {"plan_json": _CANONICAL}))
    out = json.loads(blocks[0].text)

    assert out["ok"] is True and out["node_count"] == 4


# ---------------------------------------------------------------------------
# 3. buildgraph_status — an unknown run must show the known ones
# ---------------------------------------------------------------------------

def test_buildgraph_status_lists_real_run_ids(tmp_path):
    tools = _buildgraph_tools(tmp_path)
    planned = _payload(tools["buildgraph_plan"](plan_json=_CANONICAL))["run_id"]

    out = _payload(tools["buildgraph_status"](run_id="not-a-real-run"))

    assert out["ok"] is False
    assert out["known_run_ids"] == [planned]
    assert planned in out["hint"]
    assert out["did_you_mean"] == ""          # nothing close — suggest nothing


def test_buildgraph_status_suggests_the_closest_run_without_resolving_it(tmp_path):
    tools = _buildgraph_tools(tmp_path)
    planned = _payload(tools["buildgraph_plan"](plan_json=_CANONICAL))["run_id"]
    typo = planned[:-1]

    out = _payload(tools["buildgraph_status"](run_id=typo))

    assert out["ok"] is False, "a near match is a suggestion, never an auto-resolve"
    assert out["did_you_mean"] == planned
    assert "not resolved for you" in out["hint"]


def test_buildgraph_status_says_when_nothing_was_ever_planned(tmp_path):
    out = _payload(_buildgraph_tools(tmp_path)["buildgraph_status"](run_id="x"))

    assert out["ok"] is False and out["known_run_ids"] == []
    assert "buildgraph_plan" in out["hint"]


# ---------------------------------------------------------------------------
# 4a. delegate — bind the missing context where it is obtainable
# ---------------------------------------------------------------------------

class _Provider:
    def complete(self, *_a, **_k):            # pragma: no cover - never called
        return {"text": ""}


def test_delegate_binds_a_tree_when_a_provider_is_available(monkeypatch):
    from services.agent.mission_dispatch import MissionDispatch

    md = MissionDispatch(run_id="r_autobind", provider=_Provider())
    assert md.delegation_tree is None
    # The stub must represent a child that ACTUALLY RAN. `executed` is set
    # optimistically before the child starts, and _delegation_execution_verdict now
    # downgrades a node that cannot evidence execution — so a bare
    # {"executed": True} is exactly the fabricated success that gate exists to catch
    # (a firmware turn closed as `confirmed` on one, claiming a simulation that never
    # happened). Evidence here is a spent budget and an attempt.
    monkeypatch.setattr(
        MissionDispatch, "_run_subagent",
        lambda self, node, brief: {
            "executed": True,
            "budget": {"attempts": 1, "tokens_spent": 120},
        })

    out = md.delegate(role="sketcher", brief="sketch the base plate",
                      budget_tokens=4000)

    assert md.delegation_tree is not None, "an obtainable context must be bound"
    assert out.get("status") == "approved"
    assert out.get("delegation_tree_autobound") is True   # observable, not silent
    assert out.get("executed") is True


def test_delegate_refuses_to_report_execution_it_cannot_evidence(monkeypatch):
    """The companion guard: evidence-free `executed` must NOT survive.

    Without this, the test above could go green again by weakening the honesty
    verdict rather than by delegating for real.
    """
    from services.agent.mission_dispatch import MissionDispatch

    md = MissionDispatch(run_id="r_noevidence", provider=_Provider())
    monkeypatch.setattr(MissionDispatch, "_run_subagent",
                        lambda self, node, brief: {"executed": True})

    out = md.delegate(role="sketcher", brief="sketch the base plate",
                      budget_tokens=4000)

    assert out.get("executed") is not True, (
        "a node with no tokens, no attempts, no handoff, no calls and no docs "
        "claimed execution")
    assert out.get("ok") is False
    assert out.get("subagent_status") == "not_run"
    note = out.get("note") or ""
    assert "did NOT run" in note, (
        "a downgrade must say why, or the caller cannot act on it")
    assert "do not report this brief as done" in note, (
        "the note must tell the model what NOT to conclude — this is the exact "
        "turn-closing fabrication the verdict exists to prevent")


def test_delegate_names_what_to_do_instead_when_no_tree_can_be_built(monkeypatch):
    """The unavailable refusal must stay actionable.

    A missing Python provider stopped being the trigger: Chudcode runs the child, so
    `delegate` binds a governed tree on the spot (pinned by
    `test_delegate_binds_a_tree_when_a_provider_is_available` above). The only
    remaining `unavailable` state is a tree that cannot be CONSTRUCTED, and that is
    the anti-fabrication path — it has to tell the model not to retry and what to do
    instead, or the turn closes on sub-agent work that never happened.
    """
    from services.agent import delegation as _delegation
    from services.agent.mission_dispatch import MissionDispatch

    def _no_tree(*_a, **_k):
        raise RuntimeError("governed delegation tree unavailable")

    monkeypatch.setattr(_delegation, "DelegationTree", _no_tree)

    md = MissionDispatch(run_id="r_no_tree")

    out = md.delegate(role="layout_engineer", brief="lay out the board",
                      budget_tokens=4000)

    assert out["ok"] is False and out["status"] == "unavailable"
    assert "Do NOT retry delegate" in out["error"]
    assert "ledger_note" in out["error"]
    assert out["do_instead"]


def test_mcp_delegate_spawns_when_a_tree_is_bound():
    from services.mcp import delegation_tools as dt

    class _Node:
        def to_dict(self):
            return {"id": "dg_1", "status": "approved", "role": "sketcher"}

    class _Tree:
        def request_delegation(self, role, brief, budget, **_k):
            return _Node()

    mcp = _StubMCP()
    dt.register_delegation_tools(mcp)
    dt.bind_delegation_tree(_Tree())
    try:
        out = mcp.tools["delegate"](role="sketcher", brief="b", budget_tokens=100)
    finally:
        dt.bind_delegation_tree(None)

    assert out["status"] == "approved"


def test_mcp_delegate_without_a_tree_is_honest_and_actionable():
    from services.mcp import delegation_tools as dt

    mcp = _StubMCP()
    dt.register_delegation_tools(mcp)

    out = mcp.tools["delegate"](role="sketcher", brief="b", budget_tokens=100)

    assert out["ok"] is False and out["status"] == "unavailable"
    assert "Do NOT retry delegate" in out["error"]
    assert out["do_instead"]


# ---------------------------------------------------------------------------
# 4b. workspace_status — a named-but-unknown run must be actionable
# ---------------------------------------------------------------------------

def _workspace_tools():
    from services.mcp.workspace_tools import register_workspace_tools

    mcp = _StubMCP()
    register_workspace_tools(mcp)
    return mcp.tools


def test_workspace_status_unknown_run_with_no_sessions_names_the_alternatives():
    from services.agent import workspace as ws

    assert not ws._session_registry, "test isolation: registry must start empty"
    out = _workspace_tools()["workspace_status"](run_id="gym_C1_local-24k_s2")

    assert out["ok"] is False                       # orchestrator poll contract
    assert out["known_run_ids"] == []
    assert "workspace_status()" in out["do_instead"]
    assert out["reason"] == "unknown_run_id"


def test_workspace_status_unknown_run_lists_the_live_sessions():
    from services.agent.workspace import (
        WorkspaceSession, register_session, unregister_session,
    )

    session = WorkspaceSession(run_id="run_alpha")
    register_session(session)
    try:
        out = _workspace_tools()["workspace_status"](run_id="run_alpho")
    finally:
        unregister_session("run_alpha")

    assert out["ok"] is False
    assert out["known_run_ids"] == ["run_alpha"]
    assert out["did_you_mean"] == "run_alpha"
    assert "not resolved for you" in out["hint"]
    assert out["do_instead"]


def test_workspace_status_known_run_is_unchanged():
    from services.agent.workspace import (
        WorkspaceSession, register_session, unregister_session,
    )

    session = WorkspaceSession(run_id="run_beta")
    session.open_assembly_doc("doc_asm", "orch")
    register_session(session)
    try:
        out = _workspace_tools()["workspace_status"](run_id="run_beta")
    finally:
        unregister_session("run_beta")

    assert out["ok"] is True and out["run_id"] == "run_beta"
    assert any(d["doc_id"] == "doc_asm" for d in out["docs"])
