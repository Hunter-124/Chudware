"""test_council_policy.py — Offline tests for the council policy engine.

Discipline: mock provider only (no API key, no all-skip).  Every test runs the
real council_policy code path using the shipped council_policy.yaml.

Tests:
  1. test_local_tier_defaults_wide  — local-24k/local-48k resolve to breadth=4 (all seats).
  2. test_frontier_tier_narrow      — frontier resolves to breadth=1 on most stages.
  3. test_diverse_model_selection   — with >1 provider, seats get different providers (round-robin).
  4. test_temperature_diversity_fallback — with 1 provider, seats get different temperatures.
  5. test_policy_overridable        — overrides={"breadth": 2} respected.
  6. test_stage_spec_wired          — spec stage is enabled for all tiers.
  7. test_stage_fix_wired           — fix stage enabled for local, disabled frontier.
  8. test_council_convene_with_policy — council_convene breadth cap from policy.
  9. test_policy_resolves_unknown_stage — unknown stage falls back to defaults.
  10. test_policy_schema             — council_policy.yaml has correct schema field.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
import yaml

from services.agent.councils import (
    CouncilPolicy,
    _build_temperatures,
    _SEAT_ORDER,
    council_convene,
    resolve_policy,
)

# ---------------------------------------------------------------------------
# Helpers shared with test_councils.py
# ---------------------------------------------------------------------------

def _typed_position(position: str, risks: list[str], must_haves: list[str]) -> str:
    return json.dumps({"position": position, "risks": risks, "must_haves": must_haves})


_SYNTHESIS_REPLY = json.dumps({
    "decision": "proceed",
    "rationale": "seats aligned",
    "rejected_alternatives": [],
})


class _ScriptedProvider:
    """Mock provider that records calls and returns a scripted reply."""

    def __init__(self, name: str, reply: str = "") -> None:
        self.name = name
        self._reply = reply or _typed_position(f"{name}_reply", [], [])
        self.calls: list[tuple] = []

    def complete(self, system: str, messages: list[dict],
                 max_tokens: int = 8000, **kwargs) -> str:
        self.calls.append((system[:30], kwargs))
        # Return synthesis reply when acting as chair
        if "CHAIR" in system or "synthesize" in system.lower():
            return _SYNTHESIS_REPLY
        return self._reply


def _providers(n: int) -> list[_ScriptedProvider]:
    return [_ScriptedProvider(f"prov{i}", _typed_position(f"p{i} pos", [], []))
            for i in range(n)]


# ---------------------------------------------------------------------------
# 1. Local tier defaults wide (breadth 4)
# ---------------------------------------------------------------------------

def test_local_tier_defaults_wide():
    """local-24k and local-48k both resolve to breadth=4 for spec stage."""
    for tier in ("local-24k", "local-48k"):
        policy = resolve_policy("spec", tier)
        assert policy.breadth == 4, f"{tier} spec should be breadth=4, got {policy.breadth}"
        assert policy.enabled is True, f"{tier} spec should be enabled"
        assert len(policy.temperatures) == 4
        assert policy.seat_names == _SEAT_ORDER[:4]


# ---------------------------------------------------------------------------
# 2. Frontier tier narrow (breadth 1)
# ---------------------------------------------------------------------------

def test_frontier_tier_narrow():
    """frontier resolves to breadth=1 on spec and layout; disabled on fix."""
    spec_policy = resolve_policy("spec", "frontier")
    # spec is enabled even on frontier (important stage)
    assert spec_policy.breadth == 1, f"frontier spec breadth should be 1, got {spec_policy.breadth}"
    assert spec_policy.enabled is True

    fix_policy = resolve_policy("fix", "frontier")
    assert fix_policy.breadth == 1
    assert fix_policy.enabled is False, "frontier fix should be disabled"

    # Verify local breadth >> frontier breadth (the "wide vs narrow" invariant)
    local_spec = resolve_policy("spec", "local-24k")
    assert local_spec.breadth > spec_policy.breadth, (
        f"local breadth ({local_spec.breadth}) must exceed frontier ({spec_policy.breadth})"
    )


# ---------------------------------------------------------------------------
# 3. Diverse-model selection — >1 provider → different providers per seat
# ---------------------------------------------------------------------------

def test_diverse_model_selection():
    """With 3 providers, seats get different providers (round-robin assignment)."""
    pool = _providers(3)

    record = council_convene(
        kind="build_strategy",
        topic="diverse model test",
        inputs={},
        provider=pool[0],
        providers=pool,
        stage="spec",
        tier="local-24k",
    )

    # 4 seats for local-24k / spec.  Collect which provider was called per seat.
    # providers[0] is also chair; chair gets provider_pool[0].
    # Each provider records its calls; seat round-robin: seat0→p0, seat1→p1, seat2→p2, seat3→p0
    calls_per_provider = [len(p.calls) for p in pool]
    total_calls = sum(calls_per_provider)
    # 4 seats × 2 rounds + 1 chair synthesis = 9 calls (minimum)
    # But chairs may split across providers — we only care >1 provider got calls.
    providers_used = sum(1 for c in calls_per_provider if c > 0)
    assert providers_used >= 2, (
        f"Diverse-model: expected ≥2 providers used, got {providers_used}. "
        f"Calls: {calls_per_provider}"
    )

    # Schema check: positions should exist
    assert len(record["positions"]) == 4
    assert record["policy"]["diverse_models"] is True


# ---------------------------------------------------------------------------
# 4. Temperature-diversity fallback — 1 provider → different temperatures per seat
# ---------------------------------------------------------------------------

def test_temperature_diversity_fallback():
    """With 1 provider, seats receive different temperatures (base + i*spread)."""
    prov = _ScriptedProvider("single")

    council_convene(
        kind="build_strategy",
        topic="temp diversity test",
        inputs={},
        provider=prov,
        stage="spec",
        tier="local-24k",
    )

    # Collect temperatures from kwargs recorded in provider calls.
    temps_seen = [
        kwargs.get("temperature")
        for (_, kwargs) in prov.calls
        if "temperature" in kwargs
    ]
    # Should see 4 seats × 2 rounds = 8 seat calls with temperature; chair may or may not.
    assert len(temps_seen) >= 4, (
        f"Expected ≥4 temp values, got {len(temps_seen)}: {temps_seen}"
    )
    unique_temps = set(temps_seen)
    assert len(unique_temps) > 1, (
        f"Temperature-diversity fallback should produce >1 unique temp, got {unique_temps}"
    )

    # Temperatures must be in [0.0, 1.0]
    for t in temps_seen:
        assert 0.0 <= t <= 1.0, f"Temperature {t} out of [0,1]"


# ---------------------------------------------------------------------------
# 5. Policy overridable via overrides dict
# ---------------------------------------------------------------------------

def test_policy_overridable():
    """overrides={"breadth": 2} caps breadth to 2 regardless of tier default."""
    policy = resolve_policy("spec", "local-24k", overrides={"breadth": 2})
    assert policy.breadth == 2, f"Expected breadth=2, got {policy.breadth}"
    assert len(policy.temperatures) == 2
    assert policy.seat_names == _SEAT_ORDER[:2]

    # Override enabled=False turns it off even on local
    policy_off = resolve_policy("spec", "local-24k", overrides={"enabled": False})
    assert policy_off.enabled is False

    # Override diverse_models=False
    policy_nodiv = resolve_policy("spec", "local-24k", overrides={"diverse_models": False})
    assert policy_nodiv.diverse_models is False


# ---------------------------------------------------------------------------
# 6. spec stage wired and enabled for all tiers
# ---------------------------------------------------------------------------

def test_stage_spec_wired():
    """spec stage is enabled at all four tiers (strategy select is high-value)."""
    for tier in ("local-24k", "local-48k", "mid", "frontier"):
        policy = resolve_policy("spec", tier)
        assert policy.enabled is True, f"spec should be enabled for tier={tier}"
        assert policy.breadth >= 1


# ---------------------------------------------------------------------------
# 7. fix stage wired: local=enabled, frontier=disabled
# ---------------------------------------------------------------------------

def test_stage_fix_wired():
    """fix stage enabled for local tiers, disabled for frontier (error diagnosis)."""
    for tier in ("local-24k", "local-48k"):
        policy = resolve_policy("fix", tier)
        assert policy.enabled is True, f"fix should be enabled for tier={tier}"
        assert policy.breadth == 4

    frontier_fix = resolve_policy("fix", "frontier")
    assert frontier_fix.enabled is False, "fix should be disabled for frontier"


# ---------------------------------------------------------------------------
# 8. council_convene breadth cap from policy
# ---------------------------------------------------------------------------

def test_council_convene_with_policy():
    """council_convene respects policy breadth — mid gets 2 seats, frontier 1."""
    for tier, expected_seats in [("mid", 2), ("frontier", 1)]:
        prov = _ScriptedProvider(f"prov_{tier}")
        record = council_convene(
            kind="build_strategy",
            topic=f"breadth test {tier}",
            inputs={},
            provider=prov,
            stage="spec",
            tier=tier,
        )
        assert len(record["positions"]) == expected_seats, (
            f"tier={tier}: expected {expected_seats} seats, "
            f"got {len(record['positions'])}"
        )
        # Policy metadata embedded in record
        assert "policy" in record
        assert record["policy"]["tier"] == tier
        assert record["policy"]["breadth"] == expected_seats


# ---------------------------------------------------------------------------
# 9. Unknown stage falls back to tier defaults
# ---------------------------------------------------------------------------

def test_policy_resolves_unknown_stage():
    """Unknown stage falls back to the defaults block for that tier."""
    policy_local = resolve_policy("nonexistent_stage", "local-24k")
    assert policy_local.breadth == 4  # local default is wide
    assert policy_local.enabled is True

    policy_frontier = resolve_policy("nonexistent_stage", "frontier")
    assert policy_frontier.breadth == 1  # frontier default is narrow
    assert policy_frontier.enabled is False


# ---------------------------------------------------------------------------
# 10. council_policy.yaml schema field
# ---------------------------------------------------------------------------

def test_policy_schema():
    """council_policy.yaml has the correct schema header."""
    yaml_path = Path(__file__).resolve().parent / "council_policy.yaml"
    assert yaml_path.exists(), f"council_policy.yaml not found at {yaml_path}"
    data = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    assert data.get("schema") == "aicad.council_policy/1"


# ---------------------------------------------------------------------------
# 11. _build_temperatures unit test
# ---------------------------------------------------------------------------

def test_build_temperatures():
    """Temperature spread applies correctly and clamps to [0, 1]."""
    temps = _build_temperatures(4, 0.7, 0.1)
    assert len(temps) == 4
    # Seat 0: 0.7, seat 1: 0.8, seat 2: 0.9, seat 3: 1.0
    assert abs(temps[0] - 0.7) < 1e-9
    assert abs(temps[1] - 0.8) < 1e-9
    assert abs(temps[2] - 0.9) < 1e-9
    assert abs(temps[3] - 1.0) < 1e-9

    # Clamp test: large spread
    temps_clamped = _build_temperatures(3, 0.9, 0.2)
    for t in temps_clamped:
        assert 0.0 <= t <= 1.0


# ---------------------------------------------------------------------------
# 12. Existing tests still pass: backward-compat check
# ---------------------------------------------------------------------------

def test_backward_compat_no_policy_args():
    """council_convene with no stage/tier/providers still works (existing API)."""
    prov = _ScriptedProvider("compat")
    record = council_convene(
        kind="build_strategy",
        topic="compat test",
        inputs={},
        provider=prov,
    )
    # Default: 4 seats (all seats from _new_council_record)
    assert len(record["positions"]) == 4
    # No 'policy' key when no stage/tier given
    assert "policy" not in record


# ---------------------------------------------------------------------------
# 13. regen_diagnosis fix-stage council wiring
# ---------------------------------------------------------------------------

def test_diagnose_fix_council_local_tier():
    """diagnose() with council_tier=local-24k convenes a fix council (policy=enabled)."""
    import json as _json

    # Minimal failing document (dangling ref in selector format)
    doc = {
        "tree": {
            "features": [
                {
                    "id": "f1", "op": "extrude",
                    "refs": {"features": [{"owner_feature": "f99", "kind": "face"}]},
                },
            ]
        }
    }

    pos_reply = _typed_position("repoint_ref is the correct fix", [], [])
    synth_reply = _json.dumps({
        "decision": "apply repoint_ref",
        "rationale": "seats agree",
        "rejected_alternatives": [],
    })

    class _DiagProvider:
        name = "diag_mock"
        calls: list = []

        def complete(self, system: str, messages: list, max_tokens: int = 8000,
                     **kwargs) -> str:
            self.__class__.calls.append(system[:30])
            if "CHAIR" in system or "synthesize" in system.lower():
                return synth_reply
            # Diagnosis reply (structured JSON)
            if "fix_ops" in system or "regen" in system.lower():
                return _json.dumps({"diagnosis": "dangling ref", "fix_ops": [
                    {"op": "repoint_ref", "feature_id": "f1", "param": "refs",
                     "old_value": "f99", "new_value": None}
                ]})
            return pos_reply

    from services.agent.regen_diagnosis import diagnose

    _DiagProvider.calls = []
    result = diagnose(
        doc,
        _DiagProvider(),
        wrap_logging=False,
        council_tier="local-24k",
    )

    # Council was invoked (record is not None)
    assert result.council_record is not None, "Expected council_record for local-24k"
    cr = result.council_record
    assert cr["schema"] == "aicad.council/1"
    assert cr["kind"] == "failure_review"
    # 4 seats for local-24k fix stage
    assert len(cr["positions"]) == 4
    assert cr["policy"]["tier"] == "local-24k"
    assert cr["policy"]["breadth"] == 4


def test_diagnose_fix_council_frontier_skipped():
    """diagnose() with council_tier=frontier skips the council (policy=disabled)."""
    import json as _json

    doc = {"tree": {"features": [
        {"id": "f1", "op": "extrude",
         "refs": {"features": [{"owner_feature": "f99", "kind": "face"}]}}
    ]}}

    class _SimpleProv:
        name = "simple"
        def complete(self, system: str, messages: list, max_tokens: int = 8000, **kw) -> str:
            return _json.dumps({"diagnosis": "ok", "fix_ops": []})

    from services.agent.regen_diagnosis import diagnose

    result = diagnose(
        doc,
        _SimpleProv(),
        wrap_logging=False,
        council_tier="frontier",
    )

    # Council was NOT invoked for frontier fix stage (policy.enabled=False)
    assert result.council_record is None, (
        f"Expected council_record=None for frontier fix, got {result.council_record}"
    )


def test_diagnose_no_council_when_tier_omitted():
    """diagnose() without council_tier never invokes a council (backward compat)."""
    import json as _json

    doc = {"tree": {"features": []}}

    class _P:
        name = "p"
        def complete(self, system: str, messages: list, max_tokens: int = 8000, **kw) -> str:
            return _json.dumps({"diagnosis": "ok", "fix_ops": []})

    from services.agent.regen_diagnosis import diagnose

    result = diagnose(doc, _P(), wrap_logging=False)
    assert result.council_record is None
