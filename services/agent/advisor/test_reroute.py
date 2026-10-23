"""Tests for the overseer reroute loop + user-change → stage mapping.

Covers:
  * the overseer emits ``audit`` ledger events carrying ``targeted_reentry``
    ``{stage, finding-context, role}`` per verdict;
  * a reroute lands at the CORRECT owning stage — a geometry error routes to the
    GEOMETRY stage / geometry_designer, NOT a full restart;
  * user-change mapping resolves a change request to a stage+feature re-entry;
  * master-intent re-grounding catches a drifted stage output (and the pinned
    intent survives a tiny evidence budget).

Offline discipline: a canned provider returns fixed audit JSON — real code paths
(assemble → provider → parse → validate → route → ledger), no all-skip.
"""

from __future__ import annotations

import json

import pytest

from services.agent.advisor import overseer as ov
from services.agent.advisor import change_request as cr
from services.agent.advisor import reroute
from services.agent.advisor.overseer import (
    AuditScope,
    EvidenceBudget,
    Overseer,
    assemble_evidence,
    render_master_intent,
    validate_audit,
)
from services.agent.advisor.reroute import Reentry, route_audit, route_verdict
from services.agent.ledger import Ledger


# ---------------------------------------------------------------------------
# Canned provider (offline)
# ---------------------------------------------------------------------------


class _CannedProvider:
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
    """Drive canned audit replies through the Chudcode role-turn contract."""

    def fake_run_role_turn(request, role, **kwargs):
        provider = (kwargs.get("options") or {}).get("offline_provider")
        if provider is None:
            return {
                "success": False,
                "stop_reason": "provider_error",
                "result": {"error": "missing canned provider"},
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
            "tokens_used": 0,
        }

    monkeypatch.setattr(ov, "run_role_turn", fake_run_role_turn)


def _geometry_error_audit() -> str:
    """A verdict whose evidence cites a geometry_spec event (seq 5)."""
    return json.dumps(
        {
            "schema": "aicad.audit/1",
            "trigger": "stage_exit",
            "verdicts": [
                {
                    "class": "cot_error",
                    "finding": "bore Ø 20.02 was recalled, not computed — no fit_calc provenance",
                    "evidence": {"ledger_events": [5]},
                    "required_action": {
                        "kind": "edit",
                        "detail": "recompute the bore from fit_calc",
                        "dispatch_role": "geometry_designer",
                    },
                    "severity": "action",
                }
            ],
            "model": {"provider": "mock", "id": "canned"},
            "tokens": 90,
        }
    )


def _drift_audit() -> str:
    """A spec_drift verdict citing a geometry_spec event — the drift re-grounds there."""
    return json.dumps(
        {
            "schema": "aicad.audit/1",
            "trigger": "stage_exit",
            "verdicts": [
                {
                    "class": "spec_drift",
                    "finding": "geometry made a 40mm plate but the brief calls for a 25mm bracket",
                    "evidence": {"ledger_events": [5]},
                    "required_action": {
                        "kind": "edit",
                        "detail": "re-size to the briefed 25mm envelope",
                        "dispatch_role": "geometry_designer",
                    },
                    "severity": "blocker",
                }
            ],
            "model": {"provider": "mock", "id": "canned"},
            "tokens": 88,
        }
    )


# ---------------------------------------------------------------------------
# Fixtures — a ledger carrying a brief + a geometry_spec
# ---------------------------------------------------------------------------


def _dp_ledger(tmp_path) -> Ledger:
    """A ledger with a brief (master intent) + a DP artifact chain to seq 5."""
    lg = Ledger("run_dp", tmp_path / "ledger.json")
    lg.set_brief(
        "intake",
        "25mm mounting bracket",
        "aluminium bracket, mounts a sensor to a 25mm chassis rail",
        ["fits the 25mm rail", "bore takes an M6 fastener"],
    )  # seq 0
    lg.add_requirement("intake", "R1", "mount to the 25mm chassis rail")  # seq 1
    lg.add_feature("feature_architect", "FT1", "mount to rail", "R1")  # seq 2
    lg.add_feature("feature_architect", "FT2", "fasten sensor", "R1")  # seq 3
    lg.add_linkage("linkage_planner", "LINK_MAIN")  # seq 4
    lg.add_geometry_spec(
        "geometry_designer",
        "G1",
        ["FT1"],
        [{"op": "extrude"}],
        provenance="fit_calc(H7/g6) → 20.02 [iso286.json]",
    )  # seq 5
    return lg


# ---------------------------------------------------------------------------
# 1. The frozen kind → stage table
# ---------------------------------------------------------------------------


