"""Tests for mission_dispatch.py + the agent_turn dispatch order (T-W32).

DoD coverage: the run-scoped dispatcher resolves the six named tool families
(ledger_get/ledger_note, dag_plan, ask_user, delegate, part_search) with mission
context, and agent_turn consults it BEFORE the process-wide registry (mission hit
vs global fallback — both paths tested)."""
from __future__ import annotations

import json

import pytest

from services.agent import agent_turn
from services.agent.ledger import Ledger
from services.agent.mission_dispatch import MissionDispatch, _normalize_questions


@pytest.fixture()
def cache_root(tmp_path, monkeypatch):
    monkeypatch.setenv("AICAD_CACHE_ROOT", str(tmp_path))
    return tmp_path


@pytest.fixture()
def ledger(tmp_path):
    return Ledger("t_w32_run", path=tmp_path / "ledger.json")


@pytest.fixture()
def md(ledger, cache_root):
    return MissionDispatch(run_id="t_w32_run", ledger=ledger)


# ---------------------------------------------------------------------------
# resolution surface
# ---------------------------------------------------------------------------

class TestResolve:
    def test_resolves_all_six_families(self, md):
        for tool in MissionDispatch.TOOLS:
            assert md.resolve(tool) is not None, tool

    def test_unknown_tool_returns_none(self, md):
        assert md.resolve("add_features") is None
        assert md.resolve("no_such_tool") is None


# ---------------------------------------------------------------------------
# ledger tools — bound to the run's ledger handle
# ---------------------------------------------------------------------------

class TestLedgerTools:
    def test_note_then_get_roundtrip(self, md, ledger):
        out = md.ledger_note(kind="decision", payload={
            "id": "D1", "stage": "spec", "text": "use 6061", "rationale": "cheap"})
        assert out["ok"] is True and out["run_id"] == "t_w32_run"
        got = md.ledger_get(section="decisions")
        assert got["ok"] is True
        assert any(d.get("id") == "D1" for d in got["data"])

    def test_get_full_render_within_budget(self, md):
        md.ledger_note(kind="decision", payload={"id": "D1", "stage": "s",
                                                 "text": "t", "rationale": "r"})
        got = md.ledger_get(budget_tokens=200)
        assert got["ok"] is True and "rendered" in got

    def test_get_unknown_section_is_error_data(self, md):
        got = md.ledger_get(section="nonsense")
        assert got["ok"] is False and "allowed" in got

    def test_dimension_requires_provenance(self, md):
        out = md.ledger_note(kind="dimension",
                             payload={"id": "DIM1", "name": "bore", "value": 10})
        assert out["ok"] is False and "provenance" in out["error"]

    def test_foreign_run_id_is_scoped_to_own_run(self, md):
        out = md.ledger_note(kind="note", payload={"text": "hi"},
                             run_id="someone_elses_run")
        assert out["ok"] is True and out["run_id"] == "t_w32_run"

    def test_no_ledger_is_structured_error(self, cache_root):
        bare = MissionDispatch(run_id="r")
        assert bare.ledger_get()["ok"] is False
        assert bare.ledger_note(kind="note", payload={})["ok"] is False


# ---------------------------------------------------------------------------
# ask_user — autonomous: record + default, never a fabricated human
# ---------------------------------------------------------------------------

class TestAskUser:
    def test_suggested_default_applied_and_recorded(self, md, ledger):
        out = md.ask_user(questions=[{"id": "q1", "prompt": "shaft material?",
                                      "suggested": "steel"}])
        assert out["ok"] is True
        ans = out["answers"]["q1"]
        assert ans["value"] == "steel" and ans["source"] in ("default", "resumed")
        # mirrored onto the mission design ledger for the run record
        qs = ledger.state.get("questions") or []
        assert any(q.get("id") == "q1" for q in qs)

    def test_plain_string_question_normalizes(self, md):
        out = md.ask_user(questions="what clearance class?")
        assert out["ok"] is True and len(out["answers"]) == 1

    def test_no_questions_is_error_data(self, md):
        out = md.ask_user()
        assert out["ok"] is False

    def test_normalize_caps_at_contract_four(self):
        qs = _normalize_questions([f"q {i}" for i in range(9)])
        assert len(qs) == 4
        assert all(q["suggested"] == "" and q["kind"] == "text" for q in qs)


# ---------------------------------------------------------------------------
# delegate / dag_plan / part_search
# ---------------------------------------------------------------------------

