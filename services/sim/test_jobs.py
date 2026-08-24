from __future__ import annotations

import json
import os
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


# --------------------------------------------------------------- stage allowlist / payloads

def _static_study(geometry) -> dict:
    return {"step_path": str(geometry),
            "material": {"E_mpa": 200000.0, "nu": 0.3, "yield_mpa": 250.0},
            "fixtures": [{"nodes": [0, 1, 2]}],
            "loads": [{"type": "force", "vector": [0.0, 0.0, -1.0],
                       "faces": [{"nodes": [3, 4, 5]}]}]}


@pytest.fixture
def geometry(isolated_registry):
    path = isolated_registry / "part.step"
    path.write_text("ISO-10303-21;", encoding="utf-8")
    return path


@pytest.fixture
def live_backend(monkeypatch):
    """A backend that EXISTS and rejects the empty dispatch probe, like the real one."""
    monkeypatch.setattr(cb, "available", lambda: True)
    monkeypatch.setattr(cb, "call", lambda method, params: {
        "ok": False, "error": "study is required"})


def test_payload_cannot_name_a_function_outside_the_stage_allowlist(isolated_registry):
    """A payload names a STAGE and a METHOD from closed lists — never an import path."""
    with pytest.raises(ValueError, match="unknown stage"):
        J.start("test", "os.system", {})
    with pytest.raises(ValueError, match="core_rpc method must be one of"):
        J.start("test", "core_rpc", {"method": "subprocess.run", "params": {}})
    with J._db() as connection:
        assert connection.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0


def test_doc_shaped_method_must_carry_its_exported_geometry(
        isolated_registry, geometry, live_backend):
    """sim_run_flow tessellates a DOCUMENT, and a child shares none — so the payload has to
    bring the solid as a file or it can only ever fail in the worker."""
    params = {"velocity": 10.0, "aoa": 0.0, "fluid": "air",
              "cells": 48, "n_steps": 500}
    with pytest.raises(ValueError, match="open_step"):
        J.start("cfd_flow", "core_rpc", {"method": "sim_run_flow", "params": params})
    with pytest.raises(ValueError, match="unavailable"):
        J.start("cfd_flow", "core_rpc", {"method": "sim_run_flow", "params": params,
                                         "open_step": str(isolated_registry / "gone.step")})
    with J._db() as connection:
        assert connection.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0


def test_flow_and_openfoam_payloads_reject_unphysical_setups(
        isolated_registry, geometry, live_backend):
    base = {"method": "sim_run_flow", "open_step": str(geometry),
            "params": {"velocity": 10.0, "cells": 48, "n_steps": 500, "fluid": "air"}}
    for bad, match in ((("velocity", 0.0), "velocity must be > 0"),
                       (("cells", 8), r"cells must be an integer >= 16"),
                       (("fluid", "mercury"), "air.*water")):
        payload = {**base, "params": {**base["params"], bad[0]: bad[1]}}
        with pytest.raises(ValueError, match=match):
            J.start("cfd_flow", "core_rpc", payload)
    foam = {"stl_path": str(geometry), "speed_mps": 10.0, "preset": "coarse",
            "fluid": {"nu": 1.5e-5, "rho": 1.225}}
    with pytest.raises(ValueError, match="preset must be one of"):
        J.start("cfd_openfoam", "openfoam_tunnel", {**foam, "preset": "ludicrous"})
    with pytest.raises(ValueError, match="speed_mps must be > 0"):
        J.start("cfd_openfoam", "openfoam_tunnel", {**foam, "speed_mps": 0.0})
    with pytest.raises(ValueError, match="fluid.rho"):
        J.start("cfd_openfoam", "openfoam_tunnel", {**foam, "fluid": {"nu": 1e-5, "rho": 0.0}})


