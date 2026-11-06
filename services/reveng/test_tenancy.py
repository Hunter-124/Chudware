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


def test_cell_not_found_tool_envelopes_remain_minimal(private_state):
    with context.bind_context(_actor("alice")):
        result = worker_rpc._envelope({"ok": False, "error": "not_found",
                                       "hint": "do not expose detail"}, "any_method")
        assert result == {"ok": False, "error": "not_found", "reason": "not_found",
                          "code": "not_found"}


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


def test_owned_session_rejects_paths_outside_cell_workspace_before_mutation(private_state):
    scan = private_state / "outside.ply"
    scan.write_bytes(b"outside scan")
    destination = private_state / "outside.zip"
    destination.write_bytes(b"do not replace")
    missing = private_state / "not-created" / "bundle.zip"
    with context.bind_context(_actor("alice")):
        _, root = _session("owned")
        for method, params in (
            ("import_scan_mesh", {"path": str(scan)}),
            ("export_reveng_bundle", {"dest": str(destination)}),
            ("export_reveng_bundle", {"dest": str(missing)}),
        ):
            supplied = {"session_id": "owned", **params}
            result = worker_rpc.REVENG_WORKER_METHODS[method](supplied, lambda _: None)
            direct = json.loads(worker_rpc._tool_fn(method)(**supplied))
            assert result["ok"] is False and result["error"] == "cell_refused"
            assert direct == {"ok": False, "error": "cell_refused", "code": "cell_refused"}
        assert not (root / "mesh").exists()
    assert destination.read_bytes() == b"do not replace"
    assert not missing.parent.exists()


def test_owned_export_reads_back_only_owned_session_and_imported_mesh(private_state, monkeypatch):
    # This in-process fixture has no /work mount. Emulate a confined workspace
    # without weakening the production cell_path boundary.
    workspace = private_state.resolve()

    def fixture_cell_path(path, *, private=False):
        resolved = Path(path).resolve()
        if resolved != workspace and workspace not in resolved.parents:
            raise ValueError("cell_refused")
        return resolved

    monkeypatch.setattr(context, "cell_path", fixture_cell_path)
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
        assert session["artifacts"]["mesh_path"] == "mesh/alice.ply"
        assert session["artifacts"]["raw_mesh_path"] == "mesh/alice.ply"
        assert archive.read(session["artifacts"]["mesh_path"]) == scans["alice"]
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


def test_worker_publishes_owned_session_artifact_references(private_state, monkeypatch):
    with context.bind_context(_actor("alice")):
        session, root = _session("owned")
        splat = root / "splat" / "cloud.ply"
        splat.parent.mkdir()
        splat.write_bytes(b"ply\nowned Gaussian")
        mesh = root / "mesh.ply"
        mesh.write_bytes(b"ply\nmesh")
        sparse = root / "sparse"
        sparse.mkdir()
        (sparse / "points3D.bin").write_bytes(b"sparse points")
        session["artifacts"].update(splat_path=str(splat), mesh_path=str(mesh),
                                    sparse_dir=str(sparse))
        common._save_session(session)
        response = worker_rpc.REVENG_WORKER_METHODS["get_reveng_session"](
            {"session_id": "owned"}, lambda _: None)
        assert response["ok"], response
        refs = response["artifact_refs"]
        assert refs["splat"]["kind"] == "gaussian_ply"
        assert refs["splat"]["size"] == splat.stat().st_size
        assert refs["mesh"]["kind"] == "mesh_ply"
        assert refs["sparse_points"]["kind"] == "sparse_points"
        assert len(refs["splat"]["id"]) == 64
        assert str(root) not in json.dumps(refs)
        grant = json.loads((common._reveng_dir() / "artifact_refs" /
                            refs["splat"]["id"]).read_text())
        assert grant["path"] == splat.relative_to(common._reveng_dir()).as_posix()
        repeat = worker_rpc.REVENG_WORKER_METHODS["get_reveng_session"](
            {"session_id": "owned"}, lambda _: None)
        assert repeat["artifact_refs"]["splat"]["id"] == refs["splat"]["id"]

        preview = root / "preview" / "surface.stl"
        preview.parent.mkdir()
        preview.write_bytes(b"binary STL")
        monkeypatch.setattr(worker_rpc, "_tool_fn",
                            lambda _method: lambda session_id: {
                                "ok": True, "mesh_preview_path": str(preview)})
        result = worker_rpc.REVENG_WORKER_METHODS["reveng_sparse_preview"](
            {"session_id": "owned"}, lambda _: None)
        assert result["artifact_refs"]["mesh_preview"]["kind"] == "binary_stl"
        assert result["artifact_refs"]["mesh_preview"]["size"] == preview.stat().st_size

        other = private_state / "not-session.ply"
        other.write_bytes(b"private")
        assert worker_rpc._artifact_ref("owned", str(other), "gaussian_ply") is None

    with context.bind_context(_actor("bob")):
        with pytest.raises(KeyError, match="not_found"):
            worker_rpc._artifact_ref("owned", str(splat), "gaussian_ply")


