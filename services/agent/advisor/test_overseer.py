"""Tests for the overseer plan-audit engine + change-request routing.

Covers:
  * trigger debounce (one audit per trigger point);
  * verdict validation — malformed aicad.audit/1 is rejected, not accepted;
  * repeat-blocker escalation fires at EXACTLY 2 matching blockers;
  * change-request mapping on a fixture ledger ("make the bore fit a 25mm shaft"
    → bore dim + fit recalc + interference recheck, and NOTHING else);
  * verdicts land as advisory items marked source.kind == "llm";
  * token logging on every audit record.

Offline discipline: no real LLM, no network, no core.  The provider is a
:class:`_CannedProvider` returning fixed audit JSON — real code paths (assemble →
call provider → parse → validate → ledger → advisory), no all-skip.
"""

from __future__ import annotations

import json

import pytest

from services.agent.advisor import overseer as ov
from services.agent.advisor import change_request as cr
from services.agent.advisor.overseer import (
    AuditScope,
    AuditValidationError,
    Overseer,
    audit_to_advisory,
    resolve_overseer_provider,
    validate_audit,
)
from services.agent.ledger import Ledger


# ---------------------------------------------------------------------------
# Canned provider (offline) — returns whatever audit JSON the test scripts.
# ---------------------------------------------------------------------------


class _CannedProvider:
    """A provider whose complete() returns scripted audit JSON in order."""

    name = "mock"
    supports_vision = False

    def __init__(self, replies: list[str]):
        self._replies = replies
        self._i = 0
        self.calls: list[tuple[str, list]] = []

    def complete(self, system: str, messages: list, max_tokens: int = 8000) -> str:
        self.calls.append((system, messages))
        r = self._replies[min(self._i, len(self._replies) - 1)]
        self._i += 1
        return r


@pytest.fixture(autouse=True)
def _offline_role_turn_fixture(monkeypatch):
    """Exercise Overseer through the role-turn seam without a live Chudcode host."""

    def fake_run_role_turn(request, role, **kwargs):
        options = kwargs.get("options") or {}
        provider = options.get("offline_provider")
        if provider is None:
            return {
                "success": False,
                "stop_reason": "provider_error",
                "result": {"error": "no fixture role turn"},
            }
        reply = provider.complete(
            "audit.",
            [{"role": "user", "content": request}],
            max_tokens=kwargs.get("max_tokens"),
        )
        return {
            "success": True,
            "stop_reason": "completed",
            "result": reply,
            "run_id": kwargs.get("run_id"),
            "session_id": f"fixture-{kwargs.get('parent_id')}",
            "parent_id": kwargs.get("parent_id"),
            "tokens_used": 0,
            "offline": True,
        }

    monkeypatch.setattr(ov, "run_role_turn", fake_run_role_turn)


def _ok_audit() -> str:
    return json.dumps(
        {
            "schema": "aicad.audit/1",
            "trigger": "manual",
            "verdicts": [
                {
                    "class": "ok",
                    "finding": "plan holds",
                    "evidence": {},
                    "required_action": {"kind": "none"},
                    "severity": "info",
                }
            ],
            "model": {"provider": "mock", "id": "canned"},
            "tokens": 123,
        }
    )


def _blocker_audit(
    finding: str = "plan mills pocket before drilling datum holes",
) -> str:
    return json.dumps(
        {
            "schema": "aicad.audit/1",
            "trigger": "stage_exit",
            "verdicts": [
                {
                    "class": "wrong_order",
                    "finding": finding,
                    "evidence": {"ledger_events": [3, 4], "handoffs": ["h1"]},
                    "required_action": {
                        "kind": "reorder",
                        "detail": "drill datums first",
                        "dispatch_role": "feature_modeler",
                    },
                    "severity": "blocker",
                }
            ],
            "model": {"provider": "mock", "id": "canned"},
            "tokens": 200,
        }
    )


def _scope() -> AuditScope:
    return AuditScope(
        run_id="run1",
        stage="feature",
        roles=["feature_modeler"],
        buildgraph_nodes=["bg_pocket"],
    )


