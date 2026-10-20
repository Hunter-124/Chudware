"""Incremental-rebuild prefix cache (core CompileCache) — correctness suite.

The engine now checkpoints compiler state after every feature and, on the next
compile of the same session, restores the deepest checkpoint whose input-hash
chain still matches, executing only the divergent suffix (document.h
CompileCache). These tests pin the contract:

  * edit the LAST feature of N   -> N-1 reused, 1 executed
  * edit the FIRST feature       -> 0 reused (worst case = old full rebuild)
  * edit a MIDDLE feature        -> exactly its prefix reused
  * geometry after an incremental rebuild is IDENTICAL to a cold rebuild
  * undo/redo can never be served stale state (content-addressed chain)
  * a failed batch (rolled back) doesn't poison later compiles

Run: .venv-mcp/bin/pytest services/mcp/test_incremental_rebuild.py -v
"""
from __future__ import annotations

import importlib
import os
import sys
import time

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


@pytest.fixture(autouse=True)
def native_engine_mode(monkeypatch):
    """Test native CompileCache and human undo, not the MCP agent undo policy."""
    monkeypatch.setenv("CHUDWARE_ENGINE_DAEMON", "0")
    assert not cb.active_daemon_mode()


def _apply(doc_id, ops):
    return cb.call("doc_apply_ops", {"doc_id": doc_id, "ops": ops})


def _fs(resp):
    return resp["rebuild"]["feature_status"]


def _counters(resp):
    ds = resp["rebuild"]["doc_status"]
    return ds.get("features_reused"), ds.get("features_executed")


def _mass(doc_id):
    return cb.call("eval_mass", {"doc_id": doc_id})


@pytest.fixture()
def part():
    """A 4-feature part: box -> fillet(top edges) -> blind hole -> chamfer(bottom)."""
    did = cb.call("doc_new", {"name": "inc_rebuild_fixture"})["doc_id"]
    ops = [
        {"op": "add_feature", "feature": {"type": "box",
                                          "params": {"length": 40, "width": 30, "height": 20}}},
        {"op": "add_feature", "feature": {"type": "fillet", "params": {"radius": 2},
                                          "refs": {"edges": [{"of": "edge", "side": "+z"}]}}},
        {"op": "add_feature", "feature": {"type": "hole",
                                          "params": {"style": "simple", "d": 6,
                                                     "end": "blind", "depth": 10},
                                          "refs": {"face": {"of": "face", "side": "top"}}}},
        {"op": "add_feature", "feature": {"type": "chamfer", "params": {"distance": 1.5},
                                          "refs": {"edges": [{"of": "edge", "side": "-z"}]}}},
    ]
    r = _apply(did, ops)
    assert all(s["ok"] for s in _fs(r)), _fs(r)
    fids = [s["id"] for s in _fs(r)]
    yield did, fids
    try:
        cb.call("doc_close", {"doc_id": did})
    except Exception:
        pass


def test_edit_last_reuses_prefix(part):
    did, fids = part
    r = _apply(did, [{"op": "edit_feature", "id": fids[-1], "params": {"distance": 2.0}}])
    reused, executed = _counters(r)
    assert (reused, executed) == (len(fids) - 1, 1)
    flags = [s["reused"] for s in _fs(r)]
    assert flags == [True] * (len(fids) - 1) + [False]
    assert all(s["ok"] for s in _fs(r))


def test_edit_first_full_reexecute(part):
    did, fids = part
    r = _apply(did, [{"op": "edit_feature", "id": fids[0], "params": {"length": 42}}])
    reused, executed = _counters(r)
    assert (reused, executed) == (0, len(fids))
    assert all(s["ok"] for s in _fs(r))


def test_edit_middle_partial_prefix(part):
    did, fids = part
    r = _apply(did, [{"op": "edit_feature", "id": fids[1], "params": {"radius": 3}}])
    reused, executed = _counters(r)
    assert (reused, executed) == (1, len(fids) - 1)
    assert all(s["ok"] for s in _fs(r))


