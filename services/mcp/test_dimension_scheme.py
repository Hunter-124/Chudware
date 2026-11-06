"""Tests for suggest_dimension_scheme.

Most tests exercise pure planning logic with explicit fixture geometry. The entry-point
coverage also checks the real authored document tree through an isolated engine daemon.

Run:
  python -m pytest services/mcp/test_dimension_scheme.py -v
"""
from __future__ import annotations

import json
import unittest
import unittest.mock

from services.research.fits import fit_calc, recommend_fit
from services.mcp.dimension_scheme_tools import (
    _plan_from_entities,
    _make_fixture_sketch as _dim_fixture,
    _make_bore_sketch,
    _it_grade_for_process,
    _bilateral_tolerance,
    _find_bolt_patterns,
    _drawing_plan_from_features,
    _process_verdict,
    register_dimension_scheme_tools,
)


def get_profile(name: str) -> dict:
    """Resolve the DfM profile through the product's bridge."""
    from services.mcp.dimension_scheme_tools import _get_profile
    return _get_profile(name)


class TestBoltPatternRule(unittest.TestCase):
    """The core rule: bolt patterns use datum-offset + centre-to-centre only."""

    def setUp(self):
        # 4-hole plate: 40mm spacing, 5mm holes
        self.entities = _dim_fixture(num_holes=4, bolt_spacing_mm=40.0, hole_dia_mm=5.0)
        patch = unittest.mock.patch("services.mcp.dimension_scheme_tools._profile_params",
                                    return_value={"hole_comp_mm": 0.0})
        patch.start()
        self.addCleanup(patch.stop)

    def test_fixture_has_four_circles(self):
        self.assertEqual(len(self.entities), 4)
        for e in self.entities:
            self.assertEqual(e["type"], "circle")

    def test_bolt_pattern_detected(self):
        groups = _find_bolt_patterns(self.entities)
        self.assertEqual(len(groups), 1)
        self.assertEqual(len(groups[0]), 4)

    def test_plan_has_datum_offset(self):
        plan = _plan_from_entities(self.entities, "cnc_mill", it_grade=7)
        types_emitted = [item["dim_type"] for item in plan]
        self.assertIn("datum_offset", types_emitted, "Plan must include a datum_offset dim")

    def test_plan_has_centre_to_centre(self):
        plan = _plan_from_entities(self.entities, "cnc_mill", it_grade=7)
        types_emitted = [item["dim_type"] for item in plan]
        self.assertIn("centre_to_centre", types_emitted)

    def test_no_edge_to_hole_chain(self):
        """Verify plan never emits a chain that would be 'edge-to-hole-to-hole'.
        Implementation rule: only datum_offset + centre_to_centre, never
        consecutive 'length' dims on hole entities (which would imply chaining).
        """
        plan = _plan_from_entities(self.entities, "cnc_mill", it_grade=7)
        hole_lengths = [item for item in plan
                        if item["dim_type"] == "length" and "hole" in item.get("why_critical", "")]
        self.assertEqual(len(hole_lengths), 0,
                         "Holes must not generate 'length' dims (would imply edge-to-hole chain)")

    def test_datum_offset_needs_confirm(self):
        """Datum offset is function-critical -> needs_user_confirm=True."""
        plan = _plan_from_entities(self.entities, "cnc_mill", it_grade=7)
        datum_items = [item for item in plan if item["dim_type"] == "datum_offset"]
        self.assertTrue(len(datum_items) >= 1)
        for item in datum_items:
            self.assertTrue(item["needs_user_confirm"],
                            "datum_offset dims must have needs_user_confirm=True")

    def test_centre_to_centre_no_confirm(self):
        """Centre-to-centre dims are deterministic -> needs_user_confirm=False."""
        plan = _plan_from_entities(self.entities, "cnc_mill", it_grade=7)
        ctc_items = [item for item in plan if item["dim_type"] == "centre_to_centre"]
        self.assertTrue(len(ctc_items) >= 1)
        for item in ctc_items:
            self.assertFalse(item["needs_user_confirm"],
                             "centre_to_centre dims should not require user confirmation")

    def test_diameter_needs_confirm(self):
        """Hole diameter dims are mating features -> needs_user_confirm=True."""
        plan = _plan_from_entities(self.entities, "cnc_mill", it_grade=7)
        dia_items = [item for item in plan if item["dim_type"] == "diameter"]
        self.assertTrue(len(dia_items) >= 1)
        for item in dia_items:
            self.assertTrue(item["needs_user_confirm"],
                            "diameter dims on holes are function-critical, need confirm")

    def test_priority_ordering(self):
        """Datum offset has lower priority number (higher priority) than auxiliary dims."""
        plan = _plan_from_entities(self.entities, "cnc_mill", it_grade=7)
        datum_pri = [item["priority"] for item in plan if item["dim_type"] == "datum_offset"]
        ctc_pri = [item["priority"] for item in plan if item["dim_type"] == "centre_to_centre"]
        self.assertTrue(all(d < c for d in datum_pri for c in ctc_pri),
                        "Datum offset must have lower priority number than centre-to-centre")

    def test_known_size_tolerances_use_process_grade(self):
        plan = _plan_from_entities(self.entities, "cnc_mill", it_grade=7)
        tolerances = [item["default_tolerance"] for item in plan if item.get("default_tolerance")]
        self.assertTrue(tolerances)
        self.assertTrue(all(tol["it_grade"] == "IT7" for tol in tolerances))

    def test_fdm_uses_it12(self):
        grade = _it_grade_for_process("fdm")
        self.assertEqual(grade, 12)

    def test_cnc_uses_it7(self):
        grade = _it_grade_for_process("cnc_mill")
        self.assertEqual(grade, 7)

    def test_sheet_metal_uses_it10(self):
        grade = _it_grade_for_process("sheet_metal")
        self.assertEqual(grade, 10)

    def test_bilateral_tolerance_structure(self):
        tol = _bilateral_tolerance(20.0, 7)
        self.assertIn("it_grade", tol)
        self.assertIn("upper_mm", tol)
        self.assertIn("lower_mm", tol)
        self.assertIn("band_mm", tol)
        self.assertIn("source", tol)
        self.assertEqual(tol["it_grade"], "IT7")
        # IT7 at 20 mm is 21 µm; the bilateral tolerance is ±10.5 µm.
        self.assertAlmostEqual(tol["upper_mm"], 0.0105, places=6)
        self.assertAlmostEqual(tol["lower_mm"], -0.0105, places=6)
        self.assertAlmostEqual(tol["band_mm"], 0.021, places=6)

    def test_source_citation_present(self):
        tol = _bilateral_tolerance(20.0, 7)
        self.assertIn("citation", tol["source"])
        self.assertIn("ISO 286", tol["source"]["citation"])


