"""Tests for suggest_dimension_scheme (T-W3).

Tests run in offline mode (no bridge / no live doc) using the built-in 4-hole
plate sketch fixture.  The bridge-unavailable path returns a synthetic entity
list and notes the offline mode.

Run:
  python -m pytest services/mcp/test_dimension_scheme.py -v
"""
from __future__ import annotations

import json
import sys
import types
import unittest

# ---------------------------------------------------------------------------
# Stubs so imports succeed without libaicad.so or live MCP server
# ---------------------------------------------------------------------------

def _setup_stubs():
    """Install lightweight stubs so dimension_scheme_tools and deps import cleanly."""
    # core_bridge stub
    cb = types.ModuleType("mcp_server.core_bridge")
    cb.available = lambda: False
    cb.call = lambda method, params=None: {}
    sys.modules.setdefault("mcp_server.core_bridge", cb)
    sys.modules.setdefault("core_bridge", cb)
    sys.modules.setdefault("services.mcp.core_bridge", cb)

    # mcp.server.fastmcp stub
    class _FakeMCP:
        def __init__(self): self.tools_reg = {}
        def tool(self):
            def decorator(fn):
                self.tools_reg[fn.__name__] = fn
                return fn
            return decorator

    mcp_mod = types.ModuleType("mcp")
    server_mod = types.ModuleType("mcp.server")
    fastmcp_mod = types.ModuleType("mcp.server.fastmcp")
    fastmcp_mod.FastMCP = _FakeMCP
    sys.modules.setdefault("mcp", mcp_mod)
    sys.modules.setdefault("mcp.server", server_mod)
    sys.modules.setdefault("mcp.server.fastmcp", fastmcp_mod)

    # mcp_server.cad_tools stubs for _dumps / _err / _doc
    ct = types.ModuleType("mcp_server.cad_tools")
    ct._dumps = json.dumps
    ct._err = lambda e: json.dumps({"ok": False, "error": str(e)})
    ct._doc = lambda d: d
    sys.modules.setdefault("mcp_server.cad_tools", ct)
    sys.modules.setdefault("mcp_server", types.ModuleType("mcp_server"))

    # engine stub (for research imports in fits.py / material_lookup.py)
    eng = types.ModuleType("engine")
    eng_materials = types.ModuleType("engine.materials")
    eng_materials.density = lambda name: None
    eng_paths = types.ModuleType("engine.paths")
    import pathlib
    eng_paths.CONFIG = pathlib.Path("/nonexistent/config.json")
    eng.materials = eng_materials
    eng.paths = eng_paths
    sys.modules.setdefault("engine", eng)
    sys.modules.setdefault("engine.materials", eng_materials)
    sys.modules.setdefault("engine.paths", eng_paths)


_setup_stubs()

# Now we can import the module under test
from engine_oracle.research.fits import fit_calc, recommend_fit  # noqa: E402
from engine_oracle.dfm.process_profiles import get_profile  # noqa: E402


# ---------------------------------------------------------------------------
# Import dimension_scheme internals directly (not via MCP)
# ---------------------------------------------------------------------------
from services.mcp.dimension_scheme_tools import (  # noqa: E402
    _plan_from_entities,
    _make_fixture_sketch as _dim_fixture,
    _it_grade_for_process,
    _bilateral_tolerance,
    _find_bolt_patterns,
)


class TestBoltPatternRule(unittest.TestCase):
    """The core T-W3 rule: bolt patterns use datum-offset + centre-to-centre only."""

    def setUp(self):
        # 4-hole plate: 40mm spacing, 5mm holes
        self.entities = _dim_fixture(num_holes=4, bolt_spacing_mm=40.0, hole_dia_mm=5.0)

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

    def test_tolerance_it_grade_present(self):
        """Each item's default_tolerance carries an IT grade reference."""
        plan = _plan_from_entities(self.entities, "cnc_mill", it_grade=7)
        for item in plan:
            tol = item.get("default_tolerance", {})
            self.assertIn("it_grade", tol, f"Item {item['entity_ref']} missing it_grade in tolerance")
            self.assertEqual(tol["it_grade"], "IT7")

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
        # IT7 at 20mm = 21µm; bilateral: upper=+10µm, lower=-11µm
        self.assertAlmostEqual(tol["upper_mm"], 0.010, places=6)
        self.assertAlmostEqual(tol["lower_mm"], -0.011, places=6)
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


if __name__ == "__main__":
    unittest.main()
