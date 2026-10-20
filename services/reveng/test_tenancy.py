"""Real same-process actor ownership and hostile reconstruction handle regressions."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import zipfile

import pytest

from services.agent import request_context as context
from services.mcp import reveng_common as common
from services.reveng import gaussian_jobs, worker_rpc


def _actor(actor: str) -> dict:
    return {"cell": True, "workspace_key": "team:org/team", "actor_key": actor,
            "actor_hex": hashlib.sha256(actor.encode()).hexdigest()[:32],
            "right": "editor", "epoch": 1}


@pytest.fixture
def private_state(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "private"))
    monkeypatch.setenv("CHUDWARE_REVENG_DIR", str(tmp_path / "caller-selected"))
    monkeypatch.setenv("CHUDWARE_REVENG_RUNS_DIR", str(tmp_path / "caller-runs"))
    return tmp_path


def _session(handle: str):
    session = common._make_session(handle, handle, None)
    common._save_session(session)
    return session, common._session_path(handle)



@pytest.mark.parametrize("method", ["start_capture_session", "stop_capture_session"])
def test_capture_worker_entry_points_refuse_cells_before_tool_access(
        method, private_state, monkeypatch):
    def unexpected_tool_access(_method):
        pytest.fail("capture refusal must precede tool and registry access")

    monkeypatch.setattr(worker_rpc, "_tool_fn", unexpected_tool_access)
    with context.bind_context(_actor("alice")):
        result = worker_rpc.REVENG_WORKER_METHODS[method](
            {"session_id": "foreign-or-absent"}, lambda _event: None)
    assert result == {"ok": False, "reason": "cell_refused", "error": "cell_refused"}


def test_delegated_splat_stop_audits_delegate_without_session_content(private_state, monkeypatch):
    delegate = {"actor_key": "manager", "right": "manager", "action": "cancel"}
    with context.bind_context(_actor("alice")):
        _, root = _session("owned-session")

    def stop(session_id):
        assert session_id == "owned-session"
        return {"ok": True, "status": "cancelled", "private_path": str(root),
                "dataset": {"private": "session-content"}}

    monkeypatch.setattr(worker_rpc, "_tool_fn", lambda method: stop)
    events = []
    with context.bind_context({**_actor("alice"), "control_delegate": delegate}):
        result = worker_rpc.REVENG_WORKER_METHODS["reveng_cancel_splat"](
            {"session_id": "owned-session"}, events.append)
        assert result == {"ok": True, "session_id": "owned-session", "status": "cancelled",
                          "reason": "manager_stop", "control_delegate": delegate}
        forbidden = worker_rpc.REVENG_WORKER_METHODS["get_reveng_session"](
            {"session_id": "owned-session"}, events.append)
        assert forbidden["reason"] == "forbidden_right"
    assert events[0]["reason"] == "manager_stop"
    audit = json.loads((root / "control-events.jsonl").read_text())
    assert audit["control_delegate"] == delegate
    assert "private_path" not in audit and "dataset" not in audit

def _training_process(handle: str, root: Path):
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                               start_new_session=True)
    record = {"ok": True, "session_id": handle, "run_id": "same-run-handle",
              "status": "training", "stage": "training", "_owner": context.owner_key(),
              "process": gaussian_jobs._identity(process.pid), "pgid": process.pid,
              "owner": gaussian_jobs._identity(os.getpid())}
    gaussian_jobs._atomic(root / "splat_job.json", record)
    key = context.scoped_key(record["run_id"])
    gaussian_jobs._CHILDREN[key] = (root, process)
    return process, key


@pytest.mark.skipif(sys.platform != "linux", reason="Linux process-group identity")
def test_two_actors_cannot_read_finish_cancel_or_close_foreign_sessions(private_state):
    processes = []
    keys = []
    try:
        with context.bind_context(_actor("alice")):
            alice, alice_root = _session("shared-handle")
            _session("alice-only")
            alice_process, key = _training_process("shared-handle", alice_root)
            processes.append(alice_process)
            keys.append(key)
            assert alice_root.name != "shared-handle"
            assert alice["_owner"] == context.owner_key()
            assert common._reserve_mission("alice-only", "same-mission-handle") is None
        with context.bind_context(_actor("bob")):
            assert common._list_sessions() == []
            for handle in ("alice-only", "absent"):
                with pytest.raises(KeyError) as error:
                    common._load_session(handle)
                assert error.value.args == ("not_found",)
                for operation in (gaussian_jobs.status, gaussian_jobs.finish,
                                  gaussian_jobs.cancel):
                    assert operation(handle)["error"] == "not_found"
                assert gaussian_jobs.crop(handle, None)["error"] == "not_found"
            with pytest.raises(KeyError, match="not_found"):
                common._save_session(alice)
            bob, bob_root = _session("shared-handle")
            _session("alice-only")
            assert common._reserve_mission("alice-only", "same-mission-handle") is None
            bob_process, key = _training_process("shared-handle", bob_root)
            processes.append(bob_process)
            keys.append(key)
            assert bob_root != alice_root
            assert common._load_session("shared-handle")["_owner"] == list(context.owner_key())
            assert {s["session_id"] for s in common._list_sessions()} == {"shared-handle", "alice-only"}
            assert gaussian_jobs.cancel("shared-handle")["status"] == "cancelled"
            bob_process.wait(timeout=2)
            assert alice_process.poll() is None
            gaussian_jobs.shutdown()  # Actor close must not tear down Alice's supervisor.
            assert alice_process.poll() is None
            common._release_mission("alice-only", "same-mission-handle")
        with context.bind_context(_actor("alice")):
            assert common._mission_running("alice-only") == "same-mission-handle"
            common._release_mission("alice-only", "same-mission-handle")
            assert gaussian_jobs.status("shared-handle")["status"] == "training"
            assert gaussian_jobs.finish("shared-handle")["status"] == "finalizing"
            assert gaussian_jobs.cancel("shared-handle")["status"] == "cancelled"
            alice_process.wait(timeout=2)
        assert not (private_state / "caller-selected").exists()
        assert not (private_state / "caller-runs").exists()
    finally:
        for process in processes:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=2)
        for key in keys:
            gaussian_jobs._CHILDREN.pop(key, None)
        for actor in ("alice", "bob"):
            with context.bind_context(_actor(actor)):
                common._release_mission("alice-only", "same-mission-handle")


@pytest.mark.parametrize("handle", ["../outside", "../../outside", "/etc/passwd", ".", "..",
                                   "", "..\\outside", "evil\x00handle", None, False, 7, [], {}])
def test_malicious_session_handles_never_become_paths(private_state, handle):
    outside = private_state / "outside"
    outside.write_text("private sentinel")
    with context.bind_context(_actor("alice")):
        for operation in (common._session_path, common._load_session):
            with pytest.raises(KeyError) as error:
                operation(handle)
            assert error.value.args == ("not_found",)
        with pytest.raises(KeyError, match="not_found"):
            common._make_session(handle, "attacker", None)
        assert gaussian_jobs.cancel(handle)["error"] == "not_found"
        expected = {"ok": False, "error": "not_found", "reason": "not_found",
                    "code": "not_found"}
        for operation in (gaussian_jobs.status, gaussian_jobs.finish, gaussian_jobs.cancel):
            assert operation(handle) == operation("absent") == expected
        for method in ("get_reveng_session", "reveng_add_images", "reveng_cancel_splat",
                       "reveng_finish_splat", "reveng_set_scale"):
            handler = worker_rpc.REVENG_WORKER_METHODS[method]
            assert handler({"session_id": handle}, lambda *_: None) == expected
    assert outside.read_text() == "private sentinel"
    assert not (private_state / "caller-selected").exists()


def test_manifest_owner_is_checked_before_artifacts_or_mutations(private_state):
    with context.bind_context(_actor("alice")):
        session, root = _session("owned")
        session["_owner"] = ("team:org/team", "bob")
        (root / "session.json").write_text(json.dumps(session))
        assert common._list_sessions() == []
        with pytest.raises(KeyError, match="not_found"):
            common._session_image_paths("owned")
        assert gaussian_jobs.finish("owned")["error"] == "not_found"
        assert json.loads((root / "session.json").read_text())["_owner"][1] == "bob"


def test_rpc_reads_and_mutations_share_the_absent_not_found_envelope(private_state):
    with context.bind_context(_actor("alice")):
        _session("alice-only")
    with context.bind_context(_actor("bob")):
        for method in ("get_reveng_session", "reveng_add_images", "reveng_cancel_splat",
                       "reveng_finish_splat", "reveng_set_scale"):
            handler = worker_rpc.REVENG_WORKER_METHODS[method]
            foreign = handler({"session_id": "alice-only"}, lambda *_: None)
            absent = handler({"session_id": "absent"}, lambda *_: None)
            assert foreign == absent == {"ok": False, "error": "not_found",
                                        "reason": "not_found", "code": "not_found"}


def test_mission_thread_rebinds_actor_and_explicit_roots(private_state, monkeypatch):
    completed = threading.Event()
    observed = {}

    def invoke():
        observed["owner"] = context.owner_key()
        observed["project"] = context.root_value("CHUDWARE_PROJECT_DIR")
        observed["save"] = context.root_value("CHUDWARE_PROJECT_SAVE_DIR")
        observed["state"] = common._reveng_dir()
        return {"status": "done"}

    def publish(_record, _result, _graph):
        observed["publish_owner"] = context.owner_key()
        completed.set()

    monkeypatch.setattr(common, "_apply_run_result", publish)
    with context.bind_context(_actor("alice")), context.bind_roots("/work/project", "/work/save"):
        _, root = _session("thread-owner")
        common._spawn_mission("thread-owner", "run", invoke, {}, {})
    assert completed.wait(3)
    assert observed == {"owner": ("team:org/team", "alice"),
                        "publish_owner": ("team:org/team", "alice"),
                        "project": "/work/project", "save": "/work/save", "state": root.parent}


def test_desktop_legacy_session_directory_remains_usable(tmp_path, monkeypatch):
    monkeypatch.setenv("CHUDWARE_REVENG_DIR", str(tmp_path))
    with context.bind_context(None):
        _, root = _session("re_legacy")
        assert root == tmp_path / "re_legacy"
        assert common._load_session("re_legacy")["session_id"] == "re_legacy"
        assert [row["session_id"] for row in common._list_sessions()] == ["re_legacy"]
        for handle in ("../outside", "/etc/passwd", "..\\outside"):
            assert common._load_session(handle) is None


def test_run_sidecars_and_progress_callbacks_are_actor_owned(private_state):
    from services.dag.storage import create_run

    with context.bind_context(_actor("alice")):
        run = create_run(handle="shared-run-handle")
        _session("owned")
        record = {"run_id": run.run_id, "session_id": "owned", "_owner": context.owner_key(),
                  "status": "paused", "gate": {"node_id": "gate"}}
        common._write_run_record(record)
        assert common._run_record_path(run.run_id) == run.root / "reveng.json"
        emit = common.make_reveng_emit(run.run_id, "owned", {"node": ("sparse", 30)})
        progress = common.make_reveng_stage_emitter(run.run_id, "owned")
        emit("dag.node.started", {"node_id": "node"})
        before = (run.root / "events.jsonl").read_bytes()
    with context.bind_context(_actor("bob")):
        bob_run = create_run(handle="shared-run-handle")
        for handle in (run.run_id, "absent"):
            with pytest.raises(KeyError, match="not_found"):
                common._read_run_record(handle)
            with pytest.raises(KeyError, match="not_found"):
                common._reject_reveng_gate(handle)
        with pytest.raises(KeyError, match="not_found"):
            common._write_run_record(record)
        emit("dag.node.done", {"node_id": "node"})
        progress("dense", 70, "foreign publication")
        assert not (bob_run.root / "events.jsonl").exists()
        assert (run.root / "events.jsonl").read_bytes() == before
    with context.bind_context(_actor("alice")):
        assert common._reject_reveng_gate(run.run_id)["status"] == "rejected"
        assert common._read_run_record(run.run_id)["_owner"] == list(context.owner_key())


def test_desktop_session_symlink_cannot_escape_store(tmp_path, monkeypatch):
    store = tmp_path / "store"
    outside = tmp_path / "outside"
    store.mkdir()
    outside.mkdir()
    (outside / "session.json").write_text(json.dumps({"session_id": "link"}))
    (store / "link").symlink_to(outside, target_is_directory=True)
    monkeypatch.setenv("CHUDWARE_REVENG_DIR", str(store))
    with context.bind_context(None):
        assert common._load_session("link") is None
        with pytest.raises(KeyError, match="not_found"):
            common._session_path("link")


def test_export_and_scan_import_refuse_foreign_absent_and_malformed_before_mutation(
        private_state):
    with context.bind_context(_actor("alice")):
        _, alice_root = _session("alice-owned")
        (alice_root / "alice-only.txt").write_text("alice private content")
    sentinel = private_state / "existing.zip"
    sentinel.write_bytes(b"destination must not be truncated")
    missing = private_state / "never-created" / "nested" / "bundle.zip"
    scan = private_state / "scan.ply"
    scan.write_text("ply\nformat ascii 1.0\nend_header\n")
    expected = {"ok": False, "error": "not_found", "reason": "not_found",
                "code": "not_found"}
    before = {path.relative_to(alice_root): path.read_bytes()
              for path in alice_root.rglob("*") if path.is_file()}
    with context.bind_context(_actor("bob")):
        for handle in ("alice-owned", "absent", "../outside", "/etc/passwd",
                       ".", "..", "", "..\\outside", "evil\x00handle",
                       None, False, 7, [], {}):
            for method, params in (
                ("export_reveng_bundle", {"dest": str(sentinel)}),
                ("export_reveng_bundle", {"dest": str(missing)}),
                ("import_scan_mesh", {"path": str(scan)}),
            ):
                supplied = {"session_id": handle, **params}
                result = worker_rpc.REVENG_WORKER_METHODS[method](
                    supplied, lambda _event: None)
                direct = json.loads(worker_rpc._tool_fn(method)(**supplied))
                assert result == direct == expected
                assert sentinel.read_bytes() == b"destination must not be truncated"
                assert not missing.parent.parent.exists()
    assert before == {path.relative_to(alice_root): path.read_bytes()
                      for path in alice_root.rglob("*") if path.is_file()}


def test_owned_export_reads_back_only_owned_session_and_imported_mesh(private_state):
    scans = {}
    for actor in ("alice", "bob"):
        scan = private_state / f"{actor}.ply"
        scans[actor] = f"ply\nformat ascii 1.0\ncomment {actor}-private-mesh\nend_header\n".encode()
        scan.write_bytes(scans[actor])
        with context.bind_context(_actor(actor)):
            _session(f"{actor}-owned")
            imported = worker_rpc.REVENG_WORKER_METHODS["import_scan_mesh"](
                {"session_id": f"{actor}-owned", "path": str(scan)},
                lambda _event: None)
            assert imported["ok"] is True, imported
    destination = private_state / "selected-delivery" / "alice.zip"
    with context.bind_context(_actor("alice")):
        result = worker_rpc.REVENG_WORKER_METHODS["export_reveng_bundle"](
            {"session_id": "alice-owned", "dest": str(destination)},
            lambda _event: None)
        assert result == {"ok": True, "path": str(destination)}
    with zipfile.ZipFile(destination) as archive:
        assert archive.testzip() is None
        session = json.loads(archive.read("session.json"))
        assert session["session_id"] == "alice-owned"
        assert session["_owner"] == ["team:org/team", "alice"]
        assert archive.read("mesh/alice.ply") == scans["alice"]
        assert all(b"bob-private-mesh" not in archive.read(name)
                   for name in archive.namelist())


@pytest.mark.parametrize("destination_kind", ["absolute", "relative", "home", "symlink"])
def test_desktop_bundle_export_keeps_user_selected_destinations(
        tmp_path, monkeypatch, destination_kind):
    monkeypatch.setenv("CHUDWARE_REVENG_DIR", str(tmp_path / "store"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    selected = tmp_path / "selected-delivery"
    if destination_kind == "relative":
        raw = "selected-delivery/desktop.zip"
    elif destination_kind == "home":
        raw = "~/selected-delivery/desktop.zip"
        selected = tmp_path / "home" / "selected-delivery"
    elif destination_kind == "symlink":
        selected.mkdir()
        (tmp_path / "selected-link").symlink_to(selected, target_is_directory=True)
        raw = str(tmp_path / "selected-link" / "desktop.zip")
    else:
        raw = str(selected / "desktop.zip")
    with context.bind_context(None):
        _, root = _session("desktop-owned")
        (root / "human-selected.txt").write_bytes(b"desktop archive content")
        result = worker_rpc.REVENG_WORKER_METHODS["export_reveng_bundle"](
            {"session_id": "desktop-owned", "dest": raw}, lambda _event: None)
    assert result["ok"] is True, result
    with zipfile.ZipFile(selected / "desktop.zip") as archive:
        assert archive.testzip() is None
        assert archive.read("human-selected.txt") == b"desktop archive content"
        assert json.loads(archive.read("session.json"))["_owner"] is None
