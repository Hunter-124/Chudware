"""Rules (context-injection triggers) on the LIVE turn loop — visibility contract.

Contract: contracts/advisor.md §20 (triggers, FROZEN) plus the frozen transcript
event the GUI copilot feed consumes::

    kind: "agent.context_injected"
    {"source": "rule", "rule_id", "label", "matched", "token_est"}

``test_trigger_wiring.py`` proves the pack TEXT reaches the next completion.
This suite proves the part a user can actually see and pay for:

  * a rule that matches the user's request fires on the FIRST completion (a rule
    exists to steer the model before it writes geometry) and emits exactly one
    ``agent.context_injected`` event carrying the frozen payload;
  * a disabled rule fires nothing;
  * cooldown state survives ACROSS ``run_turn`` calls of one session (a rule that
    fired on the previous message must not re-inject and re-chip on the next one)
    and permits a fire again once the window has passed;
  * a turn matching no rule emits NO event (an empty transcript, not noise);
  * ``budget_tokens`` truncates the injected pack (asserted by size).

Hermetic: scripted provider, stub dispatch, real shipped trigger table, user
overrides redirected to a tmp dir via ``CHUDWARE_CONTEXT_INJECTION_DIR``.
Effort resolution is monkeypatched so results never depend on config.json.
"""
from __future__ import annotations

import json

import pytest

from services.agent import agent_turn as at
from services.agent.advisor import triggers


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------

def _tool_reply(tool: str, args: dict | None = None, note: str = "") -> str:
    payload: dict = {"tool": tool, "args": dict(args or {})}
    if note:
        payload["args"]["note"] = note
    return "```json\n" + json.dumps(payload) + "\n```"


def _done_reply() -> str:
    return '```json\n{"done": true, "handoff": {"summary": "ok"}}\n```'


def _neutral() -> str:
    """A reply whose text matches no rule (a bare tool directive). Every turn
    needs at least one real tool call before ``done`` — the harness rejects a
    zero-tool finish that claims work."""
    return _tool_reply("measure_part", {"target": "bbox"})


class _CapturingProvider:
    """Pops canned replies; snapshots every ``messages`` list it is handed."""

    name = "scripted-capture"

    def __init__(self, replies: list[str]) -> None:
        self._replies = list(replies)
        self.seen_messages: list[list[dict]] = []

    def complete(self, system, messages, max_tokens=8000) -> str:
        self.seen_messages.append([dict(m) for m in messages])
        return self._replies.pop(0) if self._replies else _done_reply()


class _Ledger:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def append(self, actor, stage, payload) -> None:
        self.rows.append(dict(payload))


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


def _patch_effort(monkeypatch, *, on: bool = True) -> None:
    """Deterministic ``trigger_injection`` flag, and project memory off so the
    local machine's .plm store cannot inject its own context event."""
    from services.agent import config as config_mod
    from services.agent import effort as effort_mod

    class _Profile:
        values = {"trigger_injection": on}

    monkeypatch.setattr(effort_mod, "resolve_effort", lambda *a, **kw: _Profile())
    monkeypatch.setattr(config_mod, "memory_enabled", lambda: False)


class _Events:
    """Collects ``(kind, payload)`` pairs, with a rule-firing view."""

    def __init__(self) -> None:
        self.seen: list[tuple[str, dict]] = []

    def __call__(self, kind, payload=None) -> None:
        self.seen.append((str(kind), dict(payload or {})))

    def rules(self) -> list[dict]:
        return [p for k, p in self.seen
                if k == "agent.context_injected" and p.get("source") == "rule"]

    def injections(self) -> list[dict]:
        return [p for k, p in self.seen if k == "agent.context_injected"]


@pytest.fixture()
def user_dir(tmp_path, monkeypatch):
    """Redirect user overrides to a tmp dir so the local machine is untouched."""
    monkeypatch.setenv("CHUDWARE_CONTEXT_INJECTION_DIR", str(tmp_path / "ctx"))
    triggers.clear_cache()
    at._TRIG_SESSIONS.clear()
    yield tmp_path / "ctx"
    triggers.clear_cache()
    at._TRIG_SESSIONS.clear()


def _run(request, replies, events=None, ledger=None, run_id=None):
    prov = _CapturingProvider(replies)
    res = at.run_turn(
        request, prov,
        allowed_tools=["measure_part"],
        tool_dispatch=_dispatch_ok,
        max_tool_calls=4,
        on_event=events,
        ledger=ledger,
        run_id=run_id,
    )
    assert res.success is True
    return prov


