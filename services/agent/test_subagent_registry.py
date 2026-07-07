"""test_subagent_registry.py — Phase 4 (M11/M15–M17) registry + live-steering tests.

Proves, offline (mock provider, tmpdir registry — no all-skip):
  * the file-backed session registry: register / list / status / transcript,
  * the steering queue: queue → pending → consume (exactly-once) semantics,
  * the REAL consumption point: ``agent_turn.run_turn(subagent_id=...)`` drains
    queued steers at each model-call boundary (including a steer queued MID-turn,
    simulating the GUI's cross-process ``subagent_steer``),
  * the terminate guard: honored at the turn boundary, never a hard kill,
  * the full spawn→run path: ``MissionDispatch.delegate`` with a bound tree +
    provider registers the session, streams ``agent.spawn`` to the live sink,
    executes the sub-agent, and lands its transcript,
  * the ``subagent_*`` worker RPC envelopes (list / transcript / steer / the
    M16 confirm-guarded terminate).
"""
from __future__ import annotations

import pytest

from services.agent import subagent_registry as reg
from services.agent import subagent_rpc
from services.agent.agent_turn import run_turn
from services.agent.delegation import DelegationTree
from services.agent.mission_dispatch import MissionDispatch
from services.agent.providers.mock_provider import MockProvider


@pytest.fixture(autouse=True)
def _tmp_registry(tmp_path, monkeypatch):
    monkeypatch.setenv("AICAD_SUBAGENTS_DIR", str(tmp_path / "subagents"))
    yield


class RecordingProvider(MockProvider):
    """MockProvider that also records every messages list it was called with."""

    def __init__(self, scripted_replies):
        super().__init__(scripted_replies)
        self.seen_messages: list[list[dict]] = []

    def complete(self, system, messages, max_tokens=8000):
        self.seen_messages.append([dict(m) for m in messages])
        return super().complete(system, messages, max_tokens)


def _msg_texts(messages: list[dict]) -> str:
    out = []
    for m in messages:
        c = m.get("content")
        if isinstance(c, str):
            out.append(c)
        elif isinstance(c, list):
            out.extend(str(p.get("text", "")) for p in c if isinstance(p, dict))
    return "\n".join(out)


_DONE = '```json\n{"done": true, "handoff": {"summary": "finished"}}\n```'
_TOOL = '```json\n{"tool": "probe", "args": {"target": "top face"}}\n```'


# ---------------------------------------------------------------------------
# Registry basics
# ---------------------------------------------------------------------------

def test_register_list_and_status_roundtrip():
    meta = reg.register_session("dg_abc123", role="sketcher", brief="draw the plate",
                                parent_run_id="r_1", budget_tokens=4000)
    assert meta["status"] == "pending" and meta["schema"] == reg.SCHEMA
    assert reg.set_status("dg_abc123", "running")
    got = reg.get_session("dg_abc123")
    assert got["status"] == "running"
    listed = reg.list_sessions()
    assert [s["id"] for s in listed] == ["dg_abc123"]
    assert listed[0]["pending_steers"] == 0
    # unknown status rejected; unknown session is None
    assert not reg.set_status("dg_abc123", "bogus")
    assert reg.get_session("nope") is None


def test_bad_ids_rejected():
    with pytest.raises(ValueError):
        reg.register_session("../evil", role="x")
    assert reg.get_transcript("../evil") == []


def test_transcript_append_and_fetch():
    reg.register_session("dg_t1", role="worker")
    reg.append_transcript("dg_t1", "system", "started")
    reg.append_transcript("dg_t1", "subagent", "hello")
    recs = reg.get_transcript("dg_t1")
    assert [(r["who"], r["text"]) for r in recs] == [("system", "started"),
                                                     ("subagent", "hello")]


# ---------------------------------------------------------------------------
# Steering queue semantics
# ---------------------------------------------------------------------------

def test_steer_queue_consume_exactly_once():
    reg.register_session("dg_q1", role="worker")
    a = reg.queue_steer("dg_q1", "focus on the fillet")
    b = reg.queue_steer("dg_q1", "use 6061 aluminum")
    assert [s["steer_id"] for s in reg.pending_steers("dg_q1")] == \
        [a["steer_id"], b["steer_id"]]
    drained = reg.consume_steers("dg_q1")
    assert [s["text"] for s in drained] == ["focus on the fillet",
                                            "use 6061 aluminum"]
    assert reg.pending_steers("dg_q1") == []
    assert reg.consume_steers("dg_q1") == []  # exactly-once


def test_terminate_flag():
    reg.register_session("dg_k1", role="worker")
    assert not reg.terminate_requested("dg_k1")
    assert reg.request_terminate("dg_k1")
    assert reg.terminate_requested("dg_k1")
    assert not reg.request_terminate("unknown")


# ---------------------------------------------------------------------------
# The REAL consumption point: agent_turn.run_turn(subagent_id=...)
# ---------------------------------------------------------------------------

