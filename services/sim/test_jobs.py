from __future__ import annotations

import json
import signal
import time

import pytest

from services.mcp import core_bridge as cb
from services.sim import job_worker
from services.sim import jobs as J


@pytest.fixture
def isolated_registry(tmp_path, monkeypatch):
    monkeypatch.setenv("CHUDWARE_SIM_JOBS_DIR", str(tmp_path))
    J._LOCAL_PROCS.clear()
    return tmp_path


def _insert_job(job_id: str, *, state: str = J.RUNNING,
                cancel_requested: int = 0, pid: int = 999999):
    directory = J.job_dir(job_id)
    directory.mkdir(parents=True, exist_ok=True)
    now = time.time()
    with J._db() as connection:
        connection.execute(
            "INSERT INTO jobs(job_id,kind,stage,state,pid,pgid,proc_key,created_at,"
            "started_at,cancel_requested,meta) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (job_id, "test", "selftest_sleep", state, pid, pid, "key", now, now,
             cancel_requested, "{}"))


def test_malformed_job_is_rejected_before_registry_insert(isolated_registry):
    with pytest.raises(ValueError, match="finite"):
        J.start("test", "selftest_sleep", {"seconds": float("nan"), "steps": 1})
    with pytest.raises(ValueError, match="study"):
        J.start("fea_static", "core_rpc",
                {"method": "sim_run_static", "params": {}})
    geometry = isolated_registry / "part.step"
    geometry.write_text("ISO-10303-21;", encoding="utf-8")
    with pytest.raises(ValueError, match="material"):
        J.start("fea_static", "core_rpc", {
            "method": "sim_run_static",
            "params": {"study": {
                "step_path": str(geometry), "fixtures": [{}], "loads": [{}],
            }},
        })
    with J._db() as connection:
        assert connection.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0


def test_unavailable_core_backend_fails_before_queue(
        isolated_registry, monkeypatch):
    geometry = isolated_registry / "part.step"
    geometry.write_text("ISO-10303-21;", encoding="utf-8")
    payload = {
        "method": "sim_run_static",
        "params": {"study": {
            "step_path": str(geometry),
            "material": {"E_mpa": 200000.0, "nu": 0.3, "yield_mpa": 250.0},
            "fixtures": [{"nodes": [0, 1, 2]}],
            "loads": [{"type": "force", "vector": [0.0, 0.0, -1.0],
                       "faces": [{"nodes": [3, 4, 5]}]}],
        }},
    }
    monkeypatch.setattr(cb, "available", lambda: False)
    with pytest.raises(ValueError, match="backend is unavailable"):
        J.start("fea_static", "core_rpc", payload)
    with J._db() as connection:
        assert connection.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0


def test_stage_ok_false_is_a_failed_job_not_done(isolated_registry):
    job_id = "sim-false-result"
    _insert_job(job_id)
    (J.job_dir(job_id) / "result.json").write_text(json.dumps({
        "result": {"ok": False, "error": "backend unavailable"},
        "done_at": time.time(),
    }), encoding="utf-8")

    status = J.status(job_id)
    assert status["state"] == J.FAILED
    assert status["ok"] is False
    assert "backend unavailable" in status["error"]
    assert J.raw_result(job_id) is None


def test_stale_done_row_without_result_artifact_becomes_failed(isolated_registry):
    job_id = "sim-stale-done"
    _insert_job(job_id, state=J.DONE)
    status = J.status(job_id)
    assert status["state"] == J.FAILED
    assert status["ok"] is False
    assert "result artifact" in status["error"]


@pytest.mark.parametrize("stored_state", [J.RUNNING, J.DONE])
def test_cancel_requested_job_cannot_adopt_late_success(
        isolated_registry, stored_state):
    job_id = f"sim-cancel-race-{stored_state}"
    _insert_job(job_id, state=stored_state, cancel_requested=1)
    (J.job_dir(job_id) / "result.json").write_text(json.dumps({
        "result": {"ok": True, "value": 42},
        "done_at": time.time(),
    }), encoding="utf-8")

    status = J.status(job_id)
    assert status["state"] == J.CANCELLED
    assert status["ok"] is False
    assert J.raw_result(job_id) is None


def test_job_worker_promotes_stage_ok_false_to_failure(isolated_registry, monkeypatch):
    job_id = "sim-worker-failure"
    directory = J.job_dir(job_id)
    directory.mkdir(parents=True)
    (directory / "payload.json").write_text(json.dumps({
        "stage": "unit_failure",
        "kind": "test",
        "payload": {},
    }), encoding="utf-8")
    monkeypatch.setitem(J.STAGES, "unit_failure",
                        lambda _payload, _emit: {"ok": False, "error": "solver rejected"})

    old_term = signal.getsignal(signal.SIGTERM)
    old_int = signal.getsignal(signal.SIGINT)
    try:
        assert job_worker.main(["job_worker", job_id]) == 1
    finally:
        signal.signal(signal.SIGTERM, old_term)
        signal.signal(signal.SIGINT, old_int)
    finished = json.loads((directory / "result.json").read_text(encoding="utf-8"))
    assert finished["result"]["ok"] is False
    assert finished["error"] == "solver rejected"


def test_cancel_kills_surviving_process_group_after_leader_exits(
        isolated_registry, monkeypatch):
    job_id = "sim-group-survivor"
    _insert_job(job_id, pid=424242)
    first_liveness_probe = True
    group_alive = True
    signals = []

    def fake_pid_alive(_pid, _key=None):
        nonlocal first_liveness_probe
        if first_liveness_probe:
            first_liveness_probe = False
            return True
        return False

    def fake_group_alive(_pgid):
        return group_alive

    def fake_killpg(_pgid, sent_signal):
        nonlocal group_alive
        signals.append(sent_signal)
        group_alive = False

    monkeypatch.setattr(J, "pid_alive", fake_pid_alive)
    monkeypatch.setattr(J, "_group_alive", fake_group_alive)
    monkeypatch.setattr(J.os, "killpg", fake_killpg)

    result = J.cancel(job_id, timeout_s=0.01)
    assert signals == [signal.SIGTERM]
    assert result["state"] == J.CANCELLED
    assert result["terminated"] is True
    assert result["pid_alive_after"] is False
    assert result["group_alive_after"] is False
