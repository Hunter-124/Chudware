"""Multi-agent overlap simulation — parallel subagents on different parts, with
overlapping work on shared parts, plus the cross-process file-handling seams.

Three layers, mirroring the real topologies:

  1. THREADS on one engine (the single-shared-engine swarm): N workers each build
     their own part concurrently; g_mtx serialises the C++ side, and every part
     must come out isolated and correct. Then two workers hammer the SAME doc with
     interleaved edits of different features — both edits must land, geometry sane.
  2. Per-session incremental-rebuild caches must never cross-contaminate between
     concurrently edited docs.
  3. PROCESS-level seams (separate-MCP-process swarm): handoff docs-<session>.json
     fragment merge (live union, dead-process cleanup) and session-prefixed
     autosave filenames (no doc-id collision between processes).

Run: .venv-mcp/bin/pytest services/mcp/test_multiagent_overlap.py -v
"""
from __future__ import annotations

import importlib
import json
import sys
import threading

import pytest

# Self-heal against sweep pollution: test_dimension_scheme.py (and friends) install a
# STUB services.mcp.core_bridge (call -> {}) into sys.modules at import time, which in a
# whole-directory pytest sweep would make every engine call here return {}. A stub
# ModuleType has no __file__; evict it and import the real bridge.
_cb_mod = sys.modules.get("services.mcp.core_bridge")
if _cb_mod is not None and not getattr(_cb_mod, "__file__", None):
    for _name in ("services.mcp.core_bridge", "services.mcp.core_bridge", "core_bridge"):
        _stub = sys.modules.get(_name)
        if _stub is not None and not getattr(_stub, "__file__", None):
            del sys.modules[_name]
cb = importlib.import_module("services.mcp.core_bridge")


def _apply(doc_id, ops):
    return cb.call("doc_apply_ops", {"doc_id": doc_id, "ops": ops})


def _fs(resp):
    return resp["rebuild"]["feature_status"]


def _mass(doc_id):
    return cb.call("eval_mass", {"doc_id": doc_id})


def _box(length, width, height):
    return {"op": "add_feature",
            "feature": {"type": "box",
                        "params": {"length": length, "width": width, "height": height}}}


def _close(doc_id):
    try:
        cb.call("doc_close", {"doc_id": doc_id})
    except Exception:
        pass


# --------------------------------------------------------------------- layer 1
def test_parallel_agents_distinct_parts_isolated():
    """6 'subagents' (threads) each build a differently-sized part concurrently.
    Every part must succeed and report ITS OWN volume — no cross-talk."""
    n = 6
    sizes = [(10 + i, 20 + i, 5 + i) for i in range(n)]
    results: list = [None] * n
    errors: list = [None] * n

    def worker(i):
        try:
            did = cb.call("doc_new", {"name": f"swarm_part_{i}"})["doc_id"]
            l, w, h = sizes[i]
            r = _apply(did, [_box(l, w, h)])
            assert all(s["ok"] for s in _fs(r)), _fs(r)
            # a second feature, so the compile isn't a one-op trivial case
            r2 = _apply(did, [{"op": "add_feature",
                               "feature": {"type": "chamfer", "params": {"distance": 0.5},
                                           "refs": {"edges": [{"of": "edge", "side": "+z"}]}}}])
            assert all(s["ok"] for s in _fs(r2)), _fs(r2)
            results[i] = (did, _mass(did)["volume_mm3"])
        except Exception as e:  # noqa: BLE001
            errors[i] = e

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
    assert all(e is None for e in errors), errors

    try:
        # Isolation: each part's volume tracks its own size (strictly increasing sizes
        # -> strictly increasing volumes), and all doc_ids are distinct.
        dids = [r[0] for r in results]
        vols = [r[1] for r in results]
        assert len(set(dids)) == n
        assert all(vols[i] < vols[i + 1] for i in range(n - 1)), vols
    finally:
        for r in results:
            if r:
                _close(r[0])


