from __future__ import annotations

from datetime import datetime, timezone

import pytest

from services.sim.credibility import (
    ClaimLanguageError,
    CredibilityError,
    CredibilityRegistry,
    CredibilityTier,
    EvidenceRecord,
    RegistryUnavailableError,
    claim_label,
    guard_claim_label,
)


SOLVER_A = "a" * 64
SOLVER_B = "b" * 64
MODEL_A = "c" * 64
DATASET_A = "d" * 64
CREATED = "2026-08-01T00:00:00Z"
EXPIRES = "2026-09-01T00:00:00Z"
NOW = datetime(2026, 8, 24, tzinfo=timezone.utc)


def _record(evidence_id: str, kind: str, **overrides) -> EvidenceRecord:
    values = {
        "evidence_id": evidence_id,
        "family": "structural-static",
        "kind": kind,
        "solver_source": "chudware-core-tet",
        "solver_revision_hash": SOLVER_A,
        "model_revision_hash": None,
        "created_at": CREATED,
        "expires_at": EXPIRES,
        "outcome": "pass",
    }
    values.update(overrides)
    return EvidenceRecord(**values)


def test_empty_registry_is_s0_and_round_trip_benchmark_is_s1(tmp_path):
    path = tmp_path / "credibility.json"
    registry = CredibilityRegistry(path)
    assert registry.derive("structural-static", SOLVER_A, now=NOW).tier is CredibilityTier.S0_UNVALIDATED

    registry.register(_record("bench-001", "benchmark"))
    reloaded = CredibilityRegistry(path)
    assessment = reloaded.derive("structural-static", SOLVER_A, now=NOW)
    assert assessment.tier is CredibilityTier.S1_BENCHMARKED
    assert assessment.evidence_ids == ("bench-001",)
    assert assessment.to_dict()["scope"] == "internal_vv_only"


def test_s2_and_s3_require_registered_dependency_chain(tmp_path):
    registry = CredibilityRegistry(tmp_path / "credibility.json")
    registry.register(_record("bench-001", "benchmark"))
    registry.register(_record(
        "cross-001", "cross_solver", depends_on=("bench-001",),
        reference_source="calculix-subprocess", reference_revision_hash=SOLVER_B))
    assert registry.derive("structural-static", SOLVER_A, now=NOW).tier is CredibilityTier.S2_CROSS_SOLVER

    registry.register(_record(
        "field-001", "field_correlation", depends_on=("cross-001",),
        dataset_id="load-cell-cantilever-2026-08", dataset_revision_hash=DATASET_A))
    assessment = registry.derive("structural-static", SOLVER_A, now=NOW)
    assert assessment.tier is CredibilityTier.S3_FIELD_CORRELATED
    assert assessment.evidence_ids == ("bench-001", "cross-001", "field-001")


def test_evidence_cannot_self_assign_a_tier_or_skip_registration(tmp_path):
    with pytest.raises(CredibilityError, match="unknown evidence fields"):
        EvidenceRecord.from_dict({
            **_record("bench-001", "benchmark").__dict__,
            "tier": "S3_field_correlated",
        })

    registry = CredibilityRegistry(tmp_path / "credibility.json")
    with pytest.raises(CredibilityError, match="unregistered dependency"):
        registry.register(_record(
            "cross-001", "cross_solver", depends_on=("fabricated-benchmark",),
            reference_source="calculix-subprocess", reference_revision_hash=SOLVER_B))
    assert registry.derive("structural-static", SOLVER_A, now=NOW).tier is CredibilityTier.S0_UNVALIDATED


def test_revision_model_expiry_and_invalidation_fail_closed(tmp_path):
    registry = CredibilityRegistry(tmp_path / "credibility.json")
    registry.register(_record("bench-model", "benchmark", model_revision_hash=MODEL_A))

    assert registry.derive("structural-static", SOLVER_A, now=NOW).tier is CredibilityTier.S0_UNVALIDATED
    assert registry.derive("structural-static", SOLVER_B, MODEL_A, now=NOW).tier is CredibilityTier.S0_UNVALIDATED
    assert registry.derive(
        "structural-static", SOLVER_A, MODEL_A,
        now=datetime(2026, 9, 1, tzinfo=timezone.utc)).tier is CredibilityTier.S0_UNVALIDATED

    registry.invalidate("bench-model", "benchmark implementation changed", at="2026-08-20T00:00:00Z")
    assert registry.derive("structural-static", SOLVER_A, MODEL_A, now=NOW).tier is CredibilityTier.S0_UNVALIDATED


def test_corrupt_registry_derives_s0_and_refuses_overwrite(tmp_path):
    path = tmp_path / "credibility.json"
    path.write_text("{not-json", encoding="utf-8")
    registry = CredibilityRegistry(path)
    assessment = registry.derive("structural-static", SOLVER_A, now=NOW)
    assert assessment.tier is CredibilityTier.S0_UNVALIDATED
    assert "registry unavailable" in assessment.reason
    with pytest.raises(RegistryUnavailableError):
        registry.register(_record("bench-001", "benchmark"))
    assert path.read_text(encoding="utf-8") == "{not-json"


def test_claim_language_guard_never_turns_internal_evidence_into_external_approval():
    assert claim_label(CredibilityTier.S3_FIELD_CORRELATED) == "internally field-correlated"
    assert guard_claim_label("internally benchmarked") == "internally benchmarked"
    for unsafe in ("certified", "S3 certified", "accredited solver", "certification-ready"):
        with pytest.raises(ClaimLanguageError):
            guard_claim_label(unsafe)
    with pytest.raises(ClaimLanguageError):
        guard_claim_label("excellent")