class TestFitsIntegration(unittest.TestCase):
    """Cross-check that fit_calc and recommend_fit integrate well with the dimension scheme."""

    def test_sliding_to_fit_calc_pipeline(self):
        """recommend_fit -> fit_calc pipeline produces a press/clearance result."""
        rec = recommend_fit("sliding")
        self.assertTrue(rec["ok"])
        # Take first recommended class
        first_class = rec["answer"][0]["recommended_classes"][0]
        # Compute band for 20mm
        result = fit_calc(20.0, first_class)
        self.assertTrue(result["ok"], f"fit_calc failed: {result}")

    def test_fit_calc_result_integrates_with_confirm_flag(self):
        """fit_calc result fields map cleanly to ledger dimension entry."""
        result = fit_calc(20.0, "H7/g6")
        self.assertTrue(result["ok"])
        ans = result["answer"]
        # Fields needed for ledger dimension entry (contracts/advisor.md §2)
        self.assertIn("hole", ans)
        self.assertIn("shaft", ans)
        self.assertIn("fit_character", ans)
        self.assertIn("clearance", ans)

    def test_press_fit_flag(self):
        result = fit_calc(50.0, "H7/p6")
        self.assertTrue(result["ok"])
        self.assertEqual(result["answer"]["fit_character"], "press")


