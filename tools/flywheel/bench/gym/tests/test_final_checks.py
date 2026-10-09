"""P2-8 final-check selection and P1-6 ledger-only REAL grading."""
from __future__ import annotations

from tools.flywheel.bench.gym import runner as runner_mod
from tools.flywheel.bench.gym.challenges import (final_check_absence, final_checks,
                                                 last_record, ledger_artifact,
                                                 load_challenge)
from tools.flywheel.bench.gym.runner import ExecProbe, run_cell


def _call(tool: str, **extra) -> dict:
    return {"kind": "tool_call", "payload": {"tool": tool, **extra}}


def _art(kind: str, **fields) -> dict:
    return {"kind": "artifact", "payload": {"kind": kind, **fields}}


def test_initial_clash_then_repair_reads_the_final_clean_check():
    ledger = {"events": [
        _call("check_interference"), _art("interference", clashes=2),
        _call("add_features"),
        _call("check_interference"), _art("interference", clashes=0),
    ]}
    assert ledger_artifact(ledger, "interference")["clashes"] == 0


def test_check_before_the_last_mutation_is_stale():
    ledger = {"events": [
        _art("interference", clashes=0), _call("asm_mate"),
    ]}
    assert ledger_artifact(ledger, "interference") is None
    assert "stale" in final_check_absence(ledger, "interference")
    assert final_check_absence({"events": []}, "battery") == "no battery artifact recorded"


def test_errored_check_and_failed_mutation_do_not_count():
    ledger = {"events": [
        _art("battery", verdict="PASS"),
        _call("add_features", ok=False, error="refused"),   # did not mutate
        _art("battery", verdict="PASS", error="battery crashed"),  # not a verdict
    ]}
    hits = final_checks(ledger, "battery")
    assert len(hits) == 1 and "error" not in hits[0]


def test_records_are_not_subject_to_staleness():
    ledger = {"events": [_art("standard_part", family="shcs"), _call("asm_mate")]}
    assert last_record(ledger, "standard_part")["family"] == "shcs"


def test_c14_repaired_assembly_passes_final_interference_and_interface():
    ch = load_challenge("C14")
    turns = ch.scripts[1]["DL1"]
    events = []
    for t in turns:
        kind = t.get("_kind")
        payload = {k: v for k, v in t.items() if k != "_kind"}
        events.append({"kind": kind, "payload": payload})
    # An initial failing interface/interference pair BEFORE the build is superseded.
    stale = [_art("interface", verdict="FAIL", **{"pass": False}),
             _art("interference", clashes=3)]
    graded = ch.grade({"events": stale + events}, {})
    by = {c["id"]: c for c in graded["criteria"]}
    assert by["AC2"]["result"] == "PASS", by["AC2"]
    assert by["AC3"]["result"] == "PASS", by["AC3"]
    # …and a clean check followed by a later mutation is no longer final.
    late = events + [_call("asm_mate")]
    by = {c["id"]: c for c in ch.grade({"events": late}, {})["criteria"]}
    assert by["AC3"]["result"] == "FAIL" and "stale" in by["AC3"]["detail"]


def test_real_cell_ledger_only_pass_is_unverified_and_not_counted(tmp_path, monkeypatch):
    """A REAL cell whose ledger claims everything the graders want is still not a pass:
    the gym graders never reopen a native artifact."""
    ch = load_challenge("C1")

    def fake_mission(challenge, run_id, tier):
        return {"status": "completed", "phase": "deliver", "mode": "real"}

    def fold_claims(run_id, ledger):
        runner_mod.replay_turns_into_ledger(ledger, ch, 1)

    monkeypatch.setattr(runner_mod, "_run_mission_real", fake_mission)
    monkeypatch.setattr(runner_mod, "_merge_mission_ledger", fold_claims)
    probe = ExecProbe(core_available=True, frontier_key=True, local_model=True,
                      real_agent_fn=True, dag_deps=True)
    cell = run_cell(ch, "local-24k", 1, cycle="verify", probe=probe, runs_dir=tmp_path,
                    real=True)
    assert cell.mode == "real"
    assert cell.graded["ledger_passed"] is True
    assert cell.graded["verification"] == "ledger_only_unverified"
    assert cell.passed is False and cell.graded["passed"] is False