def test_incremental_geometry_identical_to_cold_rebuild(part):
    """The load-bearing guarantee: a prefix-cached rebuild must be bit-identical to a
    from-scratch compile of the same tree (same volume, CoM, inertia)."""
    did, fids = part
    # Two edits so the live doc's last compile mixed reused + executed features.
    _apply(did, [{"op": "edit_feature", "id": fids[-1], "params": {"distance": 2.0}}])
    r = _apply(did, [{"op": "edit_feature", "id": fids[1], "params": {"radius": 3}}])
    tree = r["tree"]

    ref = cb.call("doc_new", {"name": "inc_rebuild_ref"})["doc_id"]
    try:
        rr = _apply(ref, [{"op": "add_feature", "feature": f} for f in tree["features"]])
        assert all(s["ok"] for s in _fs(rr))
        m_inc, m_ref = _mass(did), _mass(ref)
        assert m_inc["volume_mm3"] == pytest.approx(m_ref["volume_mm3"], rel=1e-12)
        assert m_inc["com"] == pytest.approx(m_ref["com"], abs=1e-9)
        assert m_inc["inertia"] == pytest.approx(m_ref["inertia"], rel=1e-9)
    finally:
        try:
            cb.call("doc_close", {"doc_id": ref})
        except Exception:
            pass


def test_undo_redo_never_stale(part):
    did, fids = part
    v0 = _mass(did)["volume_mm3"]
    _apply(did, [{"op": "edit_feature", "id": fids[0], "params": {"length": 60}}])
    v1 = _mass(did)["volume_mm3"]
    assert v1 != pytest.approx(v0)
    cb.call("doc_undo", {"doc_id": did})
    assert _mass(did)["volume_mm3"] == pytest.approx(v0, rel=1e-12)
    cb.call("doc_redo", {"doc_id": did})
    assert _mass(did)["volume_mm3"] == pytest.approx(v1, rel=1e-12)


def test_failed_batch_rollback_then_clean_edit(part):
    """A rejected batch (feature errors -> engine restores pre-batch doc) must not
    poison the cache: the next good edit still compiles correct geometry."""
    did, fids = part
    v0 = _mass(did)["volume_mm3"]
    r = _apply(did, [{"op": "add_feature",
                      "feature": {"type": "fillet", "params": {"radius": 1e6},
                                  "refs": {"edges": [{"of": "edge", "side": "+z"}]}}}])
    assert not all(s["ok"] for s in _fs(r))  # rejected in-band
    assert _mass(did)["volume_mm3"] == pytest.approx(v0, rel=1e-12)  # rolled back
    r2 = _apply(did, [{"op": "edit_feature", "id": fids[-1], "params": {"distance": 2.5}}])
    assert all(s["ok"] for s in _fs(r2))


def test_suppress_unsuppress_roundtrip(part):
    """Suppression flows through the hash chain (feature json changes) — geometry
    with the hole suppressed gains its volume back, and unsuppressing restores it."""
    did, fids = part
    v0 = _mass(did)["volume_mm3"]
    _apply(did, [{"op": "set_suppressed", "id": fids[2], "value": True}])
    v_sup = _mass(did)["volume_mm3"]
    assert v_sup > v0  # blind hole removed -> more material
    _apply(did, [{"op": "set_suppressed", "id": fids[2], "value": False}])
    assert _mass(did)["volume_mm3"] == pytest.approx(v0, rel=1e-12)


def test_dry_run_reuses_large_cached_source_without_replay():
    """A long source history is already compiled; prediction executes only the
    appended candidate, not the existing features, and leaves the source cache hot."""
    from services.agent.advisor.preflight import run_preflight

    did = cb.call("doc_new", {"name": "cached_preflight_source"})["doc_id"]
    try:
        ops = [{"op": "add_feature", "feature": {
            "type": "box", "name": f"History{i}", "suppressed": i > 0,
            "params": {"length": 40, "width": 30, "height": 20},
        }} for i in range(128)]
        initial = _apply(did, ops)
        assert all(s["ok"] for s in _fs(initial))
        before = cb.call("doc_tree", {"doc_id": did})
        mass_before = _mass(did)
        candidate = [{"op": "add_feature", "feature": {
            "type": "fillet", "params": {"radius": 2},
            "refs": {"edges": [{"of": "edge", "side": "+z"}]},
        }}]
        prediction = run_preflight(did, "add_features", candidate)
        assert prediction["verdict"] == "pass", prediction
        diagnostics = prediction["predicted"]["doc_status"]
        assert (diagnostics["features_reused"], diagnostics["features_executed"]) == (128, 1)
        assert cb.call("doc_tree", {"doc_id": did}) == before
        # A shared mutable index would now contain the candidate chain. Querying
        # the untouched source must still be a full cache hit, not a prefix rebuild.
        counters_before = cb.call("prof_contention", {})["compile_cache"]
        unchanged = cb.call("doc_dry_run_ops", {"doc_id": did, "ops": []})
        counters_after = cb.call("prof_contention", {})["compile_cache"]
        assert unchanged["ok"]
        assert _counters(unchanged) == (128, 0)
        assert counters_after["full_hits"] - counters_before["full_hits"] == 1
        assert _mass(did) == mass_before
    finally:
        cb.call("doc_close", {"doc_id": did})


