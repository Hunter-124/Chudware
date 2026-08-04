"""test_subagent_turn_boundary.py — the sub-agent turn boundary in run_turn.

This is the single place where cross-process control over a running sub-agent is
obeyed, and until now it had no direct coverage at all: the tests below drive
``agent_turn.run_turn(subagent_id=...)`` against real registry state.

What the boundary guarantees (contracts/worker_rpc.md §6.3):
  1. terminate wins over everything,
  2. a pause parks until a human resumes or terminates — there is NO deadline,
  3. the one automatic release is controller LIVENESS: with nobody polling the
     registry, the party that could send the resume is provably gone, so an
     unattended bench/CI run never parks at all,
  4. every provider message is mirrored verbatim into the lossless raw log.
"""
from __future__ import annotations

import threading
import time

import pytest

from services.agent import agent_turn
from services.agent import subagent_registry as reg
from services.agent.agent_turn import run_turn
from services.agent.providers.mock_provider import MockProvider

_DONE = '```json\n{"done": true, "handoff": {"summary": "finished"}}\n```'
_TOOL = '```json\n{"tool": "probe", "args": {"target": "top face"}}\n```'


@pytest.fixture(autouse=True)
def _tmp_registry(tmp_path, monkeypatch):
    monkeypatch.setenv("CHUDWARE_SUBAGENTS_DIR", str(tmp_path / "subagents"))
    monkeypatch.setattr("services.agent.config.load_config", lambda *a, **k: {})
    reg.bind_session("", "")
    yield
    reg.bind_session("", "")


def _analysis_turn(subagent_id: str, provider: MockProvider):
    """A brief that needs no tools — the unearned-finish gate fails BUILD briefs
    that call nothing, and these tests are about the boundary, not about work."""
    return run_turn("summarize the shaft design decisions", provider,
                    name="worker", subagent_id=subagent_id)


# ---------------------------------------------------------------------------
# Pause: parks until decided, with no clock
# ---------------------------------------------------------------------------

def test_pause_parks_until_a_human_resumes():
    reg.register_session("dg_p1", role="worker", status="running")
    reg.set_paused("dg_p1", True)
    reg.note_controller("gui")

    released_at = []

    def _resume_soon():
        time.sleep(0.8)
        released_at.append(time.monotonic())
        reg.set_paused("dg_p1", False)

    waker = threading.Thread(target=_resume_soon, daemon=True)
    waker.start()
    started = time.monotonic()
    res = _analysis_turn("dg_p1", MockProvider([_DONE]))
    waker.join()

    assert res.success
    # It really parked: the turn could not have finished before the resume.
    assert released_at and time.monotonic() - started >= 0.8
    assert not reg.is_paused("dg_p1")
    assert reg.get_session("dg_p1")["status"] == "running"
    notes = " ".join(r["text"] for r in reg.get_transcript("dg_p1"))
    assert "Pause hold entered" in notes
    assert "resumed at this turn boundary" in notes


def test_pause_hold_has_no_deadline(monkeypatch):
    """A pause is not a timer. Nothing may resume it on elapsed time alone.

    The old behavior auto-resumed after CHUDWARE_SUBAGENT_HOLD_MAX_S (900 s).
    Proven here by holding the controller alive and the pause set, letting the
    poll loop iterate many times, and requiring it to still be parked.
    """
    reg.register_session("dg_p2", role="worker", status="running")
    reg.set_paused("dg_p2", True)
    reg.note_controller("gui")
    monkeypatch.setattr(reg, "controller_alive", lambda *a, **k: True)

    # A fake clock the hold loop cannot outrun: every sleep jumps an hour.
    real_sleep = time.sleep
    elapsed = {"s": 0.0}

    def _fast_forward(seconds):
        elapsed["s"] += 3600.0
        real_sleep(0.01)
        if elapsed["s"] > 36000.0:  # ten simulated hours
            reg.set_paused("dg_p2", False)

    monkeypatch.setattr(agent_turn.time, "sleep", _fast_forward)
    res = _analysis_turn("dg_p2", MockProvider([_DONE]))

    assert res.success
    # Ten simulated hours passed and only the explicit resume let it go.
    assert elapsed["s"] > 36000.0
    notes = " ".join(r["text"] for r in reg.get_transcript("dg_p2"))
    assert "no controller left" not in notes


def test_terminate_wins_over_a_pause_hold():
    reg.register_session("dg_p3", role="worker", status="running")
    reg.set_paused("dg_p3", True)
    reg.note_controller("gui")

    def _terminate_soon():
        time.sleep(0.8)
        reg.request_terminate("dg_p3")

    waker = threading.Thread(target=_terminate_soon, daemon=True)
    waker.start()
    res = _analysis_turn("dg_p3", MockProvider([_DONE]))
    waker.join()

    assert not res.success and res.stop_reason == "terminated_by_user"
    assert reg.get_session("dg_p3")["status"] == "terminated"


