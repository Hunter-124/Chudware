"""test_turn_honesty.py — zero-step / fabricated-success defenses (2026-07-27).

Grounded in the live "make me a mudbucket" run (kimi k3-256k, general lane):

- The model answered "Done - I built this as parametric features; see the
  FeatureManager tree and the viewport" with ZERO voluntary tool calls. The
  forced first-tool floor made ``tool_calls_made == 1`` and the bare-handoff
  path accepted the fabrication as a clean done (GUI showed 0/0 steps).
- ``enable_tools``/``list_tool_categories`` calls never emitted
  ``agent.tool_call`` — invisible in the timeline and events.jsonl.
- ``resolve_categories`` alphabetical cap trim silently dropped ``new_part``
  from the modeling pack (family:cad expands to 112 > cap 40), so an enabled
  model could not create a part.

Each test names the contract it defends; neutering the corresponding helper
MUST fail it.
"""
from __future__ import annotations

import json

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


def _dispatch_ok(tool, args):
    if tool == "ledger_get":
        return {"ok": True, "rendered": "[LEDGER] brief", "event_count": 1}
    if tool == "ledger_note":
        return {"ok": True, "seq": 1, "kind": (args or {}).get("kind", "note")}
    return {"ok": True}


_GENERAL_ALLOW = [
    "ledger_get", "ledger_note", "describe_project", "workspace_status",
    "enable_tools", "list_tool_categories", "delegate",
]

_CLAIM = (
    "Done - I built this as parametric features; see the FeatureManager "
    "tree and the viewport."
)


# ---------------------------------------------------------------------------
# Fabricated-success gate: claims of completed work with zero voluntary calls
# ---------------------------------------------------------------------------


def test_fabricated_prose_finish_nudged_then_honest_no_code():
    """Claim-only finishes: one corrective nudge, then honest no_code — never done."""
    from services.agent import agent_turn as at

    p = _ScriptedProvider([_CLAIM, _CLAIM, _CLAIM])
    res = at.run_turn(
        "make me a mudbucket for oil drilling and rigging",
        p,
        allowed_tools=list(_GENERAL_ALLOW),
        max_tool_calls=8,
        tool_dispatch=_dispatch_ok,
        actor="general_agent",
        run_id="t-fab-1",
    )
    assert res.success is False, (res.stop_reason, res.result)
    assert res.stop_reason == "no_code", (res.stop_reason, res.result)
    # No done handoff may carry the fabrication downstream.
    ho = (res.result or {}).get("handoff") or {}
    assert ho.get("status") != "done"
    # The corrective nudge reached the model exactly once.
    assert _flat(p).count("[NO WORK PERFORMED]") == 1, _flat(p)
    assert 'enable_tools(categories=["modeling", "sketch"])' in _flat(p)


def test_done_envelope_claim_with_zero_voluntary_calls_rejected():
    """A formal done envelope claiming work fares no better than prose."""
    from services.agent import agent_turn as at

    env = '```json\n{"status": "done", "summary": "%s", "outputs": {}}\n```' % (
        "Done - I built the part; see the FeatureManager tree.")
    p = _ScriptedProvider(["Let me look around first.", env, env])
    res = at.run_turn(
        "make me a mudbucket",
        p,
        allowed_tools=list(_GENERAL_ALLOW),
        max_tool_calls=8,
        tool_dispatch=_dispatch_ok,
        actor="general_agent",
        run_id="t-fab-2",
    )
    assert res.success is False, (res.stop_reason, res.result)
    assert res.stop_reason == "no_code", (res.stop_reason, res.result)
    assert _flat(p).count("[NO WORK PERFORMED]") == 1, _flat(p)