def _override(base_id: str, **fields) -> None:
    row = next(r for r in triggers.list_triggers() if r["id"] == base_id)
    row.update(fields)
    out = triggers.save_trigger(row)
    assert out["ok"] is True
    triggers.clear_cache()


# ---------------------------------------------------------------------------
# (0) display strings the Rules settings tab renders must be ASCII
# ---------------------------------------------------------------------------

def test_shipped_rule_display_strings_are_ascii(user_dir):
    """The GUI font atlas has no arrow/greek/typographic glyphs: a non-ASCII
    purpose renders as a literal '?' ("Stock ? setup ? tool"). Guard the strings
    the triggers_list RPC actually serves (yaml when PyYAML is present, else the
    builtin mirror) so the class cannot come back.

    Scope is purpose/label/exemplar ONLY, and must stay that way. Context-pack
    BODIES are model-facing: eps/delta/x/section signs there are correct
    engineering notation, the model reads UTF-8 fine, and the GUI sanitises them
    at the display boundary (ui::foldToAtlasGlyphs). Widening this test to pack
    bodies would punish someone for writing correct notation.
    """
    offenders: list[tuple[str, str, str]] = []
    for row in triggers.list_triggers():
        for field, val in (("purpose", row.get("purpose") or ""),
                           ("label", row.get("label") or ""),
                           ("exemplar", (row.get("inject") or {}).get("exemplar") or "")):
            bad = "".join(sorted({c for c in val if ord(c) > 127}))
            if bad:
                offenders.append((row["id"], field, bad))
    assert offenders == []


# ---------------------------------------------------------------------------
# (1) a rule fires on the user's request: pack in the prompt + one event
# ---------------------------------------------------------------------------

def test_request_keyword_fires_rule_and_emits_one_event(user_dir, monkeypatch):
    _patch_effort(monkeypatch)
    ev = _Events()
    led = _Ledger()
    prov = _run("add a tapped hole M6x1.0",
                [_neutral(), _done_reply()], events=ev, ledger=led)

    # The pack reached the FIRST completion — in time to steer the model.
    first = _messages_text(prov.seen_messages[0])
    assert "[ADVISORY" in first and "[/ADVISORY]" in first
    assert "context:thread_mention" in first
    assert "Tap drill" in first  # real body of context_packs/threads.md

    fires = ev.rules()
    assert len(fires) == 1, [p.get("rule_id") for p in fires]
    p = fires[0]
    assert p["source"] == "rule"
    assert p["rule_id"] == "thread_mention"
    assert p["label"] == "Thread mention"
    assert p["matched"]
    assert p["matched"].lower() in first.lower()
    assert isinstance(p["token_est"], int) and p["token_est"] > 0

    # §20: every firing is logged.
    assert [r for r in led.rows
            if r.get("event") == "trigger_fired"
            and r.get("rule_id") == "thread_mention"]


def test_reply_keyword_fires_rule_the_request_did_not_match(user_dir, monkeypatch):
    """A rule can also fire on the MODEL's own output, one iteration later."""
    _patch_effort(monkeypatch)
    ev = _Events()
    prov = _run(
        "clean up this bracket",
        [_tool_reply("measure_part", note="the bushing needs a press fit here"),
         _done_reply()],
        events=ev,
    )
    assert "context:press_fit_mention" not in _messages_text(prov.seen_messages[0])
    assert "context:press_fit_mention" in _messages_text(prov.seen_messages[1])
    assert [p["rule_id"] for p in ev.rules()] == ["press_fit_mention"]


# ---------------------------------------------------------------------------
# (2) disabled rule never fires
# ---------------------------------------------------------------------------

def test_disabled_rule_never_fires(user_dir, monkeypatch):
    _patch_effort(monkeypatch)
    _override("thread_mention", enabled=False)
    ev = _Events()
    prov = _run("add a tapped hole M6x1.0",
                [_neutral(), _done_reply()], events=ev)
    assert "thread_mention" not in _messages_text(prov.seen_messages[0])
    assert ev.rules() == []


# ---------------------------------------------------------------------------
# (3) cooldown — across run_turn calls of one session, then permitted again
# ---------------------------------------------------------------------------

