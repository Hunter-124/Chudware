"""tests for functional_decomposition.check_requirement_coverage.

Also contains a regression test confirming the DECOMPOSE gate extension:
- presence-only path still returns gate name "features_present" and passes;
- coverage findings extend gate reasons without changing gate name.
"""
from __future__ import annotations

import uuid

import pytest

from services.agent.pipeline.functional_decomposition import (
    CoverageResult,
    PART_CLASS_CHECKLISTS,
    check_requirement_coverage,
    gate_context_from_ledger,
)
from services.agent.pipeline.phases import Phase, PhaseMachine


# ---------------------------------------------------------------------------
# Helper factories
# ---------------------------------------------------------------------------

def _req(req_id: str, status: str = "open") -> dict:
    return {"req_id": req_id, "text": f"requirement {req_id}", "status": status}


def _feat(feat_id: str, driving: str) -> dict:
    return {
        "id": feat_id,
        "function": f"function for {feat_id}",
        "driving_requirement": driving,
        "criticality": "important",
    }


# ---------------------------------------------------------------------------
# check_requirement_coverage — basic cases
# ---------------------------------------------------------------------------

class TestCoverageBasic:
    def test_clean_pass(self):
        """Every feature traces, every requirement covered → passed."""
        reqs = [_req("R-01"), _req("R-02")]
        feats = [
            _feat("FT-01", "R-01"),
            _feat("FT-02", "R-02"),
        ]
        result = check_requirement_coverage(feats, reqs)
        assert result.passed
        assert result.untraced_features == []
        assert result.orphan_requirements == []

    def test_empty_features_fails(self):
        """No features → orphan requirement → fails strict."""
        reqs = [_req("R-01")]
        result = check_requirement_coverage([], reqs)
        assert not result.passed
        assert "R-01" in result.orphan_requirements

    def test_empty_both_passes(self):
        """No features AND no requirements → nothing to violate → passes."""
        result = check_requirement_coverage([], [])
        assert result.passed

    def test_untraced_feature_fails(self):
        """Feature with no driving_requirement → untraced → fails."""
        feats = [_feat("FT-01", "")]
        result = check_requirement_coverage(feats, [])
        assert not result.passed
        assert "FT-01" in result.untraced_features

    def test_feature_references_nonexistent_req_fails(self):
        """Feature references a req_id not in requirements → untraced."""
        reqs = [_req("R-01")]
        feats = [_feat("FT-01", "R-99")]  # R-99 not in list
        result = check_requirement_coverage(feats, reqs)
        assert not result.passed
        assert "FT-01" in result.untraced_features
        assert "R-01" in result.orphan_requirements

    def test_assumption_slug_is_valid_trace(self):
        """assumption:<slug> counts as a valid trace."""
        reqs = []
        feats = [_feat("FT-01", "assumption:no_explicit_req")]
        result = check_requirement_coverage(feats, reqs)
        assert result.passed
        assert "FT-01" not in result.untraced_features
        assert "no_explicit_req" in result.assumption_slugs

    def test_malformed_assumption_untraced(self):
        """assumption: with no slug is malformed → untraced."""
        feats = [_feat("FT-01", "assumption:")]
        result = check_requirement_coverage(feats, [])
        assert not result.passed
        assert "FT-01" in result.untraced_features

    def test_multiple_features_one_requirement(self):
        """Two features covering the same req → requirement is covered."""
        reqs = [_req("R-01"), _req("R-02")]
        feats = [
            _feat("FT-01", "R-01"),
            _feat("FT-02", "R-01"),  # second feature covering R-01
            _feat("FT-03", "R-02"),
        ]
        result = check_requirement_coverage(feats, reqs)
        assert result.passed
        assert len(result.coverage_map["R-01"]) == 2

    def test_cancelled_requirement_excluded(self):
        """Cancelled requirements are not counted as orphans."""
        reqs = [_req("R-01"), _req("R-02", status="cancelled")]
        feats = [_feat("FT-01", "R-01")]
        result = check_requirement_coverage(feats, reqs)
        assert result.passed
        assert "R-02" not in result.orphan_requirements

    def test_coverage_map_populated(self):
        """coverage_map correctly maps req_id → [feature_ids]."""
        reqs = [_req("R-01"), _req("R-02")]
        feats = [
            _feat("FT-01", "R-01"),
            _feat("FT-02", "R-02"),
            _feat("FT-03", "R-02"),
        ]
        result = check_requirement_coverage(feats, reqs)
        assert result.coverage_map["R-01"] == ["FT-01"]
        assert set(result.coverage_map["R-02"]) == {"FT-02", "FT-03"}