def test_claim_after_voluntary_tool_work_still_completes():
    """The gate keys on MISSING work, not on claim phrasing: real calls pass."""
    from services.agent import agent_turn as at

    p = _ScriptedProvider([
        '```json\n{"tool": "ledger_get", "args": {}}\n```',
        "Done - I built the summary you asked for; see the FeatureManager tree.",
    ])
    res = at.run_turn(
        "summarize the project",
        p,
        allowed_tools=list(_GENERAL_ALLOW),
        max_tool_calls=8,
        tool_dispatch=_dispatch_ok,
        actor="general_agent",
        run_id="t-fab-3",
    )
    assert res.success is True, (res.stop_reason, res.result)
    assert "[NO WORK PERFORMED]" not in _flat(p)


def test_conversational_finish_without_claims_not_gated():
    """Zero-tool chat/Q&A must never trip the gate (no false-positive loop)."""
    from services.agent import agent_turn as at

    p = _ScriptedProvider([
        "Hello! What can I do for you today?",
        "I can help with parts, drawings, assemblies and file organization.",
    ])
    res = at.run_turn(
        "hi",
        p,
        allowed_tools=list(_GENERAL_ALLOW),
        max_tool_calls=8,
        tool_dispatch=_dispatch_ok,
        actor="general_agent",
        run_id="t-fab-4",
    )
    assert res.success is True, (res.stop_reason, res.result)
    assert "[NO WORK PERFORMED]" not in _flat(p)


def test_claims_detector_precision():
    """Unit-level: the phrase matcher catches the live fabrication, not chat."""
    from services.agent import agent_turn as at

    assert at._claims_completed_work(_CLAIM)
    assert at._claims_completed_work("I created the part and saved it.")
    assert at._claims_completed_work("The boss is now in the viewport.")
    assert not at._claims_completed_work("Hello! How can I help?")
    assert not at._claims_completed_work(
        "I can build that for you if you want.")
    assert not at._claims_completed_work("")


def test_meta_only_turn_with_blank_done_envelope_is_not_work():
    """Live k3 (2026-07-27): list_tool_categories then `done` with an empty
    summary — browsing packs is not work, so the turn must not report done."""
    from services.agent import agent_turn as at

    blank_done = '```json\n{"status": "done", "summary": "", "outputs": {}}\n```'
    p = _ScriptedProvider([
        '```json\n{"tool": "list_tool_categories", "args": {}}\n```',
        blank_done,
        blank_done,
    ])
    res = at.run_turn(
        "make me a mudbucket for oil drilling and rigging",
        p,
        allowed_tools=list(_GENERAL_ALLOW),
        max_tool_calls=8,
        tool_dispatch=_dispatch_ok,
        actor="general_agent",
        run_id="t-meta-only",
    )
    assert res.success is False, (res.stop_reason, res.result)
    assert res.stop_reason == "no_code", (res.stop_reason, res.result)
    assert _flat(p).count("[NO WORK PERFORMED]") == 1, _flat(p)


def test_meta_calls_do_not_count_as_real_work():
    """Unit-level: only voluntary non-meta calls satisfy the work test."""
    from services.agent import agent_turn as at

    assert not at._did_real_work([])
    assert not at._did_real_work([{"tool": "list_tool_categories", "ok": True}])
    assert not at._did_real_work([{"tool": "enable_tools", "ok": True}])
    assert not at._did_real_work(
        [{"tool": "describe_project", "ok": True, "forced": True}])
    assert at._did_real_work([{"tool": "add_features", "ok": False}])
    assert at._did_real_work([{"tool": "fs_list", "ok": True}])


def test_blank_envelope_after_real_work_still_completes():
    """A terse done envelope is fine once real work happened (no false block)."""
    from services.agent import agent_turn as at

    p = _ScriptedProvider([
        '```json\n{"tool": "ledger_note", "args": {"kind": "note", "payload": {"text": "x"}}}\n```',
        '```json\n{"status": "done", "summary": "", "outputs": {}}\n```',
    ])
    res = at.run_turn(
        "note that down",
        p,
        allowed_tools=list(_GENERAL_ALLOW),
        max_tool_calls=8,
        tool_dispatch=_dispatch_ok,
        actor="general_agent",
        run_id="t-blank-ok",
    )
    assert res.success is True, (res.stop_reason, res.result)
    assert "[NO WORK PERFORMED]" not in _flat(p)


