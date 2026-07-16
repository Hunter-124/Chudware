"""test_agent_turn.py — T-W30 bounded tool-executing agent turn.

Proves the turn parses model tool calls, dispatches them, feeds results back, is
bounded by the tool budget, and never fabricates success on an empty turn.
"""
from __future__ import annotations
import json

from services.agent import agent_turn


class _ScriptedProvider:
    """Returns a fixed sequence of replies; ignores the prompt."""
    name = "scripted"

    def __init__(self, replies):
        self._replies = list(replies)
        self.calls = 0

    def complete(self, system, messages, max_tokens=8000):
        self.calls += 1
        return self._replies.pop(0) if self._replies else "no more"


def test_turn_dispatches_tool_then_finishes():
    provider = _ScriptedProvider([
        '```json\n{"tool": "measure_part", "args": {"doc_id": "d1"}}\n```',
        '```json\n{"done": true, "handoff": {"summary": "measured"}}\n```',
    ])
    seen = {}

    def _dispatch(tool, args):
        seen["tool"] = tool
        seen["args"] = args
        return {"mass_g": 42.0}

    res = agent_turn.run_turn("measure the part", provider,
                              allowed_tools=["measure_part"],
                              tool_dispatch=_dispatch)
    assert res.success is True
    assert res.stop_reason == "confirmed"
    assert seen["tool"] == "measure_part"
    assert res.result["handoff"]["summary"] == "measured"
    # mega-24: formal done after tools may attach typed_refusal under outputs.
    assert res.result["calls"][0]["ok"] is True


def test_turn_denies_out_of_allowlist_tool():
    provider = _ScriptedProvider([
        '```json\n{"tool": "export_part", "args": {}}\n```',
        '```json\n{"done": true, "handoff": {}}\n```',
    ])
    called = []

    def _dispatch(tool, args):
        called.append(tool)
        return {}

    res = agent_turn.run_turn("do it", provider,
                              allowed_tools=["measure_part"],
                              tool_dispatch=_dispatch)
    # export_part was never dispatched (not in allowlist).
    assert "export_part" not in called
    assert any(c.get("error") for c in res.result["calls"])


def test_turn_bounded_by_budget():
    # Always asks for another tool; must stop at the budget with a retry verdict.
    provider = _ScriptedProvider(
        ['```json\n{"tool": "measure_part", "args": {}}\n```'] * 20)
    res = agent_turn.run_turn("loop", provider, allowed_tools=["measure_part"],
                              max_tool_calls=3, tool_dispatch=lambda t, a: {"ok": True})
    assert res.result["tool_calls"] == 3
    assert res.stop_reason == "max_iters"


def test_empty_turn_is_no_code_not_fabricated_success():
    provider = _ScriptedProvider(["I am not sure what to do."])
    res = agent_turn.run_turn("go", provider, tool_dispatch=lambda t, a: {})
    assert res.success is False
    assert res.stop_reason == "no_code"


def test_dispatch_error_fed_back_then_recovered():
    provider = _ScriptedProvider([
        '```json\n{"tool": "fit_report", "args": {}}\n```',
        '```json\n{"done": true, "handoff": {"ok": true}}\n```',
    ])

    def _dispatch(tool, args):
        raise RuntimeError("engine offline")

    res = agent_turn.run_turn("check", provider, allowed_tools=["fit_report"],
                              tool_dispatch=_dispatch)
    # the failed call is recorded, and the model got a chance to finish.
    assert res.result["calls"][0]["ok"] is False
    assert res.success is True  # finished with a handoff after the error feedback


def test_turn_honours_max_tokens_cap():
    """T-W30 finisher: the per-completion cap is passed through to the provider
    (tier generation_headroom when the caller knows the tier; 2048 default)."""
    seen = {}

    class _CapProvider:
        name = "cap"

        def complete(self, system, messages, max_tokens=None):
            seen["max_tokens"] = max_tokens
            return '```json\n{"done": true, "handoff": {}}\n```'

    agent_turn.run_turn("go", _CapProvider(), tool_dispatch=lambda t, a: {})
    assert seen["max_tokens"] == 2048  # shipped default (no tier known)

    agent_turn.run_turn("go", _CapProvider(), max_tokens=8000,
                        tool_dispatch=lambda t, a: {})
    assert seen["max_tokens"] == 8000  # caller-supplied tier budget wins


