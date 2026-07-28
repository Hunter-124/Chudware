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
    # add_features is on no default floor (shared/agent/subagent), so it must
    # still be denied when the caller's allowlist omits it. (export_part moved
    # onto shared_default_tools, which now survives agent-mode narrowing.)
    provider = _ScriptedProvider([
        '```json\n{"tool": "add_features", "args": {}}\n```',
        '```json\n{"done": true, "handoff": {}}\n```',
    ])
    called = []

    def _dispatch(tool, args):
        called.append(tool)
        return {}

    res = agent_turn.run_turn("do it", provider,
                              allowed_tools=["measure_part"],
                              tool_dispatch=_dispatch)
    # add_features was never dispatched (not in allowlist, not on a floor).
    assert "add_features" not in called
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
        turns = 0

        def complete(self, system, messages, max_tokens=8000):
            if seen["messages"] is None:
                seen["messages"] = messages
            self.__class__.turns += 1
            # An EDIT request that finishes with zero tool calls is an unearned
            # finish (the harness refuses it), so do the work then hand off —
            # this test is about context_refs reaching the user turn, not about
            # the finish gate.
            if self.__class__.turns == 1:
                return '```json\n{"tool": "measure_part", "args": {}}\n```'
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



def test_provider_error_carries_stop_reason_and_error_detail():
    """Early-stop provider failures must surface scrubbed error_detail for the GUI."""
    class Boom(Exception):
        status_code = 401

    class Prov:
        name = "xai"

        def complete(self, system, messages, max_tokens=8000):
            raise Boom("Incorrect API key provided: sk-secret-ABC123XYZ")

    res = agent_turn.run_turn("go", Prov(), tool_dispatch=lambda t, a: {})
    assert res.success is False
    assert res.stop_reason == "provider_auth"
    detail = res.result.get("error_detail") or ""
    assert "xai" in detail
    assert "HTTP 401" in detail
    assert "Incorrect API key" in detail
    # secrets must not leak into the GUI-facing field
    assert "sk-secret" not in detail
    assert "ABC123XYZ" not in detail
    assert res.result.get("stop_reason") == "provider_auth"


def test_no_code_and_budget_carry_error_detail():
    empty = agent_turn.run_turn(
        "go", _ScriptedProvider(["I am not sure what to do."]),
        tool_dispatch=lambda t, a: {},
    )
    assert empty.success is False
    assert empty.stop_reason == "no_code"
    assert empty.result.get("error_detail")

    budget = agent_turn.run_turn(
        "loop",
        _ScriptedProvider(
            ['```json\n{"tool": "measure_part", "args": {}}\n```'] * 20),
        allowed_tools=["measure_part"],
        max_tool_calls=3,
        tool_dispatch=lambda t, a: {"ok": True},
    )
    assert budget.stop_reason == "max_iters"
    assert budget.result.get("error_detail")
    assert "call" in budget.result["error_detail"].lower()


def test_provider_raises_not_budget_exhausted():
    """A provider exception must surface as provider_error with real detail.

    Regression: instant API failures (e.g. invalid Anthropic effort body) were
    counted as stage retries and shown as 'budget exhausted (N)' on phase cards.
    """
    class Boom(Exception):
        status_code = 400
        body = {
            "type": "error",
            "error": {
                "type": "invalid_request_error",
                "message": "This model does not support the effort parameter.",
            },
        }

    class Prov:
        name = "anthropic"

        def complete(self, system, messages, max_tokens=8000):
            raise Boom(
                "Error code: 400 - {'type': 'error', 'error': "
                "{'type': 'invalid_request_error', "
                "'message': 'This model does not support the effort parameter.'}}"
            )

    res = agent_turn.run_turn(
        "make a 48tooth sprocket", Prov(),
        tool_dispatch=lambda t, a: {},
        max_tool_calls=4,
    )
    assert res.success is False
    # Non-retryable 400 → provider_auth (or provider_error); never max_iters/budget.
    assert res.stop_reason in ("provider_auth", "provider_error"), res.stop_reason
    assert res.stop_reason != "max_iters"
    detail = (res.result.get("error_detail") or "").lower()
    assert "budget exhausted" not in detail
    assert "effort" in detail or "400" in detail or "anthropic" in detail
    assert res.result.get("error_detail")


def test_tool_budget_path_names_tool_call_budget():
    """Genuine tool-budget exhaustion still names the tool-call budget + limit."""
    budget = agent_turn.run_turn(
        "loop",
        _ScriptedProvider(
            ['```json\n{"tool": "measure_part", "args": {}}\n```'] * 20),
        allowed_tools=["measure_part"],
        max_tool_calls=3,
        tool_dispatch=lambda t, a: {"ok": True},
    )
    assert budget.stop_reason == "max_iters"
    detail = budget.result.get("error_detail") or ""
    assert "tool-call budget" in detail.lower()
    assert "3" in detail  # limit visible
    assert "budget exhausted" in detail.lower()


def test_scrub_secrets_redacts_keys():
    raw = (
        "Authorization: Bearer sk-ant-xyzdeadbeef01 and "
        "api_key=sk-ant-xyzdeadbeef02 and openrouter or-deadbeefcafe"
    )
    scrubbed = agent_turn.scrub_secrets(raw)
    assert "sk-ant-xyzdeadbeef01" not in scrubbed
    assert "sk-ant-xyzdeadbeef02" not in scrubbed
    assert "or-deadbeefcafe" not in scrubbed
    assert "[redacted]" in scrubbed
    detail = agent_turn.format_error_detail("a\nb\n" + ("z" * 500), limit=80)
    assert "\n" not in detail
    assert len(detail) <= 80


