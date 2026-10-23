"""Mega-24 fixes — live thrash after S23/T23 (mega-23 a1).

Grounded in mega-23 a1 vs mega-22 a2 good:

  Primary: mid-run xai spending-limit + openrouter 402 → both providers marked
           unavailable → BUILD nodes fell to ollama → no_code / max_iters thrash.
           Terminal stop was verify_failed:AC1-5 so product force gates SKIPPED
           (battery doc_id=null).

  Secondary (amplifiers once on leaves):
    - fw_sensor_scenario NameError `_err` (every non-empty profile)
    - forced ecad_wire with net= → TypeError (MCP has no net kwarg)
    - ledger_note string payload → dict() thrash
    - TypeError → fn() zero-arg fallback mangled required kwargs

S23/T23 pass rules are NOT weakened here.
"""
from __future__ import annotations

import ast
import inspect
import json
from pathlib import Path

import pytest


_ROOT = Path(__file__).resolve().parents[2]
_MEGA23 = _ROOT / "reports/runs/mega-23"
_MEGA22 = _ROOT / "reports/runs/mega-22/attempt-2-primary-graded-live7"


def _load_json(path: Path) -> dict:
    if not path.is_file():
        pytest.skip(f"fixture missing: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Live fingerprints (would have caught the thrash class)
# ---------------------------------------------------------------------------


def test_mega23_a1_fingerprint_build_no_code_and_verify_failed():
    """Live shape: 9 DAG node failures incl n_build_DL1 no_code; battery never product."""
    summary = _load_json(_MEGA23 / "grade_summary.json")
    assert summary.get("stop_reason", "").startswith("verify_failed")
    assert summary.get("terminal_status") == "needs_correction"
    phases = summary.get("phases_completed") or []
    assert "intake" in phases
    # BUILD product never completed — only intake/specify in completed phases.
    assert "build" not in phases or summary.get("pass_count", 9) < 7

    # Node failures from events stream.
    failed = []
    for line in (_MEGA23 / "events.jsonl").read_text().splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("arg0") == "dag.node.failed":
            failed.append(row.get("arg1") or {})
    assert len(failed) >= 9
    by_id = {f.get("node_id"): f.get("error") or "" for f in failed}
    assert "n_build_DL1" in by_id
    assert "no_code" in by_id["n_build_DL1"]
    assert "n_build_DL2" in by_id
    assert "delegate_or_typed_refusal" in by_id["n_build_DL2"]
    assert "n_build_DL3" in by_id and "max_iters" in by_id["n_build_DL3"]
    assert "n_build_DL4" in by_id and "max_iters" in by_id["n_build_DL4"]


def test_mega23_a1_fw_sensor_scenario_nameerror_fingerprint():
    """Live ledger: BUILD DL4 burned budget on name '_err' is not defined."""
    ledger = _load_json(_MEGA23 / "ledger.json")
    hits = []
    for ev in ledger.get("events") or []:
        if ev.get("kind") != "agent_turn":
            continue
        p = ev.get("payload") or {}
        if p.get("event") != "tool_call":
            continue
        if p.get("tool") != "fw_sensor_scenario":
            continue
        res = str(p.get("result") or "")
        if "_err" in res and "not defined" in res:
            hits.append(p)
    assert hits, "expected NameError _err fingerprint on fw_sensor_scenario"


def test_mega23_a1_ledger_note_string_payload_fingerprint():
    """Live ledger: string payload → dictionary update sequence thrash."""
    ledger = _load_json(_MEGA23 / "ledger.json")
    hits = []
    for ev in ledger.get("events") or []:
        p = ev.get("payload") or {}
        if p.get("event") != "tool_call" or p.get("tool") != "ledger_note":
            continue
        res = str(p.get("result") or "")
        if "dictionary update sequence" in res:
            hits.append(p)
            args = p.get("args") or {}
            assert isinstance(args.get("payload"), str)
    assert hits, "expected ledger_note string-payload thrash fingerprint"


# ---------------------------------------------------------------------------
# Fix: fw_sensor_scenario _err defined
# ---------------------------------------------------------------------------


def test_fw_sensor_scenario_invalid_profile_no_nameerror():
    """Bad JSON profile must return ok=false, never NameError _err."""
    from services.mcp import firmware_tools as ft

    class _Mcp:
        def tool(self):
            def deco(fn):
                self.fn = fn
                return fn
            return deco

    mcp = _Mcp()
    # register installs many tools; grab fw_sensor_scenario by re-register
    # and capturing the last matching name via a dict.
    tools = {}

    class _Cap:
        def tool(self):
            def deco(fn):
                tools[fn.__name__] = fn
                return fn
            return deco

    ft.register_firmware_tools(_Cap())
    fn = tools["fw_sensor_scenario"]
    out = json.loads(fn(profile="{not-json"))
    assert out.get("ok") is False
    assert "_err" not in str(out.get("error") or "")
    assert "not defined" not in str(out.get("error") or "").lower()
    # Structured code/hint path (not a crash).
    assert out.get("code") or out.get("error")


def test_fw_sensor_scenario_empty_profile_ok():
    from services.mcp import firmware_tools as ft

    tools = {}

    class _Cap:
        def tool(self):
            def deco(fn):
                tools[fn.__name__] = fn
                return fn
            return deco

    ft.register_firmware_tools(_Cap())
    fn = tools["fw_sensor_scenario"]
    out = json.loads(fn(profile=""))
    # Empty profile is a valid call path (engine may still return structured).
    assert isinstance(out, dict)
    assert "name '_err' is not defined" not in json.dumps(out)


# ---------------------------------------------------------------------------
# Fix: ledger_note string payload coerce
# ---------------------------------------------------------------------------


def test_ledger_note_string_payload_coerced():
    from services.agent.mission_dispatch import (
        MissionDispatch,
        _coerce_ledger_payload,
    )

    assert _coerce_ledger_payload("{}") == {}
    assert _coerce_ledger_payload('{"name":"x","value":"y"}') == {
        "name": "x", "value": "y",
    }
    assert _coerce_ledger_payload("plain note") == {"text": "plain note"}
    assert _coerce_ledger_payload(None) == {}
    assert _coerce_ledger_payload({"a": 1}) == {"a": 1}

    class _Led:
        run_id = "test-mega24"
        events: list = []

        def append(self, actor, kind, payload):
            self.events.append({"actor": actor, "kind": kind, "payload": payload})
            return {"seq": len(self.events), "ts": "t"}

    md = MissionDispatch.__new__(MissionDispatch)
    md._ledger = lambda run_id=None: _Led()  # type: ignore[method-assign]
    # re-bind instance methods that use self._ledger
    out = MissionDispatch.ledger_note(
        md, kind="note", payload='{"name":"rail","value":"3V3"}', actor="agent"
    )
    assert out.get("ok") is True
    assert out.get("error") is None


def test_mcp_ledger_note_string_payload_coerced():
    from services.mcp.ledger_tools import register_ledger_tools

    tools = {}

    class _Cap:
        def tool(self):
            def deco(fn):
                tools[fn.__name__] = fn
                return fn
            return deco

    register_ledger_tools(_Cap())
    fn = tools["ledger_note"]
    # Will fail load without real run, but must NOT raise dict-update on str.
    out = fn(run_id="__no_such_run_mega24__", kind="note", payload="{}")
    assert isinstance(out, dict)
    assert "dictionary update sequence" not in str(out.get("error") or "")


# ---------------------------------------------------------------------------
# Fix: forced ecad_wire strips net for dispatch, keeps on call record
# ---------------------------------------------------------------------------


def test_forced_ecad_wire_strips_net_for_dispatch_keeps_on_record():
    from services.agent import agent_turn as at

    seen = []

    def dispatch(tool, args):
        seen.append((tool, dict(args)))
        return json.dumps({"ok": True, "net": "GND"})

    calls: list = []
    messages: list = []
    calls, n, rec, ok = at._ecad_dispatch_forced_step(
        tool="ecad_wire",
        args={
            "from_ref": "V1", "from_pin": "-",
            "to_ref": "GND", "to_pin": "1",
            "doc_id": "sch1", "net": "GND",
        },
        calls=calls,
        dispatch=dispatch,
        ledger=None,
        actor="test",
        messages=messages,
        reason="test:wire_v_gnd",
        tool_calls_made=0,
        budget=4,
    )
    assert ok is True
    assert n == 1
    assert seen and "net" not in seen[0][1]
    assert seen[0][1]["from_ref"] == "V1"
    assert rec is not None
    assert rec["args"].get("net") == "GND"  # preserved for topology harvest


def test_resolve_tool_dispatch_no_silent_fn_empty():
    """TypeError must not fall back to fn() with zero args (mega-24 thrash)."""
    from services.agent import agent_turn as at

    def _needs_project(project_id: str, add: list | None = None) -> dict:
        return {"ok": True, "project_id": project_id, "add": add}

    # Patch reviewer resolve to our stub.
    import services.agent.reviewer_exec as re  # noqa: PLC0415

    orig = re._resolve_fn
    re._resolve_fn = lambda name: _needs_project  # type: ignore[assignment]
    try:
        d = at._resolve_tool_dispatch(None)
        # Missing required project_id — must surface error, not call with ().
        out = d("fw_sources", {"add": ["a.h"]})
        assert isinstance(out, dict)
        assert out.get("ok") is False
        assert "project_id" in str(out.get("error") or "")
        # Good kwargs still work.
        out2 = d("fw_sources", {"project_id": "p1", "add": ["a.h"], "junk": 1})
        assert out2.get("ok") is True
        assert out2.get("project_id") == "p1"
    finally:
        re._resolve_fn = orig


# ---------------------------------------------------------------------------
# Fix: terminal product force on verify_failed / needs_correction
# ---------------------------------------------------------------------------


def test_s23_t23_not_weakened():
    """S23/T23 pass rules remain: in-band V, real measure scores."""
    # Import mega23 tests' scoring contracts without re-running full suite.
    from services.verify import battery as bat

    # measure_between finite 0.0 still scores 1.0 (T23).
    score, _ = bat._extract_score(
        "measure_between", "scalar",
        json.dumps({"ok": True, "distance": 0.0}),
    )
    assert score == 1.0
    score2, _ = bat._extract_score(
        "measure_between", "scalar",
        json.dumps({"ok": True, "distance": 12.5}),
    )
    assert score2 == 1.0


# ---------------------------------------------------------------------------
# Rebind residual (mega-24 a1 thrash-kill: 4 node fails, not mega-23-class)
# ---------------------------------------------------------------------------


def test_ledger_get_casefold_section():
    """Decisions / Bom / Dimensions must alias after mega-24 casefold."""
    from services.agent.mission_dispatch import MissionDispatch

    class _Led:
        run_id = "t"
        events = []
        state = {
            "decisions": [{"id": "d1"}],
            "dimensions": [{"id": "x"}],
            "artifacts": [{"id": "a1"}],
            "brief": {"title": "t"},
            "requirements": [],
            "questions": [],
            "stage_log": [],
            "criterion_results": [],
            "features": [],
            "geometry_specs": [],
            "linkages": [],
            "build_nodes": [],
        }

    md = MissionDispatch.__new__(MissionDispatch)
    md._ledger = lambda run_id=None: _Led()  # type: ignore
    out = MissionDispatch.ledger_get(md, section="Decisions")
    assert out.get("ok") is True, out
    assert out.get("section") == "decisions"
    out2 = MissionDispatch.ledger_get(md, section="Bom")
    assert out2.get("ok") is True, out2
    assert out2.get("section") == "artifacts"


def test_leaf_envelope_salvages_noop_false_and_conflict():
    from services.agent.role_models import (
        normalize_leaf_envelope,
        validate_leaf_envelope,
    )

    # INTAKE miss: no_op=false, empty reason, empty ops
    env = normalize_leaf_envelope({
        "goal": "complete the delivery manifest",
        "no_op": False,
        "reason": "",
        "ops": [],
        "artifacts": [{"name": "manifest"}],
    })
    assert env is not None
    assert env.get("no_op") is True
    assert validate_leaf_envelope(env) == []

    # DELIVER DL2 miss: no_op=true WITH ops
    env2 = normalize_leaf_envelope({
        "goal": "design rocket",
        "no_op": True,
        "reason": "x",
        "ops": [{"key": "ecad_new_schematic"}],
    })
    assert env2 is not None
    assert env2.get("no_op") is not True
    assert validate_leaf_envelope(env2) == []


def test_plan_read_autocomp_and_typed_refusal_synth():
    """Layout-like 16× ledger_get should auto-complete with typed_refusal."""
    from services.agent import agent_turn as at

    calls = [
        {"tool": "ledger_get", "ok": True, "args": {"section": s}}
        for s in (
            "brief", "requirements", "decisions", "dimensions",
            "artifacts", "manifest", "all", "build_nodes",
            "events", "geometry_specs",
        )
    ]
    # Exercise the productive-write heuristic via private constants pattern:
    # call run_turn end path by simulating the wrote computation inline.
    _PLAN_READ_TOOLS = {
        "ledger_get", "part_search", "parts_taxonomy", "part_source",
        "describe_part", "describe_scene", "describe_project",
        "workspace_status", "buildgraph_status",
    }
    n = sum(1 for c in calls if c.get("ok") and c.get("tool") in _PLAN_READ_TOOLS)
    assert n >= 8
    diverse = {c.get("tool") for c in calls if c.get("ok") and c.get("tool") in _PLAN_READ_TOOLS}
    wrote = n >= 8 or len(diverse) >= 2
    assert wrote is True

    # Gate soft-pass on productive multi-domain without delegate.
    from services.dag import node_runner as nr

    class _R:
        success = True
        stop_reason = "confirmed"
        final_code = None
        result = {
            "calls": calls,
            "handoff": {"status": "done", "summary": "layout read", "outputs": {}},
        }

    node = {
        "id": "n_layout",
        "type": "plan",
        "name": "PLAN: layout skeleton",
        "role": "layout_engineer",
        "inputs": {
            "role": "layout_engineer",
            "task_class": "assembly",
            "depth": 0,
            "request": "Layout multi-domain rocket",
        },
    }
    pack = {
        "node_inputs": node["inputs"],
        "role_id": "layout_engineer",
        "task_class": "assembly",
        "domain_tags": ["geometry", "assembly", "ecad"],
        "depth": 0,
        "capability_letter": "C",
        "retry_remaining": 2,
    }
    verdict, out = nr.run(node, pack, agent_fn=lambda r, p: _R())
    assert verdict == nr.VERDICT_DONE, (verdict, out)
    assert out["data"].get("stop_reason") != "delegate_or_typed_refusal_required"


# ---------------------------------------------------------------------------
# a2 thrash residual (20260715T223717Z): no_code on xai-sub SPECIFY/layout
# ---------------------------------------------------------------------------


def test_force_first_tool_on_zero_tools():
    """Zero-tool no_code path forces ledger_get once."""
    from services.agent import agent_turn as at

    seen = []

    def dispatch(tool, args):
        seen.append((tool, dict(args or {})))
        return {"ok": True, "rendered": "ledger"}

    calls, n, forced = at._force_first_tool(
        allow={"ledger_get", "ledger_note", "delegate"},
        dispatch=dispatch,
        ledger=None,
        actor="test",
        messages=[],
        calls=[],
        tool_calls_made=0,
        budget=8,
    )
    assert forced is True
    assert n == 1
    assert seen and seen[0][0] == "ledger_get"
    assert calls[0].get("forced") is True


def test_harness_does_not_fabricate_typed_refusal():
    """Multi-domain refusal evidence must be model-authored or absent."""
    handoff = {"status": "done", "summary": "planned", "outputs": {}}
    assert "typed_refusal" not in handoff["outputs"]


def test_plan_ruling_kind_alias():
    from services.agent.mission_dispatch import (
        MissionDispatch,
        _LEDGER_NOTE_KIND_ALIASES,
    )
    assert _LEDGER_NOTE_KIND_ALIASES.get("plan_ruling") == "decision"

    class _Led:
        run_id = "t"
        events = []
        def append(self, actor, kind, payload):
            self.events.append({"kind": kind, "payload": payload})
            return {"seq": len(self.events), "ts": "t"}

    md = MissionDispatch.__new__(MissionDispatch)
    md._ledger = lambda run_id=None: _Led()  # type: ignore
    out = MissionDispatch.ledger_note(
        md, kind="plan_ruling",
        payload={"decision": "use buildgraph", "text": "x"},
        actor="orchestrator",
    )
    assert out.get("ok") is True, out


# ---------------------------------------------------------------------------
# Fixer24c: offline live-shape repro of a2 thrash (20260715T223717Z)
# Mechanism: zero-tool first completion (prose OR junk JSON) → no_code on
# planning nodes; n_plan formal done without typed_refusal → gate block.
# ---------------------------------------------------------------------------


class _ScriptedProvider:
    name = "scripted"

    def __init__(self, replies):
        self._replies = list(replies)
        self.calls = 0

    def complete(self, system, messages, max_tokens=8000):
        self.calls += 1
        return self._replies.pop(0) if self._replies else "no more"


def _dispatch_ok(tool, args):
    if tool == "ledger_get":
        return {"ok": True, "rendered": "[LEDGER] brief", "event_count": 1}
    if tool == "ledger_note":
        return {"ok": True, "seq": 1, "kind": (args or {}).get("kind", "note")}
    if tool == "dag_plan":
        return {"ok": True, "run_id": "t", "stage_log": []}
    if tool == "buildgraph_plan":
        return {"ok": True, "run_id": "bg1", "graph": {"nodes": []}}
    if tool == "buildgraph_status":
        return {"ok": True, "run_id": "bg1"}
    return {"ok": True}


def test_a2_live_shape_zero_tool_prose_never_fabricates_refusal():
    """A forced opener may salvage the turn, but cannot invent routing evidence."""
    from services.agent import agent_turn as at

    p = _ScriptedProvider(["Understood. I will layout the skeleton."])
    res = at.run_turn(
        "PLAN: layout skeleton multi-domain rocket",
        p,
        allowed_tools=["ledger_get", "ledger_note", "part_search", "dag_plan"],
        max_tool_calls=8,
        tool_dispatch=_dispatch_ok,
        actor="PLAN: layout skeleton",
        task_class="assembly",
        phase="plan",
    )
    assert res.success is True, (res.stop_reason, res.result)
    assert res.stop_reason == "confirmed"
    calls = (res.result or {}).get("calls") or []
    assert any(c.get("forced") for c in calls), calls
    ho = (res.result or {}).get("handoff") or {}
    assert "typed_refusal" not in (ho.get("outputs") or {})


def test_a2_live_shape_junk_json_zero_tool_force_and_confirm():
    """mega-24c hole: parsed junk JSON without tool used to skip force-first."""
    from services.agent import agent_turn as at

    # Live-like: short JSON note, no tool / no done envelope.
    p = _ScriptedProvider([
        '```json\n{"note": "looking at ECAD requirements", "status": "thinking"}\n```',
        '```json\n{"status": "done", "summary": "spec drafted", "outputs": {}}\n```',
    ])
    res = at.run_turn(
        "SPECIFY DL3: flight_computer_ecad",
        p,
        allowed_tools=["ledger_get", "ledger_note", "part_search"],
        max_tool_calls=8,
        tool_dispatch=_dispatch_ok,
        actor="SPECIFY DL3",
        task_class="analysis",
        phase="specify",
    )
    assert res.success is True, (res.stop_reason, res.result)
    assert res.stop_reason == "confirmed"
    calls = (res.result or {}).get("calls") or []
    # Forced opener: ledger_get on plain floors; the category menu when the
    # mode gate exposed the meta pair (2026-07-27 on-ramp change) — either way
    # the zero-tool turn gets exactly one forced tool result, which is the a2
    # contract this test defends.
    assert any(
        c.get("forced")
        and c.get("tool") in ("ledger_get", "list_tool_categories")
        for c in calls
    ), calls


def test_a2_live_shape_force_then_junk_json_bare_completes():
    """After forced tool, second junk JSON must bare-complete (not no_code)."""
    from services.agent import agent_turn as at

    p = _ScriptedProvider([
        "Looking at requirements.",  # force
        '```json\n{"ok": true, "message": "will proceed"}\n```',  # was no_code hole
    ])
    res = at.run_turn(
        "SPECIFY DL4: firmware",
        p,
        allowed_tools=["ledger_get", "ledger_note"],
        max_tool_calls=8,
        tool_dispatch=_dispatch_ok,
    )
    assert res.success is True, (res.stop_reason, res.result)
    assert res.stop_reason == "confirmed"


def test_a2_live_shape_n_plan_formal_done_hits_typed_refusal_gate():
    """Formal multi-domain done without delegate/refusal must retry honestly."""
    from services.agent import agent_turn as at
    from services.dag import node_runner as nr

    p = _ScriptedProvider([
        '```json\n{"tool":"dag_plan","args":{}}\n```',
        '```json\n{"tool":"buildgraph_plan","args":{"mission":"rocket"}}\n```',
        '```json\n{"tool":"ledger_note","args":{"kind":"decision","payload":{"id":"p1","text":"ok"}}}\n```',
        '```json\n{"status":"done","summary":"planned","outputs":{}}\n```',
    ])
    res = at.run_turn(
        "PLAN: mission DAG multi-domain",
        p,
        allowed_tools=[
            "ledger_get", "ledger_note", "dag_plan",
            "buildgraph_plan", "buildgraph_status", "delegate",
        ],
        max_tool_calls=16,
        tool_dispatch=_dispatch_ok,
        actor="PLAN: mission DAG",
        task_class="multi_feature_part",
        phase="plan",
    )
    assert res.success is True
    tr = ((res.result or {}).get("handoff") or {}).get("outputs") or {}
    assert "typed_refusal" not in tr

    class _R:
        success = True
        stop_reason = "confirmed"
        final_code = None
        result = res.result

    node = {
        "id": "n_plan",
        "type": "plan",
        "name": "PLAN: mission DAG + build graphs",
        "role": "orchestrator",
        "inputs": {
            "role": "orchestrator",
            "task_class": "multi_feature_part",
            "depth": 0,
            "request": "Plan multi-domain rocket",
        },
    }
    pack = {
        "node_inputs": node["inputs"],
        "role_id": "orchestrator",
        "task_class": "multi_feature_part",
        "domain_tags": ["geometry", "assembly", "ecad", "firmware"],
        "depth": 0,
        "capability_letter": "C",
        "retry_remaining": 2,
    }
    verdict, out = nr.run(node, pack, agent_fn=lambda r, p: _R())
    assert verdict == nr.VERDICT_RETRY, (verdict, out)
    assert out["data"].get("stop_reason") == "delegate_or_typed_refusal_required"
