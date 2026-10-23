"""Tests for aicad.failure/1 card factory and bounded fixer loop.

Coverage:
  - All 8 class factories produce valid §5 cards with deterministic ``why`` where
    the underlying tool provides it.
  - Deterministic ``fc_id``: same inputs → same id (budget-reset proof).
  - One-candidate-per-attempt enforcement (record_attempt).
  - ``improved``/``worse``/``same``/``new_failure`` classification against delta.
  - Escalation fires at EXACTLY budget exhaustion (off-by-one test).
  - ``regen_card`` verbatim-splice: diagnosis embedded, not paraphrased.
  - ``promote_advisory_blockers`` promotes blockers, ignores non-blockers.
  - Ledger integration (ledger_note_card / ledger_note_attempt — no raises).
"""
from __future__ import annotations

import pytest

from services.agent.advisor.failures import (
    SCHEMA,
    VALID_RESULTS,
    budget_exhausted,
    card_cam,
    card_dfm,
    card_export,
    card_interference,
    card_overlap,
    card_rebuild,
    card_sim,
    card_solver,
    escalate,
    ledger_note_attempt,
    ledger_note_card,
    promote_advisory_blockers,
    record_attempt,
    _fc_id,
    _where_key,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

REBUILD_REGEN_CARD = {
    "kind": "fix_preview_card",
    "insertion_point": "IP-18",
    "artifact_id": "art_123",
    "diagnosis": {"cause": "missing_ref", "feature": "F7", "detail": "referenced face deleted"},
    "fix_ops": [
        {"op": "edit_feature_param", "feature": "F7", "param": "ref", "value": "F5",
         "predicted_effect": "the feature rebuilds"},
    ],
    "changes": ["set param ref of F7 to F5"],
    "n_applied": 1,
}

INTERFERENCE_ITEM = {
    "id": "adv_abc",
    "severity": "blocker",
    "check": "interference",
    "finding": "bodies bracket/shaft interfere 340 mm³",
    "refs": {"doc_id": "doc_1", "bodies": ["bracket", "shaft"], "features": ["F7"]},
    "numbers": {"overlap_mm3": 340, "bore_d": 19.8, "shaft_d": 20.0},
    "candidate_fixes": [{
        "action": "edit_feature_param",
        "params": {"feature": "F7", "param": "d", "value": 20.02},
        "basis": "ISO 286 H7/g6 on Ø20",
        "predicted_effect": "clearance 0.007..0.041 mm",
        "confidence": "table",
    }],
    "helper_tools": ["fit_calc", "measure_between"],
    "source": {"kind": "kernel", "detail": "doc_eval_interference"},
}

OVERLAP_ITEM = {
    "id": "adv_def",
    "severity": "blocker",
    "check": "feature_overlap",
    "finding": "feature overlap detected: F8 swallowed by F3",
    "refs": {"doc_id": "doc_1", "features": ["F3", "F8"]},
    "numbers": {"classification": "swallowed"},
    "candidate_fixes": [{
        "action": "reorder_feature",
        "params": {"feature": "F8", "before": "F3"},
        "basis": "later feature is swallowed",
        "predicted_effect": "feature contributes geometry",
        "confidence": "heuristic",
    }],
    "helper_tools": ["check_feature_overlap", "describe_part"],
    "source": {"kind": "kernel", "detail": "doc_eval_feature_overlap"},
}


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _valid_card(card: dict, cls: str) -> None:
    """Assert the card satisfies §5 structural requirements."""
    assert card["schema"] == SCHEMA
    assert card["class"] == cls
    assert card["id"].startswith("fc_")
    assert isinstance(card["what"], str) and card["what"]
    assert card["why"]["kind"] in ("deterministic", "heuristic", "unknown")
    assert isinstance(card["candidate_fixes"], list)
    assert len(card["candidate_fixes"]) >= 1, "§5: non-empty candidate_fixes required"
    for fix in card["candidate_fixes"]:
        assert "action" in fix
        assert "predicted_effect" in fix
        assert "confidence" in fix
    assert isinstance(card["attempts"], list)
    assert "max_attempts" in card["budget"]
    assert card["budget"]["on_exhaust"] in ("ask_user", "dag_replan")


# ---------------------------------------------------------------------------
# 1. Rebuild card
# ---------------------------------------------------------------------------

class TestCardRebuild:
    def test_with_regen_card_verbatim_splice(self):
        card = card_rebuild("doc_1", "F7", "rebuild failed", regen_card=REBUILD_REGEN_CARD)
        _valid_card(card, "rebuild")
        # Verbatim-splice: the diagnosis dict must be embedded, not paraphrased
        assert card["why"]["detail"] == REBUILD_REGEN_CARD["diagnosis"]
        assert card["why"]["kind"] == "deterministic"
        # candidate_fixes comes from fix_ops
        assert card["candidate_fixes"][0]["params"]["op"] == "edit_feature_param"

    def test_without_regen_card_heuristic(self):
        card = card_rebuild("doc_1", "F3", "NullPointerException in feature F3")
        _valid_card(card, "rebuild")
        assert card["why"]["kind"] == "heuristic"
        assert card["why"]["detail"]["raw"] == "NullPointerException in feature F3"
        # Fallback: should suggest calling regen_diagnose
        assert card["candidate_fixes"][0]["action"] == "diagnose_rebuild"

    def test_without_regen_card_no_error(self):
        card = card_rebuild("doc_1", "F1", "")
        _valid_card(card, "rebuild")
        assert card["why"]["kind"] == "unknown"

    def test_regen_card_with_dataclass_diagnosis(self):
        """build_fix_preview may store a Diagnosis dataclass — must survive."""
        class FakeDiagnosis:
            cause = "missing_ref"
            feature = "F7"

        regen_card = dict(REBUILD_REGEN_CARD)
        regen_card["diagnosis"] = FakeDiagnosis()
        card = card_rebuild("doc_1", "F7", "rebuild failed", regen_card=regen_card)
        _valid_card(card, "rebuild")
        assert isinstance(card["why"]["detail"], dict)
        assert "cause" in card["why"]["detail"]

    def test_stable_fc_id(self):
        """Same inputs must produce same fc_id across calls (budget-reset proof)."""
        c1 = card_rebuild("doc_1", "F7", "rebuild failed", regen_card=REBUILD_REGEN_CARD)
        c2 = card_rebuild("doc_1", "F7", "rebuild failed", regen_card=REBUILD_REGEN_CARD)
        assert c1["id"] == c2["id"]

    def test_different_features_different_ids(self):
        c1 = card_rebuild("doc_1", "F7", "rebuild failed")
        c2 = card_rebuild("doc_1", "F8", "rebuild failed")
        assert c1["id"] != c2["id"]


# ---------------------------------------------------------------------------
# 2. Solver card
# ---------------------------------------------------------------------------

class TestCardSolver:
    CONFLICTING = [
        {"id": "c7", "type": "parallel",
         "suggestion": "remove conflicting constraint c7 (parallel)"},
    ]

    def test_with_conflicting(self):
        card = card_solver("doc_1", "sketch_1", self.CONFLICTING)
        _valid_card(card, "solver")
        assert card["why"]["kind"] == "deterministic"
        assert card["why"]["detail"]["conflicting"] == self.CONFLICTING
        # One fix per conflicting constraint
        assert len(card["candidate_fixes"]) == len(self.CONFLICTING)
        fix = card["candidate_fixes"][0]
        assert fix["action"] == "delete_constraint"
        assert fix["confidence"] == "kernel"
        assert "c7" in fix["basis"]

    def test_without_conflicting_heuristic(self):
        card = card_solver("doc_1", "sketch_1", [])
        _valid_card(card, "solver")
        assert card["why"]["kind"] == "heuristic"

    def test_multiple_conflicting_one_fix_each(self):
        conflicting = [
            {"id": "c1", "type": "coincident", "suggestion": "remove c1"},
            {"id": "c2", "type": "parallel", "suggestion": "remove c2"},
        ]
        card = card_solver("doc_1", "sk", conflicting)
        assert len(card["candidate_fixes"]) == 2

    def test_stable_id(self):
        c1 = card_solver("doc_1", "sk", self.CONFLICTING)
        c2 = card_solver("doc_1", "sk", self.CONFLICTING)
        assert c1["id"] == c2["id"]


# ---------------------------------------------------------------------------
# 3. Interference card (from advisory item)
# ---------------------------------------------------------------------------

class TestCardInterference:
    def test_from_advisory_item(self):
        card = card_interference(INTERFERENCE_ITEM)
        _valid_card(card, "interference")
        # Candidate fixes reused from advisory item (no duplication)
        assert card["candidate_fixes"] == INTERFERENCE_ITEM["candidate_fixes"]
        assert card["why"]["kind"] == "deterministic"
        assert card["why"]["detail"]["overlap_mm3"] == 340

    def test_no_candidate_fixes_in_item_gets_default(self):
        item = dict(INTERFERENCE_ITEM)
        item["candidate_fixes"] = []
        card = card_interference(item)
        _valid_card(card, "interference")
        assert len(card["candidate_fixes"]) >= 1

    def test_bore_shaft_in_why(self):
        card = card_interference(INTERFERENCE_ITEM)
        assert card["why"]["detail"]["bore_d"] == 19.8
        assert card["why"]["detail"]["shaft_d"] == 20.0

    def test_stable_id(self):
        c1 = card_interference(INTERFERENCE_ITEM)
        c2 = card_interference(INTERFERENCE_ITEM)
        assert c1["id"] == c2["id"]


# ---------------------------------------------------------------------------
# 4. Overlap card (from advisory item)
# ---------------------------------------------------------------------------

class TestCardOverlap:
    def test_from_advisory_item(self):
        card = card_overlap(OVERLAP_ITEM)
        _valid_card(card, "overlap")
        assert card["candidate_fixes"] == OVERLAP_ITEM["candidate_fixes"]
        assert card["why"]["kind"] == "deterministic"
        assert card["why"]["detail"]["classification"] == "swallowed"

    def test_no_fixes_in_item_gets_default(self):
        item = dict(OVERLAP_ITEM)
        item["candidate_fixes"] = []
        card = card_overlap(item)
        _valid_card(card, "overlap")

    def test_stable_id(self):
        c1 = card_overlap(OVERLAP_ITEM)
        c2 = card_overlap(OVERLAP_ITEM)
        assert c1["id"] == c2["id"]


# ---------------------------------------------------------------------------
# 5. DFM card
# ---------------------------------------------------------------------------

class TestCardDfm:
    CHECKS = [
        {"check": "min_wall", "value_mm": 0.8, "limit_mm": 1.0, "ok": False,
         "note": "thinnest wall 0.80mm vs injection_molding minimum 1.00mm"},
        {"check": "draft", "faces_below": 2, "ok": False,
         "note": "2 faces below 1deg draft"},
        {"check": "undercut", "ok": True, "note": "no undercuts"},
    ]

    def test_failed_checks_produce_cards(self):
        cards = card_dfm("doc_1", self.CHECKS)
        # Only the 2 failed checks produce cards; the passing one is skipped
        assert len(cards) == 2

    def test_passing_checks_skipped(self):
        checks = [{"check": "undercut", "ok": True, "note": "no undercuts"}]
        cards = card_dfm("doc_1", checks)
        assert len(cards) == 0

    def test_min_wall_fix(self):
        cards = card_dfm("doc_1", self.CHECKS)
        wall_card = next(c for c in cards if "wall" in c["what"])
        _valid_card(wall_card, "dfm")
        fix = wall_card["candidate_fixes"][0]
        assert fix["action"] == "increase_wall_thickness"
        assert fix["confidence"] == "table"
        assert wall_card["why"]["kind"] == "deterministic"
        # Verbatim-splice: full check dict in why.detail
        assert wall_card["why"]["detail"]["value_mm"] == 0.8

    def test_draft_fix(self):
        cards = card_dfm("doc_1", self.CHECKS)
        draft_card = next(c for c in cards if "draft" in c["what"])
        _valid_card(draft_card, "dfm")
        fix = draft_card["candidate_fixes"][0]
        assert fix["action"] == "add_draft_angle"

    def test_stable_ids_per_check(self):
        cards1 = card_dfm("doc_1", self.CHECKS)
        cards2 = card_dfm("doc_1", self.CHECKS)
        assert [c["id"] for c in cards1] == [c["id"] for c in cards2]


# ---------------------------------------------------------------------------
# 6. Sim card
# ---------------------------------------------------------------------------

class TestCardSim:
    MISSES = [
        {"summary": "bracket fails: SF=0.8 < 1.5", "sf": 0.8,
         "features": ["F3"], "bodies": ["bracket"]},
    ]

    def test_produces_card_per_miss(self):
        cards = card_sim("study_1", "doc_1", self.MISSES)
        assert len(cards) == 1
        _valid_card(cards[0], "sim")
        assert cards[0]["why"]["kind"] == "deterministic"
        # Verbatim-splice: sf must be in why.detail
        assert cards[0]["why"]["detail"]["sf"] == 0.8

    def test_empty_misses(self):
        assert card_sim("study_1", "doc_1", []) == []

    def test_stable_id(self):
        c1 = card_sim("s", "d", self.MISSES)
        c2 = card_sim("s", "d", self.MISSES)
        assert c1[0]["id"] == c2[0]["id"]


# ---------------------------------------------------------------------------
# 7. CAM card
# ---------------------------------------------------------------------------

class TestCardCam:
    SIM_RESULTS = [
        {"setup_id": "setup_1", "ok": True,
         "gouges": 2, "max_gouge_mm": 0.15,
         "rapid_collisions": 1, "holder_collisions": 0,
         "removed_volume_mm3": 1200, "warnings": []},
        {"setup_id": "setup_2", "ok": True,
         "gouges": 0, "rapid_collisions": 0, "holder_collisions": 0,
         "removed_volume_mm3": 800, "warnings": []},
    ]

    def test_one_card_per_violation_type(self):
        cards = card_cam("job_1", "doc_1", self.SIM_RESULTS)
        # setup_1 has gouge + rapid_collision = 2 cards; setup_2 is clean
        assert len(cards) == 2
        types = {c["candidate_fixes"][0]["action"] for c in cards}
        assert "adjust_toolpath_clearance" in types
        assert "add_lead_in" in types

    def test_clean_setup_no_card(self):
        results = [{"setup_id": "s", "gouges": 0, "rapid_collisions": 0,
                    "holder_collisions": 0, "warnings": []}]
        assert card_cam("j", "d", results) == []

    def test_verbatim_splice_in_why(self):
        cards = card_cam("job_1", "doc_1", self.SIM_RESULTS)
        for card in cards:
            # The full SimResult dict must be in why.detail
            assert "setup_id" in card["why"]["detail"]
            assert "gouges" in card["why"]["detail"]

    def test_all_valid(self):
        cards = card_cam("job_1", "doc_1", self.SIM_RESULTS)
        for card in cards:
            _valid_card(card, "cam")

    def test_warnings_only_card(self):
        results = [{"setup_id": "s", "gouges": 0, "rapid_collisions": 0,
                    "holder_collisions": 0, "warnings": ["tool life 90%"]}]
        cards = card_cam("j", "d", results)
        assert len(cards) == 1
        _valid_card(cards[0], "cam")

    def test_stable_ids(self):
        cards1 = card_cam("j", "d", self.SIM_RESULTS)
        cards2 = card_cam("j", "d", self.SIM_RESULTS)
        assert [c["id"] for c in cards1] == [c["id"] for c in cards2]


# ---------------------------------------------------------------------------
# 8. Export card
# ---------------------------------------------------------------------------

class TestCardExport:
    def test_basic(self):
        card = card_export("doc_1", "step", "OpenCASCADE write failed: disk full")
        _valid_card(card, "export")
        assert "step" in card["what"]
        assert card["why"]["detail"]["error"] == "OpenCASCADE write failed: disk full"

    def test_stable_id(self):
        c1 = card_export("doc_1", "step", "disk full")
        c2 = card_export("doc_1", "step", "disk full")
        assert c1["id"] == c2["id"]

    def test_different_formats_different_ids(self):
        c1 = card_export("doc_1", "step", "err")
        c2 = card_export("doc_1", "dxf", "err")
        assert c1["id"] != c2["id"]


# ---------------------------------------------------------------------------
# Attempt protocol
# ---------------------------------------------------------------------------

class TestAttemptProtocol:
    def _fresh_card(self) -> dict:
        return card_rebuild("doc_1", "F7", "rebuild failed")

    def test_record_attempt_appends(self):
        card = self._fresh_card()
        fix = card["candidate_fixes"][0]
        record_attempt(card, fix, "improved", {"overlap_mm3_before": 340, "overlap_mm3_after": 0},
                       predicted_effect="clearance 0.007 mm")
        assert len(card["attempts"]) == 1
        a = card["attempts"][0]
        assert a["n"] == 1
        assert a["result"] == "improved"
        assert a["fix_applied"] == fix
        assert a["predicted_effect"] == "clearance 0.007 mm"
        assert a["delta"] == {"overlap_mm3_before": 340, "overlap_mm3_after": 0}

    def test_result_classification_all_valid(self):
        for result in VALID_RESULTS:
            card = self._fresh_card()
            record_attempt(card, card["candidate_fixes"][0], result, {})
            assert card["attempts"][-1]["result"] == result

    def test_invalid_result_raises(self):
        card = self._fresh_card()
        with pytest.raises(ValueError, match="result must be one of"):
            record_attempt(card, card["candidate_fixes"][0], "resolved", {})

    def test_non_dict_fix_raises(self):
        card = self._fresh_card()
        with pytest.raises(TypeError):
            record_attempt(card, "not_a_dict", "same", {})  # type: ignore[arg-type]

    def test_sequential_attempts_increment_n(self):
        card = self._fresh_card()
        fix = card["candidate_fixes"][0]
        record_attempt(card, fix, "same", {})
        record_attempt(card, fix, "worse", {})
        assert card["attempts"][0]["n"] == 1
        assert card["attempts"][1]["n"] == 2

    def test_one_candidate_per_attempt_enforced_by_type(self):
        """fix_applied must be a single dict (one candidate), not a list."""
        card = self._fresh_card()
        with pytest.raises(TypeError):
            record_attempt(card, [card["candidate_fixes"][0]], "same", {})  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Budget exhaustion + escalation
# ---------------------------------------------------------------------------

class TestBudgetAndEscalation:
    def _card_with_attempts(self, n_attempts: int, on_exhaust: str = "ask_user") -> dict:
        card = card_rebuild("doc_1", "F7", "rebuild failed")
        card["budget"]["max_attempts"] = 3
        card["budget"]["on_exhaust"] = on_exhaust
        fix = card["candidate_fixes"][0]
        for _ in range(n_attempts):
            record_attempt(card, fix, "same", {})
        return card

    def test_not_exhausted_before_budget(self):
        card = self._card_with_attempts(2)
        assert not budget_exhausted(card)

    def test_exhausted_at_budget(self):
        card = self._card_with_attempts(3)
        assert budget_exhausted(card)

    def test_not_exhausted_at_zero(self):
        card = self._card_with_attempts(0)
        assert not budget_exhausted(card)

    def test_off_by_one_exactly_at_limit(self):
        """budget_exhausted must be False at n-1 and True at n."""
        card = self._card_with_attempts(0)
        max_a = card["budget"]["max_attempts"]
        fix = card["candidate_fixes"][0]
        for i in range(max_a):
            assert not budget_exhausted(card), f"should not be exhausted after {i} attempts"
            record_attempt(card, fix, "same", {})
        assert budget_exhausted(card), "should be exhausted after max_attempts"

    def test_escalate_ask_user_returns_advisory_item(self):
        card = self._card_with_attempts(3)
        result = escalate(card)
        assert result["escalation"] == "ask_user"
        item = result["advisory_item"]
        assert item["severity"] == "blocker"
        assert "rebuild_exhausted" in item["check"]
        assert item["numbers"]["attempts"] == 3
        assert result["card"] is card

    def test_escalate_dag_replan_falls_back_without_run_id(self):
        """dag_replan escalation without run_id falls back to ask_user."""
        card = self._card_with_attempts(3, on_exhaust="dag_replan")
        result = escalate(card)
        # No run_id supplied → graceful fallback to ask_user
        assert result["escalation"] == "ask_user"

    def test_escalate_fires_at_budget_no_earlier(self):
        """Escalation must not fire before budget is exhausted."""
        card = self._card_with_attempts(2)  # one short
        # Do not call escalate until budget_exhausted is True
        assert not budget_exhausted(card)
        # After one more attempt the harness should escalate
        record_attempt(card, card["candidate_fixes"][0], "same", {})
        assert budget_exhausted(card)
        result = escalate(card)
        assert result["escalation"] == "ask_user"

    def test_escalation_evidence_includes_all_attempts(self):
        card = self._card_with_attempts(3)
        result = escalate(card)
        finding = result["advisory_item"]["finding"]
        assert "attempt 1" in finding
        assert "attempt 3" in finding


# ---------------------------------------------------------------------------
# Promote blocker advisories
# ---------------------------------------------------------------------------

class TestPromoteAdvisoryBlockers:
    def _advisory(self, items: list[dict]) -> dict:
        return {"schema": "aicad.advisory/1", "seq": 1, "stage": "assembly",
                "items": items, "user_override": False}

    def test_blocker_interference_promoted(self):
        advisory = self._advisory([INTERFERENCE_ITEM])
        cards = promote_advisory_blockers(advisory)
        assert len(cards) == 1
        _valid_card(cards[0], "interference")

    def test_blocker_overlap_promoted(self):
        advisory = self._advisory([OVERLAP_ITEM])
        cards = promote_advisory_blockers(advisory)
        assert len(cards) == 1
        _valid_card(cards[0], "overlap")

    def test_non_blocker_not_promoted(self):
        item = dict(INTERFERENCE_ITEM)
        item["severity"] = "action"
        advisory = self._advisory([item])
        cards = promote_advisory_blockers(advisory)
        assert len(cards) == 0

    def test_mixed_only_blockers_promoted(self):
        action_item = dict(INTERFERENCE_ITEM)
        action_item["severity"] = "action"
        action_item["id"] = "adv_action"
        advisory = self._advisory([INTERFERENCE_ITEM, action_item])
        cards = promote_advisory_blockers(advisory)
        assert len(cards) == 1

    def test_empty_advisory(self):
        advisory = self._advisory([])
        assert promote_advisory_blockers(advisory) == []

    def test_stage_override(self):
        advisory = self._advisory([INTERFERENCE_ITEM])
        cards = promote_advisory_blockers(advisory, stage="feature")
        assert cards[0]["stage"] == "feature"


# ---------------------------------------------------------------------------
# Engine integration: build_advisory includes failure_cards
# ---------------------------------------------------------------------------

class TestEngineIntegration:
    def test_build_advisory_includes_failure_cards_key(self):
        from services.agent.advisor.engine import build_advisory, Observation
        from services.agent.advisor.engine import Finding
        from services.agent.advisor import stages

        obs = Observation(
            stage="assembly",
            findings=[Finding(
                kind="interference",
                check=stages.CHECK_INTERFERENCE,
                severity="blocker",
                finding="bodies A/B interfere 100 mm³",
                numbers={"overlap_mm3": 100},
                refs={"doc_id": "doc_1", "bodies": ["A", "B"]},
                source_kind="kernel",
                source_detail="doc_eval_interference",
            )],
            seq=1,
        )
        advisory = build_advisory(obs)
        assert "failure_cards" in advisory
        assert isinstance(advisory["failure_cards"], list)

    def test_blocker_produces_failure_card_in_advisory(self):
        from services.agent.advisor.engine import build_advisory, Observation
        from services.agent.advisor.engine import Finding
        from services.agent.advisor import stages

        obs = Observation(
            stage="assembly",
            findings=[Finding(
                kind="interference",
                check=stages.CHECK_INTERFERENCE,
                severity="blocker",
                finding="bodies X/Y interfere 200 mm³",
                numbers={"overlap_mm3": 200},
                refs={"doc_id": "doc_2", "bodies": ["X", "Y"]},
                source_kind="kernel",
                source_detail="doc_eval_interference",
            )],
            seq=2,
        )
        advisory = build_advisory(obs)
        assert len(advisory["failure_cards"]) == 1
        _valid_card(advisory["failure_cards"][0], "interference")

    def test_non_blocker_no_failure_cards(self):
        from services.agent.advisor.engine import build_advisory, Observation
        from services.agent.advisor.engine import Finding
        from services.agent.advisor import stages

        obs = Observation(
            stage="assembly",
            findings=[Finding(
                kind="interference",
                check=stages.CHECK_INTERFERENCE,
                severity="action",   # not a blocker
                finding="bodies A/B interfere 50 mm³",
                numbers={"overlap_mm3": 50},
                refs={},
                source_kind="kernel",
                source_detail="_fit_report",
            )],
            seq=3,
        )
        advisory = build_advisory(obs)
        assert advisory["failure_cards"] == []


# ---------------------------------------------------------------------------
# Ledger integration (smoke test — no raises)
# ---------------------------------------------------------------------------

class TestLedgerIntegration:
    class _FakeLedger:
        def __init__(self):
            self.events = []

        def append(self, actor, kind, payload):
            self.events.append({"actor": actor, "kind": kind, "payload": payload})

    def test_note_card_writes_artifact_event(self):
        ledger = self._FakeLedger()
        card = card_rebuild("doc_1", "F7", "err")
        ledger_note_card(ledger, "harness", card)
        assert len(ledger.events) == 1
        ev = ledger.events[0]
        assert ev["kind"] == "artifact"
        assert ev["payload"]["kind"] == "failure_card"
        assert ev["payload"]["id"] == card["id"]
        # Full card embedded
        assert ev["payload"]["card"] is card

    def test_note_attempt_writes_artifact_event(self):
        ledger = self._FakeLedger()
        card = card_rebuild("doc_1", "F7", "err")
        fix = card["candidate_fixes"][0]
        record_attempt(card, fix, "improved", {"delta": 1})
        attempt = card["attempts"][0]
        ledger_note_attempt(ledger, "harness", card, attempt)
        assert len(ledger.events) == 1
        ev = ledger.events[0]
        assert ev["kind"] == "artifact"
        assert ev["payload"]["kind"] == "fixer_attempt"
        assert "attempt_1" in ev["payload"]["id"]

    def test_none_ledger_is_noop(self):
        """ledger=None must never raise."""
        card = card_rebuild("doc_1", "F7", "err")
        ledger_note_card(None, "harness", card)   # must not raise
        record_attempt(card, card["candidate_fixes"][0], "same", {})
        ledger_note_attempt(None, "harness", card, card["attempts"][0])  # must not raise

    def test_broken_ledger_is_noop(self):
        """A ledger that raises on append must not propagate."""
        class BrokenLedger:
            def append(self, *a, **kw):
                raise RuntimeError("disk full")

        card = card_rebuild("doc_1", "F7", "err")
        ledger_note_card(BrokenLedger(), "harness", card)  # must not raise


# ---------------------------------------------------------------------------
# Deterministic id uniqueness across classes
# ---------------------------------------------------------------------------

class TestFcIdStability:
    def test_fc_id_pure_function(self):
        """Same inputs always produce same id."""
        id1 = _fc_id("rebuild", "doc_1||", "err")
        id2 = _fc_id("rebuild", "doc_1||", "err")
        assert id1 == id2

    def test_different_classes_different_ids(self):
        key = "doc_1||"
        detail = "some detail"
        ids = {_fc_id(cls, key, detail) for cls in
               ("rebuild", "solver", "interference", "overlap", "dfm", "sim", "cam", "export")}
        assert len(ids) == 8  # all unique

    def test_where_key_deterministic(self):
        where1 = {"doc_id": "d", "features": ["F3", "F7"], "bodies": ["a", "b"]}
        where2 = {"doc_id": "d", "features": ["F7", "F3"], "bodies": ["b", "a"]}
        # Both sorted → same key
        assert _where_key(where1) == _where_key(where2)
