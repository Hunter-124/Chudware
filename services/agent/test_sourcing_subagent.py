"""Sourcing sub-agent end-to-end (mocked): parent delegates to sourcing_specialist,
the sub-agent runs with its ROLE CARD (system_prompt + tool allowlist + rendered
tool schemas + this mission's dispatch layer), walks parts_taxonomy → part_search,
and hands off recommendations the parent sees.
Also pins the _run_subagent fix contract: no more BARE sub-agents (hardcoded
max_tool_calls=4, no role prompt, no allowlist), token-derived soft warning
thresholds with a separate runaway ceiling, allowlist denial for out-of-scope
tools, and bare fallback for unknown roles.

MockProvider-scripted; no network, no LLM, no libchudware.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from services.agent.ledger import Ledger  # noqa: E402
from services.agent.mission_dispatch import MissionDispatch  # noqa: E402


# ---------------------------------------------------------------------------
# Scaffolding
# ---------------------------------------------------------------------------

class _Node:
    def __init__(self, node_id="dg_srctest", role="sourcing_specialist",
                 tokens_granted=16000):
        self._d = {"id": node_id, "role": role, "status": "approved",
                   "budget": {"tokens_granted": tokens_granted,
                              "tokens_spent": 0, "attempts": 0}}

    def to_dict(self):
        return dict(self._d)


class _FakeTree:
    """Duck-typed DelegationTree: approve immediately, record transitions."""

    def __init__(self, role="sourcing_specialist", tokens_granted=16000):
        self.role = role
        self.tokens_granted = tokens_granted
        self.transitions: list[tuple] = []

    def request_delegation(self, role, brief, budget, **kw):
        return _Node(role=role, tokens_granted=self.tokens_granted)

    def start(self, node_id):
        self.transitions.append(("start", node_id))

    def complete(self, node_id, tokens_used=0):
        self.transitions.append(("complete", node_id))

    def fail(self, node_id, reason=""):
        self.transitions.append(("fail", node_id, reason))


class _CapturingProvider:
    """MockProvider-style scripted provider that records every system prompt."""

    def __init__(self, replies: list[str]):
        self._replies = list(replies)
        self.systems: list[str] = []
        self.max_tokens: list[int | None] = []

    def complete(self, system, messages, max_tokens=None):
        self.systems.append(system)
        self.max_tokens.append(max_tokens)
        return self._replies.pop(0) if len(self._replies) > 1 else self._replies[0]


def _fence(obj) -> str:
    return "```json\n" + json.dumps(obj) + "\n```"


_HANDOFF = {
    "status": "done",
    "summary": "one bearing candidate, spec-fit on bore",
    "outputs": {
        "recommendations": [{
            "part_id": "bearing:ISO 15:6001", "vendor": "library",
            "price": None, "specs": {"bore": 12.0},
            "rationale": "bore_mm=12 spec fit; local placeable record",
            "artifact_plan": {"how": "part_place"},
        }],
        "reasoning_summary": "taxonomy walk then faceted search",
    },
}


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("CHUDWARE_CACHE_ROOT", str(tmp_path))
    # Hermetic: delegate() rebuilds a per-role provider from model assignment
    # (WS2/mega-5) — ambient config.json ai_roles on a dev machine, or the
    # ollama default when config is empty. Either replaces the scripted mock
    # with a live provider. Force the legacy no-assignment path instead.
    monkeypatch.setattr("services.agent.config.load_config", lambda *a, **k: {})
    monkeypatch.setattr("services.agent.role_models.resolve_for_node",
                        lambda *a, **k: None)
    monkeypatch.setattr("services.agent.role_models.resolve_role_model",
                        lambda *a, **k: None)
    # Deterministic parts backends — no hub/index.
    from services.mcp import parts_tools as pt
    monkeypatch.setattr(pt, "taxonomy_browse",
                        lambda node_id="", depth=2, include_facets=True:
                        {"ok": True, "node": {"id": node_id or ""},
                         "children": [{"id": "bearings_motion",
                                       "label": "Bearings & Motion",
                                       "has_children": True}],
                         "facets": [], "breadcrumbs": []})
    monkeypatch.setattr(pt, "search_parts_v2",
                        lambda query, **kw:
                        {"ok": True, "count": 1, "has_live": False,
                         "results": [{"part_id": "bearing:ISO 15:6001",
                                      "title": "6001", "params": {"bore": 12.0}}],
                         "facet_summary": {}, "applied": kw})
    return tmp_path


def _dispatch(tmp_path, provider, tree) -> MissionDispatch:
    ledger = Ledger("srctest_run", path=tmp_path / "ledger.json")
    return MissionDispatch(run_id="srctest_run", ledger=ledger,
                           delegation_tree=tree, provider=provider)


# ---------------------------------------------------------------------------
# The happy path: taxonomy walk → faceted search → handoff with recommendations
# ---------------------------------------------------------------------------

class TestSourcingSubagent:
    def test_delegate_runs_sourcing_specialist_with_role_card(self, env):
        provider = _CapturingProvider([
            _fence({"tool": "parts_taxonomy", "args": {"node_id": ""}}),
            _fence({"tool": "part_search",
                    "args": {"query": "bearing",
                             "category_id": "bearings_motion",
                             "facets": {"bore_mm": 12}}}),
            _fence({"done": True, "handoff": _HANDOFF}),
        ])
        tree = _FakeTree()
        md = _dispatch(env, provider, tree)

        node = md.delegate(role="sourcing_specialist",
                           brief="source a 12mm bore bearing",
                           budget_tokens=16000)

        # Parent sees the executed sub-agent's handoff.
        assert node["executed"] is True
        assert node["ok"] is True and node["subagent_status"] == "done"
        recs = node["handoff"]["outputs"]["recommendations"]
        assert recs[0]["part_id"] == "bearing:ISO 15:6001"
        assert recs[0]["artifact_plan"]["how"] == "part_place"
        assert ("complete", "dg_srctest") in tree.transitions

        # The sub-agent ran WITH its role card, not bare:
        system = provider.systems[0]
        assert "SOURCING SPECIALIST" in system          # card system_prompt
        assert "never fabricate" in system.lower()       # vendor etiquette rule
        # rendered tool schemas (§9.1 tools slot), not a bare name list
        assert "[TOOLS] Call tools with EXACTLY these kwargs:" in system
        assert "part_source(need: str" in system
        assert "Allowed tools:" not in system

        # Both tool calls dispatched through THIS mission's dispatch layer.
        calls = node["result"]["calls"]
        assert [c["tool"] for c in calls] == ["parts_taxonomy", "part_search"]
        assert all(c["ok"] for c in calls)
        assert set(provider.max_tokens) == {8192}

    def test_allowlist_denies_out_of_scope_tool(self, env):
        provider = _CapturingProvider([
            _fence({"tool": "add_features", "args": {}}),   # NOT in the card
            _fence({"tool": "parts_taxonomy", "args": {}}),
            _fence({"done": True, "handoff": _HANDOFF}),
        ])
        tree = _FakeTree()
        md = _dispatch(env, provider, tree)
        node = md.delegate(role="sourcing_specialist", brief="source it",
                           budget_tokens=16000)
        calls = node["result"]["calls"]
        assert calls[0]["tool"] == "add_features"
        assert "not in allowed list" in calls[0]["error"]
        assert calls[1]["tool"] == "parts_taxonomy" and calls[1]["ok"]

    def test_budget_derives_soft_threshold_not_hard_stop(self, env, monkeypatch):
        # 16000 granted → soft warning at 8; the separate ceiling remains 20.
        from services.agent import agent_turn
        monkeypatch.setattr(agent_turn, "SUBAGENT_HARD_TOOL_CALL_CEILING", 20)
        looping = _fence({"tool": "parts_taxonomy", "args": {}})
        provider = _CapturingProvider([looping])  # never finishes
        tree = _FakeTree(tokens_granted=16000)
        md = _dispatch(env, provider, tree)
        node = md.delegate(role="sourcing_specialist", brief="loop",
                           budget_tokens=16000)
        assert node["result"]["tool_calls"] == 20
        assert node["result"]["forced_stop"] == "subagent_hard_ceiling"

    def test_hard_ceiling_stays_above_derived_threshold(self, env, monkeypatch):
        from services.agent import agent_turn
        monkeypatch.setattr(agent_turn, "SUBAGENT_HARD_TOOL_CALL_CEILING", 20)
        looping = _fence({"tool": "parts_taxonomy", "args": {}})
        for granted, soft in ((2000, 4), (100000, 12), (0, 10)):
            tree = _FakeTree(tokens_granted=granted)
            md = _dispatch(env, _CapturingProvider([looping]), tree)
            node = md.delegate(role="sourcing_specialist", brief="loop",
                               budget_tokens=granted)
            assert node["result"]["tool_calls"] == max(20, soft * 2), granted

    def test_unknown_role_is_rejected_before_child_execution(self, env):
        provider = _CapturingProvider([
            _fence({"done": True, "handoff": {"summary": "ok"}}),
        ])
        tree = _FakeTree(role="mystery_role")
        md = _dispatch(env, provider, tree)
        node = md.delegate(role="mystery_role", brief="do a thing",
                           budget_tokens=4000)
        assert node["ok"] is False
        assert node["reason"] == "invalid_role"
        assert provider.systems == []

    def test_role_card_loads_and_validates(self):
        from services.agent import roles
        card = roles.load_role("sourcing_specialist")
        assert card.goal and "sourcing" in card.system_prompt.lower()
        names = card.concrete_tools
        for t in ("parts_taxonomy", "part_search", "part_get", "part_source",
                  "vendors_health", "scrape_enqueue", "scrape_queue_status",
                  "import_artifact", "part_place", "bom_price"):
            assert t in names, t
        assert len(card.tool_allowlist) <= roles.MAX_TOOLS_PER_ROLE
        # Handoff schema requires recommendations with artifact plans.
        req = card.handoff_schema["properties"]["outputs"]["required"]
        assert "recommendations" in req
        errs = roles.validate_handoff(
            card, {"schema": "aicad.handoff/1", "role": "sourcing_specialist",
                   **_HANDOFF})
        assert errs == []
