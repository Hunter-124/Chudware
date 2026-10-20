from __future__ import annotations

import asyncio
import hashlib
import os
import signal
import sqlite3
import subprocess
import sys
from pathlib import Path
import threading
import time
from types import SimpleNamespace

import pytest

from services import worker_activity as activity
from services.agent import chudcode_host as hosts
from services.agent import request_context as rc


def principal(actor="alice"):
    return {"cell": True, "workspace_key": "team:activity/work", "actor_key": actor,
            "actor_hex": hashlib.sha256(actor.encode()).hexdigest()[:32],
            "right": "editor", "epoch": 1}


def wait_until(predicate):
    deadline = time.monotonic() + 3
    while True:
        try:
            if predicate():
                return
        except RuntimeError as exc:
            if exc.args != ("registry_busy",):
                raise
        if time.monotonic() >= deadline:
            pytest.fail("activity transition did not occur")
        time.sleep(0.005)


@pytest.fixture
def isolated(monkeypatch):
    monkeypatch.delenv("CHUDWARE_CELL", raising=False)
    monkeypatch.delenv("CHUDWARE_ACTOR_SANDBOX", raising=False)
    monkeypatch.setattr(activity, "_BACKGROUND_THREADS", {})
    monkeypatch.setattr(activity, "_DESKTOP_SDK_OBSERVED", False)
    monkeypatch.setattr(activity, "_REGISTRIES", ())
    monkeypatch.setattr(activity, "_process_snapshot", lambda: {
        os.getpid(): activity.Process(os.getpid(), 0, "self")})
    return monkeypatch


class SdkState:
    """Stateful pinned-SDK boundary double, independent of the census response."""
    def __init__(self, process=None):
        self._process = process
        self._state_lock = threading.Lock()
        self._event_condition = threading.Condition()
        self._pending = {}
        self._pending_prompt_ids = set()
        self._pending_host_tool_calls = {}
        self._pending_host_uri_requests = {}
        self.state = {"isStreaming": False, "isCompacting": False,
                      "hasPendingAsyncWork": False, "isSettled": True,
                      "queuedMessageCount": 0}
        self.stt = {"state": "idle", "available": True}
        self.gate = None
        self.entered = threading.Event()
        self.calls = []
        self.closed = 0
        self._chudware_activity_owner = None
        if process is not None:
            self._chudware_activity_identity = activity.process_identity(process)
            self._chudware_activity_sdk_identity = self._chudware_activity_identity
            self._chudware_activity_sdk_argv = tuple(process.args)
            self._chudware_activity_sandboxed = False

    def _request(self, method):
        self.calls.append(method)
        if self.gate is not None:
            self.entered.set()
            self.gate.wait(3)
        if method == "get_state":
            return dict(self.state)
        if method == "stt_status":
            if isinstance(self.stt, Exception):
                raise self.stt
            return dict(self.stt)
        raise AssertionError("unexpected SDK command")

    def stop(self):
        self.closed += 1


def install_host(monkeypatch, client):
    host = hosts.ChudcodeSessionHost(client_factory=lambda **kw: client)
    host._sessions["persistent"] = hosts._Session(
        session_id="persistent", client=client, resume_file=None, workspace_dir=None)
    monkeypatch.setattr(hosts, "_DEFAULT_CHUDCODE_HOST", host)
    monkeypatch.setattr(activity, "_REGISTRIES", (hosts.__name__,))
    return host


def test_true_idle_does_not_start_or_import_subsystems(isolated, tmp_path):
    from services.browser import embed
    from services.plm import live_client
    from services.sim import jobs

    def forbidden(*args, **kwargs):
        raise AssertionError("census started a dormant subsystem")

    isolated.setattr(activity, "_REGISTRIES", (
        hosts.__name__, embed.__name__, live_client.__name__, jobs.__name__))
    isolated.setattr(hosts, "_DEFAULT_CHUDCODE_HOST", None)
    isolated.setattr(hosts, "get_default_chudcode_host", forbidden)
    isolated.setattr(embed, "_MANAGER", None)
    isolated.setattr(embed, "get_manager", forbidden)
    isolated.setattr(live_client, "_MANAGER", None)
    isolated.setattr(live_client, "get_live_manager", forbidden)
    isolated.setattr(jobs, "_LOCAL_PROCS", {})
    isolated.setattr(jobs, "_db", forbidden)
    isolated.setenv("CHUDWARE_SIM_JOBS_DIR", str(tmp_path / "unstarted"))
    before = set(sys.modules)
    assert activity.snapshot() == {"ok": True, "active": 0, "by_kind": {}, "complete": True}
    assert set(sys.modules) == before
    assert not (tmp_path / "unstarted").exists()


def test_idle_persistent_sdk_session_is_not_a_job(isolated):
    client = SdkState()
    install_host(isolated, client)
    assert activity.snapshot() == {"ok": True, "active": 0, "by_kind": {}, "complete": True}
    assert client.calls == ["get_state", "stt_status"]
    assert client.closed == 0


