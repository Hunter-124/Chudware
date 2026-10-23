"""Tests for the Python building-code compliance helpers.

Hand-calc validation
--------------------
**Deflection check (L/360):**
  Span = 3000 mm.  L/360 = 3000 / 360 = 8.333... mm.
  - 8 mm deflection  → 8 < 8.333 → passed=True
  - 10 mm deflection → 10 ≥ 8.333 → passed=False

**Post compression (NDS ASD, DF-L No.2, Fc = 9.307 MPa, ANSI/AWC PS 20 dressed dims):**
  6x6 dressed = 140 × 140 mm → A = 19 600 mm²
  Capacity = 9.307 N/mm² × 19 600 mm² = 182 417.2 N = 182.4172 kN
  - 100 kN → 100 < 182.42 → passed=True
  - 300 kN → 300 ≥ 182.42 → passed=False
  4x4 dressed = 89 × 89 mm → A = 7 921 mm²
  Capacity = 9.307 × 7 921 = 73 720.7 N = 73.7207 kN
  Unknown sizes fall back to the smallest listed post (4x4) — conservative.

Run::

    python -m pytest services/structural/test_code_check.py -q
"""
from __future__ import annotations

import pytest

from services.structural.checks import DISCLAIMER
from services.structural.code_check import (
    RuleResult,
    MockRAGProvider,
    agentic_rag,
    check_deflection,
    check_fastener_schedule,
    check_post_span,
    run_checks,
)
from services.structural.jurisdiction_meta import (
    get_jurisdiction,
    jurisdiction_label,
    list_jurisdictions,
)


# ============================================================================ RuleResult dataclass

class TestRuleResult:
    def test_disclaimer_is_canonical(self):
        """RuleResult.disclaimer defaults to the byte-exact DISCLAIMER from checks.py."""
        r = RuleResult(
            rule_id="test",
            passed=True,
            value=1.0,
            limit=2.0,
            unit="mm",
            citation="test",
            detail="test",
        )
        assert r.disclaimer == DISCLAIMER

    def test_to_dict_contains_all_keys(self):
        r = check_deflection(3000.0, 8.0)
        d = r.to_dict()
        for key in ("rule_id", "passed", "value", "limit", "unit", "citation",
                    "detail", "disclaimer"):
            assert key in d, f"missing key {key!r}"

    def test_disclaimer_in_dict(self):
        d = check_deflection(3000.0, 8.0).to_dict()
        assert d["disclaimer"] == DISCLAIMER


# ============================================================================ check_deflection

class TestCheckDeflection:
    def test_pass_8mm_span_3000mm(self):
        """DoD case: 8 mm deflection on 3 m span → passed=True (8 < 8.333)."""
        result = check_deflection(span_mm=3000.0, deflection_mm=8.0)
        assert result.passed is True
        assert result.rule_id == "deflection_L360"

    def test_fail_10mm_span_3000mm(self):
        """DoD case: 10 mm deflection on 3 m span → passed=False (10 ≥ 8.333)."""
        result = check_deflection(span_mm=3000.0, deflection_mm=10.0)
        assert result.passed is False
        assert result.rule_id == "deflection_L360"

    def test_limit_value_correct(self):
        """Limit for 3 m span should be 3000/360 = 8.333... mm."""
        result = check_deflection(3000.0, 5.0)
        assert result.limit == pytest.approx(3000.0 / 360.0, rel=1e-6)

    def test_value_is_absolute(self):
        """Negative deflection input should be treated as its absolute value."""
        r_pos = check_deflection(3000.0, 8.0)
        r_neg = check_deflection(3000.0, -8.0)
        assert r_pos.value == r_neg.value
        assert r_pos.passed == r_neg.passed

    def test_boundary_exactly_at_limit_fails(self):
        """Deflection exactly equal to L/360 should fail (strict < not <=)."""
        span = 3600.0
        limit = span / 360.0  # = 10.0 mm exactly
        result = check_deflection(span, limit)
        assert result.passed is False

    def test_just_below_limit_passes(self):
        span = 3600.0
        limit = span / 360.0  # = 10.0 mm
        result = check_deflection(span, limit - 0.001)
        assert result.passed is True

    def test_citation_contains_ibc_and_aisc(self):
        r = check_deflection(3000.0, 5.0)
        assert "IBC" in r.citation
        assert "1604" in r.citation

    def test_zero_span_returns_fail(self):
        """Zero or negative span is a degenerate case; should not crash and returns fail."""
        r = check_deflection(0.0, 5.0)
        assert r.passed is False

    def test_unit_is_mm(self):
        r = check_deflection(3000.0, 5.0)
        assert r.unit == "mm"

    def test_various_spans(self):
        """Sanity check for multiple spans."""
        for span in (1000.0, 2000.0, 4000.0, 6000.0):
            limit = span / 360.0
            assert check_deflection(span, limit - 0.1).passed is True
            assert check_deflection(span, limit + 0.1).passed is False