@pytest.mark.parametrize("outcome", ["success", "compile_failure", "structural_failure"])
@pytest.mark.parametrize("budget_ms", [0, 10_000])
def test_dry_run_preserves_geometry_cache_and_undo(part, outcome, budget_ms):
    did, fids = part
    original = cb.call("doc_tree", {"doc_id": did})["tree"]
    original_mass = _mass(did)
    _apply(did, [{"op": "edit_feature", "id": fids[0], "params": {"length": 60}}])
    before = cb.call("doc_tree", {"doc_id": did})
    mass_before = _mass(did)
    candidate = [{"op": "edit_feature", "id": fids[0], "params": {"length": 45}}]
    if outcome == "compile_failure":
        candidate.append({"op": "add_feature", "feature": {
            "type": "fillet", "params": {"radius": 1e6},
            "refs": {"edges": [{"of": "edge", "side": "+z"}]},
        }})
    elif outcome == "structural_failure":
        candidate.append({"op": "edit_feature", "id": "missing", "params": {"length": 1}})
    dry = cb.call("doc_dry_run_ops", {"doc_id": did, "ops": candidate, "budget_ms": budget_ms})
    if budget_ms and os.name != "posix":
        assert dry["skipped"] == "isolation_unavailable"
        assert dry["still_running"] is False
        assert cb.call("doc_tree", {"doc_id": did}) == before
        assert _mass(did) == mass_before
        return
    assert dry["ok"] is (outcome == "success")
    if outcome == "compile_failure":
        assert any(not s["ok"] for s in _fs(dry))
    assert cb.call("doc_tree", {"doc_id": did}) == before
    # A scratch rebuild must not replace the source's compile index, even after
    # changing its first feature or rejecting a candidate after compilation.
    counters_before = cb.call("prof_contention", {})["compile_cache"]
    unchanged = cb.call("doc_dry_run_ops", {"doc_id": did, "ops": []})
    counters_after = cb.call("prof_contention", {})["compile_cache"]
    assert unchanged["ok"]
    assert counters_after["full_hits"] - counters_before["full_hits"] == 1
    assert _mass(did) == mass_before
    cb.call("doc_undo", {"doc_id": did})
    assert cb.call("doc_tree", {"doc_id": did})["tree"] == original
    assert _mass(did) == original_mass


@pytest.mark.skipif(os.name != "posix", reason="bounded native preflight requires POSIX")
def test_preflight_timeout_stops_rebuild_and_preserves_source(part, monkeypatch):
    from services.agent.advisor import preflight

    did, fids = part
    original = cb.call("doc_tree", {"doc_id": did})["tree"]
    original_mass = _mass(did)
    _apply(did, [{"op": "edit_feature", "id": fids[0], "params": {"length": 60}}])
    before = cb.call("doc_tree", {"doc_id": did})
    mass_before = _mass(did)
    # Stall an actual native blend after the private candidate has already changed
    # its box. Disabling ordinary blend isolation must not disable the outer budget.
    monkeypatch.setenv("CHUDWARE_BLEND_ISOLATION", "0")
    monkeypatch.setenv("CHUDWARE_TEST_BLEND_DELAY_MS", "5000")
    monkeypatch.setattr(preflight, "_REBUILD_BUDGET_MS", 150)
    started = time.monotonic()
    result = preflight.run_preflight(did, "add_features", [{
        "op": "edit_feature", "id": fids[0], "params": {"length": 45},
    }])
    assert result["verdict"] == "skipped", result
    assert result["skipped"] == "budget_exceeded"
    assert result["timed_out"] is True
    assert result["still_running"] is False
    assert not result["predicted"]
    assert not result["advisory_items"]
    assert time.monotonic() - started < 3.0
    assert cb.call("doc_tree", {"doc_id": did}) == before
    assert _mass(did) == mass_before
    # The source cache and undo history survive even an interrupted native edit.
    counters_before = cb.call("prof_contention", {})["compile_cache"]
    unchanged = cb.call("doc_dry_run_ops", {"doc_id": did, "ops": []})
    counters_after = cb.call("prof_contention", {})["compile_cache"]
    assert unchanged["ok"]
    assert _counters(unchanged) == (len(fids), 0)
    assert counters_after["full_hits"] - counters_before["full_hits"] == 1
    cb.call("doc_undo", {"doc_id": did})
    assert cb.call("doc_tree", {"doc_id": did})["tree"] == original
    assert _mass(did) == original_mass


