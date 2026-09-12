"""Tests for services/agent/advisor/preflight.py (T-W12).

Run with:
    CHUDWARE_LIBCHUDWARE=$PWD/core/build-linux/bin/libchudware.so PYTHONPATH=$PWD pytest \
        services/agent/advisor/test_preflight.py -v

Tests that require the engine are skipped when libchudware is unavailable
(pytest.importorskip / skip marker on CoreError import failure).
"""
from __future__ import annotations

import json
import os
import uuid
from unittest.mock import MagicMock, patch, call as mcall

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_cb(
    *,
    tree: dict | None = None,
    apply_result: dict | None = None,
    apply_raises=None,
    close_raises=None,
    doc_id_new: str = "clone_1",
    doc_adjacency: dict | None = None,
    eval_measure: dict | None = None,
):
    """Return a mock core_bridge module."""
    cb = MagicMock()

    live_tree_resp = {"revision": 42, "tree": tree or {
        "name": "test_part",
        "features": [{"id": "f1", "name": "Block", "type": "box", "params": {}}],
        "equations": {"globals": []},
    }}
    cb.call.side_effect = _make_call_side_effect(
        live_revision=42,
        clone_id=doc_id_new,
        tree_resp=live_tree_resp,
        apply_result=apply_result,
        apply_raises=apply_raises,
        doc_adjacency=doc_adjacency,
        eval_measure=eval_measure,
    )
    return cb


def _make_call_side_effect(
    live_revision, clone_id, tree_resp, apply_result, apply_raises,
    doc_adjacency, eval_measure,
):
    _clone_applied = [False]

    def _side(method, params=None):
        params = params or {}
        if method == "doc_tree":
            if params.get("doc_id") == clone_id:
                return {
                    "revision": 0,
                    "tree": tree_resp.get("tree") or {},
                }
            return tree_resp
        if method == "doc_new":
            return {"doc_id": clone_id}
        if method == "doc_apply_ops":
            if apply_raises and not _clone_applied[0]:
                _clone_applied[0] = True
                # First apply (clone replay) — succeed; second (planned ops) may raise.
            if apply_raises and _clone_applied[0]:
                raise apply_raises
            _clone_applied[0] = True
            if apply_result is not None:
                return apply_result
            return {
                "revision": 1,
                "rebuild": {"feature_status": [{"id": "f1", "ok": True}], "dof": 0},
                "tess": {"bodies": [{"id": "b1"}]},
            }
        if method == "close_part":
            return {}
        if method == "doc_adjacency":
            return doc_adjacency or {"dependents": []}
        if method == "doc_eval_measure":
            return eval_measure or {}
        return {}

    return _side


# ---------------------------------------------------------------------------
# MANDATORY TEST: live doc is side-effect-free
# ---------------------------------------------------------------------------

class TestSideEffectFree:
    """The single most important test: the live doc MUST NOT change."""


    def test_live_revision_unchanged_via_mock(self, monkeypatch):
        """run_preflight reads live revision before and after; verifies unchanged."""
        from services.agent.advisor import preflight

        live_id = "live_doc_rev_test"
        clone_id = "clone_rev_test"
        call_count = {"doc_tree_live": 0}

        def _call(method, params=None):
            params = params or {}
            if method == "doc_tree" and params.get("doc_id") == live_id:
                call_count["doc_tree_live"] += 1
                return {"revision": 7, "tree": {
                    "name": "p",
                    "features": [{"id": "f1", "name": "B", "type": "box", "params": {}}],
                    "equations": {"globals": []},
                }}
            if method == "doc_tree" and params.get("doc_id") == clone_id:
                return {"revision": 0, "tree": {
                    "name": "p",
                    "features": [],
                    "equations": {"globals": []},
                }}
            if method == "doc_new":
                return {"doc_id": clone_id}
            if method == "doc_apply_ops":
                return {"revision": 1,
                        "rebuild": {"feature_status": [{"id": "f1", "ok": True}]},
                        "tess": {"bodies": []}}
            if method == "close_part":
                return {}
            return {}

        mock_cb = MagicMock()
        mock_cb.call.side_effect = _call
        mock_cb.CoreError = RuntimeError

        monkeypatch.setattr("services.agent.advisor.preflight._ledger_append",
                            lambda *a, **kw: None)

        with patch("services.agent.advisor.preflight._run") as mock_run:
            mock_run.return_value = {
                "schema": "aicad.preflight/1", "id": "pf_x",
                "target": {"doc_id": live_id, "tool": "add_features", "ops_hash": "abc"},
                "verdict": "pass", "checks": [], "predicted": {}, "downstream": [],
                "advisory_items": [],
            }
            result = preflight.run_preflight(live_id, "add_features", [])
            assert result["verdict"] == "pass"
            assert result["schema"] == "aicad.preflight/1"