def test_internal_dispatch_fault_teaches_not_masquerades_as_arg_error(caplog):
    """A tool_dispatch AttributeError (harness bug, e.g. live
    'str' object has no attribute 'get') must come back reason=internal_error
    with a message that does NOT masquerade as an argument-teaching error, and
    the full traceback must be logged server-side."""
    import logging
    provider = _ScriptedProvider([
        '```json\n{"tool": "add_features", "args": {"doc_id": "d1"}}\n```',
        '```json\n{"done": true, "handoff": {}}\n```',
    ])

    def _dispatch(tool, args):
        return "oops".get("foo")  # AttributeError: 'str' object has no attribute 'get'

    with caplog.at_level(logging.ERROR, logger="chudware.agent_turn"):
        res = agent_turn.run_turn("build it", provider, allowed_tools=["add_features"],
                                  tool_dispatch=_dispatch)
    call = res.result["calls"][0]
    assert call["ok"] is False
    err_payload = call["result"]
    assert isinstance(err_payload, dict)
    assert err_payload["reason"] == "internal_error"
    assert err_payload["internal_error"] is True
    msg = err_payload["error"].lower()
    assert "unknown argument" not in msg
    assert "got unexpected keyword" not in msg
    assert "not a problem with your call" in msg
    assert "attributeerror" in msg
    assert any("internal tool-dispatch fault" in r.message for r in caplog.records)


def test_legitimate_value_error_keeps_current_behavior():
    """A tool-raised ValueError (teaching rejection) must NOT be reclassified
    as an internal fault — behaviour is unchanged from before this fix."""
    provider = _ScriptedProvider([
        '```json\n{"tool": "measure_part", "args": {}}\n```',
        '```json\n{"done": true, "handoff": {}}\n```',
    ])

    def _dispatch(tool, args):
        raise ValueError("doc_id is required")

    res = agent_turn.run_turn("check", provider, allowed_tools=["measure_part"],
                              tool_dispatch=_dispatch)
    call = res.result["calls"][0]
    assert call["ok"] is False
    result = call["result"]
    assert isinstance(result, dict)
    assert result.get("reason") != "internal_error"
    assert "internal_error" not in result
    assert "doc_id is required" in result["error"]


def test_is_internal_dispatch_fault_classifies_by_type_and_depth():
    try:
        raise AttributeError("'str' object has no attribute 'get'")
    except AttributeError as e:
        assert agent_turn._is_internal_dispatch_fault(e) is True
    try:
        raise ValueError("teaching message")
    except ValueError as e:
        assert agent_turn._is_internal_dispatch_fault(e) is False
    try:
        raise RuntimeError("teaching message")
    except RuntimeError as e:
        assert agent_turn._is_internal_dispatch_fault(e) is False

    def _needs_doc_id(doc_id):
        return doc_id

    try:
        _needs_doc_id(wrong_kw=1)  # argument-binding TypeError, depth 1
    except TypeError as e:
        assert agent_turn._is_internal_dispatch_fault(e) is False

    def _raises_inside():
        return "oops".get("x")  # body-raised, but wrap as TypeError below

    def _wraps_typeerror():
        try:
            _raises_inside()
        except AttributeError as inner:
            raise TypeError(str(inner)) from inner

    try:
        _wraps_typeerror()  # TypeError raised INSIDE a nested call, depth >= 2
    except TypeError as e:
        assert agent_turn._is_internal_dispatch_fault(e) is True


def test_resolve_tool_dispatch_bad_kwarg_typeerror_unaffected(monkeypatch):
    """A genuine bad-kwarg call through _resolve_tool_dispatch's fallback
    dispatcher must keep its existing has_var/cleaned-kwargs retry behaviour
    — never reclassified as an internal fault."""
    def _fake_tool(doc_id):
        return {"ok": True, "doc_id": doc_id}

    monkeypatch.setattr(
        agent_turn, "_bind_tool",
        lambda name: _fake_tool if name == "measure_part" else None,
    )
    disp = agent_turn._resolve_tool_dispatch()
    result = disp("measure_part", {"wrong_kw": 1})
    assert result["ok"] is False
    assert result.get("reason") != "internal_error"
    assert "missing" in result["error"].lower() or "argument" in result["error"].lower()


def test_resolve_tool_dispatch_body_fault_is_internal_and_logged(monkeypatch, caplog):
    """A tool body that raises AttributeError through _resolve_tool_dispatch's
    fallback dispatcher must be classified internal and its traceback logged."""
    import logging

    def _fake_tool(doc_id=None):
        return "oops".get("x")  # AttributeError raised inside the tool body

    monkeypatch.setattr(
        agent_turn, "_bind_tool",
        lambda name: _fake_tool if name == "measure_part" else None,
    )
    disp = agent_turn._resolve_tool_dispatch()
    with caplog.at_level(logging.ERROR, logger="chudware.agent_turn"):
        result = disp("measure_part", {"doc_id": "d1"})
    assert result["reason"] == "internal_error"
    assert result["internal_error"] is True
    assert any("internal tool-dispatch fault" in r.message for r in caplog.records)