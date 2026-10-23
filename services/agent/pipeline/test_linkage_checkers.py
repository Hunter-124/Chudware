"""Tests for pipeline/linkage_integrity.py.

Covers:
  - reference-integrity checker: dangling ref caught; cycle caught; clean pass.
  - datum-completeness checker: undeclared datum_ref caught; clean pass.
  - interface-consistency checker: bore-diameter mismatch caught; clean pass.
  - gate_context_from_ledger builds the context dict correctly.
  - _gate_linkage in phases.py: presence absence fails; clean linkage passes;
    checker findings (via ctx keys) extend reasons without changing gate name.
  - Ledger.add_linkage round-trips through materialize → linkages().
  - Ledger.slice_for_stage("buildpath") returns linkages (consumed by Stage 4).

Runs headless: no FastMCP, no libchudware, no network, no LLM.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from services.agent.pipeline.linkage_integrity import (
    check_datum_completeness,
    check_interface_consistency,
    check_reference_integrity,
    gate_context_from_ledger,
    DatumCompletenessResult,
    InterfaceConsistencyResult,
    ReferenceIntegrityResult,
)
from services.agent.pipeline.phases import Phase, PhaseMachine, PipelineFlags
from services.agent.ledger import Ledger

_TMP_CACHE = tempfile.mkdtemp(prefix="test_linkage_cache_")


# ---------------------------------------------------------------------------
# Shared fixture helpers
# ---------------------------------------------------------------------------

def _fresh_ledger(suffix: str = "") -> Ledger:
    run_id = f"test_linkage_{suffix}_{os.getpid()}"
    path = Path(_TMP_CACHE) / "runs" / run_id / "ledger.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return Ledger(run_id, path)


def _spec(spec_id: str, datum_refs: list[str] | None = None,
          realizes: list[str] | None = None,
          nominal_dims: list[dict] | None = None,
          tolerances: list[dict] | None = None) -> dict:
    return {
        "id": spec_id,
        "realizes": realizes or [],
        "shapes": [],
        "nominal_dims": nominal_dims or [],
        "tolerances": tolerances or [],
        "datum_refs": datum_refs or [],
        "provenance": "test fixture",
    }


def _linkage(datum_tree: list[dict] | None = None,
             reference_edges: list[dict] | None = None,
             shared_interfaces: list[dict] | None = None,
             linkage_id: str = "LINK_MAIN") -> dict:
    return {
        "id": linkage_id,
        "datum_tree": datum_tree or [],
        "reference_edges": reference_edges or [],
        "shared_interfaces": shared_interfaces or [],
    }


def _datum(datum_id: str, kind: str = "plane") -> dict:
    return {"id": datum_id, "kind": kind, "defined_by": f"face {datum_id}", "why": "primary"}


def _edge(from_id: str, to_id: str, relation: str = "sketch_on_face") -> dict:
    return {"from": from_id, "to": to_id, "relation": relation, "why": "test"}


def _machine() -> PhaseMachine:
    flags = PipelineFlags(design_pipeline=True)
    return PhaseMachine(flags=flags)


# ---------------------------------------------------------------------------
# 1. check_reference_integrity
# ---------------------------------------------------------------------------

class TestReferenceIntegrity(unittest.TestCase):

    def test_clean_pass(self):
        """G1 → G2 via datum A: all ids known, no cycle."""
        specs = [_spec("G1"), _spec("G2")]
        link = _linkage(
            datum_tree=[_datum("A")],
            reference_edges=[_edge("G2", "G1"), _edge("G2", "A", "references_axis")],
        )
        r = check_reference_integrity(link, specs)
        self.assertTrue(r.passed)
        self.assertEqual(r.dangling_refs, [])
        self.assertEqual(r.cycles, [])

    def test_dangling_ref_to_unknown_spec_caught(self):
        """An edge pointing to G99 (not in specs or datums) is dangling."""
        specs = [_spec("G1")]
        link = _linkage(
            datum_tree=[_datum("A")],
            reference_edges=[_edge("G1", "G99")],
        )
        r = check_reference_integrity(link, specs)
        self.assertFalse(r.passed)
        self.assertEqual(len(r.dangling_refs), 1)
        self.assertIn("G99", r.dangling_refs[0])
        self.assertEqual(r.cycles, [])

    def test_dangling_from_endpoint_caught(self):
        """The 'from' endpoint can also be dangling (not in specs or datums)."""
        specs = [_spec("G1")]
        link = _linkage(
            datum_tree=[_datum("A")],
            reference_edges=[_edge("GHOST", "G1")],
        )
        r = check_reference_integrity(link, specs)
        self.assertFalse(r.passed)
        self.assertTrue(any("GHOST" in d for d in r.dangling_refs))

    def test_cycle_caught(self):
        """G1 → G2 → G1 is a direct cycle: feature requires itself."""
        specs = [_spec("G1"), _spec("G2")]
        link = _linkage(
            datum_tree=[_datum("A")],
            reference_edges=[_edge("G1", "G2"), _edge("G2", "G1")],
        )
        r = check_reference_integrity(link, specs)
        self.assertFalse(r.passed)
        self.assertEqual(len(r.cycles), 1)
        self.assertIn("G1", r.cycles[0])
        self.assertIn("G2", r.cycles[0])

    def test_longer_cycle_caught(self):
        """G1 → G2 → G3 → G1 is a 3-node cycle."""
        specs = [_spec("G1"), _spec("G2"), _spec("G3")]
        link = _linkage(
            datum_tree=[],
            reference_edges=[_edge("G1", "G2"), _edge("G2", "G3"), _edge("G3", "G1")],
        )
        r = check_reference_integrity(link, specs)
        self.assertFalse(r.passed)
        self.assertGreaterEqual(len(r.cycles), 1)

    def test_datum_as_valid_endpoint(self):
        """An edge to a datum id is valid — not dangling."""
        specs = [_spec("G1")]
        link = _linkage(
            datum_tree=[_datum("A"), _datum("B")],
            reference_edges=[_edge("G1", "A", "sketch_on_plane")],
        )
        r = check_reference_integrity(link, specs)
        self.assertTrue(r.passed)

    def test_empty_linkage_passes(self):
        """No edges → trivially passes (nothing to violate)."""
        r = check_reference_integrity(_linkage(), [])
        self.assertTrue(r.passed)


# ---------------------------------------------------------------------------
# 2. check_datum_completeness
# ---------------------------------------------------------------------------

class TestDatumCompleteness(unittest.TestCase):

    def test_clean_pass(self):
        """All datum_refs in specs resolve to datum_tree entries."""
        specs = [_spec("G1", datum_refs=["A"]), _spec("G2", datum_refs=["A", "B"])]
        link = _linkage(datum_tree=[_datum("A"), _datum("B")])
        r = check_datum_completeness(link, specs)
        self.assertTrue(r.passed)
        self.assertEqual(r.unresolved_refs, [])

    def test_undeclared_datum_caught(self):
        """G1 references datum 'Z' which is not in datum_tree."""
        specs = [_spec("G1", datum_refs=["A", "Z"])]
        link = _linkage(datum_tree=[_datum("A")])
        r = check_datum_completeness(link, specs)
        self.assertFalse(r.passed)
        self.assertEqual(len(r.unresolved_refs), 1)
        self.assertIn("G1", r.unresolved_refs[0])
        self.assertIn("Z", r.unresolved_refs[0])

    def test_unused_datums_reported_informational(self):
        """Datums declared but unreferenced appear in unused_datums (not a failure)."""
        specs = [_spec("G1", datum_refs=["A"])]
        link = _linkage(datum_tree=[_datum("A"), _datum("B")])
        r = check_datum_completeness(link, specs)
        self.assertTrue(r.passed)   # not a failure
        self.assertIn("B", r.unused_datums)

    def test_no_datum_refs_passes(self):
        """Specs with empty datum_refs are fine — datums are optional on a spec."""
        specs = [_spec("G1"), _spec("G2")]
        link = _linkage(datum_tree=[_datum("A")])
        r = check_datum_completeness(link, specs)
        self.assertTrue(r.passed)
        self.assertIn("A", r.unused_datums)

    def test_empty_datum_tree_with_no_refs_passes(self):
        """No datums, no refs → trivially passes."""
        r = check_datum_completeness(_linkage(), [_spec("G1")])
        self.assertTrue(r.passed)


# ---------------------------------------------------------------------------
# 3. check_interface_consistency
# ---------------------------------------------------------------------------

class TestInterfaceConsistency(unittest.TestCase):

    def _bore_specs(self, g1_val: float, g1_upper: float, g1_lower: float,
                    g2_val: float, g2_upper: float, g2_lower: float):
        """Two geometry specs both realizing features that share a bore."""
        return [
            _spec("G1",
                  realizes=["FT1"],
                  nominal_dims=[{"name": "bore_d", "value": g1_val, "unit": "mm"}],
                  tolerances=[{"name": "bore_d", "upper": g1_upper, "lower": g1_lower}]),
            _spec("G2",
                  realizes=["FT2"],
                  nominal_dims=[{"name": "bore_d", "value": g2_val, "unit": "mm"}],
                  tolerances=[{"name": "bore_d", "upper": g2_upper, "lower": g2_lower}]),
        ]

    def test_clean_pass_matching_dims(self):
        """Both specs agree on bore_d 20.02 H7."""
        specs = self._bore_specs(20.02, 0.021, 0.0, 20.02, 0.021, 0.0)
        link = _linkage(
            shared_interfaces=[{"features": ["FT1", "FT2"],
                                 "on": "G1.bore_d",
                                 "must_agree": "diameter+tolerance"}]
        )
        r = check_interface_consistency(link, specs)
        self.assertTrue(r.passed)
        self.assertEqual(r.mismatches, [])

    def test_diameter_mismatch_caught(self):
        """G1 has bore_d=20.02 but G2 has bore_d=20.05 — mismatch must be caught."""
        specs = self._bore_specs(20.02, 0.021, 0.0, 20.05, 0.021, 0.0)
        link = _linkage(
            shared_interfaces=[{"features": ["FT1", "FT2"],
                                 "on": "G1.bore_d",
                                 "must_agree": "diameter+tolerance"}]
        )
        r = check_interface_consistency(link, specs)
        self.assertFalse(r.passed)
        self.assertEqual(len(r.mismatches), 1)
        self.assertIn("bore_d", r.mismatches[0])
        self.assertIn("G1", r.mismatches[0])
        self.assertIn("G2", r.mismatches[0])

    def test_tolerance_mismatch_caught(self):
        """Same nominal diameter but different upper tolerance → mismatch."""
        specs = self._bore_specs(20.02, 0.021, 0.0, 20.02, 0.033, 0.0)
        link = _linkage(
            shared_interfaces=[{"features": ["FT1", "FT2"],
                                 "on": "G1.bore_d",
                                 "must_agree": "diameter+tolerance"}]
        )
        r = check_interface_consistency(link, specs)
        self.assertFalse(r.passed)
        self.assertGreaterEqual(len(r.mismatches), 1)

    def test_single_spec_no_comparison(self):
        """Only one spec declares the dim — not enough to compare, not a failure."""
        specs = [
            _spec("G1",
                  realizes=["FT1"],
                  nominal_dims=[{"name": "bore_d", "value": 20.02, "unit": "mm"}],
                  tolerances=[{"name": "bore_d", "upper": 0.021, "lower": 0.0}]),
            _spec("G2", realizes=["FT2"]),  # no bore_d declared
        ]
        link = _linkage(
            shared_interfaces=[{"features": ["FT1", "FT2"],
                                 "on": "G1.bore_d",
                                 "must_agree": "diameter+tolerance"}]
        )
        r = check_interface_consistency(link, specs)
        self.assertTrue(r.passed)

    def test_no_interfaces_passes(self):
        """Empty shared_interfaces → trivially passes."""
        r = check_interface_consistency(_linkage(), [_spec("G1")])
        self.assertTrue(r.passed)


# ---------------------------------------------------------------------------
# 4. gate_context_from_ledger
# ---------------------------------------------------------------------------

class TestGateContextBuilder(unittest.TestCase):

    def test_empty_linkages_returns_minimal_ctx(self):
        """No linkages in state → ctx has linkages=[] and no checker keys."""
        ctx = gate_context_from_ledger({"linkages": [], "geometry_specs": []})
        self.assertEqual(ctx["linkages"], [])
        self.assertNotIn("ref_integrity", ctx)

    def test_checker_results_populated(self):
        """With a valid linkage, all four checker keys appear in ctx."""
        specs = [_spec("G1", datum_refs=["A"])]
        link = _linkage(
            datum_tree=[_datum("A")],
            reference_edges=[_edge("G1", "A", "sketch_on_plane")],
        )
        state = {"linkages": [link], "geometry_specs": specs}
        ctx = gate_context_from_ledger(state)
        self.assertIn("ref_integrity", ctx)
        self.assertIn("datum_completeness", ctx)
        self.assertIn("interface_consistency", ctx)
        self.assertIsInstance(ctx["ref_integrity"], ReferenceIntegrityResult)
        self.assertIsInstance(ctx["datum_completeness"], DatumCompletenessResult)
        self.assertIsInstance(ctx["interface_consistency"], InterfaceConsistencyResult)

    def test_shortcut_lists_populated(self):
        """dangling_refs / ref_cycles / datum_gaps / interface_mismatches are set."""
        specs = [_spec("G1")]
        link = _linkage(
            datum_tree=[],
            reference_edges=[_edge("G1", "GHOST")],
        )
        state = {"linkages": [link], "geometry_specs": specs}
        ctx = gate_context_from_ledger(state)
        self.assertIn("GHOST", str(ctx["dangling_refs"]))

    def test_clean_linkage_has_empty_shortcut_lists(self):
        """A correct linkage produces empty error lists."""
        specs = [_spec("G1"), _spec("G2")]
        link = _linkage(
            datum_tree=[_datum("A")],
            reference_edges=[_edge("G2", "G1")],
        )
        state = {"linkages": [link], "geometry_specs": specs}
        ctx = gate_context_from_ledger(state)
        self.assertEqual(ctx["dangling_refs"], [])
        self.assertEqual(ctx["ref_cycles"], [])
        self.assertEqual(ctx["datum_gaps"], [])
        self.assertEqual(ctx["interface_mismatches"], [])


# ---------------------------------------------------------------------------
# 5. _gate_linkage in phases.py — presence + checker-extension
# ---------------------------------------------------------------------------

class TestGateLinkage(unittest.TestCase):

    def test_absent_linkage_fails(self):
        """Gate fails when no linkage artifact exists."""
        m = _machine()
        ctx = {"linkages": [], "geometry_specs": []}
        result = m.run_gate(Phase.LINKAGE, ctx)
        self.assertFalse(result.passed)
        self.assertEqual(result.gate, "linkage_present")

    def test_present_linkage_passes(self):
        """Gate passes when a linkage artifact exists with no checker issues."""
        m = _machine()
        specs = [_spec("G1"), _spec("G2")]
        link = _linkage(datum_tree=[_datum("A")], reference_edges=[_edge("G2", "G1")])
        ctx = gate_context_from_ledger({"linkages": [link], "geometry_specs": specs})
        result = m.run_gate(Phase.LINKAGE, ctx)
        self.assertTrue(result.passed)
        self.assertEqual(result.gate, "linkage_present")

    def test_dangling_ref_extends_gate_reasons(self):
        """Dangling ref adds a reason; gate name stays 'linkage_present'."""
        m = _machine()
        specs = [_spec("G1")]
        link = _linkage(datum_tree=[_datum("A")], reference_edges=[_edge("G1", "GHOST")])
        ctx = gate_context_from_ledger({"linkages": [link], "geometry_specs": specs})
        result = m.run_gate(Phase.LINKAGE, ctx)
        self.assertFalse(result.passed)
        self.assertEqual(result.gate, "linkage_present")
        self.assertTrue(any("dangling" in r for r in result.reasons))

    def test_cycle_extends_gate_reasons(self):
        """Cycle detection adds a reason; gate name stays 'linkage_present'."""
        m = _machine()
        specs = [_spec("G1"), _spec("G2")]
        link = _linkage(
            datum_tree=[_datum("A")],
            reference_edges=[_edge("G1", "G2"), _edge("G2", "G1")],
        )
        ctx = gate_context_from_ledger({"linkages": [link], "geometry_specs": specs})
        result = m.run_gate(Phase.LINKAGE, ctx)
        self.assertFalse(result.passed)
        self.assertEqual(result.gate, "linkage_present")
        self.assertTrue(any("cycle" in r.lower() for r in result.reasons))

    def test_interface_mismatch_extends_gate_reasons(self):
        """Interface mismatch adds a reason; gate name stays 'linkage_present'."""
        m = _machine()
        specs = [
            _spec("G1", realizes=["FT1"],
                  nominal_dims=[{"name": "bore_d", "value": 20.02, "unit": "mm"}],
                  tolerances=[{"name": "bore_d", "upper": 0.021, "lower": 0.0}]),
            _spec("G2", realizes=["FT2"],
                  nominal_dims=[{"name": "bore_d", "value": 20.05, "unit": "mm"}],
                  tolerances=[{"name": "bore_d", "upper": 0.021, "lower": 0.0}]),
        ]
        link = _linkage(
            datum_tree=[_datum("A")],
            shared_interfaces=[{"features": ["FT1", "FT2"],
                                 "on": "G1.bore_d",
                                 "must_agree": "diameter+tolerance"}],
        )
        ctx = gate_context_from_ledger({"linkages": [link], "geometry_specs": specs})
        result = m.run_gate(Phase.LINKAGE, ctx)
        self.assertFalse(result.passed)
        self.assertEqual(result.gate, "linkage_present")
        self.assertTrue(any("mismatch" in r.lower() or "interface" in r.lower()
                            for r in result.reasons))

    def test_gate_not_entered_when_pipeline_flag_off(self):
        """When design_pipeline flag is OFF, next_phase skips LINKAGE."""
        flags = PipelineFlags(design_pipeline=False)
        m = PhaseMachine(flags=flags)
        # Walk next_phase from SPECIFY with no ambiguity/no org-layer and confirm
        # LINKAGE is never returned as the next phase.
        ctx: dict = {}
        visited: set = set()
        phase = Phase.SPECIFY
        for _ in range(20):
            nxt = m.next_phase(phase, ctx)
            if nxt is None:
                break
            self.assertIsNot(nxt, Phase.LINKAGE,
                             f"LINKAGE was reached at step {phase} → {nxt} "
                             "when design_pipeline flag is OFF")
            if nxt in visited:
                break
            visited.add(nxt)
            phase = nxt


# ---------------------------------------------------------------------------
# 6. Ledger integration — add_linkage round-trip + buildpath slice
# ---------------------------------------------------------------------------

class TestLedgerLinkageIntegration(unittest.TestCase):

    def test_add_linkage_round_trips(self):
        """add_linkage → reload from disk → linkages() returns the artifact."""
        led = _fresh_ledger("roundtrip")
        led.add_linkage(
            actor="linkage_planner",
            linkage_id="LINK_MAIN",
            datum_tree=[_datum("A")],
            reference_edges=[_edge("G2", "G1")],
            shared_interfaces=[],
        )
        led2 = Ledger.load(led.run_id, led.path)
        linkages = led2.linkages()
        self.assertEqual(len(linkages), 1)
        self.assertEqual(linkages[0]["id"], "LINK_MAIN")
        self.assertEqual(len(linkages[0]["datum_tree"]), 1)
        self.assertEqual(linkages[0]["datum_tree"][0]["id"], "A")
        self.assertEqual(len(linkages[0]["reference_edges"]), 1)

    def test_slice_for_buildpath_includes_linkages(self):
        """slice_for_stage('buildpath') must return linkages (Stage 4 consumes them)."""
        led = _fresh_ledger("slice")
        led.add_linkage(
            actor="linkage_planner",
            linkage_id="LINK_MAIN",
            datum_tree=[_datum("A")],
        )
        s = led.slice_for_stage("buildpath")
        self.assertIn("linkages", s)
        self.assertEqual(len(s["linkages"]), 1)

    def test_slice_for_linkage_stage_has_geometry_specs(self):
        """slice_for_stage('linkage') exposes geometry_specs to the planner."""
        led = _fresh_ledger("linkslice")
        led.add_geometry_spec(
            actor="geometry_designer",
            spec_id="G1",
            realizes=["FT1"],
            shapes=[],
            nominal_dims=[{"name": "bore_d", "value": 20.02, "unit": "mm"}],
            tolerances=[],
            datum_refs=["A"],
            provenance="fit_calc(H7/g6, nominal=20) → 20.02 mm [iso286.json]",
        )
        s = led.slice_for_stage("linkage")
        self.assertIn("geometry_specs", s)
        self.assertEqual(len(s["geometry_specs"]), 1)
        self.assertEqual(s["geometry_specs"][0]["id"], "G1")


# ---------------------------------------------------------------------------
# 7. STAGES vocabulary — linkage is registered
# ---------------------------------------------------------------------------

class TestStagesVocabulary(unittest.TestCase):

    def test_linkage_in_stages(self):
        from services.agent.roles import STAGES
        self.assertIn("linkage", STAGES)

    def test_linkage_after_feature(self):
        from services.agent.roles import STAGES
        idx_feature = STAGES.index("feature")
        idx_linkage = STAGES.index("linkage")
        self.assertGreater(idx_linkage, idx_feature,
                           "linkage must come after feature in STAGES")


if __name__ == "__main__":
    unittest.main()
