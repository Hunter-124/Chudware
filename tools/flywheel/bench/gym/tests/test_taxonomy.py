"""Unit tests for the failure-taxonomy classifier.

These prove the CLASSIFIER LOGIC against HAND-AUTHORED replay ledgers — one per failure
class. The ledgers are synthetic fixtures (explicitly labeled), not model output: the
point is that the classifier fires the right class on the right evidence, and does NOT
fire on a clean ledger. This is legitimate (proving logic on labeled data), and separate
from any model evaluation.

Run: PYTHONPATH=<repo> python3 -m pytest tools/flywheel/bench/gym/tests/test_taxonomy.py
or:  PYTHONPATH=<repo> python3 tools/flywheel/bench/gym/tests/test_taxonomy.py
"""
from __future__ import annotations

from tools.flywheel.bench.gym.taxonomy import (
    classify_ledger, FailureClass, TAXONOMY, misuse_fingerprints,
)


def _ledger(events: list[dict]) -> dict:
    # attach seq numbers as the real ledger does
    for i, e in enumerate(events):
        e.setdefault("seq", i)
        e.setdefault("actor", e.get("actor", "role"))
    return {"events": events, "state": {}}


_MANIFEST = {
    "deliverables": [{
        "id": "DL1",
        "acceptance_criteria": [
            {"id": "AC1", "check": {"tool": "fit_report"}},
            {"id": "AC2", "check": {"tool": "verify_battery"}},
        ],
    }],
    "ambiguities": [{"text": "shaft material not specified", "blocking": True}],
}


def _classes(cls_list) -> set:
    return {c.failure_class for c in cls_list}


# --- clean ledger: nothing fires -------------------------------------------

def test_clean_ledger_fires_nothing():
    lg = _ledger([
        {"kind": "artifact", "payload": {"kind": "manifest",
            "deliverables": _MANIFEST["deliverables"], "ambiguities": []}},
        {"kind": "question", "payload": {"id": "Q1", "text": "shaft material?",
            "bearing": "critical"}},
        {"kind": "dimension", "payload": {"id": "DIM1", "name": "bore", "value": 20}},
        {"kind": "decision", "payload": {"id": "d1", "text": "bore per DIM1",
            "node": "n1", "tool": "fit_report"}},
    ])
    graded = {"criteria": [{"id": "AC1", "result": "PASS"}], "passed": True}
    res = classify_ledger(lg, manifest={**_MANIFEST, "ambiguities": []}, graded=graded)
    assert res == [], f"clean ledger should fire nothing, got {_classes(res)}"


# --- wrong_question ---------------------------------------------------------

def test_wrong_question_useless_and_missed():
    lg = _ledger([
        {"kind": "artifact", "payload": {"kind": "manifest",
            "deliverables": _MANIFEST["deliverables"],
            "ambiguities": _MANIFEST["ambiguities"]}},
        {"kind": "question", "payload": {"id": "Q1", "text": "what color?",
            "bearing": "none"}},
    ])
    res = classify_ledger(lg, manifest=_MANIFEST,
                          graded={"criteria": [], "passed": True})
    fired = [c for c in res if c.failure_class == FailureClass.WRONG_QUESTION.value]
    assert len(fired) == 2, f"expected useless + missed, got {[c.detail for c in fired]}"
    for c in fired:
        assert c.evidence, "each classification must carry evidence"


# --- incoherent_geometry ----------------------------------------------------

def test_incoherent_geometry_on_failed_ac():
    lg = _ledger([
        {"kind": "artifact", "payload": {"kind": "battery", "id": "battery_DL1",
            "verdict": "FAIL"}},
    ])
    graded = {"criteria": [{"id": "AC2", "result": "FAIL"}], "passed": False}
    res = classify_ledger(lg, manifest=_MANIFEST, graded=graded)
    fired = [c for c in res if c.failure_class == FailureClass.INCOHERENT_GEOMETRY.value]
    assert fired and "AC2" in fired[0].detail
    assert fired[0].evidence_seqs, "must ref the battery event"


# --- tool_misuse ------------------------------------------------------------