def test_kind_to_stage_table_matches_contract():
    assert reroute.stage_for_kind("feature") == ("decompose", "feature_architect")
    assert reroute.stage_for_kind("geometry_spec") == ("geometry", "geometry_designer")
    assert reroute.stage_for_kind("linkage") == ("linkage", "linkage_planner")
    assert reroute.stage_for_kind("build_node") == ("buildpath", "orchestrator")
    assert reroute.stage_for_kind("nonsense") is None


# ---------------------------------------------------------------------------
# 2. A geometry error routes to GEOMETRY / geometry_designer — NOT a restart
# ---------------------------------------------------------------------------


def test_geometry_error_routes_to_geometry_stage(tmp_path):
    lg = _dp_ledger(tmp_path)
    verdict = validate_audit(json.loads(_geometry_error_audit()))["verdicts"][0]
    re = route_verdict(verdict, ledger=lg)
    assert re is not None
    assert re.stage == "geometry", "must route to the GEOMETRY stage, not restart"
    assert re.role == "geometry_designer"
    assert re.feature == "G1", "the specific geometry_spec is the re-entry point"


def test_earliest_owning_stage_wins_when_multiple_kinds_cited(tmp_path):
    """A verdict citing both a feature (seq 2) and a geometry_spec (seq 5) re-enters
    the deepest-upstream rung (DECOMPOSE) — fix the root, not the symptom."""
    lg = _dp_ledger(tmp_path)
    verdict = {
        "class": "plan_gap",
        "finding": "feature never realized",
        "evidence": {"ledger_events": [2, 5]},
        "required_action": {"kind": "edit"},
        "severity": "action",
    }
    re = route_verdict(verdict, ledger=lg)
    assert re.stage == "decompose" and re.role == "feature_architect"


def test_class_fallback_when_no_evidence_kind():
    """No resolvable evidence kind ⇒ class fallback picks an owning stage — but only
    inside the design pipeline, so the DP context is signalled via ``dp_active``."""
    re = route_verdict(
        {
            "class": "missed_requirement",
            "finding": "R3 has no feature",
            "evidence": {"ledger_events": []},
            "required_action": {"kind": "replan"},
            "severity": "blocker",
        },
        ledger=None,
        dp_active=True,
    )
    assert re is not None and re.stage == "decompose"


def test_class_fallback_gated_off_for_legacy_audit(tmp_path):
    """A legacy audit (design_pipeline flag OFF → no DP artifacts in the ledger) with a
    class that would map to a DP stage but NO DP evidence gets targeted_reentry=None:
    the class fallback must not point at a phase machine that isn't running."""
    lg = Ledger("run_legacy", tmp_path / "ledger.json")
    lg.add_requirement("intake", "R1", "some legacy requirement")  # no DP artifact
    assert reroute._ledger_has_dp_artifacts(lg) is False
    re = route_verdict(
        {
            "class": "wrong_order",
            "finding": "operations sequenced out of order",
            "evidence": {"ledger_events": []},
            "required_action": {"kind": "replan"},
            "severity": "action",
        },
        ledger=lg,
    )
    assert re is None, "a legacy (non-DP) audit must not route to a DP stage"


def test_ok_verdict_gets_no_reentry():
    assert (
        route_verdict(
            {"class": "ok", "finding": "holds", "evidence": {}, "severity": "info"}
        )
        is None
    )


# ---------------------------------------------------------------------------
# 3. The overseer emits an audit event carrying targeted_reentry
# ---------------------------------------------------------------------------


def test_overseer_audit_event_carries_targeted_reentry(tmp_path):
    lg = _dp_ledger(tmp_path)
    prov = _CannedProvider([_geometry_error_audit()])
    o = Overseer(provider=prov, role_card={"system_prompt": "audit."}, ledger=lg)
    res = o.audit("stage_exit", AuditScope(run_id="run_dp", stage="buildpath"))

    assert res.audit, "audit must run (provider present)"
    v = res.audit["verdicts"][0]
    assert v["targeted_reentry"] is not None
    # The reroute targets GEOMETRY even though the error was DETECTED at buildpath
    # (scope.stage) — targeted upstream correction, not a restart at the scope stage.
    assert v["targeted_reentry"]["stage"] == "geometry"
    assert v["targeted_reentry"]["role"] == "geometry_designer"
    assert v["targeted_reentry"]["feature"] == "G1"

    # The audit is a ledger event carrying the reentry (persisted, inspectable).
    audit_events = [e for e in lg.events if e.get("kind") == "audit"]
    assert audit_events, "the audit must be written to the ledger"
    persisted = audit_events[-1]["payload"]["verdicts"][0]["targeted_reentry"]
    assert persisted["stage"] == "geometry"


