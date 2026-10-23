"""geometry_designer role + proactive op-aware helper/calculator registry.

Covers the Definition of Done:
  * the helper registry has ≥8 geometry kinds, each trigger-bound;
  * each resolver returns REAL calc/profile output with provenance (no fabricated
    numbers) — the provenance string names the tool that produced the value;
  * material/process gating CHANGES the emitted minimums (same feature, machined vs
    printed → different numbers) — the differentiator test;
  * the two added calculators (bend_allowance, draft_check) compute cited results;
  * the two added triggers (sheet_bend, molded_draft) fire and stay ≥ the ≥8 floor;
  * the geometry_designer role loads + validates and its handoff schema compiles;
  * the GEOMETRY stage gate reads geometry_spec[] with provenance from the ledger.

Headless: no FastMCP, no libchudware, no network, no LLM.
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve()
_ROOT = _HERE.parents[2]
for p in (str(_ROOT),):
    if p not in sys.path:
        sys.path.insert(0, p)

from services.agent import geometry_helpers as gh          # noqa: E402
from services.agent import roles                            # noqa: E402
from services.agent.ledger import Ledger                    # noqa: E402
from services.agent.pipeline import phases                  # noqa: E402
from services.research.calcs import run_calc           # noqa: E402

_TMP = os.environ.get("TMPDIR", "/tmp")


def _fresh(suffix: str) -> Ledger:
    run_id = f"dp3_{suffix}_{os.getpid()}"
    path = Path(_TMP) / "dp3_runs" / run_id / "ledger.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return Ledger(run_id, path)


# --------------------------------------------------------------------------- registry shape

class TestRegistryShape(unittest.TestCase):
    def test_at_least_eight_kinds(self):
        kinds = gh.known_kinds()
        self.assertGreaterEqual(len(kinds), 8, kinds)

    def test_every_kind_binds_a_trigger_and_tools(self):
        for kind in gh.known_kinds():
            b = gh.binding_for(kind)
            self.assertIsNotNone(b, kind)
            self.assertTrue(b.trigger_id, f"{kind}: no trigger")
            self.assertTrue(b.tools, f"{kind}: no helper tools")
            self.assertIn(b.resolver, gh._RESOLVERS, f"{kind}: resolver unregistered")

    def test_every_kind_trigger_id_is_a_real_trigger(self):
        # "trigger-bound" (DoD): each binding's trigger_id must resolve to a real
        # entry in the loaded aicad.trigger/1 table — not a typo.
        from services.agent.advisor.triggers import load_table
        ids = {t["id"] for t in load_table()}
        for kind in gh.known_kinds():
            b = gh.binding_for(kind)
            self.assertIn(b.trigger_id, ids,
                          f"{kind}: trigger_id {b.trigger_id!r} not in trigger table")

    def test_aliases_resolve(self):
        for alias, canon in (("bore", "mating_bore"), ("boss", "threaded_hole"),
                             ("wall", "thin_wall"), ("bend", "sheet_bend"),
                             ("draft", "molded_draft")):
            b = gh.binding_for(alias)
            self.assertIsNotNone(b, alias)
            self.assertEqual(b.kind, canon)

    def test_unknown_kind_returns_none(self):
        self.assertIsNone(gh.resolve_helpers("not_a_real_kind"))


# --------------------------------------------------------------------------- provenance is real

class TestProvenanceIsReal(unittest.TestCase):
    def test_wall_provenance_cites_profile(self):
        r = gh.resolve_helpers("thin_wall", process="printed")
        self.assertTrue(r.ok)
        self.assertIn("min_wall_mm", r.numbers)
        self.assertIn("process_profiles", r.provenance)

    def test_fit_provenance_cites_fit_calc(self):
        r = gh.resolve_helpers("mating_bore", nominal_mm=20, process="machined")
        self.assertIn("iso286", r.numbers)
        # the ISO 286 band is the real fit_calc clearance, not a recalled number
        self.assertEqual(r.numbers["iso286"]["clearance"], [0.007, 0.041])
        self.assertIn("fit_calc", r.provenance)

    def test_thread_provenance_cites_tap_drill(self):
        r = gh.resolve_helpers("threaded_hole", thread="M6x1.0", material="aluminum")
        self.assertTrue(r.ok)
        self.assertEqual(r.numbers["tap_drill"]["tap_drill_mm"], 5.0)
        self.assertEqual(r.numbers["min_engagement_x_dia"], 1.5)  # Al = 1.5×D
        self.assertIn("tap_drill", r.provenance)

    def test_material_provenance_cites_lookup(self):
        r = gh.resolve_helpers("material_dims", material="6061")
        self.assertTrue(r.ok)
        self.assertEqual(r.numbers["yield_mpa"], 276)
        self.assertIn("research_material_lookup", r.provenance)


# --------------------------------------------------------------------------- THE gating test

class TestMaterialProcessGating(unittest.TestCase):
    """Same feature, machined vs printed → different minimums (DoD)."""

    def test_wall_minimum_differs_machined_vs_printed(self):
        machined = gh.resolve_helpers("thin_wall", process="machined")
        printed = gh.resolve_helpers("thin_wall", process="printed")
        self.assertNotEqual(machined.numbers["min_wall_mm"],
                            printed.numbers["min_wall_mm"])
        # cnc min wall (0.5) < fdm min wall (1.0) — the printed part needs more meat
        self.assertLess(machined.numbers["min_wall_mm"],
                        printed.numbers["min_wall_mm"])

    def test_tolerance_band_differs_machined_vs_printed(self):
        machined = gh.resolve_helpers("mating_bore", nominal_mm=20, process="machined")
        printed = gh.resolve_helpers("mating_bore", nominal_mm=20, process="printed")
        self.assertNotEqual(machined.numbers["tolerance_band_mm"],
                            printed.numbers["tolerance_band_mm"])
        self.assertLess(machined.numbers["tolerance_band_mm"],
                        printed.numbers["tolerance_band_mm"])

    def test_fit_class_differs_machined_vs_molded(self):
        machined = gh.resolve_helpers("mating_bore", process="machined")
        molded = gh.resolve_helpers("mating_bore", process="molded")
        self.assertNotEqual(
            machined.numbers["process_fit_classes_mm"]["location"],
            molded.numbers["process_fit_classes_mm"]["location"],
        )


# --------------------------------------------------------------------------- new calculators

class TestAddedCalculators(unittest.TestCase):
    def test_bend_allowance_is_cited(self):
        r = run_calc("bend_allowance",
                     {"thickness_mm": 2.0, "inside_radius_mm": 2.0, "angle_deg": 90})
        self.assertTrue(r["ok"])
        self.assertAlmostEqual(r["answer"]["bend_allowance_mm"], 4.241, places=2)
        self.assertIn("Machinery", r["source"]["citation"])

    def test_bend_deduction_relation_holds(self):
        r = run_calc("bend_allowance",
                     {"thickness_mm": 1.5, "inside_radius_mm": 1.5, "angle_deg": 90})
        a = r["answer"]
        # BD = 2·OSSB − BA (exact geometry relation)
        self.assertAlmostEqual(a["bend_deduction_mm"],
                               2 * a["outside_setback_mm"] - a["bend_allowance_mm"],
                               places=3)

    def test_draft_check_smooth_min_one_degree(self):
        r = run_calc("draft_check", {"draw_depth_mm": 30.0, "finish": "smooth"})
        self.assertTrue(r["ok"])
        self.assertEqual(r["answer"]["draft_deg"], 1.0)  # process_profiles injection min
        self.assertGreater(r["answer"]["offset_per_side_mm"], 0.0)

    def test_draft_scales_with_texture(self):
        smooth = run_calc("draft_check", {"draw_depth_mm": 30.0, "finish": "smooth"})
        heavy = run_calc("draft_check", {"draw_depth_mm": 30.0, "finish": "heavy_texture"})
        self.assertLess(smooth["answer"]["draft_deg"], heavy["answer"]["draft_deg"])

    def test_bad_inputs_fail_soft(self):
        self.assertFalse(run_calc("bend_allowance", {"thickness_mm": 0,
                                                     "inside_radius_mm": 1})["ok"])
        self.assertFalse(run_calc("draft_check", {"draw_depth_mm": -5})["ok"])


# --------------------------------------------------------------------------- triggers

class TestAddedTriggers(unittest.TestCase):
    def test_trigger_floor_and_new_ids(self):
        from services.agent.advisor.triggers import load_table
        ids = [t["id"] for t in load_table()]
        self.assertGreaterEqual(len(ids), 8)
        self.assertIn("sheet_bend", ids)
        self.assertIn("molded_draft", ids)

    def test_sheet_bend_fires(self):
        from services.agent.advisor.triggers import match_triggers
        _, le = match_triggers("fold a sheet-metal flange with a 90 degree bend")
        self.assertIn("sheet_bend", [e["trigger_id"] for e in le])

    def test_molded_draft_fires(self):
        from services.agent.advisor.triggers import match_triggers
        _, le = match_triggers("this injection molded wall needs draft for release")
        self.assertIn("molded_draft", [e["trigger_id"] for e in le])


# --------------------------------------------------------------------------- role

class TestGeometryDesignerRole(unittest.TestCase):
    def test_role_loads(self):
        c = roles.load_role("geometry_designer")
        self.assertEqual(c.id, "geometry_designer")
        self.assertIn("feature", c.stage_scope)

    def test_role_under_tool_cap(self):
        c = roles.load_role("geometry_designer")
        self.assertLessEqual(len(c.tool_allowlist), roles.MAX_TOOLS_PER_ROLE)

    def test_handoff_schema_requires_provenance(self):
        c = roles.load_role("geometry_designer")
        # the geometry_specs item schema must require provenance (the never-recall rule)
        item = (c.handoff_schema["properties"]["outputs"]["properties"]
                ["geometry_specs"]["items"])
        self.assertIn("provenance", item["required"])

    def test_handoff_schema_compiles_to_gbnf(self):
        c = roles.load_role("geometry_designer")
        g = roles.schema_to_gbnf(c.handoff_schema)
        self.assertIn("root ::=", g)

    def test_role_validates_clean(self):
        c = roles.load_role("geometry_designer")
        errs = [f for f in roles.validate_role(c) if f.level == "error"]
        self.assertEqual(errs, [], [str(e) for e in errs])


# --------------------------------------------------------------------------- gate reads ledger

class TestGeometryGateReadsLedger(unittest.TestCase):
    def _machine(self):
        return phases.PhaseMachine(flags=phases.PipelineFlags(design_pipeline=True))

    def test_gate_fails_without_specs(self):
        led = _fresh("nogeo")
        led.add_feature("feature_architect", "FT1", "mount", "R1")
        ctx = {"features": led.features(), "geometry_specs": led.geometry_specs()}
        res = phases._gate_geometry(self._machine(), ctx)
        self.assertFalse(res.passed)

    def test_gate_passes_with_covered_provenanced_specs(self):
        led = _fresh("geo")
        led.add_feature("feature_architect", "FT1", "mount", "R1")
        led.add_geometry_spec(
            "geometry_designer", "G1", realizes=["FT1"],
            shapes=[{"op": "extrude"}],
            provenance="wall_thickness(machining) → 0.5 mm [process_profiles.py]",
            nominal_dims=[{"name": "wall", "value": 0.5, "unit": "mm"}],
        )
        ctx = {"features": led.features(), "geometry_specs": led.geometry_specs()}
        res = phases._gate_geometry(self._machine(), ctx)
        self.assertTrue(res.passed, res.reasons)

    def test_gate_fails_on_missing_provenance_at_write(self):
        led = _fresh("noprov")
        with self.assertRaises(ValueError):
            led.add_geometry_spec("geometry_designer", "G1", realizes=["FT1"],
                                  shapes=[{"op": "extrude"}], provenance="")

    def test_gate_fails_on_uncovered_feature(self):
        led = _fresh("uncov")
        led.add_feature("feature_architect", "FT1", "mount", "R1")
        led.add_feature("feature_architect", "FT2", "seal", "R2")
        led.add_geometry_spec("geometry_designer", "G1", realizes=["FT1"],
                              shapes=[{"op": "extrude"}],
                              provenance="fit_calc → [iso286.json]")
        ctx = {"features": led.features(), "geometry_specs": led.geometry_specs()}
        res = phases._gate_geometry(self._machine(), ctx)
        self.assertFalse(res.passed)  # FT2 unrealized


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


# ── Strict process aliasing (2026-08-04) ─────────────────────────────────────
#
# The mission said the bracket "will be machined". BOTH opus-5 and k3-256k
# called set_process("machining") / dfm_check(process="machining") and were
# refused with "Unknown process 'machining'" — while _PROCESS_ALIASES had known
# that exact word all along. Nothing consulted it on the tool path.


def test_canonical_process_strict_maps_the_words_models_use():
    from services.agent.geometry_helpers import canonical_process_strict as c
    assert c("machining") == "cnc_mill"
    assert c("cnc_milling") == "cnc_mill"
    assert c("milling") == "cnc_mill"
    assert c("machined") == "cnc_mill"
    assert c("turned") == "lathe"
    assert c("3d printed") == "fdm"
    assert c("MACHINING") == "cnc_mill"      # case/whitespace insensitive
    assert c("  machining ") == "cnc_mill"


def test_canonical_process_strict_passes_unknowns_through():
    """Unlike canonical_process, an unknown word must NOT become 'fdm'.

    Silently manufacturing for the wrong process is worse than refusing: the
    caller's "Unknown process 'x'. Known processes: [...]" error has to survive.
    """
    from services.agent.geometry_helpers import (
        canonical_process,
        canonical_process_strict,
    )
    assert canonical_process_strict("bogus_process") == "bogus_process"
    assert canonical_process_strict(None) is None
    assert canonical_process_strict("") == ""
    # the lenient variant still defaults, which is why the strict one exists
    assert canonical_process("bogus_process") == "fdm"


def test_canonical_ids_are_idempotent():
    from services.agent.geometry_helpers import canonical_process_strict as c
    for pid in ("fdm", "cnc_mill", "lathe", "sheet_metal", "laser", "injection"):
        assert c(pid) == pid, pid