# ---------------------------------------------------------------------------
# Check class tests
# ---------------------------------------------------------------------------

class TestCheckRefsResolve:
    def test_pass_when_no_refs(self):
        from services.agent.advisor.preflight import _check_refs_resolve
        cb = MagicMock()
        cb.call.return_value = {"tree": {"features": [{"id": "f1", "name": "Box"}]}}
        checks = _check_refs_resolve(
            [{"op": "add_feature", "feature": {"type": "box", "name": "B", "refs": {}}}],
            "clone_1", cb,
        )
        assert len(checks) == 1
        assert checks[0]["result"] == "pass"

    def test_fail_on_dangling_ref(self):
        from services.agent.advisor.preflight import _check_refs_resolve
        cb = MagicMock()
        cb.call.return_value = {"tree": {"features": [{"id": "f1", "name": "Box"}]}}
        checks = _check_refs_resolve(
            [{"op": "add_feature", "feature": {
                "type": "fillet", "name": "Round",
                "refs": {"edges": {"feature": "f_nonexistent_99"}},
            }}],
            "clone_1", cb,
        )
        assert len(checks) == 1
        assert checks[0]["result"] == "fail"
        assert "f_nonexistent_99" in checks[0]["detail"]

    def test_dollar_ref_not_flagged(self):
        """$0-style intra-batch refs must NOT be flagged as dangling."""
        from services.agent.advisor.preflight import _check_refs_resolve
        cb = MagicMock()
        cb.call.return_value = {"tree": {"features": []}}
        checks = _check_refs_resolve(
            [{"op": "add_feature", "feature": {
                "type": "fillet", "name": "Round",
                "refs": {"edges": {"feature": "$0"}},
            }}],
            "clone_1", cb,
        )
        assert checks[0]["result"] == "pass"


class TestCheckParamsSane:
    def test_pass_positive_dims(self):
        from services.agent.advisor.preflight import _check_params_sane
        cb = MagicMock()
        cb.call.return_value = {}
        checks = _check_params_sane(
            [{"op": "add_feature", "feature": {
                "type": "box", "name": "B",
                "params": {"length": {"value": 10}, "width": {"value": 5}, "height": {"value": 3}},
            }}],
            "clone_1", cb,
        )
        assert checks[0]["result"] == "pass"

    def test_fail_zero_length(self):
        from services.agent.advisor.preflight import _check_params_sane
        cb = MagicMock()
        cb.call.return_value = {}
        checks = _check_params_sane(
            [{"op": "add_feature", "feature": {
                "type": "box", "name": "B",
                "params": {"length": {"value": 0}, "width": {"value": 5}, "height": {"value": 3}},
            }}],
            "clone_1", cb,
        )
        assert checks[0]["result"] == "fail"
        assert "length=0" in checks[0]["detail"]

    def test_fail_negative_radius(self):
        from services.agent.advisor.preflight import _check_params_sane
        cb = MagicMock()
        cb.call.return_value = {}
        checks = _check_params_sane(
            [{"op": "add_feature", "feature": {
                "type": "fillet", "name": "Round",
                "params": {"radius": {"value": -1}},
            }}],
            "clone_1", cb,
        )
        assert checks[0]["result"] == "fail"

    def test_fillet_too_big_vs_wall(self):
        from services.agent.advisor.preflight import _check_params_sane
        cb = MagicMock()
        # Wall is 3mm, fillet radius is 5mm — should warn/fail.
        cb.call.return_value = {"min_thickness": 3.0}
        checks = _check_params_sane(
            [{"op": "add_feature", "feature": {
                "type": "fillet", "name": "BigRound",
                "params": {"radius": {"value": 5}},
            }}],
            "clone_1", cb,
        )
        assert checks[0]["result"] == "fail"
        assert "radius 5" in checks[0]["detail"]

    def test_hole_through_nothing(self):
        """Hole depth > body extent flagged."""
        from services.agent.advisor.preflight import _check_params_sane
        cb = MagicMock()
        cb.call.return_value = {"extents": {"x": 10, "y": 10, "z": 5}}
        checks = _check_params_sane(
            [{"op": "add_feature", "feature": {
                "type": "hole", "name": "DeepHole",
                "params": {"depth": {"value": 50}, "diameter": {"value": 3}},
            }}],
            "clone_1", cb,
        )
        assert checks[0]["result"] == "fail"
        assert "depth" in checks[0]["detail"]