class TestProcessDifferentiation(unittest.TestCase):
    """DoD 3: machined bore vs printed hole produce different dimension schemes.

    The differences are sourced from engine_oracle/dfm/process_profiles.py:
    - cnc_mill: hole_comp_mm=0.0, IT grade 7 (±~0.010 mm at 20mm)
    - fdm:      hole_comp_mm=0.20, IT grade 12 (±~0.090 mm at 20mm)

    A bore modeled at 20mm nominal should be:
    - CNC: suggested_value=20.0 (no comp), IT7 tolerance band ~0.021 mm
    - FDM: suggested_value=20.20 (+0.20 comp so the PRINTED hole finishes at 20mm), IT12 ~0.180 mm
    """

    NOMINAL_MM = 20.0
    def setUp(self):
        from services.mcp import core_bridge as cb
        if not cb.available():
            self.skipTest("libchudware is not built")


    def _bore_plan(self, process: str) -> list[dict]:
        entities = _make_bore_sketch(self.NOMINAL_MM)
        it_grade = _it_grade_for_process(process)
        return _plan_from_entities(entities, process, it_grade)

    # ── IT-grade differs ─────────────────────────────────────────────────────────

    def test_cnc_uses_it7_fdm_uses_it12(self):
        """CNC→IT7, FDM→IT12: a different precision tier, not a rounding diff."""
        self.assertEqual(_it_grade_for_process("cnc_mill"), 7)
        self.assertEqual(_it_grade_for_process("fdm"), 12)

    def test_tolerance_band_differs_by_process(self):
        """ISO 286 Table 1: IT7 is 21 µm and IT12 is 210 µm at 20 mm."""
        tol_cnc = _bilateral_tolerance(self.NOMINAL_MM, 7)
        tol_fdm = _bilateral_tolerance(self.NOMINAL_MM, 12)
        # CNC band: IT7 at 20mm → 21µm = 0.021 mm
        self.assertAlmostEqual(tol_cnc["band_mm"], 0.021, places=6)
        self.assertAlmostEqual(tol_fdm["band_mm"], 0.210, places=6)
        # The FDM band is materially wider — not a float-precision tie
        self.assertGreater(tol_fdm["band_mm"], tol_cnc["band_mm"] * 5)

    # ── hole_comp_mm differs ─────────────────────────────────────────────────────

    def test_cnc_bore_no_hole_comp(self):
        """CNC mill has hole_comp_mm=0 — the modeled diameter equals the nominal."""
        plan = self._bore_plan("cnc_mill")
        dia_items = [i for i in plan if i["dim_type"] == "diameter"]
        self.assertTrue(len(dia_items) >= 1, "expected at least one diameter dim")
        for item in dia_items:
            self.assertAlmostEqual(item["hole_comp_mm"], 0.0, places=6,
                                   msg="CNC has no hole compensation")
            # suggested_value = nominal (no comp) — verify it equals NOMINAL_MM
            sv = item.get("suggested_value")
            if sv is not None:
                self.assertAlmostEqual(sv, self.NOMINAL_MM, places=3,
                                       msg="CNC: suggested_value should be the nominal diameter")

    def test_fdm_bore_gets_positive_hole_comp(self):
        """FDM has hole_comp_mm=0.20 — the modeled diameter is 0.20mm larger than nominal
        so the printed hole finishes at the nominal size after shrinkage."""
        plan = self._bore_plan("fdm")
        dia_items = [i for i in plan if i["dim_type"] == "diameter"]
        self.assertTrue(len(dia_items) >= 1, "expected at least one diameter dim")
        for item in dia_items:
            self.assertAlmostEqual(item["hole_comp_mm"], 0.20, places=6,
                                   msg="FDM hole_comp_mm must be 0.20 (from process_profiles)")
            sv = item.get("suggested_value")
            if sv is not None:
                # FDM: model the hole 0.20 mm larger so the FINISHED hole is NOMINAL_MM
                self.assertAlmostEqual(sv, self.NOMINAL_MM + 0.20, places=3,
                                       msg="FDM: suggested_value should be nominal + hole_comp")

    def test_machined_vs_printed_schemes_differ(self):
        """The two plans differ in both IT grade string and suggested_value — end-to-end proof."""
        cnc_plan = self._bore_plan("cnc_mill")
        fdm_plan = self._bore_plan("fdm")

        cnc_grades = {i["default_tolerance"]["it_grade"] for i in cnc_plan}
        fdm_grades = {i["default_tolerance"]["it_grade"] for i in fdm_plan}
        self.assertIn("IT7", cnc_grades, "CNC plan must carry IT7 tolerance")
        self.assertIn("IT12", fdm_grades, "FDM plan must carry IT12 tolerance")
        self.assertNotEqual(cnc_grades, fdm_grades,
                            "CNC and FDM must produce different IT grade sets")

        cnc_comp = {i["hole_comp_mm"] for i in cnc_plan}
        fdm_comp = {i["hole_comp_mm"] for i in fdm_plan}
        self.assertNotEqual(cnc_comp, fdm_comp,
                            "CNC and FDM must produce different hole_comp_mm values")

    def test_hole_comp_sourced_from_process_profiles(self):
        """The hole_comp values match process_profiles.PROFILES — they are never invented.
        get_profile() nests DFM params under a 'params' key; extract from there."""
        cnc_prof = get_profile("cnc_mill")
        fdm_prof = get_profile("fdm")

        def _get_comp(prof: dict) -> float:
            params = prof.get("params") or prof
            return float(params.get("hole_comp_mm") or 0.0)

        expected_cnc_comp = _get_comp(cnc_prof)
        expected_fdm_comp = _get_comp(fdm_prof)

        cnc_plan = self._bore_plan("cnc_mill")
        fdm_plan = self._bore_plan("fdm")

        for item in cnc_plan:
            self.assertAlmostEqual(item["hole_comp_mm"], expected_cnc_comp, places=6,
                                   msg="CNC hole_comp must match process_profiles")
        for item in fdm_plan:
            self.assertAlmostEqual(item["hole_comp_mm"], expected_fdm_comp, places=6,
                                   msg="FDM hole_comp must match process_profiles")

    # ── the lookup must fail loudly, never degrade to "no compensation" ──────────

    def test_unknown_process_profile_raises_teaching_error(self):
        """A process the DfM oracle does not know must raise, naming it AND the menu.

        Returning None/{} here is the defect class this repo is eliminating: the caller
        reads params.hole_comp_mm off it and either explodes with an AttributeError two
        frames later or — worse — plans an uncompensated hole that silently prints
        0.20 mm undersize.
        """
        from services.mcp.dimension_scheme_tools import _get_profile
        with self.assertRaises(ValueError) as ctx:
            _get_profile("lost_wax")
        msg = str(ctx.exception)
        self.assertIn("lost_wax", msg, "the error must name the requested process")
        for known in ("fdm", "cnc_mill"):
            self.assertIn(known, msg, "the error must list the available processes")

    def test_planner_does_not_swallow_an_unresolvable_process(self):
        """_plan_from_entities must propagate the teaching error, not plan comp=0.0.

        A silent 0.0 is indistinguishable from a legitimate CNC plan, so an agent asking
        for a misspelled process would get a confidently wrong scheme.
        """
        entities = _make_bore_sketch(self.NOMINAL_MM)
        with self.assertRaises(ValueError) as ctx:
            _plan_from_entities(entities, "lost_wax", it_grade=9)
        self.assertIn("lost_wax", str(ctx.exception))

    def test_missing_profile_does_not_fabricate_compensation(self):
        with unittest.mock.patch("services.mcp.dimension_scheme_tools._get_profile",
                                 side_effect=ValueError("profile unavailable")):
            with self.assertRaises(ValueError):
                _plan_from_entities(_make_bore_sketch(self.NOMINAL_MM), "fdm", it_grade=12)