# ============================================================================ check_post_span

class TestCheckPostSpan:
    # Independent hand-calc from ANSI/AWC PS 20 dressed dims (NOT recomputed from
    # module constants):
    #   6x6 dressed 140×140 mm → A = 19 600 mm²
    #   capacity = 9.307 N/mm² × 19 600 mm² = 182 417.2 N = 182.4172 kN
    _6X6_CAPACITY_KN = 182.4172
    #   4x4 dressed 89×89 mm → A = 7 921 mm²
    #   capacity = 9.307 N/mm² × 7 921 mm² = 73 720.7 N = 73.7207 kN
    _4X4_CAPACITY_KN = 73.7207

    def test_6x6_100kn_passes(self):
        """100 kN < 6x6 capacity (182.42 kN) → passed=True."""
        r = check_post_span("6x6", 100.0)
        assert r.passed is True

    def test_6x6_300kn_fails(self):
        """300 kN > 6x6 capacity (182.42 kN) → passed=False."""
        r = check_post_span("6x6", 300.0)
        assert r.passed is False

    def test_6x6_200kn_fails(self):
        """200 kN ≥ 6x6 capacity 182.42 kN → FAIL.  The old hardcoded table overstated
        6x6 area (23 226 vs actual 19 600 mm²) and would wrongly PASS this load."""
        r = check_post_span("6x6", 200.0)
        assert r.passed is False

    def test_capacity_absolute(self):
        """6x6 capacity equals independent hand-calc 182.4172 kN (140×140 dressed)."""
        r = check_post_span("6x6", 0.0)  # zero load → passes; limit gives capacity
        assert r.limit == pytest.approx(self._6X6_CAPACITY_KN, abs=0.01)

    def test_4x4_capacity_absolute(self):
        """4x4 capacity equals independent hand-calc 73.7207 kN (89×89 dressed)."""
        r = check_post_span("4x4", 0.0)
        assert r.limit == pytest.approx(self._4X4_CAPACITY_KN, abs=0.01)

    def test_4x4_smaller_capacity(self):
        """4x4 post (A=7921 mm²) should have lower capacity than 6x6 (A=19600 mm²)."""
        r_4x4 = check_post_span("4x4", 0.0)
        r_6x6 = check_post_span("6x6", 0.0)
        assert r_4x4.limit < r_6x6.limit

    def test_case_insensitive(self):
        r_lower = check_post_span("6x6", 100.0)
        r_upper = check_post_span("6X6", 100.0)
        assert r_lower.passed == r_upper.passed
        assert r_lower.limit == r_upper.limit

    def test_unknown_size_falls_back_to_smallest(self):
        """Unknown post size should not raise; falls back to the SMALLEST listed post
        (4x4 — conservative) with an explicit note, never a larger 'default'."""
        r = check_post_span("99x99", 50.0)
        assert r.passed is True  # 50 kN < 4x4 capacity 73.72 kN
        assert r.limit == pytest.approx(self._4X4_CAPACITY_KN, abs=0.01)
        assert "unknown post size" in r.detail.lower()
        assert "conservative" in r.detail.lower()

    def test_unknown_size_is_actually_conservative(self):
        """100 kN on an unknown size must FAIL (4x4 fallback capacity 73.72 kN) —
        the old 6x6 'conservative' fallback wrongly passed this."""
        r = check_post_span("99x99", 100.0)
        assert r.passed is False

    def test_citation_contains_nds(self):
        r = check_post_span("6x6", 50.0)
        assert "NDS" in r.citation

    def test_rule_id(self):
        r = check_post_span("6x6", 50.0)
        assert r.rule_id == "post_compression_NDS_ASD"

    def test_unit_is_kn(self):
        r = check_post_span("6x6", 50.0)
        assert r.unit == "kN"