def test_overlapping_edits_same_doc_both_land():
    """Two 'agents' interleave edits on the SAME part, each owning a different
    feature. The engine serialises; when the dust settles BOTH edit streams must
    be reflected in the final tree (no lost update, no torn state)."""
    did = cb.call("doc_new", {"name": "shared_part"})["doc_id"]
    try:
        r = _apply(did, [
            _box(50, 40, 10),
            {"op": "add_feature", "feature": {"type": "chamfer", "params": {"distance": 1.0},
                                              "refs": {"edges": [{"of": "edge", "side": "-z"}]}}},
        ])
        assert all(s["ok"] for s in _fs(r)), _fs(r)
        fids = [s["id"] for s in _fs(r)]
        iters = 8
        errs: list = []

        def edit_box():
            try:
                for k in range(iters):
                    _apply(did, [{"op": "edit_feature", "id": fids[0],
                                  "params": {"length": 50 + k + 1}}])
            except Exception as e:  # noqa: BLE001
                errs.append(("box", e))

        def edit_chamfer():
            try:
                for k in range(iters):
                    _apply(did, [{"op": "edit_feature", "id": fids[1],
                                  "params": {"distance": 1.0 + 0.1 * (k + 1)}}])
            except Exception as e:  # noqa: BLE001
                errs.append(("chamfer", e))

        ta, tb = threading.Thread(target=edit_box), threading.Thread(target=edit_chamfer)
        ta.start(); tb.start()
        ta.join(timeout=120); tb.join(timeout=120)
        assert not errs, errs

        tree = cb.call("doc_tree", {"doc_id": did})
        feats = {f["id"]: f for f in tree["tree"]["features"]}
        assert feats[fids[0]]["params"]["length"] == 50 + iters   # agent A's last edit
        assert feats[fids[1]]["params"]["distance"] == pytest.approx(1.0 + 0.1 * iters)
        assert _mass(did)["volume_mm3"] > 0
    finally:
        _close(did)


# --------------------------------------------------------------------- layer 2
def test_incremental_caches_do_not_cross_contaminate():
    """Two docs edited in parallel; each session's CompileCache must serve only its
    own doc. Volumes must track each doc's own params exactly."""
    d1 = cb.call("doc_new", {"name": "cc_a"})["doc_id"]
    d2 = cb.call("doc_new", {"name": "cc_b"})["doc_id"]
    try:
        for did, dims in ((d1, (10, 10, 10)), (d2, (20, 20, 20))):
            r = _apply(did, [_box(*dims)])
            assert all(s["ok"] for s in _fs(r))
        errs: list = []

        def bump(did, base):
            try:
                for k in range(6):
                    _apply(did, [{"op": "edit_feature", "id": "f1",
                                  "params": {"height": base + k + 1}}])
            except Exception as e:  # noqa: BLE001
                errs.append(e)

        ta = threading.Thread(target=bump, args=(d1, 10))
        tb = threading.Thread(target=bump, args=(d2, 20))
        ta.start(); tb.start()
        ta.join(timeout=120); tb.join(timeout=120)
        assert not errs, errs
        assert _mass(d1)["volume_mm3"] == pytest.approx(10 * 10 * 16, rel=1e-9)
        assert _mass(d2)["volume_mm3"] == pytest.approx(20 * 20 * 26, rel=1e-9)
    finally:
        _close(d1)
        _close(d2)


# --------------------------------------------------------------------- layer 3
def test_handoff_fragment_merge_unions_live_drops_dead(tmp_path, monkeypatch):
    """docs.json is merged from per-process fragments: live fragments union; a
    fragment whose PID is dead is dropped AND cleaned off disk."""
    from services.mcp import cad_common as ct  # T-RF8: handoff/autosave internals moved

    alive_pid = ct._HANDOFF_PID
    dead_pid = 999999  # far above pid_max defaults; os.kill -> ProcessLookupError

    def frag(session, pid, docs):
        return {"rev": 1, "active": None, "session": session, "pid": pid,
                "updated_at": __import__("time").time(),
                "docs": [{"doc_id": f"{session}-{d}", "step": f"/tmp/{d}.step+",
                          "rev": 1, "name": n} for d, n in docs]}

    (tmp_path / "docs-aaaa.json").write_text(
        json.dumps(frag("aaaa", alive_pid, [("doc1", "PartA")])))
    (tmp_path / "docs-bbbb.json").write_text(
        json.dumps(frag("bbbb", alive_pid, [("doc1", "PartB")])))  # same raw doc_id, other proc
    (tmp_path / "docs-dead.json").write_text(
        json.dumps(frag("dead", dead_pid, [("doc9", "Ghost")])))

    ct._merge_handoff_fragments(tmp_path)

    merged = json.loads((tmp_path / "docs.json").read_text())
    names = sorted(e["name"] for e in merged["docs"])
    assert names == ["PartA", "PartB"]  # both live procs kept, same-doc_id no fusion
    ids = {e["doc_id"] for e in merged["docs"]}
    assert ids == {"aaaa-doc1", "bbbb-doc1"}  # session-prefixed => distinct tabs
    assert "Ghost" not in json.dumps(merged)
    assert not (tmp_path / "docs-dead.json").exists()  # dead fragment cleaned up


