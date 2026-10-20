"""Focused hostd tests for long-lived worker streaming and authenticated control."""

from __future__ import annotations

import json
import os
import shutil
import signal
import sys
import tempfile
import textwrap
import threading
import time
import uuid
from pathlib import Path

import pytest

from services.mcp import daemon_bridge
from services.mcp.daemon_bridge import DaemonClient

HOSTD = daemon_bridge.resolve_hostd_bin()
TOKEN = "a" * 64

pytestmark = pytest.mark.skipif(HOSTD is None, reason="chudware-hostd not built")


_FAKE_WORKER = r"""
import json
import os
import socket
import sys
import threading
import time
from pathlib import Path

TOKEN = "a" * 64
mode = os.environ.get("CHUDWARE_TEST_WORKER_MODE", "normal")
control_dir = Path(os.environ["CHUDWARE_TEST_CONTROL_DIR"])
control_dir.mkdir(parents=True, exist_ok=True)
socket_path = control_dir / ("control-%d.sock" % os.getpid())
counter_path = Path(os.environ["CHUDWARE_TEST_WORKER_COUNTER"])
info_counter_path = Path(os.environ["CHUDWARE_TEST_INFO_COUNTER"])
with counter_path.open("a", encoding="utf-8") as counter:
    counter.write("%d\n" % os.getpid())

listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
listener.bind(str(socket_path))
os.chmod(socket_path, 0o600)
listener.listen(4)
listener.settimeout(0.1)
condition = threading.Condition()
active = {}
stopping = threading.Event()


def send(conn, payload):
    encoded = (json.dumps(payload, separators=(",", ":")) + "\n").encode()
    if hasattr(conn, "sendall"):
        conn.sendall(encoded)
    else:
        conn.write(encoded)
        conn.flush()

def control_client(conn):
    try:
        conn.settimeout(2.0)
        data = b""
        while b"\n" not in data and len(data) <= 65536:
            chunk = conn.recv(4096)
            if not chunk:
                return
            data += chunk
        request = json.loads(data.split(b"\n", 1)[0])
        if request.get("token") != TOKEN:
            send(conn, {"ok": False, "error": {"code": "unauthorized", "message": "unauthorized"}})
            return
        action = request.get("action")
        params = request.get("params") or {}
        failure = params.get("_test_control_failure")
        if failure == "held":
            marker = control_dir / ("held-" + params["run_id"])
            marker.write_text("accepted", encoding="utf-8")
            release = control_dir / ("release-" + params["run_id"])
            while not release.exists() and not stopping.wait(0.01):
                pass
            failure = "closed"
        if failure == "closed":
            return
        if failure == "malformed":
            conn.sendall(b"not-json\n")
            return
        if failure == "refused":
            send(conn, {"ok": False, "error": {
                "code": "steer_failed", "message": "steer refused"}})
            return
        if failure == "oversized":
            conn.sendall(b"x" * (256 * 1024 + 1) + b"\n")
            return
        with condition:
            matched = bool(active) and params.get("run_id") == active.get("run_id")
            if action == "cancel":
                if matched:
                    active["cancelled"] = True
                    condition.notify_all()
                result = {"cancelled": matched}
            elif action == "steer":
                if matched:
                    active["steer"] = params.get("text", "")
                    condition.notify_all()
                result = {"steered": matched}
            elif action == "status":
                result = {"active": matched}
            elif action == "answer":
                if matched:
                    active["answer"] = params.get("answers")
                    condition.notify_all()
                result = {"ok": matched, "accepted": matched}
            elif action == "release":
                if os.environ.get("CHUDWARE_TEST_RELEASE") == "refuse":
                    result = {"ok": False, "reason": "session_busy"}
                else:
                    with (control_dir / ("released-%d" % os.getpid())).open("a") as log:
                        log.write("%s %f\n" % (params.get("session_id"), time.monotonic()))
                    result = {"ok": True, "released": True}
            else:
                send(conn, {"ok": False, "error": {"code": "invalid_action", "message": "unsupported"}})
                return
        send(conn, {"ok": True, "result": result})
    finally:
        conn.close()


def accept_controls():
    while not stopping.is_set():
        try:
            conn, _ = listener.accept()
        except TimeoutError:
            continue
        except OSError:
            return
        threading.Thread(target=control_client, args=(conn,), daemon=True).start()


threading.Thread(target=accept_controls, daemon=True).start()
for raw in sys.stdin:
    request = json.loads(raw)
    rid = request.get("id")
    method = request.get("method")
    params = request.get("params") or {}
    if method == "shutdown":
        send(sys.stdout.buffer, {"id": rid, "ok": True, "result": {"bye": True}})
        break
    if method == "worker_control_info":
        with info_counter_path.open("a", encoding="utf-8") as info_counter:
            info_counter.write("%d\n" % os.getpid())
        send(sys.stdout.buffer, {"id": rid, "ok": True, "result": {
            "transport": "unix", "endpoint": str(socket_path), "token": TOKEN}})
        continue
    if method == "agent_admin_turn":
        run_id = params.get("run_id", "")
        with condition:
            active.clear()
            active["run_id"] = run_id
        active_path = Path(os.environ["CHUDWARE_TEST_ACTIVE_PATH"])
        active_path.write_text(run_id, encoding="utf-8")
        started = time.monotonic()
        turn_info = {
            "pid": os.getpid(),
            "lane": os.environ.get("CHUDWARE_HOSTD_WORKER_LANE"),
            "started": started,
            "run_id": run_id,
        }
        run_path = control_dir / ("run-" + run_id + ".json")
        temp_path = run_path.with_suffix(".tmp")
        temp_path.write_text(json.dumps(turn_info), encoding="utf-8")
        temp_path.replace(run_path)
        send(sys.stdout.buffer, {"id": rid + 999, "event": "wrong", "data": {
            "kind": "wrong_request", "run_id": run_id}})
        send(sys.stdout.buffer, {"id": rid, "event": "run_started", "data": {
            "kind": "run_started", "source": "fake-worker", **turn_info}})
        send(sys.stdout.buffer, {"id": rid, "event": "agent.tool_call", "data": {
            "kind": "agent.tool_call", "run_id": run_id, "tool_name": "fake_tool"}})
        if mode == "die":
            os._exit(23)
        if mode == "hang":
            time.sleep(600)  # a wedged turn: ignores cancel entirely
        with condition:
            condition.wait_for(
                lambda: active.get("cancelled") or "steer" in active or "answer" in active,
                timeout=10.0,
            )
            if active.get("cancelled"):
                landed = "cancel"
            elif "steer" in active:
                landed = "steer:" + active["steer"]
            elif "answer" in active:
                landed = "answer:" + json.dumps(active["answer"], sort_keys=True)
            else:
                landed = "timeout"
            active.clear()
        active_path.unlink(missing_ok=True)
        send(sys.stdout.buffer, {"id": rid, "ok": True, "result": {
            "done": True,
            "control": landed,
            **turn_info,
            "finished": time.monotonic(),
            "events": [{"kind": "must_not_replay"}],
        }})
        continue
    if method in {
        "subscription_status", "bench_status", "triggers_list", "packs_save",
        "skills_list", "ai_roles_get", "rule_create",
        "subagent_list", "subagent_transcript", "subagent_steer", "subagent_control",
        "subagent_terminate", "dag_status", "memory_recall", "session_btw",
        "chudcode_session_stats", "ai_prompt_warm", "ai_tool_catalog",
        "plm_status", "mfg_printer_adapter_snapshot", "part_search",
        "reveng_start_from_product", "ecad_thermal_handoff", "office_extract",
        "unregistered_probe",
    }:
        send(sys.stdout.buffer, {"id": rid, "ok": True, "result": {
            "pid": os.getpid(), "method": method,
            "lane": os.environ.get("CHUDWARE_HOSTD_WORKER_LANE")}})
        continue
    if method == "worker_probe":
        send(sys.stdout.buffer, {"id": rid, "ok": True, "result": {
            "worker": "reached", "value": params.get("value")}})
        continue
    send(sys.stdout.buffer, {"id": rid, "ok": False,
                             "error": "unknown method: " + str(method)})

stopping.set()
listener.close()
socket_path.unlink(missing_ok=True)
"""