@pytest.mark.parametrize("field,value,kind", [
    ("isStreaming", True, "sdk_runtime"),
    ("isCompacting", True, "sdk_runtime"),
    ("hasPendingAsyncWork", True, "sdk_runtime"),
    ("isSettled", False, "sdk_runtime"),
    ("queuedMessageCount", 2, "sdk_queued_messages"),
])
def test_sdk_queued_or_background_work_prevents_idle(isolated, field, value, kind):
    client = SdkState()
    client.state[field] = value
    install_host(isolated, client)
    result = activity.snapshot()
    assert result["ok"] and result["complete"]
    assert result["by_kind"][kind] == (value if field == "queuedMessageCount" else 1)
    assert result["active"] >= 1


@pytest.mark.parametrize("field", ["isStreaming", "isCompacting", "hasPendingAsyncWork",
                                    "isSettled", "queuedMessageCount"])
def test_absent_raw_sdk_state_is_unknown_not_typed_false_default(isolated, field):
    client = SdkState()
    client.state.pop(field)
    install_host(isolated, client)
    result = activity.snapshot()
    assert result == {"ok": False, "active": 0, "by_kind": {}, "complete": False,
                      "reason": "sdk_unattested"}
    assert client.closed == 0


def test_stt_and_pending_cancel_remain_active_until_real_completion(isolated):
    client = SdkState()
    install_host(isolated, client)
    for state in ("recording", "transcribing"):
        client.stt["state"] = state
        assert activity.snapshot()["by_kind"]["sdk_stt"] == 1
    client.stt["state"] = "idle"
    cancel = threading.Event()
    cancel.set()
    client._pending_host_tool_calls["cancelled-but-live"] = SimpleNamespace(cancel_event=cancel)
    assert activity.snapshot()["by_kind"]["sdk_pending_calls"] == 1
    client._pending_host_tool_calls.clear()
    assert activity.snapshot()["active"] == 0


def test_partial_sdk_evidence_keeps_known_background_count(isolated):
    client = SdkState()
    client.state["hasPendingAsyncWork"] = True
    client.stt = RuntimeError("private transport detail must not escape")
    install_host(isolated, client)
    result = activity.snapshot()
    assert not result["ok"] and not result["complete"]
    assert result["by_kind"]["sdk_runtime"] == 1
    assert result["active"] == 1
    assert result["reason"] == "sdk_unattested"
    assert "private" not in str(result)


def test_sdk_census_timeout_is_single_flight_and_does_not_close_client(isolated):
    client = SdkState()
    client.gate = threading.Event()
    host = install_host(isolated, client)
    try:
        assert not activity.snapshot()["complete"]
        assert client.entered.is_set()
        assert not activity.snapshot()["complete"]
        assert client.calls == ["get_state"]
        assert len(host._activity_probes) == 1
        assert client.closed == 0
    finally:
        client.gate.set()
        for probe in tuple(host._activity_probes.values()):
            assert probe.done.wait(2)
    client.gate = None
    # A late idle answer is not reused as a new census's idle attestation.
    client.state["hasPendingAsyncWork"] = True
    result = activity.snapshot()
    assert result["complete"] and result["by_kind"]["sdk_runtime"] == 1


def test_real_queued_active_question_wait_and_pending_cancel_are_counted(isolated):
    from services.agent.test_chudcode_host import FakeClient, FakeFactory

    factory = FakeFactory(block=True)
    client = FakeClient(factory)
    factory.clients.append(client)
    sdk = SdkState()
    client._request = sdk._request
    for name in ("_state_lock", "_event_condition", "_pending", "_pending_prompt_ids",
                 "_pending_host_tool_calls", "_pending_host_uri_requests"):
        setattr(client, name, getattr(sdk, name))

    def abort_without_early_death():
        client.calls.append(("abort", {}))
        client.events = [{"type": "agent_end", "messages": [], "isTerminal": True,
                          "aborted": True}]

    client.abort = abort_without_early_death

    def wait_for_terminal(timeout=None):
        client.idle_timeouts.append(timeout)
        if not client.release.wait(10):
            raise TimeoutError("test terminal was not released")
        for event in client.events:
            for listener in tuple(client.listeners):
                listener(event)
        return tuple(client.events)

    client.wait_for_idle = wait_for_terminal
    host = install_host(isolated, client)
    session = host._sessions["persistent"]
    session.turn_lock.acquire()
    reserved_lock = True
    question_shown = threading.Event()
    output = {}
    def emit(frame):
        if frame.get("type") == "agent_question":
            question_shown.set()
    turn = threading.Thread(target=lambda: output.setdefault("turn", host.run_turn({
        "request": "work", "session_id": "persistent", "run_id": "turn",
        "interactive_questions": True}, emit)))
    question = None
    turn.start()
    try:
        wait_until(lambda: "turn" in host._runs)
        assert not client.prompted.is_set()
        assert activity.snapshot()["by_kind"]["agent_runs"] == 1
        session.turn_lock.release()
        reserved_lock = False
        assert client.prompted.wait(1)
        assert activity.snapshot()["by_kind"]["agent_runs"] == 1
        questions = host._runs["turn"].questions
        assert questions is not None
        question = threading.Thread(target=lambda: questions.ask({
            "batch_id": "batch", "questions": [{"id": "answer", "text": "Choose"}]}))
        question.start()
        assert question_shown.wait(1)
        assert questions.has_pending()
        assert activity.snapshot()["by_kind"]["agent_runs"] == 1
        assert host.cancel("turn")["cancelled"]
        assert host._runs["turn"].cancel_requested
        assert activity.snapshot()["by_kind"]["agent_runs"] == 1
        client.release.set()
        turn.join(2)
        question.join(2)
        assert not turn.is_alive() and not question.is_alive()
        assert output["turn"]["cancelled"]
        assert activity.snapshot()["active"] == 0
    finally:
        if reserved_lock:
            session.turn_lock.release()
        client.release.set()
        host.shutdown()
        turn.join(2)
        if question is not None:
            question.join(2)