class TestSketchHealthText(unittest.TestCase):
    """DoD 4 (offline portion): sketch_health_text() renders the three key states.

    The live-bridge tests (≥3 states against real engine) are in test_gsl_tools.py.
    These offline tests verify the renderer logic using synthetic health dicts.
    """

    def _make_health(self, *, dof: int = 0, status: str = "full",
                     open_endpoints: list | None = None,
                     mat_regions: int | None = None,
                     conflicting: list | None = None,
                     redundant: list | None = None,
                     hints: list | None = None,
                     dof_narration: str = "") -> dict:
        closure: dict = {"open_endpoints": open_endpoints or [],
                         "closed_chains_ok": not bool(open_endpoints),
                         "usage": "closed_profile"}
        if mat_regions is not None:
            closure["material_regions"] = mat_regions
        return {
            "ok": True,
            "status": status,
            "dof": dof,
            "dof_narration": dof_narration,
            "closure": closure,
            "conflicting": conflicting or [],
            "redundant": redundant or [],
            "hints": hints or [],
            "free_variables": [],
        }

    def _text(self, **kw) -> str:
        from services.mcp.gsl_tools import sketch_health_text
        return sketch_health_text(self._make_health(**kw))

    # ── State A: fully constrained + closed ──────────────────────────────────────

    def test_state_a_fully_defined(self):
        """State A: closed profile, DOF 0 → reads 'CLOSED' and 'fully constrained'."""
        t = self._text(dof=0, status="full", open_endpoints=[], mat_regions=1)
        self.assertIn("CLOSED", t)
        self.assertIn("1 material region", t)
        self.assertIn("fully constrained", t)
        self.assertNotIn("OPEN", t)
        self.assertNotIn("CONFLICT", t)

    # ── State B: open profile + underconstrained ─────────────────────────────────

    def test_state_b_open_underconstrained(self):
        """Open profile + DOF>0 reports dangling endpoints, narration and hints."""
        sched = [
            {"order": 1, "kind": "geometric", "action": "coincident",
             "why": "close the open chain", "handles": ["e1.p2", "e2.p1"]},
            {"order": 2, "kind": "dimension", "action": "add driving dimension",
             "why": "e1 is under-constrained in x, y", "entity": "e1"},
        ]
        t = self._text(dof=3, status="under",
                       open_endpoints=["e1.p2", "e2.p1"],
                       dof_narration="line e1 x, y; line e2 x",
                       hints=sched)
        self.assertIn("OPEN", t)
        self.assertIn("e1.p2", t)
        self.assertIn("e2.p1", t)
        self.assertIn("sketch_auto_constrain", t)
        self.assertIn("DOF: 3", t)
        self.assertIn("line e1 x, y; line e2 x", t)
        # Hints are suggestions, not an ordered plan.
        self.assertIn("[geometric] coincident", t)
        self.assertIn("[dimension] add driving dimension", t)
        self.assertIn("close the open chain", t)


    # ── State C: conflicting / over-defined ──────────────────────────────────────

    def test_state_c_conflicting(self):
        """State C: conflicting constraints → reports OVER-CONSTRAINED + lists conflicts."""
        t = self._text(dof=-1, status="over",
                       conflicting=[{"id": "c5", "type": "distance",
                                     "suggestion": "remove conflicting constraint c5 (distance)"}])
        self.assertIn("OVER-CONSTRAINED", t)
        self.assertIn("CONFLICT c5", t)
        self.assertIn("distance", t)
        self.assertIn("remove conflicting constraint", t)

    # ── Hint handles-vs-entity rendering ─────────────────────────────────────────

    def test_hint_handles_rendered(self):
        """A hint with handles renders them inline."""
        sched = [{"order": 1, "kind": "geometric", "action": "coincident",
                  "why": "close chain", "handles": ["e3.p2", "e4.p1"]}]
        t = self._text(dof=1, status="under", hints=sched)
        self.assertIn("e3.p2", t)
        self.assertIn("e4.p1", t)

    def test_empty_hints_when_fully_defined(self):
        """No hints when DOF=0 and status=full → just says 'nothing to do'."""
        t = self._text(dof=0, status="full", hints=[])
        self.assertIn("nothing to do", t)

    def test_redundant_constraint_listed(self):
        """Redundant constraints appear in the output with their id, type, suggestion."""
        t = self._text(dof=0, status="full",
                       redundant=[{"id": "c9", "type": "horizontal",
                                   "suggestion": "remove redundant constraint c9 (horizontal)"}])
        self.assertIn("REDUNDANT c9", t)
        self.assertIn("horizontal", t)

    # ── Redundancy economy (live 2026-07-30) ─────────────────────────────────────

    def test_all_identical_hint_rows_collapse_to_one(self):
        """Similar hints for 12 entities collapse without losing their count or names."""
        sched = [{"order": i + 2, "kind": "dimension",
                  "action": "add driving dimension / geometric relation for e%d" % (i + 1),
                  "why": "e%d is under-constrained in p1 x, p1 y, p2 x, p2 y" % (i + 1),
                  "entity": "e%d" % (i + 1)}
                 for i in range(12)]
        t = self._text(dof=40, status="under", mat_regions=1, hints=sched)
        rows = [ln for ln in t.split("\n") if ln.startswith("  ") and "[dimension]" in ln]
        self.assertEqual(len(rows), 1, t)
        row = rows[0]
        self.assertIn("x12", row)          # the count survives
        self.assertIn("e1", row)
        self.assertIn("+4", row)           # 8 named + "… +4"
        self.assertIn("under-constrained in p1 x", row)  # the remedy survives, once

    def test_distinct_hint_remedies_are_never_merged(self):
        """Different suggestions must both survive."""
        sched = [
            {"order": 1, "kind": "geometric", "action": "coincident",
             "why": "close the open chain", "handles": ["e1.p2", "e2.p1"]},
            {"order": 2, "kind": "dimension", "action": "add driving dimension for e1",
             "why": "e1 is under-constrained in x", "entity": "e1"},
            {"order": 3, "kind": "dimension", "action": "add radius dimension for e2",
             "why": "e2 has a free radius", "entity": "e2"},
        ]
        t = self._text(dof=4, status="under", hints=sched)
        self.assertIn("[geometric] coincident", t)
        self.assertIn("[dimension] add driving dimension for e1", t)
        self.assertIn("[dimension] add radius dimension for e2", t)

    def test_dof_narration_is_bounded_but_still_names_the_count(self):
        """The under-constrained signal is real and must survive — the 40-slot ENUMERATION
        is what does not. Keep dof, status and a bounded sample; say how many were elided."""
        narration = "; ".join("line e%d p1 x, p1 y, p2 x, p2 y" % (i + 1) for i in range(12))
        t = self._text(dof=40, status="under", mat_regions=1, dof_narration=narration)
        self.assertIn("DOF: 40 free", t)
        self.assertIn("line e1 p1 x", t)      # sample survives
        self.assertIn("+8 more", t)           # elision is counted, not silent
        self.assertNotIn("line e12", t)       # the tail is not enumerated



