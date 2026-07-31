"""test_turn_truncation.py — truncated-directive honesty + interactive lane caps.

Live 2026-07-28 (run asm-152734881473315): the general lane made three
research calls, then began a ```json {"tool":"mission_start", ...} directive
that the 2048-token output cap CUT OFF mid-string. `_parse_directive` returned
None, the bare-natural-language-finish path accepted the fragment, and the run
reported `status=done stop_reason=confirmed` having built nothing at all.

These tests pin the fixes:
  * `agent_turn._looks_truncated` detects a cut-off directive textually, retries
    ONCE, then fails honestly. The textual test is the FALLBACK: providers that
    report a stop reason now hand it over out-of-band
    (`providers.base.take_last_stop_reason`) and it is authoritative. Measured
    2026-07-30 against the live sidecar: maxTokens=16 -> stopReason "length",
    maxTokens=4000 -> "stop".
  * `mission_dispatch.run_linear_role_turn` passes a real output-token budget.
  * the three Settings → AI → Harness preference knobs actually shape the
    interactive lane's allow-set and system prompt.
"""
from __future__ import annotations

import pytest


class _ScriptedProvider:
    """Queue of canned replies; records the message list per completion."""

    name = "scripted"

    def __init__(self, replies):
        self._replies = list(replies)
        self.seen_messages: list[list] = []

    def complete(self, system, messages, max_tokens=8000):
        self.seen_messages.append(
            [str(m.get("content")) for m in messages if isinstance(m, dict)]
        )
        return self._replies.pop(0) if self._replies else "no more"


def _flat(p: _ScriptedProvider) -> str:
    return "\n".join(line for batch in p.seen_messages for line in batch)


def _nudges(p: _ScriptedProvider, marker: str) -> int:
    """How many *distinct* nudges reached the model.

    `seen_messages` snapshots the WHOLE conversation per completion, so a
    single nudge reappears in every later snapshot — count the last snapshot.
    """
    last = p.seen_messages[-1] if p.seen_messages else []
    return sum(1 for line in last if marker in line)


def _dispatch_ok(tool, args):
    if tool == "ledger_get":
        return {"ok": True, "rendered": "[LEDGER] brief", "event_count": 1}
    return {"ok": True}


_GENERAL_ALLOW = [
    "ledger_get", "ledger_note", "describe_project", "workspace_status",
    "enable_tools", "list_tool_categories", "delegate",
]

_CALL = '```json\n{"tool": "%s", "args": {}}\n```'

# The live shape: opening fence, no closing fence, cut off INSIDE a string.
_TRUNCATED = (
    "I have enough context now, spinning up the build.\n\n"
    '```json\n{"tool": "mission_start", "args": {"request": "Design a '
    'printable rig with the following constraints:\\n- All printed parts '
    'must f'
)

# Exact final reply from desktop run run-77308371568450. The model closed the
# fence but omitted the OUTER object brace after args, so no tool call fired.
_CLOSED_MALFORMED_ADD_FEATURES = (
    '```json\n{"tool": "add_features", "args": {"doc_id": "doc-cpp00000002", '
    '"ops": [{"op": "add_sketch", "name": "gap_profile", "plane": "XY", '
    '"entities": [{"id": "g1", "type": "line", "p1": [5, -8], "p2": [20, -8]}, '
    '{"id": "g2", "type": "line", "p1": [20, -8], "p2": [20, 8]}, '
    '{"id": "g3", "type": "line", "p1": [20, 8], "p2": [5, 8]}, '
    '{"id": "g4", "type": "line", "p1": [5, 8], "p2": [5, -8]}]}, '
    '{"op": "extrude", "name": "SnapGap", "sketch": "gap_profile", '
    '"distance": 14, "start_offset": -1, "mode": "cut"}]}\n```'
)
_CLOSED_FIXED_ADD_FEATURES = _CLOSED_MALFORMED_ADD_FEATURES.replace(
    "]}\n```", "]}}\n```"
)


# ---------------------------------------------------------------------------
# _looks_truncated: unit precision
# ---------------------------------------------------------------------------


def test_looks_truncated_detects_unclosed_fence_mid_string():
    from services.agent import agent_turn as at

    assert at._looks_truncated(_TRUNCATED)


