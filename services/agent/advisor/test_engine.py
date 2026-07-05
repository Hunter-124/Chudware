"""Tests for the advisory engine / foreman (T-W5).

Covers (task step 7):
  * stage inference sequences (tool-call history → stage)
  * interference → fit fix on a two-body doc fixture (real fit_calc numbers)
  * budget rendering drops lowest-severity-first with a never-silent drop line
  * blocker gates stage exit but NOT the next action (freedom rule §9.3)
  * dispute flow (kernel source ⇒ waiver required)
  * the engine §3 invariant: non-info ⇒ non-empty candidate_fixes
  * loop.py injection golden test — the [ADVISORY] block reaches session.messages

Offline: no LLM, no core, no network.  The doc path is driven by an injected
``rpc`` fixture so the two-body fit fix is tested against the REAL fit_calc.
"""
from __future__ import annotations

import pytest

from services.agent.advisor import adapters, engine, render, stages
from services.agent.advisor.engine import Finding, Observation


# ---------------------------------------------------------------------------
# Stage inference
# ---------------------------------------------------------------------------

def test_stage_inference_sequence():
    seq = [
        ("describe_project", "intake"),
        ("req_create", "spec"),
        ("add_datum", "layout"),
        ("describe_sketch", "sketch"),
        ("add_features", "feature"),
        ("verify_battery", "part_verify"),
        ("check_interference", "assembly"),
        ("run_fea_static", "sim"),
        ("cam_generate", "cam"),
        ("export_part", "deliver"),
    ]
    for tool, want in seq:
        assert stages.infer_stage(tool) == want, f"{tool} → {want}"


def test_add_features_sketch_ops_infers_sketch():
    # add_features with only sketch ops ⇒ sketch (docs §2.2)
    ops = [{"op": "sketch_circle"}, {"op": "sketch_line"}]
    assert stages.infer_stage_from_ops("add_features", ops) == "sketch"
    # add_features with a solid op ⇒ feature
    ops2 = [{"op": "sketch_circle"}, {"op": "extrude"}]
    assert stages.infer_stage_from_ops("add_features", ops2) == "feature"


def test_unmapped_tool_keeps_stage():
    assert stages.infer_stage("measure_between") is None


def test_exit_gate_and_battery_defined_per_stage():
    # Every linear stage has its battery == exit_gate shape defined (contract §1).
    for st in ("sketch", "feature", "part_verify", "assembly", "sim", "cam"):
        assert stages.check_battery(st), f"{st} battery empty"
        assert stages.exit_gate(st), f"{st} exit gate empty"


# ---------------------------------------------------------------------------
# Interference → fit fix on a two-body doc fixture (REAL fit_calc)
# ---------------------------------------------------------------------------

def _fake_rpc_two_body_bore_shaft(method, **kwargs):
    """Doc RPC fixture: an interference between a bracket bore and a shaft, with
    measured cylindrical-face diameters — the case where fit_calc fires."""
    if method == "doc_eval_interference":
        return {
            "interferences": [{
                "a": "bracket", "b": "shaft",
                "summary": "bore Ø19.80 vs shaft Ø20.00",
                "volume": 340.0,
                "bore_d": 19.80, "shaft_d": 20.00,
                "feature": "F7", "fit": "H7/g6", "nominal": 20.0,
            }],
        }
    return {}


def test_interference_fit_fix_two_body():
    obs = adapters.observe_doc("doc1", "assembly", rpc=_fake_rpc_two_body_bore_shaft)
    advisory = engine.build_advisory(obs)
    assert len(advisory["items"]) == 1
    item = advisory["items"][0]
    assert item["check"] == "interference"
    assert item["severity"] == "blocker"          # assembly interference gates exit
    fixes = item["candidate_fixes"]
    assert fixes, "interference must carry a candidate fix"
    fix = fixes[0]
    assert fix["action"] == "edit_feature_param"
    assert fix["params"]["feature"] == "F7"
    # Real ISO 286 H7 on Ø20: hole 20.000..20.021 → mid ≈ 20.0105
    assert 20.0 <= fix["params"]["value"] <= 20.021
    assert fix["confidence"] == "table"           # kernel/table numbers, not a guess
    assert "fit_calc" in item["helper_tools"]