def test_cooldown_suppresses_next_turn_of_same_session(user_dir, monkeypatch):
    _patch_effort(monkeypatch)
    ev = _Events()
    req = "add a tapped hole M6x1.0"
    _run(req, [_neutral(), _done_reply()], events=ev, run_id="sess-1")
    assert [p["rule_id"] for p in ev.rules()] == ["thread_mention"]

    # Same session, next user message, same keyword: cooldown_turns=8 → quiet.
    ev2 = _Events()
    prov2 = _run(req, [_neutral(), _done_reply()], events=ev2, run_id="sess-1")
    assert ev2.rules() == []
    assert "[ADVISORY" not in _messages_text(prov2.seen_messages[0])

    # A different session shares nothing — its own first turn fires.
    ev3 = _Events()
    _run(req, [_neutral(), _done_reply()], events=ev3, run_id="sess-2")
    assert [p["rule_id"] for p in ev3.rules()] == ["thread_mention"]


def test_cooldown_permits_fire_after_window(user_dir, monkeypatch):
    _patch_effort(monkeypatch)
    req = "add a tapped hole M6x1.0"
    _run(req, [_neutral(), _done_reply()], events=_Events(), run_id="sess-3")
    st = at._trigger_session_state("sess-3")
    assert st["cool"]["thread_mention"] == 0
    # Advance the session past thread_mention's cooldown_turns (8).
    st["seq"] = 20
    ev = _Events()
    prov = _run(req, [_neutral(), _done_reply()], events=ev, run_id="sess-3")
    assert [p["rule_id"] for p in ev.rules()] == ["thread_mention"]
    assert "context:thread_mention" in _messages_text(prov.seen_messages[0])


# ---------------------------------------------------------------------------
# (4) no match → no event (the transcript stays quiet)
# ---------------------------------------------------------------------------

def test_no_match_emits_no_context_event(user_dir, monkeypatch):
    _patch_effort(monkeypatch)
    ev = _Events()
    prov = _run(
        "rename the part to Bracket",
        [_tool_reply("measure_part", note="checking the overall envelope size"),
         _done_reply()],
        events=ev,
    )
    assert "[ADVISORY" not in _messages_text(prov.seen_messages[-1])
    assert ev.injections() == []


def test_effort_gate_off_emits_no_event(user_dir, monkeypatch):
    """The eco preset disables injection — no pack, and no chip claiming one."""
    _patch_effort(monkeypatch, on=False)
    ev = _Events()
    prov = _run("add a tapped hole M6x1.0",
                [_neutral(), _done_reply()], events=ev)
    assert "[ADVISORY" not in _messages_text(prov.seen_messages[0])
    assert ev.rules() == []


# ---------------------------------------------------------------------------
# (5) budget_tokens is honoured — asserted by SIZE, not presence
# ---------------------------------------------------------------------------

def _injected_block(prov) -> str:
    text = _messages_text(prov.seen_messages[0])
    start = text.index("[ADVISORY")
    return text[start:text.index("[/ADVISORY]", start)]


def test_budget_tokens_truncates_injected_pack(user_dir, monkeypatch):
    """A small budget truncates the pack; a large one does not. Size, not
    presence — the whole point of the knob is what it costs per turn."""
    _patch_effort(monkeypatch)
    full = (triggers.get_pack("threads") or {}).get("body") or ""
    assert len(full) > 800, "fixture assumption: threads.md is bigger than the budget"
    # Two fixed strings ride along AFTER the pack budget (§20): the exemplar
    # one-liner and the truncation marker. Bound the expected total by them
    # instead of pretending they do not exist.
    _marker = "\n[\u2026 truncated to budget]"
    exemplar = (triggers.get_trigger("thread_mention")["trigger"]["inject"]
                .get("exemplar") or "")
    extra_tok = (len("\nExample: ") + len(exemplar) + len(_marker) + 3) // 4

    _override("thread_mention", budget_tokens=50)   # 50 tok * 4 chars = 200
    ev = _Events()
    small = _injected_block(_run("add a tapped hole M6x1.0",
                                 [_neutral(), _done_reply()], events=ev))
    assert "[\u2026 truncated to budget]" in small
    pack_part = small[:small.index("[\u2026 truncated to budget]")]
    assert len(pack_part) <= 200 + 120, len(pack_part)   # budget + one item header
    fires = ev.rules()
    assert len(fires) == 1
    assert fires[0]["token_est"] <= 50 + extra_tok + 1

    # Same rule, generous budget: the pack now arrives whole and costs more.
    _override("thread_mention", budget_tokens=2000)
    ev2 = _Events()
    big = _injected_block(_run("add a tapped hole M6x1.0",
                               [_neutral(), _done_reply()], events=ev2))
    assert "truncated to budget" not in big
    assert len(big) > len(small) * 2
    assert ev2.rules()[0]["token_est"] > fires[0]["token_est"]
