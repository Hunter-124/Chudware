"""Focused hostd tests for long-lived worker streaming and authenticated control."""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
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
sessions = set()
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
        if action == "release" and os.environ.get("CHUDWARE_TEST_RELEASE") == "held":
            session = params.get("session_id", "")
            (control_dir / ("release-entered-" + session)).touch()
            while not (control_dir / ("release-permit-" + session)).exists() and not stopping.wait(0.01):
                pass
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
            elif action == "apply_compaction_policy":
                (control_dir / ("policy-params-%d" % os.getpid())).write_text(json.dumps(params))
                fail = (control_dir / ("policy-fail-%d" % os.getpid())).exists()
                result = {"ok": not fail, "sessions": [
                    {"session_id": session, "ok": not fail,
                     **({"reason": "fixture failure"} if fail else {"effective": {"enabled": True}})}
                    for session in sorted(sessions)
                ]}
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
            sessions.add(params.get("session_id", ""))
        active_path = Path(os.environ["CHUDWARE_TEST_ACTIVE_PATH"])
        active_path.write_text(run_id, encoding="utf-8")
        started = time.monotonic()
        turn_info = {
            "pid": os.getpid(),
            "lane": os.environ.get("CHUDWARE_HOSTD_WORKER_LANE"),
            "started": started,
            "run_id": run_id,
            "compact": params.get("compact"),
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
        if mode == "drain_tools":
            # The real hostd owns this process and its session. Each phase
            # creates and closes a real per-turn pool, exactly like omp_host.
            sys.path.insert(0, os.environ["CHUDWARE_TEST_REPO_ROOT"])
            from services.mcp import daemon_bridge

            def record(phase, value):
                path = control_dir / ("tools-" + phase + "-" + run_id + ".json")
                tmp = path.with_suffix(".tmp")
                tmp.write_text(json.dumps(value), encoding="utf-8")
                tmp.replace(path)

            def tool(phase, doc_id=None):
                pool = daemon_bridge.TurnConnections(
                    agent_run_scope=params.get("_hostd_doc_scope", ""))
                try:
                    with daemon_bridge.turn_connection_scope(pool):
                        conn = daemon_bridge.client()
                        if phase == "before":
                            target = params.get("_test_target_doc") or params.get("doc_id")
                            if target and params.get("_test_attach_mode"):
                                result = conn.call("attach_doc", {
                                    "doc_id": target, "mode": params["_test_attach_mode"]})
                            elif target:
                                result = conn.call("doc_tree", {"doc_id": target})
                            else:
                                result = conn.call("doc_new", {
                                    "name": "drain-session-" + run_id,
                                    "internal": bool(params.get("_test_internal"))})
                                target = result.get("doc_id")
                            job_result = None
                            if params.get("_test_job_doc"):
                                from services.mcp.cad_common import _solve_sync_or_handoff
                                job_result = _solve_sync_or_handoff(
                                    "hold-test", lambda: daemon_bridge.client().call(
                                        "doc_tree", {"doc_id": params["_test_job_doc"]}),
                                    budget_s=10)
                            resume_result = None
                            if params.get("_test_resume_doc"):
                                from mcp.server.fastmcp import FastMCP
                                from services.mcp import cad_common
                                from services.mcp.cad_doc_tools import register_cad_doc_tools
                                tools = FastMCP("doc-hold-test")
                                register_cad_doc_tools(tools)
                                with cad_common.agent_doc_context("builder", scope=run_id):
                                    resume_result = json.loads(
                                        tools._tool_manager._tools["new_part"].fn(name="resume_part_v2"))
                            return {"result": result, "doc_id": target, "pid": os.getpid(),
                                    "writer_group": conn.writer_group, "job_result": job_result,
                                    "resume_result": resume_result}
                        if phase == "after":
                            tree = conn.call("doc_tree", {"doc_id": doc_id})
                            edit = conn.call("doc_apply_ops", {
                                "doc_id": doc_id,
                                "ops": [{"op": "add_feature", "feature": {
                                    "type": "box", "name": "continued-box-" + run_id,
                                    "params": {"length": 10, "width": 10, "height": 10,
                                               "at": [20 if params.get("doc_id") else 0, 0, 0]},
                                    "refs": {}}}],
                            })
                            return {"tree": tree, "edit": edit}
                        return {"result": conn.call("doc_new", {"name": "hard-drain-forbidden"})}
                except daemon_bridge.DaemonDraining as exc:
                    return {"error": exc.detail}
                except Exception as exc:
                    return {"exception": type(exc).__name__ + ": " + str(exc)}
                finally:
                    pool.close()

            before = tool("before")
            record("before", before)
            doc_id = before.get("doc_id")
            deadline = time.monotonic() + 120
            for phase in ("after", "hard"):
                marker = control_dir / ("tools-go-" + phase + "-" + run_id)
                while not marker.exists() and time.monotonic() < deadline:
                    if (control_dir / ("tools-done-" + run_id)).exists():
                        break
                    time.sleep(0.01)
                if marker.exists():
                    record(phase, tool(phase, doc_id))
            with condition:
                active.clear()
            active_path.unlink(missing_ok=True)
            send(sys.stdout.buffer, {"id": rid, "ok": True, "result": {"done": True}})
            continue
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
    if method == "omp_login":
        # An interactive sign-in: the worker's stdio stays held until the browser step ends.
        (control_dir / "login-held").write_text(str(os.getpid()), encoding="utf-8")
        deadline = time.monotonic() + 10
        while not (control_dir / "login-release").exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        send(sys.stdout.buffer, {"id": rid, "ok": True, "result": {
            "pid": os.getpid(), "method": method,
            "lane": os.environ.get("CHUDWARE_HOSTD_WORKER_LANE")}})
        continue
    if method in {
        "subscription_status", "subscription_accounts", "subscription_usage",
        "omp_provider_list", "omp_models_list", "omp_auth_state", "omp_set_key",
        "ai_roles_set", "skills_get", "bench_status", "triggers_list", "packs_save",
        "skills_list", "ai_roles_get", "rule_create",
        "subagent_list", "subagent_transcript", "subagent_steer", "subagent_control",
        "subagent_terminate", "dag_status", "memory_recall", "session_btw",
        "omp_session_stats", "ai_prompt_warm", "ai_tool_catalog",
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
    monkeypatch.setenv("CHUDWARE_TEST_REPO_ROOT", str(Path(__file__).resolve().parents[2]))
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


def _require_peer_pidfd():
    # Older kernels lack SO_PEERPIDFD, where the daemon must fail closed.
    probe, peer = socket.socketpair()
    try:
        try:
            peer_fd = probe.getsockopt(socket.SOL_SOCKET, 77)  # Linux SO_PEERPIDFD
        except OSError:
            pytest.skip("kernel does not support SO_PEERPIDFD")
        else:
            os.close(peer_fd)
    finally:
        probe.close()
        peer.close()


@pytest.mark.skipif(sys.platform != "linux", reason="continuity requires Linux peer pidfds")
def test_soft_drain_continues_only_owned_worker_generation(
    worker_daemon, monkeypatch, tmp_path
):
    _require_peer_pidfd()
    hostd_copy = tmp_path / "chudware-hostd"
    shutil.copy2(HOSTD, hostd_copy)
    monkeypatch.setenv("CHUDWARE_HOSTD_BIN", str(hostd_copy))
    if not os.environ.get("CHUDWARE_LIBCHUDWARE"):
        monkeypatch.setenv("CHUDWARE_LIBCHUDWARE", str(Path(HOSTD).resolve().parent / "libchudware.so"))
    monkeypatch.setenv("CHUDWARE_HOSTD_STALECHECK_S", "1")
    monkeypatch.setenv("CHUDWARE_HOSTD_DRAIN_GRACE_S", "60")
    monkeypatch.setenv("CHUDWARE_TEST_WORKER_MODE", "drain_tools")
    first, second, controller = DaemonClient(), DaemonClient(), DaemonClient()
    clients = (first, second, controller)
    outcomes = {}
    threads = []
    directory = worker_daemon["turn_dir"]

    def turn(client, session, run_id, doc_id=None):
        params = {"session_id": session, "run_id": run_id, "request": "work"}
        if doc_id:
            params["doc_id"] = doc_id
        outcomes[run_id] = client.call("agent_admin_turn", params)

    def result(phase, session, timeout=60):
        path = directory / f"tools-{phase}-{session}.json"
        _wait_for(path, timeout=timeout)
        return json.loads(path.read_text(encoding="utf-8"))

    try:
        for client in clients:
            client.connect()
        pid = _pid(controller)
        worker_daemon["pids"].append(pid)
        threads.append(threading.Thread(target=turn, args=(first, "owned", "owned-first"), daemon=True))
        threads[-1].start()
        before = result("before", "owned-first")
        doc = before["doc_id"]
        assert controller.call("attach_doc", {"doc_id": doc, "mode": "rw"})["ok"]

        # The watched path is private to this fixture, never the user's binary.
        time.sleep(1.2)
        old_mtime = hostd_copy.stat().st_mtime_ns
        os.utime(hostd_copy, ns=(old_mtime + 2_000_000_000, old_mtime + 2_000_000_000))
        deadline = time.monotonic() + 10
        while not controller.call("daemon_status")["draining"]:
            assert time.monotonic() < deadline
            time.sleep(0.05)
        assert controller.call("daemon_status")["drain_reason"] == "stale_binary"

        # The worker's previous pool was closed. A fresh pool must read and edit
        # the same actual document on the same running daemon.
        (directory / "tools-go-after-owned-first").touch()
        after = result("after", "owned-first")
        assert not after["tree"].get("_is_error"), after
        assert after["edit"]["revision"] == 1, after
        assert "continued-box" in json.dumps(controller.call("doc_tree", {"doc_id": doc}))
        assert _pid(controller) == pid

        # Complete the first turn, then start a genuinely new turn for the same
        # existing session. Its old pool is gone and its worker must still read.
        (directory / "tools-done-owned-first").touch()
        threads[0].join(timeout=10)
        assert not threads[0].is_alive() and outcomes["owned-first"]["done"]
        threads.append(threading.Thread(
            target=turn, args=(first, "owned", "owned-second", doc), daemon=True
        ))
        threads[-1].start()
        resumed = result("before", "owned-second")
        assert resumed["pid"] == before["pid"], resumed
        assert not resumed["result"].get("_is_error"), resumed
        assert "continued-box" in json.dumps(resumed["result"])

        # Same uid, forged hello PID and the exact writer_group do not make an
        # independent process the daemon-owned worker.
        with socket.socket(socket.AF_UNIX) as outsider:
            outsider.settimeout(5)
            outsider.connect(daemon_bridge.socket_path(daemon_bridge.resolve_hostd_bin()))
            outsider.sendall((json.dumps({
                "id": 7, "method": "hello", "params": {
                    "client": "mcp", "pid": before["pid"],
                    "writer_group": before["writer_group"], "proto": 1,
                }
            }) + "\n").encode())
            with outsider.makefile("rb") as stream:
                event = json.loads(stream.readline())
                refusal = json.loads(stream.readline())
            assert event["event"] == "daemon.draining"
            assert refusal["error"]["code"] == "daemon_draining"
            assert refusal["error"]["reason"] == "stale_binary"

        # A second daemon-owned worker created after the snapshot is not an old
        # session merely because its process is supervised by this hostd.
        threads.append(threading.Thread(target=turn, args=(second, "new", "new"), daemon=True))
        threads[-1].start()
        fresh = result("before", "new")
        assert fresh["error"]["code"] == "daemon_draining", fresh
        assert fresh["error"]["reason"] == "stale_binary"
        (directory / "tools-done-new").touch()
        threads[-1].join(timeout=10)
        assert not threads[-1].is_alive()

        (directory / "tools-go-after-owned-second").touch()
        continued = result("after", "owned-second")
        assert continued["edit"].get("ok") is not False, continued
        assert continued["edit"]["revision"] == 2, continued
        # Explicit Stop supersedes the soft drain, even for the original worker.
        assert controller.call("daemon_drain")["ok"]
        (directory / "tools-go-hard-owned-second").touch()
        hard = result("hard", "owned-second")
        assert hard["error"]["code"] == "daemon_draining", hard
        assert hard["error"]["reason"] == "stop_requested"
        assert hard["error"]["hard_stop"] is True
        threads[1].join(timeout=10)
        assert not threads[1].is_alive()
        assert outcomes["owned-second"]["done"]
    finally:
        for session in ("owned-first", "owned-second", "new"):
            (directory / f"tools-done-{session}").touch()
        for thread in threads:
            thread.join(timeout=10)
        for client in clients:
            client.close()


@pytest.mark.skipif(sys.platform == "win32", reason="fake worker uses Unix sockets")
@pytest.mark.parametrize("mode", ["ro", "rw"])
def test_live_runs_hold_touched_docs_after_tool_disconnect(
    worker_daemon, monkeypatch, tmp_path, mode
):
    monkeypatch.setenv("CHUDWARE_TEST_WORKER_MODE", "drain_tools")
    monkeypatch.setenv("CHUDWARE_DOC_GC_GRACE_S", "1")
    monkeypatch.setenv("CHUDWARE_DOC_GC_AGENT_GRACE_S", "1")
    monkeypatch.setenv("CHUDWARE_ENGINE_DAEMON", "1")
    monkeypatch.delenv("CHUDWARE_PROJECT_DIR", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "state"))
    gui, minted, runner_a, runner_b, runner_c, runner_d, observer = (
        DaemonClient() for _ in range(7))
    clients = (gui, minted, runner_a, runner_b, runner_c, runner_d, observer)
    directory = worker_daemon["turn_dir"]
    outcomes = {}
    threads = []
    try:
        for client in clients:
            client.connect()
        worker_daemon["pids"].append(_pid(observer))
        gui.call("hello", {"client": "gui", "pid": os.getpid(), "proto": 1})
        existing = gui.call("doc_new", {"name": "gui-owned"})["doc_id"]
        edited = gui.call("doc_apply_ops", {"doc_id": existing, "ops": [{
            "op": "add_feature", "feature": {"type": "box", "name": "authored",
                "params": {"length": 10, "width": 10, "height": 10}, "refs": {}}
        }]})
        assert not edited.get("_is_error"), edited
        background = gui.call("doc_new", {"name": "background-owned"})["doc_id"]
        resumed = gui.call("doc_new", {"name": "resume_part_v1"})["doc_id"]
        built = gui.call("doc_apply_ops", {"doc_id": resumed, "ops": [{
            "op": "add_feature", "feature": {"type": "box", "name": "built",
                "params": {"length": 4, "width": 4, "height": 4}, "refs": {}}
        }]})
        assert not built.get("_is_error"), built

        def turn(client, run_id, **params):
            outcomes[run_id] = client.call("agent_admin_turn", {
                "session_id": run_id, "run_id": run_id, "request": "work", **params})

        threads.append(threading.Thread(target=turn, args=(runner_a, "existing"),
            kwargs={"_test_target_doc": existing, "_test_attach_mode": mode}, daemon=True))
        threads.append(threading.Thread(target=turn, args=(runner_b, "minted"), daemon=True))
        threads.append(threading.Thread(target=turn, args=(runner_c, "shared"),
            kwargs={"_test_target_doc": existing, "_test_job_doc": background,
                    "_test_resume_doc": resumed}, daemon=True))
        threads.append(threading.Thread(target=turn, args=(runner_d, "internal"),
            kwargs={"_test_internal": True}, daemon=True))
        for thread in threads:
            thread.start()
        for name in ("existing", "minted", "shared", "internal"):
            _wait_for(directory / f"tools-before-{name}.json", timeout=60)
        first = json.loads((directory / "tools-before-existing.json").read_text())
        second = json.loads((directory / "tools-before-minted.json").read_text())
        shared = json.loads((directory / "tools-before-shared.json").read_text())
        internal = json.loads((directory / "tools-before-internal.json").read_text())
        assert first["result"]["ok"] and second["result"]["doc_id"]
        assert not shared["result"].get("_is_error"), shared
        assert isinstance(shared["job_result"].get("tree"), dict), shared
        assert shared["resume_result"]["doc_id"] == resumed and shared["resume_result"]["reused"]
        agent_doc = second["doc_id"]
        internal_doc = internal["doc_id"]

        # The fake tool pools have disconnected. A GUI close and another client's
        # orphan must not destroy the live run's GUI-origin or minted documents.
        assert gui.call("doc_discard", {"doc_id": existing})["detached"] is True
        assert gui.call("doc_discard", {"doc_id": background})["detached"] is True
        assert gui.call("doc_discard", {"doc_id": resumed})["detached"] is True
        unrelated = minted.call("doc_new", {"name": "unrelated"})["doc_id"]
        minted.close()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            ids = {row["doc_id"] for row in observer.call("doc_list")["docs"]}
            if unrelated not in ids:
                break
            time.sleep(0.2)
        assert unrelated not in ids
        assert {existing, background, resumed, agent_doc} <= ids
        assert threads[3].is_alive()
        # Internal docs are hidden from doc_list even before GC. If the socket
        # teardown has not arrived yet, detach the probe and allow a fresh grace.
        deadline = time.monotonic() + 10
        while True:
            missing = observer.call("attach_doc", {"doc_id": internal_doc, "mode": "ro"})
            if missing.get("_is_error") or time.monotonic() >= deadline:
                break
            observer.call("detach_doc", {"doc_id": internal_doc})
            time.sleep(2)
        assert missing.get("_is_error") and missing.get("code") == "not_found"
        assert all(entry["doc_id"] != internal_doc
                   for entry in observer.call("recovery_list")["entries"])

        # One of two runs leaving the same document cannot release the other's hold.
        (directory / "tools-done-existing").touch()
        threads[0].join(timeout=10)
        assert not threads[0].is_alive() and outcomes["existing"]["done"]
        time.sleep(2.5)
        ids = {row["doc_id"] for row in observer.call("doc_list")["docs"]}
        assert {existing, background, resumed, agent_doc} <= ids
        assert threads[2].is_alive()

        # The document becomes collectible when its last owner ends, even while
        # the unrelated minted-document run is still alive.
        (directory / "tools-done-shared").touch()
        threads[2].join(timeout=10)
        assert not threads[2].is_alive() and outcomes["shared"]["done"]
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            ids = {row["doc_id"] for row in observer.call("doc_list")["docs"]}
            if existing not in ids and background not in ids and resumed not in ids:
                break
            time.sleep(0.2)
        assert not ({existing, background, resumed} & ids) and agent_doc in ids
        assert threads[1].is_alive()
        assert any(entry["doc_id"] == existing
                   for entry in observer.call("recovery_list")["entries"])

        (directory / "tools-done-minted").touch()
        threads[1].join(timeout=10)
        assert not threads[1].is_alive() and outcomes["minted"]["done"]
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            ids = {row["doc_id"] for row in observer.call("doc_list")["docs"]}
            if agent_doc not in ids:
                break
            time.sleep(0.2)
        assert agent_doc not in ids
    finally:
        for name in ("existing", "minted", "shared", "internal"):
            (directory / f"tools-done-{name}").touch()
        for thread in threads:
            thread.join(timeout=10)
        for client in clients:
            client.close()


@pytest.mark.skipif(sys.platform != "linux", reason="continuity requires Linux peer pidfds")
def test_soft_drain_queues_existing_session_on_its_busy_owner(
    worker_daemon, monkeypatch, tmp_path
):
    _require_peer_pidfd()
    hostd_copy = tmp_path / "chudware-hostd"
    shutil.copy2(HOSTD, hostd_copy)
    monkeypatch.setenv("CHUDWARE_HOSTD_BIN", str(hostd_copy))
    if not os.environ.get("CHUDWARE_LIBCHUDWARE"):
        monkeypatch.setenv("CHUDWARE_LIBCHUDWARE", str(Path(HOSTD).resolve().parent / "libchudware.so"))
    monkeypatch.setenv("CHUDWARE_HOSTD_STALECHECK_S", "1")
    monkeypatch.setenv("CHUDWARE_TEST_WORKER_MODE", "drain_tools")
    first, busy, controller = DaemonClient(), DaemonClient(), DaemonClient()
    clients = (first, busy, controller)
    directory = worker_daemon["turn_dir"]
    threads = []
    outcomes = {}

    def turn(client, session, run_id, doc_id=None):
        params = {"session_id": session, "run_id": run_id, "request": "work"}
        if doc_id:
            params["doc_id"] = doc_id
        outcomes[run_id] = client.call("agent_admin_turn", params)

    def before(run_id):
        path = directory / f"tools-before-{run_id}.json"
        _wait_for(path, timeout=60)
        return json.loads(path.read_text(encoding="utf-8"))

    try:
        for client in clients:
            client.connect()
        worker_daemon["pids"].append(_pid(controller))
        threads.append(threading.Thread(
            target=turn, args=(first, "waiting-session", "seed"), daemon=True
        ))
        threads[-1].start()
        seed = before("seed")
        doc = seed["doc_id"]
        assert controller.call("attach_doc", {"doc_id": doc, "mode": "rw"})["ok"]
        (directory / "tools-done-seed").touch()
        threads[-1].join(timeout=10)
        assert not threads[-1].is_alive() and outcomes["seed"]["done"]

        # A second conversation occupies the same worker which still owns the
        # first session; a later first-session turn must wait for that owner.
        threads.append(threading.Thread(
            target=turn, args=(busy, "busy-session", "busy"), daemon=True
        ))
        threads[-1].start()
        active = before("busy")
        assert active["pid"] == seed["pid"]
        time.sleep(1.2)
        old = hostd_copy.stat().st_mtime_ns
        os.utime(hostd_copy, ns=(old + 2_000_000_000, old + 2_000_000_000))
        deadline = time.monotonic() + 10
        while not controller.call("daemon_status")["draining"]:
            assert time.monotonic() < deadline
            time.sleep(0.05)

        threads.append(threading.Thread(
            target=turn, args=(first, "waiting-session", "resumed", doc), daemon=True
        ))
        threads[-1].start()
        deadline = time.monotonic() + 10
        while controller.call("daemon_status")["inflight"] < 3:
            assert time.monotonic() < deadline
            time.sleep(0.01)
        time.sleep(0.2)
        assert threads[-1].is_alive()
        assert not (directory / "tools-before-resumed.json").exists()
        assert _worker_count(worker_daemon["counter"]) == 1

        (directory / "tools-done-busy").touch()
        threads[1].join(timeout=10)
        assert not threads[1].is_alive() and outcomes["busy"]["done"]
        resumed = before("resumed")
        assert resumed["pid"] == seed["pid"], resumed
        assert not resumed["result"].get("_is_error"), resumed
        assert resumed["doc_id"] == doc
        assert _worker_count(worker_daemon["counter"]) == 1
        (directory / "tools-done-resumed").touch()
        threads[2].join(timeout=10)
        assert not threads[2].is_alive() and outcomes["resumed"]["done"]
    finally:
        for run_id in ("seed", "busy", "resumed"):
            (directory / f"tools-done-{run_id}").touch()
        for thread in threads:
            thread.join(timeout=10)
        for client in clients:
            client.close()


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
    selection = {"strategy": "snapcompact", "compaction": {"keep_recent_tokens": 500}}
    policy = controller.call("omp_apply_compaction_policy", selection)
    assert policy["ok"] is True
    assert {s["session_id"] for s in policy["sessions"]} == {"chat-a", "chat-b"}
    assert len(policy["workers"]) == 2
    for worker in (a, b):
        saved = worker_daemon["turn_dir"] / ("policy-params-%d" % worker["pid"])
        assert json.loads(saved.read_text()) == selection
    assert all(t.is_alive() for t in threads)
    (worker_daemon["turn_dir"] / ("policy-fail-%d" % b["pid"])).write_text("fail")
    failed = controller.call("omp_apply_compaction_policy", selection)
    assert failed["ok"] is False
    assert {s["session_id"] for s in failed["sessions"] if not s["ok"]} == {"chat-b"}
    assert all(t.is_alive() for t in threads)
    assert _worker_count(worker_daemon["counter"]) == 2

    assert controller.call("agent_cancel", {"run_id": "parallel-b"})["cancelled"]
    threads[1].join(timeout=5.0)
    assert not threads[1].is_alive()
    assert outcomes["parallel-b"]["control"] == "cancel"
    assert threads[0].is_alive()
    assert "parallel-a" not in outcomes
    # Session-scoped calls follow the extra even while the primary remains occupied.
    stats = controller.call("omp_session_stats", {"session_id": "chat-b"})
    assert stats["pid"] == b["pid"]
    assert stats["lane"] == "agent-turn"
    # Session-free service calls have a worker of their own, so the busy primary never refuses
    # them; a method that still belongs to the agent lane does, which proves the primary is held.
    served = controller.call("subscription_status")
    assert served["lane"] == "service" and served["pid"] not in (a["pid"], b["pid"]), served
    assert controller.call("dag_status")["code"] == "worker_lane_busy"
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


@pytest.mark.parametrize("session_id", ["busy-chat", "other-chat"])
def test_compaction_refuses_busy_admission_without_queueing_or_stopping_work(
    worker_daemon, monkeypatch, session_id
):
    monkeypatch.setenv("CHUDWARE_AGENT_WORKERS", "1")
    requester, maintenance, controller = DaemonClient(), DaemonClient(), DaemonClient()
    requester.connect()
    worker_daemon["pids"].append(_pid(requester))
    maintenance.connect()
    controller.connect()
    outcomes = {}
    running = threading.Thread(
        target=_run_turn, args=(requester, "busy-chat", "existing", outcomes), daemon=True
    )
    compacted = threading.Thread(target=lambda: outcomes.update(compact=maintenance.call(
        "agent_admin_turn", {"session_id": session_id, "run_id": "maintenance",
                             "compact": {"strategy": "summary"}}
    )), daemon=True)
    try:
        running.start()
        _wait_for(worker_daemon["turn_dir"] / "run-existing.json")
        compacted.start()
        compacted.join(timeout=2.0)
        assert not compacted.is_alive(), "compaction must refuse admission, not wait for the run"
        assert outcomes["compact"]["_is_error"] is True
        assert outcomes["compact"]["code"] == "worker_lane_busy"
        assert running.is_alive(), "refusing compaction must leave existing work running"
        assert not (worker_daemon["turn_dir"] / "run-maintenance.json").exists()
        assert controller.call("agent_cancel", {"run_id": "existing"})["cancelled"]
        running.join(timeout=5.0)
        assert outcomes["existing"]["control"] == "cancel"
        assert not (worker_daemon["turn_dir"] / "run-maintenance.json").exists()
    finally:
        controller.call("agent_cancel", {"run_id": "existing"})
        running.join(timeout=5.0)
        # On a regression the queued request may now have started in the fixture.
        if compacted.is_alive():
            _wait_for(worker_daemon["turn_dir"] / "run-maintenance.json")
            controller.call("agent_cancel", {"run_id": "maintenance"})
            compacted.join(timeout=5.0)
        for client in (requester, maintenance, controller):
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

@pytest.mark.parametrize("release", ["accept", "refuse"])
def test_compaction_transfers_or_refuses_without_disturbing_other_chat(
    worker_daemon, monkeypatch, release
):
    if release == "refuse":
        monkeypatch.setenv("CHUDWARE_TEST_RELEASE", "refuse")
    clients = [DaemonClient() for _ in range(4)]
    for client in clients:
        client.connect()
    worker_daemon["pids"].append(_pid(clients[0]))
    controller = clients[3]
    directory = worker_daemon["turn_dir"]
    outcomes = {}
    seed = threading.Thread(target=_run_turn, args=(clients[0], "chat-a", "seed", outcomes))
    busy = threading.Thread(target=_run_turn, args=(clients[1], "chat-b", "busy", outcomes))
    choice = {"strategy": "snapcompact", "instructions": "retain CAD decisions"}
    compact = threading.Thread(target=lambda: outcomes.update(compact=clients[2].call(
        "agent_admin_turn", {"session_id": "chat-a", "run_id": "compact", "compact": choice}
    )))
    try:
        seed.start()
        _wait_for(directory / "run-seed.json")
        assert controller.call("agent_cancel", {"run_id": "seed"})["cancelled"]
        seed.join(timeout=5)
        busy.start()
        _wait_for(directory / "run-busy.json")
        assert outcomes["seed"]["pid"] == json.loads((directory / "run-busy.json").read_text())["pid"]
        compact.start()
        if release == "accept":
            _wait_for(directory / "run-compact.json")
            info = json.loads((directory / "run-compact.json").read_text())
            assert info["pid"] != outcomes["seed"]["pid"] and info["compact"] == choice
            released = (directory / f"released-{outcomes['seed']['pid']}").read_text().split()
            assert released[0] == "chat-a" and float(released[1]) < info["started"]
            assert busy.is_alive()
            assert controller.call("agent_cancel", {"run_id": "compact"})["cancelled"]
        else:
            compact.join(timeout=2)
            assert not compact.is_alive() and outcomes["compact"]["code"] == "worker_lane_busy"
            assert not (directory / "run-compact.json").exists() and busy.is_alive()
        compact.join(timeout=5)
        assert controller.call("agent_cancel", {"run_id": "busy"})["cancelled"]
        busy.join(timeout=5)
        assert outcomes["busy"]["control"] == "cancel"
    finally:
        for run_id in ("seed", "busy", "compact"):
            controller.call("agent_cancel", {"run_id": run_id})
        for thread in (seed, busy, compact):
            if thread.ident is not None:
                thread.join(timeout=5)
        for client in clients:
            client.close()


@pytest.mark.parametrize("compaction", [False, True])
def test_stop_during_session_transfer_does_not_start_the_stopped_run(
    worker_daemon, monkeypatch, compaction
):
    monkeypatch.setenv("CHUDWARE_TEST_RELEASE", "held")
    clients = [DaemonClient() for _ in range(4)]
    for client in clients:
        client.connect()
    worker_daemon["pids"].append(_pid(clients[0]))
    controller = clients[3]
    directory = worker_daemon["turn_dir"]
    outcomes = {}
    seed = threading.Thread(target=_run_turn, args=(clients[0], "chat-a", "seed", outcomes))
    busy = threading.Thread(target=_run_turn, args=(clients[1], "chat-b", "busy", outcomes))
    params = {"session_id": "chat-a", "run_id": "moving",
              **({"compact": {"strategy": "summary"}} if compaction else {"request": "work"})}
    moving = threading.Thread(target=lambda: outcomes.update(
        moving=clients[2].call("agent_admin_turn", params)))
    try:
        seed.start()
        _wait_for(directory / "run-seed.json")
        assert controller.call("agent_cancel", {"run_id": "seed"})["cancelled"]
        seed.join(timeout=5)
        busy.start()
        _wait_for(directory / "run-busy.json")
        moving.start()
        _wait_for(directory / "release-entered-chat-a")
        assert controller.call("agent_cancel", {"run_id": "moving"})["cancelled"]
        assert busy.is_alive()
        (directory / "release-permit-chat-a").touch()
        moving.join(timeout=5)
        assert not moving.is_alive()
        assert not (directory / "run-moving.json").exists()
        assert outcomes["moving"]["turn"]["cancelled"] is True
        assert controller.call("agent_cancel", {"run_id": "busy"})["cancelled"]
        busy.join(timeout=5)
        assert outcomes["busy"]["control"] == "cancel"
    finally:
        (directory / "release-permit-chat-a").touch()
        for run_id in ("seed", "busy", "moving"):
            controller.call("agent_cancel", {"run_id": run_id})
        for thread in (seed, busy, moving):
            if thread.ident is not None:
                thread.join(timeout=5)
        for client in clients:
            client.close()


def test_stop_while_waiting_for_worker_never_runs_the_prompt(worker_daemon, monkeypatch):
    monkeypatch.setenv("CHUDWARE_AGENT_WORKERS", "1")
    first, second, controller = DaemonClient(), DaemonClient(), DaemonClient()
    for client in (first, second, controller):
        client.connect()
    worker_daemon["pids"].append(_pid(first))
    directory = worker_daemon["turn_dir"]
    outcomes = {}
    busy = threading.Thread(target=_run_turn, args=(first, "chat-a", "held", outcomes))
    waiting = threading.Thread(target=_run_turn, args=(second, "chat-b", "waiting", outcomes))
    try:
        busy.start()
        _wait_for(directory / "run-held.json")
        waiting.start()
        deadline = time.monotonic() + 5
        while controller.call("daemon_status").get("inflight", 0) < 3:
            assert time.monotonic() < deadline
            time.sleep(0.01)
        assert controller.call("agent_cancel", {"run_id": "waiting"})["cancelled"]
        waiting.join(timeout=2)
        assert not waiting.is_alive() and outcomes["waiting"]["turn"]["cancelled"]
        assert busy.is_alive() and not (directory / "run-waiting.json").exists()
        assert controller.call("agent_cancel", {"run_id": "held"})["cancelled"]
        busy.join(timeout=5)
        assert outcomes["held"]["control"] == "cancel"
        assert not (directory / "run-waiting.json").exists()
    finally:
        for run_id in ("held", "waiting"):
            controller.call("agent_cancel", {"run_id": run_id})
        for thread in (busy, waiting):
            if thread.ident is not None:
                thread.join(timeout=5)
        for client in (first, second, controller):
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
        "bench_status", "triggers_list", "packs_save", "rule_create",
        "dag_status", "memory_recall", "session_btw",
        "omp_session_stats", "ai_prompt_warm", "ai_tool_catalog",
    ):
        response = controller.call(method)
        assert response["pid"] != general_pid, method
        assert response["lane"] == "agent", method
    agent_pid = controller.call("dag_status")["pid"]
    # Session-free provider/auth/assignment/subscription/skills calls own a third worker, so a
    # turn on the agent lane can never refuse them. Every family member lands on it together.
    service_pids = set()
    for method in (
        "subscription_status", "subscription_accounts", "subscription_usage",
        "skills_list", "skills_get", "ai_roles_get", "ai_roles_set",
        "omp_provider_list", "omp_models_list", "omp_auth_state", "omp_set_key",
    ):
        response = controller.call(method)
        assert response["lane"] == "service", method
        assert response["pid"] not in (general_pid, agent_pid), method
        service_pids.add(response["pid"])
    assert len(service_pids) == 1
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
    assert _worker_count(worker_daemon["counter"]) == 3

    # Clean daemon shutdown reaps EVERY child (agent, general and service) rather than only the
    # lane last used; no lane is permitted to outlive its owner.
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
    # Live 2026-09-27: the GUI's subscription_accounts (then an agent-lane call) queued behind
    # an hours-long agent_admin_turn on the lane's stdio mutex, ignoring its 120 s bound, until
    # the long-tier watchdog _Exit'ed hostd and took the live session's docs with it; the
    # GUI's single async RPC lane stalled behind it. A call that still belongs to the agent
    # lane (subscription_accounts itself moved to the service worker) must, behind a running
    # turn, fail at once with a named error while the turn keeps running and hostd stays up.
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
    busy = gui.call("triggers_list")
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
    served = gui.call("triggers_list")
    assert served.get("lane") == "agent", served
    assert _worker_count(worker_daemon["counter"]) == 1


