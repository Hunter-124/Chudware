"""test_reviewer_exec.py — deterministic acceptance-criteria verifier.

Proves the reviewer runs each AC's named check tool and derives an HONEST verdict:
PASS only on a mechanical match, NOT-CHECKED on prose rules / unresolved tools,
FAIL on an out-of-band value or a tool error. No fabricated passes.
"""
from __future__ import annotations

from services.agent import reviewer_exec as rx
from services.agent.reviewer_exec import PASS, FAIL, NOT_CHECKED


# ---------------------------------------------------------------------------
# pass_rule evaluator — mechanical only
# ---------------------------------------------------------------------------

def test_interval_rule_pass_and_fail():
    assert rx.evaluate_pass_rule("clearance in [0,0.042]",
                                 {"clearance": 0.02})[0] == PASS
    assert rx.evaluate_pass_rule("clearance in [0,0.042]",
                                 {"clearance": 0.5})[0] == FAIL


def test_interval_rule_missing_field_is_not_checked():
    # The tool output has no matching numeric field → cannot prove PASS.
    v, _ = rx.evaluate_pass_rule("clearance in [0,0.042]", {"mass_g": 12.0})
    assert v == NOT_CHECKED


def test_literal_pass_rule():
    assert rx.evaluate_pass_rule("PASS", {"verdict": "PASS"})[0] == PASS
    assert rx.evaluate_pass_rule("PASS", {"ok": True})[0] == PASS
    assert rx.evaluate_pass_rule("PASS", {"verdict": "FAIL"})[0] == FAIL
    # no verdict field at all → NOT-CHECKED (never a guessed pass)
    assert rx.evaluate_pass_rule("PASS", {"mass_g": 1.0})[0] == NOT_CHECKED


def test_prose_rule_is_not_checked_never_fabricated_pass():
    # "4 holes d4.5 on PCD34" is not mechanically evaluable from generic output.
    v, detail = rx.evaluate_pass_rule("4 holes d4.5 on PCD34",
                                      {"holes": 4, "pcd": 34})
    assert v == NOT_CHECKED, detail


def test_tool_error_is_fail_not_pass():
    v, _ = rx.evaluate_pass_rule("clearance in [0,1]", {"ok": False, "error": "x"})
    assert v == FAIL


# ---------------------------------------------------------------------------
# run_acceptance_checks — writes one criterion_result per AC, fail-closed
# ---------------------------------------------------------------------------

class _FakeLedger:
    def __init__(self):
        self.events = []

    def add_criterion_result(self, actor, ac_id, result, **kw):
        ev = {"kind": "criterion_result",
              "payload": {"id": ac_id, "result": result, **kw}}
        self.events.append(ev)
        return ev


def test_run_acceptance_checks_dispatches_and_records(monkeypatch):
    """Each AC's tool is resolved + called; the verdict is recorded. We inject a
    fake dispatch so no engine is needed — proving the wiring, not the geometry."""
    def _fake_resolve(tool):
        if tool == "fit_report":
            return lambda **kw: '{"clearance": 0.02}'
        if tool == "verify_battery":
            return lambda **kw: '{"verdict": "PASS"}'
        return None  # unknown tool → NOT-CHECKED

    monkeypatch.setattr(rx, "_resolve_fn", _fake_resolve)

    manifest = {"deliverables": [{"id": "DL1", "acceptance_criteria": [
        {"id": "AC1", "check": {"tool": "fit_report",
                                "pass_rule": "clearance in [0,0.042]"}},
        {"id": "AC2", "check": {"tool": "verify_battery", "pass_rule": "PASS"}},
        {"id": "AC3", "check": {"tool": "measure_part",
                                "pass_rule": "4 holes d4.5 on PCD34"}},
        {"id": "AC4", "check": {"tool": "nonexistent_tool", "pass_rule": "PASS"}},
    ]}]}
    led = _FakeLedger()
    results = rx.run_acceptance_checks(manifest, led)

    assert results == {"AC1": PASS, "AC2": PASS, "AC3": NOT_CHECKED, "AC4": NOT_CHECKED}
    # one event per AC, each carrying the raw tool output as evidence.
    assert len(led.events) == 4
    ac1 = next(e for e in led.events if e["payload"]["id"] == "AC1")
    # mega-11: evidence carries provenance (source=live|ledger_tool_call|…) so a
    # PASS is auditable; the raw tool output rides alongside it.
    assert ac1["payload"]["evidence"] == {"clearance": 0.02, "source": "live"}


def test_manual_criterion_is_not_checked():
    manifest = {"deliverables": [{"id": "DL1", "acceptance_criteria": [
        {"id": "AC1", "check": {"tool": "manual", "pass_rule": ""}},
    ]}]}
    led = _FakeLedger()
    results = rx.run_acceptance_checks(manifest, led)
    assert results["AC1"] == NOT_CHECKED


def test_tool_raising_is_not_checked_never_pass(monkeypatch):
    def _boom(**kw):
        raise RuntimeError("engine down")
    monkeypatch.setattr(rx, "_resolve_fn", lambda t: _boom)

    manifest = {"deliverables": [{"id": "DL1", "acceptance_criteria": [
        {"id": "AC1", "check": {"tool": "fit_report", "pass_rule": "PASS"}},
    ]}]}
    led = _FakeLedger()
    results = rx.run_acceptance_checks(manifest, led)
    assert results["AC1"] == NOT_CHECKED
