"""Turn decoding, the fixed mandatory denominator, and ERROR-vs-FAIL classification.

The decoding fixture pairs a REAL run_mission event log (trimmed from
scratch/mission-sessions/smoke6axis4.events.jsonl) with a ``--result-json`` record in the
current ``agent_admin_turn`` response shape (services/agent/worker_rpc.py: ``turn.result``
is terminal TEXT; usage/persistence ride on the response). No engine needed here.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from benchmarks.artifact_validity.scoring import (
    artifact_freshness, decode_turn, error_grade, format_report, summarize,
    summarize_events, trial_verdict,
)
from benchmarks.artifact_validity.tasks.schema import (
    CheckResult, Status, Task, check, check_ids, declared_id, run_check, task_status,
)

REC = Path(__file__).resolve().parent / "fixtures" / "recorded"


def _ok_record() -> dict:
    return json.loads((REC / "turn_ok.result.json").read_text())


# ---------------------------------------------------------------- decoding


def test_recorded_current_format_reply_decodes_as_completed():
    t = decode_turn(_ok_record(), driver_returncode=0)
    assert t.status is Status.PASS, t.reason
    assert t.ok is True and t.terminal and not t.cancelled
    assert t.stop_reason == "completed"
    assert "deliverable.step+" in t.text  # terminal text, not a dict
    assert t.usage["total_tokens"] == 56339
    assert t.persistence["errors"] == []
    assert t.chudcode_session_file.endswith(".jsonl")


def test_recorded_event_log_digest():
    s = summarize_events(REC / "turn_ok.events.jsonl")
    assert s["present"] and s["bad_lines"] == 0
    assert s["tool_calls"] == 3 and s["tool_calls_failed"] == 0
    assert "anthropic/claude-opus-5-5" in s["runtime_models"]
    assert s["run_finished"]["status"] == "done"
    assert summarize_events(REC / "absent.jsonl") == {"present": False}


def test_legacy_dict_result_does_not_crash_decoding():
    rec = _ok_record()
    rec["response"]["turn"]["result"] = {"calls": [], "handoff": {"summary": "x"}}
    t = decode_turn(rec)
    assert t.status is Status.PASS and '"handoff"' in t.text


def test_missing_usage_is_unknown_not_zero():
    rec = _ok_record()
    del rec["response"]["usage"]
    del rec["response"]["turn"]["usage"]
    assert decode_turn(rec).usage is None


def test_zero_call_worker_usage_is_unknown_not_zero():
    rec = _ok_record()
    zero = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "calls": 0,
            "providers": []}
    rec["response"]["usage"] = zero
    rec["response"]["turn"]["usage"] = zero
    assert decode_turn(rec).usage is None


def test_recorded_session_usage_and_price(tmp_path):
    import shutil

    from benchmarks.artifact_validity.scoring import session_usage

    assert session_usage(tmp_path) is None
    sub = tmp_path / "s.subagents"
    sub.mkdir()
    shutil.copy(REC / "turn_ok.session.jsonl", tmp_path / "main.jsonl")
    u = session_usage(tmp_path)
    assert (u["messages"], u["total_tokens"]) == (3, 56344)
    assert u["cost_usd"] == pytest.approx(0.059741, abs=1e-6)
    assert u["models"] == ["openai-codex/gpt-6-sol"]
    shutil.copy(REC / "turn_ok.session.jsonl", sub / "child.jsonl")
    assert session_usage(tmp_path)["messages"] == 6  # subagent sessions count too


@pytest.mark.parametrize("mutate, status, needle", [
    (lambda r: r["response"].update(ok=False, stop_reason="turn_budget",
                                    reason="used its whole 15 min budget"),
     Status.FAIL, "budget"),
    (lambda r: r["response"].update(ok=False, stop_reason="provider_error",
                                    reason="provider error: 529 overloaded"),
     Status.ERROR, "provider"),
    (lambda r: r["response"].update(ok=False, reason="HTTP 429 rate limit"),
     Status.ERROR, "provider"),
    (lambda r: r["response"].update(ok=False, reason="agent_admin_turn finalization "
                                    "failed: persistence: disk full"),
     Status.FAIL, "not ok"),
    (lambda r: r.update(verdict="driver_failure", verdict_detail="worker exited"),
     Status.ERROR, "executor"),
    (lambda r: (r.update(verdict="cancelled"),
                r["response"]["turn"].update(cancelled=True, stop_reason="cancelled")),
     Status.ERROR, "cancelled"),
    (lambda r: r["response"]["turn"].update(terminal=None),
     Status.ERROR, "terminal"),
    (lambda r: r.update(verdict="no_output", provider_error="401 unauthorized"),
     Status.ERROR, "no assistant output"),
    (lambda r: r.update(verdict="no_output"), Status.FAIL, "no assistant output"),
    (lambda r: r.update(verdict="run_failed", verdict_detail="tool loop aborted"),
     Status.FAIL, "run failed"),
])
def test_turn_classification(mutate, status, needle):
    rec = copy.deepcopy(_ok_record())
    mutate(rec)
    t = decode_turn(rec)
    assert t.status is status, t.reason
    assert needle in t.reason


def test_no_capture_and_driver_timeout_are_infra_errors():
    assert decode_turn(None, driver_returncode=2).status is Status.ERROR
    t = decode_turn(None, driver_timed_out=True)
    assert t.status is Status.ERROR and "timed out" in t.reason
    assert decode_turn(_ok_record(), driver_timed_out=True).status is Status.ERROR


# ---------------------------------------------------------------- fixed denominator


@check("passes")
def _passes(ev):
    return CheckResult("passes", Status.PASS, measured=1, expected=1)


@check("raises")
def _raises(ev):
    raise ZeroDivisionError("boom")


def _bare_garbage(ev):
    return {"not": "a CheckResult"}


def _task(*checks) -> Task:
    return Task(id="T", version=1, domain="cad", checks=tuple(checks),
                prompt="Save it as `deliverable.step+` in the working directory.")


def test_a_crashing_check_stays_in_the_denominator_as_error():
    r = _raises(None)
    assert r.id == "raises" and r.status is Status.ERROR and "ZeroDivisionError" in r.detail
    r = run_check(_bare_garbage, None, declared_id(_bare_garbage, 3))
    assert r.id == "_bare_garbage" and r.status is Status.ERROR


def test_check_reporting_another_id_is_reported_under_its_declared_id():
    @check("declared")
    def liar(ev):
        return CheckResult("other", Status.PASS)

    r = liar(None)
    assert r.id == "declared" and "other" in r.detail


def test_task_contract_rejects_empty_duplicate_and_unnamed_deliverable():
    with pytest.raises(ValueError):
        Task(id="E", version=1, domain="cad", prompt="deliverable.step+", checks=())
    with pytest.raises(ValueError):
        _task(_passes, _passes)
    with pytest.raises(ValueError):
        Task(id="N", version=1, domain="cad", prompt="make a part", checks=(_passes,))


def test_missing_or_extra_results_make_the_trial_error_never_pass():
    ids = ["a", "b"]
    ok = [CheckResult("a", Status.PASS), CheckResult("b", Status.PASS)]
    assert task_status(Status.PASS, True, ok, ids) is Status.PASS
    assert task_status(Status.PASS, True, ok[:1], ids) is Status.ERROR
    assert task_status(Status.PASS, True, ok + [CheckResult("c", Status.PASS)],
                       ids) is Status.ERROR
    assert task_status(Status.PASS, True, [ok[0], ok[0]], ids) is Status.ERROR


@pytest.mark.parametrize("turn, exists, statuses, want", [
    (Status.PASS, True, [Status.PASS, Status.FAIL], Status.FAIL),
    (Status.PASS, True, [Status.PASS, Status.ERROR], Status.ERROR),
    (Status.PASS, True, [Status.FAIL, Status.ERROR], Status.ERROR),
    (Status.PASS, True, [Status.PASS, Status.UNAVAILABLE], Status.UNAVAILABLE),
    (Status.FAIL, True, [Status.PASS, Status.PASS], Status.FAIL),     # budget exhausted
    (Status.ERROR, True, [Status.PASS, Status.PASS], Status.ERROR),   # provider failure
    (Status.PASS, False, [Status.PASS, Status.PASS], Status.FAIL),    # stale/missing file
])
def test_status_precedence(turn, exists, statuses, want):
    results = [CheckResult(i, s) for i, s in zip("ab", statuses)]
    assert task_status(turn, exists, results, ["a", "b"]) is want


def test_dead_grader_keeps_every_declared_check_as_error():
    task = _task(_passes, _raises)
    g = error_grade(task, "grader exit 1")
    assert [c["id"] for c in g["checks"]] == check_ids(task) == ["passes", "raises"]
    assert {c["status"] for c in g["checks"]} == {"error"}
    v = trial_verdict(task, decode_turn(_ok_record()), g, None, set())
    assert v["status"] == "error" and v["checks_total"] == 2


def test_grade_with_a_dropped_check_is_error():
    task = _task(_passes, _raises)
    g = {"evidence": {"exists": True, "sha256": "x", "mtime": 10.0},
         "checks": [CheckResult("passes", Status.PASS).to_json()]}
    v = trial_verdict(task, decode_turn(_ok_record()), g, 5.0, set())
    assert v["status"] == "error"
    assert any("check set mismatch" in r for r in v["reasons"])


def test_artifact_freshness():
    ev = {"exists": True, "sha256": "abc", "mtime": 100.0}
    assert artifact_freshness(ev, 50.0, set()) == (True, "")
    assert artifact_freshness(ev, 150.0, set())[0] is False                  # predates trial
    assert artifact_freshness(ev, 50.0, {"abc"})[1].startswith("declared artifact is an")
    assert artifact_freshness({"exists": False}, 50.0, set())[0] is False
    assert artifact_freshness(None, 50.0, set())[0] is False


# ---------------------------------------------------------------- aggregation


def _m(task, model, rep, status):
    return {"task": {"id": task}, "model": {"label": model}, "replicate": rep,
            "verdict": {"status": status, "checks_passed": 0, "checks_total": 1,
                        "artifact_fresh": True, "reasons": []},
            "turn": {"status": "pass", "reason": "completed"}, "timing": {"wall_s": 1.0},
            "grade": {"checks": [{"id": "c", "status": status, "measured": 1,
                                  "expected": 1, "tolerance": 0, "detail": ""}]}}


def test_summary_counts_errors_outside_k_over_n():
    ms = [_m("C1", "x:y", 1, "pass"), _m("C1", "x:y", 2, "fail"),
          _m("C1", "x:y", 3, "error"), _m("F1", "x:y", 1, "unavailable")]
    s = summarize(ms)
    c1 = next(c for c in s["cells"] if c["task"] == "C1")
    assert (c1["pass"], c1["scored"], c1["error"], c1["trials"]) == (1, 2, 1, 3)
    f1 = next(c for c in s["cells"] if c["task"] == "F1")
    assert f1["rate"] is None and f1["unavailable"] == 1
    assert s["macro_pass_rate"] == {"x:y": 0.5}
    text = format_report(ms)
    assert "1/2" in text and "C1" in text and "measured=1" in text
