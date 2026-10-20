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
    M16 confirm-guarded terminate), including the hub's project/session scope.
"""

from __future__ import annotations

import json
import os
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
    monkeypatch.setattr(
        "services.agent.role_models.resolve_for_node", lambda *a, **k: None
    )
    monkeypatch.setattr(
        "services.agent.role_models.resolve_role_model", lambda *a, **k: None
    )
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
    meta = reg.register_session(
        "dg_abc123",
        role="sketcher",
        brief="draw the plate",
        parent_run_id="r_1",
        budget_tokens=4000,
    )
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


def test_old_live_runner_is_not_abandoned_by_age():
    reg.register_session("dg_live_old", role="worker", status="running")
    meta = reg.get_session("dg_live_old")
    meta["created_ts"] = "2020-01-01T00:00:00+00:00"
    meta["runner_heartbeat_ts"] = "2020-01-01T00:00:00+00:00"
    reg._write_meta(meta["id"], meta)
    assert reg.list_sessions()[0]["status"] == "running"
    assert reg.get_session(meta["id"])["status"] == "running"


def test_dead_runner_is_persistently_abandoned(monkeypatch):
    meta = reg.register_session("dg_orphan", role="worker", status="running")
    meta["owner_pid"] = 987654321
    reg._write_meta(meta["id"], meta)
    original_kill = reg.os.kill

    def missing_runner(pid, signal):
        if pid == meta["owner_pid"]:
            raise ProcessLookupError(pid)
        return original_kill(pid, signal)

    monkeypatch.setattr(reg.os, "kill", missing_runner)
    first = reg.list_sessions()[0]
    assert first["status"] == "abandoned"
    assert "pid" in first["status_note"]
    assert reg.get_session(meta["id"])["status"] == "abandoned"
    assert reg.list_sessions()[0]["status"] == "abandoned"
    assert not _rpc("subagent_control", {
        "subagent_id": meta["id"], "op": "pause",
    })["ok"]


def test_reused_runner_pid_is_abandoned(monkeypatch):
    meta = reg.register_session("dg_reused", role="worker", status="waiting")
    monkeypatch.setattr(reg, "_process_start_time", lambda pid: "different-start")
    assert reg.list_sessions()[0]["status"] == "abandoned"
    assert "reused" in reg.get_session(meta["id"])["status_note"]


def test_legacy_orphan_expires_but_terminal_records_are_untouched():
    old = "2020-01-01T00:00:00+00:00"
    for sid, status in (("dg_legacy_old", "in_progress"), ("dg_done_old", "done")):
        reg._write_meta(sid, {
            "schema": reg.SCHEMA, "id": sid, "status": status,
            "created_ts": old, "updated_ts": old,
        })
    rows = {row["id"]: row for row in reg.list_sessions()}
    assert rows["dg_legacy_old"]["status"] == "abandoned"
    assert rows["dg_done_old"]["status"] == "done"
    assert reg.get_session("dg_done_old")["updated_ts"] == old


def test_project_filter_excludes_foreign_and_unscoped_records(tmp_path, monkeypatch):
    monkeypatch.delenv("CHUDWARE_PROJECT_DIR", raising=False)
    monkeypatch.delenv("CHUDWARE_FS_ROOT", raising=False)
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(first, target_is_directory=True)
    reg.register_session("dg_first", role="worker", project_dir=str(first))
    reg.register_session("dg_second", role="worker", project_dir=str(second))
    reg._write_meta("dg_unscoped", {
        "schema": reg.SCHEMA, "id": "dg_unscoped", "status": "done",
        "created_ts": reg._now(), "updated_ts": reg._now(),
    })
    assert {row["id"] for row in _rpc("subagent_list", {})["sessions"]} == {
        "dg_first", "dg_second", "dg_unscoped",
    }
    scoped = _rpc("subagent_list", {"project_dir": str(alias), "limit": 1})
    assert scoped["ok"]
    assert [row["id"] for row in scoped["sessions"]] == ["dg_first"]
    assert reg.get_session("dg_first")["project_dir"] == str(first)


def test_environment_project_root_precedes_fs_root_and_fallback(tmp_path, monkeypatch):
    project = tmp_path / "project"
    jail = tmp_path / "jail"
    project.mkdir()
    jail.mkdir()
    monkeypatch.setenv("CHUDWARE_PROJECT_DIR", str(project))
    monkeypatch.setenv("CHUDWARE_FS_ROOT", str(jail))
    registered = reg.register_session("dg_env", role="worker", project_dir=str(jail))
    assert registered["project_dir"] == str(project)
    monkeypatch.delenv("CHUDWARE_PROJECT_DIR")
    assert reg.register_session("dg_fs", role="worker")["project_dir"] == str(jail)


def test_explicit_nested_workspace_is_not_widened_to_a_marked_parent(
    tmp_path, monkeypatch,
):
    from services.agent import config as agent_config
    from services.agent.project_context import resolve_project_root

    monkeypatch.delenv("CHUDWARE_PROJECT_DIR", raising=False)
    monkeypatch.delenv("CHUDWARE_FS_ROOT", raising=False)
    outer = tmp_path / "outer"
    (outer / ".plm").mkdir(parents=True)
    project_a = outer / "scratch" / "Project-A"
    project_b = outer / "scratch" / "Project-B"
    (project_a / ".chudware").mkdir(parents=True)
    project_b.mkdir(parents=True)
    # Precondition: marker discovery alone would merge both into the parent.
    assert resolve_project_root(project_a) == outer
    assert resolve_project_root(project_b) == outer

    a = reg.register_session("dg_nested_a", role="worker", project_dir=str(project_a))
    b = reg.register_session("dg_nested_b", role="worker",
                             project_dir=str(project_b) + "/")
    assert a["project_dir"] == os.path.realpath(project_a)
    assert b["project_dir"] == os.path.realpath(project_b)
    scoped = _rpc("subagent_list", {"project_dir": str(project_a)})
    assert [row["id"] for row in scoped["sessions"]] == ["dg_nested_a"]
    assert _rpc("subagent_transcript", {
        "subagent_id": "dg_nested_b", "project_dir": str(project_a),
    }) == {"ok": False, "reason": "Sub-agent is outside the requested scope."}

    # Explicit environment roots keep their precedence and are not widened.
    monkeypatch.setenv("CHUDWARE_FS_ROOT", str(project_b))
    assert reg.register_session("dg_nested_fs", role="worker")["project_dir"] == (
        os.path.realpath(project_b))
    monkeypatch.setenv("CHUDWARE_PROJECT_DIR", str(project_a))
    assert reg.register_session(
        "dg_nested_env", role="worker", project_dir=str(project_b),
    )["project_dir"] == os.path.realpath(project_a)

    # Only the ambient configured workspace climbs to its marked project root.
    monkeypatch.delenv("CHUDWARE_PROJECT_DIR")
    monkeypatch.delenv("CHUDWARE_FS_ROOT")
    monkeypatch.setattr(agent_config, "workspace_dir", lambda: str(project_a))
    assert reg.resolve_project_dir() == os.path.realpath(outer)



def test_bad_ids_rejected():
    with pytest.raises(ValueError):
        reg.register_session("../evil", role="x")
    assert reg.get_transcript("../evil") == []


def test_transcript_append_and_fetch():
    reg.register_session("dg_t1", role="worker")
    reg.append_transcript("dg_t1", "system", "started")
    reg.append_transcript("dg_t1", "subagent", "hello")
    recs = reg.get_transcript("dg_t1")
    assert [(r["who"], r["text"]) for r in recs] == [
        ("system", "started"),
        ("subagent", "hello"),
    ]


# ---------------------------------------------------------------------------
# Steering queue semantics
# ---------------------------------------------------------------------------


def test_steer_queue_consume_exactly_once():
    reg.register_session("dg_q1", role="worker")
    a = reg.queue_steer("dg_q1", "focus on the fillet")
    b = reg.queue_steer("dg_q1", "use 6061 aluminum")
    assert [s["steer_id"] for s in reg.pending_steers("dg_q1")] == [
        a["steer_id"],
        b["steer_id"],
    ]
    drained = reg.consume_steers("dg_q1")
    assert [s["text"] for s in drained] == ["focus on the fillet", "use 6061 aluminum"]
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
    res = run_turn(
        "summarize the shaft design decisions", prov, name="worker", subagent_id="dg_s1"
    )
    assert res.success
    # the steer landed as a user message BEFORE the first completion
    assert "[Human steering] prefer a revolve over a loft" in _msg_texts(
        prov.seen_messages[0]
    )
    assert reg.pending_steers("dg_s1") == []
    # transcript captured the reply + the applied-steer marker
    whos = [r["who"] for r in reg.get_transcript("dg_s1")]
    assert "subagent" in whos and "system" in whos


def test_turn_boundary_keeps_pidless_runner_live():
    meta = reg.register_session("dg_pidless", role="worker", status="running")
    meta["owner_pid"] = None
    meta["runner_heartbeat_ts"] = "2020-01-01T00:00:00+00:00"
    reg._write_meta(meta["id"], meta)
    result = run_turn(
        "summarize the shaft design decisions",
        RecordingProvider([_DONE]),
        name="worker",
        subagent_id=meta["id"],
    )
    assert result.success
    assert reg.list_sessions()[0]["status"] == "running"


def test_run_turn_consumes_steer_queued_mid_turn():
    """A steer queued while the sub-agent is mid-turn (the GUI's cross-process
    subagent_steer) is applied at the NEXT model-call boundary."""
    reg.register_session("dg_s2", role="worker", status="running")

    def tool_dispatch(tool, args):
        # simulate the user steering from another process during the tool call
        reg.queue_steer("dg_s2", "stop after this step and report")
        return {"ok": True}

    prov = RecordingProvider([_TOOL, _DONE])
    res = run_turn(
        "probe the part",
        prov,
        name="worker",
        tool_dispatch=tool_dispatch,
        subagent_id="dg_s2",
    )
    assert res.success
    # not in the first call, present in the second
    assert "stop after this step" not in _msg_texts(prov.seen_messages[0])
    assert "[Human steering] stop after this step and report" in _msg_texts(
        prov.seen_messages[1]
    )


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


def test_delegate_registers_streams_spawn_and_executes(monkeypatch, tmp_path):
    from services.agent import chudcode_roles
    monkeypatch.delenv("CHUDWARE_PROJECT_DIR", raising=False)
    monkeypatch.delenv("CHUDWARE_FS_ROOT", raising=False)
    project = tmp_path / "active-project"
    project.mkdir()

    events: list[dict] = []

    def on_event(data: dict) -> None:  # loop.py single-dict live convention
        events.append(dict(data))

    monkeypatch.setattr(
        chudcode_roles,
        "run_role_turn",
        lambda *args, **kwargs: {
            "success": True,
            "stop_reason": "completed",
            "result": "finished",
            "session_id": "cad-role-registry",
            "tokens_used": 120,
            "provider_usage_recorded": True,
            "allowed_tools": ["measure_part"],
            "cancelled": False,
        },
    )
    tree = DelegationTree(run_id="r_test", on_event=on_event)
    md = MissionDispatch(
        run_id="r_test",
        delegation_tree=tree,
        on_event=on_event,
        options={"session_id": "chat-registry", "project_dir": str(project)},
    )
    out = md.delegate(
        role="sketcher",
        brief="review the base plate layout",
        budget_tokens=2000,
    )
    assert out.get("executed") is True and out.get("ok") is True
    assert (out.get("handoff") or {}).get("summary") == "finished"
    node_id = out["id"]
    spawns = [event for event in events if event.get("kind") == "agent.spawn"]
    assert len(spawns) == 1 and spawns[0]["subagent_id"] == node_id
    sess = reg.get_session(node_id)
    assert sess["status"] == "done" and sess["role"] == "sketcher"
    assert sess["project_dir"] == str(project)
    assert any(row["who"] == "subagent" for row in reg.get_transcript(node_id))


def test_delegate_without_python_provider_surfaces_chudcode_failure(monkeypatch):
    from services.agent import chudcode_roles

    monkeypatch.setattr(
        chudcode_roles,
        "run_role_turn",
        lambda *args, **kwargs: {
            "success": False,
            "stop_reason": "provider_error",
            "result": {"error": "Chudcode session unavailable"},
            "tokens_used": 0,
            "provider_usage_recorded": False,
            "cancelled": False,
        },
    )
    tree = DelegationTree(run_id="r_np")
    md = MissionDispatch(run_id="r_np", delegation_tree=tree)
    out = md.delegate(role="sketcher", brief="do a thing", budget_tokens=1000)
    assert out.get("executed") is True and out.get("ok") is False
    assert out.get("reason") == "subagent_provider_error"
    assert "Chudcode session unavailable" in (out.get("error") or "")
    assert reg.get_session(out["id"])["status"] == "failed"


# ---------------------------------------------------------------------------
# Worker RPC envelopes
# ---------------------------------------------------------------------------


def _rpc(name, params):
    return subagent_rpc.WORKER_METHODS[name](params, lambda *_: None)


def test_rpc_list_transcript_steer_roundtrip():
    reg.register_session("dg_r1", role="reviewer", brief="check it", status="running")
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
        item for item in listed["sessions"] if item["id"] == "dg_ctl_runtime"
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
    reg.append_raw(
        "dg_raw_big",
        {
            "kind": "message",
            "message": {
                "role": "assistant",
                "content": payload,
                "tool_calls": [{"name": "fs_write", "args": {"body": payload}}],
            },
        },
    )

    records = reg.read_raw("dg_raw_big")
    body = [r for r in records if r.get("kind") == "message"]
    assert len(body) == 1
    message = body[0]["message"]
    assert message["content"] == payload
    assert message["tool_calls"][0]["args"]["body"] == payload


def test_raw_log_header_identifies_the_session_and_seq_is_monotonic():
    reg.bind_session("conv_hdr", "")
    reg.register_session(
        "dg_raw_seq", role="sketcher", status="running", parent_run_id="run_7"
    )
    for i in range(3):
        reg.append_raw(
            "dg_raw_seq", {"kind": "lifecycle", "event": "steer", "note": f"n{i}"}
        )

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

    reg.append_raw(
        "dg_raw_bad", {"kind": "message", "message": {"o": _Unserializable()}}
    )
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
        record["message"] for record in records if record.get("kind") == "message"
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
    reg.append_raw(
        "dg_raw_rpc",
        {"kind": "message", "message": {"role": "assistant", "content": "raw copy"}},
    )

    display = _rpc("subagent_transcript", {"subagent_id": "dg_raw_rpc"})
    assert display["ok"]
    assert any(m.get("text") == "display copy" for m in display["messages"])

    raw = _rpc("subagent_transcript", {"subagent_id": "dg_raw_rpc", "raw": True})
    assert raw["ok"]
    assert any(
        m.get("message", {}).get("content") == "raw copy" for m in raw["messages"]
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
# Hub scope: project_dir / session_id on every detail and control RPC
# ---------------------------------------------------------------------------


_OUT_OF_SCOPE = {"ok": False, "reason": "Sub-agent is outside the requested scope."}

# Every registry call that reads a transcript/steer queue or writes a record.
_SCOPE_GUARDED_CALLS = (
    "get_transcript", "read_raw", "pending_steers", "queue_steer",
    "append_transcript", "request_terminate", "set_paused",
    "set_runtime_override", "runtime_override",
)


def _scoped_records(tmp_path, monkeypatch) -> dict:
    """Four records with fixed ownership and updated_ts (oldest first below).

    dg_home      first project, conv_a   (oldest)
    dg_sibling   first project, conv_b
    dg_foreign   second project, conv_a
    dg_unowned   no project, no session  (newest)
    """
    monkeypatch.delenv("CHUDWARE_PROJECT_DIR", raising=False)
    monkeypatch.delenv("CHUDWARE_FS_ROOT", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(first, target_is_directory=True)
    owners = (
        ("dg_home", first, "conv_a"),
        ("dg_sibling", first, "conv_b"),
        ("dg_foreign", second, "conv_a"),
    )
    for sid, project, session in owners:
        reg.register_session(sid, role="worker", status="running",
                             session_id=session, project_dir=str(project))
    reg._write_meta("dg_unowned", {
        "schema": reg.SCHEMA, "id": "dg_unowned", "role": "worker",
        "status": "done", "created_ts": "2026-01-01T00:00:04.000+00:00",
    })
    ordered = ("dg_home", "dg_sibling", "dg_foreign", "dg_unowned")
    for sid in ordered:
        reg.append_transcript(sid, "subagent", f"{sid} display")
        reg.queue_steer(sid, f"{sid} pending", source="user")
    for sid in ordered[:3]:
        reg.append_raw(sid, {"kind": "message",
                             "message": {"role": "assistant",
                                         "content": f"{sid} raw"}})
    # Pin the newest-first order last: transcript appends bump updated_ts.
    for index, sid in enumerate(ordered, start=1):
        meta = reg.get_session(sid)
        meta["updated_ts"] = f"2026-01-01T00:00:0{index}.000+00:00"
        reg._write_meta(sid, meta)
    return {"first": first, "second": second, "alias": alias}


def _guard_registry(monkeypatch) -> list:
    calls: list = []
    for name in _SCOPE_GUARDED_CALLS:
        original = getattr(reg, name)

        def spy(*args, _name=name, _original=original, **kwargs):
            calls.append(_name)
            return _original(*args, **kwargs)

        monkeypatch.setattr(reg, name, spy)
    return calls


def _registry_files(sid: str) -> dict:
    paths = [reg._meta_path(sid), reg._transcript_path(sid), reg._steer_path(sid)]
    raw = reg.raw_path(sid)
    if raw is not None:
        paths.append(raw)
    return {str(p): p.read_bytes() if p.exists() else None for p in paths}


def _hub_calls(sid: str, scope: dict) -> list:
    return [
        ("subagent_transcript", {"subagent_id": sid, **scope}),
        ("subagent_transcript", {"subagent_id": sid, "raw": True, **scope}),
        ("subagent_steer", {"subagent_id": sid, "text": "look here", **scope}),
        ("subagent_terminate", {"subagent_id": sid, "confirm": True, **scope}),
        ("subagent_control", {"subagent_id": sid, "op": "pause", **scope}),
        ("subagent_control", {"subagent_id": sid, "op": "resume", **scope}),
        ("subagent_control", {"subagent_id": sid, "op": "set_runtime",
                              "model": "other-model", "effort": "high", **scope}),
    ]


def _assert_refused(calls: list, sid: str, scope: dict) -> None:
    before = _registry_files(sid)
    calls.clear()
    for method, params in _hub_calls(sid, scope):
        assert _rpc(method, params) == _OUT_OF_SCOPE, (method, params)
    assert calls == [], (sid, scope, calls)
    assert _registry_files(sid) == before, (sid, scope)


def test_scoped_hub_rpcs_refuse_foreign_project_without_reading_or_writing(
    tmp_path, monkeypatch,
):
    dirs = _scoped_records(tmp_path, monkeypatch)
    calls = _guard_registry(monkeypatch)
    _assert_refused(calls, "dg_foreign", {"project_dir": str(dirs["first"])})
    _assert_refused(calls, "dg_foreign", {"project_dir": str(dirs["alias"])})
    # The same chat session id does not admit a record of another project.
    _assert_refused(calls, "dg_foreign", {"project_dir": str(dirs["first"]),
                                          "session_id": "conv_a"})
    foreign = reg.get_session("dg_foreign")
    assert not foreign["paused"] and not foreign["terminate_requested"]
    assert foreign["model_override"] == "" and foreign["effort_override"] == ""


def test_scoped_hub_rpcs_refuse_foreign_session(tmp_path, monkeypatch):
    dirs = _scoped_records(tmp_path, monkeypatch)
    calls = _guard_registry(monkeypatch)
    _assert_refused(calls, "dg_sibling", {"project_dir": str(dirs["first"]),
                                          "session_id": "conv_a"})
    _assert_refused(calls, "dg_sibling", {"session_id": "conv_a"})
    assert not reg.is_paused("dg_sibling")
    assert not reg.terminate_requested("dg_sibling")


def test_scoped_hub_rpcs_fail_closed_on_missing_ownership_and_blank_scope(
    tmp_path, monkeypatch,
):
    dirs = _scoped_records(tmp_path, monkeypatch)
    calls = _guard_registry(monkeypatch)
    _assert_refused(calls, "dg_unowned", {"project_dir": str(dirs["first"])})
    _assert_refused(calls, "dg_unowned", {"session_id": "conv_a"})
    # A blank requested value never widens to "any", even for an unowned record.
    _assert_refused(calls, "dg_unowned", {"session_id": ""})
    _assert_refused(calls, "dg_unowned", {"project_dir": ""})
    _assert_refused(calls, "dg_home", {"project_dir": str(dirs["first"]),
                                       "session_id": "  "})
    _assert_refused(calls, "dg_home", {"project_dir": ""})


def test_project_scope_admits_other_session_and_canonical_aliases(
    tmp_path, monkeypatch,
):
    dirs = _scoped_records(tmp_path, monkeypatch)
    scope = {"project_dir": "~/alias/"}

    display = _rpc("subagent_transcript", {"subagent_id": "dg_sibling", **scope})
    assert display["ok"] and display["session"]["id"] == "dg_sibling"
    assert any(m.get("text") == "dg_sibling display" for m in display["messages"])
    raw = _rpc("subagent_transcript",
               {"subagent_id": "dg_sibling", "raw": True, **scope})
    assert raw["ok"]
    assert any(m.get("message", {}).get("content") == "dg_sibling raw"
               for m in raw["messages"])

    assert _rpc("subagent_steer", {"subagent_id": "dg_sibling",
                                   "text": "tighten it", **scope})["queued"]
    assert [s["text"] for s in reg.pending_steers("dg_sibling")] == [
        "dg_sibling pending", "tighten it",
    ]
    assert _rpc("subagent_control", {"subagent_id": "dg_sibling",
                                     "op": "pause", **scope})["paused"] is True
    runtime = _rpc("subagent_control", {"subagent_id": "dg_sibling",
                                        "op": "set_runtime", "effort": "high",
                                        **scope})
    assert runtime["ok"] and runtime["effort_override"] == "high"
    assert _rpc("subagent_terminate", {"subagent_id": "dg_sibling",
                                       "confirm": True, **scope})["ok"]
    assert reg.terminate_requested("dg_sibling")

    # Session-only view: the owning chat in its canonical project reads it.
    home = _rpc("subagent_transcript", {"subagent_id": "dg_home",
                                        "project_dir": str(dirs["alias"]),
                                        "session_id": "conv_a"})
    assert home["ok"] and home["session"]["id"] == "dg_home"

    # Callers without scope (older external consumers) keep unscoped access;
    # JSON null is the same as an absent key.
    unscoped = _rpc("subagent_transcript", {"subagent_id": "dg_foreign",
                                            "project_dir": None,
                                            "session_id": None})
    assert unscoped["ok"] and unscoped["session"]["id"] == "dg_foreign"


def test_scoped_list_intersects_filters_before_the_limit(tmp_path, monkeypatch):
    dirs = _scoped_records(tmp_path, monkeypatch)

    def ids(params):
        listed = _rpc("subagent_list", params)
        assert listed["ok"], listed
        return [row["id"] for row in listed["sessions"]]

    assert ids({}) == ["dg_unowned", "dg_foreign", "dg_sibling", "dg_home"]
    assert ids({"project_dir": "~/alias"}) == ["dg_sibling", "dg_home"]
    assert ids({"session_id": "conv_a"}) == ["dg_foreign", "dg_home"]
    # Newer records outside either filter must not consume the limit.
    assert ids({"project_dir": str(dirs["alias"]), "session_id": "conv_a",
                "limit": 1}) == ["dg_home"]
    assert ids({"project_dir": str(dirs["second"]),
                "session_id": "conv_b"}) == []
    assert ids({"session_id": ""}) == []
    assert ids({"project_dir": ""}) == []


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
        refused = _rpc("subagent_control", {"subagent_id": "dg_no_approval", "op": op})
        assert not refused["ok"]
        assert "pause, resume, set_runtime" in refused["reason"]


_RACE_SCRIPT = r"""
import sys, time
from pathlib import Path
from services.agent import subagent_registry as reg