# ---------------------------------------------------------------------------
# Controller liveness: the only automatic release
# ---------------------------------------------------------------------------

def test_unattended_run_never_parks_on_a_pause():
    """No controller has ever polled: a bench/CI run must not stall."""
    reg.register_session("dg_p4", role="worker", status="running")
    reg.set_paused("dg_p4", True)
    assert not reg.controller_alive()

    started = time.monotonic()
    res = _analysis_turn("dg_p4", MockProvider([_DONE]))

    assert res.success
    assert time.monotonic() - started < 2.0
    notes = " ".join(r["text"] for r in reg.get_transcript("dg_p4"))
    assert "Pause hold entered" not in notes


def test_pause_releases_when_the_controller_dies(monkeypatch):
    """The GUI that paused it went away — nobody is left who could resume."""
    reg.register_session("dg_p5", role="worker", status="running")
    reg.set_paused("dg_p5", True)

    polls = {"n": 0}

    def _alive(*_a, **_k):
        polls["n"] += 1
        return polls["n"] <= 3  # alive at entry, then the controller vanishes

    monkeypatch.setattr(reg, "controller_alive", _alive)
    res = _analysis_turn("dg_p5", MockProvider([_DONE]))

    assert res.success
    assert not reg.is_paused("dg_p5")
    notes = " ".join(r["text"] for r in reg.get_transcript("dg_p5"))
    assert "no controller left that could resume this sub-agent" in notes


# ---------------------------------------------------------------------------
# Lossless capture through a real turn
# ---------------------------------------------------------------------------

def test_run_turn_mirrors_the_whole_conversation_into_the_raw_log(tmp_path):
    """The conversation used to die with run_turn's local `messages` list.

    Losslessness is defined against what the PROVIDER actually saw: the raw log
    must reproduce every message of the real conversation, in order, verbatim.
    (Tool output is budget-capped on its way INTO the conversation — that is a
    separate, deliberate decision, and the log honestly records what was sent.)
    """
    reg.bind_session("conv_live", str(tmp_path / "conv_live.subagents"))
    reg.register_session("dg_raw1", role="worker", status="running")

    seen_by_provider: list[list[dict]] = []

    class _Recording(MockProvider):
        def complete(self, system, messages, max_tokens=8000):
            seen_by_provider.append([dict(m) for m in messages])
            return super().complete(system, messages, max_tokens)

    def tool_dispatch(tool, args):
        return {"ok": True, "detail": "probe result"}

    res = run_turn("probe the part", _Recording([_TOOL, _DONE]),
                   name="worker", tool_dispatch=tool_dispatch,
                   subagent_id="dg_raw1")
    assert res.success

    records = reg.read_raw("dg_raw1")
    assert records[0]["kind"] == "header"
    assert records[0]["session_id"] == "conv_live"

    mirrored = [r["message"] for r in records if r.get("kind") == "message"]
    final_conversation = seen_by_provider[-1]
    # Every message the provider was sent is in the log, in order and verbatim.
    assert mirrored[:len(final_conversation)] == final_conversation
    # …and the log goes further: the reply to that last call is captured too,
    # which is exactly what used to be lost when run_turn returned.
    assert len(mirrored) > len(final_conversation)
    assert mirrored[-1]["role"] == "assistant"

    lifecycle = [r["event"] for r in records if r.get("kind") == "lifecycle"]
    assert "started" in lifecycle


def test_raw_log_does_not_clip_a_long_model_reply():
    """The old record kept `str(reply)[:4000]`; anything longer was gone."""
    reg.register_session("dg_raw3", role="worker", status="running")
    summary = "z" * 12000
    long_done = (
        '```json\n{"done": true, "handoff": {"summary": "' + summary + '"}}\n```')

    res = _analysis_turn("dg_raw3", MockProvider([long_done]))
    assert res.success

    raw_blob = "\n".join(
        str(r.get("message", {}).get("content"))
        for r in reg.read_raw("dg_raw3") if r.get("kind") == "message")
    assert summary in raw_blob
    display = "\n".join(r["text"] for r in reg.get_transcript("dg_raw3"))
    assert summary in display


def test_raw_log_records_compaction_verbatim():
    """In-place context compaction must widen the archive, never shrink it."""
    reg.register_session("dg_raw2", role="worker", status="running")
    mirrored = agent_turn._RawMirroredMessages(
        "dg_raw2", reg,
        [{"role": "user", "content": "keep me"},
         {"role": "user", "content": "Result of probe: drop me"},
         {"role": "assistant", "content": "tail"}])

    del mirrored[1]

    records = reg.read_raw("dg_raw2")
    compactions = [r for r in records if r.get("kind") == "compaction"]
    assert len(compactions) == 1
    assert compactions[0]["before"] == 3 and compactions[0]["after"] == 2
    # The dropped message is still recoverable from the earlier message records.
    blob = "\n".join(str(r) for r in records)
    assert "drop me" in blob