def test_looks_truncated_distinguishes_complete_prose_and_malformed_directives():
    from services.agent import agent_turn as at

    assert not at._looks_truncated(_CALL % "ledger_get")
    assert not at._looks_truncated(
        "Done - the bracket is 40 x 20 x 5 mm with two M4 holes."
    )
    assert not at._looks_truncated("")
    # Prose that merely mentions braces is not a cut-off directive.
    assert not at._looks_truncated(
        "The config uses { } braces and an unmatched [ here, for illustration."
    )
    # A CLOSED fence is still retryable when its unmistakable directive cannot
    # decode. This is the exact class that ended run-77308371568450.
    assert at._looks_truncated('```json\n{"tool": "x", "args"\n```')
    assert at._looks_truncated(_CLOSED_MALFORMED_ADD_FEATURES)
    assert not at._looks_truncated(_CLOSED_FIXED_ADD_FEATURES)
    # Invalid fenced prose without a directive opener is not commandeered.
    assert not at._looks_truncated("```json\nnot actually a tool call {\n```")

class TestAuthoritativeStopReason:
    """A reply capped mid-PROSE is truncated too, and no amount of brace-counting
    can see it. The bridge has reported the real signal all along; it just never
    reached the harness."""

    def test_length_overrides_a_reply_the_textual_test_calls_clean(self):
        from services.agent import agent_turn as at

        clean = "Done - the bracket is 40 x 20 x 5 mm with two M4 holes."
        assert not at._looks_truncated(clean)              # textual: looks finished
        assert at._looks_truncated(clean, "length")        # provider: it was cut off

    def test_a_normal_stop_never_forces_truncation(self):
        from services.agent import agent_turn as at

        assert not at._looks_truncated("All done.", "stop")
        assert not at._looks_truncated("All done.", "end_turn")
        assert not at._looks_truncated("All done.", "tool_use")
        assert not at._looks_truncated("All done.", None)

    def test_the_wire_dialects_are_all_understood(self):
        """Different vendors spell the same thing differently."""
        from services.agent import agent_turn as at

        for spelling in ("length", "LENGTH", "  Length ", "max_tokens",
                         "max_output_tokens"):
            assert at._looks_truncated("finished.", spelling), spelling

    def test_a_truncated_directive_is_still_caught_without_any_signal(self):
        """The textual fallback must survive: most providers report nothing."""
        from services.agent import agent_turn as at

        assert at._looks_truncated(_TRUNCATED, None)

    def test_the_stop_reason_seam_is_one_shot(self):
        """Consuming semantics are load-bearing: a provider that reports nothing
        must yield None, not whatever an earlier call left on the thread."""
        from services.agent.providers import base as pb

        pb.set_last_stop_reason("length")
        assert pb.take_last_stop_reason() == "length"
        assert pb.take_last_stop_reason() is None

def test_json_balance_ignores_braces_inside_strings():
    """A naive count('{') is wrong: JSON strings carry braces and escapes."""
    from services.agent import agent_turn as at

    depth, brack, in_str = at._json_balance('{"a": "{{{ [[[ \\" }"}')
    assert (depth, brack, in_str) == (0, 0, False)
    depth, brack, in_str = at._json_balance('{"a": "unterminated')
    assert depth == 1 and in_str is True


# ---------------------------------------------------------------------------
# Turn-level: a truncated directive must never be reported as success
# ---------------------------------------------------------------------------


def test_truncated_directive_after_real_work_is_not_success():
    """The live failure: two real calls then a cut-off mission_start.

    Before the guard this returned success with a `done` handoff and zero
    geometry. Now: one re-emit request, and a second truncation ends the turn
    honestly.
    """
    from services.agent import agent_turn as at

    p = _ScriptedProvider([
        _CALL % "describe_project",
        _CALL % "workspace_status",
        _TRUNCATED,
        _TRUNCATED,
    ])
    res = at.run_turn(
        "build me a printable drilling rig",
        p,
        allowed_tools=list(_GENERAL_ALLOW),
        max_tool_calls=8,
        tool_dispatch=_dispatch_ok,
        actor="general_agent",
        run_id="t-trunc-1",
    )
    assert res.success is False, (res.stop_reason, res.result)
    assert res.stop_reason == "no_code", (res.stop_reason, res.result)
    ho = (res.result or {}).get("handoff") or {}
    assert ho.get("status") != "done", ho
    # The re-emit request went out exactly once — a second truncation must not
    # loop the budget away.
    assert _nudges(p, "[TRUNCATED]") == 1, _flat(p)


def test_truncated_directive_recovers_when_model_re_emits():
    """The happy path: the retry lands a real call and the turn completes."""
    from services.agent import agent_turn as at

    p = _ScriptedProvider([
        _CALL % "describe_project",
        _TRUNCATED,
        _CALL % "workspace_status",
        "The project has two parts and no open issues.",
    ])
    res = at.run_turn(
        "summarise the project",
        p,
        allowed_tools=list(_GENERAL_ALLOW),
        max_tool_calls=8,
        tool_dispatch=_dispatch_ok,
        actor="general_agent",
        run_id="t-trunc-2",
    )
    assert res.success is True, (res.stop_reason, res.result)
    tools = [c.get("tool") for c in (res.result or {}).get("calls") or []]
    assert "workspace_status" in tools, tools
    assert _nudges(p, "[TRUNCATED]") == 1, _flat(p)