# ---------------------------------------------------------------------------
# Force-first on-ramp
# ---------------------------------------------------------------------------


def test_force_first_prefers_category_menu_on_enable_lane():
    """General lane: the forced opener is the pack menu, not a blind read."""
    from services.agent import agent_turn as at

    seen = []

    def dispatch(tool, args):
        seen.append(tool)
        return {"ok": True, "categories": []}

    calls, n, forced = at._force_first_tool(
        allow={"enable_tools", "list_tool_categories", "describe_project",
               "ledger_get"},
        dispatch=dispatch,
        ledger=None,
        actor="test",
        messages=[],
        calls=[],
        tool_calls_made=0,
        budget=8,
    )
    assert forced is True
    assert seen == ["list_tool_categories"], seen
    assert calls[0].get("forced") is True


def test_force_first_legacy_floor_without_enable_tools():
    """Roles without the meta pair keep the ledger_get floor (regression)."""
    from services.agent import agent_turn as at

    seen = []

    def dispatch(tool, args):
        seen.append(tool)
        return {"ok": True}

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
    assert seen == ["ledger_get"], seen


# ---------------------------------------------------------------------------
# Meta-tool visibility: enable_tools / list_tool_categories emit agent.tool_call
# ---------------------------------------------------------------------------


def test_meta_tool_calls_emit_tool_call_events():
    """The GUI step counter must count pack enables (no more fake 0/0)."""
    from services.agent import agent_turn as at

    events: list[dict] = []

    def _on_event(kind, payload):
        if isinstance(payload, dict):
            events.append({"kind": kind, **payload})
        else:
            events.append({"kind": kind, "payload": payload})

    p = _ScriptedProvider([
        '```json\n{"tool": "list_tool_categories", "args": {}}\n```',
        '```json\n{"tool": "ledger_get", "args": {}}\n```',
        "Here is what I found in the ledger.",
    ])
    res = at.run_turn(
        "what categories can you use? then summarize the ledger",
        p,
        allowed_tools=list(_GENERAL_ALLOW),
        max_tool_calls=8,
        tool_dispatch=_dispatch_ok,
        actor="general_agent",
        run_id="t-meta-1",
        on_event=_on_event,
    )
    assert res.success is True, (res.stop_reason, res.result)
    names = [
        e.get("tool_name")
        for e in events
        if e.get("kind") == "agent.tool_call"
    ]
    assert "list_tool_categories" in names, names
    assert "ledger_get" in names, names


# ---------------------------------------------------------------------------
# Denial on-ramp: non-enabled tools name their pack
# ---------------------------------------------------------------------------


def test_denied_tool_names_owning_pack():
    """'not permitted' alone made models guess dialects blind; name the pack."""
    from services.agent import agent_turn as at

    p = _ScriptedProvider([
        '```json\n{"tool": "add_features", "args": {"ops": []}}\n```',
        '```json\n{"tool": "ledger_get", "args": {}}\n```',
        "The ledger says nothing was built.",
    ])
    res = at.run_turn(
        "make me a mudbucket",
        p,
        allowed_tools=list(_GENERAL_ALLOW),
        max_tool_calls=8,
        tool_dispatch=_dispatch_ok,
        actor="general_agent",
        run_id="t-deny-1",
    )
    assert res.success is True, (res.stop_reason, res.result)
    text = _flat(p)
    assert "not enabled in this turn" in text, text
    assert "enable_tools(categories=['modeling'])" in text, text


# ---------------------------------------------------------------------------
# Skill auto-invocation on tool reach
# ---------------------------------------------------------------------------


