"""Tests for telemetry: consent_tier resolution and intake audit fields."""
from __future__ import annotations

import pytest
from ..intake.intake import (
    CriticalDimension,
    CriticalDimensionLedger,
    apply_confirmation,
    new_critical_dimension,
    build_ledger,
)


# ---------------------------------------------------------------------------
# Intake audit field tests
# ---------------------------------------------------------------------------

def _make_ledger() -> CriticalDimensionLedger:
    cd = new_critical_dimension("cd1", "shaft.dia", "shaft", "shaft_dia",
                                why_it_matters="fits bearing")
    return CriticalDimensionLedger(rows=[cd])


def test_apply_confirmation_no_audit():
    """Original call without audit kwargs still works (backward compat)."""
    ledger = _make_ledger()
    row = apply_confirmation(ledger, "shaft.dia", 25.0)
    assert row.nominal_mm == pytest.approx(25.0)
    assert row.audit == {}


def test_apply_confirmation_with_audit():
    """New audit fields are stored when provided."""
    ledger = _make_ledger()
    row = apply_confirmation(
        ledger, "shaft.dia", 30.0,
        who="user:erik",
        ts="2026-07-02T12:00:00Z",
        superseded_value=25.0,
        reason="measured with caliper",
    )
    assert row.nominal_mm == pytest.approx(30.0)
    assert row.audit["who"] == "user:erik"
    assert row.audit["ts"] == "2026-07-02T12:00:00Z"
    assert row.audit["superseded_value"] == pytest.approx(25.0)
    assert row.audit["reason"] == "measured with caliper"


def test_apply_confirmation_partial_audit():
    """Partial audit kwargs — only provided fields appear in audit dict."""
    ledger = _make_ledger()
    row = apply_confirmation(ledger, "shaft.dia", 20.0, who="agent:ip3")
    assert row.audit == {"who": "agent:ip3"}
    assert "ts" not in row.audit
    assert "reason" not in row.audit


def test_critical_dimension_to_dict_from_dict_audit_roundtrip():
    """to_dict/from_dict round-trips audit field; absent field defaults to {}."""
    ledger = _make_ledger()
    row = apply_confirmation(ledger, "shaft.dia", 10.0, who="x", ts="t", reason="r")
    d = row.to_dict()
    assert d["audit"] == {"who": "x", "ts": "t", "reason": "r"}

    restored = CriticalDimension.from_dict(d)
    assert restored.audit == {"who": "x", "ts": "t", "reason": "r"}


def test_critical_dimension_from_dict_missing_audit_defaults_empty():
    """Old docs without 'audit' key deserialize cleanly to audit={}."""
    ledger = _make_ledger()
    row = apply_confirmation(ledger, "shaft.dia", 5.0)
    d = row.to_dict()
    del d["audit"]  # simulate old doc
    restored = CriticalDimension.from_dict(d)
    assert restored.audit == {}


def test_audit_does_not_break_gate_ok():
    """Audit fields on confirmed rows do not affect gate logic."""
    ledger = _make_ledger()
    from ..intake.intake import gate_ok, unresolved_critical
    assert not gate_ok(ledger)  # starts unknown
    apply_confirmation(ledger, "shaft.dia", 12.0, who="u", ts="t")
    assert gate_ok(ledger)


# ---------------------------------------------------------------------------
# Consent tier tests (Python)
# ---------------------------------------------------------------------------

def test_consent_tier_default_off():
    """Missing telemetry_consent key → off."""
    from services.agent import config as agent_config
    assert agent_config.consent_tier({}) == "off"


def test_consent_tier_explicit_off():
    assert __import__("services.agent.config", fromlist=["consent_tier"]).consent_tier(
        {"telemetry_consent": "off"}
    ) == "off"


def test_consent_tier_meta():
    from services.agent import config as agent_config
    assert agent_config.consent_tier({"telemetry_consent": "meta"}) == "meta"


def test_consent_tier_full():
    from services.agent import config as agent_config
    assert agent_config.consent_tier({"telemetry_consent": "full"}) == "full"


def test_consent_tier_unknown_value_defaults_off():
    from services.agent import config as agent_config
    assert agent_config.consent_tier({"telemetry_consent": "FULL"}) == "off"
    assert agent_config.consent_tier({"telemetry_consent": "all"}) == "off"


def test_consent_tier_offline_mode_forces_off():
    """offline_mode=True hard-overrides any consent value."""
    from services.agent import config as agent_config
    assert agent_config.consent_tier(
        {"offline_mode": True, "telemetry_consent": "full"}
    ) == "off"
    assert agent_config.consent_tier(
        {"offline_mode": True, "telemetry_consent": "meta"}
    ) == "off"


def test_consent_tier_offline_false_allows_full():
    from services.agent import config as agent_config
    assert agent_config.consent_tier(
        {"offline_mode": False, "telemetry_consent": "full"}
    ) == "full"
