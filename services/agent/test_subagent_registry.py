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

import json
import pytest

from services.agent import raw_log
from services.agent import subagent_registry as reg
from services.agent import subagent_rpc
from services.agent.agent_turn import run_turn
from services.agent.delegation import DelegationTree
from services.agent.mission_dispatch import MissionDispatch
from services.agent.providers.mock_provider import MockProvider


@pytest.fixture(autouse=True)
def _tmp_registry(tmp_path, monkeypatch):
    monkeypatch.setenv("CHUDWARE_SUBAGENTS_DIR", str(tmp_path / "subagents"))
    # Hermetic: delegate() rebuilds a per-role provider from model assignment
    # (WS2/mega-5) — ambient config.json ai_roles on a dev machine, or the
    # ollama default when config is empty. Either replaces the mock with a live
    # provider. Force the legacy no-assignment path instead.
    monkeypatch.setattr("services.agent.config.load_config", lambda *a, **k: {})
    monkeypatch.setattr("services.agent.role_models.resolve_for_node",
                        lambda *a, **k: None)
    monkeypatch.setattr("services.agent.role_models.resolve_role_model",
                        lambda *a, **k: None)
    # bind_session is a process-level global: clear it so a session-bound raw
    # path from one test can never leak into the next one's fallback assertions.
    reg.bind_session("", "")
    yield
    reg.bind_session("", "")


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
_LIST_CATS = '```json\n{"tool": "list_tool_categories", "args": {}}\n```'


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
    # A BUILD brief that finishes with zero real tool calls is an unearned
    # finish (the harness nudges, then fails it — browsing packs is not work).
    # This test covers steer DELIVERY, so use a brief that needs no tools.
    res = run_turn("summarize the shaft design decisions", prov, name="worker",
                   subagent_id="dg_s1")
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
    # mega-13 refuses a zero-tool done on product roles; the unearned-finish
    # gate goes further — a META call (list_tool_categories) is NOT work, so a
    # BUILD brief still needs a real tool. This test covers spawn/stream/
    # registry wiring, so the brief is an analysis one that needs no tools.
    prov = MockProvider([_LIST_CATS, _DONE])
    md = MissionDispatch(run_id="r_test", delegation_tree=tree, provider=prov,
                         on_event=on_event)
    out = md.delegate(role="sketcher", brief="review the base plate layout",
                      budget_tokens=2000)
    assert out.get("executed") is True and out.get("ok") is True
    # mega-8 multi-domain gate augments outputs with a residual typed_refusal —
    # assert the scripted handoff content, not exact envelope equality.
    assert (out.get("handoff") or {}).get("summary") == "finished"
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


def test_rpc_control_pause_resume_roundtrip():
    reg.register_session("dg_ctl_pause", role="worker", status="running")

    paused = _rpc(
        "subagent_control",
        {"subagent_id": "dg_ctl_pause", "op": "pause"},
    )
    assert paused["ok"] and paused["paused"] is True
    assert reg.is_paused("dg_ctl_pause")

    resumed = _rpc(
        "subagent_control",
        {"subagent_id": "dg_ctl_pause", "op": "resume"},
    )
    assert resumed["ok"] and resumed["paused"] is False
    assert not reg.is_paused("dg_ctl_pause")


def test_rpc_control_runtime_effort_validation_and_list_roundtrip():
    reg.register_session("dg_ctl_runtime", role="worker", status="running")

    refused = _rpc(
        "subagent_control",
        {
            "subagent_id": "dg_ctl_runtime",
            "op": "set_runtime",
            "effort": "impossibly-hard",
        },
    )
    assert not refused["ok"]
    assert reg.runtime_override("dg_ctl_runtime")["effort"] == ""

    updated = _rpc(
        "subagent_control",
        {
            "subagent_id": "dg_ctl_runtime",
            "op": "set_runtime",
            "effort": "high",
        },
    )
    assert updated["ok"] and updated["effort_override"] == "high"
    assert reg.runtime_override("dg_ctl_runtime")["effort"] == "high"
    listed = _rpc("subagent_list", {})
    session = next(
        item for item in listed["sessions"]
        if item["id"] == "dg_ctl_runtime"
    )
    assert session["effort_override"] == "high"