# ============================================================================ check_fastener_schedule

class TestCheckFastenerSchedule:
    def test_known_connection_passes(self):
        r = check_fastener_schedule("post_base")
        assert r.passed is True
        assert r.rule_id == "fastener_schedule"

    def test_ledger_to_band_joist_passes(self):
        r = check_fastener_schedule("ledger_to_band_joist")
        assert r.passed is True
        assert "lag" in r.detail.lower() or "bolt" in r.detail.lower()

    def test_unknown_connection_fails(self):
        r = check_fastener_schedule("mystery_joint_type")
        assert r.passed is False
        assert "not in the encoded fastener schedule" in r.detail.lower()

    def test_normalises_spaces_to_underscores(self):
        r1 = check_fastener_schedule("post_base")
        r2 = check_fastener_schedule("post base")
        assert r1.passed == r2.passed

    def test_normalises_hyphens(self):
        r1 = check_fastener_schedule("shear_wall_anchor")
        r2 = check_fastener_schedule("shear-wall-anchor")
        assert r1.passed == r2.passed

    def test_citation_contains_ibc(self):
        r = check_fastener_schedule("rim_joist_to_sill")
        assert "IBC" in r.citation

    def test_all_known_types_pass(self):
        known = [
            "ledger_to_band_joist",
            "post_base",
            "rim_joist_to_sill",
            "rafter_to_top_plate",
            "shear_wall_anchor",
        ]
        for ct in known:
            r = check_fastener_schedule(ct)
            assert r.passed is True, f"expected {ct!r} to pass"

    def test_disclaimer_present(self):
        r = check_fastener_schedule("post_base")
        assert r.disclaimer == DISCLAIMER


# ============================================================================ agentic_rag / MockRAGProvider

class TestAgenticRAG:
    def test_returns_list(self):
        """agentic_rag returns a list."""
        result = agentic_rag("deflection limit", "CA", provider=MockRAGProvider())
        assert isinstance(result, list)

    def test_mock_returns_citations(self):
        """Mock provider returns at least one citation."""
        result = agentic_rag("deflection limit", "CA", provider=MockRAGProvider())
        assert len(result) >= 1

    def test_citation_has_required_keys(self):
        """Each citation has text, source_url, section."""
        result = agentic_rag("deflection limit", "CA", provider=MockRAGProvider())
        for c in result:
            assert "text" in c
            assert "source_url" in c
            assert "section" in c

    def test_citations_tagged_agent_retrieved(self):
        """All citations are tagged agent_retrieved=True."""
        result = agentic_rag("post compression", "TX", provider=MockRAGProvider())
        for c in result:
            assert c.get("agent_retrieved") is True

    def test_citations_have_caveat(self):
        """All citations carry a caveat about verification."""
        result = agentic_rag("fastener schedule", "WA", provider=MockRAGProvider())
        for c in result:
            assert "caveat" in c

    def test_custom_mock_citations(self):
        """Custom mock citations are returned."""
        custom = [{"text": "custom", "source_url": "test://url", "section": "§1.2"}]
        result = agentic_rag("anything", "CA", provider=MockRAGProvider(citations=custom))
        assert result[0]["text"] == "custom"

    def test_default_provider_is_user_corpus(self):
        """Without a provider, agentic_rag returns a list (graceful no-op for missing corpus)."""
        # CHUDWARE_CODE_CORPUS_PATH points to a non-existent dir → advisory citation returned
        import os
        os.environ.setdefault("CHUDWARE_CODE_CORPUS_PATH", "/tmp/does_not_exist_code_corpus_test")
        result = agentic_rag("deflection", "CA")
        assert isinstance(result, list)
        assert len(result) >= 1
        # Should be the advisory note, not an exception
        assert result[0].get("agent_retrieved") is True


# ============================================================================ run_checks