@pytest.fixture
def _real_skills(monkeypatch):
    """Point the skills module at the shipped builtin dir, clean sessions."""
    for var in (
        "CHUDWARE_SKILLS_USER_DIR",
        "CHUDWARE_SKILLS_PROJECT_DIR",
        "CHUDWARE_SKILLS_BUILTIN_DIR",
    ):
        monkeypatch.delenv(var, raising=False)
    from services.agent import skills as sk

    sk.clear_cache()
    sk.reset_session("t-skillrun")
    yield sk
    sk.clear_cache()
    sk.reset_session("t-skillrun")


def test_skill_body_injected_on_first_triggered_tool(_real_skills):
    """OMP-style auto-invocation: reaching for describe_part injects edit-part."""
    from services.agent import agent_turn as at

    p = _ScriptedProvider([
        '```json\n{"tool": "describe_part", "args": {}}\n```',
        '```json\n{"tool": "describe_part", "args": {}}\n```',
        "It is a solid revolve body with a drain boss.",
    ])
    res = at.run_turn(
        "tell me about this thing",  # keyword-clean: no skill fires at turn start
        p,
        allowed_tools=list(_GENERAL_ALLOW) + ["describe_part"],
        max_tool_calls=8,
        tool_dispatch=_dispatch_ok,
        actor="general_agent",
        run_id="t-skillrun",
    )
    assert res.success is True, (res.stop_reason, res.result)
    # edit-part triggers on describe_part. History accumulates across
    # completions, so dedupe: exactly ONE unique injection message (the
    # once-per-session guard blocked the second describe_part's injection).
    skill_msgs = {
        line for batch in p.seen_messages for line in batch
        if "[SKILL: edit-part" in line
    }
    assert len(skill_msgs) == 1, skill_msgs


# ---------------------------------------------------------------------------
# Category cap: explicit pack entries survive family-expansion trim
# ---------------------------------------------------------------------------


def test_resolve_categories_prefers_explicit_entries_over_family_expansion():
    """modeling must always grant new_part — family:cad may not evict it.

    Live defect: family:cad expands to 112 names and the old alphabetical trim
    cut new_part/new_assembly/save_part/set_material/preflight_ops out of the
    modeling pack, so an enabled model could not create a part.
    """
    from services.agent.tool_categories import (
        CATEGORY_PACKS,
        CATEGORY_EXPAND_CAP,
        _expand_token,
        resolve_categories,
    )

    explicit = {t for t in CATEGORY_PACKS["modeling"]
                if not t.startswith("family:")}
    assert len(explicit) <= CATEGORY_EXPAND_CAP  # else the pack itself is over
    resolved = resolve_categories(["modeling"])
    assert explicit <= resolved, sorted(explicit - resolved)
    assert "new_part" in resolved
    assert len(resolved) <= CATEGORY_EXPAND_CAP  # hard ceiling still holds
    # The crowding precondition only holds with a warm registry catalog; when
    # it is available, prove family expansion really did have to yield room.
    family = _expand_token("family:cad")
    if len(family) > CATEGORY_EXPAND_CAP:
        assert len(resolved & family) < len(family)


def test_resolve_categories_small_cap_keeps_explicit_first():
    """Under a tight cap the curated names win the room, not family fillers."""
    from services.agent.tool_categories import CATEGORY_PACKS, resolve_categories

    explicit = {t for t in CATEGORY_PACKS["modeling"]
                if not t.startswith("family:")}
    resolved = resolve_categories(["modeling"], cap=len(explicit))
    assert resolved == explicit, sorted(resolved ^ explicit)


def test_resolve_categories_cap_is_a_hard_ceiling():
    """Priority must not defeat the cap (test_mode_tools contract)."""
    from services.agent.tool_categories import CATEGORY_PACKS, resolve_categories

    capped = resolve_categories(list(CATEGORY_PACKS.keys()), cap=5)
    assert len(capped) <= 5