@pytest.mark.skipif(os.name != "posix", reason="bounded native preflight requires POSIX")
@pytest.mark.parametrize("radius, expected", [(3, "pass"), (1e6, "fail")])
def test_bounded_preflight_retains_real_geometry_verdict(part, monkeypatch, radius, expected):
    from services.agent.advisor.preflight import run_preflight

    did, fids = part
    before = cb.call("doc_tree", {"doc_id": did})
    mass_before = _mass(did)
    monkeypatch.setenv("CHUDWARE_BLEND_ISOLATION", "0")
    result = run_preflight(did, "add_features", [{
        "op": "edit_feature", "id": fids[1], "params": {"radius": radius},
    }])
    assert result["verdict"] == expected, result
    statuses = result["predicted"]["feature_status"]
    assert all(status["ok"] for status in statuses) is (expected == "pass")
    assert cb.call("doc_tree", {"doc_id": did}) == before
    assert _mass(did) == mass_before


def test_profiler_reports_cache_section():
    prof = cb.call("prof_contention", {})
    cc = prof.get("compile_cache")
    assert isinstance(cc, dict)
    for key in ("compiles", "full_hits", "features_reused", "features_executed",
                "bodies_reused", "bodies_tessellated"):
        assert key in cc


# --- per-body tessellation+stats cache (multi-body parts) -----------------------
def _multibody(n=6):
    """n disjoint boxes (merge:false) → n surviving solid bodies."""
    did = cb.call("doc_new", {"name": "inc_multibody"})["doc_id"]
    ops = [{"op": "add_feature",
            "feature": {"type": "box", "params": {"length": 20, "width": 20,
                                                  "height": 20, "cx": 0}}}]
    for i in range(1, n):
        ops.append({"op": "add_feature",
                    "feature": {"type": "box", "merge": False,
                                "params": {"length": 20, "width": 20, "height": 20,
                                           "cx": i * 40}}})
    r = _apply(did, ops)
    assert all(s["ok"] for s in _fs(r)), _fs(r)
    return did, [s["id"] for s in _fs(r)]


def test_body_cache_reuses_unchanged_bodies():
    """Editing the LAST body of many reuses every earlier body's cached mesh+stats.
    The body cache reuses only bodies whose producing feature was in the reused
    PREFIX — re-executing a feature mints a fresh TShape (cache miss), so editing an
    upstream body busts the tessellation of everything downstream of it. Editing the
    last body is the max-reuse case: 5 upstream bodies reused, only the last re-meshed."""
    did, fids = _multibody(6)
    before = cb.call("prof_contention", {})["compile_cache"]
    _apply(did, [{"op": "edit_feature", "id": fids[-1], "params": {"height": 25}}])
    after = cb.call("prof_contention", {})["compile_cache"]
    reused = after["bodies_reused"] - before["bodies_reused"]
    tessed = after["bodies_tessellated"] - before["bodies_tessellated"]
    assert reused == 5, (reused, tessed)
    assert tessed == 1, (reused, tessed)
    try:
        cb.call("doc_close", {"doc_id": did})
    except Exception:
        pass


def test_body_cache_geometry_identical():
    """A multi-body part edited with the body cache active must have the SAME total
    volume as its from-scratch replay (the cache is geometry-only, bit-identical)."""
    did, fids = _multibody(6)
    _apply(did, [{"op": "edit_feature", "id": fids[3], "params": {"height": 30}}])
    tree = cb.call("doc_tree", {"doc_id": did})["tree"]
    v_inc = _mass(did)["volume_mm3"]
    ref = cb.call("doc_new", {"name": "inc_multibody_ref"})["doc_id"]
    try:
        rr = _apply(ref, [{"op": "add_feature", "feature": f} for f in tree["features"]])
        assert all(s["ok"] for s in _fs(rr))
        assert _mass(ref)["volume_mm3"] == pytest.approx(v_inc, rel=1e-12)
    finally:
        for x in (did, ref):
            try:
                cb.call("doc_close", {"doc_id": x})
            except Exception:
                pass