def test_interference_loop_path_no_diameters_stays_compliant():
    # Loop path: fit carries volume + names only (no bore/shaft Ø).  Must still
    # produce a NON-EMPTY candidate fix (heuristic) so §3 holds.
    class _Res:
        ok = True
        error = None
        is_assembly = True
        fit = {"interferences": [{"a": "bracket", "b": "shaft", "volume": 340.0}],
               "clearances": []}
    obs = adapters.observe_build(_Res())
    advisory = engine.build_advisory(obs)
    item = advisory["items"][0]
    assert item["candidate_fixes"], "loop interference must still carry a fix"
    assert item["candidate_fixes"][0]["confidence"] == "heuristic"
    assert item["severity"] == "action"           # advise, don't gate (no diameters)
    assert "fit_calc" in item["helper_tools"]      # fit_calc advertised as helper


# ---------------------------------------------------------------------------
# Engine §3 invariant — non-info ⇒ non-empty candidate_fixes
# ---------------------------------------------------------------------------

def test_non_info_requires_candidate_fix_invariant():
    # A finding whose kind has NO fix provider, proposed as action, must be
    # downgraded to info rather than emit an illegal empty-fix action item.
    f = Finding(kind="mystery", check="x", severity="action", finding="unknown")
    advisory = engine.build_advisory(Observation(stage="feature", findings=[f]))
    item = advisory["items"][0]
    assert item["candidate_fixes"] == []
    assert item["severity"] == "info"             # downgraded, not illegal


def test_every_non_info_item_has_fix_across_providers():
    findings = [
        Finding(kind="interference", check="interference", severity="action",
                finding="x", refs={"bodies": ["a", "b"]}, numbers={"overlap_mm3": 5}),
        Finding(kind="overlap", check="feature_overlap", severity="action",
                finding="x", refs={"features": ["F1", "F2"]},
                extra={"classification": "swallowed"}),
        Finding(kind="open_profile", check="open_profile", severity="blocker",
                finding="x", refs={"doc_id": "d", "sketch": "S1"}),
        Finding(kind="rebuild", check="rebuild", severity="action", finding="x"),
    ]
    advisory = engine.build_advisory(Observation(stage="feature", findings=findings))
    for item in advisory["items"]:
        if item["severity"] != "info":
            assert item["candidate_fixes"], f"{item['check']} missing fix"


# ---------------------------------------------------------------------------
# Budget rendering — drops lowest-severity-first, never silent
# ---------------------------------------------------------------------------

def test_render_budget_drops_lowest_severity_first():
    findings = [
        Finding(kind="rebuild", check="rebuild", severity="blocker",
                finding="critical rebuild failure " * 3),
        Finding(kind="overlap", check="feature_overlap", severity="action",
                finding="overlap " * 10, refs={"features": ["F1", "F2"]}),
        Finding(kind="mystery", check="lint", severity="info",
                finding="minor informational note " * 5),
    ]
    advisory = engine.build_advisory(Observation(stage="feature", findings=findings, seq=7))
    # Tiny budget forces drops; blocker must survive, info drops first.
    block = render.render_advisory(advisory, budget_tokens=20)
    assert block.startswith("[ADVISORY seq=7 stage=feature severity=blocker]")
    assert "[blocker]" in block
    assert "dropped for budget" in block          # never-silent drop line (§3)
    assert "lowest=info" in block


def test_render_empty_advisory_is_blank():
    advisory = engine.build_advisory(Observation(stage="feature", findings=[]))
    assert render.render_advisory(advisory) == ""