class TestDrawingDimensionScheme(unittest.TestCase):
    class Registry:
        def __init__(self):
            self.tools = {}

        def tool(self):
            def decorate(function):
                self.tools[function.__name__] = function
                return function
            return decorate

    def _tools(self):
        registry = self.Registry()
        register_dimension_scheme_tools(registry)
        return registry.tools

    def test_unknown_and_omitted_process_are_refused_with_valid_list(self):
        with unittest.mock.patch("services.mcp.dimension_scheme_tools._known_processes",
                                 return_value=["cnc_mill", "fdm"]):
            for process, reason in ((None, "process_required"), ("lost_wax", "unknown_process")):
                resolved, error = _process_verdict(process, "suggest_drawing_dimension_scheme")
                self.assertIsNone(resolved)
                self.assertEqual(error["reason"], reason)
                self.assertIn("cnc_mill", error["valid_processes"])
                self.assertIn("fdm", error["valid_processes"])

    def test_sketch_entry_point_no_longer_falls_back_to_fdm(self):
        with unittest.mock.patch("services.mcp.dimension_scheme_tools._known_processes",
                                 return_value=["cnc_mill", "fdm"]):
            result = json.loads(self._tools()["suggest_dimension_scheme"](
                doc_id="doc-1", sketch_id="sk1", process="lost_wax"))
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "unknown_process")
        self.assertNotEqual(result.get("process"), "fdm")

    def test_drawing_entry_point_reads_hole_semantics_and_requires_process(self):
        features = [{
            "id": "f-hole", "name": "Clamp threads", "type": "hole",
            "params": {"style": "tap", "thread": "M6x1.0", "depth": 14,
                       "end": "blind", "positions": [[-10, -10], [10, -10],
                                                     [10, 10], [-10, 10]]},
        }]
        plan = _drawing_plan_from_features(features, "cnc_mill", 7)
        self.assertEqual(len(plan), 1)
        self.assertEqual(plan[0]["annotation_kind"], "thread")
        self.assertEqual(plan[0]["suggested_tool"], "add_hole_callout")
        self.assertEqual(plan[0]["thread"], "M6x1.0")
        self.assertEqual(plan[0]["depth"], 14.0)

    def test_simple_custom_bore_plan_is_not_a_thread(self):
        features = [{"id": "bore", "type": "hole",
                     "params": {"style": "simple", "standard": "custom", "diameter": 5}},
                    {"id": "tap", "type": "hole",
                     "params": {"style": "tap", "standard": "custom", "depth": 14}}]
        plan = _drawing_plan_from_features(features, "cnc_mill", 7)
        bore = [row for row in plan if row["feature_ref"] == "bore"]
        self.assertEqual(len(bore), 1)
        self.assertEqual(bore[0]["dim_type"], "diameter_dim")
        self.assertEqual(bore[0]["suggested_value"], 5)
        tap = next(row for row in plan if row["feature_ref"] == "tap")
        self.assertEqual(tap["annotation_kind"], "thread")
        self.assertIsNone(tap["thread"])
        self.assertTrue(tap["needs_user_confirm"])

    def test_chamfer_plan_uses_native_chamfer_tool(self):
        plan = _drawing_plan_from_features([
            {"id": "edge-break", "type": "chamfer", "params": {"distance": 1.5}}
        ], "cnc_mill", 7)
        self.assertEqual(len(plan), 1)
        self.assertEqual(plan[0]["suggested_tool"], "add_chamfer_dimension")
        self.assertEqual(plan[0]["feature_ref"], "edge-break")
        self.assertFalse(plan[0]["placeable"])

    def test_drawing_entry_point_returns_process_specific_feature_plan(self):
        import services.mcp.core_bridge as core_bridge

        def fake_call(method, params=None):
            if method == "dfm_known_processes":
                return {"ok": True, "result": [
                    "fdm", "cnc_mill", "lathe", "sheet_metal", "laser", "injection"]}
            if method == "doc_list":
                return {"docs": [{"doc_id": "doc-1", "name": "Plate"}]}
            if method == "doc_tree":
                return {"tree": {"features": [{
                    "id": "f-box", "name": "Stock", "type": "box",
                    "params": {"length": 60, "width": 40, "height": 10},
                }]}}
            raise AssertionError(method)

        with unittest.mock.patch.object(core_bridge, "call", fake_call):
            result = json.loads(self._tools()["suggest_drawing_dimension_scheme"](
                doc_id="doc-1", process="cnc_mill"))
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["process"], "cnc_mill")
        self.assertEqual(result["it_grade"], "IT7")
        self.assertEqual({item["source_param"] for item in result["items"]},
                         {"length", "width", "height"})
        self.assertTrue(all(item["default_tolerance"]["it_grade"] == "IT7"
                            for item in result["items"]))