def test_service_calls_are_served_live_while_a_turn_holds_the_agent_lane(worker_daemon):
    # Provider/model discovery, model assignments, subscription status, skills and key save
    # are GUI polls and user actions that used to be refused for as long as ANY agent turn
    # held the primary worker (hours), leaving the provider list and model picker empty.
    # They have a worker of their own now: served at once, never by the turn's worker.
    requester, gui = DaemonClient(), DaemonClient()
    requester.connect()
    worker_daemon["pids"].append(_pid(requester))
    gui.connect()
    outcome: dict = {}
    thread = threading.Thread(
        target=lambda: outcome.update(
            result=requester.call(
                "agent_admin_turn", {"run_id": "service-live", "request": "work"}
            )
        ),
        daemon=True,
    )
    thread.start()
    record = worker_daemon["turn_dir"] / "run-service-live.json"
    _wait_for(record)
    turn_pid = json.loads(record.read_text(encoding="utf-8"))["pid"]

    started = time.monotonic()
    service_pids = set()
    for method in (
        "omp_provider_list", "omp_models_list", "omp_auth_state", "omp_set_key",
        "ai_roles_get", "ai_roles_set", "subscription_status", "subscription_accounts",
        "subscription_usage", "skills_list", "skills_get",
    ):
        response = gui.call(method)
        assert response.get("code") != "worker_lane_busy", (method, response)
        assert response.get("method") == method, response
        assert response.get("lane") == "service", (method, response)
        assert response["pid"] != turn_pid, (method, response)
        service_pids.add(response["pid"])
    assert len(service_pids) == 1  # one lazily started worker answers the whole family
    assert time.monotonic() - started < 3.0  # the fake turn holds the agent lane for 10 s
    assert thread.is_alive(), "the turn must still be running and holding the agent lane"
    assert worker_daemon["active"].exists()
    # Anything that still belongs to the agent lane (or to a session) stays serialized with it.
    assert gui.call("dag_status").get("code") == "worker_lane_busy"
    assert _worker_count(worker_daemon["counter"]) == 2  # primary + service, no extra turn worker

    assert gui.call("agent_cancel", {"run_id": "service-live"})["cancelled"]
    thread.join(timeout=5.0)
    assert not thread.is_alive()
    assert outcome["result"]["control"] == "cancel"
    # Idle-service calls keep going to the same worker child.
    assert gui.call("omp_provider_list")["pid"] in service_pids
    assert _worker_count(worker_daemon["counter"]) == 2