def test_tool_misuse_wrong_tool_for_criterion():
    lg = _ledger([
        {"kind": "artifact", "payload": {"kind": "manifest",
            "deliverables": _MANIFEST["deliverables"], "ambiguities": []}},
        {"kind": "tool_call", "payload": {"tool": "measure_part", "for_ac": "AC1",
            "node": "n1"}},   # AC1's declared tool is fit_report
    ])
    res = classify_ledger(lg, manifest={**_MANIFEST, "ambiguities": []},
                          graded={"criteria": [], "passed": True})
    fired = [c for c in res if c.failure_class == FailureClass.TOOL_MISUSE.value]
    assert fired and "measure_part" in fired[0].detail


def test_build_tool_serving_criterion_is_not_misuse():
    """A BUILD/mutation tool (add_features) tagged with for_ac is construction, not a
    verification claim — it must NOT fire tool_misuse even though add_features != the
    criterion's check tool. Regression for the C6-seed3 false-positive."""
    lg = _ledger([
        {"kind": "artifact", "payload": {"kind": "manifest",
            "deliverables": _MANIFEST["deliverables"], "ambiguities": []}},
        {"kind": "tool_call", "payload": {"tool": "add_features", "for_ac": "AC1",
            "node": "n_build", "args": {"op": "bore"}}},   # AC1 check tool is fit_report
    ])
    res = classify_ledger(lg, manifest={**_MANIFEST, "ambiguities": []},
                          graded={"criteria": [], "passed": True})
    assert not [c for c in res if c.failure_class == FailureClass.TOOL_MISUSE.value], \
        "a build tool serving a criterion is not tool_misuse"


def test_verification_flag_still_fires_misuse():
    """An explicit verifies=True call with the wrong tool DOES fire tool_misuse."""
    lg = _ledger([
        {"kind": "artifact", "payload": {"kind": "manifest",
            "deliverables": _MANIFEST["deliverables"], "ambiguities": []}},
        {"kind": "tool_call", "payload": {"tool": "add_features", "for_ac": "AC1",
            "verifies": True, "node": "n_verify"}},
    ])
    res = classify_ledger(lg, manifest={**_MANIFEST, "ambiguities": []},
                          graded={"criteria": [], "passed": True})
    assert [c for c in res if c.failure_class == FailureClass.TOOL_MISUSE.value], \
        "an explicit verification with the wrong tool must fire tool_misuse"


def test_tool_misuse_preflight_reject():
    lg = _ledger([
        {"kind": "tool_call", "payload": {"tool": "add_features", "preflight": "reject",
            "reason": "would gouge", "node": "n1"}},
    ])
    res = classify_ledger(lg, manifest=None, graded={"criteria": [], "passed": True})
    fired = [c for c in res if c.failure_class == FailureClass.TOOL_MISUSE.value]
    assert fired, "a preflight reject must fire tool_misuse"


def test_tool_misuse_rule_b_scoped_to_scripted_shape_no_real_signal():
    """Rule (b) (wrong tool for a criterion) depends on for_ac/verifies — fields
    ONLY the scripted gym emits (runner.py replay_turns_into_ledger). Real
    tool_call payloads (agent_turn._log) never tag a call with the criterion it
    claims to verify, so there is no real signal to derive the same check from.
    Decision: keep rule (b) scripted-only rather than delete it (the gym's
    challenge fixtures rely on for_ac-driven misuse detection) — it must
    correctly NOT fire on a real-shaped call using the "wrong" tool, rather than
    guess which criterion an untagged call was meant to verify."""
    lg = _ledger([
        {"kind": "agent_turn", "payload": {"event": "tool_call", "tool": "measure_part",
            "args": {"nominal": 20}, "ok": True, "result": "20.02mm"}},
    ])
    res = classify_ledger(lg, manifest=_MANIFEST,
                          graded={"criteria": [], "passed": True})
    assert not [c for c in res if c.failure_class == FailureClass.TOOL_MISUSE.value]


# --- context_loss -----------------------------------------------------------