# ---------------------------------------------------------------------------
# Trigger debounce
# ---------------------------------------------------------------------------


def test_debounce_one_audit_per_trigger_point():
    prov = _CannedProvider([_ok_audit(), _ok_audit()])
    o = Overseer(provider=prov, role_card={"system_prompt": "audit."})
    scope = _scope()
    r1 = o.audit("stage_exit", scope)
    r2 = o.audit("stage_exit", scope)  # same trigger + scope ⇒ debounced
    assert r1.audit and not r1.skipped
    assert r2.skipped and "debounc" in r2.skipped
    assert len(prov.calls) == 1, "the provider must be called only once (debounced)"


def test_manual_and_change_request_never_debounced():
    prov = _CannedProvider([_ok_audit(), _ok_audit(), _ok_audit(), _ok_audit()])
    o = Overseer(provider=prov, role_card={"system_prompt": "audit."})
    scope = _scope()
    assert not o.audit("manual", scope).skipped
    assert not o.audit("manual", scope).skipped  # manual re-runs
    assert not o.audit("change_request", scope).skipped
    assert not o.audit("change_request", scope).skipped  # change_request re-runs
    assert len(prov.calls) == 4


def test_different_scope_not_debounced():
    prov = _CannedProvider([_ok_audit(), _ok_audit()])
    o = Overseer(provider=prov, role_card={"system_prompt": "audit."})
    o.audit("stage_exit", AuditScope(run_id="run1", stage="feature"))
    r2 = o.audit("stage_exit", AuditScope(run_id="run1", stage="assembly"))
    assert not r2.skipped
    assert len(prov.calls) == 2


# ---------------------------------------------------------------------------
# Verdict validation
# ---------------------------------------------------------------------------


def test_validate_rejects_wrong_schema():
    with pytest.raises(AuditValidationError):
        validate_audit({"schema": "wrong/1", "trigger": "manual", "verdicts": []})


def test_validate_rejects_unknown_class():
    with pytest.raises(AuditValidationError):
        validate_audit(
            {
                "schema": "aicad.audit/1",
                "trigger": "manual",
                "verdicts": [
                    {
                        "class": "made_up",
                        "severity": "info",
                        "evidence": {},
                        "required_action": {"kind": "none"},
                    }
                ],
            }
        )


def test_validate_rejects_non_ok_without_evidence_refs():
    # role-card rule: a non-ok finding with no evidence refs is itself a cot_error.
    with pytest.raises(AuditValidationError):
        validate_audit(
            {
                "schema": "aicad.audit/1",
                "trigger": "stage_exit",
                "verdicts": [
                    {
                        "class": "plan_gap",
                        "finding": "x",
                        "severity": "action",
                        "evidence": {},
                        "required_action": {"kind": "edit"},
                    }
                ],
            }
        )


def test_validate_rejects_bad_action_kind():
    with pytest.raises(AuditValidationError):
        validate_audit(
            {
                "schema": "aicad.audit/1",
                "trigger": "manual",
                "verdicts": [
                    {
                        "class": "ok",
                        "finding": "",
                        "severity": "info",
                        "evidence": {},
                        "required_action": {"kind": "teleport"},
                    }
                ],
            }
        )


def test_validate_accepts_and_normalizes_good_audit():
    a = validate_audit(json.loads(_blocker_audit()))
    assert a["schema"] == "aicad.audit/1"
    assert a["id"].startswith("aud_")
    assert a["verdicts"][0]["class"] == "wrong_order"
    assert a["verdicts"][0]["evidence"]["ledger_events"] == [3, 4]


def test_malformed_reply_yields_error_not_crash():
    prov = _CannedProvider(["not json at all, sorry"])
    o = Overseer(provider=prov, role_card={"system_prompt": "audit."})
    r = o.audit("manual", _scope())
    assert r.error and not r.audit
    assert r.advisory["items"] == []  # a broken auditor never blocks the mission