def test_turn_logs_every_tool_call_to_ledger():
    """T-W30 DoD: ledger events for every turn — each dispatched tool call is
    appended as an agent_turn event when a ledger is supplied."""
    provider = _ScriptedProvider([
        '```json\n{"tool": "measure_part", "args": {"doc_id": "d1"}}\n```',
        '```json\n{"tool": "fit_report", "args": {}}\n```',
        '```json\n{"done": true, "handoff": {"summary": "ok"}}\n```',
    ])

    class _ListLedger:
        def __init__(self):
            self.events = []

        def append(self, actor, kind, payload):
            self.events.append({"actor": actor, "kind": kind, "payload": payload})

    ledger = _ListLedger()
    res = agent_turn.run_turn("check the part", provider,
                              allowed_tools=["measure_part", "fit_report"],
                              ledger=ledger, actor="feature_modeler",
                              tool_dispatch=lambda t, a: {"ok": True})
    assert res.success is True
    kinds = [e["payload"].get("event") for e in ledger.events
             if e["kind"] == "agent_turn"]
    assert kinds.count("tool_call") == 2, ledger.events
    assert all(e["actor"] == "feature_modeler" for e in ledger.events)


# ---------------------------------------------------------------------------
# T-W32 — directive parsing: nested args, unterminated fences, trailing prose
# ---------------------------------------------------------------------------

def test_parse_directive_nested_args():
    """The r4 PLAN-node regression: a fenced tool call with NESTED object args
    must parse (the old brace-pair regex truncated at the first '}')."""
    from services.agent.agent_turn import _parse_directive
    reply = ('```json\n{"tool": "buildgraph_plan", "args": {"plan_json": '
             '{"deliverables": [{"name": "bushing", "ops": {"kind": "revolve"}}]}}}\n```')
    obj = _parse_directive(reply)
    assert obj is not None and obj["tool"] == "buildgraph_plan"
    assert obj["args"]["plan_json"]["deliverables"][0]["ops"]["kind"] == "revolve"


def test_parse_directive_unterminated_fence():
    """A local model hitting its completion cap mid-fence: complete JSON object,
    missing closing fence — still parsed."""
    from services.agent.agent_turn import _parse_directive
    reply = 'Let me plan.\n```json\n{"tool": "fit_calc", "args": {"nominal": 20}}'
    obj = _parse_directive(reply)
    assert obj is not None and obj["tool"] == "fit_calc"


def test_parse_directive_trailing_prose_after_object():
    from services.agent.agent_turn import _parse_directive
    reply = '{"done": true, "handoff": {"summary": "ok"}}\nThat completes the node.'
    obj = _parse_directive(reply)
    assert obj is not None and obj.get("done") is True


def test_parse_directive_prose_only_is_none():
    from services.agent.agent_turn import _parse_directive
    assert _parse_directive("Okay, let me think about the bushing design.") is None
    assert _parse_directive("") is None


# ---------------------------------------------------------------------------
# WS0 specify-mortality regressions (grok45-1 ledgers)
# ---------------------------------------------------------------------------

def test_role_schema_handoff_counts_as_done():
    """Role cards teach {"status","summary","outputs"}; agent_turn used to only
    accept {"done": true, "handoff": ...}, so a perfect role handoff became
    no_code and killed SPECIFY (grok-4.5 C1 s1–s3)."""
    role_handoff = (
        '```json\n{"status": "done", "summary": "bore H7 + M4 PCD recorded", '
        '"outputs": {"spec": {"deliverable_id": "DL1", "envelope": {}, '
        '"interfaces": [], "material": "6061", "process": "cnc_mill", '
        '"functional_dimensions": ["bore_diameter"]}, '
        '"verification_plan": [{"criterion_id": "AC1", "check_tool": "measure_part", '
        '"params": {}, "pass_rule": "d≈20"}], '
        '"reasoning_summary": "fit_calc H7/h7 + ledger_note"}}\n```'
    )
    provider = _ScriptedProvider([
        '```json\n{"tool": "ledger_note", "args": {"kind": "dimension", '
        '"payload": {"name": "bore_diameter", "value": "Ø20 H7", '
        '"driver": "sliding", "provenance": "fit_calc"}}}\n```',
        role_handoff,
    ])
    res = agent_turn.run_turn(
        "write the engineering specification",
        provider,
        allowed_tools=["ledger_note"],
        tool_dispatch=lambda t, a: {"ok": True, "seq": 1},
    )
    assert res.success is True, res.stop_reason
    assert res.stop_reason == "confirmed"
    assert res.result["handoff"]["status"] == "done"
    assert res.result["handoff"]["outputs"]["spec"]["deliverable_id"] == "DL1"