def test_a_present_solver_that_rejects_the_probe_still_queues(
        isolated_registry, geometry, monkeypatch):
    """The dispatch probe sends EMPTY params on purpose. A solver that is present answers
    by rejecting them — that must not be read as "this build has no solver", which would
    refuse every job the solver could actually have run."""
    monkeypatch.setattr(cb, "available", lambda: True)

    def rejecting_call(method, params):
        raise RuntimeError("sim_run_static: study is required")

    monkeypatch.setattr(cb, "call", rejecting_call)
    monkeypatch.setattr(J.subprocess, "Popen", _FakePopen)
    job = J.start("fea_static", "core_rpc",
                  {"method": "sim_run_static", "params": {"study": _static_study(geometry)}})
    assert job["state"] == J.RUNNING

    monkeypatch.setattr(cb, "call", lambda method, params: {
        "ok": False, "error": "unknown method sim_run_static"})
    with pytest.raises(ValueError, match="cannot dispatch"):
        J.start("fea_static", "core_rpc",
                {"method": "sim_run_static", "params": {"study": _static_study(geometry)}})


class _FakePopen:
    """A launcher stand-in: start() must still record a pid and spool the payload."""

    def __init__(self, *_args, **_kwargs):
        self.pid = os.getpid()

    def poll(self):
        return None

    def wait(self, timeout=None):  # noqa: ANN001
        return 0


def test_progress_token_resolves_into_the_jobs_own_spool(
        isolated_registry, geometry, live_backend, monkeypatch):
    """A solver that streams progress to a file of its own cannot be handed that path by
    the caller — the job id does not exist yet, and a caller-minted temp path is
    unreachable to a LATER process reconnecting to the job."""
    monkeypatch.setattr(J.subprocess, "Popen", _FakePopen)
    job = J.start("cfd_flow", "core_rpc", {
        "method": "sim_run_flow", "open_step": str(geometry),
        "progress_file": J.PROGRESS_TOKEN,
        "params": {"velocity": 10.0, "cells": 48, "n_steps": 500, "fluid": "air",
                   "progress_path": J.PROGRESS_TOKEN}})
    spooled = json.loads(
        (J.job_dir(job["job_id"]) / "payload.json").read_text(encoding="utf-8"))["payload"]
    expected = str(J.job_dir(job["job_id"]) / "progress.json")
    assert spooled["progress_file"] == expected
    assert spooled["params"]["progress_path"] == expected
    # Only an EXACT token is substituted, so a payload cannot forge a path by prefixing it.
    assert J._resolve_tokens({"a": J.PROGRESS_TOKEN + "/etc"}, "/spool") == {
        "a": J.PROGRESS_TOKEN + "/etc"}


# --------------------------------------------------------------- collect exactly once

def test_collect_is_claimed_once_and_replayed_thereafter(isolated_registry):
    """Collect writes the solved study into the document, so it must run once however many
    times (and from however many processes) the finished job is polled."""
    job_id = "sim-collect-once"
    _insert_job(job_id, state=J.DONE)
    assert J.collected(job_id) is None
    assert J.claim_collect(job_id) is True
    assert J.claim_collect(job_id) is False      # a second poller must not collect
    J.save_collected(job_id, {"ok": True, "min_fos": 2.5})
    assert J.collected(job_id) == {"ok": True, "min_fos": 2.5}


def test_released_claim_lets_a_later_poll_retry_the_collect(isolated_registry):
    """A collect that FAILED must not leave the job stranded behind a lock file forever."""
    job_id = "sim-collect-retry"
    _insert_job(job_id, state=J.DONE)
    assert J.claim_collect(job_id) is True
    J.release_collect(job_id)
    assert J.claim_collect(job_id) is True
    assert J.collected(job_id) is None


def test_unserializable_collect_result_is_returned_not_lost(isolated_registry):
    """A reply that cannot be spooled is still handed to the caller who computed it —
    losing the answer is worse than losing the ability to replay it."""
    job_id = "sim-collect-nan"
    _insert_job(job_id, state=J.DONE)
    value = {"ok": True, "peak": float("inf")}
    assert J.save_collected(job_id, value) is value
    assert J.collected(job_id) is None