def test_interactive_sign_in_holds_only_the_service_worker(worker_daemon):
    # omp_login carries the whole browser OAuth flow in ONE call, so it holds the service
    # worker for as long as the user takes. Other service calls are refused at once (they used
    # to queue for their whole timeout behind it); the agent and general lanes are untouched.
    login, gui, requester = DaemonClient(), DaemonClient(), DaemonClient()
    login.connect()
    worker_daemon["pids"].append(_pid(login))
    gui.connect()
    requester.connect()
    outcome: dict = {}
    thread = threading.Thread(
        target=lambda: outcome.update(
            result=login.call("omp_login", {"providerId": "anthropic"})
        ),
        daemon=True,
    )
    thread.start()
    _wait_for(worker_daemon["turn_dir"] / "login-held")

    started = time.monotonic()
    busy = gui.call("omp_provider_list")
    waited = time.monotonic() - started
    assert busy.get("_is_error") is True, busy
    assert busy.get("code") == "worker_lane_busy", busy
    assert busy.get("message") == "worker lane busy (sign-in in progress)", busy
    assert waited < 2.0, waited  # the fake sign-in holds the lane until released
    assert thread.is_alive()
    assert requester.call("dag_status")["lane"] == "agent"
    assert requester.call("plm_status")["lane"] == "general"

    (worker_daemon["turn_dir"] / "login-release").write_text("go", encoding="utf-8")
    thread.join(timeout=5.0)
    assert not thread.is_alive()
    assert outcome["result"]["lane"] == "service"
    served = gui.call("omp_provider_list")
    assert served.get("lane") == "service" and served["pid"] == outcome["result"]["pid"], served


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
    # OMP session. A turn the worker received must fail by name instead.
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
