"""Real policy schema, denial, viewer-safety, and live-harvest completeness tests.

No daemon is started, and desktop sessions are never touched.
"""
from __future__ import annotations

import copy
import json

import pytest

from scripts import cell_methods


@pytest.fixture
def policy():
    return cell_methods.load_policy()


def test_every_native_worker_delegated_and_lifecycle_method_is_classified(policy):
    methods = cell_methods.harvest_registered_methods()
    cell_methods.validate_policy(policy, methods)
    assert set(policy) == methods
    assert {"hello", "attach_doc", "edit_hold", "agent_spawn", "doc_snapshot",
            "principal_revoke", "mfg_device_claim", "fw_emulate_renode",
            "hydraulic_simulate", "material_get", "agent_admin_turn"} <= methods


@pytest.mark.parametrize("method", [
    "doc_new_unclassified", "worker_unclassified", "lifecycle_unclassified",
    "agent_read_unclassified", "sim_read_unclassified", "doc_render_unclassified",
])
def test_new_method_requires_explicit_classification(policy, method):
    assert method not in policy
    methods = set(policy) | {method}
    with pytest.raises(cell_methods.PolicyError, match="missing=.*" + method):
        cell_methods.validate_policy(policy, methods)
    policy[method] = {"cell": "refuse", "right": "write", "scope": "workspace"}
    cell_methods.validate_policy(policy, methods)


def test_deleted_method_is_not_silently_retained(policy):
    with pytest.raises(cell_methods.PolicyError, match="stale=.*doc_tree"):
        cell_methods.validate_policy(policy, set(policy) - {"doc_tree"})


def test_unknown_methods_have_no_prefix_or_wildcard_entry(policy):
    for method in ("agent_invented", "sim_invented", "doc_render_invented", "invented"):
        assert policy.get(method) is None
    assert not ({"*", "agent_*", "sim_*", "doc_*"} & policy.keys())


@pytest.mark.parametrize("value", [None, [], {}, True, "policy"])
def test_non_flat_or_empty_schema_is_rejected(value):
    with pytest.raises(cell_methods.PolicyError, match="nonempty flat"):
        cell_methods.validate_schema(value)


@pytest.mark.parametrize("entry", [
    None, [], "read", {},
    {"cell": "allow", "right": "read"},
    {"cell": "allow", "right": "read", "scope": "workspace", "extra": True},
    {"cell": "allow", "right": "viewer", "scope": "workspace"},
    {"cell": "Allow", "right": "read", "scope": "workspace"},
    {"cell": "allow", "right": "read", "scope": "global"},
    {"cell": "allow", "right": True, "scope": "workspace"},
    {"cell": ["allow"], "right": "read", "scope": "workspace"},
])
def test_malformed_entry_is_rejected(entry):
    with pytest.raises(cell_methods.PolicyError):
        cell_methods.validate_schema({"doc_tree": entry})


@pytest.mark.parametrize("method", ["*", "doc_*", "DocTree", "doc tree", "doc.tree", ""])
def test_nonliteral_method_keys_are_rejected(method):
    with pytest.raises(cell_methods.PolicyError, match="invalid method"):
        cell_methods.validate_schema({method: {"cell": "allow", "right": "read", "scope": "workspace"}})


@pytest.mark.parametrize("text", [
    '{"doc_tree":{"cell":"allow","right":"read","scope":"workspace"},"doc_tree":{}}',
    '{"doc_tree":{"cell":"allow","cell":"refuse","right":"read","scope":"workspace"}}',
])
def test_duplicate_json_keys_are_rejected(tmp_path, text):
    path = tmp_path / "policy.json"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(cell_methods.PolicyError, match="duplicate JSON key"):
        cell_methods.load_policy(path)


def test_unsafe_registered_methods_are_all_refused(policy):
    unsafe = {method for method in policy
              if method in cell_methods.REFUSED_EXACT
              or method.startswith(cell_methods.REFUSED_PREFIXES)}
    assert {"agent_spawn", "agent_stop", "agent_list", "daemon_status", "recovery_open",
            "mfg_device_list", "fw_emulate_renode", "fw_serial_open", "box_sync",
            "subscription_status", "worker_control_info", "worker_activity", "cell_census",
            "cell_drain", "principal_update"} <= unsafe
    assert all(policy[method]["cell"] == "refuse" for method in unsafe)
    assert {"start_capture_session", "stop_capture_session"} <= unsafe