def _run_process_boundary(proof):
    # A full suite legitimately owns session-scoped subprocesses. Run the
    # census as a fresh worker rather than exempting the suite's descendants.
    code = (
        "import sys; sys.path.insert(0, sys.argv[1]); "
        "import pytest; from services import test_worker_activity as proofs; "
        "patch = pytest.MonkeyPatch(); "
        "getattr(proofs, sys.argv[2])(patch)"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-c", code, str(Path(__file__).resolve().parents[1]), proof],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.skipif(sys.platform != "linux", reason="Linux process ownership boundary")
def test_unknown_real_detached_process_pins_and_death_unpins():
    _run_process_boundary("_unknown_real_detached_process_pins_and_death_unpins")


def _unknown_real_detached_process_pins_and_death_unpins(monkeypatch):
    monkeypatch.delenv("CHUDWARE_CELL", raising=False)
    monkeypatch.delenv("CHUDWARE_ACTOR_SANDBOX", raising=False)
    monkeypatch.setattr(activity, "_REGISTRIES", ())
    monkeypatch.setattr(activity, "_BACKGROUND_THREADS", {})
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                             start_new_session=True)
    try:
        result = activity.snapshot()
        assert result == {"ok": False, "active": 1,
                          "by_kind": {"unclassified_process": 1}, "complete": False,
                          "reason": "unclassified_process"}
    finally:
        child.terminate()
        child.wait(timeout=3)
    assert activity.snapshot()["active"] == 0
    assert activity.snapshot()["complete"]


@pytest.mark.skipif(sys.platform != "linux", reason="Linux process ownership boundary")
def test_idle_owned_sdk_root_never_whitelists_its_real_detached_child():
    _run_process_boundary("_idle_owned_sdk_root_never_whitelists_its_real_detached_child")