class TestDimensionDependencies(unittest.TestCase):
    def plan(self, entities, constraints=(), health=None):
        with unittest.mock.patch("services.mcp.dimension_scheme_tools._profile_params",
                                 return_value={"hole_comp_mm": 0.0}):
            return _plan_from_entities(entities, "cnc_mill", 7,
                                       constraints=list(constraints), health=health)

    def lines(self):
        return [{"id": "e1", "type": "line", "p1": [0, 0], "p2": [20, 0]},
                {"id": "e2", "type": "line", "p1": [0, 8], "p2": [20, 8]}]

    def test_driven_measurement_does_not_suppress_size(self):
        plan = self.plan(self.lines()[:1], [
            {"type": "length", "refs": ["e1"], "value": 20, "driven": True}])
        self.assertEqual([row["dim_type"] for row in plan], ["length"])

    def test_relations_precede_dimensions_and_existing_relations_are_not_repeated(self):
        plan = self.plan(self.lines(), health={
            "status": "under", "dof": 2,
            "free_variables": [{"entity": "e1", "param": "p1 x"},
                               {"entity": "e2", "param": "p1 x"}]})
        relations = [row for row in plan if row["dim_type"] == "relation"]
        self.assertEqual({row["constraint"]["type"] for row in relations}, {"parallel", "equal"})
        self.assertLess(max(row["priority"] for row in relations),
                        min(row["priority"] for row in plan if row["dim_type"] == "length"))
        plan = self.plan(self.lines(), [{"type": "equal", "refs": ["e1", "e2"]}])
        self.assertNotIn("equal", [row["constraint"]["type"] for row in plan
                                   if row["dim_type"] == "relation"])

    def test_arc_radius_and_nonhole_circles_are_not_compensated(self):
        entities = [{"id": "arc", "type": "arc", "center": [0, 0], "radius": 4},
                    {"id": "boss", "type": "circle", "center": [20, 0], "radius": 5},
                    {"id": "bore", "type": "circle", "center": [40, 0], "radius": 5,
                     "role": "bore"}]
        with unittest.mock.patch("services.mcp.dimension_scheme_tools._profile_params",
                                 return_value={"hole_comp_mm": 0.2}):
            plan = _plan_from_entities(entities, "fdm", 12)
        sizes = {row["entity_ref"]: row for row in plan if row["dim_type"] != "relation"}
        self.assertEqual(sizes["arc"]["dim_type"], "radius")
        self.assertEqual(sizes["arc"]["suggested_value"], 4)
        self.assertFalse(sizes["arc"]["needs_user_confirm"])
        self.assertEqual(sizes["boss"]["suggested_value"], 10)
        self.assertEqual(sizes["boss"]["hole_comp_mm"], 0)
        self.assertEqual(sizes["bore"]["suggested_value"], 10.2)
        self.assertTrue(sizes["bore"]["needs_user_confirm"])

    def test_pitch_tolerance_uses_pitch_and_unknown_datum_has_no_tolerance(self):
        plan = self.plan(_dim_fixture())
        pitch = next(row for row in plan if row["dim_type"] == "centre_to_centre")
        self.assertEqual(pitch["suggested_value"], 40)
        self.assertEqual(pitch["default_tolerance"]["band_mm"], 0.025)
        self.assertIsNone(next(row for row in plan if row["dim_type"] == "datum_offset")
                          ["default_tolerance"])

    def test_native_health_suppresses_solved_entities_and_fixed_radius(self):
        entities = self.lines()[:1] + [
            {"id": "e2", "type": "circle", "center": [8, 0], "radius": 3}]
        plan = self.plan(entities, health={
            "status": "under", "dof": 2, "statuses": {"e1": "full", "e2": "under"},
            "free_variables": [{"entity": "e2", "param": "center x"},
                               {"entity": "e2", "param": "center y"}]})
        self.assertEqual([row for row in plan if row["dim_type"] != "relation"], [])

    def test_full_native_health_needs_no_further_dimensions(self):
        self.assertEqual(self.plan(self.lines(), health={"status": "full", "dof": 0}), [])

    def test_iso_tolerance_values_are_tabulated(self):
        self.assertEqual(_bilateral_tolerance(8, 7)["band_mm"], 0.015)
        self.assertEqual(_bilateral_tolerance(12, 7)["band_mm"], 0.018)
        self.assertEqual(_bilateral_tolerance(20, 7)["source"]["kind"], "table")

    def test_live_entrypoint_reads_authored_dependencies_and_native_health(self):
        import services.mcp.dimension_scheme_tools as scheme
        import services.mcp.core_bridge as core_bridge

        calls = []
        def call(method, params=None):
            calls.append(method)
            if method == "doc_sketch_overview":
                return {"entities": self.lines()[:1]}
            if method == "doc_tree":
                return {"tree": {"features": [{"id": "s1", "params": {
                    "entities": self.lines()[:1],
                    "constraints": [{"type": "length", "refs": ["e1"], "value": 20}]}}]}}
            if method == "sketch_health":
                return {"status": "under", "dof": 3, "statuses": {"e1": "under"},
                        "free_variables": [{"entity": "e1", "param": "p1 x"}]}
            raise AssertionError(method)

        registry = TestDrawingDimensionScheme.Registry()
        register_dimension_scheme_tools(registry)
        with (unittest.mock.patch.object(core_bridge, "call", call),
              unittest.mock.patch.object(scheme, "_doc", lambda d: d),
              unittest.mock.patch.object(scheme, "_known_processes", return_value=["cnc_mill"]),
              unittest.mock.patch.object(scheme, "_profile_params", return_value=None)):
            result = json.loads(registry.tools["suggest_dimension_scheme"](
                doc_id="doc-1", sketch_id="s1", process="cnc_mill"))
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["items"], [])
        self.assertIn("sketch_health", calls)
        self.assertIn("doc_tree", calls)

    def test_sketch_entrypoint_refuses_unavailable_tree_without_fixture(self):
        import services.mcp.dimension_scheme_tools as scheme
        import services.mcp.core_bridge as core_bridge

        def call(method, params=None):
            if method == "doc_sketch_overview":
                return {"entities": self.lines()[:1]}
            raise RuntimeError("authored tree unavailable")

        registry = TestDrawingDimensionScheme.Registry()
        register_dimension_scheme_tools(registry)
        with (unittest.mock.patch.object(core_bridge, "call", call),
              unittest.mock.patch.object(scheme, "_doc", lambda d: d),
              unittest.mock.patch.object(scheme, "_known_processes", return_value=["cnc_mill"])):
            result = json.loads(registry.tools["suggest_dimension_scheme"](
                doc_id="doc-1", sketch_id="s1", process="cnc_mill"))
        self.assertFalse(result["ok"], result)
        self.assertIn("authored tree unavailable", result["error"])
        self.assertNotIn("items", result)

    def test_sketch_entrypoint_does_not_invent_an_unavailable_size(self):
        import services.mcp.dimension_scheme_tools as scheme
        import services.mcp.core_bridge as core_bridge

        def call(method, params=None):
            if method == "doc_sketch_overview":
                return {"entities": [{"id": "e1", "type": "circle", "center": [0, 0]}]}
            if method == "doc_tree":
                return {"tree": {"features": [{"id": "s1", "params": {
                    "entities": [{"id": "e1", "type": "circle", "center": [0, 0]}],
                    "constraints": []}}]}}
            if method == "sketch_health":
                return {"status": "under", "free_variables": [{"entity": "e1", "param": "radius"}]}
            raise AssertionError(method)

        registry = TestDrawingDimensionScheme.Registry()
        register_dimension_scheme_tools(registry)
        with (unittest.mock.patch.object(core_bridge, "call", call),
              unittest.mock.patch.object(scheme, "_doc", lambda d: d),
              unittest.mock.patch.object(scheme, "_known_processes", return_value=["cnc_mill"])):
            result = json.loads(registry.tools["suggest_dimension_scheme"](
                doc_id="doc-1", sketch_id="s1", process="cnc_mill"))
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["reason"], "sketch_size_unavailable")
        self.assertEqual(result["entities"], ["e1"])