def test_extract_json_tolerates_fences_and_prose():
    prov = _CannedProvider([f"Here is my audit:\n```json\n{_ok_audit()}\n```\nDone."])
    o = Overseer(provider=prov, role_card={"system_prompt": "audit."})
    r = o.audit("manual", _scope())
    assert r.audit and r.audit["verdicts"][0]["class"] == "ok"


# ---------------------------------------------------------------------------
# Repeat-blocker escalation at EXACTLY 2
# ---------------------------------------------------------------------------


def test_repeat_blocker_escalates_at_exactly_two(tmp_path):
    ledger = Ledger("run_esc", tmp_path / "ledger.json")
    # Same finding twice ⇒ same fingerprint. Different scopes so debounce lets both run.
    prov = _CannedProvider([_blocker_audit(), _blocker_audit()])
    o = Overseer(provider=prov, role_card={"system_prompt": "audit."}, ledger=ledger)

    r1 = o.audit("stage_exit", AuditScope(run_id="run_esc", stage="feature"))
    assert not r1.escalated, "first blocker must NOT escalate"

    r2 = o.audit("stage_exit", AuditScope(run_id="run_esc", stage="assembly"))
    assert r2.escalated, "second matching blocker MUST escalate"
    assert r2.escalation_text and "human" in r2.escalation_text.lower()
    # A blocking question was written to the ledger for the human.
    open_qs = [q for q in ledger.state["questions"] if q.get("blocking")]
    assert open_qs, "escalation must write a blocking ask_user question"


def test_distinct_blockers_do_not_escalate(tmp_path):
    ledger = Ledger("run_d", tmp_path / "ledger.json")
    prov = _CannedProvider([_blocker_audit("finding A"), _blocker_audit("finding B")])
    o = Overseer(provider=prov, role_card={"system_prompt": "audit."}, ledger=ledger)
    assert not o.audit(
        "stage_exit", AuditScope(run_id="run_d", stage="feature")
    ).escalated
    assert not o.audit(
        "stage_exit", AuditScope(run_id="run_d", stage="assembly")
    ).escalated


def test_repeat_blocker_escalates_across_fresh_overseers(tmp_path):
    # THE in-app path: audit_now builds a FRESH Overseer per call. The blocker
    # counter must survive across calls (rehydrated from the persisted ledger),
    # else escalation-at-2 can never fire through a real call site.
    lpath = tmp_path / "run_rehydrate.ledger.json"
    scope = {"stage": "feature", "roles": ["feature_modeler"]}

    def _one_call():
        from services.agent.advisor import overseer_tools as ot

        prov = _CannedProvider([_blocker_audit()])  # same finding each time
        return ot.audit_now(
            "run_rehydrate",
            scope,
            trigger="stage_exit",
            providers={"mock": prov},
            ledger_path=lpath,
            offline=True,
        )

    r1 = _one_call()
    assert not r1["escalated"], "first fresh-Overseer blocker must not escalate"
    r2 = _one_call()
    assert r2["escalated"], (
        "second call (fresh Overseer) must escalate via ledger rehydration"
    )
    assert "human" in r2["escalation_text"].lower()


def test_prior_findings_rehydrated_into_evidence(tmp_path):
    # The "you already flagged X" dedupe must survive fresh-Overseer-per-call: a
    # second audit's prompt should carry the first audit's fingerprint.
    ledger = Ledger("run_prior", tmp_path / "ledger.json")
    o1 = Overseer(
        provider=_CannedProvider([_blocker_audit()]),
        role_card={"system_prompt": "audit."},
        ledger=ledger,
    )
    o1.audit("stage_exit", AuditScope(run_id="run_prior", stage="feature"))
    # Fresh Overseer, new provider — its evidence must include the prior audit.
    prov2 = _CannedProvider([_ok_audit()])
    o2 = Overseer(provider=prov2, role_card={"system_prompt": "audit."}, ledger=ledger)
    o2.audit("stage_exit", AuditScope(run_id="run_prior", stage="assembly"))
    user_msg = prov2.calls[0][1][0]["content"]
    assert "PRIOR-FINDINGS" in user_msg, (
        "prior audits must be surfaced to a fresh Overseer"
    )