class TestDelegate:
    def test_no_tree_is_structured_refusal_and_recorded(self, md, ledger):
        out = md.delegate(role="sketcher", brief="draw it", budget_tokens=500)
        assert out["ok"] is False and out["status"] == "unavailable"
        evs = [e for e in ledger.events
               if (e.get("payload") or {}).get("event") == "delegation_request"]
        assert len(evs) == 1

    def test_with_tree_routes_to_delegation_layer(self, md):
        calls = {}

        class _Tree:
            pass

        md.delegation_tree = _Tree()

        import services.agent.delegation as dlg
        real = dlg.delegate

        def _spy(role, brief, budget, **kw):
            calls["role"] = role
            calls["tree"] = kw.get("tree")
            return {"id": "d1", "status": "pending"}
        dlg.delegate = _spy
        try:
            out = md.delegate(role="sketcher", brief="b", budget_tokens=100)
        finally:
            dlg.delegate = real
        assert out == {"id": "d1", "status": "pending"}
        assert calls["role"] == "sketcher" and calls["tree"] is md.delegation_tree


class TestDagPlan:
    def test_with_graph_returns_summary(self, cache_root):
        graph = {"id": "g1", "name": "mission", "edges": [],
                 "nodes": [{"id": "n1", "type": "design", "name": "Build",
                            "inputs": {"role": "feature_modeler"}}]}
        md = MissionDispatch(run_id="r", graph=graph)
        out = md.dag_plan()
        assert out["ok"] is True
        assert out["graph"]["nodes"][0]["role"] == "feature_modeler"
        assert "replan_request" in out["note"]

    def test_without_graph_falls_back_to_stage_log(self, md, ledger):
        ledger.log_stage("pipeline", "specify")
        out = md.dag_plan()
        assert out["ok"] is True and out.get("stage_log")


class TestPartSearch:
    def test_empty_query_is_error_data(self, md):
        assert md.part_search("")["ok"] is False

    def test_routes_to_parts_hub(self, md, monkeypatch):
        import engine_oracle.parts_hub as hub

        monkeypatch.setattr(hub, "search",
                            lambda q, domain=None, limit=20: [{"designation": "M3"}])
        out = md.part_search("M3")
        assert out["ok"] is True and out["count"] == 1


# ---------------------------------------------------------------------------
# agent_turn dispatch order — mission hit BEFORE global fallback
# ---------------------------------------------------------------------------

class _ScriptedProvider:
    """Emit one tool call, then finish."""
    def __init__(self, tool, args=None):
        self._replies = [
            json.dumps({"tool": tool, "args": args or {}}),
            json.dumps({"done": True, "handoff": {"summary": "ok"}}),
        ]

    def complete(self, system, messages, max_tokens=None):
        return f"```json\n{self._replies.pop(0)}\n```"


class TestDispatchOrder:
    def test_mission_hit_wins_over_global(self, md, monkeypatch):
        """ledger_note resolves mission-scoped: the global registry is never asked."""
        def _boom(tool, args):
            raise AssertionError("global dispatch must not be consulted")
        monkeypatch.setattr(agent_turn, "_dispatch_tool", _boom)

        res = agent_turn.run_turn(
            "note a decision", _ScriptedProvider(
                "ledger_note", {"kind": "note", "payload": {"text": "hi"}}),
            allowed_tools=["ledger_note"], mission_dispatch=md)
        assert res.success is True
        call = res.result["calls"][0]
        assert call["ok"] is True and call["tool"] == "ledger_note"

    def test_global_fallback_when_mission_misses(self, md, monkeypatch):
        """A non-mission tool falls through to the process-wide registry path."""
        seen = {}

        def _global(tool, args):
            seen["tool"] = tool
            return {"ok": True}
        monkeypatch.setattr(agent_turn, "_dispatch_tool", _global)

        res = agent_turn.run_turn(
            "measure", _ScriptedProvider("measure_part", {}),
            allowed_tools=["measure_part"], mission_dispatch=md)
        assert res.success is True and seen["tool"] == "measure_part"

    def test_resolver_for_mirrors_dispatch_order(self, md):
        resolve = agent_turn.resolver_for(md)
        # mission hit: the bound method, not the registry fn
        assert resolve("ledger_get").__self__ is md
        # global fallback for a real registry tool
        assert resolve("fit_calc") is not None
        # invented tool: honestly unresolvable
        assert resolve("design_part_magically") is None

    def test_tools_text_replaces_bare_name_list(self, md):
        """The §9.1 tools slot lands in the system prompt when provided."""
        captured = {}

        class _CaptureProvider:
            def complete(self, system, messages, max_tokens=None):
                captured["system"] = system
                return '```json\n{"done": true, "handoff": {}}\n```'

        agent_turn.run_turn(
            "go", _CaptureProvider(), allowed_tools=["ledger_note"],
            mission_dispatch=md,
            tools_text="[TOOLS] Call tools with EXACTLY these kwargs:\n- ledger_note(kind: str)")
        assert "EXACTLY these kwargs" in captured["system"]
        assert "Allowed tools:" not in captured["system"]