def test_run_turn_consumes_pre_queued_steer():
    reg.register_session("dg_s1", role="worker", status="running")
    reg.queue_steer("dg_s1", "prefer a revolve over a loft")
    prov = RecordingProvider([_DONE])
    res = run_turn("model the shaft", prov, name="worker", subagent_id="dg_s1")
    assert res.success
    # the steer landed as a user message BEFORE the first completion
    assert "[Human steering] prefer a revolve over a loft" in \
        _msg_texts(prov.seen_messages[0])
    assert reg.pending_steers("dg_s1") == []
    # transcript captured the reply + the applied-steer marker
    whos = [r["who"] for r in reg.get_transcript("dg_s1")]
    assert "subagent" in whos and "system" in whos


def test_run_turn_consumes_steer_queued_mid_turn():
    """A steer queued while the sub-agent is mid-turn (the GUI's cross-process
    subagent_steer) is applied at the NEXT model-call boundary."""
    reg.register_session("dg_s2", role="worker", status="running")

    def tool_dispatch(tool, args):
        # simulate the user steering from another process during the tool call
        reg.queue_steer("dg_s2", "stop after this step and report")
        return {"ok": True}

    prov = RecordingProvider([_TOOL, _DONE])
    res = run_turn("probe the part", prov, name="worker",
                   tool_dispatch=tool_dispatch, subagent_id="dg_s2")
    assert res.success
    # not in the first call, present in the second
    assert "stop after this step" not in _msg_texts(prov.seen_messages[0])
    assert "[Human steering] stop after this step and report" in \
        _msg_texts(prov.seen_messages[1])


def test_run_turn_honors_terminate_at_boundary():
    reg.register_session("dg_s3", role="worker", status="running")
    reg.request_terminate("dg_s3")
    prov = RecordingProvider([_DONE])
    res = run_turn("model something", prov, name="worker", subagent_id="dg_s3")
    assert not res.success and res.stop_reason == "terminated_by_user"
    assert prov.seen_messages == []  # stopped BEFORE any model call
    assert reg.get_session("dg_s3")["status"] == "terminated"


# ---------------------------------------------------------------------------
# Full spawn→register→execute path (MissionDispatch + DelegationTree)
# ---------------------------------------------------------------------------

def test_delegate_registers_streams_spawn_and_executes():
    events: list[dict] = []

    def on_event(data: dict) -> None:  # loop.py single-dict live convention
        events.append(dict(data))

    tree = DelegationTree(run_id="r_test", on_event=on_event)
    prov = MockProvider([_DONE])
    md = MissionDispatch(run_id="r_test", delegation_tree=tree, provider=prov,
                         on_event=on_event)
    out = md.delegate(role="sketcher", brief="sketch the base plate",
                      budget_tokens=2000)
    assert out.get("executed") is True and out.get("ok") is True
    assert out.get("handoff") == {"summary": "finished"}
    node_id = out["id"]
    # agent.spawn streamed live with the child's subagent_id
    spawns = [e for e in events if e.get("kind") == "agent.spawn"]
    assert len(spawns) == 1 and spawns[0]["subagent_id"] == node_id
    # registry session ended done, with a real transcript
    sess = reg.get_session(node_id)
    assert sess["status"] == "done" and sess["role"] == "sketcher"
    assert any(r["who"] == "subagent" for r in reg.get_transcript(node_id))


def test_delegate_without_provider_is_honest_not_executed():
    tree = DelegationTree(run_id="r_np")
    md = MissionDispatch(run_id="r_np", delegation_tree=tree)  # no provider
    out = md.delegate(role="sketcher", brief="do a thing", budget_tokens=1000)
    assert out.get("executed") is False and "NOT" in out.get("note", "")
    assert reg.get_session(out["id"])["status"] == "approved"  # spawned, not run


# ---------------------------------------------------------------------------
# Worker RPC envelopes
# ---------------------------------------------------------------------------

def _rpc(name, params):
    return subagent_rpc.WORKER_METHODS[name](params, lambda *_: None)


def test_rpc_list_transcript_steer_roundtrip():
    reg.register_session("dg_r1", role="reviewer", brief="check it",
                         status="running")
    out = _rpc("subagent_list", {})
    assert out["ok"] and out["sessions"][0]["id"] == "dg_r1"

    st = _rpc("subagent_steer", {"subagent_id": "dg_r1", "text": "look closer"})
    assert st["ok"] and st["queued"] and "turn boundary" in st["note"]

    tr = _rpc("subagent_transcript", {"subagent_id": "dg_r1"})
    assert tr["ok"] and tr["session"]["id"] == "dg_r1"
    assert len(tr["pending_steers"]) == 1
    assert any(r.get("queued") for r in tr["messages"])  # steer mirrored to viewer

    # error envelopes, never raises
    assert not _rpc("subagent_transcript", {"subagent_id": "nope"})["ok"]
    assert not _rpc("subagent_steer", {"subagent_id": "dg_r1", "text": "  "})["ok"]


def test_rpc_terminate_requires_confirm():
    reg.register_session("dg_r2", role="worker", status="running")
    refused = _rpc("subagent_terminate", {"subagent_id": "dg_r2"})
    assert not refused["ok"] and refused.get("requires_confirm") is True
    assert not reg.terminate_requested("dg_r2")
    ok = _rpc("subagent_terminate", {"subagent_id": "dg_r2", "confirm": True})
    assert ok["ok"] and ok["terminate_requested"]
    assert reg.terminate_requested("dg_r2")