def test_context_loss_cites_nonexistent_ref():
    lg = _ledger([
        {"kind": "dimension", "payload": {"id": "DIM1", "name": "bore", "value": 20}},
        {"kind": "decision", "payload": {"id": "d1",
            "text": "flange follows DIM99 + 2*wall", "node": "n1"}},   # DIM99 never minted
    ])
    res = classify_ledger(lg, manifest=None, graded={"criteria": [], "passed": True})
    fired = [c for c in res if c.failure_class == FailureClass.CONTEXT_LOSS.value]
    assert fired and "DIM99" in fired[0].detail


def test_context_loss_manifest_ids_are_known():
    """Real cells pass the manifest as an argument (no scripted manifest
    artifact event) — citing the manifest's own DL/AC ids must NOT be context_loss."""
    lg = _ledger([
        {"kind": "agent_turn", "payload": {"event": "done",
            "text": "handoff for DL1: AC1 satisfied pending review"}},
    ])
    res = classify_ledger(lg, manifest=_MANIFEST,
                          graded={"criteria": [], "passed": True})
    assert not [c for c in res if c.failure_class == FailureClass.CONTEXT_LOSS.value]


def test_context_loss_cites_id_before_it_is_minted():
    """Ordering regression: a ledger-wide id UNION (the old behaviour) misses a ref
    cited BEFORE the event that minted it — an id must only count as known for
    events strictly AFTER its minting event."""
    lg = _ledger([
        {"kind": "decision", "payload": {"id": "d1",
            "text": "flange bore sized off DIM7", "node": "n1"}},   # DIM7 not minted yet
        {"kind": "dimension", "payload": {"id": "DIM7", "name": "bore", "value": 20}},
    ])
    res = classify_ledger(lg, manifest=None, graded={"criteria": [], "passed": True})
    fired = [c for c in res if c.failure_class == FailureClass.CONTEXT_LOSS.value]
    assert fired and "DIM7" in fired[0].detail


# --- abstraction_skip -------------------------------------------------------

def test_abstraction_skip_ladder_bounce():
    lg = _ledger([
        {"kind": "artifact", "payload": {"kind": "ladder_bounce",
            "stage": "ladder_geometry", "bounce": True,
            "finding": "geometry_plan before function_map omissions checklist"}},
    ])
    res = classify_ledger(lg, manifest=None, graded={"criteria": [], "passed": True})
    fired = [c for c in res if c.failure_class == FailureClass.ABSTRACTION_SKIP.value]
    assert fired, "a ladder validator bounce must fire abstraction_skip"


# --- grind ------------------------------------------------------------------

def test_grind_three_identical_attempts():
    same = {"tool": "run_fea_static", "node": "n1", "args": {"load": 500},
            "hypothesis": "rerun fea unchanged"}
    lg = _ledger([
        {"kind": "tool_call", "payload": dict(same)},
        {"kind": "tool_call", "payload": dict(same)},
        {"kind": "tool_call", "payload": dict(same)},
    ])
    res = classify_ledger(lg, manifest=None, graded={"criteria": [], "passed": True},
                          grind_threshold=3)
    fired = [c for c in res if c.failure_class == FailureClass.GRIND.value]
    assert fired and len(fired[0].evidence_seqs) == 3


def test_grind_does_not_fire_when_hypothesis_changes():
    lg = _ledger([
        {"kind": "tool_call", "payload": {"tool": "run_fea_static", "node": "n1",
            "hypothesis": "baseline"}},
        {"kind": "tool_call", "payload": {"tool": "run_fea_static", "node": "n1",
            "hypothesis": "thicker web"}},
        {"kind": "tool_call", "payload": {"tool": "run_fea_static", "node": "n1",
            "hypothesis": "added rib"}},
    ])
    res = classify_ledger(lg, manifest=None, graded={"criteria": [], "passed": True},
                          grind_threshold=3)
    assert not [c for c in res if c.failure_class == FailureClass.GRIND.value]


def test_grind_real_shape_repeated_identical_calls_fire():
    """Real tool_call payloads (agent_turn._log) carry no 'node' at all — grind
    must still detect >=N identical repeats via the tool+args signature."""
    same = {"event": "tool_call", "tool": "run_fea_static",
            "args": {"load_N": 500, "material": "1045"}, "ok": True}
    lg = _ledger([
        {"kind": "agent_turn", "payload": dict(same)},
        {"kind": "agent_turn", "payload": dict(same)},
        {"kind": "agent_turn", "payload": dict(same)},
    ])
    res = classify_ledger(lg, manifest=None,
                          graded={"criteria": [], "passed": True}, grind_threshold=3)
    fired = [c for c in res if c.failure_class == FailureClass.GRIND.value]
    assert fired and len(fired[0].evidence_seqs) == 3