def test_rpc_list_defaults_control_keys_for_legacy_meta():
    sid = "dg_legacy"
    legacy = {
        "schema": reg.SCHEMA,
        "id": sid,
        "role": "worker",
        "status": "running",
        "created_ts": "2026-01-01T00:00:00+00:00",
        "updated_ts": "2026-01-01T00:00:00+00:00",
    }
    reg._meta_path(sid).write_text(json.dumps(legacy), encoding="utf-8")

    listed = _rpc("subagent_list", {})
    assert listed["ok"]
    session = next(item for item in listed["sessions"] if item["id"] == sid)
    assert {
        key: session[key]
        for key in (
            "paused",
            "model_override",
            "provider_override",
            "effort_override",
        )
    } == {
        "paused": False,
        "model_override": "",
        "provider_override": "",
        "effort_override": "",
    }


# ---------------------------------------------------------------------------
# Lossless raw transcript (contracts/worker_rpc.md §6.4, chudware.subagent.raw/1)
#
# The display transcript is a human-readable lifecycle+reply stream. The raw log
# is the archive: verbatim provider traffic, never truncated, never pruned. Before
# this existed a sub-agent's real conversation died with run_turn's local list.
# ---------------------------------------------------------------------------

def test_raw_log_lands_beside_the_owning_conversation(tmp_path):
    """A bound session puts the raw log in the conversation's sidecar dir."""
    sessions = tmp_path / "Sessions"
    reg.bind_session("conv_42", str(sessions / "conv_42.subagents"))
    reg.register_session("dg_raw_bound", role="worker", status="running")

    path = reg.raw_path("dg_raw_bound")
    assert path is not None
    assert path.parent == sessions / "conv_42.subagents"
    assert path.name == "dg_raw_bound.jsonl"
    assert reg.get_session("dg_raw_bound")["session_id"] == "conv_42"


def test_raw_log_falls_back_to_the_registry_cache_when_unbound():
    """No conversation bound (headless/bench) still gets a raw log, not none."""
    reg.register_session("dg_raw_unbound", role="worker", status="running")

    path = reg.raw_path("dg_raw_unbound")
    assert path is not None
    assert path.parent == reg.subagents_dir()


def test_raw_log_is_verbatim_and_uncapped():
    """The whole point: nothing in the raw log is clipped or summarized.

    The display transcript used to be the ONLY record and it stored
    ``str(reply)[:4000]``, so any longer reply was unrecoverable.
    """
    reg.register_session("dg_raw_big", role="worker", status="running")
    payload = "x" * 25000
    reg.append_raw("dg_raw_big", {
        "kind": "message",
        "message": {"role": "assistant", "content": payload,
                    "tool_calls": [{"name": "fs_write", "args": {"body": payload}}]},
    })

    records = reg.read_raw("dg_raw_big")
    body = [r for r in records if r.get("kind") == "message"]
    assert len(body) == 1
    message = body[0]["message"]
    assert message["content"] == payload
    assert message["tool_calls"][0]["args"]["body"] == payload


def test_raw_log_header_identifies_the_session_and_seq_is_monotonic():
    reg.bind_session("conv_hdr", "")
    reg.register_session("dg_raw_seq", role="sketcher", status="running",
                         parent_run_id="run_7")
    for i in range(3):
        reg.append_raw("dg_raw_seq", {"kind": "lifecycle", "event": "steer",
                                      "note": f"n{i}"})

    records = reg.read_raw("dg_raw_seq")
    header = records[0]
    assert header["kind"] == "header"
    assert header["schema"] == "chudware.subagent.raw/1"
    assert header["subagent_id"] == "dg_raw_seq"
    assert header["session_id"] == "conv_hdr"
    assert header["role"] == "sketcher"
    assert [r["seq"] for r in records[1:]] == [1, 2, 3]


def test_raw_log_seq_survives_a_process_restart():
    """seq is derived from the file, not from in-memory state.

    A sub-agent outlives the process that spawned it; a counter kept only in
    memory would silently restart at 1 and make the archive unorderable.
    """
    reg.register_session("dg_raw_restart", role="worker", status="running")
    reg.append_raw("dg_raw_restart", {"kind": "lifecycle", "event": "started"})
    reg.append_raw("dg_raw_restart", {"kind": "lifecycle", "event": "paused"})

    import importlib  # noqa: PLC0415
    importlib.reload(reg)
    reg.append_raw("dg_raw_restart", {"kind": "lifecycle", "event": "resumed"})

    assert [r["seq"] for r in reg.read_raw("dg_raw_restart")[1:]] == [1, 2, 3]