# ---------------------------------------------------------------------------
# Verdicts → advisory items (source.kind == "llm"), token logging, ledger event
# ---------------------------------------------------------------------------


def test_verdicts_render_as_llm_marked_advisories():
    a = validate_audit(json.loads(_blocker_audit()))
    adv = audit_to_advisory(a)
    assert adv["schema"] == "aicad.advisory/1"
    assert len(adv["items"]) == 1
    item = adv["items"][0]
    assert item["source"]["kind"] == "llm", (
        "overseer findings must be marked llm review findings"
    )
    assert item["severity"] == "blocker"
    assert item["candidate_fixes"][0]["action"] == "reorder"


def test_ok_verdict_produces_no_advisory_items():
    a = validate_audit(json.loads(_ok_audit()))
    adv = audit_to_advisory(a)
    assert adv["items"] == [], (
        "an 'ok' verdict gives the working role nothing to act on"
    )


def test_non_info_verdict_with_no_action_still_carries_fix():
    # §3 invariant: a non-info advisory item MUST carry >=1 candidate fix.  A
    # blocker verdict whose required_action.kind is "none" must not yield an
    # illegal empty-fix item — it gets a review_finding fix.
    a = validate_audit(
        {
            "schema": "aicad.audit/1",
            "trigger": "manual",
            "verdicts": [
                {
                    "class": "spec_drift",
                    "finding": "dim changed w/o decision",
                    "evidence": {"ledger_events": [7]},
                    "severity": "blocker",
                    "required_action": {"kind": "none"},
                }
            ],
        }
    )
    adv = audit_to_advisory(a)
    item = adv["items"][0]
    assert item["severity"] == "blocker"
    assert item["candidate_fixes"], "a non-info audit item must never be fix-less (§3)"
    assert item["candidate_fixes"][0]["action"] == "review_finding"


def test_audit_logs_tokens_and_ledger_event(tmp_path):
    ledger = Ledger("run_tok", tmp_path / "ledger.json")
    prov = _CannedProvider([_blocker_audit()])
    o = Overseer(provider=prov, role_card={"system_prompt": "audit."}, ledger=ledger)
    r = o.audit("plan_ratify", AuditScope(run_id="run_tok", stage="layout"))
    assert r.audit["tokens"] == 200, "the model's token count must be recorded"
    audit_events = [e for e in ledger.events if e["kind"] == "audit"]
    assert len(audit_events) == 1, "every audit is a ledger event"


def test_audit_without_token_count_gets_estimate():
    canned = json.loads(_ok_audit())
    canned["tokens"] = 0
    prov = _CannedProvider([json.dumps(canned)])
    o = Overseer(provider=prov, role_card={"system_prompt": "audit."})
    r = o.audit("manual", _scope())
    assert r.audit["tokens"] > 0, "a missing token count is estimated, never left 0"


def test_online_role_turn_failure_degrades_without_fabricating_audit():
    o = Overseer(provider=None, role_card={"system_prompt": "audit."})
    r = o.audit("manual", _scope())
    assert r.error and not r.audit


# ---------------------------------------------------------------------------
# Routing seam (§14)
# ---------------------------------------------------------------------------


def test_resolve_overseer_prefers_strongest():
    weak, strong = object(), object()
    got = resolve_overseer_provider({"llamacpp": weak, "anthropic": strong})
    assert got is strong, "'strongest' must prefer anthropic over a local model"


def test_resolve_overseer_collapses_onto_only_source():
    only = object()
    assert resolve_overseer_provider({"custom_x": only}) is only
    assert resolve_overseer_provider({}) is None


# ---------------------------------------------------------------------------
# Change-request mapping — the fixture case
#   "make the bore fit a 25mm shaft" → bore dim + fit recalc + interference
#   recheck, and NOTHING else.
# ---------------------------------------------------------------------------