@pytest.fixture()
def worker_daemon(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    key = "pytest-worker-" + uuid.uuid4().hex[:8]
    worker = tmp_path / "fake-worker"
    worker.write_text(
        "#!" + sys.executable + "\n" + textwrap.dedent(_FAKE_WORKER),
        encoding="utf-8",
    )
    data_home = Path(
        os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share")
    )
    log_path = data_home / "Chudware" / "logs" / "hostd.log"
    worker.chmod(0o700)
    counter = tmp_path / "worker-pids"
    info_counter = tmp_path / "worker-control-info"
    active = tmp_path / "active-run"
    control_dir = Path(tempfile.mkdtemp(prefix="cw-control-"))
    monkeypatch.setenv("CHUDWARE_INSTANCE_KEY", key)
    monkeypatch.setenv("CHUDWARE_WORKER_BIN", str(worker))
    monkeypatch.setenv("CHUDWARE_TEST_CONTROL_DIR", str(control_dir))
    monkeypatch.setenv("CHUDWARE_TEST_WORKER_COUNTER", str(counter))
    monkeypatch.setenv("CHUDWARE_TEST_INFO_COUNTER", str(info_counter))
    monkeypatch.setenv("CHUDWARE_TEST_ACTIVE_PATH", str(active))
    monkeypatch.setenv("CHUDWARE_HOSTD_LINGER_SECS", "10")
    monkeypatch.setenv("CHUDWARE_AGENT_WORKERS", "4")
    monkeypatch.delenv("CHUDWARE_HOSTD_SOCKET", raising=False)
    pids: list[int] = []
    yield {
        "pids": pids,
        "counter": counter,
        "info_counter": info_counter,
        "active": active,
        "log_path": log_path,
        "turn_dir": control_dir,
    }
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    shutil.rmtree(control_dir, ignore_errors=True)


def _pid(client: DaemonClient) -> int:
    return client.call("daemon_status")["pid"]


def _wait_for(path: Path, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.01)
    raise AssertionError(f"timed out waiting for {path.name}")


def _worker_count(path: Path) -> int:
    if not path.exists():
        return 0
    return len(path.read_text(encoding="utf-8").splitlines())


def test_different_conversations_run_concurrently_and_cancel_independently(worker_daemon):
    first, second, controller = DaemonClient(), DaemonClient(), DaemonClient()
    first.connect()
    worker_daemon["pids"].append(_pid(first))
    second.connect()
    controller.connect()
    outcomes: dict = {}

    def turn(client, sid, run_id):
        outcomes[run_id] = client.call(
            "agent_admin_turn", {"session_id": sid, "run_id": run_id, "request": "work"}
        )

    threads = [
        threading.Thread(target=turn, args=(first, "chat-a", "parallel-a"), daemon=True),
        threading.Thread(target=turn, args=(second, "chat-b", "parallel-b"), daemon=True),
    ]
    threads[0].start()
    first_record = worker_daemon["turn_dir"] / "run-parallel-a.json"
    _wait_for(first_record)
    threads[1].start()
    second_record = worker_daemon["turn_dir"] / "run-parallel-b.json"
    _wait_for(second_record)
    assert all(thread.is_alive() for thread in threads)
    a, b = [json.loads(path.read_text()) for path in (first_record, second_record)]
    assert a["pid"] != b["pid"]
    assert (a["lane"], b["lane"]) == ("agent", "agent-turn")
    assert _worker_count(worker_daemon["counter"]) == 2

    assert controller.call("agent_cancel", {"run_id": "parallel-b"})["cancelled"]
    threads[1].join(timeout=5.0)
    assert not threads[1].is_alive()
    assert outcomes["parallel-b"]["control"] == "cancel"
    assert threads[0].is_alive()
    assert "parallel-a" not in outcomes
    # Session-scoped calls follow the extra even while the primary remains occupied.
    stats = controller.call("chudcode_session_stats", {"session_id": "chat-b"})
    assert stats["pid"] == b["pid"]
    assert stats["lane"] == "agent-turn"
    assert controller.call("subscription_status")["code"] == "worker_lane_busy"
    assert controller.call("agent_cancel", {"run_id": "parallel-a"})["cancelled"]
    threads[0].join(timeout=5.0)
    assert not threads[0].is_alive()
    assert outcomes["parallel-a"]["control"] == "cancel"
    assert max(a["started"], b["started"]) < min(
        outcomes["parallel-a"]["finished"], outcomes["parallel-b"]["finished"]
    )
    assert controller.call("rule_create", {"session_id": "chat-b"})["pid"] == a["pid"]
    info = controller.call("worker_control_info", {"session_id": "chat-b"})
    assert info["endpoint"] == str(worker_daemon["turn_dir"] / f"control-{a['pid']}.sock")
    assert controller.call("daemon_drain")["ok"]
    for client in (first, second, controller):
        client.close()
    deadline = time.monotonic() + 8.0
    while any(Path(f"/proc/{pid}").exists() for pid in (a["pid"], b["pid"])):
        assert time.monotonic() < deadline, "agent pool child outlived hostd shutdown"
        time.sleep(0.02)


def test_same_conversation_turns_wait_for_the_owning_worker(worker_daemon):
    first, second, controller = DaemonClient(), DaemonClient(), DaemonClient()
    first.connect()
    worker_daemon["pids"].append(_pid(first))
    second.connect()
    controller.connect()
    outcomes: dict = {}

    def turn(client, run_id):
        outcomes[run_id] = client.call(
            "agent_admin_turn",
            {"session_id": "same-chat", "run_id": run_id, "request": "work"},
        )

    threads = [
        threading.Thread(target=turn, args=(first, "serial-a"), daemon=True),
        threading.Thread(target=turn, args=(second, "serial-b"), daemon=True),
    ]
    first_record = worker_daemon["turn_dir"] / "run-serial-a.json"
    second_record = worker_daemon["turn_dir"] / "run-serial-b.json"
    threads[0].start()
    _wait_for(first_record)
    threads[1].start()
    deadline = time.monotonic() + 5.0
    while controller.call("daemon_status").get("inflight", 0) < 3:
        assert time.monotonic() < deadline
        time.sleep(0.01)
    time.sleep(0.2)
    assert not second_record.exists()
    assert all(thread.is_alive() for thread in threads)
    assert controller.call("agent_cancel", {"run_id": "serial-a"})["cancelled"]
    threads[0].join(timeout=5.0)
    assert not threads[0].is_alive()
    _wait_for(second_record)
    assert threads[1].is_alive()
    assert controller.call("agent_cancel", {"run_id": "serial-b"})["cancelled"]
    threads[1].join(timeout=5.0)
    assert not threads[1].is_alive()
    a, b = outcomes["serial-a"], outcomes["serial-b"]
    assert a["control"] == b["control"] == "cancel"
    assert a["pid"] == b["pid"]
    assert a["lane"] == b["lane"] == "agent"
    assert b["started"] >= a["finished"]
    assert _worker_count(worker_daemon["counter"]) == 1
    for client in (first, second, controller):
        client.close()


def _run_turn(client, sid, run_id, outcomes):
    outcomes[run_id] = client.call(
        "agent_admin_turn", {"session_id": sid, "run_id": run_id, "request": "work"}
    )


@pytest.mark.parametrize("release", ["accept", "refuse"])
def test_busy_owner_releases_a_conversation_before_another_worker_resumes_it(
    worker_daemon, monkeypatch, release
):
    """chat-a's live runtime sits in the primary, now busy with chat-b. chat-a's next turn
    moves to another worker only after the primary disposed chat-a's runtime; a worker
    that cannot release it keeps the conversation (the turn waits for it)."""
    if release == "refuse":
        monkeypatch.setenv("CHUDWARE_TEST_RELEASE", "refuse")
    clients = [DaemonClient() for _ in range(4)]
    for client in clients:
        client.connect()
    worker_daemon["pids"].append(_pid(clients[0]))
    controller = clients[3]
    turn_dir = worker_daemon["turn_dir"]
    outcomes: dict = {}

    first = threading.Thread(target=_run_turn, args=(clients[0], "chat-a", "a1", outcomes))
    first.start()
    _wait_for(turn_dir / "run-a1.json")
    assert controller.call("agent_cancel", {"run_id": "a1"})["cancelled"]
    first.join(timeout=5.0)
    primary_pid = outcomes["a1"]["pid"]

    busy = threading.Thread(target=_run_turn, args=(clients[1], "chat-b", "b1", outcomes))
    busy.start()
    _wait_for(turn_dir / "run-b1.json")
    assert json.loads((turn_dir / "run-b1.json").read_text())["pid"] == primary_pid
    moved = threading.Thread(target=_run_turn, args=(clients[2], "chat-a", "a2", outcomes))
    moved.start()
    if release == "accept":
        _wait_for(turn_dir / "run-a2.json")
        a2 = json.loads((turn_dir / "run-a2.json").read_text())
        assert a2["pid"] != primary_pid and a2["lane"] == "agent-turn"
        released = (turn_dir / f"released-{primary_pid}").read_text().split()
        assert released[0] == "chat-a"
        assert float(released[1]) < a2["started"]
        assert controller.call("agent_cancel", {"run_id": "a2"})["cancelled"]
        assert controller.call("agent_cancel", {"run_id": "b1"})["cancelled"]
    else:
        time.sleep(0.5)
        assert not (turn_dir / "run-a2.json").exists()
        assert controller.call("agent_cancel", {"run_id": "b1"})["cancelled"]
        _wait_for(turn_dir / "run-a2.json")
        assert json.loads((turn_dir / "run-a2.json").read_text())["pid"] == primary_pid
        assert controller.call("agent_cancel", {"run_id": "a2"})["cancelled"]
    for thread in (busy, moved):
        thread.join(timeout=5.0)
        assert not thread.is_alive()
    if release == "refuse":
        assert outcomes["a2"]["started"] >= outcomes["b1"]["finished"]
        assert not (turn_dir / f"released-{primary_pid}").exists()
    for client in clients:
        client.close()


@pytest.mark.parametrize("failure", ["refused", "malformed", "closed", "oversized", "held"])
@pytest.mark.parametrize("method", ["agent_steer", "agent_answer"])
def test_failed_private_request_preserves_endpoint_for_question_answer(
    worker_daemon, failure, method
):
    """A failed steer/answer must not poison the endpoint known during a long turn."""
    requester, controller = DaemonClient(), DaemonClient()
    requester.connect()
    worker_daemon["pids"].append(_pid(requester))
    controller.connect()
    run_id = "endpoint-" + failure + "-" + method
    outcomes: dict = {}
    thread = threading.Thread(
        target=_run_turn, args=(requester, "question-chat", run_id, outcomes),
        daemon=True,
    )
    thread.start()
    _wait_for(worker_daemon["turn_dir"] / f"run-{run_id}.json")
    try:
        failed = controller.call(method, {
            "run_id": run_id, "text": "not accepted",
            "_test_control_failure": failure,
        })
        assert failed["code"] == "worker_control_error"
        assert TOKEN not in json.dumps(failed)
        assert thread.is_alive()
        answers = {"mount": "under an eave", "material": "PETG"}
        answer = controller.call("agent_answer", {
            "run_id": run_id, "batch_id": "pending-question", "answers": answers,
        })
        assert answer["accepted"] is True
        thread.join(timeout=5.0)
        assert not thread.is_alive()
        assert outcomes[run_id]["control"] == "answer:" + json.dumps(answers, sort_keys=True)
        assert _worker_count(worker_daemon["info_counter"]) == 1
        assert _worker_count(worker_daemon["counter"]) == 1
        assert TOKEN not in json.dumps({"answer": answer, "outcomes": outcomes})
    finally:
        (worker_daemon["turn_dir"] / f"release-{run_id}").touch()
        if thread.is_alive():
            controller.call("agent_cancel", {"run_id": run_id})
            thread.join(timeout=5.0)
        requester.close()
        controller.close()


def test_slow_private_connection_does_not_block_controls_or_turn_terminals(worker_daemon):
    """One stalled steer cannot hold the state lock while another control lands."""
    clients = [DaemonClient() for _ in range(5)]
    for client in clients:
        client.connect()
    worker_daemon["pids"].append(_pid(clients[0]))
    outcomes: dict = {}
    turns = [
        threading.Thread(target=_run_turn, args=(clients[0], "chat-a", "held-a", outcomes),
                         daemon=True),
        threading.Thread(target=_run_turn, args=(clients[1], "chat-b", "held-b", outcomes),
                         daemon=True),
    ]
    slow_result: dict = {}
    slow = threading.Thread(
        target=lambda: slow_result.update(clients[2].call("agent_steer", {
            "run_id": "held-a", "text": "stalled",
            "_test_control_failure": "held",
        })),
        daemon=True,
    )
    try:
        for run_id, turn in zip(("held-a", "held-b"), turns):
            turn.start()
            _wait_for(worker_daemon["turn_dir"] / f"run-{run_id}.json")
        slow.start()
        _wait_for(worker_daemon["turn_dir"] / "held-held-a")
        started = time.monotonic()
        assert clients[3].call("agent_cancel", {"run_id": "held-b"})["cancelled"]
        assert clients[4].call("agent_answer", {
            "run_id": "held-a", "batch_id": "pending", "answers": {"q": "PETG"},
        })["accepted"]
        for turn in turns:
            turn.join(timeout=1.0)
            assert not turn.is_alive()
        assert time.monotonic() - started < 2.0
        assert slow.is_alive()
        assert outcomes["held-b"]["control"] == "cancel"
        assert outcomes["held-a"]["control"] == 'answer:{"q": "PETG"}'
        assert _worker_count(worker_daemon["info_counter"]) == 2
    finally:
        (worker_daemon["turn_dir"] / "release-held-a").touch()
        if slow.ident is not None:
            slow.join(timeout=5.0)
        for run_id, turn in zip(("held-a", "held-b"), turns):
            if turn.is_alive():
                clients[3].call("agent_cancel", {"run_id": run_id})
                turn.join(timeout=5.0)
        for client in clients:
            client.close()
    assert slow_result["code"] == "worker_control_error"
    assert TOKEN not in json.dumps(slow_result)



@pytest.mark.parametrize(
    ("method", "params", "result_key", "terminal"),
    [
        ("agent_cancel", {}, "cancelled", "cancel"),
        (
            "agent_steer",
            {"text": "use the rear face"},
            "steered",
            "steer:use the rear face",
        ),
    ],
)
def test_worker_progress_is_request_scoped_and_live_control_lands(
    worker_daemon, method, params, result_key, terminal
):
    requester, observer, controller = DaemonClient(), DaemonClient(), DaemonClient()
    requester.connect()
    worker_daemon["pids"].append(_pid(requester))
    observer.connect()
    controller.connect()
    run_id = "worker-run-1"
    outcome: dict = {}

    thread = threading.Thread(
        target=lambda: outcome.update(
            result=requester.call(
                "agent_admin_turn", {"run_id": run_id, "request": "work"}
            )
        ),
        daemon=True,
    )
    thread.start()
    _wait_for(worker_daemon["active"])
    active_status = controller.call("daemon_status")
    assert active_status.get("inflight", 0) >= 1

    # An exact active-run match is required. The mismatch stays on the existing
    # core path and cannot disturb the worker turn.
    wrong = controller.call(method, {"run_id": "another-run", **params})
    assert wrong.get(result_key) is False
    assert thread.is_alive()

    controlled = controller.call(method, {"run_id": run_id, **params})
    assert controlled.get(result_key) is True
    thread.join(timeout=5.0)
    assert not thread.is_alive()
    assert outcome["result"]["control"] == terminal
    assert "events" not in outcome["result"]

    requester_events = requester.poll_events()
    worker_events = [
        event.get("data", {})
        for event in requester_events
        if event.get("event") == "agent.event"
    ]
    assert [event.get("kind") for event in worker_events] == [
        "run_started",
        "agent.tool_call",
    ]
    assert all(event.get("run_id") == run_id for event in worker_events)
    observer.call("ping")
    assert not any(
        event.get("event") == "agent.event" for event in observer.poll_events()
    )

    status = controller.call("daemon_status")
    visible = json.dumps(
        {
            "status": status,
            "control": controlled,
            "requester_events": requester_events,
        }
    )
    assert TOKEN not in visible
    log_path = worker_daemon["log_path"]
    if log_path.exists():
        assert TOKEN not in log_path.read_text(encoding="utf-8", errors="replace")


def test_blocked_agent_lane_does_not_block_general_worker(worker_daemon):
    requester, general, controller = DaemonClient(), DaemonClient(), DaemonClient()
    requester.connect()
    worker_daemon["pids"].append(_pid(requester))
    general.connect()
    controller.connect()
    outcome: dict = {}
    thread = threading.Thread(
        target=lambda: outcome.update(
            result=requester.call(
                "agent_admin_turn", {"run_id": "lane-blocked", "request": "work"}
            )
        ),
        daemon=True,
    )
    thread.start()
    _wait_for(worker_daemon["active"])
    started = time.monotonic()
    general_response = general.call("plm_status")
    general_pid = general_response["pid"]
    assert general_response["lane"] == "general"
    assert time.monotonic() - started < 3.0
    assert thread.is_alive()
    assert worker_daemon["active"].exists()
    assert controller.call("agent_cancel", {"run_id": "lane-blocked"})["cancelled"]
    thread.join(timeout=5.0)
    assert not thread.is_alive()
    assert outcome["result"]["control"] == "cancel"
    assert _worker_count(worker_daemon["counter"]) == 2
    assert _worker_count(worker_daemon["info_counter"]) == 1

    for method in (
        "subscription_status", "bench_status", "triggers_list", "packs_save",
        "skills_list", "ai_roles_get", "rule_create",
        "dag_status", "memory_recall", "session_btw",
        "chudcode_session_stats", "ai_prompt_warm", "ai_tool_catalog",
    ):
        response = controller.call(method)
        assert response["pid"] != general_pid, method
        assert response["lane"] == "agent", method
    for method in (
        "part_search",
        "reveng_start_from_product", "ecad_thermal_handoff",
        "office_extract", "unregistered_probe",
        "subagent_list", "subagent_transcript", "subagent_steer", "subagent_control",
        "subagent_terminate",
    ):
        response = general.call(method)
        assert response.get("pid") == general_pid, (method, response)
        assert response["lane"] == "general", method
    assert _worker_count(worker_daemon["counter"]) == 2

    # Clean daemon shutdown reaps BOTH children rather than only the lane
    # last used; neither lane is permitted to outlive its owner.
    worker_pids = [
        int(line) for line in worker_daemon["counter"].read_text().splitlines()
    ]
    assert controller.call("daemon_drain")["ok"]
    requester.close()
    general.close()
    controller.close()
    deadline = time.monotonic() + 8.0
    while any(Path(f"/proc/{pid}").exists() for pid in worker_pids):
        assert time.monotonic() < deadline, "worker child outlived hostd shutdown"
        time.sleep(0.02)
    assert all(not Path(f"/proc/{pid}").exists() for pid in worker_pids)


def test_agent_lane_call_behind_a_turn_fails_fast_and_hostd_survives(worker_daemon):
    # Live 2026-09-27: the GUI's subscription_accounts (agent lane) queued behind an
    # hours-long agent_admin_turn on the lane's stdio mutex, ignoring its 120 s bound, until
    # the long-tier watchdog _Exit'ed hostd and took the live session's docs with it; the
    # GUI's single async RPC lane stalled behind it. A non-turn call behind a running turn
    # must fail at once with a named error while the turn keeps running and hostd stays up.
    requester, gui = DaemonClient(), DaemonClient()
    requester.connect()
    daemon_pid = _pid(requester)
    worker_daemon["pids"].append(daemon_pid)
    gui.connect()
    outcome: dict = {}
    thread = threading.Thread(
        target=lambda: outcome.update(
            result=requester.call(
                "agent_admin_turn", {"run_id": "lane-held", "request": "work"}
            )
        ),
        daemon=True,
    )
    thread.start()
    _wait_for(worker_daemon["active"])

    started = time.monotonic()
    busy = gui.call("subscription_status")
    waited = time.monotonic() - started
    assert busy.get("_is_error") is True, busy
    assert busy.get("code") == "worker_lane_busy", busy
    assert busy.get("message") == "worker lane busy (agent turn in progress)", busy
    assert waited < 2.0, waited  # the fake turn holds the lane for 10 s
    assert thread.is_alive(), "the lane-busy refusal must not disturb the running turn"
    assert _pid(gui) == daemon_pid

    assert gui.call("agent_cancel", {"run_id": "lane-held"})["cancelled"]
    thread.join(timeout=5.0)
    assert not thread.is_alive()
    assert outcome["result"]["control"] == "cancel"
    # The lane is free again: the same call is served by the same worker child.
    served = gui.call("subscription_status")
    assert served.get("lane") == "agent", served
    assert _worker_count(worker_daemon["counter"]) == 1


def test_subagent_roster_and_controls_are_served_while_a_turn_holds_the_agent_lane(
    worker_daemon,
):
    # Registry sub-agents only exist while an agent turn runs, and that turn holds the agent
    # lane for its whole length; the Current Run roster, transcript and Steer / Pause /
    # Terminate must still answer instead of failing with worker_lane_busy.
    requester, gui = DaemonClient(), DaemonClient()
    requester.connect()
    worker_daemon["pids"].append(_pid(requester))
    gui.connect()
    outcome: dict = {}
    thread = threading.Thread(
        target=lambda: outcome.update(
            result=requester.call(
                "agent_admin_turn", {"run_id": "roster-live", "request": "work"}
            )
        ),
        daemon=True,
    )
    thread.start()
    _wait_for(worker_daemon["active"])

    started = time.monotonic()
    for method in ("subagent_list", "subagent_transcript", "subagent_steer",
                   "subagent_control", "subagent_terminate"):
        response = gui.call(method, {"subagent_id": "dg_live"})
        assert response.get("code") != "worker_lane_busy", (method, response)
        assert response.get("method") == method, response
        assert response.get("lane") == "general", (method, response)
    assert time.monotonic() - started < 3.0  # the fake turn holds the agent lane for 10 s
    assert thread.is_alive(), "the turn must still be running and holding the agent lane"
    assert worker_daemon["active"].exists()
    # A method that still belongs to the agent lane is refused, proving the lane was held.
    assert gui.call("dag_status").get("code") == "worker_lane_busy"

    assert gui.call("agent_cancel", {"run_id": "roster-live"})["cancelled"]
    thread.join(timeout=5.0)
    assert not thread.is_alive()
    assert outcome["result"]["control"] == "cancel"


def test_unknown_method_worker_fallback_contract_is_unchanged(worker_daemon):
    client = DaemonClient()
    client.connect()
    worker_daemon["pids"].append(_pid(client))

    assert client.call("worker_probe", {"value": 17}) == {
        "worker": "reached",
        "value": 17,
    }
    missing = client.call("definitely_missing_worker_method", {})
    assert missing.get("_is_error") is True
    assert missing.get("code") == "worker_error"
    assert missing.get("message") == "unknown method: definitely_missing_worker_method"


def test_worker_death_mid_turn_is_not_replayed_and_clears_control_state(
    worker_daemon, monkeypatch: pytest.MonkeyPatch
):
    # Live 2026-09-27 (c221 -> c223): the worker died mid-turn and hostd replayed the
    # turn on a fresh worker; the replay ran as a second, unwatched run in a new
    # Chudcode session. A turn the worker received must fail by name instead.
    monkeypatch.setenv("CHUDWARE_TEST_WORKER_MODE", "die")
    client = DaemonClient()
    client.connect()
    worker_daemon["pids"].append(_pid(client))

    failed = client.call("agent_admin_turn", {"run_id": "dead-run", "request": "work"})
    assert failed.get("_is_error") is True
    assert failed.get("code") == "worker_lost_mid_turn"
    assert _worker_count(worker_daemon["counter"]) == 1

    # The dead child has been reaped and its cached endpoint/token/run state cleared.
    # An inactive cancel remains a core call and must not spawn a worker merely to
    # discover that nothing is active.
    cancelled = client.call("agent_cancel", {"run_id": "dead-run"})
    assert cancelled.get("cancelled") is False
    assert _worker_count(worker_daemon["info_counter"]) == 1
    time.sleep(0.1)
    assert _worker_count(worker_daemon["counter"]) == 1

    # The lane recovers: the next turn spawns a fresh worker (which dies again here).
    again = client.call("agent_admin_turn", {"run_id": "next-run", "request": "continue"})
    assert again.get("code") == "worker_lost_mid_turn"
    assert _worker_count(worker_daemon["counter"]) == 2


def test_inactive_control_does_not_construct_worker(worker_daemon):
    client = DaemonClient()
    client.connect()
    worker_daemon["pids"].append(_pid(client))

    cancelled = client.call("agent_cancel", {"run_id": "never-started"})
    steered = client.call(
        "agent_steer", {"run_id": "never-started", "text": "new direction"}
    )
    assert cancelled.get("cancelled") is False
    assert steered.get("steered") is False
    assert _worker_count(worker_daemon["counter"]) == 0


@pytest.mark.parametrize("mode", ["normal", "hang"])
def test_sigterm_mid_turn_ends_the_turn_and_the_daemon(
    worker_daemon, monkeypatch: pytest.MonkeyPatch, mode
):
    # Live 2026-09-24: hostd was stopped (tray Quit / SIGTERM) while an agent turn held the
    # worker lane. WorkerBridge::stop() waited on that lane forever: the accept loop was
    # gone, the worker's engine socket hung, and the turn's next CAD call blocked for good.
    # Stop must cancel the live run (and end a child that ignores the cancel) instead.
    monkeypatch.setenv("CHUDWARE_TEST_WORKER_MODE", mode)
    requester = DaemonClient()
    requester.connect()
    daemon_pid = _pid(requester)
    worker_daemon["pids"].append(daemon_pid)
    outcome: dict = {}

    def run() -> None:
        try:
            outcome["result"] = requester.call(
                "agent_admin_turn", {"run_id": "stopped-mid-turn", "request": "work"}
            )
        except Exception as exc:  # noqa: BLE001 — a dropped connection is an acceptable end
            outcome["error"] = exc

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    _wait_for(worker_daemon["active"])
    worker_pids = [
        int(line) for line in worker_daemon["counter"].read_text().splitlines()
    ]
    os.kill(daemon_pid, signal.SIGTERM)

    deadline = time.monotonic() + 20.0
    while Path(f"/proc/{daemon_pid}").exists():
        status = Path(f"/proc/{daemon_pid}/stat").read_text().split()[2] if Path(
            f"/proc/{daemon_pid}/stat").exists() else "X"
        if status == "Z":
            break
        assert time.monotonic() < deadline, "hostd wedged behind the in-flight turn"
        time.sleep(0.05)
    thread.join(timeout=5.0)
    assert not thread.is_alive(), "the requester never learned its turn ended"
    assert all(not Path(f"/proc/{pid}").exists() for pid in worker_pids)
    if mode == "normal":
        assert outcome.get("result", {}).get("control") == "cancel"