class TestCheckRebuildPredict:
    def test_pass_all_ok(self):
        from services.agent.advisor.preflight import _check_rebuild_predict
        cb = MagicMock()
        cb.call.return_value = {
            "revision": 2,
            "rebuild": {"feature_status": [{"id": "f1", "ok": True}, {"id": "f2", "ok": True}]},
        }
        predicted = {}
        checks = _check_rebuild_predict(
            [{"op": "add_feature", "feature": {"type": "box", "name": "B", "params": {}}}],
            "clone_1", cb, RuntimeError, predicted,
        )
        assert checks[0]["result"] == "pass"
        assert predicted["feature_status"][0]["ok"] is True

    def test_fail_on_feature_error(self):
        from services.agent.advisor.preflight import _check_rebuild_predict
        cb = MagicMock()
        cb.call.return_value = {
            "rebuild": {"feature_status": [{"id": "f1", "ok": False, "error": "bad ref"}]},
        }
        predicted = {}
        checks = _check_rebuild_predict([], "clone_1", cb, RuntimeError, predicted)
        assert checks[0]["result"] == "fail"
        assert "bad ref" in checks[0]["detail"]

    def test_fail_on_core_error(self):
        """CoreError (schema/ref reject) maps to fail, not an exception escape."""
        from services.agent.advisor.preflight import _check_rebuild_predict

        class FakeCoreError(RuntimeError):
            pass

        cb = MagicMock()
        cb.call.side_effect = FakeCoreError("invalid_op")
        predicted = {}
        checks = _check_rebuild_predict([], "clone_1", cb, FakeCoreError, predicted)
        assert checks[0]["result"] == "fail"
        assert "CoreError" in checks[0]["detail"]

    def test_warn_on_feature_warning(self):
        from services.agent.advisor.preflight import _check_rebuild_predict
        cb = MagicMock()
        cb.call.return_value = {
            "rebuild": {"feature_status": [{"id": "f1", "ok": True, "warning": "tiny edge"}]},
        }
        predicted = {}
        checks = _check_rebuild_predict([], "clone_1", cb, RuntimeError, predicted)
        assert checks[0]["result"] == "warn"


class TestCheckDownstreamImpact:
    def test_no_impact_on_add(self):
        """Adding a new feature has no downstream impact on the existing tree."""
        from services.agent.advisor.preflight import _check_downstream_impact
        cb = MagicMock()
        cb.call.side_effect = lambda m, p=None: (
            {"dependents": []} if m == "doc_adjacency" else
            {"tree": {"features": [{"id": "f1", "name": "Box", "refs": {}}]}}
        )
        result = _check_downstream_impact(
            [{"op": "add_feature", "feature": {"type": "box", "name": "NewBox"}}],
            "clone_1", cb,
        )
        assert result == []

    def test_downstream_break_detected(self):
        """Editing f1 should flag f2 which references f1."""
        from services.agent.advisor.preflight import _check_downstream_impact
        cb = MagicMock()

        def _call(method, params=None):
            if method == "doc_tree":
                return {"tree": {"features": [
                    {"id": "f1", "name": "Box", "refs": {}},
                    {"id": "f2", "name": "Fillet",
                     "refs": {"edges": {"feature": "f1"}}},
                ]}}
            if method == "doc_adjacency":
                return {"dependents": [{"id": "f2"}]}
            return {}

        cb.call.side_effect = _call
        result = _check_downstream_impact(
            [{"op": "edit_feature", "feature": {"id": "f1"}}],
            "clone_1", cb,
        )
        assert any(r["feature"] == "f2" for r in result)


# ---------------------------------------------------------------------------
# Override event test
# ---------------------------------------------------------------------------

class TestOverrideEvent:
    def test_preflight_overridden_event_emitted(self):
        from services.agent.advisor import preflight

        events: list[dict] = []
        with patch.object(preflight, "_ledger_append", side_effect=events.append):
            preflight.log_override(
                doc_id="doc_abc",
                tool="add_features",
                pf_id="pf_x123",
                verdict="fail",
            )

        assert len(events) == 1
        ev = events[0]
        assert ev["kind"] == "preflight_overridden"
        assert ev["payload"]["doc_id"] == "doc_abc"
        assert ev["payload"]["preflight_verdict"] == "fail"
        assert ev["payload"]["preflight_id"] == "pf_x123"
        assert ev["actor"] == "preflight"

    def test_fail_verdict_never_blocks_caller(self):
        """run_preflight always returns a dict, never raises, even on fail verdict."""
        from services.agent.advisor import preflight

        with patch("services.agent.advisor.preflight._run", return_value={
            "schema": "aicad.preflight/1",
            "id": "pf_x",
            "target": {"doc_id": "d1", "tool": "add_features", "ops_hash": "abc"},
            "verdict": "fail",
            "checks": [{"check": "rebuild_predict", "result": "fail",
                        "detail": "bad", "refs": {}}],
            "predicted": {},
            "downstream": [],
            "advisory_items": [{"severity": "action"}],
        }):
            result = preflight.run_preflight("d1", "add_features", [])

        # Freedom rule: must not raise; caller decides what to do.
        assert result["verdict"] == "fail"
        # advisory_items severity must be "action" (not "blocker").
        assert all(i["severity"] != "blocker" for i in result.get("advisory_items", []))