def test_live_daemon_dimension_planners_read_authored_features():
    import pytest
    from services.mcp import core_bridge as cb

    if not cb.available():
        pytest.skip("libchudware is not built")
    doc = cb.call("doc_new", {"name": "dimension_plan_live", "kind": "part"})["doc_id"]
    try:
        cb.call("doc_apply_ops", {"doc_id": doc, "ops": [
            {"op": "add_feature", "feature": {
                "type": "sketch", "name": "PlanSketch", "refs": {"plane": "plane.top"},
                "params": {"entities": [{"id": "e1", "type": "circle",
                                         "center": [12, 7], "radius": 3}],
                           "constraints": []}}},
            {"op": "add_feature", "feature": {
                "type": "box", "name": "PlanStock",
                "params": {"length": 60, "width": 40, "height": 10}}},
        ]})
        features = cb.call("doc_tree", {"doc_id": doc})["tree"]["features"]
        sketch = next(f for f in features if f.get("name") == "PlanSketch")
        stock = next(f for f in features if f.get("name") == "PlanStock")
        registry = TestDrawingDimensionScheme.Registry()
        register_dimension_scheme_tools(registry)
        drawing = json.loads(registry.tools["suggest_drawing_dimension_scheme"](
            doc_id=doc, process="cnc_mill"))
        assert drawing["ok"], drawing
        stock_items = [row for row in drawing["items"] if row["feature_ref"] == stock["id"]]
        assert {row["source_param"] for row in stock_items} == {"length", "width", "height"}
        sketch_plan = json.loads(registry.tools["suggest_dimension_scheme"](
            doc_id=doc, sketch_id=sketch["id"], process="cnc_mill"))
        assert sketch_plan["ok"], sketch_plan
        diameter = next(row for row in sketch_plan["items"]
                        if row["entity_ref"] == "e1" and row["dim_type"] == "diameter")
        assert abs(diameter["suggested_value"] - 6.0) < 1e-6, sketch_plan
        assert all("fixture" not in note.lower() for note in sketch_plan["notes"])
    finally:
        cb.call("doc_close", {"doc_id": doc})


def test_drawing_scheme_does_not_report_stored_literal_as_equation_result():
    features = [{"id": "box", "type": "box", "params": {
        "length": {"value": 10, "expr": "width * 2"},
        "width": {"value": 8, "expr": None},
        "height": {"value": 4, "expr": None},
    }}]
    rows = _drawing_plan_from_features(features, "cnc_mill", 7)
    length = next(row for row in rows if row.get("source_param") == "length")
    assert length["suggested_value"] is None
    assert length["placeable"] is False
    assert length["needs_user_confirm"] is True


if __name__ == "__main__":
    unittest.main()