def test_cell_sparse_preview_is_bounded_and_transports_artifact(private_state, monkeypatch):
    from services.mcp import reveng_session_tools

    class Capture:
        def tool(self, *args, **kwargs):
            def register(fn):
                if fn.__name__ == "reveng_sparse_preview":
                    self.preview = fn
                return fn
            return register

    capture = Capture()
    reveng_session_tools.register_reveng_session_tools(capture)
    monkeypatch.setattr(worker_rpc, "_tool_fn", lambda _method: capture.preview)
    with context.bind_context(_actor("alice")):
        session, root = _session("owned")
        sparse = root / "sparse"
        sparse.mkdir()
        (sparse / "cameras.txt").write_text("1 PINHOLE 100 80 50 50 50 40\n")
        (sparse / "images.txt").write_text("1 1 0 0 0 0 0 0 1 frame.jpg\n\n")
        points_path = sparse / "points3D.txt"
        points_path.write_text("".join(
            f"{i + 1} {i + 1} 2 3 255 128 0 0.1\n" for i in range(5001)))
        session["artifacts"]["sparse_dir"] = str(sparse)
        common._save_session(session)
        response = worker_rpc.REVENG_WORKER_METHODS["reveng_sparse_preview"](
            {"session_id": "owned"}, lambda _: None)
        assert response["ok"]
        assert response["total_points"] == 5001
        assert len(response["points"]) == 5000
        assert all(len(point) == 6 for point in response["points"])
        assert "points_native" not in response
        assert response["units"] == "reconstruction"
        assert response["artifact_refs"]["sparse_points"]["kind"] == "sparse_points"
        assert response["artifact_refs"]["sparse_points"]["size"] == points_path.stat().st_size


@pytest.mark.parametrize("has_old_dataset", [False, True])
def test_shared_dataset_gauge_change_invalidates_current_model_not_snapshots(
        private_state, has_old_dataset):
    with context.bind_context(_actor("alice")):
        session, root = _session("owned")
        images = root / "images"
        images.mkdir()
        source = images / "frame.jpg"
        source.write_bytes(b"frame")
        dataset = root / "new_dataset"
        sparse = dataset / "sparse"
        undistorted = dataset / "images"
        sparse.mkdir(parents=True)
        undistorted.mkdir()
        batch = root / "batch.json"
        batch.write_text(json.dumps({"frame.jpg": str(source)}))
        snapshot = root / "published.step"
        snapshot.write_bytes(b"published part")
        if has_old_dataset:
            session["sfm_dataset"] = {"dataset_dir": str(root / "old_dataset")}
        session["artifacts"].update(mesh_path="old-mesh", raw_mesh_path="old-raw",
                                    fits_path="old-fits", doc_id="old-doc",
                                    step_path="old-step", cadquery_path="old-script")
        session["result"] = {"state": "built"}
        session["stages"].update(model="done", review="done", deliver="done")
        common._save_session(session)
        gaussian_jobs._persist_shared_output(root, {"session_id": "owned"}, {
            "dataset_dir": str(dataset), "sparse_dir": str(sparse),
            "images_dir": str(undistorted), "batch_image_map_path": str(batch),
        }, None)
        updated = common._load_session("owned", merge_gaussian=False)
        assert updated["sfm_dataset"]["dataset_dir"] == str(dataset)
        assert updated["artifacts"]["sparse_dir"] == str(sparse)
        assert updated["artifacts"]["doc_id"] is None
        assert all(key not in updated["artifacts"] for key in
                   ("mesh_path", "raw_mesh_path", "fits_path", "step_path", "cadquery_path"))
        assert "result" not in updated
        assert all(updated["stages"][stage] == "idle" for stage in
                   ("model", "review", "deliver"))
        assert updated["stages"]["scale"] == "pending"
        assert snapshot.read_bytes() == b"published part"


def test_cell_splat_conversion_receives_session_handle_not_workspace_path(private_state, monkeypatch):
    with context.bind_context(_actor("alice")):
        _session("owned")
        received = {}

        def convert(path, out_format, out_path):
            received.update(path=path, out_format=out_format, out_path=out_path)
            return {"ok": True, "path": out_path}

        monkeypatch.setattr(worker_rpc, "_tool_fn", lambda _method: convert)
        result = worker_rpc.REVENG_WORKER_METHODS["reveng_convert_splat"](
            {"session_id": "owned", "out_format": "splat", "out_path": "/work/out.splat"},
            lambda _: None)
        assert result["ok"]
        assert received == {"path": "owned", "out_format": "splat", "out_path": "/work/out.splat"}