def test_grind_real_shape_different_deliverables_do_not_collapse():
    """Different tool+args calls for different real deliverables (identified only
    by their differing args, e.g. doc_id) must not be grouped together."""
    lg = _ledger([
        {"kind": "agent_turn", "payload": {"event": "tool_call", "tool": "verify_battery",
            "args": {"doc_id": "part_a"}, "ok": True}},
        {"kind": "agent_turn", "payload": {"event": "tool_call", "tool": "verify_battery",
            "args": {"doc_id": "part_b"}, "ok": True}},
    ])
    res = classify_ledger(lg, manifest=None,
                          graded={"criteria": [], "passed": True}, grind_threshold=3)
    assert not [c for c in res if c.failure_class == FailureClass.GRIND.value]


def test_grind_real_decision_events_same_stage_different_id_do_not_collapse():
    """Regression: real decision events (ledger.log_decision) carry a workflow
    'stage' shared by EVERY deliverable in that phase, not a deliverable id.
    Three decisions with identical text (so the fingerprint alone can't tell them
    apart) but different decision ids, all at the same stage, must NOT collapse
    into one grind bucket keyed on the shared stage (the old '?'/stage fallback
    would have wrongly grouped and fired grind here)."""
    lg = _ledger([
        {"kind": "decision", "payload": {"id": "fit_bore", "stage": "specify",
            "text": "verified clearance fit", "rationale": "fit_report result"}},
        {"kind": "decision", "payload": {"id": "fit_shaft", "stage": "specify",
            "text": "verified clearance fit", "rationale": "fit_report result"}},
        {"kind": "decision", "payload": {"id": "fit_pin", "stage": "specify",
            "text": "verified clearance fit", "rationale": "fit_report result"}},
    ])
    res = classify_ledger(lg, manifest=None,
                          graded={"criteria": [], "passed": True}, grind_threshold=3)
    assert not [c for c in res if c.failure_class == FailureClass.GRIND.value]


# --- advisory_ignored -------------------------------------------------------

def test_advisory_ignored_contradicted_without_dispute():
    lg = _ledger([
        {"kind": "advisory", "payload": {"id": "ADV1", "topic": "petg_snap_marginal",
            "text": "PETG snaps are marginal at this strain"}},
        {"kind": "decision", "payload": {"id": "d1", "topic": "petg_snap_marginal",
            "action": "proceed", "text": "proceeding with PETG snap as-is"}},
    ])
    res = classify_ledger(lg, manifest=None, graded={"criteria": [], "passed": True})
    fired = [c for c in res if c.failure_class == FailureClass.ADVISORY_IGNORED.value]
    assert fired and len(fired[0].evidence_seqs) == 2


def test_advisory_not_ignored_when_disputed():
    lg = _ledger([
        {"kind": "advisory", "payload": {"id": "ADV1", "topic": "petg_snap_marginal"}},
        {"kind": "replan", "payload": {"topic": "petg_snap_marginal",
            "scope": "spec", "text": "propose cantilever redesign"}},
        {"kind": "decision", "payload": {"id": "d1", "topic": "petg_snap_marginal",
            "action": "proceed"}},
    ])
    res = classify_ledger(lg, manifest=None, graded={"criteria": [], "passed": True})
    assert not [c for c in res if c.failure_class == FailureClass.ADVISORY_IGNORED.value]


# --- advisory_ignored (real producer shape) ----------------------------------

_REAL_ADVISORY_ITEM = {
    "id": "ADV-7-interference-boss_clash",
    "severity": "warn",
    "check": "check_interference",
    "finding": "boss clashes with mating face",
    "refs": {"doc_id": "doc123"},
    "numbers": {},
    "candidate_fixes": [{"tool": "edit_feature", "args": {}}],
    "helper_tools": [],
    "source": {"kind": "kernel", "detail": "interference_check"},
}