def test_route_audit_leaves_ok_verdict_reentry_none():
    audit = {
        "scope": {"stage": "geometry"},
        "verdicts": [
            {"class": "ok", "finding": "", "evidence": {}, "severity": "info"}
        ],
    }
    route_audit(audit, ledger=None)
    assert audit["verdicts"][0]["targeted_reentry"] is None


# ---------------------------------------------------------------------------
# 4. Master-intent re-grounding
# ---------------------------------------------------------------------------


def test_master_intent_renders_brief_and_requirements():
    txt = render_master_intent(
        {
            "title": "25mm bracket",
            "summary": "mounts a sensor",
            "success_criteria": ["fits the rail"],
        },
        [{"req_id": "R1", "text": "mount to rail"}],
    )
    assert "MASTER-INTENT" in txt
    assert "25mm bracket" in txt and "R1" in txt and "fits the rail" in txt


def test_master_intent_survives_a_tiny_budget():
    """Drift happens on small contexts — the pinned intent must NOT be truncated
    even when the ledger_text budget is nearly zero."""
    intent = render_master_intent(
        {"title": "25mm bracket", "summary": "x"},
        [{"req_id": "R1", "text": "mount to the rail"}],
    )
    # A tiny ledger budget: ledger_text is (nearly) dropped, but master_intent rides
    # its own un-budgeted block.
    ev = assemble_evidence(
        "stage_exit",
        AuditScope(run_id="r", stage="geometry"),
        ledger_text="",  # simulate a truncated-away ledger render
        master_intent=intent,
        budget=EvidenceBudget(ledger=1, buildgraph=1, handoffs=1, history=1),
    )
    assert "MASTER-INTENT" in ev
    assert "mount to the rail" in ev, "the goal must survive a tiny budget"


def test_drifted_geometry_output_is_caught_and_rerouted(tmp_path):
    """A spec_drift verdict (geometry drifted from the briefed 25mm envelope) is
    caught and re-grounds at the OWNING geometry stage."""
    lg = _dp_ledger(tmp_path)
    prov = _CannedProvider([_drift_audit()])
    o = Overseer(provider=prov, role_card={"system_prompt": "audit."}, ledger=lg)
    res = o.audit("stage_exit", AuditScope(run_id="run_dp", stage="buildpath"))

    v = res.audit["verdicts"][0]
    assert v["class"] == "spec_drift"
    # The pinned master intent was in the evidence the auditor saw.
    system, messages = prov.calls[-1]
    assert "MASTER-INTENT" in messages[0]["content"]
    assert "25mm mounting bracket" in messages[0]["content"]
    # Drift re-grounds at the owning geometry stage (not a blind restart).
    assert v["targeted_reentry"]["stage"] == "geometry"
    assert v["targeted_reentry"]["role"] == "geometry_designer"


# ---------------------------------------------------------------------------
# 5. User-change → stage+feature re-entry
# ---------------------------------------------------------------------------


def _wall_ledger_state() -> dict:
    return {
        "dimensions": [
            {
                "id": "DIM3",
                "name": "wall_thickness",
                "value": 2.0,
                "unit": "mm",
                "refs": {"features": ["G4"], "parts": ["housing"]},
            },
        ],
        "requirements": [],
        "artifacts": [],
    }


def test_user_change_maps_to_geometry_stage_and_feature():
    """ "make the wall thicker" resolves to a GEOMETRY-stage re-entry on the wall's
    dimension — a stage+feature target, not a blind re-model."""
    aff = cr.map_affected_set("make the wall thicker", _wall_ledger_state())
    plan = cr.draft_change_plan("make the wall thicker", _wall_ledger_state())
    reroute.route_change(aff, plan.dispatch)
    assert plan.dispatch, "a wall change must produce a dispatch recommendation"
    row = plan.dispatch[0]
    assert "stage_reentry" in row, "the DP-stage re-entry must be attached"
    assert row["stage_reentry"]["stage"] == "geometry"
    assert row["stage_reentry"]["role"] == "geometry_designer"
    # The wall dimension refs geometry element G4 — that is the re-entry handle.
    assert row["stage_reentry"]["feature"] == "G4"


def test_route_change_request_end_to_end_carries_stage_reentry(tmp_path):
    """The full change_request flow (map → plan → route) surfaces stage_reentry on
    the dispatch rows in the returned plan."""
    lg = Ledger("run_chg", tmp_path / "ledger.json")
    res = cr.route_change_request(
        "make the wall thicker",
        _wall_ledger_state(),
        overseer=None,
        ledger=lg,
        run_id="run_chg",
        stage="fix",
    )
    dispatch = res.plan["dispatch"]
    assert dispatch and any(
        r.get("stage_reentry", {}).get("stage") == "geometry" for r in dispatch
    )