def _idle_owned_sdk_root_never_whitelists_its_real_detached_child(monkeypatch):
    monkeypatch.delenv("CHUDWARE_CELL", raising=False)
    monkeypatch.delenv("CHUDWARE_ACTOR_SANDBOX", raising=False)
    monkeypatch.setattr(activity, "_BACKGROUND_THREADS", {})
    code = (
        "import subprocess,sys,time; "
        "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'],"
        "start_new_session=True); print(child.pid,flush=True); sys.stdin.readline(); "
        "child.terminate(); child.wait(); print('idle',flush=True); sys.stdin.readline()")
    root = subprocess.Popen([sys.executable, "-c", code], text=True,
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            start_new_session=True)
    child_pid = None
    child_identity = None
    try:
        child_pid = int(root.stdout.readline())
        child_identity = activity._process(child_pid)
        client = SdkState(root)
        install_host(monkeypatch, client)
        result = activity.snapshot()
        assert result["by_kind"]["unclassified_process"] == 1
        assert not result["complete"]
        root.stdin.write("finish child\n")
        root.stdin.flush()
        assert root.stdout.readline().strip() == "idle"
        result = activity.snapshot()
        assert result == {"ok": False, "active": 0, "by_kind": {}, "complete": False,
                          "reason": "process_ownership_unattested"}
        assert root.poll() is None
    finally:
        if child_pid is not None and activity._process(child_pid) == child_identity:
            try:
                os.kill(child_pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        if root.poll() is None:
            root.terminate()
        root.wait(timeout=3)
        root.stdin.close()
        root.stdout.close()


def test_cell_namespace_orphan_remains_unknown_even_outside_worker_descendants(isolated):
    ctx = principal()
    isolated.setenv("CHUDWARE_CELL", "1")
    isolated.setenv("CHUDWARE_ACTOR_SANDBOX", "1")
    isolated.setenv("CHUDWARE_WORKSPACE_KEY", ctx["workspace_key"])
    isolated.setenv("CHUDWARE_ACTOR_KEY", ctx["actor_key"])
    isolated.setenv("CHUDWARE_ACTOR_HEX", ctx["actor_hex"])
    records = {os.getpid(): activity.Process(os.getpid(), 1, "worker"),
               1: activity.Process(1, 0, "launcher"),
               900000: activity.Process(900000, 1, "detached")}
    isolated.setattr(activity, "_process_snapshot", lambda: records)
    isolated.setattr(activity, "_launcher_ancestors", lambda records: {os.getpid(), 1})
    with rc.bind_context(ctx):
        result = activity.snapshot()
    assert result["by_kind"] == {"unclassified_process": 1}
    assert result["active"] == 1 and not result["complete"]
    assert "alice" not in str(result) and "detached" not in str(result)


def test_failed_registry_retains_other_known_work(isolated):
    from services.piping import worker_rpc as piping
    from services.parts import hub
    isolated.setattr(activity, "_REGISTRIES", (piping.__name__, hub.__name__))
    isolated.setattr(piping, "_JOBS", {"reserved": {"state": "running"}})
    def broken():
        raise RuntimeError("private path /tenant/alice")
    isolated.setattr(hub, "worker_activity", broken)
    result = activity.snapshot()
    assert result == {"ok": False, "active": 1, "by_kind": {"piping_cfd": 1},
                      "complete": False, "reason": "registry_probe_failed"}


def test_cfd_background_cancel_keeps_scoped_context_until_completion(isolated, tmp_path):
    from services.piping import worker_rpc as piping
    isolated.setattr(activity, "_REGISTRIES", (piping.__name__,))
    isolated.setattr(piping, "_JOBS", {})
    isolated.setattr(piping.fitting_cfd, "validate_request", lambda *args: None)
    entered, finish, completed = threading.Event(), threading.Event(), threading.Event()
    seen = {}
    def compute(**kwargs):
        seen["owner"] = rc.owner_key()
        seen["root"] = rc.root_value("CHUDWARE_PROJECT_DIR")
        entered.set()
        finish.wait(3)
        completed.set()
        return {"ok": False, "reason": "cancelled"}
    isolated.setattr(piping.fitting_cfd, "compute", compute)
    with rc.bind_context(principal()), rc.bind_roots(str(tmp_path)):
        started = piping.start({})
        assert entered.wait(1)
        assert piping.worker_activity()["by_kind"]["piping_cfd"] == 1
        piping.cancel({"job_id": started["job_id"]})
        assert piping.worker_activity()["by_kind"]["piping_cfd"] == 1
        assert seen == {"owner": (principal()["workspace_key"], "alice"), "root": str(tmp_path)}
        finish.set()
        assert completed.wait(1)
        wait_until(lambda: piping.status({"job_id": started["job_id"]})["state"] == "cancelled")
        wait_until(lambda: piping.worker_activity()["by_kind"]["piping_cfd"] == 0)
    assert rc.owner_key() is None


def test_superseded_or_evicted_parts_search_threads_remain_counted(isolated):
    from services.parts import gui_rpc as parts
    isolated.setattr(parts, "_live_jobs", {})
    isolated.setattr(parts, "_live_threads", {})
    isolated.setattr(parts, "_thumb_futures", {})
    isolated.setattr(parts, "_login_threads", {})
    isolated.setattr(parts, "_health_thread", None)
    isolated.setattr(parts, "_LIVE_JOB_CAP", 1)
    isolated.setattr(parts, "_compute_routing", lambda p: {"route_sites": ["example"]})
    isolated.setattr(parts, "_cef_unavailable", lambda: False)
    entered, finish = threading.Event(), threading.Event()
    def fetch(*args):
        entered.set()
        finish.wait(3)
    isolated.setattr(parts, "_run_live_site", fetch)
    try:
        first = parts._parts_search_live_start({}, lambda e: None)
        assert entered.wait(1)
        second = parts._parts_search_live_start({}, lambda e: None)
        assert first["token"] != second["token"] and len(parts._live_jobs) == 1
        assert parts.worker_activity()["by_kind"]["parts_search_threads"] == 2
    finally:
        finish.set()
        for thread in tuple(parts._live_threads):
            thread.join(2)
    assert parts.worker_activity()["by_kind"]["parts_search_threads"] == 0


def test_running_thumbnail_after_batch_timeout_is_not_lost(isolated):
    from services.parts import gui_rpc as parts
    isolated.setattr(parts, "_live_jobs", {})
    isolated.setattr(parts, "_live_threads", {})
    isolated.setattr(parts, "_thumb_futures", {})
    isolated.setattr(parts, "_login_threads", {})
    isolated.setattr(parts, "_health_thread", None)
    isolated.setattr(parts, "_thumb_cached", lambda item: None)
    isolated.setattr(parts, "_thumb_batch_budget_s", lambda: 0.02)
    finish = threading.Event()
    entered = threading.Event()
    def thumbnail(*args):
        entered.set()
        finish.wait(3)
        return {"ok": True, "image_b64": ""}
    isolated.setattr(parts, "_thumb_produce", thumbnail)
    executor_type = parts.ThreadPoolExecutor
    class StartingExecutor(executor_type):
        def submit(self, *args, **kwargs):
            future = super().submit(*args, **kwargs)
            assert entered.wait(2)
            return future
    isolated.setattr(parts, "ThreadPoolExecutor", StartingExecutor)
    try:
        response = parts._parts_thumbnails({"items": [{"item_id": "part"}]}, lambda e: None)
        assert response["images"][0]["reason"] == "timeout"
        assert parts.worker_activity()["by_kind"]["parts_thumbnails"] == 1
    finally:
        finish.set()
        for future in tuple(parts._thumb_futures):
            future.result(timeout=2)
    assert parts.worker_activity()["by_kind"]["parts_thumbnails"] == 0


def test_sim_reservation_pending_cancel_and_group_child_survival(isolated, tmp_path):
    from services.sim import jobs
    isolated.setenv("CHUDWARE_SIM_JOBS_DIR", str(tmp_path))
    isolated.setattr(jobs, "_LOCAL_PROCS", {})
    with jobs._db() as connection:
        connection.execute("INSERT INTO jobs(job_id,kind,stage,state,owner) VALUES(?,?,?,?,NULL)",
                           ("reservation", "selftest", "sleep", jobs.QUEUED))
        connection.execute("INSERT INTO jobs(job_id,kind,stage,state,pid,pgid,proc_key,owner,cancel_requested) "
                           "VALUES(?,?,?,?,?,?,?,NULL,1)",
                           ("cancel", "selftest", "sleep", jobs.RUNNING, 800001, 800001, "start"))
    records = {os.getpid(): activity.Process(os.getpid(), 0, "self"),
               800001: activity.Process(800001, os.getpid(), "start", 800001)}
    isolated.setattr(activity, "_process_snapshot", lambda: records)
    assert jobs.worker_activity()["by_kind"]["simulation_jobs"] == 2
    records.pop(800001)
    records[800002] = activity.Process(800002, 1, "child", 800001)
    assert jobs.worker_activity()["by_kind"]["simulation_jobs"] == 2
    records.pop(800002)
    assert jobs.worker_activity()["by_kind"]["simulation_jobs"] == 1
    with sqlite3.connect(tmp_path / "jobs.sqlite") as connection:
        connection.execute("UPDATE jobs SET state=? WHERE job_id=?", (jobs.FAILED, "reservation"))
    assert jobs.worker_activity()["by_kind"]["simulation_jobs"] == 0


def test_cad_thread_registry_and_background_thread_scope(isolated, tmp_path):
    from services.mcp import cad_common as cad
    isolated.setattr(cad, "_JOBS", cad._OwnedJobs())
    finish, entered = threading.Event(), threading.Event()
    def body():
        assert rc.owner_key() == (principal()["workspace_key"], "alice")
        assert rc.root_value("CHUDWARE_PROJECT_DIR") == str(tmp_path)
        entered.set()
        finish.wait(3)
    with rc.bind_context(principal()), rc.bind_roots(str(tmp_path)):
        cad._start_job("solve", body)
        assert entered.wait(1)
        assert cad.worker_activity()["by_kind"]["cad_thread_jobs"] == 1
    with rc.bind_context(principal("bob")):
        assert cad.worker_activity()["by_kind"]["cad_thread_jobs"] == 0
    finish.set()
    with rc.bind_context(principal()):
        wait_until(lambda: cad.worker_activity()["by_kind"]["cad_thread_jobs"] == 0)


def test_scrape_reservations_and_cancel_pending_work_remain_owned(isolated):
    from services.browser import queue
    isolated.setattr(queue, "_jobs", [])
    with rc.bind_context(principal()):
        pending = queue.enqueue("example", "query")
        assert queue.worker_activity()["by_kind"] == {"scrape_jobs": 1}
        with rc.bind_context(principal("bob")):
            assert queue.worker_activity()["by_kind"] == {"scrape_jobs": 0}
        queue.cancel_job(pending["id"])
        assert queue.worker_activity()["by_kind"] == {"scrape_jobs": 0}
        running = queue.enqueue("example", "running")
        job = queue._claim_next_job()
        assert job.id == running["id"] and job.started_ts is not None
        assert queue.worker_activity()["by_kind"] == {"scrape_jobs": 1}
        assert not queue.cancel_job(job.id)["ok"]
        # A cancellation/terminal label must never hide the still-owned writer.
        for status in (queue.JobStatus.CANCELLED, queue.JobStatus.ERROR, queue.JobStatus.DONE):
            with queue._lock:
                job.status = status
            assert queue.worker_activity()["by_kind"] == {"scrape_jobs": 1}
        with queue._lock:
            job.finished_ts = time.time()
        assert queue.worker_activity()["by_kind"] == {"scrape_jobs": 0}


def test_actual_scrape_executor_is_idle_only_after_job_publication(isolated):
    from services.browser import adapters, queue
    isolated.setattr(activity, "_REGISTRIES", (queue.__name__,))
    isolated.setattr(queue, "_jobs", [])
    isolated.setattr(queue, "_workers", [])
    isolated.setattr(queue, "_worker_started", False)
    entered, finish, terminal, publish = (threading.Event() for _ in range(4))
    failures, seen = [], {}

    class StopWorkers(BaseException):
        pass

    class OwnedCondition(threading.Condition):
        waiting = 0
        stopped = False

        def wait(self, timeout=None):
            if self.stopped:
                raise StopWorkers()
            self.waiting += 1
            try:
                result = super().wait(timeout)
                if self.stopped:
                    raise StopWorkers()
                return result
            finally:
                self.waiting -= 1

    condition = OwnedCondition(queue._lock)
    isolated.setattr(queue, "_cond", condition)
    worker_loop = queue._worker_loop

    def owned_worker(index):
        try:
            worker_loop(index)
        except StopWorkers:
            pass
        except BaseException as exc:
            failures.append(exc)

    isolated.setattr(queue, "_worker_loop", owned_worker)

    class Adapter:
        def search(self, query):
            seen["owner"] = rc.owner_key()
            entered.set()
            assert finish.wait(5)
            return [{"ok": True}]

        def acquire(self, hit):
            return {"ok": True, "artifact_path": "completed.step+"}

    isolated.setattr(adapters, "get_adapter", lambda site: Adapter())
    run_job = queue._run_job

    def publish_later(job):
        run_job(job)
        terminal.set()
        assert publish.wait(5)

    isolated.setattr(queue, "_run_job", publish_later)

    def all_parked():
        with condition:
            return condition.waiting == queue._POOL_SIZE

    try:
        queue.ensure_worker_started()
        wait_until(all_parked)
        assert len(queue._workers) == queue._POOL_SIZE
        assert all(thread.is_alive() for thread in queue._workers)
        assert activity.snapshot() == {"ok": True, "active": 0, "by_kind": {}, "complete": True}
        with rc.bind_context(principal()):
            queue.enqueue("example", "detached")
        assert entered.wait(1)
        with rc.bind_context(principal()):
            assert activity.snapshot()["by_kind"] == {"scrape_jobs": 1}
        with rc.bind_context(principal("bob")):
            assert activity.snapshot()["active"] == 0
        assert seen["owner"] == (principal()["workspace_key"], "alice")
        finish.set()
        assert terminal.wait(1)
        with rc.bind_context(principal()):
            assert queue._jobs[0].status == queue.JobStatus.DONE
            assert activity.snapshot()["by_kind"] == {"scrape_jobs": 1}
            publish.set()
            wait_until(lambda: queue.worker_activity()["by_kind"]["scrape_jobs"] == 0)
        wait_until(all_parked)
        assert activity.snapshot() == {"ok": True, "active": 0, "by_kind": {}, "complete": True}
        assert all(thread.is_alive() for thread in queue._workers)
        assert not failures
    finally:
        finish.set()
        publish.set()
        with condition:
            condition.stopped = True
            condition.notify_all()
        for thread in queue._workers:
            thread.join(2)
        assert not any(thread.is_alive() for thread in queue._workers)


def test_live_plm_timeout_retains_coroutine_until_it_really_finishes(isolated):
    from services.plm import live_client
    manager = live_client.LiveClientManager()
    isolated.setattr(live_client, "_MANAGER", manager)
    entered = threading.Event()
    finish = threading.Event()
    async def write_later():
        entered.set()
        while not finish.is_set():
            await asyncio.sleep(0.005)
    try:
        with pytest.raises(live_client.LiveClientError):
            manager._submit(write_later(), timeout=0.02)
        assert entered.wait(1)
        assert live_client.worker_activity()["by_kind"]["plm_live"] == 1
        finish.set()
        wait_until(lambda: live_client.worker_activity()["by_kind"]["plm_live"] == 0)
        manager._clients[("project", "remote", "item")] = SimpleNamespace()
        assert live_client.worker_activity()["by_kind"]["plm_live"] == 1
        manager._clients.clear()
    finally:
        finish.set()
        manager.stop()


def test_generic_detached_writer_reservation_preserves_scope_and_actual_lifetime(isolated, tmp_path):
    entered, finish = threading.Event(), threading.Event()
    def write_later():
        assert rc.owner_key() == (principal()["workspace_key"], "alice")
        assert rc.root_value("CHUDWARE_PROJECT_DIR") == str(tmp_path)
        entered.set()
        finish.wait(3)
    with rc.bind_context(principal()), rc.bind_roots(str(tmp_path)):
        thread = activity.start_background_thread(write_later, name="writer")
        assert entered.wait(1)
        assert activity._background_count() == 1
    with rc.bind_context(principal("bob")):
        assert activity._background_count() == 0
    finish.set()
    thread.join(2)
    with rc.bind_context(principal()):
        assert activity._background_count() == 0


def test_worker_dispatch_binds_actual_census_identity_and_rejects_spoof(isolated):
    from services import worker_main
    from services.mcp import cad_common as cad
    isolated.setattr(activity, "_REGISTRIES", (cad.__name__,))
    isolated.setattr(cad, "_JOBS", cad._OwnedJobs())
    isolated.setattr(rc, "process_owner_key", rc.owner_key)
    isolated.setattr(activity, "_launcher_ancestors", lambda records: {os.getpid()})
    with rc.bind_context(principal()):
        cad._JOBS["owned"] = {"status": "running"}
    result = worker_main.dispatch_request({"method": "worker_activity", "params": {},
                                           "ctx": principal()}, lambda event: None)
    assert result["complete"] and result["by_kind"] == {"cad_thread_jobs": 1}
    foreign = worker_main.dispatch_request({"method": "worker_activity", "params": {},
                                            "ctx": principal("bob")}, lambda event: None)
    assert foreign == {"ok": True, "active": 0, "by_kind": {}, "complete": True}
    with pytest.raises(ValueError, match="cell_refused"):
        worker_main.dispatch_request({"method": "worker_activity", "params": {},
                                      "ctx": {**principal(), "actor_hex": "0" * 32}}, lambda e: None)
    assert rc.owner_key() is None


def test_background_registry_lock_contention_returns_unknown_without_blocking(isolated):
    from services.piping import worker_rpc as piping
    from services.parts import hub
    isolated.setattr(piping, "_JOBS", {"busy": {"state": "running"}})
    isolated.setattr(piping, "_THREADS", {})
    reservation = threading.Thread(target=lambda: None)
    isolated.setattr(hub, "_LIVE_THREADS", {reservation: None})
    isolated.setattr(activity, "_REGISTRIES", (piping.__name__, hub.__name__))
    returned = threading.Event()
    output = {}
    def query():
        output["result"] = activity.snapshot()
        returned.set()
    with piping._LOCK:
        census = threading.Thread(target=query)
        census.start()
        try:
            assert returned.wait(1), "census waited behind a live publisher"
            assert output["result"] == {
                "ok": False, "active": 1, "by_kind": {"parts_hub_search": 1},
                "complete": False, "reason": "registry_probe_failed"}
        finally:
            census.join(1)


def test_sdk_partial_state_is_retained_while_stt_query_is_still_pending(isolated):
    client = SdkState()
    client.state["hasPendingAsyncWork"] = True
    original_request = client._request
    finish = threading.Event()
    entered = threading.Event()
    def request(method):
        if method == "stt_status":
            entered.set()
            finish.wait(3)
        return original_request(method)
    client._request = request
    host = install_host(isolated, client)
    try:
        result = activity.snapshot()
        assert entered.is_set()
        assert not result["complete"] and result["active"] == 1
        assert result["by_kind"] == {"sdk_runtime": 1}
        assert client.closed == 0
    finally:
        finish.set()
        for probe in tuple(host._activity_probes.values()):
            assert probe.done.wait(2)


def test_cell_sdk_identity_tolerates_title_change_but_never_exempts_children(isolated):
    ctx = principal()
    isolated.setenv("CHUDWARE_CELL", "1")
    isolated.setenv("CHUDWARE_ACTOR_SANDBOX", "1")
    isolated.setenv("CHUDWARE_WORKSPACE_KEY", ctx["workspace_key"])
    isolated.setenv("CHUDWARE_ACTOR_KEY", ctx["actor_key"])
    isolated.setenv("CHUDWARE_ACTOR_HEX", ctx["actor_hex"])
    command = ("/opt/chudware/bin/chud", "--mode", "rpc")
    init = ("/usr/bin/python3", "-I", "-S",
            "/opt/chudware/bin/chudware-actor-init", "--", *command)
    records = {
        os.getpid(): activity.Process(os.getpid(), 1, "worker"),
        1: activity.Process(1, 0, "outer-init"),
        1001: activity.Process(1001, os.getpid(), "wrapper"),
        1002: activity.Process(1002, 1001, "sdk-init"),
        1003: activity.Process(1003, 1002, "sdk"),
    }
    argv = {1001: ("/usr/bin/bwrap", "--", *init), 1002: init, 1003: ("chud",)}
    executables = {1001: "/usr/bin/bwrap", 1002: "/usr/bin/python3", 1003: command[0]}
    isolated.setattr(activity, "_process_snapshot", lambda: records)
    isolated.setattr(activity, "_argv", lambda pid: argv[pid])
    isolated.setattr(activity, "_same_executable", lambda pid, path: executables[pid] == path)
    isolated.setattr(activity, "_launcher_ancestors", lambda records: {os.getpid(), 1})
    client = SdkState()
    client._process = SimpleNamespace(pid=1001, poll=lambda: None)
    client._chudware_activity_identity = (1001, "wrapper")
    client._chudware_activity_sdk_argv = command
    client._chudware_activity_sandboxed = True
    with rc.bind_context(ctx):
        install_host(isolated, client)
        client._chudware_activity_sdk_identity = activity.capture_sdk_identity(client)
        assert client._chudware_activity_sdk_identity == (1003, "sdk")
        assert activity.snapshot() == {"ok": True, "active": 0, "by_kind": {}, "complete": True}
        records[1004] = activity.Process(1004, 1003, "background")
        argv[1004] = ("chud",)
        executables[1004] = command[0]
        result = activity.snapshot()
        assert not result["complete"] and result["by_kind"] == {"unclassified_process": 1}
        records.pop(1004)
        records[1003] = activity.Process(1003, 1002, "reused-pid")
        assert not activity.snapshot()["complete"]


def test_ambiguous_sdk_executable_children_are_not_attested(isolated):
    command = ("/opt/chudware/bin/chud", "--mode", "rpc")
    init = ("/usr/bin/python3", "-I", "-S",
            "/opt/chudware/bin/chudware-actor-init", "--", *command)
    records = {1: activity.Process(1, 0, "wrapper"),
               2: activity.Process(2, 1, "init"),
               3: activity.Process(3, 2, "sdk"),
               4: activity.Process(4, 2, "unowned-sdk")}
    argv = {1: ("/usr/bin/bwrap", "--", *init), 2: init, 3: ("chud",), 4: ("chud",)}
    executables = {1: "/usr/bin/bwrap", 2: "/usr/bin/python3", 3: command[0], 4: command[0]}
    isolated.setattr(activity, "_argv", lambda pid: argv[pid])
    isolated.setattr(activity, "_same_executable", lambda pid, path: executables[pid] == path)
    client = SimpleNamespace(_chudware_activity_identity=(1, "wrapper"),
                             _chudware_activity_sdk_argv=command,
                             _chudware_activity_sandboxed=True)
    with pytest.raises(RuntimeError, match="sdk_process_unattested"):
        activity._sdk_process_chain(client, records)


def test_desktop_sdk_containment_stays_unknown_after_last_session_is_disposed(isolated):
    activity.note_sdk_launch(sandboxed=True)
    assert activity.snapshot()["complete"]
    activity.note_sdk_launch(sandboxed=False)
    assert activity.snapshot() == {
        "ok": False, "active": 0, "by_kind": {}, "complete": False,
        "reason": "process_ownership_unattested"}


def test_reveng_reservations_include_unstarted_threads_and_release_on_actual_finish(isolated):
    from services.mcp import reveng_common as reveng
    isolated.setattr(reveng, "_MISSION_THREADS", {})
    finish = threading.Event()
    thread = threading.Thread(target=lambda: finish.wait(3))
    with rc.bind_context(principal()):
        key = rc.scoped_key("session")
        reveng._MISSION_THREADS[key] = ("run", None)
        assert reveng.worker_activity()["by_kind"]["reveng_missions"] == 1
        reveng._MISSION_THREADS[key] = ("run", thread)
        assert reveng.worker_activity()["by_kind"]["reveng_missions"] == 1
        with rc.bind_context(principal("bob")):
            assert reveng.worker_activity()["by_kind"]["reveng_missions"] == 0
        try:
            thread.start()
            assert reveng.worker_activity()["by_kind"]["reveng_missions"] == 1
        finally:
            finish.set()
            thread.join(2)
        assert reveng.worker_activity()["by_kind"]["reveng_missions"] == 0


def test_started_browser_coroutine_is_counted_after_its_rpc_times_out(isolated):
    from services.browser import embed
    manager = embed.EmbedManager()
    isolated.setattr(embed, "_MANAGER", manager)
    entered, finish = threading.Event(), threading.Event()
    async def write_later():
        entered.set()
        while not finish.is_set():
            await asyncio.sleep(0.005)
    try:
        with pytest.raises(TimeoutError):
            manager._call(write_later(), timeout=0.02)
        assert entered.wait(1)
        assert embed.worker_activity()["by_kind"]["browser_calls"] == 1
        finish.set()
        wait_until(lambda: embed.worker_activity()["by_kind"]["browser_calls"] == 0)
    finally:
        finish.set()
        manager.stop_all()


def test_failed_registered_thread_start_releases_only_its_reservation(isolated):
    original_thread = threading.Thread
    class FailedThread(original_thread):
        def start(self):
            raise RuntimeError("thread capacity exhausted")
    isolated.setattr(activity.threading, "Thread", FailedThread)
    with pytest.raises(RuntimeError, match="thread capacity exhausted"):
        activity.start_background_thread(lambda: None, name="not-started")
    assert activity._background_count() == 0


@pytest.mark.skipif(sys.platform != "win32", reason="requires real Win32 process handles")
def test_native_windows_census_tracks_descendants_and_kernel_identity():
    # Separate interpreter: no SDK state or imported registry from other tests.
    code = r'''
import os
import subprocess
import sys
from services import worker_activity as activity

empty = activity.snapshot()
assert empty["complete"] and empty["ok"] and empty["active"] == 0, empty
child_code = r"""
import subprocess, sys
grandchild = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
try:
    print(grandchild.pid, flush=True)
    sys.stdin.read()
finally:
    grandchild.terminate()
    grandchild.wait()
"""
child = subprocess.Popen([sys.executable, "-c", child_code],
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
try:
    grandchild_pid = int(child.stdout.readline())
    identity = activity.process_identity(child)
    assert identity and identity[0] == child.pid
    assert activity.process_identity(child) == identity
    records = activity._process_snapshot()
    assert records[child.pid].start == identity[1]
    assert records[grandchild_pid].parent == child.pid
    busy = activity.snapshot()
    assert not busy["complete"] and not busy["ok"], busy
    assert busy["active"] == 2 and busy["by_kind"]["unclassified_process"] == 2, busy
finally:
    child.communicate(timeout=15)
assert child.returncode == 0
assert activity.process_identity(child) is None
settled = activity.snapshot()
assert settled["complete"] and settled["ok"] and settled["active"] == 0, settled
activity.note_sdk_launch(sandboxed=False)
unattested = activity.snapshot()
assert not unattested["complete"] and "process_ownership_unattested" in unattested["reason"], unattested
'''
    environment = {key: value for key, value in os.environ.items()
                   if key not in {"CHUDWARE_CELL", "CHUDWARE_ACTOR_SANDBOX"}}
    subprocess.run([sys.executable, "-c", code], check=True, env=environment, timeout=60)