class TestCoverageNonStrict:
    def test_orphan_requirement_ok_in_non_strict(self):
        """Non-strict: orphan requirements are informational, not failures."""
        reqs = [_req("R-01"), _req("R-02")]
        feats = [_feat("FT-01", "R-01")]  # R-02 uncovered
        result = check_requirement_coverage(feats, reqs, strict=False)
        assert result.passed  # passes because no untraced features
        assert "R-02" in result.orphan_requirements  # still reported

    def test_untraced_feature_fails_even_non_strict(self):
        """Non-strict: untraced feature is still a failure."""
        feats = [_feat("FT-01", "")]
        result = check_requirement_coverage(feats, [], strict=False)
        assert not result.passed


# ---------------------------------------------------------------------------
# gate_context_from_ledger
# ---------------------------------------------------------------------------

class TestGateContext:
    def test_clean_ledger_state(self):
        """Gate context from a clean ledger state has no coverage findings."""
        state = {
            "features": [
                {"id": "FT-01", "function": "mount", "driving_requirement": "R-01",
                 "criticality": "critical"},
            ],
            "requirements": [
                {"req_id": "R-01", "text": "part mounts to frame", "status": "open"},
            ],
        }
        ctx = gate_context_from_ledger(state)
        assert ctx["features"]
        assert ctx["untraced_features"] == []
        assert ctx["orphan_requirements"] == []
        assert ctx["coverage_result"].passed

    def test_empty_ledger_state(self):
        """Gate context from empty state: no features, no requirements."""
        ctx = gate_context_from_ledger({})
        assert ctx["features"] == []
        assert ctx["untraced_features"] == []
        assert ctx["orphan_requirements"] == []


# ---------------------------------------------------------------------------
# DECOMPOSE gate — regression: presence-only path unchanged
# ---------------------------------------------------------------------------

class TestDecomposeGateRegression:
    def test_presence_only_passes(self):
        """Presence-only ctx (no coverage keys) still passes with
        gate name 'features_present'."""
        m = PhaseMachine()
        g = m.run_gate(Phase.DECOMPOSE, {"features": [{"id": "FT1"}]})
        assert g.passed
        assert g.gate == "features_present"

    def test_no_features_fails(self):
        """Empty features always fails regardless of coverage keys."""
        m = PhaseMachine()
        g = m.run_gate(Phase.DECOMPOSE, {})
        assert not g.passed
        assert g.gate == "features_present"

    def test_coverage_findings_added_as_reasons(self):
        """Untraced features surface as gate reasons, gate still
        returns 'features_present' (name unchanged, signature unchanged)."""
        m = PhaseMachine()
        ctx = {
            "features": [{"id": "FT-01"}],
            "untraced_features": ["FT-01"],
            "orphan_requirements": [],
        }
        g = m.run_gate(Phase.DECOMPOSE, ctx)
        assert not g.passed
        assert g.gate == "features_present"
        assert any("FT-01" in r for r in g.reasons)

    def test_orphan_requirements_added_as_reasons(self):
        """Orphan requirements surface as gate reasons."""
        m = PhaseMachine()
        ctx = {
            "features": [{"id": "FT-01"}],
            "untraced_features": [],
            "orphan_requirements": ["R-99"],
        }
        g = m.run_gate(Phase.DECOMPOSE, ctx)
        assert not g.passed
        assert g.gate == "features_present"
        assert any("R-99" in r for r in g.reasons)

    def test_full_coverage_passes(self):
        """Features present + no coverage findings → gate passes."""
        m = PhaseMachine()
        ctx = {
            "features": [{"id": "FT-01"}, {"id": "FT-02"}],
            "untraced_features": [],
            "orphan_requirements": [],
        }
        g = m.run_gate(Phase.DECOMPOSE, ctx)
        assert g.passed
        assert g.gate == "features_present"

    def test_gate_name_unchanged_when_coverage_fails(self):
        """Gate name must stay 'features_present' even when coverage fails."""
        m = PhaseMachine()
        ctx = {
            "features": [{"id": "FT-01"}],
            "untraced_features": ["FT-01"],
            "orphan_requirements": ["R-01"],
        }
        g = m.run_gate(Phase.DECOMPOSE, ctx)
        assert g.gate == "features_present"