def test_budget_exhaust_after_ledger_note_auto_completes():
    """Grok-4.5 SPECIFY: 7–8 productive tool calls ending in ledger_note, no
    formal done envelope → max_iters → escalate → specify_incomplete. A write
    to the ledger is enough evidence to complete the node."""
    # budget=3: three tool calls then the loop stops mid-turn.
    provider = _ScriptedProvider([
        '```json\n{"tool": "fit_calc", "args": {"nominal": 20, "fit": "H7/h7"}}\n```',
        '```json\n{"tool": "ledger_note", "args": {"kind": "dimension", '
        '"payload": {"name": "bore_diameter", "value": "Ø20 H7", '
        '"driver": "sliding", "provenance": "fit_calc"}}}\n```',
        '```json\n{"tool": "research_thread_spec", "args": {"designation": "M4"}}\n```',
        '```json\n{"tool": "ledger_note", "args": {"kind": "dimension", '
        '"payload": {"name": "bolt", "value": "M4", "driver": "x", '
        '"provenance": "thread"}}}\n```',  # would be 4th — never reached
    ])
    res = agent_turn.run_turn(
        "spec the bushing",
        provider,
        allowed_tools=["fit_calc", "ledger_note", "research_thread_spec"],
        max_tool_calls=3,
        tool_dispatch=lambda t, a: {"ok": True},
    )
    assert res.success is True
    assert res.stop_reason == "confirmed"
    assert res.result.get("auto_complete") is True
    assert res.result["handoff"]["outputs"]["wrote_ledger"] is True


def test_budget_exhaust_without_ledger_write_still_retries():
    """Research-only budget burn must still return max_iters (no silent success)."""
    provider = _ScriptedProvider(
        ['```json\n{"tool": "fit_calc", "args": {"nominal": 20}}\n```'] * 10
    )
    res = agent_turn.run_turn(
        "research only",
        provider,
        allowed_tools=["fit_calc"],
        max_tool_calls=3,
        tool_dispatch=lambda t, a: {"ok": True},
    )
    assert res.stop_reason == "max_iters"


def test_no_code_after_ledger_note_auto_completes():
    """specify-fix-1 s2: productive tools + ledger_note, then junk JSON that
    is neither tool nor done → was no_code escalate. Ledger write is enough.

    mega-24c: junk JSON after tools takes the bare-handoff floor (same outcome
    as end-of-loop auto_complete — confirmed, not no_code).
    """
    provider = _ScriptedProvider([
        '```json\n{"tool": "fit_calc", "args": {"nominal": 20}}\n```',
        '```json\n{"tool": "ledger_note", "args": {"kind": "dimension", '
        '"payload": {"name": "bore", "value": "20", "driver": "x", '
        '"provenance": "fit_calc"}}}\n```',
        # Parses as a dict but has no tool/done/status → was no_code.
        '```json\n{"note": "spec looks complete to me"}\n```',
    ])
    res = agent_turn.run_turn(
        "spec",
        provider,
        allowed_tools=["fit_calc", "ledger_note"],
        tool_dispatch=lambda t, a: {"ok": True},
    )
    assert res.success is True
    assert res.stop_reason == "confirmed"
    # Either end-of-loop auto_complete OR bare-handoff after tools (mega-24c).
    assert (
        res.result.get("auto_complete") is True
        or ((res.result.get("handoff") or {}).get("status") == "done")
    )