def _real_advisory_payload(items=None):
    """advisor.engine.build_advisory's real shape — no top-level topic/id, ids
    live per-item (engine._finding_to_item)."""
    return {
        "schema": "aicad.advisory/1", "seq": 3, "stage": "assembly",
        "items": items if items is not None else [dict(_REAL_ADVISORY_ITEM)],
        "user_override": False, "failure_cards": [],
    }


def test_advisory_ignored_no_false_positive_when_payload_has_no_identifiable_id():
    """P0: a payload with no top-level topic/id AND no items (degenerate real
    shape) must never fire. The old comparison was
    ``pa.get("id") == pe.get("contradicts_advisory")`` — both None on virtually
    every real advisory/tool_call pair, so None == None fired on almost
    everything."""
    lg = _ledger([
        {"kind": "advisory", "payload": {"schema": "aicad.advisory/1", "seq": 1,
            "stage": "assembly", "items": [], "user_override": False,
            "failure_cards": []}},
        {"kind": "agent_turn", "payload": {"event": "tool_call", "tool": "add_features",
            "args": {"op": "boss"}, "ok": True}},
    ])
    res = classify_ledger(lg, manifest=None, graded={"criteria": [], "passed": True})
    assert not [c for c in res if c.failure_class == FailureClass.ADVISORY_IGNORED.value]


def test_advisory_ignored_real_shape_no_false_positive_when_heeded():
    """P0 regression: a real advisory (with a real, non-empty item id) followed by
    an unrelated real tool_call that heeds it (no contradiction tag at all) must
    NOT fire advisory_ignored."""
    lg = _ledger([
        {"kind": "advisory", "payload": _real_advisory_payload()},
        {"kind": "agent_turn", "payload": {"event": "tool_call", "tool": "edit_feature",
            "args": {"feature": "boss_offset", "offset_mm": -2.0}, "ok": True}},
    ])
    res = classify_ledger(lg, manifest=None, graded={"criteria": [], "passed": True})
    assert not [c for c in res if c.failure_class == FailureClass.ADVISORY_IGNORED.value]


def test_advisory_ignored_real_shape_matches_per_item_id():
    """Real advisory ids live in items[] (engine._finding_to_item), not at the
    payload's top level — a later action tagged with the item's id must still be
    recognised as a contradiction."""
    lg = _ledger([
        {"kind": "advisory", "payload": _real_advisory_payload()},
        {"kind": "agent_turn", "payload": {"event": "tool_call", "tool": "add_features",
            "args": {"op": "boss"}, "ok": True,
            "contradicts_advisory": "ADV-7-interference-boss_clash"}},
    ])
    res = classify_ledger(lg, manifest=None, graded={"criteria": [], "passed": True})
    fired = [c for c in res if c.failure_class == FailureClass.ADVISORY_IGNORED.value]
    assert fired and len(fired[0].evidence_seqs) == 2


def test_advisory_ignored_real_shape_disputed_via_advisory_dispute_tool():
    """A real advisory_dispute event (services/mcp/advisor_tools.advisory_dispute,
    ledger kind "advisory_dispute", payload carries "advisory_id") referencing the
    item id must suppress the class, same as the scripted 'replan'/'dispute'
    kinds."""
    lg = _ledger([
        {"kind": "advisory", "payload": _real_advisory_payload()},
        {"kind": "advisory_dispute", "payload": {
            "advisory_id": "ADV-7-interference-boss_clash", "reason": "acceptable risk",
            "source_kind": "kernel", "waiver_required": True, "status": "pending"}},
        {"kind": "agent_turn", "payload": {"event": "tool_call", "tool": "add_features",
            "args": {"op": "boss"}, "ok": True,
            "contradicts_advisory": "ADV-7-interference-boss_clash"}},
    ])
    res = classify_ledger(lg, manifest=None, graded={"criteria": [], "passed": True})
    assert not [c for c in res if c.failure_class == FailureClass.ADVISORY_IGNORED.value]


# --- execution_stall --------------------------------------------------------