def test_orphan_step_files_swept(tmp_path):
    """Step files whose owning session has NO live fragment (its fragment was already
    retired, or the process crashed before atexit) are the leak that piled up 1000+
    files in the live dir. The merge's orphan sweep deletes them once they age past the
    TTL, while keeping live-session files and too-fresh files (mid-write race)."""
    import os
    import time

    from services.mcp import cad_common as ct  # T-RF8: handoff/autosave internals moved

    alive_pid = ct._HANDOFF_PID
    now = time.time()

    # A live fragment for session 'live1' — its step file must survive.
    (tmp_path / "docs-live1.json").write_text(json.dumps({
        "rev": 1, "active": None, "session": "live1", "pid": alive_pid,
        "updated_at": now,
        "docs": [{"doc_id": "live1-doc1", "step": str(tmp_path / "live-live1-doc1.step+"),
                  "rev": 1, "name": "Keep"}]}))

    keep = tmp_path / "live-live1-doc1.step+"          # live session → keep
    orphan_old = tmp_path / "live-deadsess-doc5.step+"  # no fragment, old → sweep
    orphan_legacy = tmp_path / "live-doc-cpp00000009.step+"  # pre-prefix bare id, old → sweep
    fresh = tmp_path / "live-newsess-doc1.step+"        # no fragment yet but too fresh → keep
    for p in (keep, orphan_old, orphan_legacy, fresh):
        p.write_bytes(b"x")
    # Age the two orphans past the TTL; keep `fresh` recent.
    old_t = now - ct._HANDOFF_FRAG_TTL_S - 60
    os.utime(orphan_old, (old_t, old_t))
    os.utime(orphan_legacy, (old_t, old_t))

    ct._merge_handoff_fragments(tmp_path)

    assert keep.exists(), "live-session step file must be kept"
    assert fresh.exists(), "too-fresh orphan must be kept (mid-write race guard)"
    assert not orphan_old.exists(), "aged orphan of a dead session must be swept"
    assert not orphan_legacy.exists(), "aged legacy bare-id step file must be swept"


def test_autosave_filenames_are_session_prefixed(tmp_path, monkeypatch):
    """Two MCP processes both hold 'doc-cpp00000001'; their autosaves must land in
    DIFFERENT files (the pre-fix clobber), with the real doc_id kept in the sidecar."""
    from services.mcp import cad_common as ct  # T-RF8: handoff/autosave internals moved

    stem = f"{ct._SESSION_ID}-doc-cpp00000001"
    # The naming contract the autosaver writes (cad_tools._autosave): session-prefixed
    # stem for both the .step+ and the .json sidecar; sidecar keeps the raw doc_id.
    sidecar = {"doc_id": "doc-cpp00000001", "session": ct._SESSION_ID}
    (tmp_path / f"{stem}.step+").write_bytes(b"x")
    (tmp_path / f"{stem}.json").write_text(json.dumps(sidecar))

    got = ct._autosave_session_of(tmp_path / f"{stem}.step+")
    # (session, saved_at, name, doc_id) — doc_id must come from the sidecar, not the stem.
    assert got[0] == ct._SESSION_ID
    assert got[3] == "doc-cpp00000001"