def test_raw_log_never_raises_into_the_agent_loop():
    """A broken archive must not take the run down with it."""
    reg.register_session("dg_raw_bad", role="worker", status="running")

    class _Unserializable:
        pass

    reg.append_raw("dg_raw_bad", {"kind": "message",
                                  "message": {"o": _Unserializable()}})
    reg.append_raw("dg_raw_bad", {"kind": "lifecycle", "event": "finished"})

    kinds = [r.get("kind") for r in reg.read_raw("dg_raw_bad")]
    assert "lifecycle" in kinds


def test_bound_main_turn_writes_full_agent_conversation(tmp_path):
    sessions = tmp_path / "Sessions"
    reg.bind_session("conv_main", str(sessions / "conv_main.subagents"))
    provider = RecordingProvider([_DONE])

    result = run_turn(
        "summarize the shaft design decisions",
        provider,
        name="general_agent",
    )

    assert result.success
    path = sessions / "conv_main.agent.jsonl"
    records = raw_log.read(path)
    assert records[0]["kind"] == "header"
    assert records[0]["schema"] == "chudware.chat.agent/1"
    assert records[0]["session_id"] == "conv_main"
    messages = [
        record["message"] for record in records
        if record.get("kind") == "message"
    ]
    assert messages[:-1] == provider.seen_messages[0]
    assert messages[-1]["role"] == "assistant"
    assert _msg_texts([messages[-1]]) == _DONE


def test_unbound_main_turn_writes_no_agent_archive(tmp_path):
    provider = RecordingProvider([_DONE])

    result = run_turn(
        "summarize the shaft design decisions",
        provider,
        name="general_agent",
    )

    assert result.success
    assert list(tmp_path.rglob("*.agent.jsonl")) == []


def test_rpc_transcript_raw_flag_returns_raw_records():
    reg.register_session("dg_raw_rpc", role="worker", status="running")
    reg.append_transcript("dg_raw_rpc", "subagent", "display copy")
    reg.append_raw("dg_raw_rpc", {
        "kind": "message",
        "message": {"role": "assistant", "content": "raw copy"}})

    display = _rpc("subagent_transcript", {"subagent_id": "dg_raw_rpc"})
    assert display["ok"]
    assert any(m.get("text") == "display copy" for m in display["messages"])

    raw = _rpc("subagent_transcript", {"subagent_id": "dg_raw_rpc", "raw": True})
    assert raw["ok"]
    assert any(
        m.get("message", {}).get("content") == "raw copy"
        for m in raw["messages"]
    )


def test_rpc_list_filters_by_owning_conversation():
    reg.bind_session("conv_a", "")
    reg.register_session("dg_in_a", role="worker", status="running")
    reg.bind_session("conv_b", "")
    reg.register_session("dg_in_b", role="worker", status="running")

    listed = _rpc("subagent_list", {"session_id": "conv_a"})
    ids = {item["id"] for item in listed["sessions"]}
    assert "dg_in_a" in ids
    assert "dg_in_b" not in ids


# ---------------------------------------------------------------------------
# Controller liveness — the ONLY automatic release of a pause hold.
# ---------------------------------------------------------------------------

def test_controller_heartbeat_marks_a_controller_alive():
    reg.note_controller("gui")
    assert reg.controller_alive()


def test_a_stale_controller_does_not_count_as_alive():
    """Liveness, not a deadline: an old poll is not a live controller."""
    reg.note_controller("gui")
    assert not reg.controller_alive(within_s=0.0)


def test_no_controller_has_ever_polled_reads_as_dead():
    """An unattended bench/CI run must never look like someone is watching."""
    assert not reg.controller_alive()


def test_subagent_list_stamps_the_controller_heartbeat():
    """The GUI's ~4s poll IS the heartbeat — nothing else writes it."""
    assert not reg.controller_alive()
    _rpc("subagent_list", {})
    assert reg.controller_alive()


def test_removed_approval_ops_are_rejected():
    """Spawn approval is automatic now; the ops are gone, not defaulted to yes."""
    reg.register_session("dg_no_approval", role="worker", status="running")
    for op in ("approve", "deny"):
        refused = _rpc("subagent_control",
                       {"subagent_id": "dg_no_approval", "op": op})
        assert not refused["ok"]
        assert "pause, resume, set_runtime" in refused["reason"]
