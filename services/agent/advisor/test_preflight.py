"""Preflight decisions and refusal semantics; native cache/isolation coverage lives
in services/mcp/test_incremental_rebuild.py.

Run: .venv-mcp/bin/pytest services/agent/advisor/test_preflight.py -v
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from services.agent.advisor import preflight
from services.mcp import core_bridge


@pytest.mark.parametrize("refs, expected", [
    ({}, "pass"),
    ({"edges": {"feature": "missing"}}, "fail"),
    ({"edges": {"feature": "$0"}}, "pass"),
    ({"plane": {"feature": "plane.top"}}, "pass"),
])
def test_reference_resolution(refs, expected):
    cb = MagicMock()
    cb.call.return_value = {"tree": {"features": [{"id": "f1", "name": "Box"}]}}
    checks = preflight._check_refs_resolve([
        {"op": "add_feature", "feature": {"type": "fillet", "refs": refs}},
    ], "live", cb)
    assert checks[0]["result"] == expected


def test_reference_to_prior_batch_declaration():
    cb = MagicMock()
    cb.call.return_value = {"tree": {"features": []}}
    checks = preflight._check_refs_resolve([
        {"op": "add_feature", "feature": {"type": "box", "name": "Base"}},
        {"op": "add_feature", "feature": {"type": "fillet",
         "refs": {"edges": {"feature": "Base"}}}},
    ], "live", cb)
    assert checks[0]["result"] == "pass"


@pytest.mark.parametrize("params, expected", [
    ({"length": 10, "width": 5, "height": 3}, "pass"),
    ({"length": 0}, "fail"),
    ({"radius": -1}, "fail"),
])
def test_parameter_dimensions(params, expected):
    checks = preflight._check_params_sane([
        {"op": "add_feature", "feature": {"type": "box", "params": params}},
    ])
    assert checks[0]["result"] == expected


@pytest.mark.parametrize("feature", [
    {"type": "fillet", "params": {"radius": -1}},
    {"type": "chamfer", "params": {"thickness": 0}},
    {"type": "hole", "params": {"depth": 0}},
])
def test_local_dimension_refusal_does_not_require_live_measurement(feature):
    checks = preflight._check_params_sane([{"op": "add_feature", "feature": feature}])
    assert checks[0]["result"] == "fail"


@pytest.mark.parametrize("response, expected", [
    ({"ok": True, "rebuild": {"feature_status": [{"id": "f1", "ok": True}]}}, "pass"),
    ({"ok": False, "rebuild": {"feature_status": [
        {"id": "f2", "ok": False, "error": "bad ref"}]}}, "fail"),
    ({"ok": False, "error": "invalid_op"}, "fail"),
    ({"ok": True, "rebuild": {"feature_status": [
        {"id": "f1", "ok": True, "warning": "tiny edge"}]}}, "warn"),
    ({"ok": True}, "warn"),  # missing diagnostics must never mean a clean rebuild
    ({}, "warn"),
    ({"ok": False, "skipped": "budget_exceeded"}, "skipped"),
    ({"ok": False, "skipped": "isolation_unavailable"}, "skipped"),
])
def test_prediction_decision(response, expected):
    cb = MagicMock()
    cb.call.return_value = response
    checks = preflight._check_rebuild_predict([], "live", cb, core_bridge.CoreError, {})
    assert checks[0]["result"] == expected


def test_structural_exception_is_failure():
    cb = MagicMock()
    cb.call.side_effect = core_bridge.CoreError("invalid op", code="invalid_op")
    checks = preflight._check_rebuild_predict([], "live", cb, core_bridge.CoreError, {})
    assert checks[0]["result"] == "fail"


def test_downstream_checks_existing_tree_for_flat_edit_target():
    cb = MagicMock()
    cb.call.side_effect = lambda method, params: (
        {"tree": {"features": [
            {"id": "f1", "name": "Box"},
            {"id": "f2", "refs": {"edges": {"feature": "f1"}}},
        ]}} if method == "doc_tree" else {"dependents": []}
    )
    result = preflight._check_downstream_impact([
        {"op": "edit_feature", "id": "f1", "params": {"length": 20}},
    ], "live", cb)
    assert [entry["feature"] for entry in result] == ["f2"]


def _engine(response, *, revision_after=7):
    tree_reads = 0

    def call(method, params):
        nonlocal tree_reads
        if method == "doc_tree":
            tree_reads += 1
            return {"revision": 7 if tree_reads <= 2 else revision_after,
                    "tree": {"features": []}}
        if method == "doc_dry_run_ops":
            return response
        raise AssertionError(f"unexpected engine method: {method}")
    return call


def test_native_refusal_becomes_advisory_not_a_blocker(monkeypatch):
    monkeypatch.setattr(core_bridge, "call", _engine({"ok": False, "error": "invalid op"}))
    result = preflight.run_preflight("live", "add_features", [])
    assert result["verdict"] == "fail"
    assert {item["severity"] for item in result["advisory_items"]} == {"action"}



def test_complete_native_prediction_still_passes(monkeypatch):
    monkeypatch.setattr(core_bridge, "call", _engine({
        "ok": True, "source_revision": 7,
        "rebuild": {"feature_status": [{"id": "f1", "ok": True}]},
    }))
    result = preflight.run_preflight("live", "add_features", [])
    assert result["verdict"] == "pass"
    assert result["predicted"]["feature_status"] == [
        {"id": "f1", "ok": True, "error": None, "warning": None},
    ]


@pytest.mark.parametrize("reason", ["budget_exceeded", "isolation_unavailable", "isolation_failed"])
def test_incomplete_native_prediction_skips_without_geometry_blame(monkeypatch, reason):
    calls = []
    normal = _engine({"ok": False, "skipped": reason, "still_running": False,
                      "error": "candidate did not finish"})

    def call(method, params):
        calls.append((method, params))
        return normal(method, params)

    monkeypatch.setattr(core_bridge, "call", call)
    result = preflight.run_preflight("live", "add_features", [{
        "op": "add_feature", "feature": {
            "type": "fillet", "params": {"radius": 1.5},
            "refs": {"edges": [{"of": "edge", "near": [-15, 9.5, 184.8], "count": 1},
                               {"of": "edge", "near": [-15, -9.5, 184.8], "count": 1}]},
        },
    }])
    assert result["verdict"] == "skipped"
    assert result["skipped"] == reason
    assert result["still_running"] is False
    assert not result["predicted"]
    assert not result["advisory_items"]
    assert calls[-1][0] == "doc_dry_run_ops"
    assert 0 < calls[-1][1]["budget_ms"] <= preflight._REBUILD_BUDGET_MS
    assert all(method in ("doc_tree", "doc_dry_run_ops") for method, _ in calls)


def test_native_budget_is_remaining_cumulative_time(monkeypatch):
    monkeypatch.setattr(preflight.time, "monotonic", iter([100.0, 103.25]).__next__)
    seen = []
    normal = _engine({"ok": False, "skipped": "budget_exceeded"})

    def call(method, params):
        if method == "doc_dry_run_ops":
            seen.append(params["budget_ms"])
        return normal(method, params)

    monkeypatch.setattr(core_bridge, "call", call)
    assert preflight.run_preflight("live", "add_features", [])["verdict"] == "skipped"
    assert seen == [6750]


def test_expired_budget_never_starts_native_rebuild(monkeypatch):
    monkeypatch.setattr(preflight.time, "monotonic", iter([100.0, 111.0]).__next__)
    normal = _engine({})
    calls = []

    def call(method, params):
        calls.append(method)
        return normal(method, params)

    monkeypatch.setattr(core_bridge, "call", call)
    result = preflight.run_preflight("live", "add_features", [])
    assert result["verdict"] == "skipped"
    assert result["skipped"] == "budget_exceeded"
    assert result["still_running"] is False
    assert set(calls) == {"doc_tree"}

@pytest.mark.parametrize("source_revision, revision_after", [(8, 7), (7, 8)])
def test_concurrent_revision_change_invalidates_clean_prediction(monkeypatch, source_revision, revision_after):
    monkeypatch.setattr(core_bridge, "call", _engine({
        "ok": True, "source_revision": source_revision,
        "rebuild": {"feature_status": []},
    }, revision_after=revision_after))
    result = preflight.run_preflight("live", "add_features", [])
    assert result["verdict"] == "warn"


def test_feature_admission_guard_refuses_before_native_work(monkeypatch):
    monkeypatch.setattr(preflight, "_FEATURE_COUNT_LIMIT", 2)

    def call(method, params):
        if method != "doc_tree":
            pytest.fail("oversized preflight performed native candidate work")
        return {"revision": 7, "tree": {"features": [{"id": f"f{i}"} for i in range(3)]}}

    monkeypatch.setattr(core_bridge, "call", call)
    result = preflight.run_preflight("live", "add_features", [])
    assert result["verdict"] == "skipped"
    assert result["skipped"] == "budget"


@pytest.mark.parametrize("method", ["doc_tree", "doc_dry_run_ops"])
def test_connection_loss_propagates_without_followup_calls(monkeypatch, method):
    failed = False
    normal = _engine({"ok": True, "rebuild": {"feature_status": []}})

    def call(name, params):
        nonlocal failed
        if failed:
            pytest.fail("RPC after connection loss")
        if name == method:
            failed = True
            raise core_bridge.CoreError("lost", code="engine_connection_lost")
        return normal(name, params)

    monkeypatch.setattr(core_bridge, "call", call)
    with pytest.raises(core_bridge.CoreError) as error:
        preflight.run_preflight("live", "add_features", [])
    assert error.value.code == "engine_connection_lost"


def test_override_event_keeps_failed_prediction_auditable():
    events = []
    with patch.object(preflight, "_ledger_append", side_effect=events.append):
        preflight.log_override("live", "add_features", "pf_test", "fail")
    assert events[0]["kind"] == "preflight_overridden"
    assert events[0]["payload"]["preflight_verdict"] == "fail"