role, sid, go, rounds = sys.argv[1], sys.argv[2], Path(sys.argv[3]), int(sys.argv[4])
while not go.exists():
    time.sleep(0.001)

def bump(meta):
    meta["race_counter"] = int(meta.get("race_counter", 0)) + 1

for i in range(rounds):
    reg._update_meta(sid, bump)
    if role == "runner":
        reg.heartbeat(sid)
        reg.set_status(sid, "running", note="turn %d" % i)
        reg.append_transcript(sid, "subagent", "step %d" % i)
    else:
        reg.set_paused(sid, True)
        reg.request_terminate(sid)
        reg.set_runtime_override(sid, model="model-%d" % i)
"""


def test_concurrent_writers_in_two_processes_never_lose_an_update(tmp_path):
    """The runner (agent worker) and the GUI's control RPCs (general worker) are
    different processes rewriting the same record; neither may drop the other's write."""
    import os
    import subprocess
    import sys
    from pathlib import Path

    sid, rounds = "dg_race", 150
    reg.register_session(sid, role="worker", status="running")
    go = tmp_path / "go"
    repo = Path(__file__).resolve().parents[2]
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", _RACE_SCRIPT, role, sid, str(go), str(rounds)],
            cwd=repo, env=dict(os.environ),
        )
        for role in ("runner", "controller")
    ]
    go.write_text("go", encoding="utf-8")
    for proc in procs:
        assert proc.wait(timeout=120) == 0

    meta = reg.get_session(sid)
    assert meta["race_counter"] == 2 * rounds
    assert meta["paused"] is True
    assert meta["terminate_requested"] is True
    assert meta["model_override"] == f"model-{rounds - 1}"
    assert meta["status_note"] == f"turn {rounds - 1}"
    assert len(reg.get_transcript(sid, limit=10_000)) == rounds