# ---------------------------------------------------------------------------
# Part-class checklists — structural sanity
# ---------------------------------------------------------------------------

class TestPartClassChecklists:
    REQUIRED_CLASSES = {"bracket", "housing", "shaft", "plate", "enclosure", "fitting"}

    def test_required_classes_present(self):
        """All 6 required part-class checklists exist."""
        missing = self.REQUIRED_CLASSES - set(PART_CLASS_CHECKLISTS.keys())
        assert not missing, f"missing part classes: {missing}"

    def test_each_class_has_entries(self):
        """Every part class has ≥1 checklist entry."""
        for cls, entries in PART_CLASS_CHECKLISTS.items():
            assert len(entries) >= 1, f"part class {cls!r} has no checklist entries"

    def test_each_entry_is_tuple_of_strings(self):
        """Each entry is a (verb, description) tuple of strings."""
        for cls, entries in PART_CLASS_CHECKLISTS.items():
            for entry in entries:
                assert isinstance(entry, tuple) and len(entry) == 2, (
                    f"class {cls!r}: expected (verb, description) tuple, got {entry!r}")
                assert isinstance(entry[0], str) and isinstance(entry[1], str), (
                    f"class {cls!r}: entry {entry!r} must be (str, str)")

    def test_function_verbs_from_vocabulary(self):
        """All function verbs are from the defined vocabulary."""
        vocabulary = {"MOUNT", "MATE", "SEAL", "BEAR-LOAD", "CLEAR",
                      "FASTEN", "LOCATE", "DISSIPATE"}
        for cls, entries in PART_CLASS_CHECKLISTS.items():
            for verb, _ in entries:
                assert verb in vocabulary, (
                    f"class {cls!r}: unknown function verb {verb!r} — "
                    f"must be one of {vocabulary}")

    def test_bracket_has_mount_and_locate(self):
        """Bracket must have MOUNT and LOCATE (canonical load-path functions)."""
        verbs = {v for v, _ in PART_CLASS_CHECKLISTS["bracket"]}
        assert "MOUNT" in verbs
        assert "LOCATE" in verbs

    def test_shaft_has_mate_and_bear_load(self):
        """Shaft must have MATE (journal fit) and BEAR-LOAD."""
        verbs = {v for v, _ in PART_CLASS_CHECKLISTS["shaft"]}
        assert "MATE" in verbs
        assert "BEAR-LOAD" in verbs

    def test_enclosure_has_seal(self):
        """Enclosure must have SEAL (IP/NEMA requirement)."""
        verbs = {v for v, _ in PART_CLASS_CHECKLISTS["enclosure"]}
        assert "SEAL" in verbs

    def test_fitting_has_seal_and_mate(self):
        """Fitting must have both SEAL and MATE."""
        verbs = {v for v, _ in PART_CLASS_CHECKLISTS["fitting"]}
        assert "SEAL" in verbs
        assert "MATE" in verbs