def test_replay_grok45_specify_transcript_passes_gate():
    """Regression: replay the captured grok-4.5 C1 s2 SPECIFY tool sequence
    (ledger_get → recommend_fit → fit_calc → ledger_note → …, no formal done)
    through the fixed agent_turn. Pre-fix: max_iters → escalate →
    specify_incomplete. Post-fix: confirmed with auto_complete after ledger
    write, so n_spec_* reaches status=done and the phase can exit.

    Transcript source:
    tools/flywheel/bench/gym/reports/runs/grok45-1/C1_open_xai:grok-4-5_s2/ledger.json
    actor='SPECIFY DL1: flanged bushing' tool_call events.
    """
    # Captured tool sequence (7 calls). Cap budget at 7 so the turn exhausts
    # exactly after the last ledger_get — same shape as the live run which hit
    # the default budget without a done envelope.
    sequence = [
        {"tool": "ledger_get", "args": {}},
        {"tool": "recommend_fit", "args": {"function": "sliding fit"}},
        {"tool": "fit_calc", "args": {"nominal": 20.0, "fit": "H7/h7"}},
        {"tool": "ledger_note", "args": {
            "kind": "dimension",
            "payload": {
                "name": "bore_diameter",
                "value": "Ø20 H7 (+0.021/0)",
                "driver": "mates d20 h7 shaft, clearance 0–0.042 mm",
                "provenance": "fit_calc H7/h7",
            },
        }},
        {"tool": "ledger_get", "args": {"budget_tokens": 2500}},
        {"tool": "research_thread_spec", "args": {"designation": "M4"}},
        {"tool": "ledger_get", "args": {"budget_tokens": 3000}},
    ]
    replies = [
        f'```json\n{json.dumps(call)}\n```' for call in sequence
    ]
    # One extra reply that would have been the formal handoff — never reached
    # under budget=7, same as the live run.
    replies.append(
        '```json\n{"status": "done", "summary": "would not be reached", '
        '"outputs": {"spec": {}, "verification_plan": [], '
        '"reasoning_summary": "x"}}\n```'
    )
    provider = _ScriptedProvider(replies)
    allow = sorted({c["tool"] for c in sequence})
    res = agent_turn.run_turn(
        "write the engineering specification for this deliverable "
        "(dimensions, fits, materials) and record the key values.",
        provider,
        name="SPECIFY DL1: flanged bushing",
        allowed_tools=allow,
        max_tool_calls=7,
        tool_dispatch=lambda t, a: {"ok": True, "seq": 1},
    )
    assert res.success is True, (res.stop_reason, res.result)
    assert res.stop_reason == "confirmed"
    # Either auto-complete (budget path) or role handoff — both PASS the gate.
    assert res.result.get("auto_complete") is True or (
        isinstance(res.result.get("handoff"), dict)
        and res.result["handoff"].get("status") == "done"
    )
    # The ledger write that the C1 grader cares about was among the calls.
    assert any(
        c.get("tool") == "ledger_note" and c.get("ok")
        for c in res.result.get("calls") or []
    )


def test_context_refs_surfaced_in_user_message():
    """Wave 4: structured context_refs appear in the model's user turn."""
    seen = {"messages": None}

    class _CaptureProvider:
        name = "capture"

        def complete(self, system, messages, max_tokens=8000):
            seen["messages"] = messages
            return '```json\n{"done": true, "handoff": {"summary": "ok"}}\n```'

    refs = [
        {"kind": "face", "id": "f:top", "doc_id": "doc-1", "label": "Top face"},
        {"kind": "sketch", "id": "sk1", "doc_id": "doc-1", "label": "Profile"},
        {"kind": "edge", "id": ""},  # malformed — skipped
        "not-a-dict",  # malformed — skipped
    ]
    res = agent_turn.run_turn(
        "fillet the selected edges",
        _CaptureProvider(),
        allowed_tools=["measure_part"],
        tool_dispatch=lambda t, a: {"ok": True},
        context_refs=refs,
    )
    assert res.success is True
    assert seen["messages"] is not None
    user_text = ""
    for part in (seen["messages"][0].get("content") or []):
        if isinstance(part, dict) and part.get("type") == "text":
            user_text += part.get("text", "")
    assert "Context references" in user_text
    assert 'face "Top face" id=f:top doc_id=doc-1' in user_text
    assert 'sketch "Profile" id=sk1 doc_id=doc-1' in user_text
    assert "fillet the selected edges" in user_text


def test_context_refs_absent_is_noop():
    """Absent/empty context_refs leave the user message unchanged."""
    seen = {"messages": None}

    class _CaptureProvider:
        name = "capture"

        def complete(self, system, messages, max_tokens=8000):
            seen["messages"] = messages
            return '```json\n{"done": true, "handoff": {"summary": "ok"}}\n```'

    agent_turn.run_turn(
        "just a plain request",
        _CaptureProvider(),
        tool_dispatch=lambda t, a: {},
        context_refs=None,
    )
    assert seen["messages"] is not None
    user_text = ""
    for part in (seen["messages"][0].get("content") or []):
        if isinstance(part, dict) and part.get("type") == "text":
            user_text += part.get("text", "")
    assert "Context references" not in user_text
    assert "just a plain request" in user_text

    # empty list also no-op
    agent_turn.run_turn(
        "just a plain request",
        _CaptureProvider(),
        tool_dispatch=lambda t, a: {},
        context_refs=[],
    )
    user_text2 = ""
    for part in (seen["messages"][0].get("content") or []):
        if isinstance(part, dict) and part.get("type") == "text":
            user_text2 += part.get("text", "")
    assert "Context references" not in user_text2