def test_cell_subagent_surfaces_are_actor_private(tmp_path, monkeypatch):
    import hashlib
    from services.agent import request_context as ctx
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("CHUDWARE_PROJECT_DIR", "/wrong-ambient-root")

    def principal(actor):
        return {"cell": True, "workspace_key": "team:shared", "actor_key": actor,
                "actor_hex": hashlib.sha256(actor.encode()).hexdigest()[:32],
                "right": "editor", "epoch": 1}

    def call(method, sid="default", **extra):
        if sid == "default":
            sid = current_sid
        return subagent_rpc.WORKER_METHODS[method](
            {"subagent_id": sid, **extra}, lambda *_: None)

    with ctx.bind_context(principal("alice")), ctx.bind_roots(str(tmp_path / "work")):
        reg.bind_session("alice-chat", "/untrusted/transcript/path")
        a = reg.register_session("shared", role="alice", status="running")
        alice_sid = current_sid = a["id"]
        alice_only = reg.register_session("alice-only", role="secret")["id"]
        assert alice_sid != "shared"
        assert reg.get_session("shared") is None
        reg.append_transcript(alice_sid, "subagent", "alice transcript")
        reg.append_raw(alice_sid, {"kind": "message", "text": "alice raw"})
        assert a["project_dir"] == str(tmp_path / "work")
        assert a["session_id"] == "alice-chat"
        assert alice_sid not in reg._meta_path(alice_sid).name
        assert reg.raw_path(alice_sid).is_relative_to(tmp_path / "state")
        alice_path = reg._meta_path(alice_sid)

    with ctx.bind_context(principal("bob")), ctx.bind_roots(str(tmp_path / "work")):
        assert call("subagent_list")["sessions"] == []
        for method, args in (
            ("subagent_transcript", {}),
            ("subagent_transcript", {"raw": True}),
            ("subagent_steer", {"text": "foreign steer"}),
            ("subagent_terminate", {"confirm": True}),
            ("subagent_control", {"op": "pause"}),
        ):
            foreign = call(method, alice_only, project_dir=str(tmp_path / "work"),
                           session_id="alice-chat", **args)
            absent = call(method, "absent", **args)
            assert foreign == absent == {"ok": False, "error": "not_found",
                                        "reason": "not_found", "code": "not_found"}
            for handle in ("../escape", "/absolute", r"a\b", "", None, False, 7,
                           [], {}, "bad\x00id", " shared "):
                assert call(method, handle, **args) == absent
        for method in ("subagent_steer", "subagent_terminate", "subagent_control"):
            assert call(method, alice_only) == call(method, "absent") == {
                "ok": False, "error": "not_found", "reason": "not_found",
                "code": "not_found",
            }
        for op in (lambda: reg.append_transcript(alice_only, "user", "attack"),
                   lambda: reg.queue_steer(alice_only, "attack"),
                   lambda: reg.request_terminate(alice_only),
                   lambda: reg.read_raw(alice_only)):
            with pytest.raises(KeyError, match="not_found"):
                op()
        reg.bind_session("bob-chat", "")
        current_sid = reg.register_session("shared", role="bob", status="running")["id"]
        assert current_sid != alice_sid
        assert current_sid.startswith("dg_") and len(current_sid) == 35
        reg.append_transcript(current_sid, "subagent", "bob transcript")
        assert reg._meta_path(current_sid) != alice_path
        assert [s["role"] for s in call("subagent_list")["sessions"]] == ["bob"]
        assert call("subagent_transcript")["messages"][0]["text"] == "bob transcript"
        assert call("subagent_steer", text="bob steer")["ok"]
        assert call("subagent_terminate", confirm=True)["ok"]
        assert call("subagent_list", project_dir="/other")["sessions"] == []
        for sid in ("../escape", "/absolute", r"a\b"):
            assert call("subagent_transcript", sid)["error"] == "not_found"

    with ctx.bind_context(principal("alice")), ctx.bind_roots(str(tmp_path / "work")):
        assert reg.get_transcript(alice_sid)[0]["text"] == "alice transcript"
        assert reg.pending_steers(alice_sid) == []
        assert not reg.terminate_requested(alice_sid)
        assert reg.read_raw(alice_sid)[-1]["text"] == "alice raw"


