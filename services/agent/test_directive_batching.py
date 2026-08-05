"""test_directive_batching.py — several tool calls per reply.

Before 2026-08-04 the turn loop ran the FIRST directive in a reply and dropped
the rest, so every tool call cost a full billed completion with the whole
conversation resent. Live run-135571474839343 spent 12 completions to make 4
tool calls; the repaired replay spent ~28 to make 21.

A reply may now carry several ```json fences. They dispatch in order, one per
loop iteration, with no completion in between, and the batch halts the moment a
call fails or is refused so the model always answers for bad news itself.

These tests pin the things that make batching safe rather than merely fast:
order, stop-on-failure, stop-on-refusal, the budget bound, and that a drained
call neither bills a completion nor re-appends the model's words.
"""
from __future__ import annotations

import json

from services.agent import agent_turn as at


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


_ALLOW = ["ledger_get", "ledger_note", "describe_project", "workspace_status",
          "new_part", "add_features", "render_part", "export_part"]

_DONE = ('```json\n{"done": true, "handoff": {"status": "done", '
         '"summary": "ok"}}\n```')


def _fence(tool: str, **args) -> str:
    return '```json\n' + json.dumps({"tool": tool, "args": args}) + '\n```'


def _batch(*fences: str) -> str:
    return "Setting up the part now.\n\n" + "\n\n".join(fences)


# ---------------------------------------------------------------------------
# _parse_directives
# ---------------------------------------------------------------------------


def test_every_fence_is_returned_in_order():
    reply = _batch(_fence("new_part", name="p"),
                   _fence("add_features", doc_id="d", ops=[1]),
                   _fence("render_part", doc_id="d"))
    got = at._parse_directives(reply)
    assert [d["tool"] for d in got] == ["new_part", "add_features",
                                        "render_part"]
    # The single-directive accessor still answers with the first.
    assert at._parse_directive(reply)["tool"] == "new_part"


def test_a_plan_block_never_displaces_a_real_call():
    """The lane-delegate-1 regression: a non-directive fence came first."""
    reply = ('```json\n{"plan": ["a", "b"]}\n```\n\n'
             + _fence("new_part", name="p"))
    assert [d["tool"] for d in at._parse_directives(reply)] == ["new_part"]


def test_runaway_replies_are_capped():
    reply = _batch(*[_fence("ledger_note", text=str(i)) for i in range(20)])
    assert len(at._parse_directives(reply)) == at._MAX_BATCH_DIRECTIVES


def test_single_directive_replies_are_unchanged():
    for reply in (_fence("ledger_get"),
                  '{"tool": "ledger_get", "args": {}}',
                  _DONE):
        assert len(at._parse_directives(reply)) == 1, reply
    assert at._parse_directives("just prose, no call") == []
    assert at._parse_directives("") == []


# ---------------------------------------------------------------------------
# The turn loop
# ---------------------------------------------------------------------------


def test_a_batch_runs_every_call_on_one_completion():
    seen = []
    p = _ScriptedProvider([
        _batch(_fence("new_part", name="holster"),
               _fence("add_features", doc_id="d", ops=[{"op": "add_feature"}]),
               _fence("render_part", doc_id="d")),
        _DONE,
    ])

    def dispatch(tool, args):
        # How many completions had been billed when this call went out.
        seen.append((tool, len(p.seen_messages)))
        return {"ok": True}

    res = at.run_turn("build me a holster", p, allowed_tools=_ALLOW,
                      max_tool_calls=12, tool_dispatch=dispatch,
                      actor="general_agent")
    assert res.success is True, (res.stop_reason, res.result)
    assert [t for t, _ in seen] == ["new_part", "add_features", "render_part"]
    # All three rode the SAME completion. One-per-reply billed three.
    assert [n for _, n in seen] == [1, 1, 1], seen


def test_a_drained_call_does_not_replay_the_models_words():
    """The reply is appended to the conversation once, not once per call."""
    p = _ScriptedProvider([
        _batch(_fence("ledger_note", text="a"),
               _fence("ledger_note", text="b")),
        _DONE,
    ])
    at.run_turn("note both of these", p, allowed_tools=_ALLOW,
                max_tool_calls=12, tool_dispatch=lambda t, a: {"ok": True},
                actor="general_agent")
    final = p.seen_messages[-1]
    assert sum(1 for m in final if "Setting up the part now." in m) == 1, final


def test_a_failure_drops_the_rest_of_the_batch():
    seen = []

    def dispatch(tool, args):
        seen.append(tool)
        if tool == "add_features":
            return {"ok": False, "error": "sketch profile is not closed"}
        return {"ok": True}

    p = _ScriptedProvider([
        _batch(_fence("new_part", name="holster"),
               _fence("add_features", doc_id="d", ops=[{"op": "add_feature"}]),
               _fence("render_part", doc_id="d"),
               _fence("export_part", doc_id="d")),
        _DONE,
    ])
    at.run_turn("build me a holster", p, allowed_tools=_ALLOW,
                max_tool_calls=12, tool_dispatch=dispatch,
                actor="general_agent")
    # render_part/export_part were planned against a sketch that never landed.
    assert seen == ["new_part", "add_features"], seen
    told = "\n".join(p.seen_messages[-1])
    assert "[BATCH HALTED]" in told
    assert "render_part" in told and "export_part" in told


def test_a_refusal_drops_the_rest_of_the_batch():
    seen = []
    p = _ScriptedProvider([
        _batch(_fence("ledger_note", text="a"),
               _fence("export_part", doc_id="d"),
               _fence("ledger_note", text="c")),
        _DONE,
    ])
    at.run_turn("do these", p, allowed_tools=["ledger_note", "ledger_get"],
                max_tool_calls=12,
                tool_dispatch=lambda t, a: seen.append(t) or {"ok": True},
                actor="general_agent")
    assert seen == ["ledger_note"], seen


def test_a_finish_envelope_after_calls_still_finishes():
    seen = []
    p = _ScriptedProvider([
        _batch(_fence("new_part", name="p"),
               _fence("add_features", doc_id="d", ops=[{"op": "add_feature"}]))
        + "\n\n" + _DONE,
        _DONE,
    ])

    def dispatch(tool, args):
        seen.append((tool, len(p.seen_messages)))
        return {"ok": True}

    res = at.run_turn("build a plate", p, allowed_tools=_ALLOW,
                      max_tool_calls=12, tool_dispatch=dispatch,
                      actor="general_agent")
    assert [t for t, _ in seen] == ["new_part", "add_features"], seen
    assert [n for _, n in seen] == [1, 1], seen
    assert res.success is True, (res.stop_reason, res.result)


def test_the_batch_cannot_outrun_the_call_budget():
    seen = []
    p = _ScriptedProvider([
        _batch(*[_fence("ledger_note", text=str(i)) for i in range(6)]),
        _DONE, _DONE, _DONE,
    ])
    at.run_turn("note all six", p, allowed_tools=_ALLOW, max_tool_calls=3,
                tool_dispatch=lambda t, a: seen.append(t) or {"ok": True},
                actor="general_agent")
    assert len(seen) <= 4, seen