def test_execution_stall_all_calls_failed_suppresses_incoherent():
    """The real-run stall shape: every real tool call errored (agent_turn events),
    ACs failed — execution_stall must fire WITH evidence seqs, and
    incoherent_geometry (whose evidence would be empty) must be suppressed."""
    lg = _ledger([
        {"kind": "agent_turn", "payload": {"event": "tool_call", "tool": "ledger_get",
            "args": {}, "ok": False, "error": "tool 'ledger_get' could not be resolved"}},
        {"kind": "agent_turn", "payload": {"event": "tool_call", "tool": "add_features",
            "args": {"features": []}, "ok": False,
            "error": "add_features() got an unexpected keyword argument 'features'"}},
    ])
    graded = {"criteria": [{"id": "AC1", "result": "FAIL"}], "passed": False}
    res = classify_ledger(lg, manifest=_MANIFEST, graded=graded)
    stall = [c for c in res if c.failure_class == FailureClass.EXECUTION_STALL.value]
    assert len(stall) == 1, _classes(res)
    assert stall[0].evidence_seqs == [0, 1], "must ref the errored call events"
    assert stall[0].evidence, "must carry evidence refs"
    assert not [c for c in res
                if c.failure_class == FailureClass.INCOHERENT_GEOMETRY.value], \
        "a pre-BUILD stall must not classify as incoherent_geometry"


def test_execution_stall_turns_without_any_tool_call():
    lg = _ledger([
        {"kind": "agent_turn", "payload": {"event": "turn", "kind": "text",
                                           "text": "I need more information."}},
        {"kind": "agent_turn", "payload": {"event": "turn", "kind": "text",
                                           "text": "Please provide the brief."}},
    ])
    graded = {"criteria": [{"id": "AC1", "result": "FAIL"}], "passed": False}
    res = classify_ledger(lg, manifest=_MANIFEST, graded=graded)
    stall = [c for c in res if c.failure_class == FailureClass.EXECUTION_STALL.value]
    assert stall and stall[0].evidence_seqs


def test_no_stall_when_one_call_succeeded():
    """One successful execution means the run got past the stall — a failed AC is
    then genuine incoherent_geometry, not execution_stall."""
    lg = _ledger([
        {"kind": "agent_turn", "payload": {"event": "tool_call", "tool": "ledger_get",
            "args": {}, "ok": False, "error": "tool 'ledger_get' could not be resolved"}},
        {"kind": "agent_turn", "payload": {"event": "tool_call", "tool": "add_features",
            "args": {"ops": []}, "ok": True, "result": "{\"ok\": true}"}},
    ])
    graded = {"criteria": [{"id": "AC1", "result": "FAIL"}], "passed": False}
    res = classify_ledger(lg, manifest=_MANIFEST, graded=graded)
    assert not [c for c in res
                if c.failure_class == FailureClass.EXECUTION_STALL.value]
    assert [c for c in res
            if c.failure_class == FailureClass.INCOHERENT_GEOMETRY.value]


def test_no_stall_on_passed_run_or_empty_ledger():
    graded_pass = {"criteria": [{"id": "AC1", "result": "PASS"}], "passed": True}
    lg = _ledger([
        {"kind": "agent_turn", "payload": {"event": "tool_call", "tool": "x",
                                           "ok": False, "error": "boom"}},
    ])
    assert not [c for c in classify_ledger(lg, manifest=None, graded=graded_pass)
                if c.failure_class == FailureClass.EXECUTION_STALL.value]
    # no tool activity at all (mock cells without agent turns) → not a stall
    graded_fail = {"criteria": [{"id": "AC1", "result": "FAIL"}], "passed": False}
    empty = _ledger([{"kind": "artifact", "payload": {"kind": "battery",
                                                      "id": "battery_DL1"}}])
    res = classify_ledger(empty, manifest=None, graded=graded_fail)
    assert not [c for c in res
                if c.failure_class == FailureClass.EXECUTION_STALL.value]
    assert [c for c in res
            if c.failure_class == FailureClass.INCOHERENT_GEOMETRY.value]