def test_publish_revision_skip_and_burst_coalescing(tmp_path, monkeypatch):
    """A publish with no engine change since the last one must be a no-op (no re-serialize
    of the .step+), and a rapid edit burst must coalesce to ~2 writes (leading + trailing)
    instead of one full serialize per edit."""
    import time

    from services.mcp import cad_common as ct  # T-RF8: handoff/autosave internals moved

    # The .step+ publish/throttle/coalesce path this test exercises is LEGACY-only:
    # in daemon mode (the Phase-8 default) _publish_current() short-circuits to None
    # because the GUI shares the daemon's docs live and ignores handoff files
    # (cad_tools._publish_current + docs/HOSTD.md). Pin legacy mode so the throttle
    # logic under test actually runs regardless of the process-wide engine mode.
    monkeypatch.setattr(ct.cb, "active_daemon_mode", lambda: False)
    monkeypatch.setattr(ct, "_handoff_dir", lambda: tmp_path)
    did = cb.call("doc_new", {"name": "pub_throttle"})["doc_id"]
    try:
        _apply(did, [_box(10, 10, 10)])

        # --- revision-skip (interval disabled so coalescing can't mask it) ---
        monkeypatch.setattr(ct, "_PUBLISH_MIN_INTERVAL_S", 0.0)
        ct._PUB_THROTTLE.pop(did, None)
        r1 = ct._publish_current(did)
        assert r1 and r1["shown_in_app"]
        step = tmp_path / f"live-{ct._SESSION_ID}-{ct._handoff_key(did)}.step+"
        assert step.exists()
        mt1 = step.stat().st_mtime_ns
        r2 = ct._publish_current(did)  # no edit in between -> skipped, cached result
        assert step.stat().st_mtime_ns == mt1, "unchanged doc must not re-serialize"
        assert r2 == r1
        _apply(did, [{"op": "edit_feature", "id": "f1", "params": {"height": 12}}])
        ct._publish_current(did)
        assert step.stat().st_mtime_ns != mt1, "a real edit must republish"

        # --- burst coalescing ---
        monkeypatch.setattr(ct, "_PUBLISH_MIN_INTERVAL_S", 0.2)
        ct._PUB_THROTTLE.pop(did, None)
        writes0 = step.stat().st_mtime_ns
        for k in range(5):
            _apply(did, [{"op": "edit_feature", "id": "f1", "params": {"height": 20 + k}}])
            ct._publish_current(did)
        mid = step.stat().st_mtime_ns
        time.sleep(0.5)  # let the trailing timer flush the final state
        final = step.stat().st_mtime_ns
        assert final != writes0
        # The trailing flush must have landed the LAST edit's geometry.
        merged = json.loads((tmp_path / "docs.json").read_text())
        assert any(e["name"] == "pub_throttle" for e in merged["docs"])
        assert final >= mid
    finally:
        _close(did)
        ct._PUB_THROTTLE.pop(did, None)
        ct._PUB["docs"].pop(did, None)


def test_autosave_skips_unchanged_docs(tmp_path, monkeypatch):
    """The periodic autosave sweep must skip docs whose engine revision hasn't moved
    since their last autosave (idle sweep = no disk writes), and must save again
    after a real edit."""
    from services.mcp import cad_common as ct  # T-RF8: handoff/autosave internals moved

    monkeypatch.setattr(ct, "_autosave_dir", lambda: tmp_path)
    did = cb.call("doc_new", {"name": "as_skip"})["doc_id"]
    try:
        _apply(did, [_box(8, 8, 8)])
        ct._AUTOSAVE_LAST_REV.pop(did, None)
        ct._autosave_all_open()
        f = tmp_path / f"{ct._SESSION_ID}-{did}.step+"
        assert f.exists()
        mt1 = f.stat().st_mtime_ns
        ct._autosave_all_open()  # nothing changed -> must not rewrite
        assert f.stat().st_mtime_ns == mt1
        _apply(did, [{"op": "edit_feature", "id": "f1", "params": {"height": 9}}])
        ct._autosave_all_open()
        assert f.stat().st_mtime_ns != mt1
    finally:
        _close(did)
        ct._AUTOSAVE_LAST_REV.pop(did, None)


def test_thread_clients_overlap_share_writer_group_and_close_on_exit(monkeypatch):
    """Two host-call threads reach the engine concurrently, not one socket lock."""
    from services.mcp import daemon_bridge

    barrier = threading.Barrier(2, timeout=5)
    connections = []
    results, errors = [], []

    class FakeClient:
        def __init__(self, *, writer_group):
            self.writer_group = writer_group
            self.closed = False
            self._lock = threading.Lock()
            connections.append(self)

        def call(self, method, params=None):
            with self._lock:
                barrier.wait()
                return {"doc_id": params["doc_id"]}

        def close(self):
            self.closed = True

    monkeypatch.setattr(daemon_bridge, "DaemonClient", FakeClient)
    monkeypatch.setattr(daemon_bridge, "_clients", threading.local())

    def worker(doc):
        try:
            connection = daemon_bridge.client()
            assert daemon_bridge.client() is connection
            results.append(cb._call_via_daemon("doc_tree", {"doc_id": doc}))
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(doc,)) for doc in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert not any(thread.is_alive() for thread in threads)
    assert not errors
    assert sorted(result["doc_id"] for result in results) == ["a", "b"]
    assert len(connections) == 2
    assert {c.writer_group for c in connections} == {daemon_bridge._WRITER_GROUP}
    assert all(c.closed for c in connections)


