"""Independent writer groups serialize batches without losing committed geometry."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import subprocess
import tempfile
import threading

import pytest

from services.mcp import core_bridge as cb, private_hostd
from services.mcp.daemon_bridge import DaemonClient


@pytest.fixture(scope="module")
def same_doc_daemon():
    # Always own this daemon, even when the runner has an explicit socket override.
    with tempfile.TemporaryDirectory(prefix="cw-writers-") as directory:
        started = private_hostd.spawn_private_daemon(Path(directory))
        if started is None:
            pytest.skip(private_hostd.last_failure)
        proc, socket_path, key = started
        try:
            with pytest.MonkeyPatch.context() as env:
                env.setenv("CHUDWARE_HOSTD_SOCKET", socket_path)
                env.setenv("CHUDWARE_INSTANCE_KEY", key)
                yield
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()


@pytest.fixture()
def writers(same_doc_daemon):
    a = DaemonClient(writer_group="same-doc-a")
    b = DaemonClient(writer_group="same-doc-b")
    try:
        a.connect()
        b.connect()
        doc = a.call("doc_new", {"kind": "part", "name": "SequentialWriters"})["doc_id"]
        attached = b.call("attach_doc", {"doc_id": doc, "mode": "rw"})
        assert attached.get("ok") is True, attached
        yield a, b, doc
    finally:
        b.close()
        a.close()


def _box_ops(index):
    return [{"op": "add_feature", "feature": {
        "type": "box", "name": f"WriterBox{index}",
        "params": {"length": 5, "width": 5, "height": 5,
                   "at": [20 * index, 0, 0]}, "refs": {},
    }}]


def test_independent_writers_apply_interleaved_atomic_batches(writers):
    a, b, doc = writers
    count = 4
    rendezvous = threading.Barrier(2)

    def apply(connection, offset):
        revisions = []
        for i in range(count):
            rendezvous.wait(timeout=60)
            result = connection.call("doc_apply_ops", {
                "doc_id": doc, "base_revision": -1, "ops": _box_ops(offset + i),
            })
            assert not result.get("_is_error"), result
            assert "rejected_tree" not in result, result
            assert all(s.get("ok") for s in result["rebuild"]["feature_status"]), result
            revisions.append(result["revision"])
        assert all(left < right for left, right in zip(revisions, revisions[1:]))
        return revisions

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(apply, a, 0)
        second = pool.submit(apply, b, count)
        revisions = first.result(timeout=180) + second.result(timeout=180)
    assert sorted(revisions) == list(range(1, 2 * count + 1))
    before = a.call("doc_tree", {"doc_id": doc})
    assert before["revision"] == 2 * count
    assert len(before["tree"]["features"]) == 2 * count
    assert {f["name"] for f in before["tree"]["features"]} == {
        f"WriterBox{i}" for i in range(2 * count)
    }

    # A box has no round edges: normal compile validation rejects this atomically.
    rejected = b.call("doc_apply_ops", {
        "doc_id": doc, "base_revision": -1,
        "ops": [{"op": "add_feature", "feature": {
            "type": "fillet", "name": "InvalidRoundEdgeFillet",
            "params": {"radius": 3}, "refs": {"edges": {"of": "edge", "surf": "round"}},
        }}],
    })
    assert not rejected.get("_is_error"), rejected
    assert "rejected_tree" in rejected, rejected
    assert any(not s.get("ok") for s in rejected["rebuild"]["feature_status"]), rejected
    after = a.call("doc_tree", {"doc_id": doc})
    assert after["revision"] == before["revision"]
    assert after["tree"] == before["tree"]


def test_raw_hostd_serializes_implicit_batches_and_honors_explicit_cas(writers):
    a, b, doc = writers
    committed = a.call("doc_apply_ops", {"doc_id": doc, "ops": _box_ops(0)})
    assert committed["revision"] == 1, committed
    second = b.call("doc_apply_ops", {"doc_id": doc, "ops": _box_ops(1)})
    assert not second.get("_is_error") and second["revision"] == 2, second
    stale = a.call("doc_apply_ops", {
        "doc_id": doc, "base_revision": 1, "ops": _box_ops(2),
    })
    assert stale.get("_is_error") is True, stale
    assert stale.get("code") == "stale_revision", stale
    after = a.call("doc_tree", {"doc_id": doc})
    assert after["revision"] == 2
    assert {f["name"] for f in after["tree"]["features"]} == {"WriterBox0", "WriterBox1"}
    current = a.call("doc_apply_ops", {
        "doc_id": doc, "base_revision": 2, "ops": _box_ops(2),
    })
    assert not current.get("_is_error") and current["revision"] == 3, current


def test_push_pull_refuses_foreign_edit_after_own_revert(writers):
    a, b, doc = writers
    built = a.call("doc_apply_ops", {
        "doc_id": doc, "base_revision": -1, "ops": _box_ops(0)})
    checkpoint = a.call("doc_checkpoint", {"doc_id": doc})
    reverted = a.call("doc_revert", {
        "doc_id": doc, "checkpoint": checkpoint["checkpoint"],
        "expect_revision": built["revision"]})
    assert not reverted.get("_is_error"), reverted
    foreign = b.call("doc_apply_ops", {
        "doc_id": doc, "base_revision": -1, "ops": _box_ops(1)})
    assert not foreign.get("_is_error"), foreign
    before = a.call("doc_tree", {"doc_id": doc})
    for distance, commit in ((0, False), (2, False), (2, True)):
        refused = a.call("doc_push_pull", {
            "doc_id": doc, "body_id": 1, "topo_id": 1, "distance": distance,
            "commit": commit, "expect_revision": reverted["revision"]})
        assert refused.get("code") == "stale_revision", refused
        assert refused.get("detail", {}).get("current_revision") == before["revision"]
        after = a.call("doc_tree", {"doc_id": doc})
        assert after["revision"] == before["revision"]
        assert after["tree"] == before["tree"]
    # Both the explicitly current and legacy unguarded no-op forms remain usable.
    params = {"doc_id": doc, "body_id": 1, "topo_id": 1, "distance": 0}
    assert not a.call("doc_push_pull", {**params, "expect_revision": before["revision"]}).get("_is_error")
    assert not a.call("doc_push_pull", params).get("_is_error")


def test_solve_mates_refuses_foreign_edit_after_own_revert(writers, tmp_path):
    a, b, source = writers
    built = a.call("doc_apply_ops", {
        "doc_id": source, "base_revision": -1, "ops": _box_ops(0)})
    assert not built.get("_is_error"), built
    path = str(tmp_path / "component.step+")
    assert not a.call("doc_save", {"doc_id": source, "path": path}).get("_is_error")
    doc = a.call("doc_new", {"kind": "assembly", "name": "GuardedPreviewAssembly"})["doc_id"]
    checkpoint = a.call("doc_checkpoint", {"doc_id": doc})
    reverted = a.call("doc_revert", {
        "doc_id": doc, "checkpoint": checkpoint["checkpoint"],
        "expect_revision": checkpoint["revision"]})
    assert not reverted.get("_is_error"), reverted
    foreign = b.call("doc_apply_ops", {
        "doc_id": doc, "base_revision": -1, "ops": [
            {"op": "asm_insert_component", "source": {"path": path}, "name": "Peer"}]})
    assert not foreign.get("_is_error"), foreign
    before = a.call("doc_tree", {"doc_id": doc})
    for drag in (None, {"component": "k1", "target": [100, 0, 0]}):
        refused = a.call("doc_solve_mates", {
            "doc_id": doc, "drag": drag, "expect_revision": reverted["revision"]})
        assert refused.get("code") == "stale_revision", refused
        assert refused.get("detail", {}).get("current_revision") == before["revision"]
        after = a.call("doc_tree", {"doc_id": doc})
        assert after["revision"] == before["revision"]
        assert after["tree"] == before["tree"]
    current = a.call("doc_solve_mates", {"doc_id": doc, "expect_revision": before["revision"]})
    assert not current.get("_is_error"), current
    legacy = a.call("doc_solve_mates", {"doc_id": doc})
    assert not legacy.get("_is_error"), legacy


def test_sketch_preview_guards_peer_edits_and_newer_editor(writers):
    a, b, doc = writers
    built = a.call("doc_apply_ops", {"doc_id": doc, "base_revision": -1, "ops": [
        {"op": "add_feature", "feature": {
            "type": "sketch", "name": "Circle", "refs": {"plane": "plane.top"},
            "params": {"entities": [{"id": "e1", "type": "circle",
                                      "center": [0, 0], "radius": 10}],
                       "constraints": [{"id": "c1", "type": "fix", "refs": ["e1.center"]}]}}}]})
    sketch = built["id_map"]["$0"]
    first = a.call("sketch_enter", {"doc_id": doc, "sketch": sketch})
    token = first["session_id"]
    assert token > 0 and first["revision"] == built["revision"]
    dragged = a.call("sketch_drag", {"doc_id": doc, "sketch": sketch,
                                      "session_id": token, "handle": "e1.rim",
                                      "xy": [25, 0]})
    assert dragged["revision"] == first["revision"]
    foreign = b.call("doc_apply_ops", {"doc_id": doc, "base_revision": -1, "ops": [
        {"op": "sketch_edit_entity", "sketch": sketch, "id": "e1", "params": {"radius": 15}}]})
    assert not foreign.get("_is_error"), foreign
    before = a.call("doc_tree", {"doc_id": doc})
    for method in ("sketch_drag_commit", "sketch_exit"):
        stale = a.call(method, {"doc_id": doc, "sketch": sketch,
                                "session_id": token, "expect_revision": dragged["revision"]})
        assert stale.get("code") == "stale_revision", stale
        after = a.call("doc_tree", {"doc_id": doc})
        assert after["revision"] == before["revision"] and after["tree"] == before["tree"]

    newer = b.call("sketch_enter", {"doc_id": doc, "sketch": sketch})
    assert newer["session_id"] != token
    discarded = a.call("sketch_exit", {"doc_id": doc, "sketch": sketch,
                                        "session_id": token, "discard_active": True})
    assert discarded.get("discarded") is False, discarded
    for method, extra in (("sketch_solve", {}), ("sketch_drag", {
            "handle": "e1.rim", "xy": [30, 0]}), ("sketch_drag_commit", {}),
            ("sketch_exit", {})):
        refused = a.call(method, {"doc_id": doc, "sketch": sketch, "session_id": token, **extra})
        assert refused.get("code") == "sketch_session_changed", refused
    fresh = b.call("sketch_solve", {"doc_id": doc, "sketch": sketch,
                                     "session_id": newer["session_id"]})
    assert fresh["revision"] == before["revision"]
    sample = b.call("sketch_drag", {"doc_id": doc, "sketch": sketch,
                                      "session_id": newer["session_id"],
                                      "handle": "e1.rim", "xy": [20, 0]})
    assert sample["revision"] == fresh["revision"]
    committed = b.call("sketch_drag_commit", {"doc_id": doc, "sketch": sketch,
                                               "session_id": newer["session_id"],
                                               "expect_revision": sample["revision"]})
    assert not committed.get("_is_error"), committed
    assert committed["revision"] > before["revision"]
    done = b.call("sketch_exit", {"doc_id": doc, "sketch": sketch,
                                  "session_id": newer["session_id"], "discard_active": True})
    assert done.get("discarded") is True, done


def test_sketch_discard_does_not_clear_different_editor_and_survives_deletion(writers):
    a, b, doc = writers
    def sketch(name):
        return {"op": "add_feature", "feature": {
            "type": "sketch", "name": name, "refs": {"plane": "plane.top"},
            "params": {"entities": [{"id": "e1", "type": "circle",
                                      "center": [0, 0], "radius": 10}], "constraints": []}}}

    built = a.call("doc_apply_ops", {"doc_id": doc, "base_revision": -1,
                                     "ops": [sketch("First"), sketch("Second")]})
    one, two = built["id_map"]["$0"], built["id_map"]["$1"]
    old = a.call("sketch_enter", {"doc_id": doc, "sketch": one})
    newer = b.call("sketch_enter", {"doc_id": doc, "sketch": two})
    result = a.call("sketch_exit", {"doc_id": doc, "sketch": one,
                                     "session_id": old["session_id"], "discard_active": True})
    assert result.get("discarded") is False, result
    solved = b.call("sketch_solve", {"doc_id": doc, "sketch": two,
                                      "session_id": newer["session_id"]})
    assert not solved.get("_is_error"), solved
    deleted = a.call("doc_apply_ops", {"doc_id": doc, "base_revision": -1,
                                       "ops": [{"op": "delete_feature", "id": two}]})
    assert not deleted.get("_is_error"), deleted
    before = b.call("doc_tree", {"doc_id": doc})
    done = b.call("sketch_exit", {"doc_id": doc, "sketch": two,
                                  "session_id": newer["session_id"], "discard_active": True})
    assert done.get("discarded") is True, done
    after = b.call("doc_tree", {"doc_id": doc})
    assert after["tree"] == before["tree"] and after["revision"] == before["revision"]


@pytest.mark.parametrize("checkpoint,applied_revision", [
    (None, 5), ({"checkpoint": "c"}, 5),
    ({"checkpoint": "c", "revision": "4"}, 5),
    ({"checkpoint": "c", "revision": 4}, None),
    ({"checkpoint": "c", "revision": 4}, 4),
    ({"checkpoint": "c", "revision": 4}, 6),
])
def test_rollback_skips_batches_not_owned_since_checkpoint(monkeypatch, checkpoint, applied_revision):
    calls = []
    monkeypatch.setattr(cb, "call", lambda *args: calls.append(args))
    assert cb.revert_own_batch("part", checkpoint, applied_revision) is False
    assert not calls


def test_rollback_sends_live_revision_guard(monkeypatch):
    calls = []
    monkeypatch.setattr(cb, "call", lambda *args: calls.append(args) or {"revision": 6})
    checkpoint = {"checkpoint": "c", "revision": 4}
    assert cb.revert_own_batch("part", checkpoint, 5) is True
    assert calls == [("doc_revert", {
        "doc_id": "part", "checkpoint": "c", "expect_revision": 5,
    })]
    assert checkpoint == {"checkpoint": "c", "revision": 4}


@pytest.mark.parametrize("code", ["stale_revision", "invalid_op", "engine_connection_lost"])
def test_rollback_refusal_never_claims_success(monkeypatch, code):
    def refuse(method, params):
        raise cb.CoreError("refused", code=code)

    monkeypatch.setattr(cb, "call", refuse)
    checkpoint = {"checkpoint": "c", "revision": 4}
    if code == "engine_connection_lost":
        with pytest.raises(cb.CoreError) as error:
            cb.revert_own_batch("part", checkpoint, 5)
        assert error.value.code == code
    else:
        assert cb.revert_own_batch("part", checkpoint, 5) is False


@pytest.mark.parametrize("rejected_tree,reverted", [(True, False), (False, True), (False, False)])
def test_sketch_failure_reports_only_actual_rollback(monkeypatch, rejected_tree, reverted):
    from services.mcp import sketch3d_tools

    result = {"revision": 5, "rebuild": {
        "feature_status": [{"id": "bad", "ok": False, "error": "invalid geometry"}],
    }}
    if rejected_tree:
        result["rejected_tree"] = {"features": [{"id": "bad"}]}
    calls = []

    def call(method, params):
        if method == "doc_checkpoint":
            return {"checkpoint": "c", "revision": 4}
        assert method == "doc_apply_ops"
        return result

    def rollback(*args):
        calls.append(args)
        return reverted

    monkeypatch.setattr(sketch3d_tools.cb, "call", call)
    monkeypatch.setattr(sketch3d_tools.cb, "revert_own_batch", rollback)
    state = "doc rolled back" if rejected_tree or reverted else "failed features left in the tree"
    with pytest.raises(ValueError, match=state):
        sketch3d_tools._apply_ops("part", [])
    assert calls == ([] if rejected_tree else [("part", {"checkpoint": "c", "revision": 4}, 5)])


@pytest.mark.parametrize("rejected", [False, True])
def test_calibration_cleanup_deletes_only_its_probe(monkeypatch, rejected):
    from services.mcp import recon_tools

    features = ["base"]
    calls = []

    def call(method, params):
        calls.append((method, params))
        if method == "doc_face_wires":
            return {"entities": [{"p1": [0, 0], "p2": [20, 20]}]}
        assert method == "doc_apply_ops"
        op = params["ops"][0]
        if op["op"] == "add_feature":
            # Another writer commits while calibration is using its temporary probe.
            features.append("other-writer")
            result = {"id_map": {"$0": "probe"}, "rebuild": {
                "feature_status": [{"id": "probe", "ok": not rejected}],
            }}
            if rejected:
                result["rejected_tree"] = {"features": [{"id": "probe"}]}
            else:
                features.append("probe")
            return result
        assert op == {"op": "delete_feature", "id": "probe"}
        features.remove("probe")
        return {"revision": 3}

    monkeypatch.setattr(recon_tools.cb, "call", call)
    monkeypatch.setattr(recon_tools, "_query_faces", lambda doc: [])
    assert recon_tools._calibrate_face_frame("part", {"of": "face"}, [0, 0, 0]) is None
    assert features == ["base", "other-writer"]
    deletes = [params["ops"] for method, params in calls
               if method == "doc_apply_ops" and params["ops"][0]["op"] == "delete_feature"]
    assert deletes == ([] if rejected else [[{"op": "delete_feature", "id": "probe"}]])
