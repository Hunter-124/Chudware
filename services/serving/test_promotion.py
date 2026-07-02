"""Tests for services/serving/promotion.py (T-H9).

Focus: the Erik-ratified gate — automated evidence is necessary but NOT sufficient; a human
sign-off is REQUIRED; there is no auto-promote path. Plus rollback safety.

Run: python -m pytest services/serving/test_promotion.py -q
"""
from __future__ import annotations

import pytest

from services.serving.promotion import (
    PromotionEvidence,
    PromotionRefused,
    SignOff,
    evaluate_promotion,
    promote,
    rollback,
    set_canary,
    write_evidence_report,
)
from services.serving.routing import Fallback, RouteEntry, RoutingTable


# --------------------------------------------------------------------------- fixtures

class _Row:
    """Minimal ShadowRow stand-in (duck-typed to shadow_report.ShadowRow)."""

    def __init__(self, ip, inc_score, inc_acc, ch_score, ch_acc):
        self.insertion_point = ip
        self.artifact_id = "abcd1234"
        self.prompt_hash = "deadbeef"
        self.mode = "replay"
        self.incumbent_provider = "anthropic"
        self.incumbent_model = "claude"
        self.incumbent_latency_ms = 1000.0
        self.incumbent_accepted = inc_acc
        self.incumbent_scores = {"verifier": inc_score}
        self.challenger_provider = "vllm"
        self.challenger_model = "qwen3-4b"
        self.challenger_latency_ms = 900.0
        self.challenger_accepted = ch_acc
        self.challenger_scores = {"verifier": ch_score}


def _rows(ip="IP-13", n=120, *, challenger_wins=True):
    rows = []
    for i in range(n):
        if challenger_wins:
            rows.append(_Row(ip, 0.6, i % 2 == 0, 0.8, True))       # ch better on both
        else:
            rows.append(_Row(ip, 0.8, True, 0.5, i % 3 == 0))       # ch worse on both
    return rows


def _table(adapter="IP-13/v3"):
    return RoutingTable([RouteEntry(
        insertion_point="IP-13", provider="vllm", model="qwen3-4b",
        adapter_version=adapter, canary_pct=10.0,
        fallback=Fallback(mode="frontier", provider="anthropic", model="claude"),
    )])


# --------------------------------------------------------------------------- evidence

def test_evidence_eligible_when_challenger_wins_both():
    ev = evaluate_promotion(_rows(challenger_wins=True), "IP-13", min_calls=100)
    assert ev.n == 120
    assert ev.verifier_pass and ev.accept_pass
    assert ev.eligible
    assert ev.reason == "eligible"


def test_evidence_not_eligible_when_challenger_loses():
    ev = evaluate_promotion(_rows(challenger_wins=False), "IP-13", min_calls=100)
    assert not ev.eligible
    assert "verifier" in ev.reason or "accept-rate" in ev.reason


def test_evidence_not_eligible_when_too_few_calls():
    ev = evaluate_promotion(_rows(n=10, challenger_wins=True), "IP-13", min_calls=100)
    assert not ev.enough_calls
    assert not ev.eligible


def test_evidence_ignores_other_insertion_points():
    rows = _rows("IP-13", 120) + _rows("IP-1", 50)
    ev = evaluate_promotion(rows, "IP-13", min_calls=100)
    assert ev.n == 120  # only IP-13 counted


# --------------------------------------------------------------------------- the gate

def _good_evidence():
    return evaluate_promotion(_rows(challenger_wins=True), "IP-13", min_calls=100)


def test_promote_refused_without_signoff():
    """No human sign-off => never promote, even with winning metrics."""
    t = _table()
    with pytest.raises(PromotionRefused, match="sign-off"):
        promote(t, "IP-13", "IP-13/v4", evidence=_good_evidence(), signoff=None)
    # routing unchanged
    assert t.get("IP-13").adapter_version == "IP-13/v3"


def test_metrics_alone_are_not_sufficient(tmp_path):
    """Eligible evidence + valid report but the caller passes signoff=None -> refused.

    This is the core ratified invariant: automated evidence beating both metrics does NOT
    promote anything on its own.
    """
    report = tmp_path / "evidence.txt"
    ev = write_evidence_report(_rows(challenger_wins=True), "IP-13", report)
    assert ev.eligible and report.exists()
    t = _table()
    with pytest.raises(PromotionRefused):
        promote(t, "IP-13", "IP-13/v4", evidence=ev, signoff=None)