@pytest.mark.parametrize("requested", ["same-child", "../escape", "", None, 7, {}, []])
def test_cell_registry_mints_ids_and_never_refreshes_a_requested_handle(
    requested, tmp_path, monkeypatch,
):
    import hashlib
    from services.agent import request_context as ctx
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "state"))
    principal = {"cell": True, "workspace_key": "team:shared", "actor_key": "alice",
                 "actor_hex": hashlib.sha256(b"alice").hexdigest()[:32],
                 "right": "editor", "epoch": 1}
    with ctx.bind_context(principal):
        first = reg.register_session(requested, role="first")
        second = reg.register_session(requested, role="second")
        third = reg.register_session(first["id"], role="third")
        identifiers = {first["id"], second["id"], third["id"]}
        assert len(identifiers) == 3
        assert all(sid.startswith("dg_") and len(sid) == 35 for sid in identifiers)
        assert reg.get_session(first["id"])["role"] == "first"
        assert {meta["id"] for meta in reg.list_sessions()} == identifiers
        assert all(sid not in reg._meta_path(sid).name for sid in identifiers)


def test_cell_delegation_uses_registry_minted_id_for_nodes_controls_and_spawn(
    tmp_path, monkeypatch,
):
    import hashlib
    from services.agent import delegation, request_context as ctx
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(delegation, "_fresh_id", lambda: "caller-chosen")
    principal = {"cell": True, "workspace_key": "team:shared", "actor_key": "alice",
                 "actor_hex": hashlib.sha256(b"alice").hexdigest()[:32],
                 "right": "editor", "epoch": 1}
    events = []
    with ctx.bind_context(principal):
        tree = delegation.DelegationTree(
            run_id="owned-run", orchestrator_budget=100_000, max_subagent_depth=4,
            on_event=lambda kind, payload: events.append((kind, payload)),
        )
        child = tree.request_delegation("sketcher", "Sketch the bracket", 5_000)
        assert child.id.startswith("dg_") and child.id != "caller-chosen"
        assert tree.get_node(child.id) is child
        assert reg.get_session(child.id)["status"] == child.status == "approved"
        spawn = [payload for kind, payload in events if kind == "agent.spawn"]
        assert len(spawn) == 1 and spawn[0]["subagent_id"] == child.id
        assert reg.request_terminate(child.id)
        assert reg.terminate_requested(child.id)
        assert reg.get_session("caller-chosen") is None