def test_closed_malformed_live_add_features_is_reemitted_and_runs():
    """run-77308371568450 must retry the dropped call, not confirm the prose."""
    from services.agent import agent_turn as at

    p = _ScriptedProvider([
        _CALL % "describe_project",
        _CLOSED_MALFORMED_ADD_FEATURES,
        _CLOSED_FIXED_ADD_FEATURES,
        "Done - the snap gap is cut.",
    ])
    res = at.run_turn(
        "open the imported ring into a printable C-clip",
        p,
        allowed_tools=[*_GENERAL_ALLOW, "add_features"],
        max_tool_calls=8,
        tool_dispatch=_dispatch_ok,
        actor="general_agent",
        run_id="run-77308371568450-replay",
    )
    assert res.success is True, (res.stop_reason, res.result)
    tools = [c.get("tool") for c in (res.result or {}).get("calls") or []]
    assert tools.count("add_features") == 1, tools
    assert _nudges(p, "[TRUNCATED]") == 1, _flat(p)


def test_complete_directive_and_prose_finish_unaffected():
    """No regression on the normal paths: no truncation nudge may appear."""
    from services.agent import agent_turn as at

    p = _ScriptedProvider([
        _CALL % "describe_project",
        "The project holds one part; nothing needs changing.",
    ])
    res = at.run_turn(
        "what is in the project?",
        p,
        allowed_tools=list(_GENERAL_ALLOW),
        max_tool_calls=8,
        tool_dispatch=_dispatch_ok,
        actor="general_agent",
        run_id="t-trunc-3",
    )
    assert res.success is True, (res.stop_reason, res.result)
    assert "[TRUNCATED]" not in _flat(p)


# ---------------------------------------------------------------------------
# run_linear_role_turn: output cap + the three preference knobs
# ---------------------------------------------------------------------------


@pytest.fixture
def _captured_turn(monkeypatch):
    """Monkeypatch agent_turn.run_turn and hand back the captured kwargs."""
    from services.agent import agent_turn as at

    seen: dict = {}

    def _fake_run_turn(request, provider, **kw):
        seen.update(kw)
        return at.TurnResult(True, "confirmed", {"calls": [], "tool_calls": 0})

    monkeypatch.setattr(at, "run_turn", _fake_run_turn)
    return seen


def _run_linear(**kw):
    from services.agent import mission_dispatch as md

    return md.run_linear_role_turn(
        "make a bracket", "general_agent", _ScriptedProvider([]), **kw)


def test_linear_role_turn_passes_real_output_cap(_captured_turn):
    """2048 truncated the live turn; the universal online entry needs headroom."""
    from services.agent import mission_dispatch as md

    _run_linear()
    assert _captured_turn.get("max_tokens") == md.LINEAR_ROLE_MAX_OUTPUT_TOKENS
    assert int(_captured_turn["max_tokens"]) >= 8192


def test_linear_role_turn_caller_max_tokens_wins(_captured_turn):
    _run_linear(max_tokens=16000)
    assert _captured_turn.get("max_tokens") == 16000


def _pin_prefs(monkeypatch, *, task="encourage", plan="off", sub="option"):
    from services.agent import config as agent_config

    monkeypatch.setattr(
        agent_config, "get_plan_preference", lambda *a, **k: plan, raising=False)
    monkeypatch.setattr(
        agent_config, "get_subagent_preference", lambda *a, **k: sub,
        raising=False)
    monkeypatch.setattr(
        agent_config, "get_task_list_preference", lambda *a, **k: task,
        raising=False)


def test_subagent_preference_off_drops_delegation_tools(
        monkeypatch, _captured_turn):
    """Dead knob no more: `off` must remove the tools, not just say so."""
    _pin_prefs(monkeypatch, sub="off")
    _run_linear()
    allowed = list(_captured_turn.get("allowed_tools") or [])
    assert "delegate" not in allowed, allowed
    assert "mission_start" not in allowed, allowed
    assert "not available" in (_captured_turn.get("system_prompt") or "")


def test_task_list_preference_off_drops_the_tool(monkeypatch, _captured_turn):
    _pin_prefs(monkeypatch, task="off")
    _run_linear()
    assert "task_list" not in list(_captured_turn.get("allowed_tools") or [])


