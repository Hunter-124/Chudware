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
        send(sys.stdout.buffer, {"id": rid + 999, "event": "wrong", "data": {
            "kind": "wrong_request", "run_id": run_id}})
        send(sys.stdout.buffer, {"id": rid, "event": "run_started", "data": {
            "kind": "run_started", "run_id": run_id, "source": "fake-worker"}})
        send(sys.stdout.buffer, {"id": rid, "event": "agent.tool_call", "data": {
            "kind": "agent.tool_call", "run_id": run_id, "tool_name": "fake_tool"}})
        if mode == "die":
            os._exit(23)
        if mode == "hang":
            time.sleep(600)  # a wedged turn: ignores cancel entirely
        with condition:
            condition.wait_for(
                lambda: active.get("cancelled") or "steer" in active,
                timeout=10.0,
            )
            if active.get("cancelled"):
                landed = "cancel"
            elif "steer" in active:
                landed = "steer:" + active["steer"]
            else:
                landed = "timeout"
            active.clear()
        active_path.unlink(missing_ok=True)
        send(sys.stdout.buffer, {"id": rid, "ok": True, "result": {
            "done": True,
            "control": landed,
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
    monkeypatch.delenv("CHUDWARE_HOSTD_SOCKET", raising=False)
    pids: list[int] = []
    yield {
        "pids": pids,
        "counter": counter,
        "info_counter": info_counter,
        "active": active,
        "log_path": log_path,
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