def _bore_shaft_ledger_state() -> dict:
    """A fixture ledger state with a bore + shaft dimension and unrelated ones."""
    return {
        "dimensions": [
            {
                "id": "DIM1",
                "name": "bore diameter",
                "value": 19.8,
                "unit": "mm",
                "tolerance": {"fit": "H7/g6"},
                "refs": {"features": ["F7"], "parts": ["bracket"]},
            },
            {
                "id": "DIM2",
                "name": "shaft diameter",
                "value": 20.0,
                "unit": "mm",
                "refs": {"features": ["shaft_body"], "parts": ["shaft"]},
            },
            # Unrelated dims that MUST NOT be pulled into the change:
            {
                "id": "DIM3",
                "name": "flange thickness",
                "value": 8.0,
                "unit": "mm",
                "refs": {"features": ["F2"]},
            },
            {
                "id": "DIM4",
                "name": "overall height",
                "value": 60.0,
                "unit": "mm",
                "refs": {"features": ["F1"]},
            },
        ],
        "requirements": [
            {"req_id": "R1", "text": "bore is a sliding fit on the shaft"},
            {"req_id": "R2", "text": "mounting holes on a 40mm bolt circle"},
        ],
        "artifacts": [],
    }


def test_change_request_maps_only_bore_and_shaft():
    aff = cr.map_affected_set(
        "make the bore fit a 25mm shaft", _bore_shaft_ledger_state()
    )
    dim_ids = {d["id"] for d in aff.dimensions}
    assert "DIM1" in dim_ids, "the bore dimension must be in scope"
    assert "DIM2" in dim_ids, (
        "the shaft dimension (named '25mm shaft') must be in scope"
    )
    assert "DIM3" not in dim_ids and "DIM4" not in dim_ids, (
        "unrelated dimensions (flange, height) must NOT be pulled in"
    )
    feats = {f["feature"] for f in aff.features}
    assert "F7" in feats, "the bore feature F7 comes in via the bore dim's refs"
    assert "F2" not in feats and "F1" not in feats


def test_change_request_plan_bore_fit_and_interference_only():
    plan = cr.draft_change_plan(
        "make the bore fit a 25mm shaft", _bore_shaft_ledger_state()
    )
    # Checks: fit recalc + interference recheck, and NOTHING like wall_thickness.
    assert "fit_report" in plan.checks_to_rerun
    assert "check_interference" in plan.checks_to_rerun
    assert "wall_thickness" not in plan.checks_to_rerun, "no unrelated checks"
    # Ordered edits: the shaft gets the explicit new value (25), the bore is recalced.
    edits_by_dim = {e["target"]["dim_id"]: e for e in plan.ordered_edits}
    assert edits_by_dim["DIM2"]["op"] == "edit_dimension"
    assert edits_by_dim["DIM2"]["new_value"] == 25.0
    assert edits_by_dim["DIM1"]["op"] == "recalc_from_fit", (
        "the bore is recomputed from the resized shaft + fit, not given a raw value"
    )


def test_change_request_routes_through_overseer_and_ledger(tmp_path):
    ledger = Ledger("run_cr", tmp_path / "ledger.json")
    # Seed the ledger with the bore/shaft dims (so ledger.state matches the fixture).
    ledger.upsert_dimension(
        "intake",
        "DIM1",
        "bore diameter",
        19.8,
        "mm",
        driver="fit on shaft",
        provenance="user",
        tolerance={"fit": "H7/g6"},
        refs={"features": ["F7"], "parts": ["bracket"]},
    )
    ledger.upsert_dimension(
        "intake",
        "DIM2",
        "shaft diameter",
        20.0,
        "mm",
        driver="given",
        provenance="user",
        refs={"features": ["shaft_body"], "parts": ["shaft"]},
    )
    prov = _CannedProvider([_ok_audit()])
    o = Overseer(provider=prov, role_card={"system_prompt": "audit."}, ledger=ledger)

    result = cr.route_change_request(
        "make the bore fit a 25mm shaft",
        ledger.state,
        overseer=o,
        ledger=ledger,
        run_id="run_cr",
        stage="fix",
    )
    assert not result.empty_scope
    assert result.audit and result.audit["verdicts"][0]["class"] == "ok"
    # The plan was audited under the change_request trigger (provider called once).
    assert len(prov.calls) == 1
    # A change_plan artifact + a scoped decision were written to the ledger.
    assert any(e["kind"] == "change_plan" for e in ledger.events)
    assert ledger.state["decisions"], "a scoped-change decision is written for the user"
    # Approach-scope change with no criteria change ⇒ dispatchable (no user gate).
    assert result.dispatch, "an approach-scoped change recommends a dispatch"
    assert not result.needs_user_confirm


