"""Harness integrity: the model must never be told to do the impossible.

Every defect this file guards against is the same shape — the harness advertises
a capability, dispatches it, and it cannot succeed, so no amount of model skill
or retrying recovers. Found live in one model3d run plus a four-way audit:

  * a tool named on a role card that resolves nowhere;
  * a tool-call budget smaller than the role's own mandated chain;
  * a "done" that the harness accepts with zero artifacts produced;
  * a transient provider hiccup that hard-escalates on first occurrence;
  * a HARD guardrail whose remedy names a tool the blocked role does not hold.
"""
from __future__ import annotations

import glob
import os

import pytest

from services.agent import agent_turn as at
from services.agent import guardrails as gr
from services.agent import reviewer_exec as rx
from services.agent import roles as R
from services.agent import tool_budget as tb
from services.agent.mission_dispatch import MissionDispatch

_ROLES_GLOB = os.path.join(os.path.dirname(__file__), "roles", "*.yaml")

# Resolved by MissionDispatch.resolve() (run-scoped), not the global registry.
_MISSION_SCOPED = set(MissionDispatch.TOOLS) | {
    "coordinate_claim", "coordinate_release", "coordinate_progress",
    "coordinate_board", "corporate_channel_post", "corporate_channel_read",
    "corporate_chart", "corporate_spawn_worker", "corporate_hold_meeting",
    "corporate_record_completion", "corporate_activate",
}
# Bound inside agent_turn itself rather than the MCP registry.
_META_TOOLS = {"enable_tools", "list_tool_categories", "advise", "done"}


def _role_ids() -> list[str]:
    return sorted(os.path.basename(p)[:-5] for p in glob.glob(_ROLES_GLOB))


# ---------------------------------------------------------------------------
# 1. No role card may advertise a tool that cannot be dispatched
# ---------------------------------------------------------------------------

def test_every_role_card_tool_actually_resolves():
    """The anti-drift gate for the whole 'advertised but dead' bug class.

    Resolution is checked through the REAL dispatch path (reviewer_exec).
    A glob over services/mcp/*_tools.py is not evidence: org_tools.py matched
    such a glob for months while nothing ever registered it, so
    channel_send/request_resolve/council_convene were advertised to
    department_manager and failed every call. Same for general_agent's own
    mission_start, cam_programmer's cam_audit, and orchestrator's dag_run.
    """
    dead: dict[str, list[str]] = {}
    for rid in _role_ids():
        try:
            card = R.load_role(rid)
        except Exception:  # noqa: BLE001 — card-loading is covered by test_roles
            continue
        for tool in card.concrete_tools:
            if tool in _MISSION_SCOPED or tool in _META_TOOLS:
                continue
            if rx._resolve_fn(tool) is None:
                dead.setdefault(tool, []).append(rid)
    assert not dead, (
        "role cards advertise tools that resolve nowhere — every call fails "
        f"unrecoverably: { {k: v for k, v in sorted(dead.items())} }"
    )


@pytest.mark.parametrize("tool", [
    "mission_start",       # general_agent's own escalation path
    "channel_send",        # department_manager, aicad.org/1 §18
    "request_resolve",
    "council_convene",
    "bom_price",           # sourcing_specialist
    "dag_run",             # orchestrator
    "dag_status",
    "cam_audit",           # cam_programmer's mandated final gate
    "render_sketch",       # sketch2d
    "match_part_number",   # re_datasheet_reader
    "advisory_dispute",    # feature_architect
])
def test_previously_unreachable_tools_resolve(tool):
    assert rx._resolve_fn(tool) is not None


