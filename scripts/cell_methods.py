#!/usr/bin/env python3
"""Validate/canonicalize the explicit tenant-cell RPC policy (stdlib only).

No method is inferred from a prefix, and generation never classifies a new method.
Add an explicit entry to contracts/cell_methods.json before canonicalizing it.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

try:
    from scripts import parity_audit
except ModuleNotFoundError:
    import parity_audit

REPO = Path(parity_audit.REPO)
POLICY_PATH = REPO / "contracts/cell_methods.json"
# These are the dispatchers called by libchudware after its own literal handlers.
# Keep the mapping explicit: an added delegated dispatcher must be reviewed too.
DELEGATED_SOURCES = {
    "sourcing": "core/sourcing/sourcing.cpp",
    "parts": "core/parts/place.cpp",
    "hydraulic": "core/hydraulic/hydraulic.cpp",
    "piping": "core/piping/piping.cpp",
    "structural": "core/structural/structural_rpc.cpp",
    "tolerance": "core/tolerance/stackup.cpp",
    "mechanical": "core/mechanical/mechanical.cpp",
    "materials": "core/materials/materials.cpp",
    "dfm": "core/dfm/process_profiles.cpp",
    "firmware": "core/firmware/firmware_rpc.cpp",
}
LIFECYCLE_SOURCES = (
    "core/hostd/server.cpp",
    "core/hostd/doc_registry.cpp",
    "core/hostd/child_supervisor.cpp",
    "core/hostd/child_supervisor_win.cpp",
    "core/hostd/mfg_lane.cpp",
)
DISPATCH_RE = re.compile(r'\bmethod\s*(?:==|!=)\s*"([a-z_][a-z0-9_]*)"')
DELEGATE_RE = re.compile(r'chudware::([a-z_]+)::(?:call|place_call)\(m\s*,')
METHOD_RE = re.compile(r'[a-z_][a-z0-9_]*\Z')
FIELDS = {"cell", "right", "scope"}
VALUES = {
    "cell": {"allow", "refuse"},
    "right": {"read", "write", "run", "manage"},
    "scope": {"workspace", "actor"},
}
# Safety assertions for the validator, NOT a runtime policy/prefix fallback.
REFUSED_PREFIXES = (
    "daemon_", "cell_", "recovery_", "mfg_", "fw_", "firmware_", "device_", "serial_",
    "subscription_", "provider_pool_", "pool_", "box_", "claude_cli", "codex_cli",
    "capture_",
)
REFUSED_EXACT = {
    "agent_spawn", "agent_stop", "agent_list", "worker_control_info", "worker_activity",
    "__hostd_test_stall", "agent_test_stall",
    "principal_update", "principal_revoke", "agent_run_machine",
    "start_capture_session", "stop_capture_session",
}
# DocRegistry's read-only predicate protects document revisions, not workspace or
# actor state. Its broad agent_/sim_/export/prof_ prefixes are not viewer rights.
NON_READ_EXACT = {
    "doc_save", "doc_export_step", "doc_clear_history", "doc_compile_cpp",
    "doc_checkpoint", "doc_new", "doc_close", "doc_discard",
    "doc_set_display_quality", "doc_set_explode_state", "doc_file_operations",
    "import_file",
    "doc_apply_ops", "doc_undo", "doc_redo", "doc_revert", "doc_push_pull",
    "doc_refresh_derived", "doc_set_meta", "doc_set_name", "doc_solve_mates",
    "doc_dep_notify", "doc_dep_register", "doc_dep_unregister",
    "config_activate", "config_add", "config_delete", "config_set_override",
    "config_set_suppression", "sketch_enter", "sketch_exit", "sketch_drag",
    "sketch_drag_commit", "sketch3d_drag", "sketch_solve", "sketch_auto_constrain",
    "sketch_project_entities", "sketch_project_ref", "edit_hold", "edit_release",
    "ecad_apply_ops", "ecad_undo", "ecad_redo", "ecad_swap_doc", "ecad_autoroute",
    "ecad_fill_zones", "ecad_route_diff_pair", "ecad_update_from_sch",
    "cam_apply_ops", "cam_undo", "cam_redo", "cam_generate", "cam_post", "cam_save",
    "drawing_export", "drawing_save_standalone", "ecad_export", "ecad_save",
    "export_file", "export_drawing", "export2d_drawing", "export_flat_dxf",
    "agent_run_event", "agent_run_events_batch", "agent_cancel", "agent_steer",
    "agent_answer", "agent_admin_turn", "agent_run", "agent_run_v2",
    "agent_handles_new",
    "agent_run_design", "agent_run_micro", "agent_mission_v1", "agent_complete",
    "agent_ask_v2", "agent_answer_question", "agent_clarify", "agent_clarify_v1",
    "agent_pm_assist", "agent_pm_assist_v1", "agent_simulate_v1", "run_model",
    "sim_study_add", "sim_study_edit", "sim_study_delete", "sim_cancel",
    "sim_static_overlay", "sim_modal_overlay", "sim_mesh", "simulate_motion",
    "prof_start", "prof_stop", "prof_report", "prof_tree", "prof_phases",
    "prof_phases_tree", "prof_contention", "prof_target_push", "prof_target_pop",
    "part_place", "vendor_part_fetch", "vendor_part_register_local_cad",
    "parts_search_live_start", "cad_graph_config_set", "piping_fitting_k_cfd_start",
    "piping_fitting_k_cfd_cancel", "chudcode_set_key", "memory_remember",
    # These query-shaped handlers open/seed Vault or export optional thumbnails.
    "plm_branch_list", "plm_compare", "plm_history", "plm_item_list", "plm_overview",
    "plm_settings_get", "plm_status", "plm_timeline", "plm_version_graph",
    "plm_version_list", "plm_where_used",
}


class PolicyError(ValueError):
    """Malformed, incomplete, or unsafe cell policy."""


def _source(relative: str) -> str:
    return parity_audit._read(str(REPO / relative))


def harvest_registered_methods() -> set[str]:
    """Parity's live native/worker harvest plus delegated and hostd lifecycle RPCs."""
    native, _ = parity_audit.harvest_native_rpc()
    methods = set(native)
    for names in parity_audit.harvest_python_rpc().values():
        methods.update(names)
    delegated = set(DELEGATE_RE.findall(_source("core/api/libchudware.cpp")))
    if delegated != set(DELEGATED_SOURCES):
        raise PolicyError("delegated dispatcher sources need explicit review: "
                          f"added={sorted(delegated - DELEGATED_SOURCES.keys())}, "
                          f"removed={sorted(DELEGATED_SOURCES.keys() - delegated)}")
    for relative in (*DELEGATED_SOURCES.values(), *LIFECYCLE_SOURCES):
        names = set(DISPATCH_RE.findall(_source(relative)))
        if not names:
            raise PolicyError(f"empty RPC harvest from {relative}; dispatcher format changed")
        methods.update(names)
    return methods


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise PolicyError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def load_policy(path: Path = POLICY_PATH) -> dict:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"),
                          object_pairs_hook=_unique_object)
    except (OSError, json.JSONDecodeError) as exc:
        raise PolicyError(f"cannot read cell policy: {exc}") from exc


