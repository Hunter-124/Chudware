"""Trigger-injection wiring — agent_turn.run_turn's production seam (§20 / mega-25).

Contract: contracts/advisor.md §20 (triggers, FROZEN).

Before this fix, ``advisor/adapters.observe_build``/``observe_doc`` never received
``output_text``/``tool_names``/``cooldown_state``, so ``advisor/engine.build_advisory``'s
trigger gate was never satisfied outside tests that constructed an ``Observation`` by
hand — all 21 triggers/context-packs were inert in a live mission. This suite drives
the real production seam: ``agent_turn.run_turn()`` end to end, with a scripted
provider that records every ``messages`` list it is handed, proving:

  a) a turn whose signals match a known trigger injects the expected
     ``[ADVISORY ... ] ... [/ADVISORY]`` block — on the FIRST completion when the
     USER's request matched (thread_mention keyword), on the NEXT completion when
     it was the model's own output/tool call that matched (bearing_seat via
     ``part_search``);
  b) an identical second fire within ``cooldown_turns`` (triggers.yaml: 8 for
     thread_mention) does NOT re-inject a second block;
  c) a turn with no trigger-matching text/tools injects nothing.

Hermetic: no network, no real provider, no core kernel — only the shipped
triggers.yaml table (real, like advisor/test_triggers.py) plus a scripted provider
and a stub tool_dispatch. Effort resolution is monkeypatched so results never depend
on the local machine's config.json (style mirrors test_mega24_fixes.py's
_ScriptedProvider and advisor/test_triggers.py's match_triggers expectations).
"""
from __future__ import annotations

import json

from services.agent import agent_turn as at


def _tool_reply(tool: str, args: dict | None = None, note: str = "") -> str:
    """A fenced JSON tool-call directive; *note* rides in an ignored arg so the
    reply TEXT can also carry trigger-keyword prose (mirrors a real model reply
    that narrates and calls a tool in the same completion)."""
    payload: dict = {"tool": tool, "args": dict(args or {})}
    if note:
        payload["args"]["note"] = note
    return "```json\n" + json.dumps(payload) + "\n```"


def _done_reply() -> str:
    return '```json\n{"done": true, "handoff": {"summary": "ok"}}\n```'


class _CapturingProvider:
    """Pops canned replies; snapshots every ``messages`` list it is handed so
    tests can inspect what the model would actually see on each completion."""

    name = "scripted-capture"

    def __init__(self, replies: list[str]) -> None:
        self._replies = list(replies)
        self.seen_messages: list[list[dict]] = []

    def complete(self, system, messages, max_tokens=8000) -> str:
        self.seen_messages.append([dict(m) for m in messages])
        return self._replies.pop(0) if self._replies else _done_reply()


def _dispatch_ok(tool, args):
    return {"ok": True}


def _messages_text(messages: list[dict]) -> str:
    out: list[str] = []
    for m in messages:
        content = m.get("content")
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    out.append(part.get("text", ""))
        elif isinstance(content, str):
            out.append(content)
    return "\n".join(out)


# ---------------------------------------------------------------------------
# (a) production-shaped run: a matching turn injects the expected block
# ---------------------------------------------------------------------------

def test_matching_request_injects_advisory_block(monkeypatch):
    """A request mentioning a threaded hole (thread_mention keyword) is matched
    before the first completion — a rule exists to steer the model, so waiting
    for the model to say it first would deliver the pack a turn too late."""
    prov = _CapturingProvider([
        _tool_reply("measure_part", note="I'll add a tapped hole M6x1.0 through it"),
        _done_reply(),
    ])
    res = at.run_turn(
        "add a threaded hole", prov,
        allowed_tools=["measure_part"],
        tool_dispatch=_dispatch_ok,
        max_tool_calls=4,
    )
    assert res.success is True
    assert len(prov.seen_messages) == 2
    first_text = _messages_text(prov.seen_messages[0])
    assert "[ADVISORY" in first_text
    assert "[/ADVISORY]" in first_text
    assert "thread_mention" in first_text
    # Iteration 1's output repeats the keyword; cooldown_turns=8 keeps it quiet.
    assert _messages_text(prov.seen_messages[1]).count("[ADVISORY") == 1


def test_tool_name_match_also_injects(monkeypatch):
    """bearing_seat fires on the part_search TOOL NAME, not just keywords."""
    prov = _CapturingProvider([
        _tool_reply("part_search", {"query": "6203 bearing"}),
        _done_reply(),
    ])
    res = at.run_turn(
        "find a bearing for the shaft", prov,
        allowed_tools=["part_search"],
        tool_dispatch=_dispatch_ok,
        max_tool_calls=4,
    )
    assert res.success is True
    assert len(prov.seen_messages) == 2
    second_text = _messages_text(prov.seen_messages[1])
    assert "[ADVISORY" in second_text
    assert "bearing_seat" in second_text


# ---------------------------------------------------------------------------
# (b) cooldown suppresses a second fire within the same turn's loop
# ---------------------------------------------------------------------------

def test_cooldown_suppresses_second_fire_within_turn(monkeypatch):
    prov = _CapturingProvider([
        _tool_reply("measure_part", note="tap M6x1.0 pilot hole"),
        _tool_reply("measure_part", note="tap M6x1.0 again"),
        _done_reply(),
    ])
    res = at.run_turn(
        "add two threaded holes", prov,
        allowed_tools=["measure_part"],
        tool_dispatch=_dispatch_ok,
        max_tool_calls=4,
    )
    assert res.success is True
    assert len(prov.seen_messages) == 3
    # The request ("threaded") fired thread_mention before completion 1; the two
    # replies repeat the keyword one and two iterations later. cooldown_turns=8
    # -> delta < 8 both times, so no second block is ever appended. The single
    # block stays visible in the accumulated history: exactly 1, never 2.
    for i in (0, 1, 2):
        assert _messages_text(prov.seen_messages[i]).count("[ADVISORY") == 1, i


# ---------------------------------------------------------------------------
# (d) non-matching turn injects nothing
# ---------------------------------------------------------------------------

def test_non_matching_turn_injects_nothing(monkeypatch):
    prov = _CapturingProvider([
        _tool_reply("measure_part", note="just check the overall envelope size"),
        _done_reply(),
    ])
    res = at.run_turn(
        "measure the part", prov,
        allowed_tools=["measure_part"],
        tool_dispatch=_dispatch_ok,
        max_tool_calls=4,
    )
    assert res.success is True
    assert len(prov.seen_messages) == 2
    assert "[ADVISORY" not in _messages_text(prov.seen_messages[1])