def test_render_heuristic_shows_suggest_not_fix():
    f = Finding(kind="interference", check="interference", severity="action",
                finding="overlap", refs={"bodies": ["a", "b"]},
                numbers={"overlap_mm3": 5})
    advisory = engine.build_advisory(Observation(stage="assembly", findings=[f]))
    block = render.render_advisory(advisory)
    assert "suggest:" in block                    # heuristic ⇒ suggest, not fact
    assert "[heuristic]" in block


# ---------------------------------------------------------------------------
# Blocker gates stage exit but NOT the next action (§9.3 freedom rule)
# ---------------------------------------------------------------------------

def test_blocker_gates_exit_not_action():
    obs = adapters.observe_doc("doc1", "assembly", rpc=_fake_rpc_two_body_bore_shaft)
    advisory = engine.build_advisory(obs)
    # has_blocker() is the orchestrator's stage-exit signal...
    assert engine.has_blocker(advisory) is True
    # ...but the item is still a plain injected advisory (no control-flow field
    # that would stop the model acting).  The block is informational text.
    block = render.render_advisory(advisory)
    assert "[blocker]" in block
    assert "candidate_fix" not in block.lower() or "fix:" in block  # fix offered, not forced


def test_user_override_downgrades_to_info():
    f = Finding(kind="interference", check="interference", severity="blocker",
                finding="x", refs={"bodies": ["a", "b"]}, numbers={"overlap_mm3": 1})
    advisory = engine.build_advisory(
        Observation(stage="assembly", findings=[f], user_override=True))
    assert all(it["severity"] == "info" for it in advisory["items"])
    block = render.render_advisory(advisory)
    assert "user override active" in block


# ---------------------------------------------------------------------------
# Dispute flow (MCP tool) — kernel source ⇒ waiver required
# ---------------------------------------------------------------------------