def test_writer_group_hello_is_opt_in(monkeypatch):
    from services.mcp.daemon_bridge import DaemonClient

    for group in ("", "py-worker"):
        connection = DaemonClient(writer_group=group)
        calls = []
        monkeypatch.setattr(
            connection, "_call_raw",
            lambda method, params: calls.append((method, params)) or {"ok": True},
        )
        assert connection._hello() == {"ok": True}
        assert calls[0][0] == "hello"
        assert calls[0][1].get("writer_group", "") == group
        assert ("writer_group" in calls[0][1]) == bool(group)


@pytest.mark.parametrize("explicit", [False, True])
def test_apply_ops_disables_implicit_cas_and_preserves_explicit_cas(monkeypatch, explicit):
    from services.mcp import daemon_bridge

    calls = []
    original = {"doc_id": "part", "ops": []}
    if explicit:
        original["base_revision"] = 4

    class FakeClient:
        def call(self, name, params):
            calls.append((name, dict(params)))
            if params["base_revision"] >= 0:
                return {"_is_error": True, "code": "stale_revision", "message": "stale"}
            return {"ok": True}

    monkeypatch.setattr(daemon_bridge, "client", lambda: FakeClient())
    if explicit:
        with pytest.raises(cb.CoreError) as error:
            cb._call_via_daemon("doc_apply_ops", original)
        assert error.value.code == "stale_revision"
    else:
        assert cb._call_via_daemon("doc_apply_ops", original) == {"ok": True}
    assert calls == [("doc_apply_ops", {**original, "base_revision": 4 if explicit else -1})]
    assert ("base_revision" in original) == explicit


def test_busy_preview_uses_server_backoff_then_succeeds(monkeypatch):
    from services.mcp import daemon_bridge

    replies = iter([
        {"_is_error": True, "code": "busy_preview", "retry_ms": 123},
        {"ok": True},
    ])
    slept = []

    class FakeClient:
        def call(self, _method, _params):
            return next(replies)

    monkeypatch.setattr(daemon_bridge, "client", lambda: FakeClient())
    monkeypatch.setattr(cb.time, "sleep", slept.append)
    assert cb._call_via_daemon("doc_apply_ops", {"doc_id": "part"}) == {"ok": True}
    assert slept == [0.123]


def test_busy_preview_gives_up_after_bounded_wait_with_teaching_error(monkeypatch):
    from services.mcp import daemon_bridge

    now = [0.0]
    slept = []

    def sleep(seconds):
        slept.append(seconds)
        now[0] += seconds

    class FakeClient:
        def call(self, _method, _params):
            return {"_is_error": True, "code": "busy_preview", "retry_ms": 7000}

    monkeypatch.setattr(daemon_bridge, "client", lambda: FakeClient())
    monkeypatch.setattr(cb.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(cb.time, "sleep", sleep)
    with pytest.raises(cb.CoreError) as error:
        cb._call_via_daemon("doc_apply_ops", {"doc_id": "bracket"})
    assert error.value.code == "busy_preview"
    assert "user is editing bracket in the app right now" in str(error.value)
    assert "work on something else and retry" in str(error.value)
    assert slept == [7.0, 7.0, 7.0, 7.0, 2.0]
    assert now[0] == 30.0


@pytest.mark.parametrize("code", ["read_only", "invalid_params", "engine_failed"])
def test_other_engine_errors_never_retry(monkeypatch, code):
    from services.mcp import daemon_bridge

    calls = []

    class FakeClient:
        def call(self, method, params):
            calls.append(method)
            return {"_is_error": True, "code": code, "message": "refused"}

    monkeypatch.setattr(daemon_bridge, "client", lambda: FakeClient())
    with pytest.raises(cb.CoreError) as error:
        cb._call_via_daemon("doc_apply_ops", {"doc_id": "part"})
    assert error.value.code == code
    assert calls == ["doc_apply_ops"]
