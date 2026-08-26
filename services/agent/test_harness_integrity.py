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

    Resolution is checked through the REAL dispatch path — ``agent_turn._bind_tool``,
    which is what an agent turn actually calls. It has three tiers:
    ``reviewer_exec._resolve_fn`` (battery singleton + curated modules), the full
    MCP server registry, and the two in-process tools that are not MCP callables
    (``ask_user``, ``design_recipe``). Asserting on tier 1 alone under-reports
    resolution and flagged ``design_recipe`` as dead while every real call to it
    succeeded.

    A glob over services/mcp/*_tools.py is not evidence either: org_tools.py
    matched such a glob for months while nothing ever registered it, so
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
            if at._bind_tool(tool) is None:
                dead.setdefault(tool, []).append(rid)
    assert not dead, (
        "role cards advertise tools that resolve nowhere — every call fails "
        f"unrecoverably: { {k: v for k, v in sorted(dead.items())} }"
    )


@pytest.mark.parametrize("tool", [
    "mission_start",       # general_agent's own escalation path
    "channel_send",        # department_manager, aicad.org/1 §18
    "request_resolve",
    "request_raise",       # org_tools registers all four together; workers raise this
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
    why = at._bootstrap_incomplete(
        [{"tool": "ledger_note", "ok": True}], _CAM_ALLOW, "cam_programmer")
    assert why is not None and "toolpath" in why


def test_artifact_gate_clears_once_the_product_tool_ran():
    calls = [{"tool": "ledger_note", "ok": True}, {"tool": "cam_generate", "ok": True}]
    assert at._bootstrap_incomplete(calls, _CAM_ALLOW, "cam_programmer") is None


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


def test_artifact_gate_ignores_a_pack_the_build_pulled_in_mid_turn():
    """A holster build must not be told it owes an FEA study.

    Live 2026-08-04 (replay repro-holster-2): the general lane called
    enable_tools to finish a geometry build, which put run_fea_static on the
    allow-set. The simulation gate then fired on a turn that had produced six
    successful add_features batches and a render, flipping `wrote` to False —
    so a built part came back `status: blocked`, `no study was run —
    run_fea_static/run_fea_modal/run_cfd_flow never ran`. These gates describe
    what a ROLE exists to produce, not every pack in scope.
    """
    allow = {"new_part", "add_features", "render_part", "ledger_note",
             "run_fea_static", "run_fea_modal", "run_cfd_flow"}
    built = [{"tool": "new_part", "ok": True},
             {"tool": "add_features", "ok": True},
             {"tool": "render_part", "ok": True}]
    assert at._bootstrap_incomplete(built, allow, "general_agent") is None


def test_artifact_gate_still_fires_when_nothing_was_delivered():
    """The gate's real job survives: no product in ANY domain, so it holds."""
    allow = {"ledger_note", "ledger_get", "run_fea_static", "run_cfd_flow"}
    why = at._bootstrap_incomplete(
        [{"tool": "ledger_note", "ok": True}], allow, "sim_engineer")
    assert why is not None and "no study was run" in why

def test_general_agent_may_inspect_existing_sim_without_running_new_study():
    allow = {"describe_results", "run_fea_static", "run_cfd_flow"}
    calls = [{"tool": "describe_results", "ok": True, "result": {"study": "existing"}}]
    assert at._bootstrap_incomplete(calls, allow, "general_agent") is None


# ---------------------------------------------------------------------------
# 4. A transient provider hiccup must not burn the node
# ---------------------------------------------------------------------------

def test_provider_retry_requires_structured_retryable_disposition():
    """A provider_error string alone cannot prove replay is safe."""
    from services.dag import node_runner as nr
    assert "provider_error" not in nr._RETRY_REASONS
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


# ---------------------------------------------------------------------------
# 6. WI-12 auto-checkpoint must never permanently lock a document
# ---------------------------------------------------------------------------

def _failing_checkpoint(doc_id, trigger_tool):
    return {"ok": False, "error": "engine cannot snapshot this doc"}


def _blocked(tool, doc="doc-wedged"):
    return gr.pre_dispatch_checkpoint(
        tool, {"doc_id": doc}, checkpoint_fn=_failing_checkpoint)


def test_checkpoint_failure_blocks_the_first_destructive_attempt():
    gr.reset_checkpoint_failures()
    v = _blocked("delete_feature")
    assert v is not None and not v.ok
    # ...and the message must say how to get unstuck.
    assert "UNPROTECTED" in v.message or "unprotected" in v.message


def test_repeat_attempt_proceeds_rather_than_locking_the_doc_forever():
    """WI-12 was the only HARD guard with no waiver of any kind. Once
    doc_checkpoint started failing, every destructive call on that doc was
    refused forever and the model held no tool that could repair it."""
    gr.reset_checkpoint_failures()
    assert _blocked("delete_feature") is not None      # warned once
    assert _blocked("delete_feature") is None          # then allowed through
    assert _blocked("shell") is None                   # doc-wide, not per-tool


def test_close_part_is_never_blocked_by_a_failed_checkpoint():
    """close_part is in DESTRUCTIVE_TOOLS, so a failing checkpoint also blocked
    the only way to escape the wedged doc."""
    gr.reset_checkpoint_failures()
    assert _blocked("close_part") is None


def test_successful_checkpoint_resets_the_failure_memo():
    gr.reset_checkpoint_failures()
    assert _blocked("delete_feature") is not None
    ok = gr.pre_dispatch_checkpoint(
        "delete_feature", {"doc_id": "doc-wedged"},
        checkpoint_fn=lambda d, t: {"ok": True})
    assert ok is None
    # Memo cleared → a later failure warns again instead of silently passing.
    assert _blocked("delete_feature") is not None


def test_non_destructive_tools_are_untouched():
    gr.reset_checkpoint_failures()
    assert gr.pre_dispatch_checkpoint(
        "measure_part", {"doc_id": "d1"},
        checkpoint_fn=_failing_checkpoint) is None


# ---------------------------------------------------------------------------
# 7. A guardrail-blocked finish must not be reported as "no output"
# ---------------------------------------------------------------------------

def test_guardrail_blocked_finish_is_not_reported_as_no_code(monkeypatch):
    """last_reason was initialised to _NOCODE and never touched when a HARD
    guardrail rejected a finish, so a turn that tried to finish repeatedly
    reported "Model produced no valid tool call or completion" — false, and it
    hid which guardrail fired."""
    monkeypatch.setattr(
        at, "_done_guardrail_block",
        lambda handoff, ledger, actor, calls: "HARD guardrail: run verify_battery first.")

    class _Prov:
        name = model = "mock"

        def complete(self, system, messages, max_tokens=8000):
            return '```json\n{"done":true,"handoff":{"status":"done","summary":"x"}}\n```'

    res = at.run_turn("do it", _Prov(), name="t", max_tool_calls=2)
    assert res.success is False
    assert res.stop_reason == "guardrail_blocked", res.stop_reason
    detail = (res.result or {}).get("error_detail") or ""
    assert "no valid tool call" not in detail
    assert "verify_battery" in ((res.result or {}).get("guardrail_block") or "")

def test_productive_work_cannot_bypass_a_hard_finish_guard(monkeypatch):
    """A successful tool call must not turn a rejected finish into synthetic success."""
    monkeypatch.setattr(
        at, "_done_guardrail_block",
        lambda handoff, ledger, actor, calls: "HARD guardrail: run verify_battery first.")

    class _Prov:
        name = model = "mock"

        def __init__(self):
            self.calls = 0

        def complete(self, system, messages, max_tokens=8000):
            self.calls += 1
            if self.calls == 1:
                return '```json\n{"tool":"ledger_note","args":{"section":"decisions"}}\n```'
            return '```json\n{"done":true,"handoff":{"status":"done","summary":"x"}}\n```'

    res = at.run_turn(
        "do it",
        _Prov(),
        name="t",
        allowed_tools=["ledger_note"],
        max_tool_calls=2,
        tool_dispatch=lambda tool, args: {"ok": True},
    )
    assert res.success is False
    assert res.stop_reason == "guardrail_blocked", res.stop_reason
    assert res.result.get("auto_complete") is not True
    assert "verify_battery" in ((res.result or {}).get("guardrail_block") or "")



def test_delegate_rejects_unknown_role_before_allocating_a_child():
    dispatch = MissionDispatch(run_id="invalid-role-test")
    out = dispatch.delegate(
        role="general-agent",
        brief="build a bracket",
        budget_tokens=8000,
    )
    assert out["ok"] is False
    assert out["executed"] is False
    assert out["reason"] == "invalid_role"
    assert "general_agent" in out["suggested_roles"]
def test_guardrail_blocked_is_retryable_not_escalate():
    from services.dag import node_runner as nr
    assert "guardrail_blocked" in nr._RETRY_REASONS


def test_done_guardrail_exception_fails_closed(monkeypatch):
    from services.agent import guardrails
    monkeypatch.setattr(
        guardrails,
        "check_done",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("broken guard")),
    )
    block = at._done_guardrail_block({}, None, "test", [])
    assert "HARD guardrail failed" in block
    assert "broken guard" in block


def test_sketch_role_handoff_skips_only_orphan_product_guard(monkeypatch):
    from services.agent import guardrails
    captured = {}

    class _Verdict:
        ok = True

    def _check(*args, **kwargs):
        captured.update(kwargs)
        return _Verdict()

    monkeypatch.setattr(guardrails, "check_done", _check)
    assert at._done_guardrail_block({}, None, "sketcher", []) is None
    assert captured["skip"] == {"orphan_sketch"}


def test_failed_delegate_with_successful_child_calls_counts_as_partial_work():
    call = {
        "tool": "delegate",
        "ok": False,
        "result": {
            "executed": True,
            "status": "failed",
            "error": "provider stopped after tool work",
            "result": {
                "calls": [{"tool": "add_features", "ok": True}],
                "stop_reason": "provider_error",
            },
        },
    }
    assert at._delegate_actually_ran(call) is True

# ---------------------------------------------------------------------------
# 8. The universal entry must always return a handoff
# ---------------------------------------------------------------------------

def test_run_linear_role_turn_synthesises_a_missing_handoff(monkeypatch):
    """Every online top-level run lands here via agent_admin_turn, but it
    returned run_turn's raw result — so a turn that did real tool work without a
    formal envelope reached the caller with no "handoff" key at all."""
    from services.agent import mission_dispatch as md

    class _Res:
        success = True
        stop_reason = "confirmed"
        result = {"calls": [{"tool": "fs_list", "ok": True}], "tool_calls": 1}

    monkeypatch.setattr(at, "run_turn", lambda *a, **k: _Res())
    out = md.run_linear_role_turn("tidy up", "general_agent", provider=object())
    handoff = (out.get("result") or {}).get("handoff")
    assert isinstance(handoff, dict) and handoff.get("status") == "done"
    assert handoff["outputs"]["tool_calls"] == 1


def test_linear_turn_keeps_a_real_handoff_untouched(monkeypatch):
    from services.agent import mission_dispatch as md

    real = {"status": "done", "summary": "authored by the model"}

    class _Res:
        success = True
        stop_reason = "confirmed"
        result = {"handoff": real, "calls": []}

    monkeypatch.setattr(at, "run_turn", lambda *a, **k: _Res())
    out = md.run_linear_role_turn("x", "general_agent", provider=object())
    assert (out.get("result") or {}).get("handoff") is real

def test_linear_turn_reports_save_failure_as_partial(monkeypatch):
    from services.agent import mission_dispatch as md
    from services.mcp import cad_common

    class _Res:
        success = True
        stop_reason = "confirmed"
        result = {
            "handoff": {"status": "done", "summary": "built", "outputs": {}},
            "calls": [{"tool": "add_features", "ok": True}],
        }

    monkeypatch.setattr(at, "run_turn", lambda *args, **kwargs: _Res())
    monkeypatch.setattr(
        cad_common,
        "save_touched_docs",
        lambda: {"saved": [], "errors": ["doc_save failed for d1"]},
    )
    out = md.run_linear_role_turn(
        "build it",
        "general_agent",
        provider=object(),
        tool_dispatch=lambda tool, args: {"ok": True},
    )
    assert out["success"] is False
    assert out["stop_reason"] == "persistence_failed"
    assert out["result"]["handoff"]["status"] == "partial"
    assert "doc_save failed" in out["result"]["error_detail"]


# ---------------------------------------------------------------------------
# 9. Nested delegation must actually nest
# ---------------------------------------------------------------------------

def test_nested_delegate_attaches_to_the_calling_node_not_the_root():
    """A model cannot know its own dg_<hex> id and nothing injected it, so every
    spawn arrived with parent_id=None and request_delegation fell back to the
    tree root — pinning the whole chain at depth 1, which made
    max_subagent_depth/soft_depth_limit unreachable via the only tool a model
    has to spawn."""
    md = MissionDispatch(run_id="r_depth")
    seen = {}

    class _Tree:
        root_id = "root"
        _nodes = {"root": type("N", (), {"depth": 0, "role": "orchestrator"})()}

    md.delegation_tree = _Tree()

    def _spy(role, brief, budget_tokens, **kw):
        seen["parent_id"] = kw.get("parent_id")
        return {"id": "dg_child", "status": "pending", "role": role}

    import services.agent.delegation as dlg
    real = dlg.delegate
    dlg.delegate = _spy
    try:
        # No node in flight → root (unchanged top-level behaviour).
        md.delegate(role="model3d", brief="b", budget_tokens=4000)
        assert seen["parent_id"] is None
        # Inside node dg_parent's turn → attaches to dg_parent.
        md._current_node_id = "dg_parent"
        md.delegate(role="sketch2d", brief="b", budget_tokens=4000)
        assert seen["parent_id"] == "dg_parent"
        # An explicit parent_id from the caller still wins.
        md.delegate(role="sketch2d", brief="b", budget_tokens=4000,
                    parent_id="dg_explicit")
        assert seen["parent_id"] == "dg_explicit"
    finally:
        dlg.delegate = real