def test_advisory_dispute_kernel_requires_waiver(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_CAD_CACHE_ROOT", str(tmp_path))
    from services.mcp import advisor_tools

    captured = {}

    class _MCP:
        def tool(self):
            def deco(fn):
                captured[fn.__name__] = fn
                return fn
            return deco

    advisor_tools.register_advisor_tools(_MCP())
    dispute = captured["advisory_dispute"]

    r_kernel = dispute("r_test", "adv_123", "I measured it myself", source_kind="kernel")
    assert r_kernel["ok"] and r_kernel["waiver_required"] is True

    r_heur = dispute("r_test", "adv_456", "heuristic wrong", source_kind="heuristic")
    assert r_heur["ok"] and r_heur["waiver_required"] is False


def test_stage_set_records_override(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_CAD_CACHE_ROOT", str(tmp_path))
    from services.mcp import advisor_tools

    captured = {}

    class _MCP:
        def tool(self):
            def deco(fn):
                captured[fn.__name__] = fn
                return fn
            return deco

    advisor_tools.register_advisor_tools(_MCP())
    r = captured["stage_set"]("r_test", "sim", "user jumped to analysis")
    assert r["ok"] and r["stage"] == "sim"
    # unknown stage rejected
    bad = captured["stage_set"]("r_test", "nonsense", "x")
    assert not bad["ok"]


# ---------------------------------------------------------------------------
# loop.py injection golden test — [ADVISORY] reaches session.messages
# ---------------------------------------------------------------------------

def test_loop_injects_advisory_block(tmp_path, monkeypatch):
    """A rebuild failure in the loop must prepend an [ADVISORY] block to the next
    user turn (the injection-live-in-loop.py DoD proof).  Mirrors the
    test_live_seam_wiring.py fixture pattern — no LLM, no core."""
    from dataclasses import dataclass, field
    from services.agent import loop
    from services.agent.providers.mock_provider import MockProvider

    @dataclass
    class _FailBuild:
        ok: bool = False
        stl_path = None
        error: str = "BRepCheck: feature F7 failed to rebuild"
        stdout: str = ""
        volume: float = 0.0
        triangles: int = 0
        bbox: dict = field(default_factory=lambda: {"size": [0, 0, 0]})
        is_assembly: bool = False
        parts = None
        fit = None

    # Mock provider: emit a code block so the loop attempts a build (which fails).
    replies = ["```python\nresult = Box(1,1,1)\n```", "```python\nresult = Box(1,1,1)\n```"]
    provider = MockProvider(replies)
    monkeypatch.setattr(loop, "run_code_multi", lambda code, out_dir, name: _FailBuild())
    monkeypatch.setattr(loop.paths, "model_dir", lambda name: tmp_path)
    monkeypatch.setenv("AI_CAD_ADVISOR", "1")
    monkeypatch.setenv("AI_CAD_CACHE_ROOT", str(tmp_path))

    session = loop.Session(name="adv_golden")
    loop.run_turn(session, "a 1mm cube", provider, max_iters=2)

    # Find the [ADVISORY] block in the assembled messages.
    joined = "\n".join(
        p.get("text", "")
        for m in session.messages
        for p in m.get("content", [])
        if p.get("type") == "text"
    )
    assert "[ADVISORY" in joined, "advisory block was not injected into the loop messages"
    assert "rebuild" in joined
    assert "regen_diagnose" in joined            # helper tool advertised


# ---------------------------------------------------------------------------
# dag node runner injection — output_state["advisory"] populated (DoD: both hosts)
# ---------------------------------------------------------------------------

def _fake_overlap_rpc(method, **kw):
    # The design node runs the FEATURE battery ([rebuild, feature_overlap]) — NOT
    # interference — so the fixture must return an overlap to produce a finding.
    if method == "doc_eval_feature_overlap":
        return {"overlaps": [{"summary": "F2 swallowed by F1",
                              "classification": "swallowed",
                              "features": ["F1", "F2"]}]}
    return {}


def test_node_runner_injects_advisory(monkeypatch):
    from services.dag import node_runner
    monkeypatch.setattr(adapters, "_default_rpc", lambda: _fake_overlap_rpc)
    monkeypatch.setenv("AI_CAD_ADVISOR", "1")

    class _R:
        stop_reason = "confirmed"
        success = True
        final_code = None
        result = None

    node = {"id": "n1", "type": "design", "name": "build"}
    ctx = {"node_inputs": {"doc_id": "doc1"}, "retry_remaining": 1}
    verdict, out = node_runner.run(node, ctx, agent_fn=lambda req, cp: _R())
    assert verdict == "done"
    assert "[ADVISORY" in out.get("advisory", ""), "DAG node did not inject an advisory"
    assert "swallowed" in out["advisory"] or "reorder" in out["advisory"]


def test_node_runner_no_doc_no_advisory(monkeypatch):
    # A node without a doc_id must not attach an advisory (no doc to check).
    from services.dag import node_runner
    monkeypatch.setattr(adapters, "_default_rpc", lambda: _fake_overlap_rpc)

    class _R:
        stop_reason = "confirmed"
        success = True
        final_code = None
        result = None

    node = {"id": "n2", "type": "design", "name": "build"}
    ctx = {"node_inputs": {}, "retry_remaining": 1}
    _, out = node_runner.run(node, ctx, agent_fn=lambda req, cp: _R())
    assert "advisory" not in out


# ---------------------------------------------------------------------------
# advise() MCP tool — on-demand battery over a doc
# ---------------------------------------------------------------------------

def test_advise_tool_returns_block(monkeypatch):
    monkeypatch.setattr(adapters, "_default_rpc", lambda: _fake_rpc_two_body_bore_shaft)
    from services.mcp import advisor_tools

    captured = {}

    class _MCP:
        def tool(self):
            def deco(fn):
                captured[fn.__name__] = fn
                return fn
            return deco

    advisor_tools.register_advisor_tools(_MCP())
    r = captured["advise"]("doc1", stage="assembly")
    assert r["ok"] is True
    assert "[ADVISORY" in r["block"]
    assert r["has_blocker"] is True              # interference on the doc path gates exit
    # unknown stage rejected
    bad = captured["advise"]("doc1", stage="nonsense")
    assert not bad["ok"]