class TestRunChecks:
    def test_returns_required_keys(self):
        """run_checks always returns rule_results, citations, disclaimer."""
        out = run_checks(
            span_mm=3000.0,
            deflection_mm=8.0,
            jurisdiction="CA",
            rag_provider=MockRAGProvider(),
        )
        assert "rule_results" in out
        assert "citations" in out
        assert "disclaimer" in out

    def test_disclaimer_always_present(self):
        out = run_checks(rag_provider=MockRAGProvider())
        assert out["disclaimer"] == DISCLAIMER

    def test_deflection_check_included(self):
        out = run_checks(span_mm=3000.0, deflection_mm=8.0, rag_provider=MockRAGProvider())
        rules = {r["rule_id"] for r in out["rule_results"]}
        assert "deflection_L360" in rules

    def test_post_check_included(self):
        out = run_checks(post_size="6x6", post_load_kn=100.0, rag_provider=MockRAGProvider())
        rules = {r["rule_id"] for r in out["rule_results"]}
        assert "post_compression_NDS_ASD" in rules

    def test_fastener_check_included(self):
        out = run_checks(connection_type="post_base", rag_provider=MockRAGProvider())
        rules = {r["rule_id"] for r in out["rule_results"]}
        assert "fastener_schedule" in rules

    def test_no_rag_query_yields_empty_citations(self):
        out = run_checks(span_mm=3000.0, deflection_mm=5.0, rag_provider=MockRAGProvider())
        assert out["citations"] == []

    def test_rag_query_yields_citations(self):
        out = run_checks(
            span_mm=3000.0,
            deflection_mm=5.0,
            rag_query="L/360 deflection limit",
            jurisdiction="CA",
            rag_provider=MockRAGProvider(),
        )
        assert len(out["citations"]) >= 1

    def test_all_checks_together(self):
        """All three rule types in a single call."""
        out = run_checks(
            span_mm=3000.0, deflection_mm=8.0,
            post_size="6x6", post_load_kn=100.0,
            connection_type="post_base",
            jurisdiction="TX",
            rag_query="building code compliance",
            rag_provider=MockRAGProvider(),
        )
        rules = {r["rule_id"] for r in out["rule_results"]}
        assert rules == {"deflection_L360", "post_compression_NDS_ASD", "fastener_schedule"}
        assert len(out["citations"]) >= 1
        assert out["disclaimer"] == DISCLAIMER

    def test_no_inputs_returns_empty_rule_results(self):
        """With no inputs, rule_results is empty but disclaimer is still present."""
        out = run_checks(rag_provider=MockRAGProvider())
        assert out["rule_results"] == []
        assert out["disclaimer"] == DISCLAIMER


# ============================================================================ jurisdiction_meta

class TestJurisdictionMeta:
    def test_known_states_present(self):
        """The six required states (CA, TX, NY, FL, WA, OR) are in the bundled data."""
        for state in ("CA", "TX", "NY", "FL", "WA", "OR"):
            meta = get_jurisdiction(state)
            assert "ibc_edition" in meta
            assert "asce7_edition" in meta

    def test_at_least_six_states(self):
        """At least 6 states in the bundled data (DoD requirement)."""
        assert len(list_jurisdictions()) >= 6

    def test_unknown_state_raises_key_error(self):
        with pytest.raises(KeyError, match="ZZ"):
            get_jurisdiction("ZZ")

    def test_case_normalised(self):
        meta_upper = get_jurisdiction("CA")
        meta_lower = get_jurisdiction("ca")
        assert meta_upper == meta_lower

    def test_ibc_edition_is_string(self):
        for state in list_jurisdictions():
            meta = get_jurisdiction(state)
            assert isinstance(meta["ibc_edition"], str)

    def test_jurisdiction_label_format(self):
        label = jurisdiction_label("CA")
        assert "IBC" in label
        assert "ASCE7" in label

    def test_jurisdiction_label_unknown_state(self):
        """Unknown state returns a graceful label, not an exception."""
        label = jurisdiction_label("ZZ")
        assert "unknown" in label.lower()

    def test_defensive_copy(self):
        """get_jurisdiction returns a copy — mutating it doesn't affect subsequent calls."""
        meta1 = get_jurisdiction("TX")
        meta1["ibc_edition"] = "MUTATED"
        meta2 = get_jurisdiction("TX")
        assert meta2["ibc_edition"] != "MUTATED"