def test_kwarg_error_call_is_also_tool_misuse_but_resolution_gap_is_not():
    """Tool-call normalisation: an agent-turn call the tool REJECTED (bad kwargs) is
    tool_misuse; a pure resolution failure is a harness gap → stall only."""
    lg = _ledger([
        {"kind": "agent_turn", "payload": {"event": "tool_call", "tool": "add_features",
            "args": {"x": 1}, "ok": False,
            "error": "add_features() got an unexpected keyword argument 'x'"}},
        {"kind": "agent_turn", "payload": {"event": "tool_call", "tool": "ledger_get",
            "args": {}, "ok": False, "error": "tool 'ledger_get' could not be resolved"}},
    ])
    res = classify_ledger(lg, manifest=None,
                          graded={"criteria": [], "passed": False})
    misuse = [c for c in res if c.failure_class == FailureClass.TOOL_MISUSE.value]
    assert len(misuse) == 1 and "add_features" in misuse[0].detail


def test_grind_counts_real_agent_turn_calls():
    same = {"event": "tool_call", "tool": "ledger_get", "args": {}, "ok": False,
            "error": "tool 'ledger_get' could not be resolved", "node": "n1"}
    lg = _ledger([
        {"kind": "agent_turn", "payload": dict(same)},
        {"kind": "agent_turn", "payload": dict(same)},
        {"kind": "agent_turn", "payload": dict(same)},
    ])
    res = classify_ledger(lg, manifest=None,
                          graded={"criteria": [], "passed": False},
                          grind_threshold=3)
    assert [c for c in res if c.failure_class == FailureClass.GRIND.value]


def test_policy_refusals_are_outcomes_not_tool_failures():
    """Calls declined before dispatch are harness decisions, not failed attempts."""
    calls = []
    for status in ("call_cap_reached", "loop_guard_refused"):
        for _ in range(3):
            calls.append({"kind": "agent_turn", "payload": {
                "event": "tool_call", "tool": "add_features",
                "args": {"ops": []}, "ok": False, "status": status,
                "policy_refusal": True, "executed": False,
                "error": f"{status}: call declined before dispatch",
            }})
    lg = _ledger(calls)
    res = classify_ledger(
        lg, manifest=None, graded={"criteria": [], "passed": False},
        grind_threshold=3,
    )
    classes = _classes(res)
    assert FailureClass.POLICY_REFUSAL.value in classes
    assert FailureClass.EXECUTION_STALL.value not in classes
    assert FailureClass.TOOL_MISUSE.value not in classes
    assert FailureClass.GRIND.value not in classes
    assert misuse_fingerprints(lg) == []
    details = " ".join(c.detail for c in res
                       if c.failure_class == FailureClass.POLICY_REFUSAL.value)
    assert "call_cap_reached" in details
    assert "loop_guard_refused" in details


def test_terminal_infrastructure_outcomes_cannot_pass_real_cell():
    for payload in (
        {"event": "reply_truncated_fatal"},
        {"event": "turn_terminal", "stop_reason": "guardrail_internal_error"},
        {"event": "done_guardrail_internal_error", "error": "KeyError: state"},
    ):
        graded = {"criteria": [], "passed": True}
        res = classify_ledger(
            _ledger([{"kind": "agent_turn", "payload": payload}]),
            manifest=None,
            graded=graded,
        )
        assert graded["passed"] is False
        assert FailureClass.HARNESS_FAULT.value in _classes(res)


def test_every_taxonomy_class_is_covered():
    """Guard: the test file exercises every class in TAXONOMY."""
    covered = {
        FailureClass.WRONG_QUESTION.value, FailureClass.INCOHERENT_GEOMETRY.value,
        FailureClass.TOOL_MISUSE.value, FailureClass.CONTEXT_LOSS.value,
        FailureClass.ABSTRACTION_SKIP.value, FailureClass.GRIND.value,
        FailureClass.ADVISORY_IGNORED.value, FailureClass.EXECUTION_STALL.value,
    }
    assert covered == set(TAXONOMY), f"uncovered classes: {set(TAXONOMY) - covered}"


def main() -> None:
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  PASS {fn.__name__}")
    print(f"\nTAXONOMY TESTS PASSED ({len(fns)} tests, all {len(TAXONOMY)} classes covered)")


if __name__ == "__main__":
    main()