def test_feature_change_maps_to_decompose():
    aff = cr.AffectedSet(features=[{"feature": "FT1", "via": "DIM1"}])
    re = reroute._stage_reentry_from_affected(aff)
    assert re is not None and re.stage == "decompose" and re.feature == "FT1"


def test_empty_affected_set_yields_no_stage_reentry():
    assert reroute._stage_reentry_from_affected(cr.AffectedSet()) is None


# ---------------------------------------------------------------------------
# 6. Repeat-misroute escalation keys on the deterministic reentry, not prose
#    (P1 fix: overseer._fingerprint / _check_repeat_blockers)
# ---------------------------------------------------------------------------


def _geometry_action_audit(finding: str) -> str:
    """A non-blocker (severity=action) verdict citing the same geometry_spec
    (seq 5) as ``_geometry_error_audit`` — same targeted_reentry, reworded
    finding, non-blocker severity: exactly the case the old prose+blocker-only
    fingerprint let loop forever."""
    return json.dumps(
        {
            "schema": "aicad.audit/1",
            "trigger": "stage_exit",
            "verdicts": [
                {
                    "class": "cot_error",
                    "finding": finding,
                    "evidence": {"ledger_events": [5]},
                    "required_action": {
                        "kind": "edit",
                        "detail": "recompute the bore from fit_calc",
                        "dispatch_role": "geometry_designer",
                    },
                    "severity": "action",
                }
            ],
            "model": {"provider": "mock", "id": "canned"},
            "tokens": 90,
        }
    )


def _feature_action_audit(finding: str) -> str:
    """A non-blocker verdict citing the feature artifact (seq 2) — a DIFFERENT
    targeted_reentry (decompose/feature_architect) than the geometry ones."""
    return json.dumps(
        {
            "schema": "aicad.audit/1",
            "trigger": "stage_exit",
            "verdicts": [
                {
                    "class": "cot_error",
                    "finding": finding,
                    "evidence": {"ledger_events": [2]},
                    "required_action": {
                        "kind": "edit",
                        "detail": "revisit feature FT1",
                        "dispatch_role": "feature_architect",
                    },
                    "severity": "action",
                }
            ],
            "model": {"provider": "mock", "id": "canned"},
            "tokens": 80,
        }
    )


def test_repeat_misroute_escalates_on_reentry_fingerprint_despite_reworded_prose(
    tmp_path,
):
    """A correction that keeps getting routed to the same DP stage must escalate
    even when the LLM rewords the finding and never assigns severity=blocker."""
    lg = _dp_ledger(tmp_path)
    prov = _CannedProvider(
        [
            _geometry_action_audit("bore Ø 20.02 was recalled, not computed"),
            _geometry_action_audit(
                "the bore diameter still has no fit_calc provenance"
            ),
        ]
    )
    o = Overseer(provider=prov, role_card={"system_prompt": "audit."}, ledger=lg)

    r1 = o.audit("stage_exit", AuditScope(run_id="run_dp", stage="buildpath"))
    assert r1.audit["verdicts"][0]["severity"] == "action"
    assert not r1.escalated, "first non-blocker misroute must not escalate"

    r2 = o.audit("stage_exit", AuditScope(run_id="run_dp", stage="assembly"))
    assert r2.escalated, (
        "a second reworded finding routed to the SAME geometry "
        "stage must escalate — the fingerprint is the deterministic "
        "reentry, not prose, and severity=='action' still counts"
    )
    assert "human" in r2.escalation_text.lower()


def test_repeat_misroute_does_not_escalate_across_different_reentry_targets(tmp_path):
    """Same finding text, but each verdict routes to a DIFFERENT owning stage — the
    reentry-based fingerprint must NOT collapse these into one repeat count."""
    lg = _dp_ledger(tmp_path)
    prov = _CannedProvider(
        [
            _geometry_action_audit("something is off"),
            _feature_action_audit("something is off"),
        ]
    )
    o = Overseer(provider=prov, role_card={"system_prompt": "audit."}, ledger=lg)
    r1 = o.audit("stage_exit", AuditScope(run_id="run_dp", stage="buildpath"))
    assert not r1.escalated
    r2 = o.audit("stage_exit", AuditScope(run_id="run_dp", stage="assembly"))
    assert not r2.escalated, (
        "different targeted_reentry stages must not count as the same repeat"
    )