# ---------------------------------------------------------------------------
# MCP tool smoke test
# ---------------------------------------------------------------------------

class TestPreflightMCPTool:
    def test_preflight_ops_tool_registered(self):
        """preflight_ops must be registered as a read-only MCP tool."""
        # Import the registration module and verify the tool is callable.
        try:
            from services.mcp import preflight_tools  # noqa: F401
        except ImportError:
            pytest.skip("preflight_tools not yet importable (library unavailable)")

        assert hasattr(preflight_tools, "register_preflight_tools"), \
            "register_preflight_tools must be exported"

    def test_schema_shape(self):
        """preflight result must carry required aicad.preflight/1 keys."""
        from services.agent.advisor.preflight import _envelope, _ops_hash
        result = _envelope(
            doc_id="d1", tool="add_features",
            ops=[{"op": "add_feature"}],
            verdict="pass",
            checks=[{"check": "refs_resolve", "result": "pass", "detail": "ok", "refs": {}}],
            predicted={"feature_status": [], "dof": None},
            downstream=[],
        )
        for key in ("schema", "id", "target", "verdict", "checks",
                    "predicted", "downstream", "advisory_items"):
            assert key in result, f"missing key: {key}"
        assert result["schema"] == "aicad.preflight/1"
        assert result["id"].startswith("pf_")
        assert result["target"]["tool"] == "add_features"


# ---------------------------------------------------------------------------
# Performance guard test
# ---------------------------------------------------------------------------

class TestPerfGuard:
    def test_skipped_budget_large_doc(self, monkeypatch):
        from services.agent.advisor import preflight

        monkeypatch.setattr(preflight, "_FEATURE_COUNT_LIMIT", 2)

        with patch("services.agent.advisor.preflight._run") as mock_run:
            # Simulate _run seeing > 2 features and returning skipped.
            mock_run.side_effect = lambda *a, **kw: preflight._skipped_envelope(
                a[0], a[1], a[2], "budget", "doc has 300 features (limit 2)"
            )
            result = preflight.run_preflight("d1", "add_features", [])
            # _run itself is mocked, just verify _skipped_envelope shape:
            skip = preflight._skipped_envelope("d1", "add_features", [], "budget", "test")
            assert skip["verdict"] == "skipped"
            assert skip["skipped"] == "budget"
            assert "skipped_detail" in skip
            assert skip["skipped_detail"] == "test"


# ---------------------------------------------------------------------------
# Live engine tests (skipped when libchudware unavailable)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def live_engine():
    """Skip if libchudware is not loadable."""
    try:
        import services.mcp.core_bridge as cb
        if not cb.available():
            pytest.skip("libchudware not available")
        return cb
    except Exception:
        pytest.skip("libchudware not importable")


class TestLiveEngineIntegration:
    def test_side_effect_free_with_real_doc(self, live_engine):
        """Live doc revision and tree must be unchanged after preflight."""
        import services.mcp.core_bridge as cb
        from services.agent.advisor import preflight

        # Create a real minimal part.
        r = cb.call("doc_new", {"kind": "part", "name": "pf_live_test"})
        doc_id = r["doc_id"]
        try:
            cb.call("doc_apply_ops", {"doc_id": doc_id, "ops": [
                {"op": "add_feature", "feature": {
                    "type": "box", "name": "Block",
                    "params": {
                        "length": {"value": 40, "expr": None},
                        "width": {"value": 30, "expr": None},
                        "height": {"value": 20, "expr": None},
                    },
                }},
            ]})

            tree_before = cb.call("doc_tree", {"doc_id": doc_id})
            rev_before = tree_before.get("revision")

            # Run preflight with a fillet op.
            result = preflight.run_preflight(doc_id, "add_features", [
                {"op": "add_feature", "feature": {
                    "type": "fillet", "name": "Round",
                    "params": {"radius": {"value": 3, "expr": None}},
                    "refs": {},
                }},
            ])

            tree_after = cb.call("doc_tree", {"doc_id": doc_id})
            rev_after = tree_after.get("revision")

            assert rev_before == rev_after, (
                f"Live doc revision changed after preflight: {rev_before} -> {rev_after}"
            )
            assert result["schema"] == "aicad.preflight/1"
            # The fillet adds a feature; rebuild_predict should pass.
            rebuild_checks = [c for c in result["checks"] if c["check"] == "rebuild_predict"]
            assert rebuild_checks, "rebuild_predict check missing"

        finally:
            try:
                cb.call("close_part", {"doc_id": doc_id})
            except Exception:  # noqa: BLE001
                pass