def validate_schema(policy: object) -> None:
    if not isinstance(policy, dict) or not policy:
        raise PolicyError("policy must be a nonempty flat method object")
    for method, entry in policy.items():
        if not isinstance(method, str) or not METHOD_RE.fullmatch(method):
            raise PolicyError(f"invalid method key: {method!r}")
        if not isinstance(entry, dict) or set(entry) != FIELDS:
            raise PolicyError(f"{method}: entry must have exactly cell, right, scope")
        for field, allowed in VALUES.items():
            value = entry[field]
            if not isinstance(value, str) or value not in allowed:
                raise PolicyError(f"{method}: invalid {field}: {value!r}")
        if (method in REFUSED_EXACT or method.startswith(REFUSED_PREFIXES)) and entry["cell"] != "refuse":
            raise PolicyError(f"{method}: unsafe method must be refused")
        if (method in NON_READ_EXACT or method.startswith("sim_run_")) and entry["right"] == "read":
            raise PolicyError(f"{method}: mutating/executing method cannot require read")


def validate_policy(policy: object, methods: set[str] | None = None) -> None:
    validate_schema(policy)
    registered = harvest_registered_methods() if methods is None else methods
    missing = registered - policy.keys()
    stale = policy.keys() - registered
    if missing or stale:
        raise PolicyError("explicit cell classifications required: "
                          f"missing={sorted(missing)}, stale={sorted(stale)}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", type=Path, default=POLICY_PATH)
    parser.add_argument("--generate", action="store_true",
                        help="canonicalize existing explicit classifications; never invent entries")
    args = parser.parse_args(argv)
    try:
        policy = load_policy(args.policy)
        validate_policy(policy)
        if args.generate:
            args.policy.write_text(json.dumps(policy, indent=2, sort_keys=True) + "\n",
                                   encoding="utf-8")
        print(f"cell policy: {len(policy)} explicitly classified methods")
    except (PolicyError, parity_audit.SourceError, OSError) as exc:
        print(f"cell policy: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