def test_task_list_preference_require_states_the_directive(
        monkeypatch, _captured_turn):
    _pin_prefs(monkeypatch, task="require")
    _run_linear()
    prompt = _captured_turn.get("system_prompt") or ""
    assert "SESSION PREFERENCES" in prompt, prompt[-400:]
    assert "FIRST tool call" in prompt, prompt[-400:]


def test_plan_preference_off_says_act_directly(monkeypatch, _captured_turn):
    """config default is plan=off; the model must be told, not left guessing."""
    _pin_prefs(monkeypatch, plan="off")
    _run_linear()
    assert "do NOT draft a plan first" in (
        _captured_turn.get("system_prompt") or "")


def test_curated_roles_keep_their_card_untouched(monkeypatch, _captured_turn):
    """A session knob must never reshape a curated DAG/sub-agent role card."""
    from services.agent import mission_dispatch as md

    _pin_prefs(monkeypatch, sub="off", task="require")
    md.run_linear_role_turn(
        "make a bracket", "feature_modeler", _ScriptedProvider([]))
    prompt = _captured_turn.get("system_prompt") or ""
    assert "SESSION PREFERENCES" not in prompt
    # SUBAGENT_DEFAULT_TOOLS puts delegate on every card; `off` must not have
    # stripped it from a curated one.
    assert "delegate" in list(_captured_turn.get("allowed_tools") or [])

# ---------------------------------------------------------------------------
# Task-list nudge: the preference must ACT, not merely advise
# ---------------------------------------------------------------------------
# Live 2026-07-29: with task_list advertised, resolvable, and the role prompt
# demanding it, claude-opus-5 still made 20 tool calls without publishing one —
# the Task list panel stayed empty for the entire build. Encourage/Require now
# push once, so the knob has an observable effect at its default setting.

_TL_ALLOW = [*_GENERAL_ALLOW, "task_list"]


def _run_general(monkeypatch, *, task, replies, calls=6):
    from services.agent import agent_turn as at

    _pin_prefs(monkeypatch, task=task)
    p = _ScriptedProvider(replies)
    res = at.run_turn(
        "organize the project and build the bracket",
        p,
        allowed_tools=list(_TL_ALLOW),
        max_tool_calls=calls,
        tool_dispatch=_dispatch_ok,
        # The flag that marks the interactive general lane (mission_dispatch
        # sets it for role == general_agent and nothing else).
        auto_enable_packs=True,
    )
    return p, res


def test_encourage_nudges_once_after_a_multi_step_run(monkeypatch):
    p, _ = _run_general(
        monkeypatch, task="encourage",
        replies=[_CALL % "ledger_get", _CALL % "describe_project",
                 _CALL % "workspace_status", _CALL % "ledger_note",
                 "All set - nothing else to do."],
    )
    assert _nudges(p, "[TASK LIST]") == 1, _flat(p)[-600:]


def test_require_nudges_at_the_first_call(monkeypatch):
    p, _ = _run_general(
        monkeypatch, task="require",
        replies=[_CALL % "ledger_get", "Done."],
    )
    assert _nudges(p, "[TASK LIST]") == 1, _flat(p)[-600:]


def test_no_nudge_once_the_model_published(monkeypatch):
    """Publishing satisfies the preference — no scolding after the fact."""
    p, _ = _run_general(
        monkeypatch, task="require",
        replies=[_CALL % "task_list", _CALL % "ledger_get",
                 _CALL % "describe_project", _CALL % "workspace_status",
                 "Done."],
    )
    assert _nudges(p, "[TASK LIST]") == 0, _flat(p)[-600:]


def test_off_and_option_never_nudge(monkeypatch):
    for pref in ("off", "option"):
        p, _ = _run_general(
            monkeypatch, task=pref,
            replies=[_CALL % "ledger_get", _CALL % "describe_project",
                     _CALL % "workspace_status", _CALL % "ledger_note",
                     "Done."],
        )
        assert _nudges(p, "[TASK LIST]") == 0, f"{pref}: {_flat(p)[-400:]}"


def test_curated_subagent_lane_is_never_nudged(monkeypatch):
    """auto_enable_packs is off for DAG nodes/sub-agents — no session knob there."""
    from services.agent import agent_turn as at

    _pin_prefs(monkeypatch, task="require")
    p = _ScriptedProvider([_CALL % "ledger_get", _CALL % "describe_project",
                           _CALL % "workspace_status", "Done."])
    at.run_turn("build the bracket", p, allowed_tools=list(_TL_ALLOW),
                max_tool_calls=6, tool_dispatch=_dispatch_ok)
    assert _nudges(p, "[TASK LIST]") == 0, _flat(p)[-400:]