def test_promote_refused_when_report_missing(tmp_path):
    t = _table()
    so = SignOff(approver="erik", report_ref=str(tmp_path / "does-not-exist.txt"))
    with pytest.raises(PromotionRefused, match="report_ref not found"):
        promote(t, "IP-13", "IP-13/v4", evidence=_good_evidence(), signoff=so)


def test_promote_refused_when_evidence_not_eligible(tmp_path):
    report = tmp_path / "evidence.txt"
    write_evidence_report(_rows(challenger_wins=False), "IP-13", report)
    ev = evaluate_promotion(_rows(challenger_wins=False), "IP-13", min_calls=100)
    t = _table()
    so = SignOff(approver="erik", report_ref=str(report))
    with pytest.raises(PromotionRefused, match="not eligible"):
        promote(t, "IP-13", "IP-13/v4", evidence=ev, signoff=so)


def test_promote_refused_on_invalid_signoff(tmp_path):
    report = tmp_path / "evidence.txt"
    write_evidence_report(_rows(challenger_wins=True), "IP-13", report)
    t = _table()
    with pytest.raises(PromotionRefused, match="invalid"):
        promote(t, "IP-13", "IP-13/v4", evidence=_good_evidence(),
                signoff=SignOff(approver="", report_ref=str(report)))


def test_promote_succeeds_with_evidence_and_signoff(tmp_path):
    report = tmp_path / "evidence.txt"
    ev = write_evidence_report(_rows(challenger_wins=True), "IP-13", report)
    t = _table()
    so = SignOff(approver="erik", report_ref=str(report), note="LGTM 2026-07-02")
    entry = promote(t, "IP-13", "IP-13/v4", evidence=ev, signoff=so)
    assert entry.adapter_version == "IP-13/v4"
    assert entry.canary_pct == 100.0            # promoted to full production
    assert "IP-13/v3" in entry.history          # outgoing version retained for rollback


def test_promote_evidence_ip_mismatch_refused(tmp_path):
    report = tmp_path / "evidence.txt"
    report.write_text("x", encoding="utf-8")
    ev = evaluate_promotion(_rows("IP-1", 120), "IP-1", min_calls=100)
    t = _table()
    so = SignOff(approver="erik", report_ref=str(report))
    with pytest.raises(PromotionRefused, match="not IP-13|is for"):
        promote(t, "IP-13", "IP-13/v4", evidence=ev, signoff=so)


def test_no_auto_promote_symbol_exists():
    """Guard: there is no auto/unattended promote helper in the module surface."""
    import services.serving.promotion as mod
    names = [n for n in dir(mod) if "promote" in n.lower()]
    # only the human-gated `promote` is exported; no auto_promote/promote_if_eligible etc.
    assert names == ["promote"], f"unexpected promote-like symbols: {names}"


# --------------------------------------------------------------------------- rollback (always safe)

def test_rollback_to_previous_history_version(tmp_path):
    report = tmp_path / "e.txt"
    ev = write_evidence_report(_rows(challenger_wins=True), "IP-13", report)
    t = _table()
    promote(t, "IP-13", "IP-13/v4", evidence=ev,
            signoff=SignOff(approver="erik", report_ref=str(report)))
    assert t.get("IP-13").adapter_version == "IP-13/v4"
    # rollback flips straight back to the previous version, no confirmation, no evidence
    entry = rollback(t, "IP-13")
    assert entry.adapter_version == "IP-13/v3"


def test_rollback_explicit_version():
    t = _table("IP-13/v9")
    entry = rollback(t, "IP-13", to_version="IP-13/v2")
    assert entry.adapter_version == "IP-13/v2"


def test_rollback_with_no_history_drops_to_base():
    t = _table("IP-13/v1")  # no history
    entry = rollback(t, "IP-13")
    assert entry.adapter_version is None
    # and resolve now serves the fallback (kill-switch)
    d = t.resolve("IP-13", user_id="anyone")
    assert d.is_fallback


def test_set_canary_dials_traffic():
    t = _table()
    set_canary(t, "IP-13", 25.0, opt_in=["early1", "early2"])
    e = t.get("IP-13")
    assert e.canary_pct == 25.0
    assert e.canary_opt_in == ("early1", "early2")


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