@pytest.mark.parametrize("method", [
    "agent_spawn", "agent_stop", "agent_list", "worker_control_info", "daemon_status",
    "recovery_open", "mfg_device_list", "fw_emulate_renode", "fw_serial_open",
    "device_scan", "serial_read", "claude_cli", "codex_cli", "provider_pool_add",
    "pool_status", "subscription_status", "box_sync", "principal_update",
    "capture_start", "capture_status", "start_capture_session", "stop_capture_session",
    "cell_census", "cell_drain", "worker_activity",
])
def test_unsafe_allow_entry_is_rejected(method):
    with pytest.raises(cell_methods.PolicyError, match="unsafe method must be refused"):
        cell_methods.validate_schema({method: {"cell": "allow", "right": "read", "scope": "actor"}})


@pytest.mark.parametrize("method", sorted(cell_methods.NON_READ_EXACT))
def test_viewer_cannot_mutate_or_execute(policy, method):
    assert policy[method]["right"] != "read"
    modified = copy.deepcopy(policy)
    modified[method]["right"] = "read"
    with pytest.raises(cell_methods.PolicyError, match="cannot require read"):
        cell_methods.validate_schema(modified)


@pytest.mark.parametrize("method", [
    "doc_tree", "doc_query", "doc_bbox", "doc_activate", "doc_rebuild",
    "doc_open",
    "doc_render_png", "doc_dry_run_ops", "ecad_tree", "drawing_get",
    "eval_mass", "sim_study_list", "piping_route", "piping_edit", "material_get",
])
def test_genuine_cad_reads_remain_viewer_accessible(policy, method):
    assert policy[method] == {"cell": "allow", "right": "read", "scope": "workspace"}


def test_viewer_file_open_does_not_grant_import_or_document_mutation(policy):
    assert policy["doc_open"] == {
        "cell": "allow", "right": "read", "scope": "workspace",
    }
    for method in ("import_file", "doc_apply_ops", "doc_save", "doc_new", "doc_set_meta"):
        assert policy[method]["right"] != "read"


@pytest.mark.parametrize("method", [
    "agent_admin_turn", "agent_cancel", "agent_steer", "agent_answer",
    "agent_handles_new",
    "chudcode_branch_session", "chudcode_close_session", "chudcode_set_key",
    "chudcode_session_stats", "memory_recall", "memory_remember", "dag_status",
    "subagent_transcript", "reveng_start_session", "sim_cancel",
])
def test_private_methods_are_actor_scoped(policy, method):
    assert policy[method]["scope"] == "actor"
    assert policy[method]["cell"] == "allow"


@pytest.mark.parametrize("method", [
    "agent_cancel", "sim_cancel", "dag_abort", "subagent_terminate",
    "agent_steer", "agent_answer", "chudcode_close_session",
])
def test_control_policy_never_grants_workspace_wide_actor_access(policy, method):
    assert policy[method]["scope"] == "actor"
    assert policy[method]["right"] != "read"
    assert set(policy[method]) == {"cell", "right", "scope"}
    modified = copy.deepcopy(policy)
    modified[method]["control_delegate"] = {
        "actor_key": "manager", "right": "manager", "action": "cancel",
    }
    with pytest.raises(cell_methods.PolicyError, match="exactly cell, right, scope"):
        cell_methods.validate_schema(modified)


def test_new_delegated_dispatcher_requires_source_review(monkeypatch):
    original = cell_methods._source

    def source(relative):
        text = original(relative)
        if relative == "core/api/libchudware.cpp":
            text += '\nchudware::unreviewed::call(m, params, result);\n'
        return text

    monkeypatch.setattr(cell_methods, "_source", source)
    with pytest.raises(cell_methods.PolicyError, match="dispatcher sources need explicit review"):
        cell_methods.harvest_registered_methods()


def test_generate_never_fills_missing_entries(policy, tmp_path, monkeypatch, capsys):
    path = tmp_path / "policy.json"
    raw = json.dumps(policy)
    path.write_text(raw, encoding="utf-8")
    monkeypatch.setattr(cell_methods, "harvest_registered_methods", lambda: set(policy) | {"new_method"})
    assert cell_methods.main(["--policy", str(path), "--generate"]) == 1
    assert "new_method" in capsys.readouterr().err
    assert path.read_text(encoding="utf-8") == raw


def test_generate_preserves_all_explicit_classifications(policy, tmp_path, monkeypatch):
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(policy), encoding="utf-8")
    monkeypatch.setattr(cell_methods, "harvest_registered_methods", lambda: set(policy))
    assert cell_methods.main(["--policy", str(path), "--generate"]) == 0
    assert cell_methods.load_policy(path) == policy
    assert path.read_text(encoding="utf-8").endswith("\n")
