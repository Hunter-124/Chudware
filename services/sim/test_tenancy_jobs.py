"""Same-process actor control isolation with real cancellable process groups."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time

import pytest

from services.agent import request_context as context
from services.sim import job_worker, jobs


def _actor(actor):
    return {"cell": True, "workspace_key": "team:org/team", "actor_key": actor,
            "actor_hex": hashlib.sha256(actor.encode()).hexdigest()[:32],
            "right": "editor", "epoch": 1}


@pytest.fixture
def private_state(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "private"))
    monkeypatch.setenv("CHUDWARE_SIM_JOBS_DIR", str(tmp_path / "caller-selected"))
    return tmp_path


def _running_job(handle):
    directory = Path(tempfile.mkdtemp(prefix="fixture-", dir=jobs.jobs_dir()))
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                               start_new_session=True)
    now = time.time()
    with jobs._db() as connection:
        connection.execute(
            "INSERT INTO jobs(job_id,kind,stage,state,pid,pgid,proc_key,created_at,started_at,"
            "meta,owner,storage_dir) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (handle, "test", "selftest_sleep", jobs.RUNNING, process.pid, process.pid,
             jobs._proc_key(process.pid), now, now, "{}", jobs._owner_text(), str(directory)))
    key = context.scoped_key(handle)
    jobs._LOCAL_PROCS[key] = process
    job_worker._Progress(directory, handle).emit({"phase": "test.running", "actor": context.owner_key()[1]})
    return process, directory, key


@pytest.mark.skipif(sys.platform != "linux", reason="Linux process-group identity")
def test_two_actors_cannot_list_read_collect_or_cancel_foreign_jobs(private_state):
    processes = []
    keys = []
    try:
        with context.bind_context(_actor("alice")):
            alice_process, alice_directory, key = _running_job("shared-handle")
            processes.append(alice_process)
            keys.append(key)
            unique_process, _, key = _running_job("alice-only")
            processes.append(unique_process)
            keys.append(key)
            assert jobs._row("shared-handle")["owner"] == jobs._owner_text()
        with context.bind_context(_actor("bob")):
            assert jobs.list_jobs() == []
            for handle in ("alice-only", "absent"):
                assert jobs.status(handle)["error"] == "not_found"
                assert jobs.cancel(handle)["error"] == "not_found"
                for reader in (jobs.job_dir, jobs.partial, jobs.raw_result, jobs.refusal_result,
                               jobs.terminal_result, jobs.collected, jobs.collect_evidence,
                               jobs.claim_collect, jobs.release_collect):
                    with pytest.raises(KeyError) as error:
                        reader(handle)
                    assert error.value.args == ("not_found",)
                for writer in (jobs.save_collected, jobs.save_collect_evidence):
                    with pytest.raises(KeyError, match="not_found"):
                        writer(handle, {"ok": True})
                with pytest.raises(KeyError, match="not_found"):
                    jobs._set(handle, cancel_requested=1)
            assert alice_process.poll() is None
            assert unique_process.poll() is None
            bob_process, bob_directory, key = _running_job("shared-handle")
            processes.append(bob_process)
            keys.append(key)
            assert bob_directory != alice_directory
            assert "shared-handle" not in bob_directory.parts
            assert jobs.partial("shared-handle")["actor"] == "bob"
            assert [row["job_id"] for row in jobs.list_jobs()] == ["shared-handle"]
            assert jobs.claim_collect("shared-handle")
            jobs.save_collect_evidence("shared-handle", {"actor": "bob"})
            assert jobs.collect_evidence("shared-handle") == {"actor": "bob"}
            jobs.release_collect("shared-handle")
            result = jobs.cancel("shared-handle", timeout_s=0.3)
            assert result["state"] == jobs.CANCELLED and result["terminated"]
            bob_process.wait(timeout=2)
            assert alice_process.poll() is None
        with context.bind_context(_actor("alice")):
            assert jobs.partial("shared-handle")["actor"] == "alice"
            assert jobs.collected("shared-handle") is None
            assert jobs.collect_evidence("shared-handle") is None
            assert {row["job_id"] for row in jobs.list_jobs()} == {"shared-handle", "alice-only"}
            for handle in ("shared-handle", "alice-only"):
                assert jobs.cancel(handle, timeout_s=0.3)["terminated"]
        assert not (private_state / "caller-selected").exists()
    finally:
        for process in processes:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=2)
        for key in keys:
            jobs._LOCAL_PROCS.pop(key, None)


@pytest.mark.skipif(sys.platform != "linux", reason="Linux process-group identity")
def test_delegated_stop_kills_target_and_audits_without_process_metadata(private_state):
    delegate = {"actor_key": "manager", "right": "manager", "action": "cancel"}
    with context.bind_context(_actor("alice")):
        process, directory, key = _running_job("owned-job")
    try:
        with context.bind_context({**_actor("alice"), "control_delegate": delegate}):
            result = jobs.cancel("owned-job", timeout_s=0.3)
        assert result == {"ok": True, "job_id": "owned-job", "state": jobs.CANCELLED,
                          "terminated": True, "reason": "manager_stop",
                          "control_delegate": delegate}
        process.wait(timeout=2)
        audit = json.loads((directory / "control-events.jsonl").read_text())
        assert audit["kind"] == "run_stopped"
        assert audit["reason"] == "manager_stop"
        assert audit["control_delegate"] == delegate
        with context.bind_context(_actor("alice")):
            metadata = jobs.status("owned-job")["meta"]["last_control_stop"]
            assert metadata == {"reason": "manager_stop", "control_delegate": delegate}
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=2)
        jobs._LOCAL_PROCS.pop(key, None)


@pytest.mark.parametrize("handle", ["../../outside", "/etc/passwd", "..\\outside", "..", "",
                                   "bad\x00id", None, False, 7, [], {}])
def test_unregistered_job_handles_cannot_address_paths(private_state, handle):
    with context.bind_context(_actor("alice")):
        with pytest.raises(KeyError, match="not_found"):
            jobs.job_dir(handle)
        expected = {"ok": False, "state": "unknown", "error": "not_found",
                    "reason": "not_found", "code": "not_found"}
        assert jobs.status(handle) == jobs.status("absent") == expected
        assert jobs.cancel(handle) == jobs.cancel("absent") == expected
        for reader in (jobs.partial, jobs.raw_result, jobs.refusal_result,
                       jobs.terminal_result, jobs.collected, jobs.claim_collect):
            with pytest.raises(KeyError, match="not_found"):
                reader(handle)


def test_worker_stage_rebinds_context_and_roots_from_server_payload(private_state, monkeypatch):
    observed = {}

    def stage(_payload, emit):
        observed["owner"] = context.owner_key()
        observed["project"] = context.root_value("CHUDWARE_PROJECT_DIR")
        observed["save"] = context.root_value("CHUDWARE_PROJECT_SAVE_DIR")
        emit({"phase": "test.done", "owner": context.owner_key()})
        return {"ok": True, "actor": context.owner_key()[1]}

    monkeypatch.setitem(jobs.STAGES, "tenant_stage", stage)
    with context.bind_context(_actor("alice")):
        directory = Path(tempfile.mkdtemp(prefix="fixture-", dir=jobs.jobs_dir()))
        handle = "opaque-job-handle"
        with jobs._db() as connection:
            connection.execute(
                "INSERT INTO jobs(job_id,kind,stage,state,owner,storage_dir) VALUES(?,?,?,?,?,?)",
                (handle, "test", "tenant_stage", jobs.QUEUED, jobs._owner_text(), str(directory)))
        (directory / "payload.json").write_text(json.dumps({
            "stage": "tenant_stage", "kind": "test", "payload": {},
            "ctx": context.current_context(), "roots": {"project": "/work/project", "save": "/work/save"},
        }))
    old_term, old_int = signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGINT)
    try:
        # No request is bound in this thread: the child entry must restore its own.
        assert job_worker.main(["job_worker", handle, "--job-dir", str(directory)]) == 0
    finally:
        signal.signal(signal.SIGTERM, old_term)
        signal.signal(signal.SIGINT, old_int)
    assert observed == {"owner": ("team:org/team", "alice"), "project": "/work/project", "save": "/work/save"}
    with context.bind_context(_actor("alice")):
        assert jobs.status(handle)["state"] == jobs.DONE
        assert jobs.raw_result(handle) == {"ok": True, "actor": "alice"}
    with context.bind_context(_actor("bob")):
        assert jobs.status(handle)["error"] == "not_found"
        with pytest.raises(KeyError, match="not_found"):
            job_worker.main(["job_worker", handle, "--job-dir", str(directory)])


def test_cell_worker_refuses_legacy_unbound_entry(private_state, monkeypatch):
    monkeypatch.setenv("CHUDWARE_CELL", "1")
    with pytest.raises(ValueError, match="cell_refused"):
        job_worker.main(["job_worker", "unknown"])


def test_start_uses_trusted_context_mapping_and_fails_closed_on_sandbox_failure(
        private_state, monkeypatch):
    captured = {}

    def refuse_launch(argv, **kwargs):
        captured["argv"] = argv
        captured["cwd"] = kwargs["cwd"]
        directory = Path(argv[argv.index("--job-dir") + 1])
        captured["directory"] = directory
        captured["spec"] = json.loads((directory / "payload.json").read_text())
        raise FileNotFoundError("sandbox unavailable")

    monkeypatch.setattr(jobs.subprocess, "Popen", refuse_launch)
    with context.bind_context(_actor("alice")), context.bind_roots("/work/project", "/work/save"):
        with pytest.raises(FileNotFoundError, match="sandbox unavailable"):
            jobs.start("test", "selftest_sleep", {"seconds": 1, "steps": 1})
        assert captured["argv"][:4] == [
            "/opt/chudware/bin/chudware-actor-sandbox", context.actor_hex(), "rw", "--"]
        assert captured["spec"]["ctx"] == context.current_context()
        assert captured["spec"]["roots"] == {"project": "/work/project", "save": "/work/save"}
        assert captured["cwd"] == "/work"
        handle = captured["argv"][captured["argv"].index("--job-dir") - 1]
        assert handle not in captured["directory"].parts
        assert not captured["directory"].exists()
        assert jobs.list_jobs() == []
    assert not (private_state / "caller-selected").exists()