def test_change_request_self_audit_sees_the_ledger(tmp_path):
    # The self-audit must judge the map against the ACTUAL ledger — a mis-scoped
    # map (missed dimension) is only catchable if the audit sees the ledger.
    ledger = Ledger("run_cr2", tmp_path / "ledger.json")
    ledger.upsert_dimension(
        "intake",
        "DIM1",
        "bore diameter",
        19.8,
        "mm",
        driver="fit",
        provenance="user",
        refs={"features": ["F7"]},
    )
    prov = _CannedProvider([_ok_audit()])
    o = Overseer(provider=prov, role_card={"system_prompt": "audit."}, ledger=ledger)
    cr.route_change_request(
        "resize the bore", ledger.state, overseer=o, ledger=ledger, run_id="run_cr2"
    )
    # The audit prompt (the user message) carries the rendered ledger evidence.
    assert prov.calls, "the self-audit must call the provider"
    user_msg = prov.calls[0][1][0]["content"]
    assert "LEDGER" in user_msg and "bore diameter" in user_msg, (
        "the change-request self-audit must include the ledger in its evidence"
    )


def test_change_request_empty_scope_asks_user(tmp_path):
    ledger = Ledger("run_empty", tmp_path / "ledger.json")
    result = cr.route_change_request(
        "make it more aerodynamic",
        {"dimensions": [], "requirements": [], "artifacts": []},
        overseer=None,
        ledger=ledger,
        run_id="run_empty",
    )
    assert result.empty_scope
    assert not result.dispatch, "an unmatched request must not dispatch anything"
    assert "did not match" in result.user_summary


# ---------------------------------------------------------------------------
# In-app entry points (overseer_tools) — the in-app acceptance surface:
# the capability working through the IN-APP path (loads run ledger + buildgraph
# sidecar, resolves provider, returns JSON-serializable dicts).
# ---------------------------------------------------------------------------


def _seed_bore_shaft_run(run_id: str, tmp_path):
    lpath = tmp_path / f"{run_id}.ledger.json"
    ledger = Ledger(run_id, lpath)
    ledger.upsert_dimension(
        "intake",
        "DIM1",
        "bore diameter",
        19.8,
        "mm",
        driver="fit on shaft",
        provenance="user",
        tolerance={"fit": "H7/g6"},
        refs={"features": ["F7"], "parts": ["bracket"]},
    )
    ledger.upsert_dimension(
        "intake",
        "DIM2",
        "shaft diameter",
        20.0,
        "mm",
        driver="given",
        provenance="user",
        refs={"features": ["shaft_body"], "parts": ["shaft"]},
    )
    return lpath


def test_inapp_change_request_end_to_end(tmp_path):
    from services.agent.advisor import overseer_tools as ot

    lpath = _seed_bore_shaft_run("run_inapp", tmp_path)
    prov = _CannedProvider([_ok_audit()])
    out = ot.change_request(
        "make the bore fit a 25mm shaft",
        "run_inapp",
        providers={"mock": prov},
        ledger_path=lpath,
        offline=True,
    )
    assert out["ok"] and not out["empty_scope"]
    assert out["plan"]["checks_to_rerun"] == ["fit_report", "check_interference"]
    assert out["audit"]["verdicts"][0]["class"] == "ok"
    assert out["dispatch"], "an in-app change request yields a dispatch recommendation"
    # JSON-serializable (it crosses the in-app/GUI boundary).
    json.dumps(out)