# ---------------------------------------------------------------------------
# 2. Budgets must fit the role's mandated chain
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("role,allowed", [
    ("cam_programmer", ["ledger_get", "cam_generate", "cam_post"]),
    ("sim_engineer", ["ledger_get", "run_fea_static"]),
    ("drawing_detailer", ["ledger_get", "make_drawing", "export_drawing"]),
    ("model3d", ["ledger_get", "new_part", "add_features"]),
    ("ecad_engineer", ["ledger_get", "ecad_new_schematic", "ecad_run_drc"]),
])
def test_producer_roles_clear_the_old_twelve_call_ceiling(role, allowed):
    """max(4, min(12, granted // 2000)) capped every producer at 12 calls no
    matter how large the grant, while cam/drawing/ecad chains need 15-16."""
    derived = max(4, min(12, 12000 // 2000))  # the old computation → 6
    assert tb.floor_for(role, allowed, None, base=derived) >= 16


def test_non_producer_roles_keep_their_derived_budget():
    assert tb.floor_for("reviewer", ["ledger_get", "design_review"],
                        None, base=6) == 6


def test_producer_detection_survives_a_role_rename():
    """Tool grant is the primary signal precisely because names drift."""
    assert tb.is_producer_role("some_brand_new_card", ["cam_generate"])
    assert not tb.is_producer_role("some_brand_new_card", ["ledger_get"])


# ---------------------------------------------------------------------------
# 3. "done" requires an artifact, not a ledger row
# ---------------------------------------------------------------------------

_CAM_ALLOW = {"ledger_note", "ledger_get", "cam_new_job", "cam_generate", "cam_post"}


def test_ledger_only_turn_is_not_cam_product_success():
    """ledger_note is in _PRODUCTIVE_TOOLS (it IS the deliverable for PLAN
    nodes), so `wrote` was True and a CAM turn that only wrote a ledger row
    auto-completed as status=done with no toolpath in existence."""
    why = at._bootstrap_incomplete([{"tool": "ledger_note", "ok": True}], _CAM_ALLOW)
    assert why is not None and "toolpath" in why


def test_artifact_gate_clears_once_the_product_tool_ran():
    calls = [{"tool": "ledger_note", "ok": True}, {"tool": "cam_generate", "ok": True}]
    assert at._bootstrap_incomplete(calls, _CAM_ALLOW) is None


def test_artifact_gate_does_not_fire_for_roles_without_those_tools():
    """A reviewer writing only ledger rows is doing its job."""
    assert at._bootstrap_incomplete(
        [{"tool": "ledger_note", "ok": True}],
        {"ledger_note", "ledger_get", "design_review"}) is None


def test_geometry_bootstrap_rule_is_unchanged():
    geo = {"ledger_note", "new_part", "add_features"}
    assert at._bootstrap_incomplete(
        [{"tool": "new_part", "ok": True}], geo) is not None   # mega-6 still holds
    assert at._bootstrap_incomplete(
        [{"tool": "new_part", "ok": True},
         {"tool": "add_features", "ok": True}], geo) is None


# ---------------------------------------------------------------------------
# 4. A transient provider hiccup must not burn the node
# ---------------------------------------------------------------------------

def test_transient_provider_error_retries_rather_than_escalating():
    """agent_turn rewrites a 429/5xx from "failed" to "provider_error" so the
    GUI does not mislabel it as budget exhaustion — which made the mega-5
    retryable rescue (matching the literal "failed") unreachable, so every
    transient hiccup escalated on its FIRST occurrence with retry budget
    still untouched."""
    from services.dag import node_runner as nr
    assert "provider_error" in nr._RETRY_REASONS
    assert "phase_step_cap" in nr._RETRY_REASONS
    # Genuinely terminal reasons must still escalate.
    assert "provider_auth" not in nr._RETRY_REASONS
    assert "no_code" not in nr._RETRY_REASONS


# ---------------------------------------------------------------------------
# 5. A HARD block must name an action the blocked role can actually take
# ---------------------------------------------------------------------------

def test_interference_block_names_a_reachable_action():
    """model3d/feature_modeler are at the 14-tool cap and hold no
    check_interference, and a bare verify_battery(doc_id) runs only subsecond
    checks while check_interference is cost-class 'seconds' — so the old
    "run check_interference" remedy was unsatisfiable for the two flagship
    build roles. The message must name the call they can actually make."""
    for rid in ("model3d", "feature_modeler"):
        card = R.load_role(rid)
        assert "check_interference" not in card.concrete_tools
        assert "verify_battery" in card.concrete_tools
    assert "checks=['check_interference']" in gr.MSG_INTERFERENCE


def test_refs_block_still_carries_recovery_context():
    v = gr.pre_dispatch_refs(
        "add_features",
        {"doc_id": "d1", "ops": [{"op": "delete_feature", "id": "Ghost"}]},
        known=({"f1"}, {"Base"}))
    assert v is not None and "Base" in v.message
