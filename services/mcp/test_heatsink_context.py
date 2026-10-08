"""Engine-free heatsink ownership and candidate-parameter regressions.

Run: python -m unittest services.mcp.test_heatsink_context -v
"""
from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from services.mcp import ecad_thermal as et
from services.mcp import heatsink as hs
from services.mcp import simhandoff as sh
from services.parts.adapters.heatsinks_adapter import HeatsinksAdapter


def _context(doc_id="board-a", handoff_id="thermal-a"):
    # The real last_ctx layout produced by ecad_thermal_handoff; mesh=None selects
    # the documented network estimate without an engine, board files, or daemon.
    return {
        "doc_id": doc_id, "handoff_envelope_id": handoff_id,
        "env": {"name": "still_air", "t_amb_c": 25.0, "h_w_m2k": 10.0},
        "board": {"bbox_mm": [0.0, 0.0, 50.0, 40.0],
                  "z_bot_mm": 0.0, "z_top_mm": 1.6},
        "components": [{"ref": "U1", "power_w": {"typ": 3.0},
                        "theta_c_per_w": {"jc": 2.0},
                        "fea": {"rect_mm": [0.0, 0.0, 5.0, 5.0],
                                "top_area_mm2": 25.0, "z_top_mm": 3.6}}],
        "material": {"name": "FR4", "k_w_mk": 0.3,
                     "cp_j_kgk": 1000.0, "rho_kg_m3": 1900.0},
        "fixtures": [], "flux_loads": [], "transient": {}, "mesh": None,
    }


def _catalog_search(query, *, domain, include_live, limit):
    # Use the real curated adapter, isolating the process-wide hub/disk index only.
    if include_live:
        raise AssertionError("heatsink selection must not require a live catalog")
    return HeatsinksAdapter().search(query, domain=domain)[:limit]


class HeatsinkContextTests(unittest.TestCase):
    def setUp(self):
        self.ctx = _context()
        self.enterContext(patch.dict(et._THERMAL, {"last_ctx": self.ctx}))
        self.enterContext(patch("services.parts.search", new=_catalog_search))
        self.params = {"component_ref": "U1", "tj_target_c": 85.0,
                       "include_library": False, "include_parametric": True}

    def select(self, **params):
        return hs.heatsink_select_from_params({**self.params, **params})

    def assert_source(self, out):
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["mode"], "network_estimate")
        self.assertEqual(out["simhandoff"]["source"]["doc_id"], "board-a")
        self.assertEqual(out["simhandoff"]["source"]["handoff_id"], "thermal-a")
        self.assertEqual(sh.validate_envelope(out["simhandoff"]), [])
        self.assertTrue(out["table"])

    def test_cross_board_and_stale_same_board_refuse_before_candidate_work(self):
        for expected, reason in (
                ({"doc_id": "board-b", "handoff_id": "thermal-a"},
                 "thermal_source_doc_mismatch"),
                ({"doc_id": "board-a", "handoff_id": "thermal-old"},
                 "thermal_source_handoff_mismatch")):
            with self.subTest(expected=expected), patch.object(
                    hs, "gather_candidates", side_effect=AssertionError("candidate work")), \
                    patch.object(hs, "_resolve_candidate",
                                 side_effect=AssertionError("solver work")):
                out = self.select(**expected)
            self.assertFalse(out["ok"])
            self.assertEqual(out["reason"], reason)
            self.assertEqual(out["expected_source"], expected)
            self.assertEqual(out["current_source"],
                             {"doc_id": "board-a", "handoff_id": "thermal-a"})
            self.assertIn("before candidate work", out["error"])
            self.assertIn("thermal handoff again", out["hint"])
            self.assertTrue(out["next_actions"])
            self.assertFalse({"table", "winner", "simhandoff"} & out.keys())
            self.assertIs(et._THERMAL["last_ctx"], self.ctx)

    def test_legacy_and_current_or_partial_expectations_use_real_context(self):
        for ownership in ({}, {"doc_id": "board-a"}, {"handoff_id": "thermal-a"},
                          {"doc_id": "board-a", "handoff_id": "thermal-a"}):
            with self.subTest(ownership=ownership):
                self.assert_source(self.select(**ownership))

    def test_captured_context_is_used_even_if_new_handoff_arrives_during_candidates(self):
        gather = hs.gather_candidates

        def replace_context(*args, **kwargs):
            et._THERMAL["last_ctx"] = _context("board-b", "thermal-b")
            return gather(*args, **kwargs)

        with patch.object(hs, "gather_candidates", new=replace_context):
            out = self.select(doc_id="board-a", handoff_id="thermal-a")
        self.assert_source(out)
        self.assertEqual(et._THERMAL["last_ctx"]["doc_id"], "board-b")

    def test_current_source_honors_tim_catalog_flags_case_target_and_exact_limit(self):
        for library, parametric in ((True, False), (False, True), (True, True)):
            with self.subTest(library=library, parametric=parametric):
                out = self.select(doc_id="board-a", handoff_id="thermal-a",
                                  tj_target_c=None, t_case_target_c=85.0,
                                  tim_t_mm=0.24, tim_k_w_mk=4.5,
                                  include_library=library, include_parametric=parametric,
                                  max_candidates=1)
                self.assert_source(out)
                self.assertEqual(out["target"], {"kind": "t_case", "value_c": 85.0})
                self.assertEqual(out["tim"]["t_mm"], 0.24)
                self.assertEqual(out["tim"]["k_w_mk"], 4.5)
                self.assertEqual(len(out["table"]), 1)
                sources = {row["source"] for row in out["table"]}
                if not library:
                    self.assertEqual(sources, {"parametric"})
                if not parametric:
                    self.assertEqual(sources, {"library"})
                row = out["table"][0]
                resistance = 0.24e-3 / (4.5 * 25.0e-6)
                self.assertAlmostEqual(row["r_tim_c_per_w"], resistance, places=4)
                self.assertEqual(row["achieved_t_case_c"],
                                 round(25.0 + 3.0 * (resistance + row["theta_sa_c_per_w"]), 2))
                self.assertEqual(row["achieved_c"], row["achieved_t_case_c"])
                self.assertEqual(row["achieved_tj_c"], round(row["achieved_t_case_c"] + 6.0, 2))
        disabled = self.select(doc_id="board-a", handoff_id="thermal-a",
                               include_library=False, include_parametric=False)
        self.assertFalse(disabled["ok"])
        self.assertEqual(disabled["error"], "no heatsink candidates available")

    def test_invalid_candidate_limits_refuse_without_coercion_or_defaulting(self):
        for value in (0, -1, 1.5, True, None, "2", float("nan")):
            with self.subTest(value=value), patch.object(
                    hs, "gather_candidates", side_effect=AssertionError("candidate work")):
                out = self.select(max_candidates=value)
                direct = hs.heatsink_select_impl("U1", tj_target_c=85.0,
                                                max_candidates=value)
            for result in (out, direct):
                self.assertFalse(result["ok"])
                self.assertEqual(result["reason"], "invalid_max_candidates")
                self.assertNotIn("table", result)
        self.assert_source(self.select(max_candidates=100))

    def test_registered_mcp_tool_keeps_legacy_last_study_semantics(self):
        class ToolCollector:
            def tool(inner_self):
                def register(fn):
                    inner_self.select = fn
                    return fn
                return register

        collector = ToolCollector()
        hs.register_heatsink_tools(collector)
        out = json.loads(collector.select("U1", tj_target_c=85.0,
                                          include_library=False, max_candidates=1))
        self.assert_source(out)
        self.assertEqual(len(out["table"]), 1)


if __name__ == "__main__":
    unittest.main()