def test_inapp_change_request_offline_no_provider(tmp_path):
    from services.agent.advisor import overseer_tools as ot

    lpath = _seed_bore_shaft_run("run_off", tmp_path)
    out = ot.change_request(
        "make the bore fit a 25mm shaft",
        "run_off",
        providers={},
        ledger_path=lpath,
        offline=True,
    )
    # Deterministic mapping still works with no provider (audit just absent).
    assert out["ok"] and not out["empty_scope"]
    assert out["plan"]["ordered_edits"]
    assert out["audit"] == {}


def test_inapp_audit_now_end_to_end(tmp_path):
    from services.agent.advisor import overseer_tools as ot

    lpath = _seed_bore_shaft_run("run_audit", tmp_path)
    prov = _CannedProvider([_blocker_audit()])
    out = ot.audit_now(
        "run_audit",
        {"stage": "feature", "roles": ["feature_modeler"]},
        trigger="manual",
        providers={"mock": prov},
        ledger_path=lpath,
        offline=True,
    )
    assert out["ok"]
    assert out["audit"]["verdicts"][0]["class"] == "wrong_order"
    assert out["advisory"]["items"][0]["source"]["kind"] == "llm"
    json.dumps(out)


def test_audit_now_executes_online_chudcode_role_turn(
    tmp_path,
    monkeypatch,
):
    from services.agent.advisor import overseer_tools as ot

    calls = []

    def online_turn(request, role, **kwargs):
        calls.append((request, role, kwargs))
        return {
            "success": True,
            "stop_reason": "completed",
            "result": _ok_audit(),
            "tokens_used": 321,
        }

    monkeypatch.setattr(ov, "run_role_turn", online_turn)
    result = ot.audit_now(
        "run-online",
        {"stage": "feature", "buildgraph_nodes": ["build"]},
        ledger_path=tmp_path / "ledger.json",
        session_id="parent-session",
    )
    assert result["ok"] is True
    assert result["audit"]["tokens"] == 123
    assert calls[0][1] == "overseer"
    assert calls[0][2]["session_id"] == "parent-session"
    assert calls[0][2]["close_session"] is True
    assert calls[0][2]["options"]["offline"] is False


def test_explicit_offline_overseer_routes_provider_only_through_role_helper():
    provider = _CannedProvider([_ok_audit()])
    result = Overseer(provider=provider, role_card={"system_prompt": "audit."}).audit(
        "manual", _scope()
    )

    assert not result.error
    assert provider.calls


# ---------------------------------------------------------------------------
# DAG stage-exit trigger seam (services/dag/node_runner._run_overseer_stage_exit)
# ---------------------------------------------------------------------------


def test_dag_stage_exit_seam_noops_only_without_run_id(monkeypatch):
    from services.agent.advisor import overseer_tools as ot
    from services.dag import node_runner

    calls = []
    monkeypatch.setattr(
        ot,
        "audit_now",
        lambda *args, **kwargs: calls.append((args, kwargs)) or {"ok": True},
    )
    node = {"id": "n1", "type": "model", "name": "make bracket"}
    assert node_runner._run_overseer_stage_exit(node, {}) is None
    assert node_runner._run_overseer_stage_exit(node, {"run_id": "r1"}) == {"ok": True}
    assert calls[0][1]["providers"] is None
    assert calls[0][1]["offline"] is False


def test_dag_stage_exit_seam_fires_online_with_one_shot_lineage(
    tmp_path,
    monkeypatch,
):
    from services.dag import node_runner

    monkeypatch.setenv("CHUDWARE_CACHE_ROOT", str(tmp_path))
    seen = []

    def online_turn(request, role, **kwargs):
        seen.append((request, role, kwargs))
        return {
            "success": True,
            "stop_reason": "completed",
            "result": _ok_audit(),
            "tokens_used": 123,
        }

    monkeypatch.setattr(ov, "run_role_turn", online_turn)
    node = {"id": "bg1", "type": "model", "name": "bore"}
    ctx = {"run_id": "run_seam", "session_id": "parent-session"}
    out = node_runner._run_overseer_stage_exit(node, ctx)
    assert out is not None and out["ok"]
    assert seen[0][1] == "overseer"
    assert seen[0][2]["session_id"] == "parent-session"
    assert seen[0][2]["close_session"] is True
